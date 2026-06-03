import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from cotacoes_ceasa.collectors.ceasa_campinas import (
    CEASA_CAMPINAS_HEADERS,
    CeasaCampinasCollector,
)
from cotacoes_ceasa.collectors.ceasa_ce import CEASA_CE_HEADERS, CeasaCeCollector
from cotacoes_ceasa.collectors.ceasa_go import CEASA_GO_HEADERS, CeasaGoCollector
from cotacoes_ceasa.collectors.ceasa_mg import CeasaMgCollector
from cotacoes_ceasa.collectors.ceasa_pe import CeasaPeCollector
from cotacoes_ceasa.collectors.ceasa_pr import CEASA_PR_HEADERS, CeasaPrCollector
from cotacoes_ceasa.config import AppConfig, SourceConfig, load_config
from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Cotacao
from cotacoes_ceasa.parsers.ceasa_campinas import CeasaCampinasParser
from cotacoes_ceasa.parsers.ceasa_ce import CeasaCeParser
from cotacoes_ceasa.parsers.ceasa_go import CeasaGoParser
from cotacoes_ceasa.parsers.ceasa_mg import CeasaMgParser
from cotacoes_ceasa.parsers.ceasa_pe import CeasaPeParser
from cotacoes_ceasa.parsers.ceasa_pr import CeasaPrParser
from cotacoes_ceasa.prohort import ProhortComplementer, ProhortComplementResult
from cotacoes_ceasa.storage.raw_html import RawArchiveResult, RawHtmlStorage
from cotacoes_ceasa.storage.sqlite import SQLiteStorage


RAW_FILE_PATTERN = re.compile(
    r"^(?P<storage_category>.+)_(?P<downloaded_at>\d{8}_\d{6})\.(?:html|pdf)$"
)
RAW_CATEGORY_DATE_PATTERN = re.compile(r"_(?P<target_date>\d{4}-\d{2}-\d{2})$")


def main() -> None:
    """Executa comandos de coleta disponiveis no projeto."""
    config = load_config()
    parser = build_parser(config)
    args = parser.parse_args()

    if args.archive_raw_old:
        archive_raw_old_and_report(RawHtmlStorage(Path(args.raw_dir)))
        return

    if args.complement_prohort:
        complement_prohort_and_report(args)
        return

    source_config = config.sources[args.source]
    collector = build_collector(
        args=args,
        config=config,
        source_config=source_config,
    )
    parser = build_source_parser(args.source)

    if args.list_categories:
        for category in collector.discover_categories():
            print(f"{category.slug}\t{category.name}")
        return

    if args.process_raw:
        cotacoes = process_raw_and_report(
            parser=parser,
            raw_dir=Path(args.raw_dir),
            source_slug=args.source,
            base_url=args.base_url or source_config.base_url,
        )
        inserted_count = save_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
        )
        print(
            f"total: {len(cotacoes)} cotacoes processadas. "
            f"{inserted_count} registros novos salvos em {args.database_path}."
        )
        return

    if args.save:
        cotacoes = collect_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
        )
        inserted_count = save_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
        )
        print(
            f"total: {len(cotacoes)} cotacoes extraidas. "
            f"{inserted_count} registros novos salvos em {args.database_path}."
        )
        return

    if args.parse:
        cotacoes = collect_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
        )
        print(f"total: {len(cotacoes)} cotacoes extraidas.")
        return

    saved_files = download_and_report(
        collector=collector,
        target_date=parse_target_date(args.target_date),
        quotes_back=args.quotes_back,
    )

    for file_path in saved_files:
        print(file_path)


def build_collector(args, config: AppConfig, source_config: SourceConfig):
    http_client = HttpClient(
        timeout_seconds=args.http_timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
    )
    raw_storage = RawHtmlStorage(Path(args.raw_dir))
    base_url = args.base_url or source_config.base_url

    if args.source == "ceasa-pe":
        return CeasaPeCollector(
            http_client=http_client,
            raw_storage=raw_storage,
            parser=CeasaPeParser(),
            base_url=base_url,
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

    if args.source == "ceasa-mg":
        if args.quotes_back:
            raise ValueError("CEASA-MG nao suporta cotacoes anteriores.")

        return CeasaMgCollector(
            http_client=http_client,
            raw_storage=raw_storage,
            parser=CeasaMgParser(),
            base_url=base_url,
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

    if args.source == "ceasa-pr":
        ceasa_pr_http_client = HttpClient(
            timeout_seconds=args.http_timeout_seconds,
            request_delay_seconds=args.request_delay_seconds,
            headers=CEASA_PR_HEADERS,
        )

        return CeasaPrCollector(
            http_client=ceasa_pr_http_client,
            raw_storage=raw_storage,
            parser=CeasaPrParser(),
            base_url=base_url,
            target_date=parse_target_date(args.target_date),
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

    if args.source == "ceasa-campinas":
        ceasa_campinas_http_client = HttpClient(
            timeout_seconds=args.http_timeout_seconds,
            request_delay_seconds=args.request_delay_seconds,
            headers=CEASA_CAMPINAS_HEADERS,
        )

        return CeasaCampinasCollector(
            http_client=ceasa_campinas_http_client,
            raw_storage=raw_storage,
            parser=CeasaCampinasParser(),
            base_url=base_url,
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

    if args.source == "ceasa-go":
        ceasa_go_http_client = HttpClient(
            timeout_seconds=args.http_timeout_seconds,
            request_delay_seconds=args.request_delay_seconds,
            headers=CEASA_GO_HEADERS,
        )

        return CeasaGoCollector(
            http_client=ceasa_go_http_client,
            raw_storage=raw_storage,
            parser=CeasaGoParser(),
            base_url=base_url,
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

    if args.source == "ceasa-ce":
        if args.quotes_back:
            raise ValueError("CEASA-CE nao suporta cotacoes anteriores.")

        ceasa_ce_http_client = HttpClient(
            timeout_seconds=args.http_timeout_seconds,
            request_delay_seconds=args.request_delay_seconds,
            headers=CEASA_CE_HEADERS,
        )

        return CeasaCeCollector(
            http_client=ceasa_ce_http_client,
            raw_storage=raw_storage,
            parser=CeasaCeParser(),
            base_url=base_url,
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

    raise ValueError(f"Fonte nao suportada: {args.source}")


def build_source_parser(source_slug: str):
    if source_slug == "ceasa-pe":
        return CeasaPeParser()

    if source_slug == "ceasa-mg":
        return CeasaMgParser()

    if source_slug == "ceasa-pr":
        return CeasaPrParser()

    if source_slug == "ceasa-campinas":
        return CeasaCampinasParser()

    if source_slug == "ceasa-go":
        return CeasaGoParser()

    if source_slug == "ceasa-ce":
        return CeasaCeParser()

    raise ValueError(f"Fonte nao suportada: {source_slug}")


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


def collect_and_report(
    collector,
    target_date: date | None,
    quotes_back: int,
) -> list[Cotacao]:
    """Coleta cotacoes e imprime um resumo por categoria."""
    categories = collector.discover_categories()
    target_dates = resolve_quotation_dates(
        collector=collector,
        probe_category_slug=categories[0].slug,
        target_date=target_date,
        quotes_back=quotes_back,
    )
    cotacoes: list[Cotacao] = []

    if collector.supports_target_dates:
        print(f"datas: {format_target_dates(target_dates)}")

    for category in categories:
        category_total = 0

        for target_date in target_dates:
            try:
                category_cotacoes = collector._collect_category(category.slug, target_date)
            except Exception as error:
                print(f"{category.slug} {format_target_date(target_date)}: erro - {error}")
                continue

            cotacoes.extend(category_cotacoes)
            category_total += len(category_cotacoes)

        print(f"{category.slug}: {category_total} cotacoes")

    return cotacoes


def download_and_report(
    collector,
    target_date: date | None,
    quotes_back: int,
) -> list[Path]:
    """Baixa arquivos brutos para a janela de datas configurada."""
    categories = collector.discover_categories()
    target_dates = resolve_quotation_dates(
        collector=collector,
        probe_category_slug=categories[0].slug,
        target_date=target_date,
        quotes_back=quotes_back,
    )
    saved_files: list[Path] = []

    for category in categories:
        for target_date in target_dates:
            saved_files.append(collector._download_category(category.slug, target_date))

    return saved_files


def archive_raw_old_and_report(raw_storage: RawHtmlStorage) -> None:
    results = raw_storage.archive_old_html_files()

    if not results:
        print("Nenhum HTML antigo encontrado para compactar.")
        return

    for result in results:
        print(format_archive_result(result))


def format_archive_result(result: RawArchiveResult) -> str:
    return (
        f"{result.source}: {result.archived_count} HTMLs compactados em "
        f"{result.archive_path}"
    )


def complement_prohort_and_report(args) -> None:
    result = ProhortComplementer(
        database_path=Path(args.database_path),
        prohort_url=args.prohort_url,
        timeout_seconds=args.http_timeout_seconds,
    ).complement()

    print(format_prohort_complement_result(result, args.database_path))


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


def process_raw_and_report(
    parser,
    raw_dir: Path,
    source_slug: str,
    base_url: str,
) -> list[Cotacao]:
    """Processa arquivos brutos salvos em disco e retorna cotacoes normalizadas."""
    raw_files = list_raw_files(raw_dir, source_slug)

    if not raw_files:
        raise RuntimeError(f"Nenhum arquivo bruto encontrado em {raw_dir / source_slug}.")

    cotacoes: list[Cotacao] = []

    for file_path in raw_files:
        try:
            category_slug, target_date = parse_raw_file_metadata(file_path)
            url_origem = build_raw_source_url(
                source_slug,
                base_url,
                category_slug,
                target_date,
            )
            parsed_cotacoes = parser.parse_category(
                read_raw_file(file_path),
                category_slug,
                url_origem,
            )
        except Exception as error:
            print(f"{file_path}: erro - {error}")
            continue

        cotacoes.extend(parsed_cotacoes)
        print(f"{file_path}: {len(parsed_cotacoes)} cotacoes")

    return cotacoes


def read_raw_file(file_path: Path) -> bytes | str:
    """Le arquivo bruto em texto ou bytes conforme a extensao."""
    if file_path.suffix.lower() == ".pdf":
        return file_path.read_bytes()

    return file_path.read_text(encoding="utf-8")


def list_raw_files(raw_dir: Path, source_slug: str) -> list[Path]:
    """Lista os raws ativos da fonte, ignorando arquivos arquivados em `old`."""
    source_raw_dir = raw_dir / source_slug

    if not source_raw_dir.exists():
        return []

    return sorted(
        file_path
        for file_path in source_raw_dir.glob("*.*")
        if file_path.is_file() and file_path.suffix.lower() in {".html", ".pdf"}
    )


def parse_raw_file_metadata(file_path: Path) -> tuple[str, date | None]:
    """Extrai categoria e data limite a partir do nome do arquivo bruto."""
    match = RAW_FILE_PATTERN.match(file_path.name)

    if match is None:
        raise ValueError("Nome de arquivo bruto fora do padrao esperado.")

    storage_category = match.group("storage_category")
    date_match = RAW_CATEGORY_DATE_PATTERN.search(storage_category)

    if date_match is None:
        return storage_category, None

    category_slug = storage_category[: date_match.start()]
    target_date = datetime.strptime(date_match.group("target_date"), "%Y-%m-%d").date()

    return category_slug, target_date


def build_category_url(
    base_url: str,
    category_slug: str,
    target_date: date | None,
) -> str:
    """Monta a URL original da categoria usada na coleta."""
    url = f"{base_url.rstrip('/')}/{category_slug}"

    if target_date is None:
        return url

    params = urlencode({"data": target_date.strftime("%d/%m/%Y")})

    return f"{url}?{params}"


def build_raw_source_url(
    source_slug: str,
    base_url: str,
    category_slug: str,
    target_date: date | None,
) -> str:
    if source_slug == "ceasa-pe":
        return build_category_url(base_url, category_slug, target_date)

    if source_slug == "ceasa-pr" and target_date is not None:
        return f"{base_url.rstrip('/')}-{target_date.year}"

    return base_url


def resolve_quotation_dates(
    collector,
    probe_category_slug: str,
    target_date: date | None,
    quotes_back: int,
) -> list[date | None]:
    """Descobre datas de cotacao disponiveis voltando a partir da data limite."""
    if quotes_back < 0:
        raise ValueError("--quotes-back nao pode ser negativo.")

    if target_date is None and quotes_back == 0:
        return [None]

    target_date = target_date or date.today()

    if quotes_back == 0:
        return [target_date]

    expected_count = quotes_back + 1
    found_dates: list[date] = []
    candidate_date = target_date
    max_calendar_days = max(expected_count * 4, 30)

    for _ in range(max_calendar_days):
        try:
            cotacoes = collector._collect_category(
                probe_category_slug,
                candidate_date,
                save_raw=False,
            )
        except Exception:
            candidate_date -= timedelta(days=1)
            continue

        quotation_dates = {
            cotacao.data_cotacao
            for cotacao in cotacoes
            if cotacao.data_cotacao is not None
        }

        for quotation_date in sorted(quotation_dates, reverse=True):
            if quotation_date not in found_dates:
                found_dates.append(quotation_date)

        if len(found_dates) >= expected_count:
            return found_dates[:expected_count]

        candidate_date -= timedelta(days=1)

    raise RuntimeError(
        f"Nao foi possivel encontrar {expected_count} datas de cotacao "
        f"em {max_calendar_days} dias corridos."
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


def format_target_dates(target_dates: list[date | None]) -> str:
    return ", ".join(format_target_date(target_date) for target_date in target_dates)


def format_target_date(target_date: date | None) -> str:
    return target_date.isoformat() if target_date is not None else "ultima disponivel"


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
