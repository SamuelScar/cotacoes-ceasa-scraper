"""Planejamento somente leitura da migracao dos backups legados."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from cotacoes_ceasa.backups.manifest import record_legacy_migration_cleanup
from cotacoes_ceasa.backups.orchestration import (
    LayeredBackupRequest,
    run_layered_backup,
)
from cotacoes_ceasa.backups.packaging import (
    BackupLayer,
    BackupLayerSpec,
    calculate_sha256,
)
from cotacoes_ceasa.backups.policy import (
    layer_file_date,
    normalize_remote_root,
    validate_layer_remote_path,
    validate_remote_name,
)
from cotacoes_ceasa.backups.remote import (
    RemotePublicationError,
    RemotePublicationRequest,
    publish_backup_atomically,
)
from cotacoes_ceasa.backups.restore import RestoreRequest, restore_backup
from cotacoes_ceasa.execution import (
    ExecutionContext,
    execution_now,
    sanitize_log_text,
    write_json_atomic,
)


MIGRATION_PLAN_SCHEMA_VERSION = 1
DEFAULT_DEEP_RETENTION_DAYS = 30
DEFAULT_DAILY_RETENTION_DAYS = 7
HISTORY_MIGRATION_BATCH_COUNT = 3
MAX_AUDIT_MANIFEST_BYTES = 5 * 1024 * 1024
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
APPLY_CONFIRMATION = "EXCLUIR_LEGADOS_VENCIDOS"
HISTORY_APPLY_CONFIRMATION = "MIGRAR_HISTORICOS_VALIDOS"
HISTORY_FINALIZE_CONFIRMATION = "EXCLUIR_HISTORICOS_MIGRADOS"
ZPAQ_BENCHMARK_MAX_RSS_BYTES = 2_042_736_640
MEMORY_SAFETY_MARGIN_BYTES = 512 * 1024 * 1024
MINIMUM_DISK_FREE_BYTES = 10 * 1024 * 1024 * 1024
LEGACY_ARCHIVE_DISK_MULTIPLIER = 12

LEGACY_LATEST_PATTERN = re.compile(
    r"^latest/ceasa-data-full-latest\.(?:tar\.xz|tar\.gz)$"
)
LEGACY_HISTORY_PATTERN = re.compile(
    r"^history/ceasa-data-full-latest-"
    r"(?P<timestamp>[0-9]{8}T[0-9]{6}Z)-"
    r"(?P<run_id>[0-9]+)\.(?P<format>tar\.xz|tar\.gz)$"
)


class LegacyMigrationPlanError(RuntimeError):
    """Indica que o inventario nao permite gerar um plano confiavel."""


@dataclass(frozen=True)
class LegacyMigrationPlanRequest:
    remote: str
    remote_root: str
    output_path: Path
    report_path: Path
    workspace_path: Path
    reference_date: date | None = None
    deep_retention_days: int = DEFAULT_DEEP_RETENTION_DAYS


@dataclass(frozen=True)
class LegacyHistoryMigrationPlanRequest:
    remote: str
    remote_root: str
    output_path: Path
    report_path: Path
    workspace_path: Path
    reference_date: date | None = None
    daily_retention_days: int = DEFAULT_DAILY_RETENTION_DAYS
    deep_retention_days: int = DEFAULT_DEEP_RETENTION_DAYS
    batch_count: int = HISTORY_MIGRATION_BATCH_COUNT


@dataclass(frozen=True)
class LegacyHistoryBatchRequest:
    approved_plan_path: Path
    expected_plan_sha256: str
    confirmation: str
    batch_number: int
    execution_id: str
    operation_root: Path
    workspace_path: Path
    result_path: Path
    report_path: Path


@dataclass(frozen=True)
class LegacyHistoryFinalizeRequest:
    approved_plan_path: Path
    expected_plan_sha256: str
    confirmation: str
    execution_id: str
    workspace_path: Path
    result_path: Path
    report_path: Path


@dataclass(frozen=True)
class LegacyMigrationApplyCheckRequest:
    plan_request: LegacyMigrationPlanRequest
    expected_plan_sha256: str
    confirmation: str
    output_path: Path
    report_path: Path


@dataclass(frozen=True)
class LegacyMigrationConversionRequest:
    apply_check_request: LegacyMigrationApplyCheckRequest
    execution_id: str
    operation_root: Path
    result_path: Path
    report_path: Path
    backup_result_path: Path


class LegacyMigrationConversionError(RuntimeError):
    """Mantem o resultado parcial de uma conversao que falhou."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


class LegacyHistoryOperationError(RuntimeError):
    """Mantem as evidencias parciais de uma data historica."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


def create_legacy_migration_plan(
    request: LegacyMigrationPlanRequest,
) -> dict[str, Any]:
    """Inventaria o remote e gera decisoes sem executar mutacoes."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    reference_date = request.reference_date or execution_now().date()
    if (
        isinstance(request.deep_retention_days, bool)
        or not isinstance(request.deep_retention_days, int)
        or request.deep_retention_days < 1
    ):
        raise LegacyMigrationPlanError(
            "A retencao profunda deve ser um inteiro maior ou igual a 1."
        )
    cutoff_date = reference_date - timedelta(
        days=request.deep_retention_days - 1
    )

    raw_entries = _run_rclone_json(
        "lsjson",
        f"{remote}:{remote_root.as_posix()}",
        "--recursive",
        "--files-only",
        "--hash",
    )
    if not isinstance(raw_entries, list):
        raise LegacyMigrationPlanError(
            "A listagem do OneDrive nao retornou uma lista JSON."
        )

    inventory = [
        _normalize_remote_entry(entry, remote_root, cutoff_date)
        for entry in raw_entries
    ]
    inventory.sort(key=lambda item: item["relative_path"])

    legacy_latest = [
        item for item in inventory if item["classification"] == "legacy_latest"
    ]
    if len(legacy_latest) > 1:
        raise LegacyMigrationPlanError(
            "Mais de um backup latest legado foi encontrado."
        )
    read_probe = _probe_legacy_latest_read(remote, legacy_latest)

    legacy_history = [
        item for item in inventory if item["classification"] == "legacy_history"
    ]
    removal_candidates = [
        item
        for item in legacy_history
        if item["decision"] == "delete_after_replacement"
    ]
    retained_legacy = [
        item
        for item in legacy_history
        if item["decision"] == "preserve_until_expiration"
    ]

    deterministic_plan = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "timezone": "America/Sao_Paulo",
        "reference_date": reference_date.isoformat(),
        "remote": remote,
        "remote_root": remote_root.as_posix(),
        "policy": {
            "deep_retention_days": request.deep_retention_days,
            "cutoff_date": cutoff_date.isoformat(),
        },
        "legacy_latest": legacy_latest,
        "legacy_history": legacy_history,
        "removal_candidates": removal_candidates,
        "retained_legacy": retained_legacy,
        "inventory_fingerprint": inventory,
    }
    plan_sha256 = _canonical_sha256(deterministic_plan)
    quota = _run_rclone_json("about", f"{remote}:", "--json")
    if not isinstance(quota, dict):
        raise LegacyMigrationPlanError(
            "A consulta de quota do OneDrive nao retornou um objeto JSON."
        )

    result = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "completed",
        "mode": "plan",
        "generated_at": execution_now().isoformat(timespec="seconds"),
        "plan_sha256": plan_sha256,
        "plan": deterministic_plan,
        "summary": {
            "remote_files": len(inventory),
            "legacy_latest_files": len(legacy_latest),
            "legacy_history_files": len(legacy_history),
            "removal_candidates": len(removal_candidates),
            "removal_candidate_bytes": sum(
                item["size_bytes"] for item in removal_candidates
            ),
            "retained_legacy_files": len(retained_legacy),
        },
        "onedrive_quota": quota,
        "runner": _runner_information(request.workspace_path),
        "safety": {
            "read_only": True,
            "legacy_latest_read_probe": read_probe,
            "downloaded_files": 0,
            "moved_files": 0,
            "removed_files": 0,
        },
    }
    write_json_atomic(request.output_path, result)
    request.report_path.parent.mkdir(parents=True, exist_ok=True)
    request.report_path.write_text(
        _render_markdown(result),
        encoding="utf-8",
    )
    return result


def create_legacy_history_migration_plan(
    request: LegacyHistoryMigrationPlanRequest,
) -> dict[str, Any]:
    """Planeja a importacao historica sem modificar o remote."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    reference_date = request.reference_date or execution_now().date()
    daily_days = _positive_retention_days(
        request.daily_retention_days,
        "diaria",
    )
    deep_days = _positive_retention_days(
        request.deep_retention_days,
        "profunda",
    )
    if (
        isinstance(request.batch_count, bool)
        or not isinstance(request.batch_count, int)
        or request.batch_count < 1
    ):
        raise LegacyMigrationPlanError(
            "A quantidade de lotes deve ser um inteiro maior ou igual a 1."
        )
    daily_cutoff = reference_date - timedelta(days=daily_days - 1)
    deep_cutoff = reference_date - timedelta(days=deep_days - 1)

    raw_entries = _run_rclone_json(
        "lsjson",
        f"{remote}:{remote_root.as_posix()}",
        "--recursive",
        "--files-only",
        "--hash",
    )
    if not isinstance(raw_entries, list):
        raise LegacyMigrationPlanError(
            "A listagem do OneDrive nao retornou uma lista JSON."
        )
    inventory = [
        _normalize_remote_entry(entry, remote_root, deep_cutoff)
        for entry in raw_entries
    ]
    inventory.sort(key=lambda item: item["relative_path"])
    inventory_by_path = {
        item["relative_path"]: item
        for item in inventory
    }

    legacy_history = [
        _historical_legacy_entry(item)
        for item in inventory
        if item["classification"] == "legacy_history"
    ]
    future_dated = [
        item
        for item in legacy_history
        if date.fromisoformat(item["backup_date"]) > reference_date
    ]
    if future_dated:
        paths = ", ".join(item["relative_path"] for item in future_dated)
        raise LegacyMigrationPlanError(
            "O inventario possui backups legados posteriores a data de "
            f"referencia: {sanitize_log_text(paths)}."
        )

    eligible = [
        item
        for item in legacy_history
        if date.fromisoformat(item["backup_date"]) >= deep_cutoff
    ]
    expired = [
        item
        for item in legacy_history
        if date.fromisoformat(item["backup_date"]) < deep_cutoff
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in eligible:
        grouped.setdefault(item["backup_date"], []).append(item)

    audit_proofs, ignored_audits = _collect_remote_audit_proofs(
        remote,
        remote_root,
        inventory,
    )
    operations = [
        _historical_operation(
            backup_date=date.fromisoformat(day),
            candidates=items,
            daily_cutoff=daily_cutoff,
            remote_root=remote_root,
            inventory_by_path=inventory_by_path,
            audit_proofs=audit_proofs,
        )
        for day, items in sorted(grouped.items())
    ]
    pending_operations = [
        operation
        for operation in operations
        if operation["status"] == "pending"
    ]
    blocked_operations = [
        operation
        for operation in operations
        if operation["status"] == "blocked"
    ]
    batches = _split_history_batches(
        pending_operations,
        request.batch_count,
    )
    read_probes = [
        _probe_remote_file_read(remote, operation["selected_source"])
        for operation in pending_operations
    ]

    deterministic_plan = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "operation": "historical_import",
        "timezone": "America/Sao_Paulo",
        "reference_date": reference_date.isoformat(),
        "remote": remote,
        "remote_root": remote_root.as_posix(),
        "policy": {
            "daily_retention_days": daily_days,
            "deep_retention_days": deep_days,
            "daily_cutoff_date": daily_cutoff.isoformat(),
            "deep_cutoff_date": deep_cutoff.isoformat(),
            "batch_count": request.batch_count,
        },
        "legacy_history": legacy_history,
        "expired_legacy": expired,
        "operations": operations,
        "batches": batches,
        "audit_evidence": {
            "valid_layer_proofs": sorted(
                audit_proofs.values(),
                key=lambda item: (item["remote_path"], item["audit_path"]),
            ),
            "ignored_manifests": ignored_audits,
        },
        "inventory_fingerprint": inventory,
    }
    plan_sha256 = _canonical_sha256(deterministic_plan)
    quota = _run_rclone_json("about", f"{remote}:", "--json")
    if not isinstance(quota, dict):
        raise LegacyMigrationPlanError(
            "A consulta de quota do OneDrive nao retornou um objeto JSON."
        )
    result = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "blocked" if blocked_operations else "completed",
        "mode": "history-plan",
        "generated_at": execution_now().isoformat(timespec="seconds"),
        "plan_sha256": plan_sha256,
        "plan": deterministic_plan,
        "summary": {
            "remote_files": len(inventory),
            "legacy_history_files": len(legacy_history),
            "eligible_legacy_files": len(eligible),
            "expired_legacy_files": len(expired),
            "historical_dates": len(operations),
            "pending_dates": len(pending_operations),
            "already_migrated_dates": sum(
                operation["status"] == "completed_existing"
                for operation in operations
            ),
            "blocked_dates": len(blocked_operations),
            "pending_daily_layers": sum(
                "daily" in operation["pending_layers"]
                for operation in pending_operations
            ),
            "pending_deep_layers": sum(
                "deep" in operation["pending_layers"]
                for operation in pending_operations
            ),
            "batch_sizes": [len(batch["operations"]) for batch in batches],
        },
        "onedrive_quota": quota,
        "runner": _runner_information(request.workspace_path),
        "safety": {
            "read_only": True,
            "selected_source_read_probes": read_probes,
            "downloaded_files": 0,
            "moved_files": 0,
            "removed_files": 0,
        },
    }
    write_json_atomic(request.output_path, result)
    request.report_path.parent.mkdir(parents=True, exist_ok=True)
    request.report_path.write_text(
        _render_history_plan_markdown(result),
        encoding="utf-8",
    )
    return result


def execute_legacy_history_batch(
    request: LegacyHistoryBatchRequest,
) -> dict[str, Any]:
    """Converte um lote aprovado sem remover os arquivos legados."""
    result: dict[str, Any] = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "running",
        "mode": "history-batch",
        "batch_number": request.batch_number,
        "execution_id": request.execution_id,
        "started_at": execution_now().isoformat(timespec="seconds"),
        "plan_sha256": None,
        "operations": [],
        "legacy_files_removed": 0,
        "collector_lock": {
            "variable": "DATA_MIGRATION_LOCKED",
            "changed_by_batch": False,
            "required_state_after_execution": "true",
        },
        "errors": [],
    }
    write_json_atomic(request.result_path, result)
    prepared_root: Path | None = None
    try:
        _require_history_migration_lock()
        approved = _load_approved_history_plan(
            request,
            required_confirmation=HISTORY_APPLY_CONFIRMATION,
        )
        plan = approved["plan"]
        result["plan_sha256"] = approved["plan_sha256"]
        batch = _history_plan_batch(plan, request.batch_number)
        operations = _history_plan_operations(plan)
        operation_dates = batch["operations"]
        selected_operations = [operations[item] for item in operation_dates]
        remote = validate_remote_name(plan["remote"])
        remote_root = normalize_remote_root(plan["remote_root"])
        policy = plan["policy"]
        reference_date = date.fromisoformat(plan["reference_date"])
        deep_cutoff = date.fromisoformat(policy["deep_cutoff_date"])

        prepared_root = _prepare_history_batch_root(request)
        inventory = _load_current_remote_inventory(
            remote,
            remote_root,
            deep_cutoff,
        )
        inventory_by_path = {
            item["relative_path"]: item
            for item in inventory
        }
        audit_proofs, ignored_audits = _collect_remote_audit_proofs(
            remote,
            remote_root,
            inventory,
        )
        result["remote_validation"] = {
            "inventory_files": len(inventory),
            "valid_layer_proofs": len(audit_proofs),
            "ignored_audit_manifests": ignored_audits,
        }
        write_json_atomic(request.result_path, result)

        for operation in selected_operations:
            try:
                operation_result = _execute_history_operation(
                    request=request,
                    operation=operation,
                    remote=remote,
                    remote_root=remote_root,
                    reference_date=reference_date,
                    batch_root=prepared_root,
                    inventory_by_path=inventory_by_path,
                    audit_proofs=audit_proofs,
                )
            except LegacyHistoryOperationError as error:
                result["operations"].append(error.result)
                write_json_atomic(request.result_path, result)
                raise LegacyMigrationPlanError(str(error)) from error
            result["operations"].append(operation_result)
            write_json_atomic(request.result_path, result)

        _remove_history_batch_root(prepared_root)
        prepared_root = None
        result["status"] = "completed"
        result["finished_at"] = execution_now().isoformat(timespec="seconds")
        result["next_actions"] = [
            "Preservar DATA_MIGRATION_LOCKED=true.",
            "Executar o proximo lote somente depois deste resultado.",
            "Nao remover os legados antes da validacao consolidada.",
        ]
        write_json_atomic(request.result_path, result)
        request.report_path.parent.mkdir(parents=True, exist_ok=True)
        request.report_path.write_text(
            _render_history_batch_markdown(result),
            encoding="utf-8",
        )
        return result
    except Exception as error:
        detail = sanitize_log_text(error)
        result["status"] = "failed"
        result["finished_at"] = execution_now().isoformat(timespec="seconds")
        result["errors"].append({"message": detail})
        result["next_actions"] = [
            "Manter DATA_MIGRATION_LOCKED=true.",
            "Nao iniciar os lotes seguintes.",
            "Revisar o relatorio e repetir este lote com o mesmo plano.",
        ]
        write_json_atomic(request.result_path, result)
        request.report_path.parent.mkdir(parents=True, exist_ok=True)
        request.report_path.write_text(
            _render_history_batch_markdown(result),
            encoding="utf-8",
        )
        raise LegacyMigrationConversionError(detail, result) from error
    finally:
        if prepared_root is not None:
            try:
                _remove_history_batch_root(prepared_root)
            except Exception as cleanup_error:
                result["cleanup_error"] = sanitize_log_text(cleanup_error)
                write_json_atomic(request.result_path, result)


def execute_legacy_history_finalization(
    request: LegacyHistoryFinalizeRequest,
) -> dict[str, Any]:
    """Valida os historicos publicados e remove os legados aprovados."""
    result: dict[str, Any] = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "running",
        "mode": "history-finalize",
        "execution_id": request.execution_id,
        "started_at": execution_now().isoformat(timespec="seconds"),
        "plan_sha256": None,
        "validation": None,
        "cleanup": {
            "status": "pending",
            "strategy": "exact_deletefile_resumable",
            "planned_files": [],
            "already_absent_files": [],
            "removed_files": [],
            "failed_files": [],
        },
        "preservation_validation": None,
        "onedrive_quota": {"before": None, "after": None},
        "runner": _runner_information(request.workspace_path),
        "audit_publication": None,
        "collector_lock": {
            "variable": "DATA_MIGRATION_LOCKED",
            "changed_by_finalization": False,
            "required_state_after_execution": "true",
        },
        "errors": [],
    }
    write_json_atomic(request.result_path, result)
    try:
        _require_history_migration_lock()
        approved = _load_approved_history_plan(
            request,
            required_confirmation=HISTORY_FINALIZE_CONFIRMATION,
        )
        plan = approved["plan"]
        result["plan_sha256"] = approved["plan_sha256"]
        remote = validate_remote_name(plan["remote"])
        remote_root = normalize_remote_root(plan["remote_root"])
        deep_cutoff = date.fromisoformat(
            plan["policy"]["deep_cutoff_date"]
        )
        inventory_before = _load_current_remote_inventory(
            remote,
            remote_root,
            deep_cutoff,
        )
        inventory_by_path = {
            item["relative_path"]: item
            for item in inventory_before
        }
        audit_proofs, ignored_audits = _collect_remote_audit_proofs(
            remote,
            remote_root,
            inventory_before,
        )
        target_proofs = _validate_completed_history_targets(
            plan,
            remote_root,
            inventory_by_path,
            audit_proofs,
        )
        planned_files = _history_cleanup_entries(plan, remote_root)
        planned_paths = {
            item["relative_path"]
            for item in planned_files
        }
        present_files: list[dict[str, Any]] = []
        already_absent_files: list[dict[str, Any]] = []
        for item in planned_files:
            observed = inventory_by_path.get(item["relative_path"])
            if observed is None:
                already_absent_files.append(
                    {**item, "result": "already_absent"}
                )
                continue
            _require_remote_entry_unchanged(remote, item)
            present_files.append(item)

        preserved_before = {
            item["relative_path"]: item
            for item in inventory_before
            if item["relative_path"] not in planned_paths
        }
        result["validation"] = {
            "status": "passed",
            "target_layers": target_proofs,
            "target_layer_count": len(target_proofs),
            "valid_audit_proofs": len(audit_proofs),
            "ignored_audit_manifests": ignored_audits,
            "planned_legacy_files": len(planned_files),
            "present_legacy_files": len(present_files),
            "already_absent_legacy_files": len(already_absent_files),
            "preserved_files_before_cleanup": len(preserved_before),
        }
        cleanup = result["cleanup"]
        cleanup["status"] = "running"
        cleanup["planned_files"] = planned_files
        cleanup["already_absent_files"] = already_absent_files
        result["onedrive_quota"]["before"] = _run_rclone_json(
            "about",
            f"{remote}:",
            "--json",
        )
        write_json_atomic(request.result_path, result)

        for item in present_files:
            try:
                _require_remote_entry_unchanged(remote, item)
                _run_rclone(
                    "deletefile",
                    f"{remote}:{item['remote_path']}",
                )
            except Exception as error:
                failed = {
                    **item,
                    "result": "failed",
                    "error": sanitize_log_text(error),
                    "finished_at": execution_now().isoformat(
                        timespec="seconds"
                    ),
                }
                cleanup["failed_files"].append(failed)
                cleanup["status"] = "failed"
                write_json_atomic(request.result_path, result)
                raise
            cleanup["removed_files"].append(
                {
                    **item,
                    "result": "removed",
                    "removed_at": execution_now().isoformat(
                        timespec="seconds"
                    ),
                }
            )
            write_json_atomic(request.result_path, result)

        inventory_after = _load_current_remote_inventory(
            remote,
            remote_root,
            deep_cutoff,
        )
        remaining_paths = {
            item["relative_path"]
            for item in inventory_after
        }.intersection(planned_paths)
        if remaining_paths:
            raise LegacyMigrationPlanError(
                "A limpeza terminou com legados planejados ainda presentes: "
                + sanitize_log_text(", ".join(sorted(remaining_paths)))
                + "."
            )
        preservation = _validate_preserved_remote_entries(
            preserved_before,
            inventory_after,
        )
        cleanup["status"] = "completed"
        cleanup["finished_at"] = execution_now().isoformat(
            timespec="seconds"
        )
        result["preservation_validation"] = preservation
        result["onedrive_quota"]["after"] = _run_rclone_json(
            "about",
            f"{remote}:",
            "--json",
        )
        result["status"] = "completed"
        result["finished_at"] = execution_now().isoformat(
            timespec="seconds"
        )
        result["next_actions"] = [
            "Revisar o relatorio consolidado e o manifesto remoto.",
            "Manter DATA_MIGRATION_LOCKED=true durante a revisao manual.",
            (
                "Desativar a trava somente depois de confirmar as evidencias "
                "da migracao historica."
            ),
        ]
        write_json_atomic(request.result_path, result)
        audit_publication = _publish_history_finalization_audit(
            request,
            result,
            remote,
            remote_root,
        )
        result["audit_publication"] = audit_publication
        write_json_atomic(request.result_path, result)
        if audit_publication.get("status") != "completed":
            raise LegacyMigrationPlanError(
                "O manifesto consolidado da migracao historica nao foi "
                "publicado."
            )
        request.report_path.parent.mkdir(parents=True, exist_ok=True)
        request.report_path.write_text(
            _render_history_finalization_markdown(result),
            encoding="utf-8",
        )
        return result
    except Exception as error:
        detail = sanitize_log_text(error)
        result["status"] = "failed"
        result["finished_at"] = execution_now().isoformat(
            timespec="seconds"
        )
        result["errors"].append({"message": detail})
        write_json_atomic(request.result_path, result)
        request.report_path.parent.mkdir(parents=True, exist_ok=True)
        request.report_path.write_text(
            _render_history_finalization_markdown(result),
            encoding="utf-8",
        )
        raise LegacyMigrationConversionError(detail, result) from error


def validate_legacy_migration_apply(
    request: LegacyMigrationApplyCheckRequest,
) -> dict[str, Any]:
    """Revalida plano, confirmacao e recursos antes de qualquer mutacao."""
    expected_sha256 = request.expected_plan_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise LegacyMigrationPlanError(
            "Informe um SHA-256 valido do plano aprovado."
        )
    if request.confirmation != APPLY_CONFIRMATION:
        raise LegacyMigrationPlanError(
            f"A confirmacao deve ser exatamente {APPLY_CONFIRMATION}."
        )

    current_plan = create_legacy_migration_plan(request.plan_request)
    current_sha256 = current_plan["plan_sha256"]
    if current_sha256 != expected_sha256:
        raise LegacyMigrationPlanError(
            "O inventario ou a politica mudou desde o plano aprovado. "
            f"Esperado {expected_sha256}, recalculado {current_sha256}."
        )

    legacy_latest = current_plan["plan"]["legacy_latest"]
    if len(legacy_latest) != 1:
        raise LegacyMigrationPlanError(
            "O apply exige exatamente um backup latest legado como origem."
        )

    runner = current_plan["runner"]
    memory_available = runner.get("memory_available_bytes")
    disk = runner.get("disk")
    disk_free = disk.get("free_bytes") if isinstance(disk, dict) else None
    required_memory = (
        ZPAQ_BENCHMARK_MAX_RSS_BYTES + MEMORY_SAFETY_MARGIN_BYTES
    )
    legacy_latest_size = legacy_latest[0]["size_bytes"]
    required_disk = max(
        MINIMUM_DISK_FREE_BYTES,
        legacy_latest_size * LEGACY_ARCHIVE_DISK_MULTIPLIER,
    )
    if not isinstance(memory_available, int) or memory_available < required_memory:
        raise LegacyMigrationPlanError(
            "Memoria disponivel insuficiente para a migracao: "
            f"necessario {required_memory}, disponivel {memory_available}."
        )
    if not isinstance(disk_free, int) or disk_free < required_disk:
        raise LegacyMigrationPlanError(
            "Disco livre insuficiente para a migracao: "
            f"necessario {required_disk}, disponivel {disk_free}."
        )

    result = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "completed",
        "mode": "apply-check",
        "checked_at": execution_now().isoformat(timespec="seconds"),
        "expected_plan_sha256": expected_sha256,
        "current_plan_sha256": current_sha256,
        "confirmation": "validated",
        "inventory_validation": "passed",
        "resources": {
            "memory": {
                "benchmark_peak_bytes": ZPAQ_BENCHMARK_MAX_RSS_BYTES,
                "safety_margin_bytes": MEMORY_SAFETY_MARGIN_BYTES,
                "required_bytes": required_memory,
                "available_bytes": memory_available,
                "status": "passed",
            },
            "disk": {
                "legacy_latest_size_bytes": legacy_latest_size,
                "multiplier": LEGACY_ARCHIVE_DISK_MULTIPLIER,
                "minimum_free_bytes": MINIMUM_DISK_FREE_BYTES,
                "required_bytes": required_disk,
                "available_bytes": disk_free,
                "status": "passed",
            },
        },
        "safety": {
            "preconditions_validated": True,
            "remote_mutations_executed": False,
            "downloaded_files": 0,
            "moved_files": 0,
            "removed_files": 0,
        },
    }
    write_json_atomic(request.output_path, result)
    request.report_path.parent.mkdir(parents=True, exist_ok=True)
    request.report_path.write_text(
        _render_apply_check_markdown(result),
        encoding="utf-8",
    )
    return result


def execute_legacy_migration_conversion(
    request: LegacyMigrationConversionRequest,
) -> dict[str, Any]:
    """Converte os dados e remove somente os legados aprovados pelo plano."""
    execution_id = request.execution_id.strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_-]{0,127}", execution_id):
        raise LegacyMigrationPlanError(
            "Identificador de execucao invalido para a conversao."
        )

    if request.operation_root.is_symlink():
        raise LegacyMigrationPlanError(
            "O diretorio temporario da conversao nao pode ser um link simbolico."
        )
    operation_root = request.operation_root.resolve(strict=False)
    workspace = (
        request.apply_check_request.plan_request.workspace_path.resolve(
            strict=True
        )
    )
    try:
        operation_relative = operation_root.relative_to(workspace)
    except ValueError as error:
        raise LegacyMigrationPlanError(
            "O diretorio temporario da conversao esta fora do workspace."
        ) from error
    if (
        operation_root.name != "operation"
        or operation_root.is_symlink()
        or len(operation_relative.parts) < 3
        or operation_relative.parts[0] != ".migration-work"
    ):
        raise LegacyMigrationPlanError(
            "O diretorio temporario da conversao deve ser "
            ".migration-work/<execucao>/operation."
        )
    if operation_root.exists() and any(operation_root.iterdir()):
        raise LegacyMigrationPlanError(
            "O diretorio temporario da conversao deve estar vazio."
        )
    operation_root.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "running",
        "mode": "apply",
        "execution_id": execution_id,
        "started_at": execution_now().isoformat(timespec="seconds"),
        "plan_sha256": None,
        "apply_check": None,
        "legacy_download": None,
        "restoration": None,
        "layered_backup": None,
        "legacy_cleanup": None,
        "legacy_latest_removal": {
            "authorized": False,
            "executed": False,
            "remote_path": None,
        },
        "collector_lock": {
            "variable": "DATA_MIGRATION_LOCKED",
            "changed_by_migration": False,
            "required_state_after_execution": "true",
        },
        "next_actions": [
            "Manter DATA_MIGRATION_LOCKED=true.",
            "Revisar o relatorio e os artifacts antes de qualquer nova acao.",
        ],
        "errors": [],
    }
    write_json_atomic(request.result_path, result)

    try:
        apply_check = validate_legacy_migration_apply(
            request.apply_check_request
        )
        result["apply_check"] = apply_check
        result["plan_sha256"] = apply_check["current_plan_sha256"]
        write_json_atomic(request.result_path, result)

        current_plan = _read_json_file(
            request.apply_check_request.plan_request.output_path
        )
        legacy_latest = current_plan["plan"]["legacy_latest"]
        legacy_entry = legacy_latest[0]
        remote = validate_remote_name(current_plan["plan"]["remote"])
        remote_root = normalize_remote_root(
            current_plan["plan"]["remote_root"]
        )
        relative_path = _safe_relative_path(legacy_entry["relative_path"])
        if not LEGACY_LATEST_PATTERN.fullmatch(relative_path.as_posix()):
            raise LegacyMigrationPlanError(
                "O plano recalculado nao aponta para um latest legado valido."
            )
        expected_remote_path = (remote_root / relative_path).as_posix()
        if legacy_entry["remote_path"] != expected_remote_path:
            raise LegacyMigrationPlanError(
                "O caminho do latest legado diverge da raiz aprovada."
            )

        downloads = operation_root / "downloads"
        restored_project = operation_root / "restored-project"
        packages = operation_root / "packages"
        downloads.mkdir()
        restored_project.mkdir()
        packages.mkdir()
        archive_path = downloads / relative_path.name

        download_started = execution_now()
        _run_rclone(
            "copyto",
            f"{remote}:{expected_remote_path}",
            archive_path.as_posix(),
        )
        if archive_path.is_symlink() or not archive_path.is_file():
            raise LegacyMigrationPlanError(
                "O download nao produziu um pacote legado regular."
            )
        downloaded_size = archive_path.stat().st_size
        if downloaded_size != legacy_entry["size_bytes"]:
            raise LegacyMigrationPlanError(
                "O tamanho baixado diverge do inventario aprovado: "
                f"esperado {legacy_entry['size_bytes']}, "
                f"recebido {downloaded_size}."
            )
        archive_sha256 = calculate_sha256(archive_path)
        result["legacy_download"] = {
            "status": "completed",
            "remote_path": expected_remote_path,
            "local_name": archive_path.name,
            "size_bytes": downloaded_size,
            "sha256": archive_sha256,
            "started_at": download_started.isoformat(timespec="seconds"),
            "finished_at": execution_now().isoformat(timespec="seconds"),
        }
        write_json_atomic(request.result_path, result)

        restoration = restore_backup(
            RestoreRequest(
                archive_path=archive_path,
                project_root=restored_project,
                expected_sha256=archive_sha256,
            )
        ).as_dict()
        result["restoration"] = restoration
        write_json_atomic(
            request.result_path.parent / "migration-restoration.json",
            restoration,
        )
        write_json_atomic(request.result_path, result)

        restored_data = restored_project / "data"
        execution_directory = (
            restored_data / "logs" / "execucao" / execution_id
        )
        context = ExecutionContext(
            execution_id=execution_id,
            directory=execution_directory,
            externally_managed=True,
            step_name="migration-backup",
        )
        context.initialize()
        context.record_stage("publishing-migrated-backups")

        layered_backup = run_layered_backup(
            LayeredBackupRequest(
                execution_id=execution_id,
                execution_directory=execution_directory,
                source_directory=restored_data,
                work_directory=packages,
                result_path=request.backup_result_path,
                remote=remote,
                remote_root=remote_root.as_posix(),
                backup_date=request.apply_check_request.plan_request.reference_date,
                pre_audit_action=lambda manifest_path: _execute_legacy_cleanup(
                    current_plan,
                    remote,
                    remote_root,
                    manifest_path,
                    request.result_path.parent
                    / "migration-legacy-cleanup.json",
                ),
            )
        )
        result["layered_backup"] = layered_backup
        result["legacy_cleanup"] = layered_backup.get("pre_audit_action")
        write_json_atomic(request.result_path, result)
        _preserve_backup_manifest(layered_backup, request.result_path.parent)

        latest = layered_backup.get("layers", {}).get("latest", {})
        publication = latest.get("publication", {})
        package = latest.get("package", {})
        latest_proven = (
            latest.get("status") == "completed"
            and package.get("validation") == "passed"
            and publication.get("status") == "completed"
            and publication.get("validation") == "passed"
            and package.get("sha256") == publication.get("sha256")
        )
        cleanup = result.get("legacy_cleanup")
        removed_files = (
            cleanup.get("removed_files", [])
            if isinstance(cleanup, dict)
            else []
        )
        legacy_latest_removed = any(
            isinstance(item, dict)
            and item.get("classification") == "legacy_latest"
            and item.get("result") == "removed"
            for item in removed_files
        )
        result["legacy_latest_removal"] = {
            "authorized": latest_proven,
            "executed": legacy_latest_removed,
            "remote_path": expected_remote_path,
            "reason": "new_latest_validated" if latest_proven else None,
            "new_latest_path": publication.get("final_path"),
            "new_latest_sha256": publication.get("sha256"),
        }
        write_json_atomic(request.result_path, result)

        if layered_backup.get("status") != "completed":
            raise LegacyMigrationPlanError(
                "A publicacao e a limpeza da migracao nao foram concluidas."
            )
        if not latest_proven:
            raise LegacyMigrationPlanError(
                "A nova camada latest nao possui prova completa de publicacao."
            )
        if not legacy_latest_removed:
            raise LegacyMigrationPlanError(
                "O latest legado nao foi removido ao final da limpeza."
            )
        result["status"] = "completed"
        result["finished_at"] = execution_now().isoformat(timespec="seconds")
        result["next_actions"] = [
            "Revisar o manifesto e o inventario remoto da migracao.",
            (
                "Revisar futuramente os legados recentes preservados quando "
                "eles ultrapassarem a retencao aprovada."
            ),
            (
                "Desativar DATA_MIGRATION_LOCKED manualmente somente depois "
                "da aprovacao das evidencias."
            ),
            "Executar o coletor normalmente apenas depois do desbloqueio.",
        ]
        write_json_atomic(request.result_path, result)
        request.report_path.parent.mkdir(parents=True, exist_ok=True)
        request.report_path.write_text(
            _render_conversion_markdown(result),
            encoding="utf-8",
        )
        return result
    except Exception as error:
        detail = sanitize_log_text(error)
        result["status"] = "failed"
        result["finished_at"] = execution_now().isoformat(timespec="seconds")
        result["errors"].append({"message": detail})
        write_json_atomic(request.result_path, result)
        request.report_path.parent.mkdir(parents=True, exist_ok=True)
        request.report_path.write_text(
            _render_conversion_markdown(result),
            encoding="utf-8",
        )
        raise LegacyMigrationConversionError(detail, result) from error
    finally:
        _remove_operation_root(operation_root)


def _execute_legacy_cleanup(
    current_plan: dict[str, Any],
    remote: str,
    remote_root: PurePosixPath,
    manifest_path: Path,
    evidence_path: Path,
) -> dict[str, Any]:
    """Remove somente legados aprovados, deixando o latest por ultimo."""
    deterministic_plan = current_plan.get("plan")
    if not isinstance(deterministic_plan, dict):
        raise LegacyMigrationPlanError(
            "O plano atual nao contem as decisoes da migracao."
        )
    policy = deterministic_plan.get("policy")
    if not isinstance(policy, dict):
        raise LegacyMigrationPlanError(
            "O plano atual nao contem a politica aprovada."
        )
    try:
        cutoff_date = date.fromisoformat(str(policy["cutoff_date"]))
    except (KeyError, ValueError) as error:
        raise LegacyMigrationPlanError(
            "A data de corte da migracao e invalida."
        ) from error

    raw_history = deterministic_plan.get("removal_candidates")
    raw_latest = deterministic_plan.get("legacy_latest")
    if not isinstance(raw_history, list) or not isinstance(raw_latest, list):
        raise LegacyMigrationPlanError(
            "O plano atual nao contem as listas de limpeza esperadas."
        )
    if len(raw_latest) != 1:
        raise LegacyMigrationPlanError(
            "A limpeza exige exatamente um latest legado aprovado."
        )

    history = [
        _validate_legacy_cleanup_entry(
            item,
            remote_root,
            expected_classification="legacy_history",
            cutoff_date=cutoff_date,
        )
        for item in raw_history
    ]
    history.sort(key=lambda item: item["remote_path"])
    latest = _validate_legacy_cleanup_entry(
        raw_latest[0],
        remote_root,
        expected_classification="legacy_latest",
        cutoff_date=cutoff_date,
    )
    planned_files = [*history, latest]
    planned_paths = [item["remote_path"] for item in planned_files]
    if len(planned_paths) != len(set(planned_paths)):
        raise LegacyMigrationPlanError(
            "O plano de limpeza contem caminhos legados duplicados."
        )

    result: dict[str, Any] = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "running",
        "started_at": execution_now().isoformat(timespec="seconds"),
        "strategy": "exact_deletefile_latest_last",
        "planned_files": planned_files,
        "removed_files": [],
        "failed_files": [],
        "preserved_legacy_files": deterministic_plan.get(
            "retained_legacy",
            [],
        ),
        "errors": [],
    }
    write_json_atomic(evidence_path, result)

    for item in planned_files:
        try:
            _require_remote_entry_unchanged(remote, item)
            _run_rclone(
                "deletefile",
                f"{remote}:{item['remote_path']}",
            )
        except Exception as error:
            detail = sanitize_log_text(error)
            failed = {
                **item,
                "result": "failed",
                "error": detail,
                "finished_at": execution_now().isoformat(
                    timespec="seconds"
                ),
            }
            result["failed_files"].append(failed)
            result["errors"].append(
                {"path": item["remote_path"], "message": detail}
            )
            result["status"] = "failed"
            break

        removed = {
            **item,
            "result": "removed",
            "removed_at": execution_now().isoformat(timespec="seconds"),
        }
        result["removed_files"].append(removed)
        write_json_atomic(evidence_path, result)

    if result["status"] == "running":
        result["status"] = "completed"
    result["finished_at"] = execution_now().isoformat(timespec="seconds")
    write_json_atomic(evidence_path, result)
    record_legacy_migration_cleanup(manifest_path, result)
    return result


def _validate_legacy_cleanup_entry(
    value: object,
    remote_root: PurePosixPath,
    *,
    expected_classification: str,
    cutoff_date: date,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LegacyMigrationPlanError(
            "O plano de limpeza contem uma entrada invalida."
        )
    raw_relative_path = value.get("relative_path")
    if not isinstance(raw_relative_path, str):
        raise LegacyMigrationPlanError(
            "O plano de limpeza contem um caminho relativo invalido."
        )
    relative_path = _safe_relative_path(raw_relative_path)
    if expected_classification == "legacy_latest":
        expected_pattern = LEGACY_LATEST_PATTERN
        expected_decision = "bootstrap_source"
    elif expected_classification == "legacy_history":
        expected_pattern = LEGACY_HISTORY_PATTERN
        expected_decision = "delete_after_replacement"
    else:
        raise LegacyMigrationPlanError(
            "Classificacao de limpeza legada desconhecida."
        )
    if not expected_pattern.fullmatch(relative_path.as_posix()):
        raise LegacyMigrationPlanError(
            f"Caminho nao autorizado para limpeza: {relative_path}."
        )
    if value.get("classification") != expected_classification:
        raise LegacyMigrationPlanError(
            f"Classificacao divergente para {relative_path}."
        )
    if value.get("decision") != expected_decision:
        raise LegacyMigrationPlanError(
            f"Decisao nao autorizada para {relative_path}."
        )

    remote_path = (remote_root / relative_path).as_posix()
    if value.get("remote_path") != remote_path:
        raise LegacyMigrationPlanError(
            f"Caminho fora da raiz aprovada: {relative_path}."
        )
    size_bytes = value.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise LegacyMigrationPlanError(
            f"Tamanho invalido no plano para {relative_path}."
        )

    backup_date = value.get("backup_date")
    if expected_classification == "legacy_history":
        try:
            parsed_date = date.fromisoformat(str(backup_date))
        except ValueError as error:
            raise LegacyMigrationPlanError(
                f"Data invalida no plano para {relative_path}."
            ) from error
        if parsed_date >= cutoff_date:
            raise LegacyMigrationPlanError(
                f"Arquivo ainda dentro da retencao: {relative_path}."
            )
    elif backup_date is not None:
        raise LegacyMigrationPlanError(
            f"O latest legado nao deve possuir data: {relative_path}."
        )

    hashes = value.get("hashes")
    if not isinstance(hashes, dict):
        raise LegacyMigrationPlanError(
            f"Hashes invalidos no plano para {relative_path}."
        )
    return {
        "relative_path": relative_path.as_posix(),
        "remote_path": remote_path,
        "size_bytes": size_bytes,
        "backup_date": backup_date,
        "classification": expected_classification,
        "decision": expected_decision,
        "hashes": hashes,
    }


def _require_remote_entry_unchanged(
    remote: str,
    approved: dict[str, Any],
) -> None:
    observed = _run_rclone_json(
        "lsjson",
        f"{remote}:{approved['remote_path']}",
        "--stat",
        "--hash",
    )
    if not isinstance(observed, dict):
        raise LegacyMigrationPlanError(
            f"Resposta remota invalida para {approved['remote_path']}."
        )
    if observed.get("Size") != approved["size_bytes"]:
        raise LegacyMigrationPlanError(
            f"O tamanho remoto mudou para {approved['remote_path']}."
        )
    observed_hashes = _normalize_hashes(observed.get("Hashes"))
    if approved["hashes"] and observed_hashes != approved["hashes"]:
        raise LegacyMigrationPlanError(
            f"Os hashes remotos mudaram para {approved['remote_path']}."
        )


def _normalize_remote_entry(
    value: object,
    remote_root: PurePosixPath,
    cutoff_date: date,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LegacyMigrationPlanError(
            "O inventario remoto contem uma entrada que nao e um objeto."
        )
    raw_path = value.get("Path")
    raw_size = value.get("Size")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise LegacyMigrationPlanError(
            "O inventario remoto contem uma entrada sem caminho."
        )
    if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
        raise LegacyMigrationPlanError(
            f"Tamanho remoto invalido para {sanitize_log_text(raw_path)}."
        )
    relative_path = _safe_relative_path(raw_path)
    full_path = remote_root / relative_path
    hashes = _normalize_hashes(value.get("Hashes"))
    mod_time = _normalize_optional_text(value.get("ModTime"))

    classification = "unmanaged"
    decision = "ignore"
    backup_date: date | None = None
    match = LEGACY_HISTORY_PATTERN.fullmatch(relative_path.as_posix())
    if LEGACY_LATEST_PATTERN.fullmatch(relative_path.as_posix()):
        classification = "legacy_latest"
        decision = "bootstrap_source"
    elif match is not None:
        classification = "legacy_history"
        backup_date = _legacy_timestamp_date(match.group("timestamp"))
        decision = (
            "delete_after_replacement"
            if backup_date < cutoff_date
            else "preserve_until_expiration"
        )
    elif relative_path.parts and relative_path.parts[0] == "backups":
        classification = "managed_new_layout"

    return {
        "relative_path": relative_path.as_posix(),
        "remote_path": full_path.as_posix(),
        "size_bytes": raw_size,
        "mod_time": mod_time,
        "hashes": hashes,
        "classification": classification,
        "backup_date": backup_date.isoformat() if backup_date else None,
        "decision": decision,
    }


def _load_approved_history_plan(
    request: LegacyHistoryBatchRequest | LegacyHistoryFinalizeRequest,
    *,
    required_confirmation: str,
) -> dict[str, Any]:
    expected_sha256 = request.expected_plan_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise LegacyMigrationPlanError(
            "Informe um SHA-256 valido do plano historico aprovado."
        )
    if request.confirmation != required_confirmation:
        raise LegacyMigrationPlanError(
            "A confirmacao deve ser exatamente "
            f"{required_confirmation}."
        )
    plan_path = request.approved_plan_path
    if plan_path.is_symlink():
        raise LegacyMigrationPlanError(
            "O plano historico aprovado nao pode ser um link simbolico."
        )
    resolved_plan = plan_path.resolve(strict=True)
    if not resolved_plan.is_file():
        raise LegacyMigrationPlanError(
            "O plano historico aprovado nao e um arquivo regular."
        )
    approved = _read_json_file(resolved_plan)
    if (
        approved.get("schema_version") != MIGRATION_PLAN_SCHEMA_VERSION
        or approved.get("mode") != "history-plan"
        or approved.get("status") != "completed"
    ):
        raise LegacyMigrationPlanError(
            "O arquivo informado nao e um plano historico concluido."
        )
    plan = approved.get("plan")
    if not isinstance(plan, dict) or plan.get("operation") != "historical_import":
        raise LegacyMigrationPlanError(
            "O plano aprovado nao descreve uma importacao historica."
        )
    calculated_sha256 = _canonical_sha256(plan)
    if (
        approved.get("plan_sha256") != expected_sha256
        or calculated_sha256 != expected_sha256
    ):
        raise LegacyMigrationPlanError(
            "O conteudo do plano historico diverge do SHA-256 aprovado."
        )
    _validate_history_plan_structure(plan)
    return approved


def _require_history_migration_lock() -> None:
    value = os.getenv("DATA_MIGRATION_LOCKED", "").strip().lower()
    if value != "true":
        raise LegacyMigrationPlanError(
            "Ative DATA_MIGRATION_LOCKED=true antes de executar um lote."
        )


def _validate_history_plan_structure(plan: dict[str, Any]) -> None:
    if (
        plan.get("schema_version") != MIGRATION_PLAN_SCHEMA_VERSION
        or plan.get("operation") != "historical_import"
        or plan.get("timezone") != "America/Sao_Paulo"
    ):
        raise LegacyMigrationPlanError(
            "A identificacao do plano historico e invalida."
        )
    try:
        reference_date = date.fromisoformat(plan["reference_date"])
        policy = plan["policy"]
        daily_days = _positive_retention_days(
            policy["daily_retention_days"],
            "diaria",
        )
        deep_days = _positive_retention_days(
            policy["deep_retention_days"],
            "profunda",
        )
        daily_cutoff = date.fromisoformat(policy["daily_cutoff_date"])
        deep_cutoff = date.fromisoformat(policy["deep_cutoff_date"])
        batch_count = policy["batch_count"]
    except (KeyError, TypeError, ValueError) as error:
        raise LegacyMigrationPlanError(
            "A politica do plano historico e invalida."
        ) from error
    if daily_cutoff != reference_date - timedelta(days=daily_days - 1):
        raise LegacyMigrationPlanError(
            "O corte diario diverge da politica do plano historico."
        )
    if deep_cutoff != reference_date - timedelta(days=deep_days - 1):
        raise LegacyMigrationPlanError(
            "O corte profundo diverge da politica do plano historico."
        )
    if (
        isinstance(batch_count, bool)
        or not isinstance(batch_count, int)
        or batch_count < 1
    ):
        raise LegacyMigrationPlanError(
            "A quantidade de lotes do plano historico e invalida."
        )
    remote_root = normalize_remote_root(plan.get("remote_root", ""))
    validate_remote_name(plan.get("remote", ""))
    operations = _history_plan_operations(plan)
    for operation in operations.values():
        _validate_planned_history_operation(
            operation,
            remote_root,
            reference_date,
            daily_cutoff,
            deep_cutoff,
        )
    batches = plan.get("batches")
    if not isinstance(batches, list) or len(batches) != batch_count:
        raise LegacyMigrationPlanError(
            "Os lotes divergem da politica do plano historico."
        )
    flattened: list[str] = []
    for expected_number, batch in enumerate(batches, start=1):
        if (
            not isinstance(batch, dict)
            or batch.get("number") != expected_number
            or not isinstance(batch.get("operations"), list)
            or any(
                not isinstance(item, str)
                for item in batch.get("operations", [])
            )
        ):
            raise LegacyMigrationPlanError(
                "O plano historico possui um lote invalido."
            )
        flattened.extend(batch["operations"])
    if len(flattened) != len(set(flattened)):
        raise LegacyMigrationPlanError(
            "Uma data aparece em mais de um lote historico."
        )
    expected_pending = sorted(
        day
        for day, operation in operations.items()
        if operation.get("status") == "pending"
    )
    if sorted(flattened) != expected_pending:
        raise LegacyMigrationPlanError(
            "Os lotes nao cobrem exatamente as datas pendentes."
        )


def _history_plan_operations(
    plan: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    raw_operations = plan.get("operations")
    if not isinstance(raw_operations, list):
        raise LegacyMigrationPlanError(
            "O plano historico nao possui operacoes validas."
        )
    operations: dict[str, dict[str, Any]] = {}
    for operation in raw_operations:
        if not isinstance(operation, dict):
            raise LegacyMigrationPlanError(
                "O plano historico possui uma operacao invalida."
            )
        raw_date = operation.get("backup_date")
        if not isinstance(raw_date, str):
            raise LegacyMigrationPlanError(
                "Uma operacao historica nao possui data valida."
            )
        try:
            date.fromisoformat(raw_date)
        except ValueError as error:
            raise LegacyMigrationPlanError(
                f"Data historica invalida no plano: {sanitize_log_text(raw_date)}."
            ) from error
        if raw_date in operations:
            raise LegacyMigrationPlanError(
                f"Data historica duplicada no plano: {raw_date}."
            )
        operations[raw_date] = operation
    return operations


def _history_plan_batch(
    plan: dict[str, Any],
    batch_number: int,
) -> dict[str, Any]:
    if isinstance(batch_number, bool) or not isinstance(batch_number, int):
        raise LegacyMigrationPlanError("O numero do lote deve ser um inteiro.")
    batches = plan.get("batches")
    if not isinstance(batches, list):
        raise LegacyMigrationPlanError("O plano historico nao possui lotes.")
    for batch in batches:
        if isinstance(batch, dict) and batch.get("number") == batch_number:
            return batch
    raise LegacyMigrationPlanError(
        f"O lote {batch_number} nao existe no plano historico."
    )


def _validate_planned_history_operation(
    operation: dict[str, Any],
    remote_root: PurePosixPath,
    reference_date: date,
    daily_cutoff: date,
    deep_cutoff: date,
) -> None:
    operation_date = date.fromisoformat(str(operation.get("backup_date")))
    if operation_date < deep_cutoff or operation_date > reference_date:
        raise LegacyMigrationPlanError(
            f"A data {operation_date} esta fora da retencao aprovada."
        )
    required_layers = (
        ["daily", "deep"]
        if operation_date >= daily_cutoff
        else ["deep"]
    )
    if operation.get("required_layers") != required_layers:
        raise LegacyMigrationPlanError(
            f"As camadas da data {operation_date} divergem da politica."
        )
    pending_layers = operation.get("pending_layers")
    blocked_layers = operation.get("blocked_layers")
    if (
        not isinstance(pending_layers, list)
        or len(pending_layers) != len(set(pending_layers))
        or not set(pending_layers).issubset(required_layers)
        or not isinstance(blocked_layers, list)
        or not set(blocked_layers).issubset(required_layers)
    ):
        raise LegacyMigrationPlanError(
            f"As decisoes da data {operation_date} sao invalidas."
        )
    expected_status = (
        "blocked"
        if blocked_layers
        else "pending"
        if pending_layers
        else "completed_existing"
    )
    if operation.get("status") != expected_status:
        raise LegacyMigrationPlanError(
            f"O status da data {operation_date} e inconsistente."
        )
    source = _validate_history_source(
        operation.get("selected_source"),
        operation_date,
        remote_root,
    )
    cleanup_candidates = operation.get("cleanup_candidates")
    duplicates = operation.get("duplicate_sources")
    if not isinstance(cleanup_candidates, list) or not isinstance(duplicates, list):
        raise LegacyMigrationPlanError(
            f"As fontes da data {operation_date} sao invalidas."
        )
    validated_cleanup = [
        _validate_history_source(item, operation_date, remote_root)
        for item in cleanup_candidates
    ]
    cleanup_paths = [item["remote_path"] for item in validated_cleanup]
    duplicate_paths = [
        _validate_history_source(item, operation_date, remote_root)[
            "remote_path"
        ]
        for item in duplicates
    ]
    if (
        len(cleanup_paths) != len(set(cleanup_paths))
        or source["remote_path"] not in cleanup_paths
        or sorted(duplicate_paths)
        != sorted(path for path in cleanup_paths if path != source["remote_path"])
    ):
        raise LegacyMigrationPlanError(
            f"A selecao das fontes da data {operation_date} e inconsistente."
        )
    latest_source = max(
        validated_cleanup,
        key=lambda item: (
            item["legacy_timestamp_utc"],
            item["legacy_run_id"],
            item["relative_path"],
        ),
    )
    if latest_source["remote_path"] != source["remote_path"]:
        raise LegacyMigrationPlanError(
            f"A fonte selecionada para {operation_date} nao e a mais recente."
        )
    targets = operation.get("targets")
    if not isinstance(targets, list) or len(targets) != len(required_layers):
        raise LegacyMigrationPlanError(
            f"Os destinos da data {operation_date} sao invalidos."
        )
    target_layers = [target.get("layer") for target in targets if isinstance(target, dict)]
    if target_layers != required_layers:
        raise LegacyMigrationPlanError(
            f"Os destinos da data {operation_date} estao fora de ordem."
        )
    validated_targets = [
        _validate_history_target(target, operation_date, remote_root)
        for target in targets
    ]
    target_pending = [
        target["layer"]
        for target in validated_targets
        if target["status"] == "missing"
    ]
    target_blocked = [
        target["layer"]
        for target in validated_targets
        if target["status"] == "existing_unverified"
    ]
    if target_pending != pending_layers or target_blocked != blocked_layers:
        raise LegacyMigrationPlanError(
            f"Os estados dos destinos de {operation_date} sao inconsistentes."
        )


def _validate_history_source(
    value: object,
    operation_date: date,
    remote_root: PurePosixPath,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LegacyMigrationPlanError("A fonte historica aprovada e invalida.")
    raw_relative_path = value.get("relative_path")
    if not isinstance(raw_relative_path, str):
        raise LegacyMigrationPlanError("A fonte historica nao possui caminho.")
    relative_path = _safe_relative_path(raw_relative_path)
    match = LEGACY_HISTORY_PATTERN.fullmatch(relative_path.as_posix())
    if match is None:
        raise LegacyMigrationPlanError(
            f"Fonte historica invalida para {operation_date}: {relative_path}."
        )
    timestamp = _legacy_timestamp(match.group("timestamp"))
    if timestamp.astimezone(SAO_PAULO).date() != operation_date:
        raise LegacyMigrationPlanError(
            f"Fonte historica invalida para {operation_date}: {relative_path}."
        )
    remote_path = (remote_root / relative_path).as_posix()
    size_bytes = value.get("size_bytes")
    hashes = value.get("hashes")
    if (
        value.get("remote_path") != remote_path
        or value.get("classification") != "legacy_history"
        or value.get("backup_date") != operation_date.isoformat()
        or value.get("legacy_timestamp_utc")
        != timestamp.isoformat(timespec="seconds")
        or value.get("legacy_run_id") != int(match.group("run_id"))
        or value.get("legacy_format") != match.group("format")
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 1
        or not isinstance(hashes, dict)
    ):
        raise LegacyMigrationPlanError(
            f"Metadados historicos invalidos para {relative_path}."
        )
    return value


def _validate_history_target(
    value: object,
    operation_date: date,
    remote_root: PurePosixPath,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LegacyMigrationPlanError("O destino historico aprovado e invalido.")
    try:
        layer = BackupLayer(str(value["layer"]))
    except (KeyError, ValueError) as error:
        raise LegacyMigrationPlanError(
            "O destino historico possui uma camada invalida."
        ) from error
    if layer not in {BackupLayer.DAILY, BackupLayer.DEEP}:
        raise LegacyMigrationPlanError(
            "Somente as camadas daily e deep sao permitidas na "
            "importacao historica."
        )
    specification = BackupLayerSpec.for_layer(layer, operation_date)
    expected_relative = (
        specification.remote_directory / specification.file_name
    ).as_posix()
    expected_remote = (remote_root / expected_relative).as_posix()
    if (
        value.get("format") != specification.format
        or value.get("relative_path") != expected_relative
        or value.get("remote_path") != expected_remote
        or value.get("status")
        not in {"missing", "validated_existing", "existing_unverified"}
    ):
        raise LegacyMigrationPlanError(
            f"Destino historico divergente para {layer.value} em {operation_date}."
        )
    return value


def _prepare_history_batch_root(
    request: LegacyHistoryBatchRequest,
) -> Path:
    execution_id = request.execution_id.strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_-]{0,79}", execution_id):
        raise LegacyMigrationPlanError(
            "Identificador de execucao invalido para o lote historico."
        )
    workspace = request.workspace_path.resolve(strict=True)
    if not workspace.is_dir():
        raise LegacyMigrationPlanError("O workspace do lote nao e um diretorio.")
    if request.operation_root.is_symlink():
        raise LegacyMigrationPlanError(
            "O diretorio temporario do lote nao pode ser um link simbolico."
        )
    operation_root = request.operation_root.resolve(strict=False)
    try:
        relative = operation_root.relative_to(workspace)
    except ValueError as error:
        raise LegacyMigrationPlanError(
            "O diretorio temporario do lote esta fora do workspace."
        ) from error
    expected_name = f"history-batch-{request.batch_number}"
    if (
        len(relative.parts) < 3
        or relative.parts[0] != ".migration-work"
        or operation_root.name != expected_name
    ):
        raise LegacyMigrationPlanError(
            "O diretorio temporario deve ser "
            ".migration-work/<execucao>/history-batch-<numero>."
        )
    for evidence_path in (request.result_path, request.report_path):
        if evidence_path.resolve(strict=False).is_relative_to(operation_root):
            raise LegacyMigrationPlanError(
                "As evidencias do lote devem ficar fora do diretorio temporario."
            )
    if operation_root.exists() and any(operation_root.iterdir()):
        raise LegacyMigrationPlanError(
            "O diretorio temporario do lote deve estar vazio."
        )
    operation_root.mkdir(parents=True, exist_ok=True)
    return operation_root


def _load_current_remote_inventory(
    remote: str,
    remote_root: PurePosixPath,
    deep_cutoff: date,
) -> list[dict[str, Any]]:
    raw_entries = _run_rclone_json(
        "lsjson",
        f"{remote}:{remote_root.as_posix()}",
        "--recursive",
        "--files-only",
        "--hash",
    )
    if not isinstance(raw_entries, list):
        raise LegacyMigrationPlanError(
            "A listagem atual do OneDrive nao retornou uma lista JSON."
        )
    inventory = [
        _normalize_remote_entry(item, remote_root, deep_cutoff)
        for item in raw_entries
    ]
    inventory.sort(key=lambda item: item["relative_path"])
    return inventory


def _execute_history_operation(
    *,
    request: LegacyHistoryBatchRequest,
    operation: dict[str, Any],
    remote: str,
    remote_root: PurePosixPath,
    reference_date: date,
    batch_root: Path,
    inventory_by_path: dict[str, dict[str, Any]],
    audit_proofs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    operation_date = date.fromisoformat(operation["backup_date"])
    compact_date = operation_date.strftime("%Y%m%d")
    evidence_path = (
        request.result_path.parent
        / f"history-operation-{compact_date}.json"
    )
    operation_result: dict[str, Any] = {
        "schema_version": MIGRATION_PLAN_SCHEMA_VERSION,
        "status": "running",
        "backup_date": operation_date.isoformat(),
        "started_at": execution_now().isoformat(timespec="seconds"),
        "selected_source": operation["selected_source"],
        "target_validation": None,
        "resources": None,
        "download": None,
        "restoration": None,
        "layers": {},
        "legacy_files_removed": 0,
        "errors": [],
    }
    write_json_atomic(evidence_path, operation_result)
    date_root = batch_root / f"date-{compact_date}"
    if date_root.exists() or date_root.is_symlink():
        raise LegacyHistoryOperationError(
            f"O diretorio temporario da data {operation_date} ja existe.",
            operation_result,
        )
    date_root.mkdir()
    try:
        source = _validate_history_source(
            operation["selected_source"],
            operation_date,
            remote_root,
        )
        _require_remote_entry_unchanged(remote, source)
        remaining_layers, completed_layers = _resolve_history_targets(
            operation,
            operation_date,
            remote_root,
            inventory_by_path,
            audit_proofs,
        )
        operation_result["target_validation"] = {
            "status": "passed",
            "pending_layers": remaining_layers,
            "already_completed_layers": completed_layers,
        }
        write_json_atomic(evidence_path, operation_result)
        if not remaining_layers:
            operation_result["status"] = "skipped_completed"
            operation_result["finished_at"] = execution_now().isoformat(
                timespec="seconds"
            )
            _remove_history_date_root(date_root, batch_root)
            write_json_atomic(evidence_path, operation_result)
            return operation_result

        resources = _validate_history_operation_resources(source, date_root)
        operation_result["resources"] = resources
        downloads = date_root / "downloads"
        restored_project = date_root / "restored-project"
        downloads.mkdir()
        restored_project.mkdir()
        relative_path = _safe_relative_path(source["relative_path"])
        archive_path = downloads / relative_path.name
        download_started = execution_now()
        _run_rclone(
            "copyto",
            f"{remote}:{source['remote_path']}",
            archive_path.as_posix(),
        )
        if archive_path.is_symlink() or not archive_path.is_file():
            raise LegacyMigrationPlanError(
                "O download historico nao produziu um arquivo regular."
            )
        downloaded_size = archive_path.stat().st_size
        if downloaded_size != source["size_bytes"]:
            raise LegacyMigrationPlanError(
                "O tamanho baixado diverge da fonte historica aprovada."
            )
        archive_sha256 = calculate_sha256(archive_path)
        operation_result["download"] = {
            "status": "completed",
            "remote_path": source["remote_path"],
            "local_name": archive_path.name,
            "size_bytes": downloaded_size,
            "sha256": archive_sha256,
            "started_at": download_started.isoformat(timespec="seconds"),
            "finished_at": execution_now().isoformat(timespec="seconds"),
        }
        write_json_atomic(evidence_path, operation_result)

        restoration = restore_backup(
            RestoreRequest(
                archive_path=archive_path,
                project_root=restored_project,
                expected_sha256=archive_sha256,
            )
        ).as_dict()
        operation_result["restoration"] = restoration
        write_json_atomic(evidence_path, operation_result)
        restored_data = restored_project / "data"

        for layer_name in remaining_layers:
            layer = BackupLayer(layer_name)
            layer_execution_id = _history_layer_execution_id(
                request.execution_id,
                compact_date,
                layer,
            )
            layer_execution_directory = (
                date_root / "execution" / layer.value
            )
            layer_execution_directory.mkdir(parents=True)
            layer_result_path = (
                request.result_path.parent
                / f"history-backup-{compact_date}-{layer.value}.json"
            )
            layered_backup = run_layered_backup(
                LayeredBackupRequest(
                    execution_id=layer_execution_id,
                    execution_directory=layer_execution_directory,
                    source_directory=restored_data,
                    work_directory=date_root / "packages" / layer.value,
                    result_path=layer_result_path,
                    remote=remote,
                    remote_root=remote_root.as_posix(),
                    backup_date=operation_date,
                    layers=frozenset({layer}),
                    apply_retention=False,
                    retention_reference_date=reference_date,
                )
            )
            manifest_name = (
                f"history-manifest-{compact_date}-{layer.value}.json"
            )
            _preserve_backup_manifest(
                layered_backup,
                request.result_path.parent,
                file_name=manifest_name,
            )
            proof = _require_historical_layer_publication(
                layered_backup,
                layer,
                operation_date,
                remote_root,
            )
            operation_result["layers"][layer.value] = proof
            write_json_atomic(evidence_path, operation_result)

        operation_result["status"] = "completed"
        operation_result["finished_at"] = execution_now().isoformat(
            timespec="seconds"
        )
        _remove_history_date_root(date_root, batch_root)
        write_json_atomic(evidence_path, operation_result)
        return operation_result
    except Exception as error:
        detail = sanitize_log_text(error)
        try:
            _remove_history_date_root(date_root, batch_root)
        except Exception as cleanup_error:
            detail = sanitize_log_text(f"{detail}; limpeza local: {cleanup_error}")
        operation_result["status"] = "failed"
        operation_result["finished_at"] = execution_now().isoformat(
            timespec="seconds"
        )
        operation_result["errors"].append({"message": detail})
        write_json_atomic(evidence_path, operation_result)
        raise LegacyHistoryOperationError(detail, operation_result) from error


def _resolve_history_targets(
    operation: dict[str, Any],
    operation_date: date,
    remote_root: PurePosixPath,
    inventory_by_path: dict[str, dict[str, Any]],
    audit_proofs: dict[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    pending_layers = set(operation["pending_layers"])
    remaining: list[str] = []
    completed: list[dict[str, Any]] = []
    for raw_target in operation["targets"]:
        target = _validate_history_target(
            raw_target,
            operation_date,
            remote_root,
        )
        layer_name = target["layer"]
        existing = inventory_by_path.get(target["relative_path"])
        proof = audit_proofs.get(target["remote_path"])
        if existing is None:
            if layer_name not in pending_layers:
                raise LegacyMigrationPlanError(
                    f"O destino aprovado desapareceu: {target['remote_path']}."
                )
            remaining.append(layer_name)
            continue
        if proof is None or proof["size_bytes"] != existing["size_bytes"]:
            raise LegacyMigrationPlanError(
                "Existe um destino sem prova valida no caminho "
                f"{target['remote_path']}."
            )
        completed.append(proof)
    return remaining, completed


def _validate_completed_history_targets(
    plan: dict[str, Any],
    remote_root: PurePosixPath,
    inventory_by_path: dict[str, dict[str, Any]],
    audit_proofs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    proofs: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for operation in _history_plan_operations(plan).values():
        operation_date = date.fromisoformat(operation["backup_date"])
        for raw_target in operation["targets"]:
            target = _validate_history_target(
                raw_target,
                operation_date,
                remote_root,
            )
            relative_path = target["relative_path"]
            remote_path = target["remote_path"]
            if remote_path in seen_paths:
                raise LegacyMigrationPlanError(
                    f"Destino historico duplicado no plano: {remote_path}."
                )
            seen_paths.add(remote_path)
            existing = inventory_by_path.get(relative_path)
            proof = audit_proofs.get(remote_path)
            if existing is None:
                raise LegacyMigrationPlanError(
                    f"Destino historico ausente: {remote_path}."
                )
            if (
                proof is None
                or proof.get("layer") != target["layer"]
                or proof.get("size_bytes") != existing["size_bytes"]
            ):
                raise LegacyMigrationPlanError(
                    "Destino historico sem manifesto valido: "
                    f"{remote_path}."
                )
            proofs.append(
                {
                    **proof,
                    "backup_date": operation_date.isoformat(),
                    "validation": "passed",
                }
            )
    return sorted(
        proofs,
        key=lambda item: (item["backup_date"], item["layer"]),
    )


def _history_cleanup_entries(
    plan: dict[str, Any],
    remote_root: PurePosixPath,
) -> list[dict[str, Any]]:
    raw_history = plan.get("legacy_history")
    raw_expired = plan.get("expired_legacy")
    if not isinstance(raw_history, list) or not isinstance(raw_expired, list):
        raise LegacyMigrationPlanError(
            "O plano historico nao possui o inventario de limpeza esperado."
        )
    deep_cutoff = date.fromisoformat(plan["policy"]["deep_cutoff_date"])
    history: list[dict[str, Any]] = []
    for item in raw_history:
        if not isinstance(item, dict):
            raise LegacyMigrationPlanError(
                "O inventario legado do plano possui uma entrada invalida."
            )
        try:
            operation_date = date.fromisoformat(str(item["backup_date"]))
        except (KeyError, ValueError) as error:
            raise LegacyMigrationPlanError(
                "O inventario legado do plano possui uma data invalida."
            ) from error
        history.append(
            _validate_history_source(item, operation_date, remote_root)
        )

    paths = [item["remote_path"] for item in history]
    if len(paths) != len(set(paths)):
        raise LegacyMigrationPlanError(
            "O plano historico possui caminhos legados duplicados."
        )
    expired_paths: set[str] = set()
    for item in raw_expired:
        if not isinstance(item, dict) or not isinstance(
            item.get("backup_date"),
            str,
        ):
            raise LegacyMigrationPlanError(
                "A lista de legados vencidos possui uma entrada invalida."
            )
        try:
            expired_date = date.fromisoformat(item["backup_date"])
        except ValueError as error:
            raise LegacyMigrationPlanError(
                "A lista de legados vencidos possui uma data invalida."
            ) from error
        expired = _validate_history_source(
            item,
            expired_date,
            remote_root,
        )
        if expired_date >= deep_cutoff:
            raise LegacyMigrationPlanError(
                "A lista de vencidos contem um legado dentro da retencao."
            )
        if expired["remote_path"] in expired_paths:
            raise LegacyMigrationPlanError(
                "A lista de legados vencidos possui caminhos duplicados."
            )
        expired_paths.add(expired["remote_path"])
    expected_expired_paths = {
        item["remote_path"]
        for item in history
        if date.fromisoformat(item["backup_date"]) < deep_cutoff
    }
    if expired_paths != expected_expired_paths:
        raise LegacyMigrationPlanError(
            "Os legados vencidos divergem do inventario aprovado."
        )

    operation_path_list: list[str] = []
    for operation in _history_plan_operations(plan).values():
        for item in operation["cleanup_candidates"]:
            operation_path_list.append(item["remote_path"])
    operation_paths = set(operation_path_list)
    if len(operation_paths) != len(operation_path_list):
        raise LegacyMigrationPlanError(
            "Uma fonte legada aparece em mais de uma data da limpeza."
        )
    expected_operation_paths = set(paths) - expected_expired_paths
    if operation_paths != expected_operation_paths:
        raise LegacyMigrationPlanError(
            "As fontes das datas migradas divergem da limpeza aprovada."
        )
    return sorted(history, key=lambda item: item["remote_path"])


def _validate_preserved_remote_entries(
    preserved_before: dict[str, dict[str, Any]],
    inventory_after: list[dict[str, Any]],
) -> dict[str, Any]:
    after_by_path = {
        item["relative_path"]: item
        for item in inventory_after
    }
    for relative_path, expected in preserved_before.items():
        observed = after_by_path.get(relative_path)
        if observed is None:
            raise LegacyMigrationPlanError(
                "Um arquivo fora da limpeza desapareceu: "
                f"{relative_path}."
            )
        if observed["size_bytes"] != expected["size_bytes"]:
            raise LegacyMigrationPlanError(
                "Um arquivo fora da limpeza mudou de tamanho: "
                f"{relative_path}."
            )
        if expected["hashes"] and observed["hashes"] != expected["hashes"]:
            raise LegacyMigrationPlanError(
                "Os hashes de um arquivo preservado mudaram: "
                f"{relative_path}."
            )
    return {
        "status": "passed",
        "verified_files": len(preserved_before),
        "new_files_ignored": len(set(after_by_path) - set(preserved_before)),
    }


def _publish_history_finalization_audit(
    request: LegacyHistoryFinalizeRequest,
    result: dict[str, Any],
    remote: str,
    remote_root: PurePosixPath,
) -> dict[str, Any]:
    created_at = execution_now()
    file_name = f"backup-{created_at.strftime('%Y%m%d-%H%M%S')}.json"
    audit_path = request.result_path.parent / file_name
    audit_payload = json.loads(json.dumps(result, ensure_ascii=False))
    audit_payload["document_type"] = "historical_migration_finalization"
    audit_payload["file_name"] = file_name
    audit_payload["audit_remote_path"] = (
        remote_root / "backups" / "audit" / file_name
    ).as_posix()
    write_json_atomic(audit_path, audit_payload)
    try:
        publication = publish_backup_atomically(
            RemotePublicationRequest(
                layer="audit",
                local_file=audit_path,
                destination_path=f"backups/audit/{file_name}",
                execution_id=request.execution_id,
                remote=remote,
                remote_root=remote_root.as_posix(),
            )
        ).as_dict()
    except RemotePublicationError as error:
        return error.result.as_dict()
    except Exception as error:
        return {
            "status": "failed",
            "validation": "failed",
            "final_path": audit_payload["audit_remote_path"],
            "error": sanitize_log_text(error),
        }
    if (
        publication.get("status") != "completed"
        or publication.get("validation") != "passed"
        or publication.get("already_present")
    ):
        publication["status"] = "failed"
        publication["error"] = (
            "O manifesto consolidado nao foi publicado como um novo arquivo."
        )
    return publication


def _validate_history_operation_resources(
    source: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    memory = _memory_information()
    available_memory = memory.get("memory_available_bytes")
    required_memory = ZPAQ_BENCHMARK_MAX_RSS_BYTES + MEMORY_SAFETY_MARGIN_BYTES
    disk = shutil.disk_usage(workspace.resolve(strict=True))
    required_disk = max(
        MINIMUM_DISK_FREE_BYTES,
        source["size_bytes"] * LEGACY_ARCHIVE_DISK_MULTIPLIER,
    )
    if not isinstance(available_memory, int) or available_memory < required_memory:
        raise LegacyMigrationPlanError(
            "Memoria disponivel insuficiente para a data historica: "
            f"necessario {required_memory}, disponivel {available_memory}."
        )
    if disk.free < required_disk:
        raise LegacyMigrationPlanError(
            "Disco livre insuficiente para a data historica: "
            f"necessario {required_disk}, disponivel {disk.free}."
        )
    return {
        "status": "passed",
        "memory": {
            "required_bytes": required_memory,
            "available_bytes": available_memory,
        },
        "disk": {
            "required_bytes": required_disk,
            "available_bytes": disk.free,
        },
    }


def _history_layer_execution_id(
    base: str,
    compact_date: str,
    layer: BackupLayer,
) -> str:
    execution_id = f"{base}_{compact_date}_{layer.value}"
    if len(execution_id) > 128:
        raise LegacyMigrationPlanError(
            "O identificador da camada historica excede 128 caracteres."
        )
    return execution_id


def _require_historical_layer_publication(
    layered_backup: dict[str, Any],
    layer: BackupLayer,
    operation_date: date,
    remote_root: PurePosixPath,
) -> dict[str, Any]:
    if layered_backup.get("status") != "completed":
        raise LegacyMigrationPlanError(
            f"A publicacao historica {layer.value} nao foi concluida."
        )
    if layered_backup.get("requested_layers") != [layer.value]:
        raise LegacyMigrationPlanError(
            "A publicacao historica tentou processar camadas nao autorizadas."
        )
    latest = layered_backup.get("layers", {}).get("latest", {})
    if latest.get("status") != "skipped_not_requested":
        raise LegacyMigrationPlanError(
            "A camada latest nao foi isolada da importacao historica."
        )
    layer_result = layered_backup.get("layers", {}).get(layer.value, {})
    package = layer_result.get("package", {})
    publication = layer_result.get("publication", {})
    specification = BackupLayerSpec.for_layer(layer, operation_date)
    expected_path = (
        remote_root
        / specification.remote_directory
        / specification.file_name
    ).as_posix()
    if (
        layer_result.get("status") != "completed"
        or package.get("validation") != "passed"
        or publication.get("status") != "completed"
        or publication.get("validation") != "passed"
        or package.get("sha256") != publication.get("sha256")
        or publication.get("final_path") != expected_path
    ):
        raise LegacyMigrationPlanError(
            f"A prova de publicacao {layer.value} esta incompleta."
        )
    audit = layered_backup.get("audit_publication")
    retention = layered_backup.get("retention")
    if not isinstance(audit, dict) or audit.get("status") != "completed":
        raise LegacyMigrationPlanError(
            f"O manifesto remoto da camada {layer.value} nao foi publicado."
        )
    if not isinstance(retention, dict) or retention.get("mode") != "dry-run":
        raise LegacyMigrationPlanError(
            "A retencao nao permaneceu em modo somente leitura."
        )
    return {
        "status": "completed",
        "layer": layer.value,
        "remote_path": expected_path,
        "size_bytes": package.get("size_bytes"),
        "sha256": package.get("sha256"),
        "manifest_path": audit.get("final_path"),
        "publication": publication,
    }


def _remove_history_date_root(path: Path, batch_root: Path) -> None:
    if not path.exists():
        return
    if (
        path.is_symlink()
        or path.parent != batch_root
        or not re.fullmatch(r"date-[0-9]{8}", path.name)
    ):
        raise LegacyMigrationPlanError(
            f"Recusa ao limpar diretorio historico inseguro: {path}."
        )
    shutil.rmtree(path)


def _remove_history_batch_root(path: Path) -> None:
    if not path.exists():
        return
    if (
        path.is_symlink()
        or not re.fullmatch(r"history-batch-[1-9][0-9]*", path.name)
        or len(path.parts) < 3
        or path.parent.parent.name != ".migration-work"
    ):
        raise LegacyMigrationPlanError(
            f"Recusa ao limpar lote historico inseguro: {path}."
        )
    shutil.rmtree(path)


def _historical_legacy_entry(entry: dict[str, Any]) -> dict[str, Any]:
    relative_path = str(entry["relative_path"])
    match = LEGACY_HISTORY_PATTERN.fullmatch(relative_path)
    if match is None:
        raise LegacyMigrationPlanError(
            "Uma entrada historica nao corresponde ao formato legado: "
            f"{sanitize_log_text(relative_path)}."
        )
    timestamp = _legacy_timestamp(match.group("timestamp"))
    return {
        **entry,
        "legacy_timestamp_utc": timestamp.isoformat(timespec="seconds"),
        "legacy_run_id": int(match.group("run_id")),
        "legacy_format": match.group("format"),
    }


def _historical_operation(
    *,
    backup_date: date,
    candidates: list[dict[str, Any]],
    daily_cutoff: date,
    remote_root: PurePosixPath,
    inventory_by_path: dict[str, dict[str, Any]],
    audit_proofs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            item["legacy_timestamp_utc"],
            item["legacy_run_id"],
            item["relative_path"],
        ),
    )
    selected = ordered[-1]
    duplicates = ordered[:-1]
    required_layers = (
        [BackupLayer.DAILY, BackupLayer.DEEP]
        if backup_date >= daily_cutoff
        else [BackupLayer.DEEP]
    )
    targets = [
        _historical_target(
            layer=layer,
            backup_date=backup_date,
            remote_root=remote_root,
            inventory_by_path=inventory_by_path,
            audit_proofs=audit_proofs,
        )
        for layer in required_layers
    ]
    pending_layers = [
        target["layer"] for target in targets if target["status"] == "missing"
    ]
    blocked_layers = [
        target["layer"]
        for target in targets
        if target["status"] == "existing_unverified"
    ]
    if blocked_layers:
        status = "blocked"
    elif pending_layers:
        status = "pending"
    else:
        status = "completed_existing"
    return {
        "backup_date": backup_date.isoformat(),
        "status": status,
        "selected_source": selected,
        "duplicate_sources": duplicates,
        "cleanup_candidates": ordered,
        "required_layers": [layer.value for layer in required_layers],
        "pending_layers": pending_layers,
        "blocked_layers": blocked_layers,
        "targets": targets,
    }


def _historical_target(
    *,
    layer: BackupLayer,
    backup_date: date,
    remote_root: PurePosixPath,
    inventory_by_path: dict[str, dict[str, Any]],
    audit_proofs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    specification = BackupLayerSpec.for_layer(layer, backup_date)
    relative_path = (
        specification.remote_directory / specification.file_name
    ).as_posix()
    remote_path = (remote_root / relative_path).as_posix()
    existing = inventory_by_path.get(relative_path)
    proof = audit_proofs.get(remote_path)
    if existing is None:
        status = "missing"
        proof = None
    elif proof is not None and proof["size_bytes"] == existing["size_bytes"]:
        status = "validated_existing"
    else:
        status = "existing_unverified"
        proof = None
    return {
        "layer": layer.value,
        "format": specification.format,
        "relative_path": relative_path,
        "remote_path": remote_path,
        "status": status,
        "existing_file": existing,
        "manifest_proof": proof,
    }


def _collect_remote_audit_proofs(
    remote: str,
    remote_root: PurePosixPath,
    inventory: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    inventory_by_remote_path = {
        item["remote_path"]: item
        for item in inventory
    }
    audit_entries = sorted(
        (
            item
            for item in inventory
            if PurePosixPath(item["relative_path"]).parent
            == PurePosixPath("backups/audit")
            and PurePosixPath(item["relative_path"]).suffix == ".json"
        ),
        key=lambda item: item["relative_path"],
    )
    proofs: dict[str, dict[str, Any]] = {}
    ignored: list[dict[str, str]] = []
    for audit in audit_entries:
        if audit["size_bytes"] > MAX_AUDIT_MANIFEST_BYTES:
            ignored.append(
                {
                    "audit_path": audit["remote_path"],
                    "reason": "manifest_too_large",
                }
            )
            continue
        try:
            completed = _run_rclone(
                "cat",
                f"{remote}:{audit['remote_path']}",
            )
            payload = json.loads(completed.stdout)
            layer_proofs = _audit_manifest_proofs(
                payload,
                audit["remote_path"],
                remote_root,
                inventory_by_remote_path,
            )
        except (
            json.JSONDecodeError,
            LegacyMigrationPlanError,
            RuntimeError,
        ) as error:
            ignored.append(
                {
                    "audit_path": audit["remote_path"],
                    "reason": sanitize_log_text(error),
                }
            )
            continue
        for proof in layer_proofs:
            proofs[proof["remote_path"]] = proof
    return proofs, ignored


def _audit_manifest_proofs(
    payload: object,
    audit_path: str,
    remote_root: PurePosixPath,
    inventory_by_remote_path: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if (
        not isinstance(payload, dict)
        or payload.get("status") not in {"completed", "failed"}
    ):
        raise LegacyMigrationPlanError(
            "O manifesto nao registra uma execucao finalizada."
        )
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise LegacyMigrationPlanError("O manifesto nao possui camadas validas.")
    proofs: list[dict[str, Any]] = []
    for layer in (BackupLayer.DAILY, BackupLayer.DEEP):
        data = layers.get(layer.value)
        if not isinstance(data, dict) or data.get("status") != "uploaded":
            continue
        package = data.get("package")
        validation = data.get("validation")
        upload = data.get("upload")
        if (
            not isinstance(package, dict)
            or package.get("status") != "completed"
            or not isinstance(validation, dict)
            or validation.get("status") != "passed"
            or not isinstance(upload, dict)
            or upload.get("status") != "completed"
        ):
            continue
        remote_path = upload.get("final_path")
        size_bytes = package.get("size_bytes")
        sha256 = package.get("sha256")
        if (
            not isinstance(remote_path, str)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            continue
        full_path = _safe_relative_path(remote_path)
        try:
            managed_path = full_path.relative_to(remote_root)
        except ValueError:
            continue
        try:
            validate_layer_remote_path(layer.value, managed_path)
        except ValueError:
            continue
        target_backup_date = layer_file_date(layer.value, managed_path.name)
        raw_backup_date = payload.get("backup_date")
        if raw_backup_date is None:
            manifest_backup_date = target_backup_date
        else:
            try:
                manifest_backup_date = date.fromisoformat(raw_backup_date)
            except (TypeError, ValueError):
                continue
        if target_backup_date != manifest_backup_date:
            continue
        if (
            data.get("file_name") != managed_path.name
            or data.get("remote_directory") != managed_path.parent.as_posix()
        ):
            continue
        remote_entry = inventory_by_remote_path.get(full_path.as_posix())
        if remote_entry is None or remote_entry["size_bytes"] != size_bytes:
            continue
        proofs.append(
            {
                "layer": layer.value,
                "remote_path": full_path.as_posix(),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "audit_path": audit_path,
                "execution_id": payload.get("execution_id"),
                "manifest_status": payload.get("status"),
            }
        )
    return proofs


def _split_history_batches(
    operations: list[dict[str, Any]],
    batch_count: int,
) -> list[dict[str, Any]]:
    quotient, remainder = divmod(len(operations), batch_count)
    batches: list[dict[str, Any]] = []
    offset = 0
    for index in range(batch_count):
        size = quotient + (1 if index < remainder else 0)
        selected = operations[offset : offset + size]
        offset += size
        batches.append(
            {
                "number": index + 1,
                "operations": [item["backup_date"] for item in selected],
            }
        )
    return batches


def _probe_remote_file_read(
    remote: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    remote_path = entry.get("remote_path")
    if not isinstance(remote_path, str) or not remote_path:
        raise LegacyMigrationPlanError(
            "A origem historica selecionada nao possui caminho remoto."
        )
    try:
        completed = subprocess.run(
            [
                "rclone",
                "cat",
                f"{remote}:{remote_path}",
                "--offset",
                "0",
                "--count",
                "1",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = (error.stderr or b"").decode(errors="replace").strip()
        suffix = f": {sanitize_log_text(detail)}" if detail else ""
        raise LegacyMigrationPlanError(
            "Nao foi possivel ler a origem historica selecionada "
            f"{sanitize_log_text(remote_path)}{suffix}."
        ) from error
    if len(completed.stdout) != 1:
        raise LegacyMigrationPlanError(
            "A prova de leitura da origem historica nao retornou um byte: "
            f"{sanitize_log_text(remote_path)}."
        )
    return {
        "status": "passed",
        "remote_path": remote_path,
        "bytes_read": 1,
    }


def _positive_retention_days(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LegacyMigrationPlanError(
            f"A retencao {label} deve ser um inteiro maior ou igual a 1."
        )
    return value


def _safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise LegacyMigrationPlanError(
            f"Caminho remoto inseguro no inventario: {sanitize_log_text(value)}."
        )
    return path


def _legacy_timestamp_date(value: str) -> date:
    return _legacy_timestamp(value).astimezone(SAO_PAULO).date()


def _legacy_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise LegacyMigrationPlanError(
            f"Timestamp legado invalido: {sanitize_log_text(value)}."
        ) from error


def _normalize_hashes(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LegacyMigrationPlanError("Hashes remotos em formato invalido.")
    hashes: dict[str, str] = {}
    for name, digest in sorted(value.items(), key=lambda item: str(item[0])):
        if isinstance(name, str) and isinstance(digest, str):
            hashes[sanitize_log_text(name)] = sanitize_log_text(digest)
    return hashes


def _normalize_optional_text(value: object) -> str | None:
    return sanitize_log_text(value) if isinstance(value, str) else None


def _canonical_sha256(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _runner_information(workspace_path: Path) -> dict[str, Any]:
    memory = _memory_information()
    disk = shutil.disk_usage(workspace_path.resolve(strict=True))
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "cpu_count": os.cpu_count(),
        **memory,
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        "github": {
            "run_id": os.getenv("GITHUB_RUN_ID"),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
            "sha": os.getenv("GITHUB_SHA"),
        },
    }


def _memory_information() -> dict[str, int | None]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            name, raw_value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                values[name] = int(raw_value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {
        "memory_total_bytes": values.get("MemTotal"),
        "memory_available_bytes": values.get("MemAvailable"),
    }


def _run_rclone_json(*arguments: str) -> object:
    if shutil.which("rclone") is None:
        raise LegacyMigrationPlanError("rclone nao encontrado no ambiente.")
    try:
        completed = subprocess.run(
            ["rclone", *arguments],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError as error:
        detail = sanitize_log_text(error.stderr.strip() or error.stdout.strip())
        suffix = f": {detail}" if detail else ""
        raise LegacyMigrationPlanError(
            f"rclone {arguments[0]} encerrou com codigo {error.returncode}{suffix}."
        ) from error
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LegacyMigrationPlanError(
            f"Saida JSON invalida do rclone {arguments[0]}: {error}."
        ) from error


def _probe_legacy_latest_read(
    remote: str,
    legacy_latest: list[dict[str, Any]],
) -> dict[str, Any]:
    """Comprova acesso ao conteudo sem baixar o pacote completo."""
    if not legacy_latest:
        return {
            "status": "skipped",
            "reason": "legacy_latest_not_found",
            "bytes_read": 0,
        }
    remote_path = legacy_latest[0]["remote_path"]
    try:
        completed = subprocess.run(
            [
                "rclone",
                "cat",
                f"{remote}:{remote_path}",
                "--head",
                "1",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        detail = sanitize_log_text(
            error.stderr.decode("utf-8", errors="replace").strip()
        )
        suffix = f": {detail}" if detail else ""
        raise LegacyMigrationPlanError(
            "Nao foi possivel ler o conteudo do latest legado"
            f"{suffix}."
        ) from error
    if len(completed.stdout) != 1:
        raise LegacyMigrationPlanError(
            "A prova de leitura do latest legado nao retornou um byte."
        )
    return {
        "status": "passed",
        "remote_path": remote_path,
        "bytes_read": 1,
    }


def _run_rclone(*arguments: str) -> subprocess.CompletedProcess[str]:
    allowed_operations = {"cat", "copyto", "deletefile"}
    if not arguments or arguments[0] not in allowed_operations:
        raise LegacyMigrationPlanError(
            "A migracao recusou uma operacao rclone nao autorizada."
        )
    if shutil.which("rclone") is None:
        raise LegacyMigrationPlanError("rclone nao encontrado no ambiente.")
    try:
        return subprocess.run(
            ["rclone", *arguments],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError as error:
        detail = sanitize_log_text(error.stderr.strip() or error.stdout.strip())
        suffix = f": {detail}" if detail else ""
        operation = arguments[0]
        raise LegacyMigrationPlanError(
            f"rclone {operation} encerrou com codigo "
            f"{error.returncode}{suffix}."
        ) from error


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LegacyMigrationPlanError(
            f"JSON de migracao invalido em {path}: {error}."
        ) from error
    if not isinstance(payload, dict):
        raise LegacyMigrationPlanError(
            f"O JSON de migracao em {path} deve ser um objeto."
        )
    return payload


def _preserve_backup_manifest(
    layered_backup: dict[str, Any],
    evidence_directory: Path,
    *,
    file_name: str = "migration-backup-manifest.json",
) -> None:
    raw_manifest_path = layered_backup.get("manifest_path")
    if not isinstance(raw_manifest_path, str) or not raw_manifest_path:
        raise LegacyMigrationPlanError(
            "O backup em camadas nao informou o manifesto local."
        )
    manifest_candidate = Path(raw_manifest_path)
    if manifest_candidate.is_symlink():
        raise LegacyMigrationPlanError(
            "O manifesto do backup em camadas nao pode ser um link simbolico."
        )
    manifest_path = manifest_candidate.resolve(strict=True)
    if not manifest_path.is_file():
        raise LegacyMigrationPlanError(
            "O manifesto do backup em camadas nao e um arquivo regular."
        )
    if (
        PurePosixPath(file_name).name != file_name
        or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*\.json", file_name)
    ):
        raise LegacyMigrationPlanError(
            "Nome invalido para preservar o manifesto da migracao."
        )
    evidence_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        manifest_path,
        evidence_directory / file_name,
    )


def _remove_operation_root(operation_root: Path) -> None:
    if not operation_root.exists():
        return
    if operation_root.name != "operation" or operation_root.is_symlink():
        raise LegacyMigrationPlanError(
            f"Recusa ao limpar diretorio temporario inseguro: {operation_root}."
        )
    shutil.rmtree(operation_root)


def _render_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    runner = result["runner"]
    disk = runner["disk"]
    plan = result["plan"]
    lines = [
        "# Plano da migracao dos backups legados",
        "",
        f"- SHA-256 do plano: `{result['plan_sha256']}`",
        f"- Data de referencia: `{plan['reference_date']}`",
        f"- Corte da retencao profunda: `{plan['policy']['cutoff_date']}`",
        f"- Arquivos remotos: {summary['remote_files']}",
        f"- Latest legados: {summary['legacy_latest_files']}",
        f"- Historicos legados: {summary['legacy_history_files']}",
        f"- Candidatos a remocao: {summary['removal_candidates']}",
        f"- Bytes candidatos a remocao: {summary['removal_candidate_bytes']}",
        f"- Historicos ainda retidos: {summary['retained_legacy_files']}",
        "",
        "## Recursos do runner",
        "",
        f"- Memoria total: {runner['memory_total_bytes']}",
        f"- Memoria disponivel: {runner['memory_available_bytes']}",
        f"- Disco total: {disk['total_bytes']}",
        f"- Disco livre: {disk['free_bytes']}",
        "",
        "## Garantia desta execucao",
        "",
        (
            "O modo `plan` apenas consultou metadados e quota. Nenhum arquivo "
            "foi baixado, movido ou removido."
        ),
        "",
    ]
    return "\n".join(lines)


def _render_history_plan_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    plan = result["plan"]
    lines = [
        "# Plano da migracao historica dos backups legados",
        "",
        f"- Status: `{result['status']}`",
        f"- SHA-256 do plano: `{result['plan_sha256']}`",
        f"- Data de referencia: `{plan['reference_date']}`",
        (
            "- Corte diario: `"
            f"{plan['policy']['daily_cutoff_date']}`"
        ),
        (
            "- Corte profundo: `"
            f"{plan['policy']['deep_cutoff_date']}`"
        ),
        f"- Arquivos legados elegiveis: {summary['eligible_legacy_files']}",
        f"- Datas historicas: {summary['historical_dates']}",
        f"- Datas pendentes: {summary['pending_dates']}",
        (
            "- Datas ja migradas e comprovadas: "
            f"{summary['already_migrated_dates']}"
        ),
        f"- Datas bloqueadas: {summary['blocked_dates']}",
        f"- Camadas daily pendentes: {summary['pending_daily_layers']}",
        f"- Camadas deep pendentes: {summary['pending_deep_layers']}",
        "- Tamanhos dos lotes: `"
        + ", ".join(str(size) for size in summary["batch_sizes"])
        + "`",
        "",
        "## Lotes",
        "",
    ]
    for batch in plan["batches"]:
        dates = ", ".join(batch["operations"]) or "sem operacoes"
        lines.append(f"- Lote {batch['number']}: {dates}")
    blocked = [
        operation
        for operation in plan["operations"]
        if operation["status"] == "blocked"
    ]
    if blocked:
        lines.extend(["", "## Bloqueios", ""])
        for operation in blocked:
            layers = ", ".join(operation["blocked_layers"])
            lines.append(f"- {operation['backup_date']}: {layers}")
    lines.extend(
        [
            "",
            "## Seguranca",
            "",
            "- O plano e somente leitura.",
            "- Nenhum arquivo foi baixado, movido ou removido.",
            "- Destinos existentes sem manifesto valido bloqueiam a data.",
            "- A camada latest nao faz parte da importacao historica.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_history_batch_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# Lote historico {result['batch_number']}",
        "",
        f"- Status: `{result['status']}`",
        f"- Execucao: `{result['execution_id']}`",
        f"- SHA-256 do plano: `{result.get('plan_sha256')}`",
        f"- Datas processadas: {len(result.get('operations', []))}",
        f"- Legados removidos: {result.get('legacy_files_removed', 0)}",
        "",
        "## Operacoes",
        "",
    ]
    operations = result.get("operations", [])
    if operations:
        for operation in operations:
            layer_names = ", ".join(operation.get("layers", {})) or "nenhuma"
            lines.append(
                f"- {operation.get('backup_date')}: "
                f"`{operation.get('status')}`; camadas: {layer_names}"
            )
    else:
        lines.append("- Nenhuma data foi concluida.")
    errors = result.get("errors", [])
    if errors:
        lines.extend(["", "## Erros", ""])
        lines.extend(
            f"- {item.get('message', 'Falha sem detalhe.')}"
            for item in errors
            if isinstance(item, dict)
        )
    lines.extend(
        [
            "",
            "## Seguranca",
            "",
            "- Nenhum arquivo legado foi removido por este lote.",
            "- A camada latest permaneceu fora da operacao.",
            "- A retencao permaneceu em modo somente leitura.",
            "- Mantenha DATA_MIGRATION_LOCKED=true.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_history_finalization_markdown(
    result: dict[str, Any],
) -> str:
    validation = result.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    cleanup = result.get("cleanup")
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    preservation = result.get("preservation_validation")
    preservation = preservation if isinstance(preservation, dict) else {}
    publication = result.get("audit_publication")
    publication = publication if isinstance(publication, dict) else {}
    lines = [
        "# Encerramento da migracao historica",
        "",
        f"- Status: `{result.get('status')}`",
        f"- Execucao: `{result.get('execution_id')}`",
        f"- SHA-256 do plano: `{result.get('plan_sha256')}`",
        f"- Validacao das camadas: `{validation.get('status', 'nao executada')}`",
        f"- Camadas comprovadas: {validation.get('target_layer_count', 0)}",
        f"- Legados planejados: {len(_mapping_list(cleanup.get('planned_files')))}",
        f"- Legados ja ausentes: {len(_mapping_list(cleanup.get('already_absent_files')))}",
        f"- Legados removidos: {len(_mapping_list(cleanup.get('removed_files')))}",
        f"- Legados com falha: {len(_mapping_list(cleanup.get('failed_files')))}",
        f"- Validacao da preservacao: `{preservation.get('status', 'nao executada')}`",
        f"- Arquivos preservados conferidos: {preservation.get('verified_files', 0)}",
        f"- Manifesto remoto: `{publication.get('status', 'nao publicado')}`",
        f"- Caminho do manifesto: `{publication.get('final_path', 'indisponivel')}`",
        "",
        "## Seguranca",
        "",
        "- A exclusao usou somente caminhos exatos presentes no plano aprovado.",
        "- Nenhuma exclusao recursiva ou por curinga foi utilizada.",
        "- A camada latest e os arquivos nao gerenciados ficaram fora da limpeza.",
        "- DATA_MIGRATION_LOCKED deve permanecer true ate a revisao manual.",
        "",
    ]
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        lines.extend(["## Erros", ""])
        lines.extend(
            f"- {item.get('message', 'Falha sem detalhe.')}"
            for item in errors
            if isinstance(item, dict)
        )
        lines.append("")
    next_actions = result.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        lines.extend(["## Proximas acoes manuais", ""])
        lines.extend(
            f"- {sanitize_log_text(action)}"
            for action in next_actions
            if isinstance(action, str)
        )
        lines.append("")
    return "\n".join(lines)


def _render_apply_check_markdown(result: dict[str, Any]) -> str:
    memory = result["resources"]["memory"]
    disk = result["resources"]["disk"]
    lines = [
        "# Validacao previa do apply",
        "",
        f"- SHA-256 aprovado: `{result['expected_plan_sha256']}`",
        f"- SHA-256 recalculado: `{result['current_plan_sha256']}`",
        f"- Inventario: `{result['inventory_validation']}`",
        f"- Confirmacao: `{result['confirmation']}`",
        f"- Memoria necessaria: {memory['required_bytes']}",
        f"- Memoria disponivel: {memory['available_bytes']}",
        f"- Disco necessario: {disk['required_bytes']}",
        f"- Disco disponivel: {disk['available_bytes']}",
        "",
        "Nenhuma mutacao remota foi executada nesta etapa.",
        "",
    ]
    return "\n".join(lines)


def _render_conversion_markdown(result: dict[str, Any]) -> str:
    download = result.get("legacy_download")
    restoration = result.get("restoration")
    layered = result.get("layered_backup")
    removal = result.get("legacy_latest_removal")
    cleanup = result.get("legacy_cleanup")
    download = download if isinstance(download, dict) else {}
    restoration = restoration if isinstance(restoration, dict) else {}
    layered = layered if isinstance(layered, dict) else {}
    removal = removal if isinstance(removal, dict) else {}
    cleanup = cleanup if isinstance(cleanup, dict) else {}
    layers = layered.get("layers")
    layers = layers if isinstance(layers, dict) else {}
    planned_files = _mapping_list(cleanup.get("planned_files"))
    removed_files = _mapping_list(cleanup.get("removed_files"))
    failed_files = _mapping_list(cleanup.get("failed_files"))
    preserved_files = _mapping_list(cleanup.get("preserved_legacy_files"))

    lines = [
        "# Conversao dos backups legados",
        "",
        f"- Status: `{result.get('status')}`",
        f"- Execucao: `{result.get('execution_id')}`",
        f"- SHA-256 do plano: `{result.get('plan_sha256')}`",
        f"- Download legado: `{download.get('status', 'nao executado')}`",
        f"- Pacote legado: `{download.get('remote_path', 'nao identificado')}`",
        f"- SHA-256 local legado: `{download.get('sha256', 'nao calculado')}`",
        f"- Restauracao: `{restoration.get('status', 'nao executada')}`",
        (
            "- Validacao SQLite: `"
            f"{restoration.get('sqlite_validation', 'nao executada')}`"
        ),
        f"- Backup latest: `{_layer_status(layers, 'latest')}`",
        f"- Backup daily: `{_layer_status(layers, 'daily')}`",
        f"- Backup deep: `{_layer_status(layers, 'deep')}`",
        f"- Limpeza legada: `{cleanup.get('status', 'nao executada')}`",
        f"- Legados planejados: {len(planned_files)}",
        f"- Legados removidos: {len(removed_files)}",
        f"- Legados com falha: {len(failed_files)}",
        f"- Legados ainda dentro da retencao: {len(preserved_files)}",
        (
            "- Remocao do latest legado autorizada: `"
            f"{str(bool(removal.get('authorized'))).lower()}`"
        ),
        (
            "- Remocao do latest legado executada: `"
            f"{str(bool(removal.get('executed'))).lower()}`"
        ),
        "",
        "## Trava do coletor",
        "",
        (
            "A migracao nao altera `DATA_MIGRATION_LOCKED`. A variavel deve "
            "permanecer `true` ate a revisao manual de todas as evidencias."
        ),
        "",
    ]
    if removed_files:
        lines.extend(["## Arquivos legados removidos", ""])
        lines.extend(_render_cleanup_file(item) for item in removed_files)
        lines.append("")
    if failed_files:
        lines.extend(["## Arquivos legados com falha", ""])
        lines.extend(_render_cleanup_file(item) for item in failed_files)
        lines.append("")
    if preserved_files:
        lines.extend(["## Arquivos legados preservados", ""])
        lines.extend(_render_cleanup_file(item) for item in preserved_files)
        lines.append("")
    errors = result.get("errors")
    if isinstance(errors, list) and errors:
        lines.extend(["## Erros", ""])
        for error in errors:
            if isinstance(error, dict):
                lines.append(
                    f"- {sanitize_log_text(error.get('message', 'Falha sem detalhe.'))}"
                )
        lines.append("")
    next_actions = result.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        lines.extend(["## Proximas acoes manuais", ""])
        lines.extend(
            f"- {sanitize_log_text(action)}"
            for action in next_actions
            if isinstance(action, str)
        )
        lines.append("")
    return "\n".join(lines)


def _mapping_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _render_cleanup_file(item: dict[str, Any]) -> str:
    path = sanitize_log_text(item.get("remote_path", "caminho ausente"))
    size = item.get("size_bytes", "tamanho ausente")
    backup_date = item.get("backup_date") or "sem data"
    classification = sanitize_log_text(
        item.get("classification", "classificacao ausente")
    )
    result = sanitize_log_text(item.get("result", "preservado"))
    return (
        f"- `{path}` | bytes: `{size}` | data: `{backup_date}` | "
        f"classificacao: `{classification}` | resultado: `{result}`"
    )


def _layer_status(layers: dict[str, Any], name: str) -> object:
    layer = layers.get(name)
    if isinstance(layer, dict):
        return layer.get("status", "nao executado")
    return "nao executado"
