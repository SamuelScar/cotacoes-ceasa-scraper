import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from cotacoes_ceasa.config import SourceConfig
from cotacoes_ceasa.storage.sqlite import SQLiteStorage


PUBLICATION_GATE_SCHEMA_VERSION = 1
MINIMUM_HEALTH_SCHEMA_VERSION = 2
PUBLICATION_GATE_TIMEZONE = ZoneInfo("America/Sao_Paulo")
FULL_COLLECTION_MODES = {"current", "legacy"}


@dataclass(frozen=True)
class PublicationGateReason:
    code: str
    message: str
    blocking: bool
    source_slug: str | None = None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
            "source_slug": self.source_slug,
        }


@dataclass(frozen=True)
class PublicationGateSource:
    source_slug: str
    policy: str
    status: str
    download_status: str | None
    persistence_status: str | None
    latest_quote_date: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_slug": self.source_slug,
            "policy": self.policy,
            "status": self.status,
            "download_status": self.download_status,
            "persistence_status": self.persistence_status,
            "latest_quote_date": self.latest_quote_date,
        }


@dataclass(frozen=True)
class PublicationGateResult:
    evaluated_at: datetime
    status: str
    database_path: str
    database_status: str
    quick_check: tuple[str, ...]
    foreign_key_violations: int | None
    total_quotes: int | None
    max_quote_id: int | None
    health_report_path: str
    health_schema_version: int | None
    health_status: str | None
    collection_mode: str | None
    sources: tuple[PublicationGateSource, ...]
    reasons: tuple[PublicationGateReason, ...]

    @property
    def blocking_reasons(self) -> int:
        return sum(reason.blocking for reason in self.reasons)

    @property
    def warnings(self) -> int:
        return sum(not reason.blocking for reason in self.reasons)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PUBLICATION_GATE_SCHEMA_VERSION,
            "evaluated_at": self.evaluated_at.isoformat(timespec="seconds"),
            "status": self.status,
            "blocking": self.status == "rejected",
            "database": {
                "path": self.database_path,
                "status": self.database_status,
                "quick_check": list(self.quick_check),
                "foreign_key_violations": self.foreign_key_violations,
                "total_quotes": self.total_quotes,
                "max_quote_id": self.max_quote_id,
            },
            "health_report": {
                "path": self.health_report_path,
                "schema_version": self.health_schema_version,
                "status": self.health_status,
                "collection_mode": self.collection_mode,
            },
            "summary": {
                "blocking_reasons": self.blocking_reasons,
                "warnings": self.warnings,
                "sources": len(self.sources),
            },
            "reasons": [reason.to_dict() for reason in self.reasons],
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class _DatabaseInspection:
    status: str
    quick_check: tuple[str, ...] = ()
    foreign_key_violations: int | None = None
    total_quotes: int | None = None
    max_quote_id: int | None = None
    error: str | None = None


def evaluate_publication_gate(
    database_path: Path,
    health_report_path: Path,
    source_configs: Mapping[str, SourceConfig],
    evaluated_at: datetime | None = None,
) -> PublicationGateResult:
    assessment_time = evaluated_at or datetime.now(PUBLICATION_GATE_TIMEZONE)
    reasons: list[PublicationGateReason] = []
    database = _inspect_database(database_path)
    health_payload = _load_health_report(health_report_path, reasons)

    _append_database_reasons(database, reasons)
    sources: tuple[PublicationGateSource, ...] = ()
    health_schema_version = None
    health_status = None
    collection_mode = None

    if health_payload is not None:
        health_schema_version = _optional_int(health_payload.get("schema_version"))
        health_status = _optional_str(health_payload.get("status"))
        collection_mode = _optional_str(health_payload.get("collection_mode"))
        _append_health_contract_reasons(health_payload, reasons)
        _append_baseline_reasons(
            health_payload,
            database,
            database_path,
            reasons,
        )
        sources = _evaluate_sources(
            health_payload,
            source_configs,
            reasons,
        )

    status = "rejected" if any(reason.blocking for reason in reasons) else "approved"

    return PublicationGateResult(
        evaluated_at=assessment_time,
        status=status,
        database_path=database_path.as_posix(),
        database_status=database.status,
        quick_check=database.quick_check,
        foreign_key_violations=database.foreign_key_violations,
        total_quotes=database.total_quotes,
        max_quote_id=database.max_quote_id,
        health_report_path=health_report_path.as_posix(),
        health_schema_version=health_schema_version,
        health_status=health_status,
        collection_mode=collection_mode,
        sources=sources,
        reasons=tuple(reasons),
    )


def evaluate_checkpoint_gate(
    database_path: Path,
    health_report_path: Path,
    evaluated_at: datetime | None = None,
) -> PublicationGateResult:
    """Valida somente a integridade e a ausência de regressões do checkpoint."""
    return evaluate_publication_gate(
        database_path=database_path,
        health_report_path=health_report_path,
        source_configs={},
        evaluated_at=evaluated_at,
    )


def write_publication_gate_result(
    result: PublicationGateResult,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(f"{destination.suffix}.tmp")
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    try:
        temp_path.write_text(f"{payload}\n", encoding="utf-8")
        temp_path.replace(destination)
    except OSError:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _inspect_database(database_path: Path) -> _DatabaseInspection:
    if not database_path.exists():
        return _DatabaseInspection(
            status="unavailable",
            error="database_missing",
        )

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"

    try:
        with sqlite3.connect(database_uri, uri=True) as connection:
            quick_check = tuple(
                str(row[0]) for row in connection.execute("PRAGMA quick_check")
            )
            foreign_key_violations = len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            total_row = connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()
            max_id_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM cotacoes"
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError) as error:
        return _DatabaseInspection(
            status="unavailable",
            error=f"{type(error).__name__}: {error}",
        )

    return _DatabaseInspection(
        status="available",
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        total_quotes=int(total_row[0]) if total_row else 0,
        max_quote_id=int(max_id_row[0]) if max_id_row else 0,
    )


def _load_health_report(
    health_report_path: Path,
    reasons: list[PublicationGateReason],
) -> dict[str, object] | None:
    if not health_report_path.exists():
        reasons.append(
            PublicationGateReason(
                "health_report_missing",
                f"Relatorio de saude ausente: {health_report_path}.",
                True,
            )
        )
        return None

    try:
        payload = json.loads(health_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        reasons.append(
            PublicationGateReason(
                "health_report_invalid",
                f"Relatorio de saude invalido: {type(error).__name__}: {error}",
                True,
            )
        )
        return None

    if not isinstance(payload, dict):
        reasons.append(
            PublicationGateReason(
                "health_report_invalid",
                "O relatorio de saude deve conter um objeto JSON.",
                True,
            )
        )
        return None

    return payload


def _append_database_reasons(
    database: _DatabaseInspection,
    reasons: list[PublicationGateReason],
) -> None:
    if database.status != "available":
        reasons.append(
            PublicationGateReason(
                "database_unavailable",
                f"SQLite indisponivel para publicacao: {database.error}.",
                True,
            )
        )
        return

    if database.quick_check != ("ok",):
        reasons.append(
            PublicationGateReason(
                "database_quick_check_failed",
                "PRAGMA quick_check encontrou inconsistencias: "
                + "; ".join(database.quick_check),
                True,
            )
        )

    if database.foreign_key_violations:
        reasons.append(
            PublicationGateReason(
                "database_foreign_key_violations",
                f"PRAGMA foreign_key_check encontrou "
                f"{database.foreign_key_violations} violacao(oes).",
                True,
            )
        )

    if database.total_quotes == 0:
        reasons.append(
            PublicationGateReason(
                "database_without_quotes",
                "O SQLite nao possui cotacoes para publicar.",
                True,
            )
        )


def _append_health_contract_reasons(
    payload: dict[str, object],
    reasons: list[PublicationGateReason],
) -> None:
    schema_version = _optional_int(payload.get("schema_version"))

    if schema_version is None or schema_version < MINIMUM_HEALTH_SCHEMA_VERSION:
        reasons.append(
            PublicationGateReason(
                "health_schema_unsupported",
                "O relatorio de saude nao possui a baseline exigida pelo gate.",
                True,
            )
        )

    health_reasons = payload.get("reasons")
    if not isinstance(health_reasons, list):
        reasons.append(
            PublicationGateReason(
                "health_reasons_invalid",
                "O relatorio de saude nao possui uma lista valida de motivos.",
                True,
            )
        )
        return

    blocking_health_codes = {
        "database_unavailable",
        "database_id_regression",
        "health_assessment_failed",
        "run_failed",
    }

    for health_reason in health_reasons:
        if not isinstance(health_reason, dict):
            continue

        code = _optional_str(health_reason.get("code"))
        if code not in blocking_health_codes:
            continue

        reasons.append(
            PublicationGateReason(
                f"health_{code}",
                _optional_str(health_reason.get("message"))
                or f"A saude registrou o motivo bloqueante {code}.",
                True,
            )
        )


def _append_baseline_reasons(
    payload: dict[str, object],
    database: _DatabaseInspection,
    database_path: Path,
    reasons: list[PublicationGateReason],
) -> None:
    baseline = payload.get("baseline")

    if not isinstance(baseline, dict):
        reasons.append(
            PublicationGateReason(
                "baseline_missing",
                "Baseline estrutural ausente no relatorio de saude.",
                True,
            )
        )
        return

    if baseline.get("measurement_status") != "available":
        reasons.append(
            PublicationGateReason(
                "baseline_unavailable",
                f"Baseline estrutural indisponivel: {baseline.get('error')}.",
                True,
            )
        )
        return

    previous_total = _optional_int(baseline.get("previous_total_quotes"))
    previous_max_id = _optional_int(baseline.get("previous_max_id"))

    if previous_total is None or previous_max_id is None:
        reasons.append(
            PublicationGateReason(
                "baseline_incomplete",
                "Baseline sem contagem total ou maior ID anterior.",
                True,
            )
        )
        return

    if database.total_quotes is not None and database.total_quotes < previous_total:
        reasons.append(
            PublicationGateReason(
                "database_total_regression",
                f"A contagem total regrediu de {previous_total} para "
                f"{database.total_quotes}.",
                True,
            )
        )

    if database.max_quote_id is not None and database.max_quote_id < previous_max_id:
        reasons.append(
            PublicationGateReason(
                "database_max_id_regression",
                f"O maior ID regrediu de {previous_max_id} para "
                f"{database.max_quote_id}.",
                True,
            )
        )

    previous_dates = baseline.get("previous_latest_quote_dates")
    if not isinstance(previous_dates, dict):
        reasons.append(
            PublicationGateReason(
                "baseline_dates_missing",
                "Baseline sem maiores datas por fonte.",
                True,
            )
        )
        return

    try:
        current_dates = SQLiteStorage(database_path).find_latest_cotacao_dates(
            tuple(str(source_slug) for source_slug in previous_dates)
        )
    except (OSError, sqlite3.Error, ValueError) as error:
        reasons.append(
            PublicationGateReason(
                "database_dates_unavailable",
                f"Nao foi possivel comparar datas por fonte: "
                f"{type(error).__name__}: {error}",
                True,
            )
        )
        return

    for source_slug, previous_value in previous_dates.items():
        if previous_value is None:
            continue

        try:
            previous_date = date.fromisoformat(str(previous_value))
        except ValueError:
            reasons.append(
                PublicationGateReason(
                    "baseline_date_invalid",
                    f"Data anterior invalida para {source_slug}: {previous_value}.",
                    True,
                    str(source_slug),
                )
            )
            continue

        current_date = current_dates.get(str(source_slug))
        if current_date is not None and current_date >= previous_date:
            continue

        reasons.append(
            PublicationGateReason(
                "source_latest_date_regression",
                f"A maior data de {source_slug} regrediu de "
                f"{previous_date.isoformat()} para "
                f"{current_date.isoformat() if current_date else 'ausente'}.",
                True,
                str(source_slug),
            )
        )


def _evaluate_sources(
    payload: dict[str, object],
    source_configs: Mapping[str, SourceConfig],
    reasons: list[PublicationGateReason],
) -> tuple[PublicationGateSource, ...]:
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        reasons.append(
            PublicationGateReason(
                "health_sources_invalid",
                "O relatorio de saude nao possui fontes validas.",
                True,
            )
        )
        return ()

    assessed_sources = {
        str(source["source_slug"]): source
        for source in raw_sources
        if isinstance(source, dict) and source.get("source_slug")
    }
    collection_mode = _optional_str(payload.get("collection_mode"))
    full_scope = collection_mode in FULL_COLLECTION_MODES
    results: list[PublicationGateSource] = []

    for source_slug, source_config in source_configs.items():
        policy = source_config.publication_policy
        source = assessed_sources.get(source_slug)

        if policy in {"disabled", "backfill_only", "unsupported"}:
            results.append(
                _build_source_result(source_slug, policy, "ignored", source)
            )
            continue

        if source is None and not full_scope:
            results.append(
                _build_source_result(source_slug, policy, "out_of_scope", source)
            )
            continue

        source_reasons = _source_reasons(source_slug, policy, source)
        reasons.extend(source_reasons)
        status = (
            "rejected"
            if any(reason.blocking for reason in source_reasons)
            else "warning"
            if source_reasons
            else "approved"
        )
        results.append(_build_source_result(source_slug, policy, status, source))

    return tuple(results)


def _source_reasons(
    source_slug: str,
    policy: str,
    source: dict[str, object] | None,
) -> list[PublicationGateReason]:
    blocking = policy == "required"

    if source is None:
        return [
            PublicationGateReason(
                "required_source_missing" if blocking else "optional_source_missing",
                f"A fonte {source_slug} nao aparece no relatorio da rodada.",
                blocking,
                source_slug,
            )
        ]

    source_reasons: list[PublicationGateReason] = []
    download_status = _optional_str(source.get("download_status"))
    persistence_status = _optional_str(source.get("persistence_status"))
    latest_quote_date = _optional_str(source.get("latest_quote_date"))
    freshness_status = _optional_str(source.get("freshness_status"))
    download_completed = download_status == "completed"

    if not download_completed:
        source_reasons.append(
            PublicationGateReason(
                "required_download_failed" if blocking else "optional_download_failed",
                f"{source_slug} terminou o download como {download_status}.",
                False,
                source_slug,
            )
        )

    if persistence_status != "completed" and download_completed:
        source_reasons.append(
            PublicationGateReason(
                (
                    "required_persistence_failed"
                    if blocking
                    else "optional_persistence_failed"
                ),
                f"{source_slug} terminou a persistencia como {persistence_status}.",
                blocking,
                source_slug,
            )
        )

    if latest_quote_date is None:
        source_reasons.append(
            PublicationGateReason(
                "required_source_without_data" if blocking else "optional_source_without_data",
                f"{source_slug} nao possui cotacao persistida.",
                blocking,
                source_slug,
            )
        )

    if freshness_status == "future":
        source_reasons.append(
            PublicationGateReason(
                "required_future_date" if blocking else "optional_future_date",
                f"{source_slug} possui data de cotacao futura.",
                blocking,
                source_slug,
            )
        )

    return source_reasons


def _build_source_result(
    source_slug: str,
    policy: str,
    status: str,
    source: dict[str, object] | None,
) -> PublicationGateSource:
    return PublicationGateSource(
        source_slug=source_slug,
        policy=policy,
        status=status,
        download_status=(
            _optional_str(source.get("download_status")) if source else None
        ),
        persistence_status=(
            _optional_str(source.get("persistence_status")) if source else None
        ),
        latest_quote_date=(
            _optional_str(source.get("latest_quote_date")) if source else None
        ),
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None

    return int(value) if isinstance(value, int) else None


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None
