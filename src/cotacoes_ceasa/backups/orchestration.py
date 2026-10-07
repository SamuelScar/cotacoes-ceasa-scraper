"""Orquestracao sequencial das camadas de backup do OneDrive."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from cotacoes_ceasa.backups.manifest import (
    ManifestRequest,
    create_backup_manifest,
    finalize_backup_manifest,
    record_backup_artifact,
    record_layer_outcome,
    record_onedrive_quota,
    record_upload_result,
)
from cotacoes_ceasa.backups.packaging import (
    BackupLayer,
    BackupLayerSpec,
    create_backup,
)
from cotacoes_ceasa.backups.policy import (
    LAYER_REMOTE_DIRECTORIES,
    join_remote_root,
    layer_file_date,
    normalize_remote_root,
    validate_remote_name,
)
from cotacoes_ceasa.backups.remote import (
    RemotePublicationError,
    RemotePublicationRequest,
    publish_backup_atomically,
)
from cotacoes_ceasa.backups.retention import (
    RetentionPolicy,
    RetentionRequest,
    apply_remote_retention,
)
from cotacoes_ceasa.execution import (
    execution_now,
    sanitize_log_text,
    write_json_atomic,
)


@dataclass(frozen=True)
class LayeredBackupRequest:
    execution_id: str
    execution_directory: Path
    source_directory: Path
    work_directory: Path
    result_path: Path
    remote: str = "onedrive"
    remote_root: str = "cotacoes-ceasa"
    backup_date: date | None = None
    pre_audit_action: Callable[[Path], dict[str, Any]] | None = None


def run_layered_backup(request: LayeredBackupRequest) -> dict[str, Any]:
    """Executa empacotamento, publicacao, retencao e auditoria em sequencia."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    source = request.source_directory.resolve(strict=True)
    execution_directory = request.execution_directory.resolve(strict=True)
    work_directory = request.work_directory.resolve(strict=False)
    work_directory.mkdir(parents=True, exist_ok=True)
    backup_date = request.backup_date or execution_now().date()
    manifest_path = create_backup_manifest(
        ManifestRequest(
            execution_id=request.execution_id,
            execution_directory=execution_directory,
            source_directory=source,
            backup_date=backup_date,
        )
    )

    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "execution_id": request.execution_id,
        "timezone": "America/Sao_Paulo",
        "backup_date": backup_date.isoformat(),
        "started_at": execution_now().isoformat(timespec="seconds"),
        "manifest_path": manifest_path.as_posix(),
        "remote": remote,
        "remote_root": remote_root.as_posix(),
        "layers": {},
        "quota": {"before": None, "after": None},
        "retention": None,
        "pre_audit_action": None,
        "audit_publication": None,
        "errors": [],
    }
    write_json_atomic(request.result_path, result)

    remote_setup_error: str | None = None
    try:
        _ensure_remote_directories(remote, remote_root)
    except Exception as error:
        remote_setup_error = sanitize_log_text(error)
        result["errors"].append(
            {"stage": "remote_setup", "message": remote_setup_error}
        )
    quota_before = _collect_quota(remote)
    result["quota"]["before"] = quota_before
    record_onedrive_quota(manifest_path, "before", quota_before)

    newly_published: set[str] = set()
    for layer in BackupLayer:
        layer_name = layer.value
        specification = BackupLayerSpec.for_layer(layer, backup_date)
        destination = (
            specification.remote_directory / specification.file_name
        ).as_posix()
        if remote_setup_error is not None:
            record_layer_outcome(
                manifest_path,
                layer_name,
                "failed",
                remote_setup_error,
            )
            result["layers"][layer_name] = {
                "status": "failed",
                "file_name": specification.file_name,
                "destination_path": destination,
                "error": remote_setup_error,
            }
            write_json_atomic(request.result_path, result)
            continue
        if layer is not BackupLayer.LATEST and _remote_layer_backup_exists(
            remote,
            remote_root,
            specification,
            backup_date,
        ):
            detail = "Backup valido da data ja existe no OneDrive."
            record_layer_outcome(manifest_path, layer_name, "skipped", detail)
            result["layers"][layer_name] = {
                "status": "skipped_existing",
                "file_name": specification.file_name,
                "destination_path": destination,
                "detail": detail,
            }
            write_json_atomic(request.result_path, result)
            continue

        layer_result = _create_and_publish_layer(
            specification,
            source,
            work_directory,
            manifest_path,
            request.execution_id,
            remote,
            remote_root.as_posix(),
            destination,
        )
        result["layers"][layer_name] = layer_result
        if layer_result.get("status") == "completed":
            newly_published.add(layer_name)
        elif layer_result.get("status") == "failed":
            result["errors"].append(
                {
                    "stage": layer_name,
                    "message": layer_result.get("error") or "Falha sem detalhe.",
                }
            )
        write_json_atomic(request.result_path, result)

    try:
        retention = apply_remote_retention(
            RetentionRequest(
                remote=remote,
                remote_root=remote_root.as_posix(),
                policy=RetentionPolicy.from_environment(),
                newly_published_layers=frozenset(newly_published),
                apply_changes=True,
                manifest_path=manifest_path,
                reference_date=backup_date,
            )
        )
    except Exception as error:
        retention = {
            "status": "failed",
            "mode": "apply",
            "removed_files": [],
            "errors": [{"message": sanitize_log_text(error)}],
        }
    result["retention"] = retention
    if retention.get("status") != "completed":
        result["errors"].append(
            {
                "stage": "retention",
                "message": "A retencao remota nao foi concluida.",
            }
        )

    if request.pre_audit_action is not None:
        if result["errors"]:
            pre_audit_result = {
                "status": "skipped",
                "reason": "layered_backup_not_valid",
            }
        else:
            try:
                pre_audit_result = request.pre_audit_action(manifest_path)
            except Exception as error:
                pre_audit_result = {
                    "status": "failed",
                    "errors": [{"message": sanitize_log_text(error)}],
                }
            if not isinstance(pre_audit_result, dict):
                pre_audit_result = {
                    "status": "failed",
                    "errors": [
                        {
                            "message": (
                                "A operacao anterior ao manifesto retornou "
                                "um resultado invalido."
                            )
                        }
                    ],
                }
            if pre_audit_result.get("status") != "completed":
                result["errors"].append(
                    {
                        "stage": "pre_audit_action",
                        "message": (
                            "A operacao anterior ao manifesto de auditoria "
                            "nao foi concluida."
                        ),
                    }
                )
        result["pre_audit_action"] = pre_audit_result
        write_json_atomic(request.result_path, result)

    quota_after = _collect_quota(remote)
    result["quota"]["after"] = quota_after
    record_onedrive_quota(manifest_path, "after", quota_after)

    manifest_status = "failed" if result["errors"] else "completed"
    manifest_error = (
        "; ".join(str(item["message"]) for item in result["errors"])
        if result["errors"]
        else None
    )
    finalize_backup_manifest(manifest_path, manifest_status, manifest_error)

    audit_destination = f"backups/audit/{manifest_path.name}"
    try:
        audit_publication = publish_backup_atomically(
            RemotePublicationRequest(
                layer="audit",
                local_file=manifest_path,
                destination_path=audit_destination,
                execution_id=request.execution_id,
                remote=remote,
                remote_root=remote_root.as_posix(),
            )
        ).as_dict()
    except RemotePublicationError as error:
        audit_publication = error.result.as_dict()
    except Exception as error:
        audit_publication = {
            "status": "failed",
            "layer": "audit",
            "final_path": audit_destination,
            "error": sanitize_log_text(error),
        }
    result["audit_publication"] = audit_publication
    if audit_publication.get("status") not in {"completed", "skipped"}:
        result["errors"].append(
            {
                "stage": "audit",
                "message": audit_publication.get("error")
                or "Falha ao publicar o manifesto de auditoria.",
            }
        )

    latest_status = result["layers"].get("latest", {}).get("status")
    result["status"] = (
        "completed"
        if latest_status == "completed" and not result["errors"]
        else "failed"
    )
    result["finished_at"] = execution_now().isoformat(timespec="seconds")
    write_json_atomic(request.result_path, result)
    return result


def _create_and_publish_layer(
    specification: BackupLayerSpec,
    source: Path,
    work_directory: Path,
    manifest_path: Path,
    execution_id: str,
    remote: str,
    remote_root: str,
    destination: str,
) -> dict[str, Any]:
    layer_name = specification.layer.value
    artifact_path = work_directory / specification.file_name
    artifact_recorded = False
    try:
        artifact = create_backup(specification, source, work_directory)
        record_backup_artifact(manifest_path, artifact)
        artifact_recorded = True
        try:
            publication = publish_backup_atomically(
                RemotePublicationRequest(
                    layer=layer_name,
                    local_file=artifact_path,
                    destination_path=destination,
                    execution_id=execution_id,
                    expected_sha256=artifact.sha256,
                    remote=remote,
                    remote_root=remote_root,
                )
            )
            publication_result = publication.as_dict()
        except RemotePublicationError as error:
            publication_result = error.result.as_dict()

        record_upload_result(
            manifest_path,
            layer_name,
            str(publication_result["status"]),
            temporary_path=_optional_string(
                publication_result.get("temporary_path")
            ),
            final_path=_optional_string(publication_result.get("final_path")),
            duration_seconds=_optional_float(
                publication_result.get("duration_seconds")
            ),
            error=_optional_string(publication_result.get("error")),
        )
        return {
            "status": publication_result["status"],
            "package": artifact.as_dict(),
            "publication": publication_result,
        }
    except Exception as error:
        detail = sanitize_log_text(error)
        try:
            if artifact_recorded:
                record_upload_result(
                    manifest_path,
                    layer_name,
                    "failed",
                    final_path=destination,
                    error=detail,
                )
            else:
                record_layer_outcome(
                    manifest_path,
                    layer_name,
                    "failed",
                    detail,
                )
        except Exception as manifest_error:
            detail = sanitize_log_text(f"{detail}; manifesto: {manifest_error}")
        return {
            "status": "failed",
            "file_name": specification.file_name,
            "destination_path": destination,
            "error": detail,
        }
    finally:
        artifact_path.unlink(missing_ok=True)


def _ensure_remote_directories(
    remote: str,
    remote_root: PurePosixPath,
) -> None:
    for directory in LAYER_REMOTE_DIRECTORIES.values():
        full_path = join_remote_root(remote_root, directory)
        _run_rclone("mkdir", f"{remote}:{full_path.as_posix()}")


def _remote_layer_backup_exists(
    remote: str,
    remote_root: PurePosixPath,
    specification: BackupLayerSpec,
    backup_date: date,
) -> bool:
    full_directory = join_remote_root(
        remote_root,
        specification.remote_directory,
    )
    completed = _run_rclone(
        "lsf",
        f"{remote}:{full_directory.as_posix()}",
        "--files-only",
    )
    for name in completed.stdout.splitlines():
        try:
            if layer_file_date(specification.layer.value, name) == backup_date:
                return True
        except ValueError:
            continue
    return False


def _collect_quota(remote: str) -> dict[str, Any]:
    try:
        completed = _run_rclone("about", f"{remote}:", "--json")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("A quota nao retornou um objeto JSON.")
        return {"status": "available", **payload}
    except Exception as error:
        return {
            "status": "unavailable",
            "error": sanitize_log_text(error),
        }


def _run_rclone(*arguments: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("rclone") is None:
        raise RuntimeError("rclone nao encontrado no ambiente.")
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


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Duracao remota invalida.")
    return float(value)
