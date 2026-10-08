"""Consolidacao diaria dos backups intradiarios validados."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from cotacoes_ceasa.backups.manifest import ManifestSourceBackup
from cotacoes_ceasa.backups.orchestration import (
    LayeredBackupRequest,
    run_layered_backup,
)
from cotacoes_ceasa.backups.packaging import BackupLayer, BackupLayerSpec
from cotacoes_ceasa.backups.policy import (
    LAYER_REMOTE_DIRECTORIES,
    join_remote_root,
    layer_file_date,
    normalize_remote_root,
    validate_layer_remote_path,
    validate_remote_name,
)
from cotacoes_ceasa.backups.restore import RestoreRequest, restore_backup
from cotacoes_ceasa.execution import (
    execution_now,
    sanitize_log_text,
    write_json_atomic,
)


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
EXECUTION_ID_PATTERN = re.compile(
    r"^[0-9A-Za-z][0-9A-Za-z_-]{0,127}$"
)


class DailyConsolidationError(RuntimeError):
    """Indica que a consolidacao nao pode continuar com seguranca."""


@dataclass(frozen=True)
class DailyConsolidationRequest:
    execution_id: str
    execution_directory: Path
    work_directory: Path
    result_path: Path
    remote: str = "onedrive"
    remote_root: str = "cotacoes-ceasa"
    consolidation_date: date | None = None


@dataclass(frozen=True)
class StagingCandidate:
    name: str
    managed_path: PurePosixPath
    full_path: PurePosixPath
    size_bytes: int
    backup_date: date
    manifest_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "managed_path": self.managed_path.as_posix(),
            "remote_path": self.full_path.as_posix(),
            "size_bytes": self.size_bytes,
            "backup_date": self.backup_date.isoformat(),
            "manifest_name": self.manifest_name,
        }


def run_daily_consolidation(
    request: DailyConsolidationRequest,
) -> dict[str, Any]:
    """Seleciona, restaura e consolida o ultimo staging valido da data."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    execution_directory = request.execution_directory.resolve(strict=True)
    work_parent = request.work_directory.resolve(strict=False)
    work_parent.mkdir(parents=True, exist_ok=True)
    consolidation_date = request.consolidation_date or execution_now().date()
    started_at = execution_now()
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "mode": "daily_consolidation",
        "execution_id": request.execution_id,
        "timezone": "America/Sao_Paulo",
        "consolidation_date": consolidation_date.isoformat(),
        "started_at": started_at.isoformat(timespec="seconds"),
        "candidate_inventory": [],
        "rejected_candidates": [],
        "selected_candidate": None,
        "target_validation": None,
        "consolidated_staging_dates": [],
        "restoration": None,
        "layered_backup": None,
        "errors": [],
    }
    write_json_atomic(request.result_path, result)

    operation_root = Path(
        tempfile.mkdtemp(
            prefix="daily-consolidation-",
            dir=work_parent,
        )
    )
    try:
        candidates = _list_staging_candidates(
            remote,
            remote_root,
            consolidation_date,
        )
        result["candidate_inventory"] = [
            item.as_dict() for item in candidates
        ]
        write_json_atomic(request.result_path, result)
        selected, manifest_path, manifest = _select_candidate(
            remote=remote,
            remote_root=remote_root,
            candidates=candidates,
            download_directory=operation_root / "manifests",
            rejected=result["rejected_candidates"],
        )
        result["selected_candidate"] = {
            **selected.as_dict(),
            "manifest_remote_path": (
                LAYER_REMOTE_DIRECTORIES["audit"] / selected.manifest_name
            ).as_posix(),
            "execution_id": manifest["execution_id"],
            "sha256": _manifest_layer_sha256(manifest, "staging"),
        }

        target_validation = _validate_existing_targets(
            remote=remote,
            remote_root=remote_root,
            backup_date=consolidation_date,
            audit_directory=operation_root / "target-proofs",
        )
        result["target_validation"] = target_validation
        proven_dates = _discover_consolidated_staging_dates(
            remote=remote,
            remote_root=remote_root,
            reference_date=consolidation_date,
            audit_directory=operation_root / "consolidation-proofs",
        )
        consolidated_dates = frozenset(
            {*proven_dates, consolidation_date}
        )
        result["consolidated_staging_dates"] = sorted(
            item.isoformat() for item in consolidated_dates
        )
        write_json_atomic(request.result_path, result)

        archive_path = operation_root / "downloads" / selected.name
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        _run_rclone(
            "copyto",
            f"{remote}:{selected.full_path.as_posix()}",
            archive_path.as_posix(),
        )
        if archive_path.stat().st_size != selected.size_bytes:
            raise DailyConsolidationError(
                "O tamanho baixado do staging diverge do inventario remoto."
            )

        restored_project = operation_root / "restored"
        restored_project.mkdir()
        restoration = restore_backup(
            RestoreRequest(
                archive_path=archive_path,
                project_root=restored_project,
                manifest_path=manifest_path,
                expected_sha256=result["selected_candidate"]["sha256"],
            )
        ).as_dict()
        result["restoration"] = restoration
        write_json_atomic(request.result_path, result)

        source_reference = ManifestSourceBackup(
            execution_id=str(manifest["execution_id"]),
            manifest_remote_path=(
                LAYER_REMOTE_DIRECTORIES["audit"] / selected.manifest_name
            ).as_posix(),
            staging_remote_path=selected.managed_path.as_posix(),
            sha256=str(result["selected_candidate"]["sha256"]),
            backup_date=consolidation_date,
        )
        layered_result_path = (
            request.result_path.parent
            / f"{request.result_path.stem}-layers.json"
        )
        layered_backup = run_layered_backup(
            LayeredBackupRequest(
                execution_id=request.execution_id,
                execution_directory=execution_directory,
                source_directory=restored_project / "data",
                work_directory=operation_root / "packages",
                result_path=layered_result_path,
                remote=remote,
                remote_root=remote_root.as_posix(),
                backup_date=consolidation_date,
                layers=frozenset(
                    {BackupLayer.DAILY, BackupLayer.DEEP}
                ),
                apply_retention=True,
                retention_reference_date=consolidation_date,
                manifest_document_type="daily_consolidation",
                manifest_source_backup=source_reference,
                consolidated_staging_dates=consolidated_dates,
                validated_existing_layers=frozenset(
                    BackupLayer(layer_name)
                    for layer_name, validation in target_validation[
                        "layers"
                    ].items()
                    if validation.get("status")
                    == "validated_existing"
                ),
            )
        )
        result["layered_backup"] = layered_backup
        if layered_backup.get("status") != "completed":
            raise DailyConsolidationError(
                "As camadas da consolidacao diaria nao foram concluidas."
            )
        result["status"] = "completed"
    except Exception as error:
        detail = sanitize_log_text(error)
        result["status"] = "failed"
        result["errors"].append({"message": detail})
    finally:
        result["finished_at"] = execution_now().isoformat(
            timespec="seconds"
        )
        write_json_atomic(request.result_path, result)
        shutil.rmtree(operation_root)
    return result


def _list_staging_candidates(
    remote: str,
    remote_root: PurePosixPath,
    backup_date: date,
) -> list[StagingCandidate]:
    directory = LAYER_REMOTE_DIRECTORIES["staging"]
    full_directory = join_remote_root(remote_root, directory)
    entries = _rclone_list(remote, full_directory)
    candidates: list[StagingCandidate] = []
    for entry in entries:
        name = entry.get("Name")
        size = entry.get("Size")
        if (
            not isinstance(name, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
        ):
            continue
        try:
            managed_path = validate_layer_remote_path(
                "staging",
                directory / name,
            )
            file_date = layer_file_date("staging", name)
        except ValueError:
            continue
        if file_date != backup_date:
            continue
        timestamp = name.removeprefix("ceasa-data-").removesuffix(".zip")
        candidates.append(
            StagingCandidate(
                name=name,
                managed_path=managed_path,
                full_path=join_remote_root(remote_root, managed_path),
                size_bytes=size,
                backup_date=backup_date,
                manifest_name=f"backup-{timestamp}.json",
            )
        )
    candidates.sort(key=lambda item: item.name, reverse=True)
    if not candidates:
        raise DailyConsolidationError(
            f"Nenhum staging valido encontrado para {backup_date}."
        )
    return candidates


def _select_candidate(
    *,
    remote: str,
    remote_root: PurePosixPath,
    candidates: list[StagingCandidate],
    download_directory: Path,
    rejected: list[dict[str, Any]],
) -> tuple[StagingCandidate, Path, dict[str, Any]]:
    download_directory.mkdir(parents=True, exist_ok=True)
    audit_directory = join_remote_root(
        remote_root,
        LAYER_REMOTE_DIRECTORIES["audit"],
    )
    for candidate in candidates:
        local_manifest = download_directory / candidate.manifest_name
        try:
            _run_rclone(
                "copyto",
                (
                    f"{remote}:{audit_directory.as_posix()}/"
                    f"{candidate.manifest_name}"
                ),
                local_manifest.as_posix(),
            )
            payload = _read_json(local_manifest)
            _validate_collection_manifest(
                payload,
                candidate,
                remote_root,
            )
            return candidate, local_manifest, payload
        except Exception as error:
            rejected.append(
                {
                    "name": candidate.name,
                    "manifest_name": candidate.manifest_name,
                    "reason": sanitize_log_text(error),
                }
            )
            local_manifest.unlink(missing_ok=True)
    raise DailyConsolidationError(
        "Nenhum staging possui manifesto de coleta valido."
    )


def _validate_collection_manifest(
    payload: dict[str, Any],
    candidate: StagingCandidate,
    remote_root: PurePosixPath,
) -> None:
    if (
        payload.get("schema_version") != 1
        or payload.get("document_type") != "collection_backup"
        or payload.get("status") != "completed"
        or payload.get("backup_date") != candidate.backup_date.isoformat()
        or not EXECUTION_ID_PATTERN.fullmatch(
            str(payload.get("execution_id", ""))
        )
        or payload.get("file_name") != candidate.manifest_name
        or payload.get("required_layers") != ["latest", "staging"]
    ):
        raise DailyConsolidationError(
            "O manifesto da coleta nao esta concluido ou e inconsistente."
        )
    expected_audit_path = (
        LAYER_REMOTE_DIRECTORIES["audit"] / candidate.manifest_name
    ).as_posix()
    if payload.get("audit_remote_path") != expected_audit_path:
        raise DailyConsolidationError(
            "O caminho de auditoria diverge do manifesto da coleta."
        )
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise DailyConsolidationError(
            "O manifesto da coleta nao possui camadas validas."
        )
    staging = _validated_manifest_layer(
        layers,
        "staging",
        candidate.name,
        (
            remote_root
            / LAYER_REMOTE_DIRECTORIES["staging"]
            / candidate.name
        ).as_posix(),
        candidate.size_bytes,
    )
    latest = _validated_manifest_layer(
        layers,
        "latest",
        "ceasa-data-latest.zip",
        (
            remote_root
            / LAYER_REMOTE_DIRECTORIES["latest"]
            / "ceasa-data-latest.zip"
        ).as_posix(),
        candidate.size_bytes,
    )
    if latest["package"]["sha256"] != staging["package"]["sha256"]:
        raise DailyConsolidationError(
            "Staging e latest nao reutilizam o mesmo conteudo validado."
        )
    relationships = payload.get("relationships")
    if (
        not isinstance(relationships, dict)
        or relationships.get("staging_path")
        != candidate.managed_path.as_posix()
        or relationships.get("latest_path")
        != (
            LAYER_REMOTE_DIRECTORIES["latest"]
            / "ceasa-data-latest.zip"
        ).as_posix()
    ):
        raise DailyConsolidationError(
            "Os relacionamentos do manifesto da coleta sao invalidos."
        )
    if not SHA256_PATTERN.fullmatch(str(staging["package"]["sha256"])):
        raise DailyConsolidationError(
            "O manifesto da coleta nao possui SHA-256 valido."
        )


def _validate_existing_targets(
    *,
    remote: str,
    remote_root: PurePosixPath,
    backup_date: date,
    audit_directory: Path,
) -> dict[str, Any]:
    inventories: dict[str, dict[str, Any] | None] = {}
    for layer in (BackupLayer.DAILY, BackupLayer.DEEP):
        specification = BackupLayerSpec.for_layer(layer, backup_date)
        directory = specification.remote_directory
        entries = _rclone_list(
            remote,
            join_remote_root(remote_root, directory),
        )
        inventories[layer.value] = next(
            (
                entry
                for entry in entries
                if entry.get("Name") == specification.file_name
            ),
            None,
        )

    result: dict[str, Any] = {"status": "passed", "layers": {}}
    unresolved: dict[str, dict[str, Any]] = {}
    for layer in (BackupLayer.DAILY, BackupLayer.DEEP):
        specification = BackupLayerSpec.for_layer(layer, backup_date)
        inventory = inventories[layer.value]
        if inventory is None:
            result["layers"][layer.value] = {"status": "missing"}
            continue
        size = inventory.get("Size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
        ):
            raise DailyConsolidationError(
                f"O inventario da camada {layer.value} possui tamanho "
                "invalido."
            )
        unresolved[layer.value] = {
            "specification": specification,
            "size_bytes": size,
        }

    if not unresolved:
        return result

    audit_directory.mkdir(parents=True, exist_ok=True)
    audit_entries = _rclone_list(
        remote,
        join_remote_root(remote_root, LAYER_REMOTE_DIRECTORIES["audit"]),
    )
    audit_entries.sort(
        key=lambda entry: str(entry.get("Name", "")),
        reverse=True,
    )
    for entry in audit_entries:
        name = entry.get("Name")
        if not isinstance(name, str):
            continue
        try:
            validate_layer_remote_path(
                "audit",
                LAYER_REMOTE_DIRECTORIES["audit"] / name,
            )
        except ValueError:
            continue
        local_path = audit_directory / name
        try:
            _run_rclone(
                "copyto",
                _remote_file_spec(
                    remote,
                    remote_root,
                    LAYER_REMOTE_DIRECTORIES["audit"] / name,
                ),
                local_path.as_posix(),
            )
            payload = _read_json(local_path)
        except Exception:
            local_path.unlink(missing_ok=True)
            continue
        finally:
            local_path.unlink(missing_ok=True)

        for layer_name, target in list(unresolved.items()):
            specification = target["specification"]
            expected_path = (
                remote_root
                / specification.remote_directory
                / specification.file_name
            ).as_posix()
            if not _manifest_proves_layer(
                payload,
                layer_name,
                specification.file_name,
                expected_path,
                target["size_bytes"],
                backup_date,
                name,
            ):
                continue
            result["layers"][layer_name] = {
                "status": "validated_existing",
                "size_bytes": target["size_bytes"],
                "manifest_execution_id": payload.get("execution_id"),
                "manifest_status": payload.get("status"),
                "manifest_name": name,
            }
            del unresolved[layer_name]
        if not unresolved:
            break

    if unresolved:
        raise DailyConsolidationError(
            "Camadas existentes sem manifesto valido: "
            + ", ".join(sorted(unresolved))
            + "."
        )
    return result


def _manifest_proves_layer(
    payload: dict[str, Any],
    layer_name: str,
    file_name: str,
    remote_path: str,
    size_bytes: object,
    backup_date: date,
    manifest_name: str,
) -> bool:
    expected_audit_path = (
        LAYER_REMOTE_DIRECTORIES["audit"] / manifest_name
    ).as_posix()
    if (
        payload.get("schema_version") != 1
        or payload.get("status") not in {"completed", "failed"}
        or not EXECUTION_ID_PATTERN.fullmatch(
            str(payload.get("execution_id", ""))
        )
        or payload.get("backup_date") != backup_date.isoformat()
        or payload.get("file_name") != manifest_name
        or payload.get("audit_remote_path") != expected_audit_path
    ):
        return False
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        return False
    try:
        _validated_manifest_layer(
            layers,
            layer_name,
            file_name,
            remote_path,
            size_bytes,
        )
    except DailyConsolidationError:
        return False
    return True


def _discover_consolidated_staging_dates(
    *,
    remote: str,
    remote_root: PurePosixPath,
    reference_date: date,
    audit_directory: Path,
) -> frozenset[date]:
    audit_directory.mkdir(parents=True, exist_ok=True)
    entries = _rclone_list(
        remote,
        join_remote_root(remote_root, LAYER_REMOTE_DIRECTORIES["audit"]),
    )
    if not entries:
        return frozenset()
    _run_rclone(
        "copy",
        _remote_file_spec(
            remote,
            remote_root,
            LAYER_REMOTE_DIRECTORIES["audit"],
        ),
        audit_directory.as_posix(),
        "--include",
        "backup-*.json",
    )
    proven_dates: set[date] = set()
    for local_path in sorted(audit_directory.glob("backup-*.json")):
        name = local_path.name
        try:
            validate_layer_remote_path(
                "audit",
                LAYER_REMOTE_DIRECTORIES["audit"] / name,
            )
        except ValueError:
            continue
        try:
            payload = _read_json(local_path)
            proven_date = _consolidation_manifest_date(
                payload,
                name,
                remote_root,
            )
        except Exception:
            continue
        if proven_date is not None and proven_date <= reference_date:
            proven_dates.add(proven_date)
    return frozenset(proven_dates)


def _consolidation_manifest_date(
    payload: dict[str, Any],
    manifest_name: str,
    remote_root: PurePosixPath,
) -> date | None:
    expected_audit_path = (
        LAYER_REMOTE_DIRECTORIES["audit"] / manifest_name
    ).as_posix()
    if (
        payload.get("schema_version") != 1
        or payload.get("document_type") != "daily_consolidation"
        or payload.get("status") != "completed"
        or payload.get("file_name") != manifest_name
        or payload.get("audit_remote_path") != expected_audit_path
        or payload.get("required_layers") != ["daily", "deep"]
    ):
        return None
    try:
        backup_date = date.fromisoformat(str(payload.get("backup_date", "")))
    except ValueError:
        return None

    relationships = payload.get("relationships")
    source = (
        relationships.get("consolidation_source")
        if isinstance(relationships, dict)
        else None
    )
    if not isinstance(source, dict):
        return None
    try:
        staging_path = validate_layer_remote_path(
            "staging",
            str(source.get("staging_remote_path", "")),
        )
        source_manifest_path = validate_layer_remote_path(
            "audit",
            str(source.get("manifest_remote_path", "")),
        )
    except ValueError:
        return None
    if (
        source.get("backup_date") != backup_date.isoformat()
        or layer_file_date("staging", staging_path.name) != backup_date
        or layer_file_date("audit", source_manifest_path.name)
        != backup_date
        or not SHA256_PATTERN.fullmatch(str(source.get("sha256", "")))
    ):
        return None

    layers = payload.get("layers")
    if not isinstance(layers, dict):
        return None
    for layer in (BackupLayer.DAILY, BackupLayer.DEEP):
        specification = BackupLayerSpec.for_layer(layer, backup_date)
        layer_payload = layers.get(layer.value)
        if not isinstance(layer_payload, dict):
            return None
        if layer_payload.get("status") == "uploaded":
            package = layer_payload.get("package")
            size_bytes = (
                package.get("size_bytes")
                if isinstance(package, dict)
                else None
            )
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes < 1
            ):
                return None
            try:
                _validated_manifest_layer(
                    layers,
                    layer.value,
                    specification.file_name,
                    (
                        remote_root
                        / specification.remote_directory
                        / specification.file_name
                    ).as_posix(),
                    size_bytes,
                )
            except DailyConsolidationError:
                return None
            continue
        if not _is_valid_skipped_layer(layer_payload, specification):
            return None
    return backup_date


def _is_valid_skipped_layer(
    layer: dict[str, Any],
    specification: BackupLayerSpec,
) -> bool:
    package = layer.get("package")
    validation = layer.get("validation")
    upload = layer.get("upload")
    return bool(
        layer.get("status") == "skipped"
        and layer.get("file_name") == specification.file_name
        and isinstance(package, dict)
        and package.get("status") == "skipped"
        and isinstance(validation, dict)
        and validation.get("status") == "skipped"
        and isinstance(upload, dict)
        and upload.get("status") == "skipped"
    )


def _remote_file_spec(
    remote: str,
    remote_root: PurePosixPath,
    managed_path: PurePosixPath,
) -> str:
    full_path = join_remote_root(remote_root, managed_path)
    return f"{remote}:{full_path.as_posix()}"


def _validated_manifest_layer(
    layers: dict[str, Any],
    layer_name: str,
    file_name: str,
    remote_path: str,
    size_bytes: object,
) -> dict[str, Any]:
    layer = layers.get(layer_name)
    if not isinstance(layer, dict) or layer.get("status") != "uploaded":
        raise DailyConsolidationError(
            f"A camada {layer_name} nao foi publicada no manifesto."
        )
    package = layer.get("package")
    validation = layer.get("validation")
    upload = layer.get("upload")
    if (
        layer.get("file_name") != file_name
        or not isinstance(package, dict)
        or package.get("status") != "completed"
        or package.get("size_bytes") != size_bytes
        or not SHA256_PATTERN.fullmatch(str(package.get("sha256", "")))
        or not isinstance(validation, dict)
        or validation.get("status") != "passed"
        or not isinstance(upload, dict)
        or upload.get("status") != "completed"
        or upload.get("final_path") != remote_path
    ):
        raise DailyConsolidationError(
            f"A prova da camada {layer_name} esta incompleta."
        )
    return layer


def _manifest_layer_sha256(
    payload: dict[str, Any],
    layer_name: str,
) -> str:
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise DailyConsolidationError("Manifesto sem camadas validas.")
    layer = layers.get(layer_name)
    package = layer.get("package") if isinstance(layer, dict) else None
    sha256 = package.get("sha256") if isinstance(package, dict) else None
    if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
        raise DailyConsolidationError("SHA-256 ausente no manifesto.")
    return sha256


def _rclone_list(
    remote: str,
    directory: PurePosixPath,
) -> list[dict[str, Any]]:
    completed = _run_rclone(
        "lsjson",
        f"{remote}:{directory.as_posix()}",
        "--files-only",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DailyConsolidationError(
            "O inventario remoto retornou JSON invalido."
        ) from error
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) for item in payload
    ):
        raise DailyConsolidationError(
            "O inventario remoto possui formato invalido."
        )
    return payload


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DailyConsolidationError(
            f"Manifesto invalido em {path.name}."
        ) from error
    if not isinstance(payload, dict):
        raise DailyConsolidationError(
            f"O manifesto {path.name} nao e um objeto."
        )
    return payload


def _run_rclone(*arguments: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("rclone") is None:
        raise DailyConsolidationError("rclone nao encontrado no ambiente.")
    try:
        return subprocess.run(
            ["rclone", *arguments],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError as error:
        detail = sanitize_log_text(
            error.stderr.strip() or error.stdout.strip()
        )
        suffix = f": {detail}" if detail else ""
        raise DailyConsolidationError(
            f"rclone {arguments[0]} encerrou com codigo "
            f"{error.returncode}{suffix}."
        ) from error
