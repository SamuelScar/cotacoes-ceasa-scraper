from copy import copy
from pathlib import Path
from time import perf_counter

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import (
    format_incremental_history,
    format_quotes_back,
    parse_target_date,
)
from cotacoes_ceasa.config import AppConfig, SourceConfig
from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.sources.registry import (
    build_registered_collector,
    build_source_parser,
)
from cotacoes_ceasa.parsers.pdf import configure_pdf_text_cache
from cotacoes_ceasa.storage.sqlite import SQLiteStorage
from cotacoes_ceasa.workflows.collection import (
    PartialDownloadError,
    collect_and_report,
    download_and_report,
)
from cotacoes_ceasa.workflows.raw_processing import process_raw_and_report


def run_source(
    args,
    config: AppConfig,
    output: TerminalOutput,
    show_summary: bool = True,
    raw_files: list[Path] | None = None,
) -> list[Path] | None:
    """Executa a operacao solicitada para uma fonte."""
    source_config = config.sources[args.source]
    requested_quotes_back = args.quotes_back
    args = copy(args)
    database_path = (
        Path(args.database_path)
        if getattr(args, "database_path", None)
        else None
    )
    args.quotes_back = resolve_effective_quotes_back(
        source_config,
        args.quotes_back,
    )
    limited_history = source_config.limited_history
    operation = resolve_source_operation(args)
    output.header(
        operation,
        build_source_execution_details(
            args,
            source_config,
            config.incremental_history,
            requested_quotes_back,
        ),
    )
    collector = build_collector(
        args=args,
        config=config,
        source_config=source_config,
    )
    configure_pdf_text_cache(Path(args.pdf_text_cache_dir))
    source_parser = build_source_parser(args.source)

    if args.list_categories:
        output.section("Categorias")
        output.info("Descobrindo categorias disponiveis.")
        categories = collector.discover_categories()
        output.success(f"{len(categories)} categoria(s) descoberta(s).")

        for category in categories:
            output.success(f"{category.slug} | {category.name}")

        complete_source_operation(
            output,
            show_summary,
            (("Categorias", len(categories)),),
        )
        return

    if args.process_raw:
        cotacoes = process_raw_and_report(
            parser=source_parser,
            raw_dir=Path(args.raw_dir),
            source_slug=args.source,
            base_url=args.base_url or source_config.base_url,
            database_path=Path(args.database_path),
            pdf_text_cache_dir=Path(args.pdf_text_cache_dir),
            force_reprocess=args.force_reprocess,
            raw_detail_report=args.raw_detail_report,
            output=output,
            raw_files=raw_files,
        )
        output.section("Persistencia")
        output.info(f"Salvando cotacoes em {args.database_path}.")
        persistence_started_at = perf_counter()
        inserted_count, rejected_count = save_valid_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
            output=output,
        )
        persistence_seconds = perf_counter() - persistence_started_at
        output.success(f"{inserted_count} registro(s) novo(s) salvo(s).")
        complete_source_operation(
            output,
            show_summary,
            (
                ("Cotacoes processadas", len(cotacoes)),
                ("Registros novos", inserted_count),
                ("Cotacoes rejeitadas", rejected_count),
                ("Tempo persistencia SQLite (s)", f"{persistence_seconds:.2f}"),
                ("Banco", args.database_path),
            ),
        )
        return None

    if args.save:
        cotacoes = collect_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
            raw_dir=Path(args.raw_dir),
            source_slug=args.source,
            incremental_history=config.incremental_history,
            output=output,
            limited_history=limited_history,
            database_path=database_path,
        )
        output.section("Persistencia")
        output.info(f"Salvando cotacoes em {args.database_path}.")
        persistence_started_at = perf_counter()
        inserted_count, rejected_count = save_valid_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
            output=output,
        )
        persistence_seconds = perf_counter() - persistence_started_at
        output.success(f"{inserted_count} registro(s) novo(s) salvo(s).")
        complete_source_operation(
            output,
            show_summary,
            (
                ("Cotacoes extraidas", len(cotacoes)),
                ("Registros novos", inserted_count),
                ("Cotacoes rejeitadas", rejected_count),
                ("Tempo persistencia SQLite (s)", f"{persistence_seconds:.2f}"),
                ("Banco", args.database_path),
            ),
        )
        return None

    if args.download_only:
        saved_files = download_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
            raw_dir=Path(args.raw_dir),
            source_slug=args.source,
            incremental_history=config.incremental_history,
            output=output,
            limited_history=limited_history,
            database_path=database_path,
        )
        complete_source_operation(
            output,
            show_summary,
            (("Arquivos salvos", len(saved_files)),),
        )
        return saved_files

    cotacoes = collect_and_report(
        collector=collector,
        target_date=parse_target_date(args.target_date),
        quotes_back=args.quotes_back,
        raw_dir=Path(args.raw_dir),
        source_slug=args.source,
        incremental_history=config.incremental_history,
        output=output,
        limited_history=limited_history,
        database_path=database_path,
    )
    complete_source_operation(
        output,
        show_summary,
        (("Cotacoes extraidas", len(cotacoes)),),
    )
    return None


def run_source_download_and_process(
    args,
    config: AppConfig,
    output: TerminalOutput,
) -> None:
    """Baixa e processa uma unica fonte selecionada explicitamente."""
    download_args = copy(args)
    download_args.download_and_process = False
    download_args.download_only = True
    download_args.process_raw = False
    download_args.save = False
    download_error: PartialDownloadError | None = None

    try:
        downloaded_files = run_source(download_args, config, output)
    except PartialDownloadError as error:
        if not error.saved_files:
            raise

        download_error = error
        downloaded_files = error.saved_files
        output.warning(
            f"{args.source} | {len(error.saved_files)} arquivo(s) baixado(s) "
            "antes da falha serao processados na persistencia."
        )

    process_args = copy(args)
    process_args.download_and_process = False
    process_args.download_only = False
    process_args.process_raw = True
    process_args.save = False
    run_source(process_args, config, output, raw_files=downloaded_files or [])

    if download_error is not None:
        raise download_error.original_error


def complete_source_operation(
    output: TerminalOutput,
    show_summary: bool,
    rows: tuple[tuple[str, object], ...],
) -> None:
    if show_summary:
        output.summary(rows)
        return

    output.report_summary(rows)


def resolve_effective_quotes_back(
    source_config: SourceConfig,
    quotes_back: int | None,
) -> int | None:
    max_quotes_back = source_config.max_quotes_back

    if max_quotes_back is None or quotes_back == 0:
        return quotes_back

    if quotes_back is None:
        return max_quotes_back

    return min(quotes_back, max_quotes_back)


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


def save_valid_cotacoes(
    args,
    cotacoes: list[Cotacao],
    source_config: SourceConfig,
    output: TerminalOutput,
) -> tuple[int, int]:
    valid_cotacoes, rejection_counts = split_valid_cotacoes(cotacoes)
    rejected_count = sum(rejection_counts.values())

    if rejected_count:
        output.warning(
            f"{args.source} | {rejected_count} cotacao(oes) rejeitada(s) "
            f"antes da persistencia: {format_rejection_counts(rejection_counts)}."
        )

    if not valid_cotacoes:
        return 0, rejected_count

    inserted_count = save_cotacoes(
        args=args,
        cotacoes=valid_cotacoes,
        source_config=source_config,
    )

    return inserted_count, rejected_count


def split_valid_cotacoes(
    cotacoes: list[Cotacao],
) -> tuple[list[Cotacao], dict[str, int]]:
    valid_cotacoes: list[Cotacao] = []
    rejection_counts: dict[str, int] = {}

    for cotacao in cotacoes:
        reason = reject_cotacao_reason(cotacao)

        if reason is None:
            valid_cotacoes.append(cotacao)
            continue

        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1

    return valid_cotacoes, rejection_counts


def reject_cotacao_reason(cotacao: Cotacao) -> str | None:
    if cotacao.data_cotacao is None:
        return "data ausente"

    prices = (cotacao.preco_minimo, cotacao.preco_comum, cotacao.preco_maximo)

    if all(price is None for price in prices):
        return "preco ausente"

    if any(price is not None and price < 0 for price in prices):
        return "preco negativo"

    return None


def format_rejection_counts(rejection_counts: dict[str, int]) -> str:
    return ", ".join(
        f"{count} {reason}"
        for reason, count in sorted(rejection_counts.items())
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

    if args.download_only:
        return "Baixar arquivos brutos"

    return "Coletar e extrair cotacoes"


def build_source_execution_details(
    args,
    source_config: SourceConfig,
    incremental_history: bool,
    requested_quotes_back: int | None = None,
) -> tuple[tuple[str, object], ...]:
    details: list[tuple[str, object]] = [
        ("Fonte", f"{source_config.name} ({args.source})"),
    ]

    if not args.list_categories and not args.process_raw:
        details.append(("Data limite", args.target_date or "ultima disponivel"))

        if requested_quotes_back != args.quotes_back:
            details.extend(
                [
                    (
                        "Cotacoes anteriores solicitadas",
                        format_quotes_back(requested_quotes_back),
                    ),
                    (
                        "Cotacoes anteriores efetivas",
                        format_quotes_back(args.quotes_back),
                    ),
                ]
            )
        else:
            details.append(
                ("Cotacoes anteriores", format_quotes_back(args.quotes_back))
            )

        if source_config.limited_history:
            details.append(("Historico limitado pela fonte", "sim"))
            max_quotes_back = source_config.max_quotes_back

            if max_quotes_back is not None:
                details.append(
                    ("Limite de cotacoes anteriores da fonte", max_quotes_back)
                )

        details.append(
            (
                "Historico incremental",
                format_incremental_history(
                    incremental_history,
                    args.target_date,
                    args.quotes_back,
                ),
            )
        )

    if not args.list_categories:
        details.append(("Diretorio raw", args.raw_dir))

    if args.save or args.process_raw:
        details.append(("Banco", args.database_path))

    return tuple(details)
