from datetime import date, timedelta
from pathlib import Path

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.core.contracts import SourceCollector
from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.http.client import HttpSourceBlockedError


def collect_and_report(
    collector: SourceCollector,
    target_date: date | None,
    quotes_back: int,
    output: TerminalOutput | None = None,
) -> list[Cotacao]:
    """Coleta cotacoes e imprime um resumo por categoria."""
    output = output or TerminalOutput()
    output.section("Coleta por categoria")
    output.info("Descobrindo categorias disponiveis.")
    categories = collector.discover_categories()
    output.success(f"{len(categories)} categoria(s) descoberta(s).")
    output.info("Resolvendo datas de cotacao.")
    target_dates_by_category = resolve_category_target_dates(
        collector,
        categories,
        target_date,
        quotes_back,
        output,
    )
    cotacoes: list[Cotacao] = []

    for category in categories:
        target_dates = target_dates_by_category[category.slug]
        category_total = 0

        if collector.supports_target_dates and getattr(
            collector,
            "category_specific_dates",
            False,
        ):
            output.info(
                f"{category.slug} | datas: {format_target_dates(target_dates)}"
            )

        output.info(
            f"{category.slug} | processando {len(target_dates)} data(s)."
        )

        for target_date in target_dates:
            try:
                category_cotacoes = collector.collect_category(
                    category.slug,
                    target_date,
                )
            except HttpSourceBlockedError:
                raise
            except Exception as error:
                output.warning(
                    f"{category.slug} | {format_target_date(target_date)} | {error}"
                )
                continue

            cotacoes.extend(category_cotacoes)
            category_total += len(category_cotacoes)

        output.success(f"{category.slug} | {category_total} cotacoes.")

    return cotacoes


def download_and_report(
    collector: SourceCollector,
    target_date: date | None,
    quotes_back: int,
    output: TerminalOutput | None = None,
) -> list[Path]:
    """Baixa arquivos brutos para a janela de datas configurada."""
    output = output or TerminalOutput()
    output.section("Download por categoria")
    output.info("Descobrindo categorias disponiveis.")
    categories = collector.discover_categories()
    output.success(f"{len(categories)} categoria(s) descoberta(s).")
    output.info("Resolvendo datas de cotacao.")
    downloaded_files: dict[tuple[str, date | None], Path] = {}
    target_dates_by_category = resolve_category_target_dates(
        collector,
        categories,
        target_date,
        quotes_back,
        output,
        downloaded_files,
    )
    saved_files = list(downloaded_files.values())

    for category in categories:
        target_dates = target_dates_by_category[category.slug]
        output.info(f"{category.slug} | baixando {len(target_dates)} arquivo(s).")

        for target_date in target_dates:
            downloaded_file = downloaded_files.get((category.slug, target_date))

            if downloaded_file is not None:
                continue

            try:
                file_path = collector.download_category(category.slug, target_date)
            except HttpSourceBlockedError:
                raise
            except Exception as error:
                output.warning(
                    f"{category.slug} | {format_target_date(target_date)} | {error}"
                )
                continue

            saved_files.append(file_path)
            output.success(f"{category.slug} | salvo em {file_path}")

    return saved_files


def resolve_quotation_dates(
    collector: SourceCollector,
    probe_category_slug: str,
    target_date: date | None,
    quotes_back: int,
    downloaded_files: dict[tuple[str, date | None], Path] | None = None,
    output: TerminalOutput | None = None,
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
    max_attempts = max(expected_count * 4, 30)

    for _ in range(max_attempts):
        try:
            cotacoes = collector.collect_category(
                probe_category_slug,
                candidate_date,
                save_raw=False,
            )
        except HttpSourceBlockedError:
            raise
        except Exception:
            candidate_date -= timedelta(days=1)
            continue

        quotation_dates = {
            cotacao.data_cotacao
            for cotacao in cotacoes
            if cotacao.data_cotacao is not None
        }

        new_dates: list[date] = []

        for quotation_date in sorted(quotation_dates, reverse=True):
            if quotation_date not in found_dates:
                found_dates.append(quotation_date)

                if len(found_dates) <= expected_count:
                    new_dates.append(quotation_date)

        if downloaded_files is not None:
            for quotation_date in new_dates:
                download_key = (probe_category_slug, quotation_date)

                if download_key in downloaded_files:
                    continue

                try:
                    downloaded_file = collector.download_category(
                        probe_category_slug,
                        quotation_date,
                    )
                    downloaded_files[download_key] = downloaded_file

                    if output is not None:
                        output.success(
                            f"{probe_category_slug} | salvo em {downloaded_file}"
                        )
                except HttpSourceBlockedError:
                    raise
                except Exception:
                    continue

        if len(found_dates) >= expected_count:
            return found_dates[:expected_count]

        candidate_date = (
            min(quotation_dates) - timedelta(days=1)
            if quotation_dates
            else candidate_date - timedelta(days=1)
        )

    raise RuntimeError(
        f"Nao foi possivel encontrar {expected_count} datas de cotacao "
        f"apos {max_attempts} tentativas."
    )


def resolve_category_target_dates(
    collector: SourceCollector,
    categories: tuple[Category, ...],
    target_date: date | None,
    quotes_back: int,
    output: TerminalOutput | None = None,
    downloaded_files: dict[tuple[str, date | None], Path] | None = None,
) -> dict[str, list[date | None]]:
    """Resolve as datas que devem ser consultadas para cada categoria."""
    if getattr(collector, "category_specific_dates", False):
        target_dates_by_category: dict[str, list[date | None]] = {}

        for category in categories:
            if output is not None:
                output.info(f"{category.slug} | buscando datas disponiveis.")

            target_dates_by_category[category.slug] = resolve_quotation_dates(
                collector=collector,
                probe_category_slug=category.slug,
                target_date=target_date,
                quotes_back=quotes_back,
                downloaded_files=downloaded_files,
                output=output,
            )

        return target_dates_by_category

    target_dates = resolve_quotation_dates(
        collector=collector,
        probe_category_slug=categories[0].slug,
        target_date=target_date,
        quotes_back=quotes_back,
        downloaded_files=downloaded_files,
        output=output,
    )

    if collector.supports_target_dates:
        (output or TerminalOutput()).info(
            f"Datas selecionadas: {format_target_dates(target_dates)}"
        )

    return {category.slug: target_dates for category in categories}


def format_target_dates(target_dates: list[date | None]) -> str:
    return ", ".join(format_target_date(target_date) for target_date in target_dates)


def format_target_date(target_date: date | None) -> str:
    return target_date.isoformat() if target_date is not None else "ultima disponivel"
