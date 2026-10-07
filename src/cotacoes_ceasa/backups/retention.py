"""Planejamento e aplicacao segura da retencao remota por camada."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from cotacoes_ceasa.backups.manifest import record_retention_removal
from cotacoes_ceasa.backups.policy import (
    LAYER_REMOTE_DIRECTORIES,
    ensure_remote_cleanup_enabled,
    join_remote_root,
    layer_file_date,
    normalize_remote_root,
    validate_layer_remote_path,
    validate_remote_name,
)
from cotacoes_ceasa.execution import execution_now, sanitize_log_text


RETENTION_ENVIRONMENTS = {
    "daily": ("DATA_ONEDRIVE_DAILY_RETENTION_DAYS", 7),
    "deep": ("DATA_ONEDRIVE_DEEP_RETENTION_DAYS", 30),
    "audit": ("DATA_ONEDRIVE_AUDIT_RETENTION_DAYS", 365),
}


class BackupRetentionError(RuntimeError):
    """Indica configuracao ou inventario remoto invalido para retencao."""


@dataclass(frozen=True)
class RetentionPolicy:
    daily_days: int
    deep_days: int
    audit_days: int

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "RetentionPolicy":
        values = environment if environment is not None else os.environ
        return cls(
            daily_days=_positive_days(values, *RETENTION_ENVIRONMENTS["daily"]),
            deep_days=_positive_days(values, *RETENTION_ENVIRONMENTS["deep"]),
            audit_days=_positive_days(values, *RETENTION_ENVIRONMENTS["audit"]),
        )

    def days_for(self, layer: str) -> int | None:
        return {
            "latest": None,
            "daily": self.daily_days,
            "deep": self.deep_days,
            "audit": self.audit_days,
        }[layer]

    def as_dict(self) -> dict[str, int]:
        return {
            "daily_days": self.daily_days,
            "deep_days": self.deep_days,
            "audit_days": self.audit_days,
        }


@dataclass(frozen=True)
class RetentionRequest:
    remote: str
    remote_root: str
    policy: RetentionPolicy
    newly_published_layers: frozenset[str]
    apply_changes: bool = False
    manifest_path: Path | None = None
    reference_date: date | None = None


@dataclass(frozen=True)
class RemoteBackupFile:
    layer: str
    name: str
    managed_path: PurePosixPath
    full_path: PurePosixPath
    backup_date: date | None
    size_bytes: int
    mod_time: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "path": self.full_path.as_posix(),
            "backup_date": (
                self.backup_date.isoformat() if self.backup_date else None
            ),
            "size_bytes": self.size_bytes,
            "mod_time": self.mod_time,
        }


def apply_remote_retention(request: RetentionRequest) -> dict[str, Any]:
    """Planeja ou remove somente arquivos definitivos e vencidos."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    reference_date = request.reference_date or execution_now().date()
    unknown_layers = request.newly_published_layers.difference(
        LAYER_REMOTE_DIRECTORIES
    )
    if unknown_layers:
        raise BackupRetentionError(
            "Camadas publicadas desconhecidas: "
            + ", ".join(sorted(unknown_layers))
            + "."
        )
    if request.apply_changes:
        ensure_remote_cleanup_enabled()
        if request.manifest_path is None:
            raise BackupRetentionError(
                "A aplicacao da retencao exige um manifesto aberto."
            )
    if shutil.which("rclone") is None:
        raise BackupRetentionError("rclone nao encontrado no ambiente.")

    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "mode": "apply" if request.apply_changes else "dry-run",
        "timezone": "America/Sao_Paulo",
        "reference_date": reference_date.isoformat(),
        "generated_at": execution_now().isoformat(timespec="seconds"),
        "remote": remote,
        "remote_root": remote_root.as_posix(),
        "policy": request.policy.as_dict(),
        "newly_published_layers": sorted(request.newly_published_layers),
        "layers": {},
        "planned_removals": [],
        "removed_files": [],
        "errors": [],
    }

    for layer in ("latest", "daily", "deep", "audit"):
        layer_result = _plan_layer(
            remote,
            remote_root,
            layer,
            request.policy.days_for(layer),
            reference_date,
            request.newly_published_layers,
        )
        result["layers"][layer] = layer_result
        result["planned_removals"].extend(layer_result["planned_removals"])

    if request.apply_changes:
        _execute_plan(request, remote, result)

    result["status"] = "failed" if result["errors"] else "completed"
    result["finished_at"] = execution_now().isoformat(timespec="seconds")
    return result


def _plan_layer(
    remote: str,
    remote_root: PurePosixPath,
    layer: str,
    retention_days: int | None,
    reference_date: date,
    newly_published_layers: frozenset[str],
) -> dict[str, Any]:
    files, ignored = _list_layer_files(remote, remote_root, layer)
    eligible = layer in newly_published_layers or (
        layer == "audit" and "latest" in newly_published_layers
    )
    cutoff = (
        reference_date - timedelta(days=retention_days - 1)
        if retention_days is not None
        else None
    )
    expired = [
        item
        for item in files
        if cutoff is not None
        and item.backup_date is not None
        and item.backup_date < cutoff
    ]
    protected: list[RemoteBackupFile] = []
    if files and len(files) == len(expired):
        newest = max(
            files,
            key=lambda item: (item.backup_date or date.min, item.name),
        )
        expired.remove(newest)
        protected.append(newest)

    expired.sort(key=lambda item: (item.backup_date or date.min, item.name))
    expired_paths = {item.full_path for item in expired}
    retained = [item for item in files if item.full_path not in expired_paths]
    return {
        "directory": join_remote_root(
            remote_root,
            LAYER_REMOTE_DIRECTORIES[layer],
        ).as_posix(),
        "retention_days": retention_days,
        "cutoff_date": cutoff.isoformat() if cutoff else None,
        "eligible_for_apply": eligible,
        "status": "planned" if eligible else "waiting_new_valid_backup",
        "valid_files": [item.as_dict() for item in files],
        "retained_files": [item.as_dict() for item in retained],
        "protected_last_files": [item.as_dict() for item in protected],
        "planned_removals": [item.as_dict() for item in expired],
        "ignored_files": ignored,
    }


def _list_layer_files(
    remote: str,
    remote_root: PurePosixPath,
    layer: str,
) -> tuple[list[RemoteBackupFile], list[dict[str, Any]]]:
    directory = LAYER_REMOTE_DIRECTORIES[layer]
    full_directory = join_remote_root(remote_root, directory)
    entries = _rclone_list(remote, full_directory)
    valid: list[RemoteBackupFile] = []
    ignored: list[dict[str, Any]] = []
    for entry in entries:
        name = entry.get("Name")
        size = entry.get("Size")
        if not isinstance(name, str):
            ignored.append({"name": None, "reason": "missing_name"})
            continue
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            ignored.append({"name": name, "reason": "invalid_size"})
            continue
        managed_path = directory / name
        try:
            validate_layer_remote_path(layer, managed_path)
            backup_date = layer_file_date(layer, name)
        except ValueError as error:
            ignored.append(
                {
                    "name": sanitize_log_text(name),
                    "reason": sanitize_log_text(error),
                }
            )
            continue
        valid.append(
            RemoteBackupFile(
                layer=layer,
                name=name,
                managed_path=managed_path,
                full_path=join_remote_root(remote_root, managed_path),
                backup_date=backup_date,
                size_bytes=size,
                mod_time=(
                    str(entry["ModTime"])
                    if entry.get("ModTime") is not None
                    else None
                ),
            )
        )
    valid.sort(key=lambda item: (item.backup_date or date.max, item.name))
    return valid, ignored


def _execute_plan(
    request: RetentionRequest,
    remote: str,
    result: dict[str, Any],
) -> None:
    for layer in ("latest", "daily", "deep", "audit"):
        layer_result = result["layers"][layer]
        if not layer_result["eligible_for_apply"]:
            layer_result["status"] = "skipped_no_new_valid_backup"
            continue
        layer_result["status"] = "completed"
        for item in layer_result["planned_removals"]:
            try:
                managed_path = _managed_path_from_full(
                    request.remote_root,
                    item["path"],
                )
                validate_layer_remote_path(layer, managed_path)
                _run_rclone("deletefile", f"{remote}:{item['path']}")
                removed = {
                    **item,
                    "layer": layer,
                    "removed_at": execution_now().isoformat(
                        timespec="seconds"
                    ),
                }
                result["removed_files"].append(removed)
                backup_date = (
                    date.fromisoformat(item["backup_date"])
                    if item["backup_date"]
                    else None
                )
                record_retention_removal(
                    request.manifest_path,
                    item["path"],
                    item["size_bytes"],
                    backup_date,
                )
            except Exception as error:
                detail = sanitize_log_text(error)
                layer_result["status"] = "failed"
                result["errors"].append(
                    {
                        "layer": layer,
                        "path": item["path"],
                        "message": detail,
                    }
                )
                return


def _managed_path_from_full(
    configured_root: str,
    full_path: str,
) -> PurePosixPath:
    root = normalize_remote_root(configured_root)
    path = normalize_remote_root(full_path)
    try:
        return path.relative_to(root)
    except ValueError as error:
        raise BackupRetentionError(
            f"Caminho fora da raiz configurada: {full_path}."
        ) from error


def _rclone_list(
    remote: str,
    directory: PurePosixPath,
) -> list[dict[str, Any]]:
    try:
        completed = _run_rclone(
            "lsjson",
            f"{remote}:{directory.as_posix()}",
            "--files-only",
        )
    except RuntimeError as error:
        detail = str(error).lower()
        missing_markers = (
            "directory not found",
            "path not found",
            "item not found",
            "itemnotfound",
            "couldn't find directory",
            "object not found",
        )
        if any(marker in detail for marker in missing_markers):
            return []
        raise
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise BackupRetentionError(
            f"Listagem JSON invalida para {directory.as_posix()}: {error}"
        ) from error
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise BackupRetentionError(
            f"Listagem remota invalida para {directory.as_posix()}."
        )
    return payload


def _positive_days(
    environment: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw_value = environment.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise BackupRetentionError(
            f"{name} deve ser um numero inteiro maior ou igual a 1."
        ) from error
    if value < 1:
        raise BackupRetentionError(
            f"{name} deve ser maior ou igual a 1."
        )
    return value


def _run_rclone(*arguments: str) -> subprocess.CompletedProcess[str]:
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
        raise RuntimeError(
            f"rclone {arguments[0]} encerrou com codigo {error.returncode}{suffix}."
        ) from error
