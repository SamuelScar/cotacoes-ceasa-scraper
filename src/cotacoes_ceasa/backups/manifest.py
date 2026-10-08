"""Manifesto auditavel das camadas de backup de uma execucao."""

from __future__ import annotations

import json
import os
import platform
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from cotacoes_ceasa.backups.packaging import (
    BackupArtifact,
    BackupLayer,
    BackupLayerSpec,
    COLLECTION_BACKUP_LAYERS,
    CONSOLIDATION_BACKUP_LAYERS,
    LAYERED_BACKUP_LAYERS,
    collect_source_metrics,
)
from cotacoes_ceasa.backups.policy import (
    layer_file_date,
    managed_remote_path,
    validate_layer_remote_path,
)
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
MANIFEST_DOCUMENT_TYPES = {
    "layered_backup",
    "collection_backup",
    "daily_consolidation",
}


class BackupManifestError(RuntimeError):
    """Indica um manifesto ausente, inconsistente ou encerrado."""


@dataclass(frozen=True)
class ManifestSourceBackup:
    """Referencia imutavel ao backup usado por uma consolidacao."""

    execution_id: str
    manifest_remote_path: str
    staging_remote_path: str
    sha256: str
    backup_date: date


@dataclass(frozen=True)
class ManifestRequest:
    execution_id: str
    execution_directory: Path
    source_directory: Path
    backup_date: date | None = None
    required_layers: frozenset[BackupLayer] | None = None
    document_type: str = "layered_backup"
    source_backup: ManifestSourceBackup | None = None
    started_at: datetime | None = None


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
    started_at = _normalize_started_at(request.started_at)
    backup_date = request.backup_date or started_at.date()
    document_type = _normalize_document_type(request.document_type)
    required_layers = _normalize_required_layers(
        request.required_layers,
        document_type,
    )
    source_backup = _normalize_source_backup(
        request.source_backup,
        document_type,
    )
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
    for layer in _manifest_layers(document_type):
        specification = BackupLayerSpec.for_layer(
            layer,
            backup_date,
            backup_datetime=started_at,
        )
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
        "document_type": document_type,
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
        "relationships": {
            "staging_path": _layer_final_path(layers, "staging"),
            "latest_path": _layer_final_path(layers, "latest"),
            "consolidation_source": source_backup,
        },
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


def record_reused_backup_artifact(
    manifest_path: Path,
    source_layer_name: str,
    target_layer_name: str,
) -> None:
    """Registra que duas publicacoes reutilizam o mesmo pacote validado."""
    if source_layer_name == target_layer_name:
        raise BackupManifestError(
            "As camadas de origem e destino da reutilizacao devem diferir."
        )
    payload = _load_running_manifest(manifest_path)
    source_layer = _manifest_layer(payload, source_layer_name)
    target_layer = _manifest_layer(payload, target_layer_name)
    if (
        source_layer.get("status") != "packaged"
        or source_layer.get("package", {}).get("status") != "completed"
        or source_layer.get("validation", {}).get("status") != "passed"
    ):
        raise BackupManifestError(
            "A reutilizacao exige um pacote de origem validado."
        )
    for field in ("format", "algorithm", "parameters"):
        _require_equal(
            field,
            source_layer.get(field),
            target_layer.get(field),
        )
    target_layer["status"] = "packaged"
    target_layer["package"] = deepcopy(source_layer["package"])
    target_layer["validation"] = {
        "status": "passed",
        "detail": f"Pacote reutilizado da camada {source_layer_name}.",
    }
    _append_history(
        payload,
        "package_reused",
        target_layer_name,
        f"Origem: {source_layer_name}.",
    )
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
    document_type = _manifest_document_type(payload)
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
    if document_type == "collection_backup":
        not_uploaded = [
            layer.value
            for layer in COLLECTION_BACKUP_LAYERS
            if not isinstance(layers.get(layer.value), dict)
            or layers[layer.value].get("status") != "uploaded"
        ]
        if not_uploaded:
            raise BackupManifestError(
                "O manifesto da coleta exige staging e latest publicados: "
                + ", ".join(sorted(not_uploaded))
                + "."
            )
    _normalize_source_backup(
        _source_backup_from_payload(payload),
        document_type,
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
    document_type: str,
) -> frozenset[BackupLayer]:
    defaults = {
        "layered_backup": LAYERED_BACKUP_LAYERS,
        "collection_backup": COLLECTION_BACKUP_LAYERS,
        "daily_consolidation": CONSOLIDATION_BACKUP_LAYERS,
    }
    required = defaults[document_type] if value is None else value
    if not required:
        raise BackupManifestError(
            "O manifesto exige ao menos uma camada de backup."
        )
    invalid = [item for item in required if not isinstance(item, BackupLayer)]
    if invalid:
        raise BackupManifestError(
            "O manifesto recebeu uma camada de backup desconhecida."
        )
    allowed = defaults[document_type]
    if not required.issubset(allowed):
        raise BackupManifestError(
            f"Camadas invalidas para o manifesto {document_type}: "
            + ", ".join(
                sorted(layer.value for layer in required.difference(allowed))
            )
            + "."
        )
    if document_type == "collection_backup" and required != allowed:
        raise BackupManifestError(
            "O manifesto da coleta exige as camadas staging e latest."
        )
    return required


def _manifest_required_layers(
    payload: dict[str, Any],
) -> frozenset[BackupLayer]:
    document_type = _manifest_document_type(payload)
    raw_layers = payload.get("required_layers")
    if raw_layers is None:
        return _normalize_required_layers(None, document_type)
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
    return _normalize_required_layers(required, document_type)


def _normalize_document_type(value: object) -> str:
    document_type = str(value).strip()
    if document_type not in MANIFEST_DOCUMENT_TYPES:
        raise BackupManifestError(
            f"Tipo de manifesto desconhecido: {document_type}."
        )
    return document_type


def _manifest_document_type(payload: dict[str, Any]) -> str:
    return _normalize_document_type(
        payload.get("document_type", "layered_backup")
    )


def _manifest_layers(document_type: str) -> tuple[BackupLayer, ...]:
    if document_type == "collection_backup":
        return (BackupLayer.STAGING, BackupLayer.LATEST)
    if document_type == "daily_consolidation":
        return (BackupLayer.DAILY, BackupLayer.DEEP)
    return (
        BackupLayer.LATEST,
        BackupLayer.DAILY,
        BackupLayer.DEEP,
    )


def _normalize_started_at(value: datetime | None) -> datetime:
    started_at = value or execution_now()
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise BackupManifestError(
            "A data de inicio do manifesto deve possuir fuso horario."
        )
    return started_at.astimezone(execution_now().tzinfo)


def _normalize_source_backup(
    value: ManifestSourceBackup | dict[str, Any] | None,
    document_type: str,
) -> dict[str, Any] | None:
    if value is None:
        if document_type == "daily_consolidation":
            raise BackupManifestError(
                "O manifesto da consolidacao exige o backup de origem."
            )
        return None
    if document_type != "daily_consolidation":
        raise BackupManifestError(
            "Somente a consolidacao diaria aceita um backup de origem."
        )
    source = (
        {
            "execution_id": value.execution_id,
            "manifest_remote_path": value.manifest_remote_path,
            "staging_remote_path": value.staging_remote_path,
            "sha256": value.sha256,
            "backup_date": value.backup_date.isoformat(),
        }
        if isinstance(value, ManifestSourceBackup)
        else value
    )
    if not isinstance(source, dict):
        raise BackupManifestError(
            "A referencia ao backup de origem deve ser um objeto."
        )
    execution_id = sanitize_log_text(str(source.get("execution_id", "")).strip())
    if not re.fullmatch(
        r"[0-9A-Za-z][0-9A-Za-z_-]{0,127}",
        execution_id,
    ):
        raise BackupManifestError(
            "A referencia de origem exige um identificador de execucao "
            "valido."
        )
    manifest_path = validate_layer_remote_path(
        "audit",
        str(source.get("manifest_remote_path", "")),
    )
    staging_path = validate_layer_remote_path(
        "staging",
        str(source.get("staging_remote_path", "")),
    )
    sha256 = str(source.get("sha256", "")).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise BackupManifestError(
            "A referencia de origem exige um SHA-256 valido."
        )
    try:
        backup_date = date.fromisoformat(str(source.get("backup_date", "")))
    except ValueError as error:
        raise BackupManifestError(
            "A referencia de origem possui uma data invalida."
        ) from error
    if layer_file_date("staging", staging_path.name) != backup_date:
        raise BackupManifestError(
            "A data do staging diverge da referencia de origem."
        )
    if layer_file_date("audit", manifest_path.name) != backup_date:
        raise BackupManifestError(
            "A data do manifesto diverge da referencia de origem."
        )
    return {
        "execution_id": execution_id,
        "manifest_remote_path": manifest_path.as_posix(),
        "staging_remote_path": staging_path.as_posix(),
        "sha256": sha256,
        "backup_date": backup_date.isoformat(),
    }


def _source_backup_from_payload(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    relationships = payload.get("relationships")
    if not isinstance(relationships, dict):
        return None
    source = relationships.get("consolidation_source")
    return source if isinstance(source, dict) else None


def _layer_final_path(
    layers: dict[str, dict[str, Any]],
    layer_name: str,
) -> str | None:
    layer = layers.get(layer_name)
    if not isinstance(layer, dict):
        return None
    upload = layer.get("upload")
    if not isinstance(upload, dict):
        return None
    final_path = upload.get("final_path")
    return str(final_path) if final_path is not None else None


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
