import argparse
from datetime import date, datetime, timedelta
from pathlib import Path

from cotacoes_ceasa.collectors.ceasa_pe import CeasaPeCollector
from cotacoes_ceasa.config import AppConfig, load_config
from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_pe import CeasaPeParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage
from cotacoes_ceasa.storage.sqlite import SQLiteStorage


def main() -> None:
    """Executa comandos de coleta disponiveis no projeto."""
    config = load_config()
    parser = build_parser(config)
    args = parser.parse_args()

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
        )

        if args.list_categories:
            for category in collector.discover_categories():
                print(f"{category.slug}\t{category.name}")
            return

        if args.save:
            cotacoes = collect_and_report(
                collector=collector,
                category_slug=args.category,
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
                category_slug=args.category,
                target_date=parse_target_date(args.target_date),
                quotes_back=args.quotes_back,
            )
            print(f"total: {len(cotacoes)} cotacoes extraidas.")
            return

        saved_files = download_and_report(
            collector=collector,
            category_slug=args.category,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
        )

        for file_path in saved_files:
            print(file_path)


def collect_and_report(
    collector: CeasaPeCollector,
    category_slug: str,
    target_date: date,
    quotes_back: int,
) -> list[Cotacao]:
    """Coleta cotacoes e imprime um resumo por categoria."""
    categories = resolve_categories(collector, category_slug)
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
                category_cotacoes = collector.collect_category(category.slug, target_date)
            except Exception as error:
                print(f"{category.slug} {target_date.isoformat()}: erro - {error}")
                continue

            cotacoes.extend(category_cotacoes)
            category_total += len(category_cotacoes)

        print(f"{category.slug}: {category_total} cotacoes")

    return cotacoes


def download_and_report(
    collector: CeasaPeCollector,
    category_slug: str,
    target_date: date,
    quotes_back: int,
) -> list[Path]:
    """Baixa HTML bruto para a janela de datas configurada."""
    categories = resolve_categories(collector, category_slug)
    target_dates = resolve_quotation_dates(
        collector=collector,
        probe_category_slug=categories[0].slug,
        target_date=target_date,
        quotes_back=quotes_back,
    )
    saved_files: list[Path] = []

    for category in categories:
        for target_date in target_dates:
            saved_files.append(collector.download_category(category.slug, target_date))

    return saved_files


def resolve_categories(
    collector: CeasaPeCollector,
    category_slug: str,
) -> tuple[Category, ...]:
    """Resolve uma categoria especifica ou todas as categorias descobertas."""
    if category_slug == "todas":
        return collector.discover_categories()

    return (Category(slug=category_slug, name=category_slug),)


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
            cotacoes = collector.collect_category(
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
        "--category",
        default=config.category,
        help="Categoria da CEASA-PE ou 'todas'.",
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
        "--list-categories",
        action="store_true",
        help="Lista categorias descobertas na fonte sem baixar as tabelas.",
    )

    return parser


if __name__ == "__main__":
    main()
