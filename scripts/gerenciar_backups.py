#!/usr/bin/env python3
"""Gerencia operacoes locais dos backups completos do projeto."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if SOURCE_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, SOURCE_ROOT.as_posix())

from cotacoes_ceasa.backups.packaging import (  # noqa: E402
    BackupLayer,
    BackupLayerSpec,
    BackupPackagingError,
    create_backup,
)
from cotacoes_ceasa.backups.manifest import (  # noqa: E402
    BackupManifestError,
    ManifestRequest,
    create_backup_manifest,
    finalize_backup_manifest,
    get_manifest_layer_sha256,
    record_backup_artifact,
    record_layer_outcome,
    record_onedrive_quota,
    record_retention_removal,
    record_upload_result,
)
from cotacoes_ceasa.backups.remote import (  # noqa: E402
    RemotePublicationError,
    RemotePublicationRequest,
    publish_backup_atomically,
)
from cotacoes_ceasa.backups.retention import (  # noqa: E402
    BackupRetentionError,
    RetentionPolicy,
    RetentionRequest,
    apply_remote_retention,
)
from cotacoes_ceasa.backups.restore import (  # noqa: E402
    BackupRestoreError,
    RestoreRequest,
    restore_backup,
)
from cotacoes_ceasa.execution import (  # noqa: E402
    sanitize_log_text,
    write_json_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gerencia os backups completos do projeto."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser(
        "criar",
        help="Cria e valida uma camada local de backup.",
    )
    create_parser.add_argument(
        "--camada",
        choices=tuple(layer.value for layer in BackupLayer),
        required=True,
    )
    create_parser.add_argument("--origem", type=Path, default=Path("data"))
    create_parser.add_argument("--saida", type=Path, required=True)
    create_parser.add_argument(
        "--data",
        type=date.fromisoformat,
        help="Data local no formato AAAA-MM-DD; o padrao e America/Sao_Paulo.",
    )
    create_parser.add_argument(
        "--resultado",
        type=Path,
        help="Caminho opcional para salvar as metricas em JSON.",
    )
    create_parser.add_argument(
        "--manifesto",
        type=Path,
        help="Manifesto que recebera o resultado da camada.",
    )

    publish_parser = subparsers.add_parser("publicar")
    publish_parser.add_argument(
        "--camada",
        choices=("staging", "latest", "daily", "deep", "audit"),
        required=True,
    )
    publish_parser.add_argument("--arquivo", type=Path, required=True)
    publish_parser.add_argument("--destino", required=True)
    publish_parser.add_argument(
        "--execution-id",
        default=os.getenv("COTACOES_EXECUTION_ID"),
    )
    publish_parser.add_argument(
        "--remote",
        default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
    )
    publish_parser.add_argument(
        "--root",
        default=os.getenv("DATA_ONEDRIVE_DIR", "cotacoes-ceasa"),
    )
    publish_parser.add_argument("--resultado", type=Path, required=True)
    publish_parser.add_argument("--manifesto", type=Path)
    publish_parser.add_argument("--sha256")

    retention_parser = subparsers.add_parser("retencao")
    retention_parser.add_argument(
        "--remote",
        default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
    )
    retention_parser.add_argument(
        "--root",
        default=os.getenv("DATA_ONEDRIVE_DIR", "cotacoes-ceasa"),
    )
    retention_parser.add_argument(
        "--publicacao",
        type=Path,
        action="append",
        default=[],
        help="JSON de uma publicacao valida desta execucao.",
    )
    retention_parser.add_argument("--manifesto", type=Path)
    retention_parser.add_argument("--resultado", type=Path, required=True)
    retention_parser.add_argument("--apply", action="store_true")
    retention_parser.add_argument(
        "--data-referencia",
        type=date.fromisoformat,
    )

    restore_parser = subparsers.add_parser(
        "restaurar",
        help="Valida e restaura atomicamente um pacote completo.",
    )
    restore_parser.add_argument("--arquivo", type=Path, required=True)
    restore_parser.add_argument(
        "--raiz-projeto",
        type=Path,
        default=PROJECT_ROOT,
    )
    restore_parser.add_argument("--manifesto", type=Path)
    restore_parser.add_argument("--sha256")
    restore_parser.add_argument(
        "--comparar-com",
        type=Path,
        help="Arvore data/ original para comparacao integral opcional.",
    )
    restore_parser.add_argument(
        "--preservar",
        type=Path,
        action="append",
        default=[],
        help=(
            "Diretorio da execucao atual, dentro de data/, que deve ser "
            "preservado."
        ),
    )
    restore_parser.add_argument("--resultado", type=Path, required=True)

    manifest_start_parser = subparsers.add_parser("manifesto-iniciar")
    manifest_start_parser.add_argument(
        "--diretorio-execucao",
        type=Path,
        default=_environment_path("COTACOES_EXECUTION_DIR"),
    )
    manifest_start_parser.add_argument(
        "--execution-id",
        default=os.getenv("COTACOES_EXECUTION_ID"),
    )
    manifest_start_parser.add_argument(
        "--origem", type=Path, default=Path("data")
    )
    manifest_start_parser.add_argument("--data", type=date.fromisoformat)

    package_parser = subparsers.add_parser("manifesto-registrar-pacote")
    package_parser.add_argument("--manifesto", type=Path, required=True)
    package_parser.add_argument("--resultado", type=Path, required=True)

    layer_parser = subparsers.add_parser("manifesto-registrar-camada")
    layer_parser.add_argument("--manifesto", type=Path, required=True)
    layer_parser.add_argument(
        "--camada",
        choices=tuple(layer.value for layer in BackupLayer),
        required=True,
    )
    layer_parser.add_argument(
        "--status", choices=("skipped", "failed"), required=True
    )
    layer_parser.add_argument("--detalhe")

    upload_parser = subparsers.add_parser("manifesto-registrar-upload")
    upload_parser.add_argument("--manifesto", type=Path, required=True)
    upload_parser.add_argument(
        "--camada",
        choices=tuple(layer.value for layer in BackupLayer),
        required=True,
    )
    upload_parser.add_argument(
        "--status",
        choices=("completed", "failed", "skipped"),
        required=True,
    )
    upload_parser.add_argument("--caminho-temporario")
    upload_parser.add_argument("--caminho-final")
    upload_parser.add_argument("--duracao", type=float)
    upload_parser.add_argument("--erro")

    quota_parser = subparsers.add_parser("manifesto-registrar-quota")
    quota_parser.add_argument("--manifesto", type=Path, required=True)
    quota_parser.add_argument(
        "--momento", choices=("before", "after"), required=True
    )
    quota_parser.add_argument("--arquivo", type=Path, required=True)

    removal_parser = subparsers.add_parser("manifesto-registrar-remocao")
    removal_parser.add_argument("--manifesto", type=Path, required=True)
    removal_parser.add_argument("--caminho", required=True)
    removal_parser.add_argument("--tamanho", type=int)
    removal_parser.add_argument("--data-backup", type=date.fromisoformat)

    finalize_parser = subparsers.add_parser("manifesto-finalizar")
    finalize_parser.add_argument("--manifesto", type=Path, required=True)
    finalize_parser.add_argument(
        "--status", choices=("completed", "failed"), required=True
    )
    finalize_parser.add_argument("--erro")
    args = parser.parse_args()

    try:
        if args.command == "criar":
            return create_local_backup(args)
        if args.command == "publicar":
            return publish_remote_backup(args)
        if args.command == "retencao":
            return run_retention(args)
        if args.command == "restaurar":
            return restore_local_backup(args)
        if args.command == "manifesto-iniciar":
            return start_manifest(args)
        if args.command == "manifesto-registrar-pacote":
            record_backup_artifact(args.manifesto, _read_json(args.resultado))
        elif args.command == "manifesto-registrar-camada":
            record_layer_outcome(
                args.manifesto, args.camada, args.status, args.detalhe
            )
        elif args.command == "manifesto-registrar-upload":
            record_upload_result(
                args.manifesto,
                args.camada,
                args.status,
                temporary_path=args.caminho_temporario,
                final_path=args.caminho_final,
                duration_seconds=args.duracao,
                error=args.erro,
            )
        elif args.command == "manifesto-registrar-quota":
            record_onedrive_quota(
                args.manifesto, args.momento, _read_json(args.arquivo)
            )
        elif args.command == "manifesto-registrar-remocao":
            record_retention_removal(
                args.manifesto,
                args.caminho,
                args.tamanho,
                args.data_backup,
            )
        elif args.command == "manifesto-finalizar":
            finalize_backup_manifest(args.manifesto, args.status, args.erro)
        else:
            parser.error(f"Comando desconhecido: {args.command}")
    except (
        OSError,
        ValueError,
        BackupManifestError,
        BackupPackagingError,
        BackupRestoreError,
    ) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    print(args.manifesto.as_posix())
    return 0


def create_local_backup(args: argparse.Namespace) -> int:
    try:
        layer = BackupLayer(args.camada)
        specification = BackupLayerSpec.for_layer(layer, args.data)
        artifact = create_backup(specification, args.origem, args.saida)
    except (OSError, ValueError, BackupPackagingError) as error:
        if args.manifesto:
            try:
                record_layer_outcome(
                    args.manifesto, args.camada, "failed", str(error)
                )
            except (OSError, BackupManifestError) as manifest_error:
                print(f"Erro no manifesto: {manifest_error}", file=sys.stderr)
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    result = artifact.as_dict()
    try:
        if args.resultado:
            write_json_atomic(args.resultado, result)
        if args.manifesto:
            record_backup_artifact(args.manifesto, artifact)
    except (OSError, BackupManifestError) as error:
        print(f"Erro ao registrar o resultado: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def start_manifest(args: argparse.Namespace) -> int:
    if args.diretorio_execucao is None or not args.execution_id:
        raise BackupManifestError(
            "Informe --diretorio-execucao e --execution-id, ou configure "
            "COTACOES_EXECUTION_DIR e COTACOES_EXECUTION_ID."
        )
    manifest_path = create_backup_manifest(
        ManifestRequest(
            execution_id=args.execution_id,
            execution_directory=args.diretorio_execucao,
            source_directory=args.origem,
            backup_date=args.data,
        )
    )
    print(manifest_path.as_posix())
    return 0


def publish_remote_backup(args: argparse.Namespace) -> int:
    if not args.execution_id:
        print("Erro: informe --execution-id.", file=sys.stderr)
        return 1

    result: dict[str, object]
    exit_code = 0
    try:
        expected_sha256 = args.sha256
        if args.manifesto and args.camada != "audit":
            manifest_sha256 = get_manifest_layer_sha256(
                args.manifesto,
                args.camada,
            )
            if expected_sha256 and expected_sha256.lower() != manifest_sha256:
                raise BackupManifestError(
                    "O SHA-256 informado diverge do manifesto."
                )
            expected_sha256 = manifest_sha256
        publication = publish_backup_atomically(
            RemotePublicationRequest(
                layer=args.camada,
                local_file=args.arquivo,
                destination_path=args.destino,
                execution_id=args.execution_id,
                expected_sha256=expected_sha256,
                remote=args.remote,
                remote_root=args.root,
            )
        )
        result = publication.as_dict()
    except RemotePublicationError as error:
        result = error.result.as_dict()
        exit_code = 1
    except (OSError, ValueError, BackupManifestError) as error:
        result = {
            "schema_version": 1,
            "status": "failed",
            "execution_id": args.execution_id,
            "layer": args.camada,
            "local_path": args.arquivo.as_posix(),
            "temporary_path": None,
            "final_path": args.destino,
            "rollback_path": None,
            "duration_seconds": None,
            "error": sanitize_log_text(error),
        }
        exit_code = 1

    if args.manifesto and args.camada != "audit":
        try:
            record_upload_result(
                args.manifesto,
                args.camada,
                str(result["status"]),
                temporary_path=_optional_string(
                    result.get("temporary_path")
                ),
                final_path=_optional_string(result.get("final_path")),
                duration_seconds=_optional_float(
                    result.get("duration_seconds")
                ),
                error=_optional_string(result.get("error")),
            )
            result["manifest_recording"] = {"status": "completed"}
        except (OSError, ValueError, BackupManifestError) as error:
            result["manifest_recording"] = {
                "status": "failed",
                "error": sanitize_log_text(error),
            }
            exit_code = 1

    write_json_atomic(args.resultado, result)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


def run_retention(args: argparse.Namespace) -> int:
    try:
        expected_execution_id = None
        if args.manifesto:
            manifest = _read_json(args.manifesto)
            manifest_execution_id = manifest.get("execution_id")
            if not isinstance(manifest_execution_id, str):
                raise BackupRetentionError(
                    "O manifesto nao possui identificador de execucao valido."
                )
            expected_execution_id = manifest_execution_id
        newly_published_layers = _newly_published_layers(
            args.publicacao,
            expected_execution_id,
        )
        result = apply_remote_retention(
            RetentionRequest(
                remote=args.remote,
                remote_root=args.root,
                policy=RetentionPolicy.from_environment(),
                newly_published_layers=frozenset(newly_published_layers),
                apply_changes=args.apply,
                manifest_path=args.manifesto,
                reference_date=args.data_referencia,
            )
        )
    except (OSError, ValueError, RuntimeError) as error:
        result = {
            "schema_version": 1,
            "status": "failed",
            "mode": "apply" if args.apply else "dry-run",
            "errors": [{"message": sanitize_log_text(error)}],
        }
    write_json_atomic(args.resultado, result)
    exit_code = 0 if result.get("status") == "completed" else 1
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output, file=sys.stdout if exit_code == 0 else sys.stderr)
    return exit_code


def restore_local_backup(args: argparse.Namespace) -> int:
    try:
        restoration = restore_backup(
            RestoreRequest(
                archive_path=args.arquivo,
                project_root=args.raiz_projeto,
                manifest_path=args.manifesto,
                expected_sha256=args.sha256,
                compare_with=args.comparar_com,
                preserve_paths=tuple(args.preservar),
            )
        )
        result = restoration.as_dict()
        exit_code = 0
    except (OSError, ValueError, BackupRestoreError) as error:
        result = {
            "schema_version": 1,
            "status": "failed",
            "archive_path": args.arquivo.as_posix(),
            "error": sanitize_log_text(error),
        }
        exit_code = 1
    write_json_atomic(args.resultado, result)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output, file=sys.stdout if exit_code == 0 else sys.stderr)
    return exit_code


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BackupManifestError(f"JSON invalido em {path}: {error}") from error
    if not isinstance(payload, dict):
        raise BackupManifestError(f"O JSON em {path} deve ser um objeto.")
    return payload


def _newly_published_layers(
    paths: list[Path],
    expected_execution_id: str | None,
) -> set[str]:
    layers: set[str] = set()
    for path in paths:
        publication = _read_json(path)
        layer = publication.get("layer")
        if layer not in {"latest", "daily", "deep", "audit"}:
            raise BackupRetentionError(
                f"Camada invalida no resultado de publicacao {path}."
            )
        if (
            expected_execution_id is not None
            and publication.get("execution_id") != expected_execution_id
        ):
            raise BackupRetentionError(
                f"A publicacao {path} pertence a outra execucao."
            )
        sha256 = publication.get("sha256")
        size_bytes = publication.get("size_bytes")
        integrity_valid = (
            isinstance(sha256, str)
            and len(sha256) == 64
            and all(character in "0123456789abcdef" for character in sha256)
            and isinstance(size_bytes, int)
            and not isinstance(size_bytes, bool)
            and size_bytes >= 0
        )
        if (
            publication.get("status") == "completed"
            and publication.get("validation") == "passed"
            and publication.get("already_present") is not True
            and integrity_valid
        ):
            layers.add(str(layer))
    return layers


def _environment_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Duracao remota invalida.")
    return float(value)


if __name__ == "__main__":
    raise SystemExit(main())
