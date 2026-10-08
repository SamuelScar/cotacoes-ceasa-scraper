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
    ManifestSourceBackup,
    create_backup_manifest,
    finalize_backup_manifest,
    record_backup_artifact,
    record_layer_outcome,
    record_onedrive_quota,
    record_reused_backup_artifact,
    record_upload_result,
)
from cotacoes_ceasa.backups.packaging import (
    BackupLayer,
    BackupLayerSpec,
    COLLECTION_BACKUP_LAYERS,
    LAYERED_BACKUP_LAYERS,
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
    layers: frozenset[BackupLayer] | None = None
    apply_retention: bool = True
    retention_reference_date: date | None = None
    pre_audit_action: Callable[[Path], dict[str, Any]] | None = None
    manifest_document_type: str = "layered_backup"
    manifest_source_backup: ManifestSourceBackup | None = None
    consolidated_staging_dates: frozenset[date] = frozenset()
    validated_existing_layers: frozenset[BackupLayer] = frozenset()


@dataclass(frozen=True)
class CollectionBackupRequest:
    execution_id: str
    execution_directory: Path
    source_directory: Path
    work_directory: Path
    result_path: Path
    remote: str = "onedrive"
    remote_root: str = "cotacoes-ceasa"


def run_layered_backup(request: LayeredBackupRequest) -> dict[str, Any]:
    """Executa empacotamento, publicacao, retencao e auditoria em sequencia."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    source = request.source_directory.resolve(strict=True)
    execution_directory = request.execution_directory.resolve(strict=True)
    work_directory = request.work_directory.resolve(strict=False)
    work_directory.mkdir(parents=True, exist_ok=True)
    backup_date = request.backup_date or execution_now().date()
    requested_layers = _normalize_requested_layers(request.layers)
    if not request.validated_existing_layers.issubset(requested_layers):
        raise ValueError(
            "Camadas existentes validadas devem pertencer a execucao."
        )
    manifest_path = create_backup_manifest(
        ManifestRequest(
            execution_id=request.execution_id,
            execution_directory=execution_directory,
            source_directory=source,
            backup_date=backup_date,
            required_layers=requested_layers,
            document_type=request.manifest_document_type,
            source_backup=request.manifest_source_backup,
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
        "requested_layers": sorted(layer.value for layer in requested_layers),
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
    execution_layers = (
        (BackupLayer.DAILY, BackupLayer.DEEP)
        if request.manifest_document_type == "daily_consolidation"
        else (
            BackupLayer.LATEST,
            BackupLayer.DAILY,
            BackupLayer.DEEP,
        )
    )
    for layer in execution_layers:
        layer_name = layer.value
        specification = BackupLayerSpec.for_layer(layer, backup_date)
        destination = (
            specification.remote_directory / specification.file_name
        ).as_posix()
        if layer not in requested_layers:
            detail = "Camada nao solicitada para esta execucao."
            record_layer_outcome(
                manifest_path,
                layer_name,
                "skipped",
                detail,
            )
            result["layers"][layer_name] = {
                "status": "skipped_not_requested",
                "file_name": specification.file_name,
                "destination_path": destination,
                "detail": detail,
            }
            write_json_atomic(request.result_path, result)
            continue
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
        else:
            result["errors"].append(
                {
                    "stage": layer_name,
                    "message": layer_result.get("error")
                    or "A publicacao da camada nao foi concluida.",
                }
            )
        write_json_atomic(request.result_path, result)

    successful_layer_statuses = {"completed", "skipped_existing"}
    requested_layers_completed = all(
        result["layers"].get(layer.value, {}).get("status")
        in successful_layer_statuses
        for layer in requested_layers
    )
    if request.apply_retention and not requested_layers_completed:
        retention = {
            "status": "skipped_invalid_backup",
            "mode": "apply",
            "removed_files": [],
            "errors": [],
            "reason": (
                "A retencao exige todas as camadas solicitadas validas."
            ),
        }
    else:
        try:
            retention = apply_remote_retention(
                RetentionRequest(
                    remote=remote,
                    remote_root=remote_root.as_posix(),
                    policy=RetentionPolicy.from_environment(),
                    newly_published_layers=frozenset(newly_published),
                    validated_existing_layers=frozenset(
                        layer.value
                        for layer in request.validated_existing_layers
                    ),
                    apply_changes=request.apply_retention,
                    manifest_path=(
                        manifest_path if request.apply_retention else None
                    ),
                    reference_date=(
                        request.retention_reference_date or backup_date
                    ),
                    consolidated_staging_dates=(
                        request.consolidated_staging_dates
                    ),
                )
            )
        except Exception as error:
            retention = {
                "status": "failed",
                "mode": (
                    "apply" if request.apply_retention else "dry-run"
                ),
                "removed_files": [],
                "errors": [{"message": sanitize_log_text(error)}],
            }
    result["retention"] = retention
    if retention.get("status") not in {
        "completed",
        "skipped_invalid_backup",
    }:
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

    result["status"] = (
        "completed"
        if requested_layers_completed and not result["errors"]
        else "failed"
    )
    result["finished_at"] = execution_now().isoformat(timespec="seconds")
    write_json_atomic(request.result_path, result)
    return result


def run_collection_backup(
    request: CollectionBackupRequest,
) -> dict[str, Any]:
    """Publica um unico ZIP em staging e latest, sem executar retencao."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    source = request.source_directory.resolve(strict=True)
    execution_directory = request.execution_directory.resolve(strict=True)
    work_directory = request.work_directory.resolve(strict=False)
    work_directory.mkdir(parents=True, exist_ok=True)
    started_at = execution_now()
    backup_date = started_at.date()
    staging_spec = BackupLayerSpec.for_layer(
        BackupLayer.STAGING,
        backup_date,
        backup_datetime=started_at,
    )
    latest_spec = BackupLayerSpec.for_layer(
        BackupLayer.LATEST,
        backup_date,
        backup_datetime=started_at,
    )
    manifest_path = create_backup_manifest(
        ManifestRequest(
            execution_id=request.execution_id,
            execution_directory=execution_directory,
            source_directory=source,
            backup_date=backup_date,
            required_layers=COLLECTION_BACKUP_LAYERS,
            document_type="collection_backup",
            started_at=started_at,
        )
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "mode": "collection_backup",
        "execution_id": request.execution_id,
        "timezone": "America/Sao_Paulo",
        "backup_date": backup_date.isoformat(),
        "started_at": started_at.isoformat(timespec="seconds"),
        "manifest_path": manifest_path.as_posix(),
        "remote": remote,
        "remote_root": remote_root.as_posix(),
        "layers": {},
        "quota": {"before": None, "after": None},
        "retention": {
            "status": "not_executed",
            "reason": "A retencao completa pertence a consolidacao diaria.",
        },
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

    artifact_path = work_directory / staging_spec.file_name
    artifact = None
    if remote_setup_error is None:
        try:
            created_artifact = create_backup(
                staging_spec,
                source,
                work_directory,
            )
            record_backup_artifact(manifest_path, created_artifact)
            record_reused_backup_artifact(
                manifest_path,
                BackupLayer.STAGING.value,
                BackupLayer.LATEST.value,
            )
            artifact = created_artifact
        except Exception as error:
            detail = sanitize_log_text(error)
            artifact_path.unlink(missing_ok=True)
            result["errors"].append(
                {"stage": "package", "message": detail}
            )
            for layer in COLLECTION_BACKUP_LAYERS:
                try:
                    record_layer_outcome(
                        manifest_path,
                        layer.value,
                        "failed",
                        detail,
                    )
                except Exception:
                    pass
    else:
        for layer in COLLECTION_BACKUP_LAYERS:
            record_layer_outcome(
                manifest_path,
                layer.value,
                "failed",
                remote_setup_error,
            )

    if artifact is not None:
        staging_result = _publish_collection_layer(
            specification=staging_spec,
            local_file=artifact_path,
            expected_sha256=artifact.sha256,
            package=artifact.as_dict(),
            manifest_path=manifest_path,
            execution_id=request.execution_id,
            remote=remote,
            remote_root=remote_root.as_posix(),
        )
        result["layers"]["staging"] = staging_result
        if staging_result.get("status") == "completed":
            latest_result = _publish_collection_layer(
                specification=latest_spec,
                local_file=artifact_path,
                expected_sha256=artifact.sha256,
                package={
                    **artifact.as_dict(),
                    "layer": "latest",
                    "file_name": latest_spec.file_name,
                    "remote_directory": (
                        latest_spec.remote_directory.as_posix()
                    ),
                },
                manifest_path=manifest_path,
                execution_id=request.execution_id,
                remote=remote,
                remote_root=remote_root.as_posix(),
            )
        else:
            detail = "Latest nao publicado porque staging falhou."
            record_upload_result(
                manifest_path,
                "latest",
                "failed",
                final_path=(
                    latest_spec.remote_directory / latest_spec.file_name
                ).as_posix(),
                error=detail,
            )
            latest_result = {
                "status": "failed",
                "error": detail,
            }
        result["layers"]["latest"] = latest_result
        for layer_name, layer_result in result["layers"].items():
            if layer_result.get("status") != "completed":
                result["errors"].append(
                    {
                        "stage": layer_name,
                        "message": layer_result.get("error")
                        or "Publicacao nao concluida.",
                    }
                )
        artifact_path.unlink(missing_ok=True)
    else:
        for layer in COLLECTION_BACKUP_LAYERS:
            result["layers"][layer.value] = {
                "status": "failed",
                "error": "O pacote da coleta nao foi criado.",
            }

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
    audit_publication = _publish_audit_manifest(
        manifest_path=manifest_path,
        execution_id=request.execution_id,
        remote=remote,
        remote_root=remote_root.as_posix(),
    )
    result["audit_publication"] = audit_publication
    if audit_publication.get("status") not in {"completed", "skipped"}:
        result["errors"].append(
            {
                "stage": "audit",
                "message": audit_publication.get("error")
                or "Falha ao publicar o manifesto da coleta.",
            }
        )
    result["status"] = "failed" if result["errors"] else "completed"
    result["finished_at"] = execution_now().isoformat(timespec="seconds")
    write_json_atomic(request.result_path, result)
    return result


def _publish_collection_layer(
    *,
    specification: BackupLayerSpec,
    local_file: Path,
    expected_sha256: str,
    package: dict[str, Any],
    manifest_path: Path,
    execution_id: str,
    remote: str,
    remote_root: str,
) -> dict[str, Any]:
    layer_name = specification.layer.value
    destination = (
        specification.remote_directory / specification.file_name
    ).as_posix()
    try:
        publication = publish_backup_atomically(
            RemotePublicationRequest(
                layer=layer_name,
                local_file=local_file,
                destination_path=destination,
                execution_id=execution_id,
                expected_sha256=expected_sha256,
                remote=remote,
                remote_root=remote_root,
                allow_local_name_mismatch=(
                    specification.layer is BackupLayer.LATEST
                ),
            )
        ).as_dict()
    except RemotePublicationError as error:
        publication = error.result.as_dict()
    except Exception as error:
        publication = {
            "status": "failed",
            "layer": layer_name,
            "final_path": destination,
            "error": sanitize_log_text(error),
        }
    try:
        record_upload_result(
            manifest_path,
            layer_name,
            str(publication["status"]),
            temporary_path=_optional_string(
                publication.get("temporary_path")
            ),
            final_path=_optional_string(publication.get("final_path")),
            duration_seconds=_optional_float(
                publication.get("duration_seconds")
            ),
            error=_optional_string(publication.get("error")),
        )
    except Exception as error:
        return {
            "status": "failed",
            "package": package,
            "publication": publication,
            "error": sanitize_log_text(
                f"Falha ao registrar a publicacao no manifesto: {error}"
            ),
        }
    return {
        "status": publication["status"],
        "package": package,
        "publication": publication,
        "error": publication.get("error"),
    }


def _publish_audit_manifest(
    *,
    manifest_path: Path,
    execution_id: str,
    remote: str,
    remote_root: str,
) -> dict[str, Any]:
    destination = f"backups/audit/{manifest_path.name}"
    try:
        return publish_backup_atomically(
            RemotePublicationRequest(
                layer="audit",
                local_file=manifest_path,
                destination_path=destination,
                execution_id=execution_id,
                remote=remote,
                remote_root=remote_root,
            )
        ).as_dict()
    except RemotePublicationError as error:
        return error.result.as_dict()
    except Exception as error:
        return {
            "status": "failed",
            "layer": "audit",
            "final_path": destination,
            "error": sanitize_log_text(error),
        }


def _normalize_requested_layers(
    value: frozenset[BackupLayer] | None,
) -> frozenset[BackupLayer]:
    requested = LAYERED_BACKUP_LAYERS if value is None else value
    if not requested:
        raise ValueError("A execucao exige ao menos uma camada de backup.")
    if any(not isinstance(layer, BackupLayer) for layer in requested):
        raise ValueError("A execucao recebeu uma camada de backup invalida.")
    return requested


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
        if name != specification.file_name:
            continue
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
