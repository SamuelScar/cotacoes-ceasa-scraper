from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from cotacoes_ceasa.storage.sqlite import BackfillState, SQLiteStorage


BACKFILL_RECHECK_DAYS = 30


@dataclass(frozen=True)
class BackfillBaseline:
    source_slug: str
    oldest_date: date | None


def capture_backfill_baselines(
    database_path: Path,
    source_slugs: Iterable[str],
) -> dict[str, BackfillBaseline]:
    storage = SQLiteStorage(database_path)

    return {
        source_slug: BackfillBaseline(
            source_slug=source_slug,
            oldest_date=storage.find_oldest_cotacao_date(source_slug),
        )
        for source_slug in source_slugs
    }


def find_deferred_backfill_states(
    database_path: Path,
    source_slugs: Iterable[str],
    reference_date: date | None = None,
) -> dict[str, BackfillState]:
    storage = SQLiteStorage(database_path)
    effective_date = reference_date or date.today()
    deferred_states: dict[str, BackfillState] = {}

    for source_slug in source_slugs:
        state = storage.find_backfill_state(source_slug)

        if state is not None and state.is_deferred(effective_date):
            deferred_states[source_slug] = state

    return deferred_states


def finalize_backfill_state(
    database_path: Path,
    baseline: BackfillBaseline,
    download_status: str,
    persistence_status: str,
    error: str | None = None,
    reference_date: date | None = None,
) -> BackfillState:
    storage = SQLiteStorage(database_path)
    previous_state = storage.find_backfill_state(baseline.source_slug)
    oldest_date = storage.find_oldest_cotacao_date(baseline.source_slug)
    cursor_date = oldest_date or baseline.oldest_date

    if download_status == "partial":
        return storage.save_backfill_state(
            source_slug=baseline.source_slug,
            status="partial",
            cursor_date=cursor_date,
            consecutive_no_progress=_no_progress_count(previous_state),
            last_error=error,
        )

    if download_status != "completed" or persistence_status != "completed":
        return storage.save_backfill_state(
            source_slug=baseline.source_slug,
            status="failed",
            cursor_date=cursor_date,
            consecutive_no_progress=_no_progress_count(previous_state),
            last_error=error,
        )

    if _cursor_advanced(baseline.oldest_date, oldest_date):
        return storage.save_backfill_state(
            source_slug=baseline.source_slug,
            status="complete",
            cursor_date=oldest_date,
        )

    effective_date = reference_date or date.today()

    return storage.save_backfill_state(
        source_slug=baseline.source_slug,
        status="paused_for_recheck",
        cursor_date=cursor_date,
        consecutive_no_progress=_no_progress_count(previous_state) + 1,
        next_check_date=effective_date + timedelta(days=BACKFILL_RECHECK_DAYS),
    )


def format_backfill_state(state: BackfillState) -> str:
    status = {
        "complete": "completo",
        "partial": "parcial",
        "exhausted": "esgotado",
        "paused_for_recheck": "pausado para rechecagem",
        "failed": "falhou",
    }.get(state.status, state.status)

    if state.next_check_date is None:
        return status

    return f"{status} ate {state.next_check_date.isoformat()}"


def _cursor_advanced(previous: date | None, current: date | None) -> bool:
    return current is not None and (previous is None or current < previous)


def _no_progress_count(state: BackfillState | None) -> int:
    return state.consecutive_no_progress if state is not None else 0
