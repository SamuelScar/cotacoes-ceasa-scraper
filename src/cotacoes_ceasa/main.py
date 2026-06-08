import argparse
from datetime import date, datetime
from pathlib import Path

from cotacoes_ceasa.collection import (
    collect_and_report,
    download_and_report,
    format_target_date,
    format_target_dates,
    resolve_category_target_dates,
    resolve_quotation_dates,
)
from cotacoes_ceasa.config import AppConfig, SourceConfig, load_config
from cotacoes_ceasa.models import Cotacao
from cotacoes_ceasa.prohort import ProhortComplementer, ProhortComplementResult
from cotacoes_ceasa.raw_processing import (
    RAW_CATEGORY_DATE_PATTERN,
    RAW_FILE_PATTERN,
    build_category_url,
    build_raw_source_url,
    list_raw_files,
    parse_raw_file_metadata,
    process_raw_and_report,
    read_raw_file,
)
from cotacoes_ceasa.source_registry import (
    build_registered_collector,
    build_source_parser,
)
from cotacoes_ceasa.storage.raw_html import RawArchiveResult, RawHtmlStorage
from cotacoes_ceasa.storage.sqlite import SQLiteStorage
from cotacoes_ceasa.terminal import TerminalOutput


def main() -> None:
    """Executa comandos de coleta disponiveis no projeto."""
    output = TerminalOutput()

    try:
        run(output)
    except KeyboardInterrupt:
        output.error("Execucao interrompida pelo usuario.")
        output.summary()
        raise SystemExit(130)
    except Exception as error:
        output.error(f"{type(error).__name__}: {error}")
        output.summary()
        raise SystemExit(1)


def run(output: TerminalOutput) -> None:
    """Seleciona e executa o fluxo solicitado pela CLI."""
    config = load_config()
    parser = build_parser(config)
    args = parser.parse_args()

    if args.archive_raw_old:
        output.header(
            "Compactar arquivos antigos",
            (("Diretorio raw", args.raw_dir),),
        )
        archive_raw_old_and_report(RawHtmlStorage(Path(args.raw_dir)), output)
        return

    if args.complement_prohort:
        output.header(
            "Complementar cotacoes com PROHORT",
            (("Banco", args.database_path),),
        )
        complement_prohort_and_report(args, output)
        return

    source_config = config.sources[args.source]
    operation = resolve_source_operation(args)
    output.header(
        operation,
        build_source_execution_details(args, source_config),
    )
    collector = build_collector(
        args=args,
        config=config,
        source_config=source_config,
    )
    source_parser = build_source_parser(args.source)

    if args.list_categories:
        output.section("Categorias")
        output.info("Descobrindo categorias disponiveis.")
        categories = collector.discover_categories()
        output.success(f"{len(categories)} categoria(s) descoberta(s).")

        for category in categories:
            output.success(f"{category.slug} | {category.name}")

        output.summary((("Categorias", len(categories)),))
        return

    if args.process_raw:
        cotacoes = process_raw_and_report(
            parser=source_parser,
            raw_dir=Path(args.raw_dir),
            source_slug=args.source,
            base_url=args.base_url or source_config.base_url,
            output=output,
        )
        output.section("Persistencia")
        output.info(f"Salvando cotacoes em {args.database_path}.")
        inserted_count = save_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
        )
        output.success(f"{inserted_count} registro(s) novo(s) salvo(s).")
        output.summary(
            (
                ("Cotacoes processadas", len(cotacoes)),
                ("Registros novos", inserted_count),
                ("Banco", args.database_path),
            )
        )
        return

    if args.save:
        cotacoes = collect_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
            output=output,
        )
        output.section("Persistencia")
        output.info(f"Salvando cotacoes em {args.database_path}.")
        inserted_count = save_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
        )
        output.success(f"{inserted_count} registro(s) novo(s) salvo(s).")
        output.summary(
            (
                ("Cotacoes extraidas", len(cotacoes)),
                ("Registros novos", inserted_count),
                ("Banco", args.database_path),
            )
        )
        return

    if args.parse:
        cotacoes = collect_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
            output=output,
        )
        output.summary((("Cotacoes extraidas", len(cotacoes)),))
        return

    saved_files = download_and_report(
        collector=collector,
        target_date=parse_target_date(args.target_date),
        quotes_back=args.quotes_back,
        output=output,
    )
    output.summary((("Arquivos salvos", len(saved_files)),))


def build_collector(args, config: AppConfig, source_config: SourceConfig):
    return build_registered_collector(
        source_slug=args.source,
        base_url=args.base_url or source_config.base_url,
        raw_dir=Path(args.raw_dir),
        http_timeout_seconds=args.http_timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        reuse_raw_before_request=config.reuse_raw_before_request,
        target_date=(
            parse_target_date(args.target_date)
            if args.source == "ceasa-pr"
            else None
        ),
        quotes_back=args.quotes_back,
    )


def save_cotacoes(args, cotacoes: list[Cotacao], source_config: SourceConfig) -> int:
    storage = SQLiteStorage(Path(args.database_path))

    return storage.save_cotacoes(
        cotacoes=cotacoes,
        source_slug=args.source,
        source_name=source_config.name,
        state_name=source_config.state,
        uf=source_config.uf,
        city=source_config.city,
        source_url=source_config.base_url,
    )


def resolve_source_operation(args) -> str:
    if args.list_categories:
        return "Listar categorias"

    if args.process_raw:
        return "Processar arquivos brutos"

    if args.save:
        return "Coletar e salvar cotacoes"

    if args.parse:
        return "Coletar e extrair cotacoes"

    return "Baixar arquivos brutos"


def build_source_execution_details(
    args,
    source_config: SourceConfig,
) -> tuple[tuple[str, object], ...]:
    details: list[tuple[str, object]] = [
        ("Fonte", f"{source_config.name} ({args.source})"),
    ]

    if not args.list_categories and not args.process_raw:
        details.extend(
            [
                ("Data limite", args.target_date or "ultima disponivel"),
                ("Cotacoes anteriores", args.quotes_back),
            ]
        )

    if not args.list_categories:
        details.append(("Diretorio raw", args.raw_dir))

    if args.save or args.process_raw:
        details.append(("Banco", args.database_path))

    return tuple(details)


def archive_raw_old_and_report(
    raw_storage: RawHtmlStorage,
    output: TerminalOutput | None = None,
) -> None:
    output = output or TerminalOutput()
    results = raw_storage.archive_old_html_files()
    output.section("Compactacao")

    if not results:
        output.info("Nenhum HTML antigo encontrado para compactar.")
        output.summary((("Arquivos compactados", 0),))
        return

    for result in results:
        output.success(format_archive_result(result))

    output.summary(
        (("Arquivos compactados", sum(result.archived_count for result in results)),)
    )


def format_archive_result(result: RawArchiveResult) -> str:
    return (
        f"{result.source}: {result.archived_count} HTMLs compactados em "
        f"{result.archive_path}"
    )


def complement_prohort_and_report(
    args,
    output: TerminalOutput | None = None,
) -> None:
    output = output or TerminalOutput()
    output.section("Complemento PROHORT")
    output.info("Lendo cotacoes salvas e buscando correspondencias confiaveis.")
    result = ProhortComplementer(
        database_path=Path(args.database_path),
        prohort_url=args.prohort_url,
        timeout_seconds=args.http_timeout_seconds,
    ).complement()

    if not result.database_found:
        output.warning(format_prohort_complement_result(result, args.database_path))
    elif result.candidate_count == 0 and result.fallback_scope_count == 0:
        output.info(format_prohort_complement_result(result, args.database_path))
    else:
        output.success(format_prohort_complement_result(result, args.database_path))

    output.summary(
        (
            ("Linhas lidas", result.scanned_rows),
            ("Cotacoes complementadas", result.updated_count),
            ("Cotacoes inseridas", result.inserted_count),
            ("Sem mapeamento", result.unmapped_count),
            ("Ambiguas", result.ambiguous_count),
        )
    )


def format_prohort_complement_result(
    result: ProhortComplementResult,
    database_path: str,
) -> str:
    if not result.database_found:
        return f"Banco SQLite nao encontrado em {database_path}."

    if result.candidate_count == 0 and result.fallback_scope_count == 0:
        return "Nenhuma cotacao com preco comum vazio encontrada para complementar."

    return (
        f"prohort: {result.scanned_rows} linhas lidas, "
        f"{result.candidate_count} cotacoes candidatas, "
        f"{result.fallback_scope_count} datas/CEASAs com fallback, "
        f"{result.matched_rows} correspondencias confiaveis, "
        f"{result.updated_count} cotacoes complementadas, "
        f"{result.inserted_count} cotacoes faltantes inseridas, "
        f"{result.unmapped_count} sem mapeamento e "
        f"{result.ambiguous_count} ambiguas."
    )


def parse_target_date(value: str | None) -> date | None:
    """Converte a data limite da CLI para date."""
    if not value:
        return None

    for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    raise ValueError("Data invalida. Use DD/MM/YYYY ou YYYY-MM-DD.")


def build_parser(config: AppConfig) -> argparse.ArgumentParser:
    """Cria o parser de argumentos da CLI."""
    parser = argparse.ArgumentParser(
        description="Coleta cotacoes publicas de CEASAs brasileiras."
    )
    parser.add_argument(
        "--source",
        choices=sorted(config.sources),
        default=config.source,
        help="Fonte que sera coletada.",
    )
    parser.add_argument(
        "--raw-dir",
        default=config.raw_dir,
        help="Diretorio onde o HTML bruto sera salvo.",
    )
    parser.add_argument(
        "--database-path",
        default=config.database_path,
        help="Arquivo SQLite onde as cotacoes serao salvas.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Sobrescreve a URL base da fonte informada.",
    )
    parser.add_argument(
        "--http-timeout-seconds",
        default=config.http_timeout_seconds,
        type=int,
        help="Tempo maximo de espera para requisicoes HTTP.",
    )
    parser.add_argument(
        "--request-delay-seconds",
        default=config.request_delay_seconds,
        type=float,
        help="Intervalo minimo entre requisicoes HTTP.",
    )
    parser.add_argument(
        "--prohort-url",
        default=config.prohort_url,
        help="URL do arquivo ProhortDiario.txt usado no complemento.",
    )
    parser.add_argument(
        "--target-date",
        default=config.target_date,
        help=(
            "Data limite da coleta em DD/MM/YYYY ou YYYY-MM-DD. "
            "Quando omitida, busca a ultima cotacao disponivel."
        ),
    )
    parser.add_argument(
        "--quotes-back",
        default=config.quotes_back,
        type=int,
        help="Quantidade de datas de cotacao anteriores para coletar.",
    )
    parser.add_argument(
        "--parse",
        action="store_true",
        help="Extrai cotacoes do HTML baixado, alem de salvar o HTML bruto.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Extrai cotacoes e salva os registros no SQLite.",
    )
    parser.add_argument(
        "--process-raw",
        action="store_true",
        help="Processa HTML bruto salvo em disco e salva os registros no SQLite.",
    )
    parser.add_argument(
        "--archive-raw-old",
        action="store_true",
        help="Compacta HTMLs da pasta old de cada fonte e remove os originais.",
    )
    parser.add_argument(
        "--complement-prohort",
        action="store_true",
        help=(
            "Complementa cotacoes ja salvas usando o PROHORT, "
            "sem sobrescrever campos preenchidos."
        ),
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Lista categorias descobertas na fonte sem baixar as tabelas.",
    )

    return parser


if __name__ == "__main__":
    main()
