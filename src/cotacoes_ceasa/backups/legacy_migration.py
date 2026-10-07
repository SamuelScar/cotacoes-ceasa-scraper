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
from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.backups.policy import (
    normalize_remote_root,
    validate_remote_name,
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
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
APPLY_CONFIRMATION = "EXCLUIR_LEGADOS_VENCIDOS"
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
    try:
        timestamp = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise LegacyMigrationPlanError(
            f"Timestamp legado invalido: {sanitize_log_text(value)}."
        ) from error
    return timestamp.astimezone(SAO_PAULO).date()


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
    allowed_operations = {"copyto", "deletefile"}
    if not arguments or arguments[0] not in allowed_operations:
        raise LegacyMigrationPlanError(
            "A migracao permite somente copyto e deletefile controlados."
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
    evidence_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        manifest_path,
        evidence_directory / "migration-backup-manifest.json",
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
