"""Publicacao transacional dos backups em um remote rclone."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.backups.policy import (
    join_remote_root,
    normalize_remote_root,
    validate_layer_remote_path,
    validate_remote_name,
)
from cotacoes_ceasa.execution import execution_now, sanitize_log_text


@dataclass(frozen=True)
class RemotePublicationRequest:
    layer: str
    local_file: Path
    destination_path: str
    execution_id: str
    expected_sha256: str | None = None
    remote: str = "onedrive"
    remote_root: str = "cotacoes-ceasa"
    allow_local_name_mismatch: bool = False


@dataclass
class RemotePublicationResult:
    schema_version: int
    status: str
    execution_id: str
    layer: str
    local_path: str
    size_bytes: int
    sha256: str
    validation: str
    remote: str
    remote_root: str
    temporary_path: str
    final_path: str
    rollback_path: str
    duration_seconds: float | None = None
    already_present: bool = False
    previous_destination_preserved: bool = False
    rollback_restored: bool = False
    removed_temporary_paths: list[str] = field(default_factory=list)
    error: str | None = None
    completed_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RemotePublicationError(RuntimeError):
    """Mantem o resultado parcial de uma publicacao remota que falhou."""

    def __init__(self, message: str, result: RemotePublicationResult):
        super().__init__(message)
        self.result = result


def publish_backup_atomically(
    request: RemotePublicationRequest,
) -> RemotePublicationResult:
    """Envia, valida e promove um arquivo, restaurando o anterior na falha."""
    remote = validate_remote_name(request.remote)
    remote_root = normalize_remote_root(request.remote_root)
    destination = validate_layer_remote_path(
        request.layer,
        request.destination_path,
    )
    execution_id = request.execution_id.strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z_-]{0,127}", execution_id):
        raise ValueError("Identificador de execucao invalido para publicacao.")
    if request.local_file.is_symlink():
        raise ValueError("Links simbolicos nao podem ser publicados.")
    local_file = request.local_file.resolve(strict=True)
    if not local_file.is_file():
        raise ValueError(f"Arquivo local invalido para publicacao: {local_file}.")
    if (
        not request.allow_local_name_mismatch
        and local_file.name != destination.name
    ):
        raise ValueError(
            "O nome do arquivo local deve ser igual ao nome definitivo remoto."
        )
    if shutil.which("rclone") is None:
        missing_tool_result = _initial_result(
            request,
            remote,
            remote_root,
            destination,
            local_file,
            execution_id,
        )
        missing_tool_result.status = "failed"
        missing_tool_result.validation = "failed"
        missing_tool_result.error = "rclone nao encontrado no ambiente."
        missing_tool_result.completed_at = execution_now().isoformat(
            timespec="seconds"
        )
        raise RemotePublicationError(
            "rclone nao encontrado no ambiente.",
            missing_tool_result,
        )

    result = _initial_result(
        request,
        remote,
        remote_root,
        destination,
        local_file,
        execution_id,
    )
    started_at = time.monotonic()
    final_spec = _remote_spec(remote, result.final_path)
    temporary_spec = _remote_spec(remote, result.temporary_path)
    rollback_spec = _remote_spec(remote, result.rollback_path)

    try:
        final_parent = str(PurePosixPath(result.final_path).parent)
        _run_rclone("mkdir", _remote_spec(remote, final_parent))
        _recover_interrupted_attempt(
            remote,
            final_spec,
            temporary_spec,
            rollback_spec,
            result,
        )
        final_exists = _remote_file_exists(remote, final_spec)
        if request.layer != "latest" and final_exists:
            _require_remote_size(final_spec, result.size_bytes)
            result.already_present = True
            return _complete(result, started_at, status="skipped")

        _run_rclone("copyto", local_file.as_posix(), temporary_spec)
        _require_remote_size(temporary_spec, result.size_bytes)

        if request.layer != "latest" and _remote_file_exists(remote, final_spec):
            _require_remote_size(final_spec, result.size_bytes)
            _delete_remote_file(temporary_spec, result)
            result.already_present = True
            return _complete(result, started_at, status="skipped")

        if request.layer == "latest" and final_exists:
            previous_size = _remote_file_size(final_spec)
            _run_rclone("copyto", final_spec, rollback_spec)
            _require_remote_size(rollback_spec, previous_size)
            result.previous_destination_preserved = True

        try:
            _run_rclone("moveto", temporary_spec, final_spec)
            _require_remote_size(final_spec, result.size_bytes)
        except Exception:
            _restore_previous_destination(
                remote,
                final_spec,
                temporary_spec,
                rollback_spec,
                result,
            )
            raise

        if _remote_file_exists(remote, rollback_spec):
            _delete_remote_file(rollback_spec, result)
        return _complete(result, started_at)
    except Exception as error:
        result.error = sanitize_log_text(error)
        _best_effort_recovery(
            remote,
            final_spec,
            temporary_spec,
            rollback_spec,
            result,
        )
        result.status = "failed"
        result.validation = "failed"
        result.duration_seconds = round(time.monotonic() - started_at, 3)
        result.completed_at = execution_now().isoformat(timespec="seconds")
        raise RemotePublicationError(result.error, result) from error


def _initial_result(
    request: RemotePublicationRequest,
    remote: str,
    remote_root: PurePosixPath,
    destination: PurePosixPath,
    local_file: Path,
    execution_id: str,
) -> RemotePublicationResult:
    final_path = join_remote_root(remote_root, destination)
    temporary_path = final_path.with_name(
        f"{final_path.name}.tmp.{execution_id}"
    )
    rollback_path = final_path.with_name(f"{final_path.name}.rollback")
    sha256 = calculate_sha256(local_file)
    if request.expected_sha256 is not None:
        expected_sha256 = request.expected_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("SHA-256 esperado invalido para publicacao.")
        if sha256 != expected_sha256:
            raise ValueError(
                "O SHA-256 local diverge do pacote registrado no manifesto."
            )
    return RemotePublicationResult(
        schema_version=1,
        status="running",
        execution_id=execution_id,
        layer=request.layer,
        local_path=local_file.as_posix(),
        size_bytes=local_file.stat().st_size,
        sha256=sha256,
        validation="pending",
        remote=remote,
        remote_root=remote_root.as_posix(),
        temporary_path=temporary_path.as_posix(),
        final_path=final_path.as_posix(),
        rollback_path=rollback_path.as_posix(),
    )


def _recover_interrupted_attempt(
    remote: str,
    final_spec: str,
    temporary_spec: str,
    rollback_spec: str,
    result: RemotePublicationResult,
) -> None:
    final_exists = _remote_file_exists(remote, final_spec)
    if _remote_file_exists(remote, rollback_spec):
        if final_exists:
            try:
                _require_remote_size(final_spec, result.size_bytes)
            except RuntimeError:
                _restore_rollback_copy(
                    final_spec,
                    rollback_spec,
                    result,
                )
            else:
                _delete_remote_file(rollback_spec, result)
        else:
            _restore_rollback_copy(final_spec, rollback_spec, result)
    if _remote_file_exists(remote, temporary_spec):
        _delete_remote_file(temporary_spec, result)


def _restore_previous_destination(
    remote: str,
    final_spec: str,
    temporary_spec: str,
    rollback_spec: str,
    result: RemotePublicationResult,
) -> None:
    if _remote_file_exists(remote, rollback_spec):
        _restore_rollback_copy(final_spec, rollback_spec, result)
    elif _remote_file_exists(remote, final_spec):
        try:
            _require_remote_size(final_spec, result.size_bytes)
        except RuntimeError:
            _delete_remote_file(final_spec, result, record=False)
    if _remote_file_exists(remote, temporary_spec):
        _delete_remote_file(temporary_spec, result)


def _best_effort_recovery(
    remote: str,
    final_spec: str,
    temporary_spec: str,
    rollback_spec: str,
    result: RemotePublicationResult,
) -> None:
    try:
        if _remote_file_exists(remote, rollback_spec):
            _restore_rollback_copy(final_spec, rollback_spec, result)
        if _remote_file_exists(remote, temporary_spec):
            _delete_remote_file(temporary_spec, result)
    except Exception as recovery_error:
        recovery_detail = sanitize_log_text(recovery_error)
        result.error = (
            f"{result.error}; falha adicional no rollback: {recovery_detail}"
            if result.error
            else f"Falha adicional no rollback: {recovery_detail}"
        )


def _restore_rollback_copy(
    final_spec: str,
    rollback_spec: str,
    result: RemotePublicationResult,
) -> None:
    rollback_size = _remote_file_size(rollback_spec)
    _run_rclone("copyto", rollback_spec, final_spec)
    _require_remote_size(final_spec, rollback_size)
    result.rollback_restored = True
    _delete_remote_file(rollback_spec, result)


def _complete(
    result: RemotePublicationResult,
    started_at: float,
    *,
    status: str = "completed",
) -> RemotePublicationResult:
    result.status = status
    result.validation = (
        "passed" if status == "completed" else "skipped_existing"
    )
    result.duration_seconds = round(time.monotonic() - started_at, 3)
    result.completed_at = execution_now().isoformat(timespec="seconds")
    return result


def _remote_file_exists(remote: str, remote_spec: str) -> bool:
    path = PurePosixPath(remote_spec.split(":", 1)[1])
    parent_spec = _remote_spec(remote, path.parent.as_posix())
    completed = _run_rclone(
        "lsf",
        parent_spec,
        "--files-only",
        "--include",
        path.name,
    )
    return path.name in completed.stdout.splitlines()


def _remote_file_size(remote_spec: str) -> int:
    completed = _run_rclone("lsjson", remote_spec, "--stat")
    try:
        payload = json.loads(completed.stdout)
        size = payload["Size"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"O rclone nao informou o tamanho de {remote_spec}."
        ) from error
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RuntimeError(f"Tamanho remoto invalido para {remote_spec}.")
    return size


def _require_remote_size(remote_spec: str, expected_size: int) -> None:
    received_size = _remote_file_size(remote_spec)
    if received_size != expected_size:
        raise RuntimeError(
            f"Tamanho remoto divergente em {remote_spec}: "
            f"esperado {expected_size}, recebido {received_size}."
        )


def _delete_remote_file(
    remote_spec: str,
    result: RemotePublicationResult,
    *,
    record: bool = True,
) -> None:
    _run_rclone("deletefile", remote_spec)
    path = remote_spec.split(":", 1)[1]
    if record and path not in result.removed_temporary_paths:
        result.removed_temporary_paths.append(path)


def _remote_spec(remote: str, path: str) -> str:
    return f"{remote}:{path}"


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
