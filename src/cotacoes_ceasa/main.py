import argparse
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from cotacoes_ceasa.collectors.ceasa_pe import CeasaPeCollector
from cotacoes_ceasa.config import AppConfig, load_config
from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Cotacao
from cotacoes_ceasa.parsers.ceasa_pe import CeasaPeParser
from cotacoes_ceasa.storage.raw_html import RawArchiveResult, RawHtmlStorage
from cotacoes_ceasa.storage.sqlite import SQLiteStorage


RAW_FILE_PATTERN = re.compile(
    r"^(?P<storage_category>.+)_(?P<downloaded_at>\d{8}_\d{6})\.html$"
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

    if args.source == "ceasa-pe":
        source_config = config.sources[args.source]
        collector = CeasaPeCollector(
            http_client=HttpClient(
                timeout_seconds=args.http_timeout_seconds,
                request_delay_seconds=args.request_delay_seconds,
            ),
            raw_storage=RawHtmlStorage(Path(args.raw_dir)),
            parser=CeasaPeParser(),
            base_url=args.base_url or source_config.base_url,
            reuse_raw_before_request=config.reuse_raw_before_request,
        )

        if args.list_categories:
            for category in collector.discover_categories():
                print(f"{category.slug}\t{category.name}")
            return

        if args.process_raw:
            cotacoes = process_raw_and_report(
                parser=CeasaPeParser(),
                raw_dir=Path(args.raw_dir),
                source_slug=args.source,
                base_url=args.base_url or source_config.base_url,
            )
            storage = SQLiteStorage(Path(args.database_path))
            inserted_count = storage.save_cotacoes(
                cotacoes=cotacoes,
                source_slug=args.source,
                source_name=source_config.name,
                state_name=source_config.state,
                uf=source_config.uf,
                city=source_config.city,
                source_url=source_config.base_url,
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
            storage = SQLiteStorage(Path(args.database_path))
            inserted_count = storage.save_cotacoes(
                cotacoes=cotacoes,
                source_slug=args.source,
                source_name=source_config.name,
                state_name=source_config.state,
                uf=source_config.uf,
                city=source_config.city,
                source_url=source_config.base_url,
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


def collect_and_report(
    collector: CeasaPeCollector,
    target_date: date,
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

    print(f"datas: {', '.join(day.isoformat() for day in target_dates)}")

    for category in categories:
        category_total = 0

        for target_date in target_dates:
            try:
                category_cotacoes = collector._collect_category(category.slug, target_date)
            except Exception as error:
                print(f"{category.slug} {target_date.isoformat()}: erro - {error}")
                continue

            cotacoes.extend(category_cotacoes)
            category_total += len(category_cotacoes)

        print(f"{category.slug}: {category_total} cotacoes")

    return cotacoes


def download_and_report(
    collector: CeasaPeCollector,
    target_date: date,
    quotes_back: int,
) -> list[Path]:
    """Baixa HTML bruto para a janela de datas configurada."""
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


def process_raw_and_report(
    parser: CeasaPeParser,
    raw_dir: Path,
    source_slug: str,
    base_url: str,
) -> list[Cotacao]:
    """Processa HTMLs brutos salvos em disco e retorna cotacoes normalizadas."""
    raw_files = list_raw_files(raw_dir, source_slug)

    if not raw_files:
        raise RuntimeError(f"Nenhum HTML bruto encontrado em {raw_dir / source_slug}.")

    cotacoes: list[Cotacao] = []

    for file_path in raw_files:
        try:
            category_slug, target_date = parse_raw_file_metadata(file_path)
            url_origem = build_category_url(base_url, category_slug, target_date)
            parsed_cotacoes = parser.parse_category(
                file_path.read_text(encoding="utf-8"),
                category_slug,
                url_origem,
            )
        except Exception as error:
            print(f"{file_path}: erro - {error}")
            continue

        cotacoes.extend(parsed_cotacoes)
        print(f"{file_path}: {len(parsed_cotacoes)} cotacoes")

    return cotacoes


def list_raw_files(raw_dir: Path, source_slug: str) -> list[Path]:
    """Lista os HTMLs ativos da fonte, ignorando arquivos arquivados em `old`."""
    source_raw_dir = raw_dir / source_slug

    if not source_raw_dir.exists():
        return []

    return sorted(
        file_path
        for file_path in source_raw_dir.glob("*.html")
        if file_path.is_file()
    )


def parse_raw_file_metadata(file_path: Path) -> tuple[str, date | None]:
    """Extrai categoria e data alvo a partir do nome do HTML bruto."""
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


def resolve_quotation_dates(
    collector: CeasaPeCollector,
    probe_category_slug: str,
    target_date: date,
    quotes_back: int,
) -> list[date]:
    """Descobre datas de cotacao disponiveis voltando a partir da data alvo."""
    if quotes_back < 0:
        raise ValueError("--quotes-back nao pode ser negativo.")

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


def parse_target_date(value: str | None) -> date:
    """Converte data alvo da CLI para date; por padrao usa hoje."""
    if not value:
        return date.today()

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
        choices=["ceasa-pe"],
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
        "--target-date",
        default=config.target_date,
        help="Data alvo da coleta em DD/MM/YYYY ou YYYY-MM-DD.",
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
        "--list-categories",
        action="store_true",
        help="Lista categorias descobertas na fonte sem baixar as tabelas.",
    )

    return parser


if __name__ == "__main__":
    main()
