from datetime import date, timedelta
from pathlib import Path

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.core.contracts import SourceCollector
from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.http.client import HttpRequestError, HttpSourceBlockedError
from cotacoes_ceasa.workflows.raw_processing import find_oldest_raw_target_date


INFINITE_HISTORY_EMPTY_ATTEMPTS = 366


def collect_and_report(
    collector: SourceCollector,
    target_date: date | None,
    quotes_back: int | None,
    raw_dir: Path,
    source_slug: str,
    incremental_history: bool = False,
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
        raw_dir,
        source_slug,
        incremental_history,
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
            except (HttpRequestError, HttpSourceBlockedError):
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
    quotes_back: int | None,
    raw_dir: Path,
    source_slug: str,
    incremental_history: bool = False,
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
        raw_dir,
        source_slug,
        incremental_history,
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
            except (HttpRequestError, HttpSourceBlockedError):
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
    quotes_back: int | None,
    allow_empty_history: bool = False,
    downloaded_files: dict[tuple[str, date | None], Path] | None = None,
    output: TerminalOutput | None = None,
) -> list[date | None]:
    """Descobre datas de cotacao disponiveis voltando a partir da data limite."""
    if quotes_back is not None and quotes_back < 0:
        raise ValueError("--quotes-back nao pode ser negativo.")

    if target_date is None and quotes_back == 0:
        return [None]

    target_date = target_date or date.today()

    if quotes_back == 0:
        return [target_date]

    expected_count = quotes_back + 1 if quotes_back is not None else None
    found_dates: list[date] = []
    candidate_date = target_date
    attempt_count = 0
    empty_attempts = 0
    max_attempts = (
        max(expected_count * 4, 30) if expected_count is not None else None
    )

    while max_attempts is None or attempt_count < max_attempts:
        attempt_count += 1

        try:
            cotacoes = collector.collect_category(
                probe_category_slug,
                candidate_date,
                save_raw=False,
            )
        except (HttpRequestError, HttpSourceBlockedError):
            raise
        except Exception:
            empty_attempts += 1

            if _infinite_history_exhausted(
                expected_count,
                found_dates,
                empty_attempts,
                output,
                probe_category_slug,
                allow_empty_history,
            ):
                return found_dates

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

                if expected_count is None or len(found_dates) <= expected_count:
                    new_dates.append(quotation_date)

        empty_attempts = 0 if new_dates else empty_attempts + 1

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
                except (HttpRequestError, HttpSourceBlockedError):
                    raise
                except Exception:
                    continue

        if expected_count is not None and len(found_dates) >= expected_count:
            return found_dates[:expected_count]

        if _infinite_history_exhausted(
            expected_count,
            found_dates,
            empty_attempts,
            output,
            probe_category_slug,
            allow_empty_history,
        ):
            return found_dates

        candidate_date = (
            min(quotation_dates) - timedelta(days=1)
            if quotation_dates
            else candidate_date - timedelta(days=1)
        )

    raise RuntimeError(
        f"Nao foi possivel encontrar {expected_count} datas de cotacao "
        f"apos {max_attempts} tentativas."
    )


def _infinite_history_exhausted(
    expected_count: int | None,
    found_dates: list[date],
    empty_attempts: int,
    output: TerminalOutput | None,
    category_slug: str,
    allow_empty_history: bool,
) -> bool:
    if (
        expected_count is not None
        or empty_attempts < INFINITE_HISTORY_EMPTY_ATTEMPTS
    ):
        return False

    if not found_dates:
        if allow_empty_history:
            if output is not None:
                output.info(
                    f"{category_slug} | nenhum dado historico adicional encontrado "
                    f"apos {INFINITE_HISTORY_EMPTY_ATTEMPTS} tentativa(s)."
                )

            return True

        raise RuntimeError(
            f"Nenhuma data de cotacao encontrada para {category_slug} apos "
            f"{INFINITE_HISTORY_EMPTY_ATTEMPTS} tentativas."
        )

    if output is not None:
        output.info(
            f"{category_slug} | historico encerrado apos "
            f"{INFINITE_HISTORY_EMPTY_ATTEMPTS} tentativa(s) sem data mais antiga."
        )

    return True


def resolve_category_target_dates(
    collector: SourceCollector,
    categories: tuple[Category, ...],
    target_date: date | None,
    quotes_back: int | None,
    raw_dir: Path,
    source_slug: str,
    incremental_history: bool,
    output: TerminalOutput | None = None,
    downloaded_files: dict[tuple[str, date | None], Path] | None = None,
) -> dict[str, list[date | None]]:
    """Resolve as datas que devem ser consultadas para cada categoria."""
    if getattr(collector, "category_specific_dates", False):
        target_dates_by_category: dict[str, list[date | None]] = {}

        for category in categories:
            if output is not None:
                output.info(f"{category.slug} | buscando datas disponiveis.")

            (
                category_target_date,
                allow_empty_history,
            ) = resolve_incremental_target_date(
                raw_dir=raw_dir,
                source_slug=source_slug,
                category_slug=category.slug,
                target_date=target_date,
                quotes_back=quotes_back,
                incremental_history=incremental_history,
                output=output,
            )
            target_dates_by_category[category.slug] = resolve_quotation_dates(
                collector=collector,
                probe_category_slug=category.slug,
                target_date=category_target_date,
                quotes_back=quotes_back,
                allow_empty_history=allow_empty_history,
                downloaded_files=downloaded_files,
                output=output,
            )

        return target_dates_by_category

    target_date, allow_empty_history = resolve_incremental_target_date(
        raw_dir=raw_dir,
        source_slug=source_slug,
        category_slug=None,
        target_date=target_date,
        quotes_back=quotes_back,
        incremental_history=incremental_history,
        output=output,
    )
    target_dates = resolve_quotation_dates(
        collector=collector,
        probe_category_slug=categories[0].slug,
        target_date=target_date,
        quotes_back=quotes_back,
        allow_empty_history=allow_empty_history,
        downloaded_files=downloaded_files,
        output=output,
    )

    if collector.supports_target_dates:
        (output or TerminalOutput()).info(
            f"Datas selecionadas: {format_target_dates(target_dates)}"
        )

    return {category.slug: target_dates for category in categories}


def resolve_incremental_target_date(
    raw_dir: Path,
    source_slug: str,
    category_slug: str | None,
    target_date: date | None,
    quotes_back: int | None,
    incremental_history: bool,
    output: TerminalOutput | None,
) -> tuple[date | None, bool]:
    """Define a data inicial anterior ao raw historico mais antigo."""
    if target_date is not None or quotes_back == 0 or not incremental_history:
        return target_date, False

    oldest_raw_date = find_oldest_raw_target_date(
        raw_dir,
        source_slug,
        category_slug,
    )

    if oldest_raw_date is None:
        if output is not None:
            scope = category_slug or source_slug
            output.info(
                f"{scope} | nenhum raw historico encontrado; "
                "iniciando pela cotacao mais recente."
            )

        return None, False

    incremental_target_date = oldest_raw_date - timedelta(days=1)

    if output is not None:
        scope = category_slug or source_slug
        output.info(
            f"{scope} | historico incremental iniciando em "
            f"{incremental_target_date.isoformat()} "
            f"(raw mais antigo: {oldest_raw_date.isoformat()})."
        )

    return incremental_target_date, True


def format_target_dates(target_dates: list[date | None]) -> str:
    return ", ".join(format_target_date(target_date) for target_date in target_dates)


def format_target_date(target_date: date | None) -> str:
    return target_date.isoformat() if target_date is not None else "ultima disponivel"
