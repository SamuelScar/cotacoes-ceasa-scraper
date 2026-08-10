import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from cotacoes_ceasa.config import SourceConfig
from cotacoes_ceasa.storage.sqlite import LogicalCotacaoDelta, SQLiteStorage


HEALTH_SCHEMA_VERSION = 2
HEALTH_MODE = "observation"
HEALTH_TIMEZONE = ZoneInfo("America/Sao_Paulo")


@dataclass(frozen=True)
class SourceRunObservation:
    source_slug: str
    download_status: str
    persistence_status: str
    raw_files: int = 0
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BatchRunResult:
    sources: tuple[SourceRunObservation, ...]


@dataclass(frozen=True)
class HealthBaseline:
    previous_max_id: int | None
    previous_total_quotes: int | None = None
    previous_latest_dates: tuple[tuple[str, date | None], ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "measurement_status": "available" if self.error is None else "unavailable",
            "previous_max_id": self.previous_max_id,
            "previous_total_quotes": self.previous_total_quotes,
            "previous_latest_quote_dates": {
                source_slug: latest_date.isoformat() if latest_date else None
                for source_slug, latest_date in self.previous_latest_dates
            },
            "error": self.error,
        }


@dataclass(frozen=True)
class HealthReason:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class LogicalDeltaObservation:
    measurement_status: str
    previous_max_id: int | None = None
    current_max_id: int | None = None
    observations_inserted: int | None = None
    logical_new: int | None = None
    repeated_observations: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "measurement_status": self.measurement_status,
            "previous_max_id": self.previous_max_id,
            "current_max_id": self.current_max_id,
            "observations_inserted": self.observations_inserted,
            "logical_new": self.logical_new,
            "repeated_observations": self.repeated_observations,
            "error": self.error,
        }


@dataclass(frozen=True)
class SourceHealth:
    source_slug: str
    source_name: str
    download_status: str
    persistence_status: str
    raw_files: int
    latest_quote_date: date | None
    age_days: int | None
    max_staleness_days: int | None
    freshness_status: str
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "source_slug": self.source_slug,
            "source_name": self.source_name,
            "download_status": self.download_status,
            "persistence_status": self.persistence_status,
            "raw_files": self.raw_files,
            "latest_quote_date": (
                self.latest_quote_date.isoformat() if self.latest_quote_date else None
            ),
            "age_days": self.age_days,
            "max_staleness_days": self.max_staleness_days,
            "freshness_status": self.freshness_status,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class RunHealthAssessment:
    assessed_at: datetime
    status: str
    database_path: str
    database_status: str
    database_error: str | None
    sources: tuple[SourceHealth, ...]
    logical_delta: LogicalDeltaObservation
    baseline: HealthBaseline
    reasons: tuple[HealthReason, ...]
    collection_mode: str = "legacy"

    @property
    def download_completed(self) -> int:
        return self._count_source_status("download_status", "completed")

    @property
    def download_partial(self) -> int:
        return self._count_source_status("download_status", "partial")

    @property
    def download_failed(self) -> int:
        return self._count_source_status("download_status", "failed")

    @property
    def download_not_run(self) -> int:
        return self._count_source_status("download_status", "not_run")

    @property
    def persistence_completed(self) -> int:
        return self._count_source_status("persistence_status", "completed")

    @property
    def persistence_failed(self) -> int:
        return self._count_source_status("persistence_status", "failed")

    @property
    def persistence_skipped(self) -> int:
        return self._count_source_status("persistence_status", "skipped")

    @property
    def persistence_not_run(self) -> int:
        return self._count_source_status("persistence_status", "not_run")

    @property
    def stale_sources(self) -> int:
        return self._count_source_status("freshness_status", "stale")

    @property
    def sources_without_data(self) -> int:
        return self._count_source_status("freshness_status", "no_data")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "mode": HEALTH_MODE,
            "collection_mode": self.collection_mode,
            "blocking": False,
            "assessed_at": self.assessed_at.isoformat(timespec="seconds"),
            "status": self.status,
            "database": {
                "path": self.database_path,
                "status": self.database_status,
                "error": self.database_error,
            },
            "summary": {
                "configured_sources": len(self.sources),
                "download_completed": self.download_completed,
                "download_partial": self.download_partial,
                "download_failed": self.download_failed,
                "download_not_run": self.download_not_run,
                "persistence_completed": self.persistence_completed,
                "persistence_failed": self.persistence_failed,
                "persistence_skipped": self.persistence_skipped,
                "persistence_not_run": self.persistence_not_run,
                "stale_sources": self.stale_sources,
                "sources_without_data": self.sources_without_data,
            },
            "logical_delta": self.logical_delta.to_dict(),
            "baseline": self.baseline.to_dict(),
            "reasons": [reason.to_dict() for reason in self.reasons],
            "limitations": [
                "download_completed_does_not_guarantee_full_category_coverage",
                "persistence_completed_does_not_guarantee_every_raw_succeeded",
                "logical_delta_assumes_a_single_sqlite_writer",
            ],
            "sources": [source.to_dict() for source in self.sources],
        }

    def _count_source_status(self, field_name: str, expected_status: str) -> int:
        return sum(
            getattr(source, field_name) == expected_status for source in self.sources
        )


def capture_health_baseline(
    database_path: Path,
    source_slugs: tuple[str, ...] = (),
) -> HealthBaseline:
    if not database_path.exists():
        return HealthBaseline(
            previous_max_id=None,
            error="database_missing_before_run",
        )

    try:
        storage = SQLiteStorage(database_path)
        previous_max_id = storage.find_latest_cotacao_id()
        previous_total_quotes = storage.count_cotacoes()
        previous_latest_dates = storage.find_latest_cotacao_dates(source_slugs)
    except (OSError, sqlite3.Error, ValueError) as error:
        return HealthBaseline(
            previous_max_id=None,
            error=f"{type(error).__name__}: {error}",
        )

    return HealthBaseline(
        previous_max_id=previous_max_id,
        previous_total_quotes=previous_total_quotes,
        previous_latest_dates=tuple(previous_latest_dates.items()),
    )


def evaluate_run_health(
    batch_result: BatchRunResult,
    source_configs: Mapping[str, SourceConfig],
    database_path: Path,
    baseline: HealthBaseline,
    assessed_at: datetime | None = None,
    run_error: str | None = None,
    collection_mode: str = "legacy",
) -> RunHealthAssessment:
    assessment_time = assessed_at or datetime.now(HEALTH_TIMEZONE)
    latest_dates, database_status, database_error = _load_latest_dates(
        database_path,
        tuple(source_configs),
    )
    logical_delta = _load_logical_delta(database_path, baseline)
    run_sources = {source.source_slug: source for source in batch_result.sources}
    sources = tuple(
        _build_source_health(
            source_slug,
            source_config,
            run_sources.get(source_slug),
            latest_dates.get(source_slug),
            database_status,
            assessment_time.date(),
        )
        for source_slug, source_config in source_configs.items()
    )
    status, reasons = _classify_health(
        sources,
        database_status,
        logical_delta,
        run_error,
    )

    return RunHealthAssessment(
        assessed_at=assessment_time,
        status=status,
        database_path=database_path.as_posix(),
        database_status=database_status,
        database_error=database_error,
        sources=sources,
        logical_delta=logical_delta,
        baseline=baseline,
        reasons=reasons,
        collection_mode=collection_mode,
    )


def build_unavailable_run_health(
    batch_result: BatchRunResult,
    source_configs: Mapping[str, SourceConfig],
    database_path: Path,
    error: str,
    run_error: str | None = None,
    collection_mode: str = "legacy",
    assessed_at: datetime | None = None,
) -> RunHealthAssessment:
    assessment_time = assessed_at or datetime.now(HEALTH_TIMEZONE)
    run_sources = {source.source_slug: source for source in batch_result.sources}
    sources = tuple(
        _build_source_health(
            source_slug,
            source_config,
            run_sources.get(source_slug),
            latest_quote_date=None,
            database_status="not_assessed",
            assessment_date=assessment_time.date(),
        )
        for source_slug, source_config in source_configs.items()
    )
    reasons = [
        HealthReason(
            "health_assessment_failed",
            f"A avaliacao de saude falhou: {error}",
        )
    ]

    if run_error:
        reasons.append(
            HealthReason(
                "run_failed",
                f"A execucao principal falhou: {run_error}",
            )
        )

    return RunHealthAssessment(
        assessed_at=assessment_time,
        status="inadequate",
        database_path=database_path.as_posix(),
        database_status="not_assessed",
        database_error=error,
        sources=sources,
        logical_delta=LogicalDeltaObservation(
            measurement_status="unavailable",
            error="health_assessment_failed",
        ),
        baseline=HealthBaseline(
            previous_max_id=None,
            error="health_assessment_failed",
        ),
        reasons=tuple(reasons),
        collection_mode=collection_mode,
    )


def write_health_assessment(
    assessment: RunHealthAssessment,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(f"{destination.suffix}.tmp")
    payload = json.dumps(
        assessment.to_dict(),
        ensure_ascii=False,
        indent=2,
    )

    try:
        temp_path.write_text(f"{payload}\n", encoding="utf-8")
        temp_path.replace(destination)
    except OSError:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _load_latest_dates(
    database_path: Path,
    source_slugs: tuple[str, ...],
) -> tuple[dict[str, date | None], str, str | None]:
    empty_dates = {source_slug: None for source_slug in source_slugs}

    if not database_path.exists():
        return empty_dates, "unavailable", "database_missing_after_run"

    try:
        latest_dates = SQLiteStorage(database_path).find_latest_cotacao_dates(
            source_slugs
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        return empty_dates, "unavailable", f"{type(error).__name__}: {error}"

    return latest_dates, "available", None


def _load_logical_delta(
    database_path: Path,
    baseline: HealthBaseline,
) -> LogicalDeltaObservation:
    if baseline.previous_max_id is None:
        return LogicalDeltaObservation(
            measurement_status="unavailable",
            error=baseline.error or "baseline_unavailable",
        )

    try:
        delta = SQLiteStorage(database_path).summarize_logical_cotacao_delta(
            baseline.previous_max_id
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        return LogicalDeltaObservation(
            measurement_status="unavailable",
            previous_max_id=baseline.previous_max_id,
            error=f"{type(error).__name__}: {error}",
        )

    return _build_logical_delta_observation(delta)


def _build_logical_delta_observation(
    delta: LogicalCotacaoDelta,
) -> LogicalDeltaObservation:
    measurement_status = (
        "database_regressed"
        if delta.current_max_id < delta.previous_max_id
        else "available"
    )

    return LogicalDeltaObservation(
        measurement_status=measurement_status,
        previous_max_id=delta.previous_max_id,
        current_max_id=delta.current_max_id,
        observations_inserted=delta.observations_inserted,
        logical_new=delta.logical_new,
        repeated_observations=delta.repeated_observations,
    )


def _build_source_health(
    source_slug: str,
    source_config: SourceConfig,
    run_observation: SourceRunObservation | None,
    latest_quote_date: date | None,
    database_status: str,
    assessment_date: date,
) -> SourceHealth:
    observation = run_observation or SourceRunObservation(
        source_slug=source_slug,
        download_status="not_run",
        persistence_status="not_run",
    )
    age_days = (
        (assessment_date - latest_quote_date).days if latest_quote_date else None
    )
    freshness_status = _classify_freshness(
        database_status,
        age_days,
        source_config.max_staleness_days,
    )

    return SourceHealth(
        source_slug=source_slug,
        source_name=source_config.name,
        download_status=observation.download_status,
        persistence_status=observation.persistence_status,
        raw_files=observation.raw_files,
        latest_quote_date=latest_quote_date,
        age_days=age_days,
        max_staleness_days=source_config.max_staleness_days,
        freshness_status=freshness_status,
        error_type=observation.error_type,
        error_message=observation.error_message,
    )


def _classify_freshness(
    database_status: str,
    age_days: int | None,
    max_staleness_days: int | None,
) -> str:
    if database_status != "available":
        return "unavailable"

    if age_days is None:
        return "no_data"

    if age_days < 0:
        return "future"

    if max_staleness_days is None:
        return "not_configured"

    return "current" if age_days <= max_staleness_days else "stale"


def _classify_health(
    sources: tuple[SourceHealth, ...],
    database_status: str,
    logical_delta: LogicalDeltaObservation,
    run_error: str | None,
) -> tuple[str, tuple[HealthReason, ...]]:
    reasons: list[HealthReason] = []
    configured_count = len(sources)
    completed_count = _count_status(sources, "download_status", "completed")
    partial_count = _count_status(sources, "download_status", "partial")
    failed_count = _count_status(sources, "download_status", "failed")
    not_run_count = _count_status(sources, "download_status", "not_run")
    persistence_failed = _count_status(sources, "persistence_status", "failed")
    persistence_skipped = _count_status(sources, "persistence_status", "skipped")
    persistence_not_run = _count_status(sources, "persistence_status", "not_run")
    stale_count = _count_status(sources, "freshness_status", "stale")
    no_data_count = _count_status(sources, "freshness_status", "no_data")
    future_count = _count_status(sources, "freshness_status", "future")

    if run_error:
        reasons.append(
            HealthReason(
                "run_failed",
                f"A execucao principal falhou: {run_error}",
            )
        )

    if database_status != "available":
        reasons.append(
            HealthReason(
                "database_unavailable",
                "O SQLite nao ficou disponivel para avaliar frescor.",
            )
        )

    if configured_count and completed_count < configured_count // 2 + 1:
        reasons.append(
            HealthReason(
                "insufficient_completed_sources",
                f"Somente {completed_count}/{configured_count} fontes concluiram o download.",
            )
        )

    if persistence_failed:
        reasons.append(
            HealthReason(
                "persistence_failures",
                f"{persistence_failed} fonte(s) falharam na persistencia.",
            )
        )

    if future_count:
        reasons.append(
            HealthReason(
                "future_quote_dates",
                f"{future_count} fonte(s) possuem data de cotacao futura.",
            )
        )

    if logical_delta.measurement_status == "database_regressed":
        reasons.append(
            HealthReason(
                "database_id_regression",
                "O maior id do SQLite regrediu durante a rodada.",
            )
        )

    if logical_delta.measurement_status == "unavailable":
        reasons.append(
            HealthReason(
                "logical_delta_unavailable",
                "Nao foi possivel medir o delta logico da rodada.",
            )
        )

    if failed_count:
        reasons.append(
            HealthReason(
                "download_failures",
                f"{failed_count} fonte(s) falharam no download.",
            )
        )

    if partial_count:
        reasons.append(
            HealthReason(
                "partial_downloads",
                f"{partial_count} fonte(s) concluiram o download parcialmente.",
            )
        )

    if not_run_count:
        reasons.append(
            HealthReason(
                "sources_not_run",
                f"{not_run_count} fonte(s) nao tiveram resultado de download.",
            )
        )

    if persistence_skipped:
        reasons.append(
            HealthReason(
                "persistence_skipped",
                f"{persistence_skipped} fonte(s) nao chegaram a persistencia.",
            )
        )

    if persistence_not_run:
        reasons.append(
            HealthReason(
                "persistence_not_run",
                f"{persistence_not_run} fonte(s) nao tiveram resultado de persistencia.",
            )
        )

    if stale_count:
        reasons.append(
            HealthReason(
                "stale_sources",
                f"{stale_count} fonte(s) ultrapassaram o limite de frescor.",
            )
        )

    if no_data_count:
        reasons.append(
            HealthReason(
                "sources_without_data",
                f"{no_data_count} fonte(s) configuradas nao possuem cotacoes.",
            )
        )

    if configured_count and no_data_count == configured_count:
        reasons.append(
            HealthReason(
                "database_without_quotes",
                "Nenhuma fonte configurada possui cotacoes no SQLite.",
            )
        )

    inadequate_codes = {
        "database_unavailable",
        "run_failed",
        "insufficient_completed_sources",
        "persistence_failures",
        "future_quote_dates",
        "database_id_regression",
        "database_without_quotes",
    }
    status = (
        "inadequate"
        if any(reason.code in inadequate_codes for reason in reasons)
        else "partial"
        if reasons
        else "healthy"
    )

    return status, tuple(reasons)


def _count_status(
    sources: tuple[SourceHealth, ...],
    field_name: str,
    expected_status: str,
) -> int:
    return sum(getattr(source, field_name) == expected_status for source in sources)
