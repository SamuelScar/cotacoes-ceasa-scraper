"""Manifesto auditavel das camadas de backup de uma execucao."""

from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from cotacoes_ceasa.backups.packaging import (
    BackupArtifact,
    BackupLayer,
    BackupLayerSpec,
    collect_source_metrics,
)
from cotacoes_ceasa.backups.policy import managed_remote_path
from cotacoes_ceasa.execution import (
    ExecutionContext,
    execution_now,
    sanitize_log_text,
    write_json_atomic,
)


MANIFEST_SCHEMA_VERSION = 1
FINAL_MANIFEST_STATUSES = {"completed", "failed"}
LAYER_OUTCOMES = {"skipped", "failed"}
UPLOAD_STATUSES = {"completed", "failed", "skipped"}


class BackupManifestError(RuntimeError):
    """Indica um manifesto ausente, inconsistente ou encerrado."""


@dataclass(frozen=True)
class ManifestRequest:
    execution_id: str
    execution_directory: Path
    source_directory: Path
    backup_date: date | None = None
    required_layers: frozenset[BackupLayer] | None = None


def create_backup_manifest(request: ManifestRequest) -> Path:
    """Cria o manifesto dentro dos logs da mesma execucao operacional."""
    execution_id = sanitize_log_text(request.execution_id.strip())
    if not execution_id:
        raise BackupManifestError("O identificador da execucao nao pode ser vazio.")

    source = request.source_directory.resolve(strict=True)
    if not source.is_dir():
        raise BackupManifestError(
            f"A origem do manifesto nao e um diretorio: {source}."
        )
    started_at = execution_now()
    backup_date = request.backup_date or started_at.date()
    required_layers = _normalize_required_layers(request.required_layers)
    file_name = f"backup-{started_at.strftime('%Y%m%d-%H%M%S')}.json"
    manifest_path = request.execution_directory / "etapas" / file_name
    if manifest_path.exists():
        raise BackupManifestError(f"O manifesto ja existe: {manifest_path}.")

    context = ExecutionContext(
        execution_id=execution_id,
        directory=request.execution_directory,
        externally_managed=True,
        step_name="backup-manifest",
    )
    context.initialize()
    _validate_execution_id(context.state_path, execution_id)

    layers: dict[str, dict[str, Any]] = {}
    for layer in BackupLayer:
        specification = BackupLayerSpec.for_layer(layer, backup_date)
        final_remote_path = managed_remote_path(
            specification.remote_directory.as_posix(),
            specification.file_name,
        )
        layers[layer.value] = {
            "status": "pending",
            "format": specification.format,
            "algorithm": specification.algorithm,
            "parameters": list(specification.parameters),
            "file_name": specification.file_name,
            "remote_directory": specification.remote_directory.as_posix(),
            "package": {
                "status": "pending",
                "local_path": None,
                "created_at": None,
                "source": None,
                "size_bytes": None,
                "reduction_percent": None,
                "compression_duration_seconds": None,
                "sha256": None,
            },
            "validation": {
                "status": "pending",
                "detail": None,
            },
            "upload": {
                "status": "pending",
                "temporary_path": None,
                "final_path": final_remote_path.as_posix(),
                "duration_seconds": None,
                "completed_at": None,
                "error": None,
            },
        }

    payload: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "execution_id": execution_id,
        "status": "running",
        "timezone": "America/Sao_Paulo",
        "backup_date": backup_date.isoformat(),
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": None,
        "file_name": file_name,
        "audit_remote_path": managed_remote_path(
            "backups/audit", file_name
        ).as_posix(),
        "source": {
            "path": source.as_posix(),
            "files": 0,
            "bytes": 0,
        },
        "required_layers": sorted(layer.value for layer in required_layers),
        "layers": layers,
        "onedrive": {
            "quota_before": None,
            "quota_after": None,
        },
        "retention": {
            "removed_files": [],
        },
        "runner": _runner_information(),
        "errors": [],
        "history": [
            {
                "occurred_at": started_at.isoformat(timespec="seconds"),
                "event": "manifest_created",
                "detail": "Manifesto de backup iniciado.",
            }
        ],
    }
    _write_manifest(manifest_path, payload)
    context.register_file(manifest_path)
    _stabilize_source_metrics(manifest_path, payload, source)
    return manifest_path


def record_backup_artifact(
    manifest_path: Path,
    artifact: BackupArtifact | dict[str, Any],
) -> None:
    """Registra metricas de um pacote que ja foi validado localmente."""
    payload = _load_running_manifest(manifest_path)
    artifact_data = (
        artifact.as_dict()
        if isinstance(artifact, BackupArtifact)
        else artifact
    )
    if not isinstance(artifact_data, dict):
        raise BackupManifestError("O resultado do pacote deve ser um objeto.")
    layer_name = str(artifact_data.get("layer", ""))
    layer = _manifest_layer(payload, layer_name)

    _require_equal("formato", layer["format"], artifact_data.get("format"))
    _require_equal("arquivo", layer["file_name"], artifact_data.get("file_name"))
    _require_equal("algoritmo", layer["algorithm"], artifact_data.get("algorithm"))
    _require_equal(
        "parametros",
        layer["parameters"],
        list(artifact_data.get("parameters") or []),
    )
    if artifact_data.get("validation") != "passed":
        raise BackupManifestError(
            "Somente pacotes com validacao concluida podem ser registrados."
        )

    artifact_source = artifact_data.get("source")
    if not isinstance(artifact_source, dict):
        raise BackupManifestError("As metricas da origem do pacote sao invalidas.")
    for field in ("files", "bytes"):
        value = artifact_source.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BackupManifestError(
                f"A metrica origem.{field} do pacote e invalida."
            )
    _validate_artifact_metrics(artifact_data)

    layer["status"] = "packaged"
    layer["package"].update(
        {
            "status": "completed",
            "local_path": artifact_data.get("local_path"),
            "created_at": artifact_data.get("created_at"),
            "source": {
                "path": artifact_source.get("path"),
                "files": artifact_source.get("files"),
                "bytes": artifact_source.get("bytes"),
            },
            "size_bytes": artifact_data.get("size_bytes"),
            "reduction_percent": artifact_data.get("reduction_percent"),
            "compression_duration_seconds": artifact_data.get(
                "compression_duration_seconds"
            ),
            "sha256": artifact_data.get("sha256"),
        }
    )
    layer["validation"] = {"status": "passed", "detail": None}
    _append_history(payload, "package_completed", layer_name)
    _write_manifest(manifest_path, payload)


def record_layer_outcome(
    manifest_path: Path,
    layer_name: str,
    status: str,
    detail: str | None = None,
) -> None:
    """Registra uma camada ignorada ou uma falha anterior ao upload."""
    if status not in LAYER_OUTCOMES:
        raise BackupManifestError(f"Resultado de camada invalido: {status}.")
    payload = _load_running_manifest(manifest_path)
    layer = _manifest_layer(payload, layer_name)
    layer["status"] = status
    if status == "failed":
        layer["package"]["status"] = "failed"
        layer["validation"] = {
            "status": "failed",
            "detail": sanitize_log_text(detail or "Falha sem detalhe."),
        }
        _append_error(payload, f"{layer_name}: {detail or 'Falha sem detalhe.'}")
    else:
        layer["package"]["status"] = "skipped"
        layer["validation"] = {
            "status": "skipped",
            "detail": sanitize_log_text(detail) if detail else None,
        }
        layer["upload"]["status"] = "skipped"
    _append_history(payload, f"layer_{status}", layer_name, detail)
    _write_manifest(manifest_path, payload)


def record_upload_result(
    manifest_path: Path,
    layer_name: str,
    status: str,
    *,
    temporary_path: str | None = None,
    final_path: str | None = None,
    duration_seconds: float | None = None,
    error: str | None = None,
) -> None:
    """Registra a publicacao remota de uma camada."""
    if status not in UPLOAD_STATUSES:
        raise BackupManifestError(f"Resultado de upload invalido: {status}.")
    if duration_seconds is not None and duration_seconds < 0:
        raise BackupManifestError("A duracao do upload nao pode ser negativa.")
    if status == "completed" and not final_path:
        raise BackupManifestError(
            "Um upload concluido precisa informar o caminho definitivo."
        )

    payload = _load_running_manifest(manifest_path)
    layer = _manifest_layer(payload, layer_name)
    if status == "completed" and (
        layer["package"].get("status") != "completed"
        or layer["validation"].get("status") != "passed"
    ):
        raise BackupManifestError(
            "O upload nao pode ser concluido sem um pacote local validado."
        )
    layer["upload"].update(
        {
            "status": status,
            "temporary_path": temporary_path,
            "final_path": final_path or layer["upload"]["final_path"],
            "duration_seconds": duration_seconds,
            "completed_at": execution_now().isoformat(timespec="seconds"),
            "error": sanitize_log_text(error) if error else None,
        }
    )
    if status == "completed":
        layer["status"] = "uploaded"
    elif status == "failed":
        layer["status"] = "failed"
        _append_error(payload, f"{layer_name}: {error or 'Falha no upload.'}")
    else:
        layer["status"] = "skipped"
    _append_history(payload, f"upload_{status}", layer_name, error)
    _write_manifest(manifest_path, payload)


def record_onedrive_quota(
    manifest_path: Path,
    moment: str,
    quota: dict[str, Any],
) -> None:
    """Registra o retorno do rclone about antes ou depois da publicacao."""
    field_names = {"before": "quota_before", "after": "quota_after"}
    if moment not in field_names:
        raise BackupManifestError("O momento da quota deve ser before ou after.")
    payload = _load_running_manifest(manifest_path)
    payload["onedrive"][field_names[moment]] = quota
    _append_history(payload, f"onedrive_quota_{moment}")
    _write_manifest(manifest_path, payload)


def record_retention_removal(
    manifest_path: Path,
    remote_path: str,
    size_bytes: int | None = None,
    backup_date: date | None = None,
) -> None:
    """Registra um arquivo removido pela politica de retencao."""
    if not remote_path.strip():
        raise BackupManifestError("O caminho removido nao pode ser vazio.")
    if size_bytes is not None and size_bytes < 0:
        raise BackupManifestError("O tamanho removido nao pode ser negativo.")
    payload = _load_running_manifest(manifest_path)
    payload["retention"]["removed_files"].append(
        {
            "path": remote_path,
            "size_bytes": size_bytes,
            "backup_date": backup_date.isoformat() if backup_date else None,
            "removed_at": execution_now().isoformat(timespec="seconds"),
        }
    )
    _append_history(payload, "retention_file_removed", detail=remote_path)
    _write_manifest(manifest_path, payload)


def record_legacy_migration_cleanup(
    manifest_path: Path,
    cleanup: dict[str, Any],
) -> None:
    """Registra a limpeza legada antes de finalizar o manifesto."""
    if cleanup.get("status") not in {"completed", "failed"}:
        raise BackupManifestError(
            "A limpeza legada deve estar concluida ou ter falhado."
        )
    if not isinstance(cleanup.get("planned_files"), list):
        raise BackupManifestError(
            "A limpeza legada deve informar os arquivos planejados."
        )
    if not isinstance(cleanup.get("removed_files"), list):
        raise BackupManifestError(
            "A limpeza legada deve informar os arquivos removidos."
        )

    payload = _load_running_manifest(manifest_path)
    if "legacy_migration" in payload:
        raise BackupManifestError(
            "A limpeza legada ja foi registrada neste manifesto."
        )
    payload["legacy_migration"] = {"cleanup": cleanup}
    _append_history(
        payload,
        "legacy_migration_cleanup",
        detail=str(cleanup["status"]),
    )
    _write_manifest(manifest_path, payload)


def finalize_backup_manifest(
    manifest_path: Path,
    status: str,
    error: str | None = None,
) -> None:
    """Encerra o manifesto sem impedir seu envio em caso de falha."""
    if status not in FINAL_MANIFEST_STATUSES:
        raise BackupManifestError(f"Status final invalido: {status}.")
    if status == "completed" and error:
        raise BackupManifestError(
            "Um manifesto concluido nao pode receber um erro final."
        )
    payload = _load_running_manifest(manifest_path)
    if status == "completed":
        _validate_completed_manifest(payload)
    if error:
        _append_error(payload, error)
    payload["status"] = status
    payload["finished_at"] = execution_now().isoformat(timespec="seconds")
    _append_history(payload, "manifest_finalized", detail=status)
    _write_manifest(manifest_path, payload)


def get_manifest_layer_sha256(manifest_path: Path, layer_name: str) -> str:
    """Retorna o hash do pacote validado registrado para uma camada."""
    payload = _load_manifest(manifest_path)
    layer = _manifest_layer(payload, layer_name)
    package = layer.get("package")
    if not isinstance(package, dict) or package.get("status") != "completed":
        raise BackupManifestError(
            f"A camada {layer_name} ainda nao possui pacote concluido."
        )
    sha256 = package.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise BackupManifestError(
            f"A camada {layer_name} nao possui SHA-256 valido."
        )
    return sha256


def _stabilize_source_metrics(
    manifest_path: Path,
    payload: dict[str, Any],
    source_directory: Path,
) -> None:
    """Inclui o manifesto na medicao sem deixar o tamanho autorreferente."""
    for _ in range(10):
        metrics = collect_source_metrics(source_directory)
        current = payload["source"]
        if (
            current.get("files") == metrics.files
            and current.get("bytes") == metrics.bytes
        ):
            return
        payload["source"] = {
            "path": metrics.path,
            "files": metrics.files,
            "bytes": metrics.bytes,
        }
        _write_manifest(manifest_path, payload)
    raise BackupManifestError(
        "Nao foi possivel estabilizar as metricas da origem do manifesto."
    )


def _load_running_manifest(manifest_path: Path) -> dict[str, Any]:
    payload = _load_manifest(manifest_path)
    if payload.get("status") != "running":
        raise BackupManifestError(
            f"O manifesto nao aceita novas alteracoes: {manifest_path}."
        )
    return payload


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupManifestError(
            f"Manifesto invalido em {manifest_path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise BackupManifestError(f"Manifesto invalido em {manifest_path}.")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise BackupManifestError("Versao de manifesto nao suportada.")
    return payload


def _write_manifest(manifest_path: Path, payload: dict[str, Any]) -> None:
    sanitized = _sanitize_value(payload)
    if not isinstance(sanitized, dict):
        raise BackupManifestError("O manifesto deve ser um objeto JSON.")
    write_json_atomic(manifest_path, sanitized)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_log_text(value)
    if isinstance(value, dict):
        return {
            sanitize_log_text(key): _sanitize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_log_text(value)


def _manifest_layer(
    payload: dict[str, Any],
    layer_name: str,
) -> dict[str, Any]:
    layers = payload.get("layers")
    if not isinstance(layers, dict) or layer_name not in layers:
        raise BackupManifestError(f"Camada ausente no manifesto: {layer_name}.")
    layer = layers[layer_name]
    if not isinstance(layer, dict):
        raise BackupManifestError(f"Camada invalida no manifesto: {layer_name}.")
    return layer


def _require_equal(field: str, expected: object, received: object) -> None:
    if expected != received:
        raise BackupManifestError(
            f"O campo {field} diverge do manifesto: esperado {expected}, "
            f"recebido {received}."
        )


def _validate_artifact_metrics(artifact: dict[str, Any]) -> None:
    for field in ("size_bytes", "compression_duration_seconds"):
        value = artifact.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BackupManifestError(f"A metrica {field} e invalida.")
        if value < 0:
            raise BackupManifestError(f"A metrica {field} nao pode ser negativa.")
    reduction = artifact.get("reduction_percent")
    if reduction is not None and (
        isinstance(reduction, bool) or not isinstance(reduction, (int, float))
    ):
        raise BackupManifestError("A metrica reduction_percent e invalida.")
    sha256 = artifact.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise BackupManifestError("O SHA-256 do pacote e invalido.")
    for field in ("local_path", "created_at"):
        value = artifact.get(field)
        if not isinstance(value, str) or not value.strip():
            raise BackupManifestError(f"O campo {field} e obrigatorio.")


def _validate_completed_manifest(payload: dict[str, Any]) -> None:
    layers = payload.get("layers")
    if not isinstance(layers, dict):
        raise BackupManifestError("As camadas do manifesto sao invalidas.")
    incomplete = [
        name
        for name, layer in layers.items()
        if not isinstance(layer, dict)
        or layer.get("status") not in {"uploaded", "skipped"}
    ]
    if incomplete:
        raise BackupManifestError(
            "O manifesto nao pode ser concluido; camadas pendentes: "
            + ", ".join(sorted(incomplete))
            + "."
        )
    required_layers = _manifest_required_layers(payload)
    latest = layers.get(BackupLayer.LATEST.value)
    if (
        BackupLayer.LATEST in required_layers
        and (
            not isinstance(latest, dict)
            or latest.get("status") != "uploaded"
        )
    ):
        raise BackupManifestError(
            "O manifesto nao pode ser concluido sem publicar a camada latest."
        )
    if payload.get("errors"):
        raise BackupManifestError(
            "Um manifesto com erros deve ser finalizado com status failed."
        )
    onedrive = payload.get("onedrive")
    if not isinstance(onedrive, dict) or any(
        not isinstance(onedrive.get(field), dict)
        for field in ("quota_before", "quota_after")
    ):
        raise BackupManifestError(
            "As quotas do OneDrive antes e depois devem ser registradas."
        )


def _normalize_required_layers(
    value: frozenset[BackupLayer] | None,
) -> frozenset[BackupLayer]:
    required = frozenset(BackupLayer) if value is None else value
    if not required:
        raise BackupManifestError(
            "O manifesto exige ao menos uma camada de backup."
        )
    invalid = [item for item in required if not isinstance(item, BackupLayer)]
    if invalid:
        raise BackupManifestError(
            "O manifesto recebeu uma camada de backup desconhecida."
        )
    return required


def _manifest_required_layers(
    payload: dict[str, Any],
) -> frozenset[BackupLayer]:
    raw_layers = payload.get("required_layers")
    if raw_layers is None:
        return frozenset(BackupLayer)
    if not isinstance(raw_layers, list) or not raw_layers:
        raise BackupManifestError(
            "As camadas obrigatorias do manifesto sao invalidas."
        )
    try:
        required = frozenset(BackupLayer(item) for item in raw_layers)
    except (TypeError, ValueError) as error:
        raise BackupManifestError(
            "As camadas obrigatorias do manifesto sao desconhecidas."
        ) from error
    if len(required) != len(raw_layers):
        raise BackupManifestError(
            "As camadas obrigatorias do manifesto estao duplicadas."
        )
    return required


def _append_error(payload: dict[str, Any], error: str) -> None:
    payload.setdefault("errors", []).append(
        {
            "occurred_at": execution_now().isoformat(timespec="seconds"),
            "message": sanitize_log_text(error),
        }
    )


def _append_history(
    payload: dict[str, Any],
    event: str,
    layer: str | None = None,
    detail: str | None = None,
) -> None:
    payload.setdefault("history", []).append(
        {
            "occurred_at": execution_now().isoformat(timespec="seconds"),
            "event": event,
            "layer": layer,
            "detail": sanitize_log_text(detail) if detail else None,
        }
    )


def _validate_execution_id(state_path: Path, execution_id: str) -> None:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupManifestError(
            f"Estado da execucao invalido em {state_path}: {error}"
        ) from error
    if not isinstance(state, dict) or state.get("execution_id") != execution_id:
        raise BackupManifestError(
            "O identificador do manifesto diverge do contexto da execucao."
        )
    if state.get("complete") is True:
        raise BackupManifestError(
            "Nao e permitido iniciar backup em uma execucao ja finalizada."
        )


def _runner_information() -> dict[str, Any]:
    environment_names = (
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "GITHUB_REF",
        "GITHUB_WORKFLOW",
        "GITHUB_JOB",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "RUNNER_NAME",
    )
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "environment": {
            name.lower(): os.getenv(name) for name in environment_names
        },
    }
