"""Cria uma copia candidata com raws recuperados de backups validados."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.backups.policy import (
    LAYER_REMOTE_DIRECTORIES,
    join_remote_root,
    layer_file_date,
    normalize_remote_root,
    validate_layer_remote_path,
)
from cotacoes_ceasa.backups.restore import (
    RestoreRequest,
    collect_data_inventory,
    restore_backup,
)
from cotacoes_ceasa.execution import (
    execution_now,
    write_json_atomic,
    write_text_atomic,
)


SHA256_LENGTH = 64
INCOMPLETE_MARKER = "_INCOMPLETA"
PLAN_HEADERS = {
    "coleta_id",
    "fonte",
    "caminho_registrado",
    "destino_em_data",
    "sha256_esperado",
    "status",
    "acao_proposta",
    "backup_remoto",
    "sha256_backup",
    "data_backup",
    "manifesto",
    "conteudo_no_backup",
    "tipo_conteudo",
    "tamanho_conteudo",
}
RECOVERY_LAYERS = ("staging", "daily", "deep")


class RawRecoveryError(RuntimeError):
    """Indica que a copia candidata nao pode ser concluida com seguranca."""


@dataclass(frozen=True)
class BackupReference:
    remote_path: str
    sha256: str
    manifest_name: str
    backup_date: str

    @property
    def archive_name(self) -> str:
        return PurePosixPath(self.remote_path).name


@dataclass
class RecoveryOperation:
    target_path: PurePosixPath
    expected_sha256: str
    backup: BackupReference
    content_location: str
    content_kind: str
    content_size: int
    collection_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class RecoveryPlan:
    rows: int
    missing_unique_hashes: int
    recoverable_records: int
    recoverable_unique_hashes: int
    not_found_records: int
    invalid_target_records: int
    operations: tuple[RecoveryOperation, ...]


def build_recovery_input_inventory(
    *,
    plan_path: Path,
    planning_result_path: Path,
    remote_root: str,
) -> dict[str, object]:
    """Lista somente os backups autorizados e exigidos pelo plano validado."""
    plan_file = _resolve_file(plan_path, "plano de recuperacao")
    planning_result_file = _resolve_file(
        planning_result_path,
        "resultado do planejamento",
    )
    plan = _read_plan(plan_file)
    planning_result = _read_json(planning_result_file)
    _validate_planning_result(planning_result, plan, plan_file.name)
    root = normalize_remote_root(remote_root)

    references = sorted(
        {operation.backup for operation in plan.operations},
        key=lambda item: (item.backup_date, item.remote_path),
    )
    archive_names: dict[str, BackupReference] = {}
    manifest_names: dict[str, BackupReference] = {}
    backups: list[dict[str, object]] = []
    for reference in references:
        remote_path = PurePosixPath(reference.remote_path)
        try:
            managed_path = remote_path.relative_to(root)
        except ValueError as error:
            raise RawRecoveryError(
                "O backup do plano esta fora da raiz remota configurada: "
                f"{reference.remote_path}."
            ) from error

        layer = _recovery_layer(managed_path)
        validated_path = validate_layer_remote_path(layer, managed_path)
        backup_date = date.fromisoformat(reference.backup_date)
        if layer_file_date(layer, validated_path.name) != backup_date:
            raise RawRecoveryError(
                f"A data do backup diverge do plano: {validated_path.name}."
            )

        manifest_path = validate_layer_remote_path(
            "audit",
            PurePosixPath("backups/audit") / reference.manifest_name,
        )

        _reject_local_name_collision(
            archive_names,
            reference.archive_name,
            reference,
            "arquivo de backup",
        )
        _reject_local_name_collision(
            manifest_names,
            reference.manifest_name,
            reference,
            "manifesto",
        )
        backups.append(
            {
                "remote_path": join_remote_root(root, validated_path).as_posix(),
                "archive_name": reference.archive_name,
                "sha256": reference.sha256,
                "backup_date": reference.backup_date,
                "layer": layer,
                "manifest_name": reference.manifest_name,
                "manifest_remote_path": join_remote_root(
                    root,
                    manifest_path,
                ).as_posix(),
            }
        )

    return {
        "schema_version": 1,
        "status": "validated",
        "operation": "raw_recovery_input_inventory",
        "plan": {
            "path": plan_file.as_posix(),
            "sha256": calculate_sha256(plan_file),
            "planning_result": planning_result_file.as_posix(),
            "planning_result_sha256": calculate_sha256(
                planning_result_file
            ),
        },
        "remote_root": root.as_posix(),
        "backups_required": len(backups),
        "backups": backups,
        "safety": {
            "allowed_remote_operations": ["copyto_download"],
            "remote_files_modified": 0,
            "package_published": False,
        },
    }


def create_raw_recovery_candidate(
    *,
    current_data_directory: Path,
    plan_path: Path,
    planning_result_path: Path,
    archives_directory: Path,
    manifests_directory: Path,
    candidate_root: Path,
    work_directory: Path,
    result_path: Path,
    report_path: Path,
) -> dict[str, object]:
    """Recupera raws em uma nova arvore candidata sem alterar a origem."""
    started_at = execution_now()
    current_data = _resolve_directory(current_data_directory, "data atual")
    plan_file = _resolve_file(plan_path, "plano de recuperacao")
    planning_result_file = _resolve_file(
        planning_result_path,
        "resultado do planejamento",
    )
    archives = _resolve_directory(archives_directory, "diretorio de backups")
    manifests = _resolve_directory(
        manifests_directory,
        "diretorio de manifestos",
    )
    candidate = candidate_root.resolve()
    work = work_directory.resolve()
    _require_new_directory(candidate, "candidata")
    _require_new_directory(work, "trabalho")
    _reject_nested_path(candidate, current_data, "candidata")
    _reject_nested_path(work, current_data, "trabalho")

    planning_result = _read_json(planning_result_file)
    plan = _read_plan(plan_file)
    _validate_planning_result(planning_result, plan, plan_file.name)

    source_sqlite = current_data / "cotacoes.sqlite"
    if not source_sqlite.is_file() or source_sqlite.is_symlink():
        raise RawRecoveryError("O SQLite atual nao e um arquivo regular.")
    _validate_sqlite(source_sqlite)
    source_database_sha256 = calculate_sha256(source_sqlite)
    planned_database = planning_result["database"]
    assert isinstance(planned_database, dict)
    if planned_database.get("sha256_before") != source_database_sha256:
        raise RawRecoveryError(
            "O SQLite atual diverge do SQLite usado para gerar o plano."
        )

    source_inventory = collect_data_inventory(current_data)
    candidate.mkdir(parents=True)
    marker = candidate / INCOMPLETE_MARKER
    marker.write_text(
        "Copia candidata ainda nao concluida.\n",
        encoding="utf-8",
    )
    candidate_data = candidate / "data"
    shutil.copytree(current_data, candidate_data, symlinks=False)
    baseline_inventory = collect_data_inventory(candidate_data)
    if baseline_inventory != source_inventory:
        raise RawRecoveryError(
            "A copia inicial da candidata diverge da arvore atual."
        )
    candidate_database_sha256 = calculate_sha256(
        candidate_data / "cotacoes.sqlite"
    )
    if candidate_database_sha256 != source_database_sha256:
        raise RawRecoveryError("O SQLite mudou durante a criacao da candidata.")

    _preflight_targets(candidate_data, plan.operations)
    work.mkdir(parents=True)
    restores = work / "restores"
    restores.mkdir()

    operations_by_backup: dict[BackupReference, list[RecoveryOperation]] = (
        defaultdict(list)
    )
    for operation in plan.operations:
        operations_by_backup[operation.backup].append(operation)

    operation_results: list[dict[str, object]] = []
    backup_results: list[dict[str, object]] = []
    for backup in sorted(
        operations_by_backup,
        key=lambda item: (item.backup_date, item.remote_path),
    ):
        archive_path = archives / backup.archive_name
        manifest_path = manifests / backup.manifest_name
        archive = _resolve_file(archive_path, "arquivo de backup")
        manifest = _resolve_file(manifest_path, "manifesto de backup")
        if calculate_sha256(archive) != backup.sha256:
            raise RawRecoveryError(
                f"O backup local diverge do plano: {backup.archive_name}."
            )

        with tempfile.TemporaryDirectory(
            prefix="backup-",
            dir=restores,
        ) as temporary_root:
            restored_root = Path(temporary_root)
            restoration = restore_backup(
                RestoreRequest(
                    archive_path=archive,
                    project_root=restored_root,
                    manifest_path=manifest,
                    expected_sha256=backup.sha256,
                )
            )
            restored_data = restored_root / "data"
            recovered_before = len(operation_results)
            for operation in sorted(
                operations_by_backup[backup],
                key=lambda item: item.target_path.as_posix(),
            ):
                operation_results.append(
                    _apply_operation(
                        operation,
                        restored_data=restored_data,
                        candidate_data=candidate_data,
                    )
                )
            backup_results.append(
                {
                    "remote_path": backup.remote_path,
                    "sha256": backup.sha256,
                    "manifest": backup.manifest_name,
                    "backup_date": backup.backup_date,
                    "restore_status": restoration.status,
                    "archive_validation": restoration.archive_validation,
                    "checksum_validation": restoration.checksum_validation,
                    "manifest_status": restoration.manifest_status,
                    "sqlite_validation": restoration.sqlite_validation,
                    "inventory_validation": restoration.inventory_validation,
                    "target_operations": (
                        len(operation_results) - recovered_before
                    ),
                }
            )

    final_database_sha256 = calculate_sha256(
        candidate_data / "cotacoes.sqlite"
    )
    if final_database_sha256 != source_database_sha256:
        raise RawRecoveryError("O SQLite candidato foi alterado pela recuperacao.")
    final_inventory = collect_data_inventory(candidate_data)
    source_database_sha256_after = calculate_sha256(source_sqlite)
    source_inventory_after = collect_data_inventory(current_data)
    if (
        source_database_sha256_after != source_database_sha256
        or source_inventory_after != source_inventory
    ):
        raise RawRecoveryError(
            "A arvore de dados atual mudou durante a recuperacao."
        )
    recovered_operations = sum(
        1 for item in operation_results if item["status"] == "recovered"
    )
    already_present_operations = sum(
        1 for item in operation_results if item["status"] == "already_present"
    )
    recovered_records = sum(
        int(item["records"])
        for item in operation_results
        if item["status"] == "recovered"
    )
    already_present_records = sum(
        int(item["records"])
        for item in operation_results
        if item["status"] == "already_present"
    )

    result: dict[str, object] = {
        "schema_version": 1,
        "status": "completed",
        "operation": "raw_recovery_candidate",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": execution_now().isoformat(timespec="seconds"),
        "source": {
            "data_directory": current_data.as_posix(),
            "database_sha256_before": source_database_sha256,
            "database_sha256_after": source_database_sha256_after,
            "database_unchanged": True,
            "inventory_before": source_inventory.as_dict(),
            "inventory_after": source_inventory_after.as_dict(),
            "tree_unchanged": True,
        },
        "plan": {
            "path": plan_file.as_posix(),
            "sha256": calculate_sha256(plan_file),
            "planning_result": planning_result_file.as_posix(),
            "planning_result_sha256": calculate_sha256(
                planning_result_file
            ),
            "rows": plan.rows,
            "missing_unique_hashes": plan.missing_unique_hashes,
            "recoverable_records": plan.recoverable_records,
            "recoverable_unique_hashes": plan.recoverable_unique_hashes,
            "not_found_records": plan.not_found_records,
            "invalid_target_records": plan.invalid_target_records,
        },
        "candidate": {
            "root": candidate.as_posix(),
            "data_directory": candidate_data.as_posix(),
            "database_sha256_before": candidate_database_sha256,
            "database_sha256_after": final_database_sha256,
            "database_unchanged": True,
            "baseline_inventory": baseline_inventory.as_dict(),
            "final_inventory": final_inventory.as_dict(),
        },
        "recovery": {
            "backups_required": len(operations_by_backup),
            "target_operations": len(plan.operations),
            "operations_recovered": recovered_operations,
            "operations_already_present": already_present_operations,
            "records_recovered": recovered_records,
            "records_already_present": already_present_records,
        },
        "processed_backups": backup_results,
        "operations": operation_results,
        "safety": {
            "current_data_modified": False,
            "remote_access": "none",
            "remote_files_modified": 0,
            "existing_candidate_files_overwritten": 0,
            "package_published": False,
        },
    }
    write_json_atomic(result_path, result)
    write_text_atomic(report_path, _build_report(result))
    marker.unlink()
    return result


def _read_plan(path: Path) -> RecoveryPlan:
    with path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        headers = set(reader.fieldnames or ())
        if headers != PLAN_HEADERS:
            missing = sorted(PLAN_HEADERS - headers)
            unexpected = sorted(headers - PLAN_HEADERS)
            raise RawRecoveryError(
                "Cabecalho invalido no plano: "
                f"ausentes={missing}, inesperados={unexpected}."
            )
        rows = list(reader)

    operations: dict[str, RecoveryOperation] = {}
    recoverable_records = 0
    missing_hashes: set[str] = set()
    recoverable_hashes: set[str] = set()
    not_found_records = 0
    invalid_target_records = 0
    backup_signatures: dict[str, tuple[str, str, str]] = {}
    for row in rows:
        collection_id = _positive_int(row["coleta_id"], "coleta_id")
        expected_sha256 = _sha256(row["sha256_esperado"], "sha256_esperado")
        missing_hashes.add(expected_sha256)
        status = row["status"]
        if status == "nao_encontrado":
            if row["acao_proposta"] != "nenhuma":
                raise RawRecoveryError(
                    f"Acao invalida para a coleta {collection_id}."
                )
            not_found_records += 1
            continue
        if status == "destino_invalido":
            if row["acao_proposta"] != "revisao_manual":
                raise RawRecoveryError(
                    f"Acao invalida para a coleta {collection_id}."
                )
            invalid_target_records += 1
            continue
        if status != "encontrado" or row["acao_proposta"] != (
            "copiar_sem_sobrescrever"
        ):
            raise RawRecoveryError(
                f"Status ou acao invalida para a coleta {collection_id}."
            )

        target = _safe_data_target(row["destino_em_data"])
        remote_path = _safe_remote_path(row["backup_remoto"])
        backup_sha256 = _sha256(row["sha256_backup"], "sha256_backup")
        manifest_name = _safe_file_name(row["manifesto"], "manifesto")
        backup_date = date.fromisoformat(row["data_backup"]).isoformat()
        content_kind = row["tipo_conteudo"]
        if content_kind not in {"file", "zip_member"}:
            raise RawRecoveryError(
                f"Tipo de conteudo invalido para a coleta {collection_id}."
            )
        content_location = _safe_content_location(
            row["conteudo_no_backup"],
            content_kind,
        )
        content_size = _nonnegative_int(
            row["tamanho_conteudo"],
            "tamanho_conteudo",
        )
        backup = BackupReference(
            remote_path=remote_path,
            sha256=backup_sha256,
            manifest_name=manifest_name,
            backup_date=backup_date,
        )
        signature = (backup_sha256, manifest_name, backup_date)
        previous_signature = backup_signatures.setdefault(
            remote_path,
            signature,
        )
        if previous_signature != signature:
            raise RawRecoveryError(
                f"Evidencias conflitantes para o backup {remote_path}."
            )

        target_key = target.as_posix()
        operation = operations.get(target_key)
        if operation is None:
            operation = RecoveryOperation(
                target_path=target,
                expected_sha256=expected_sha256,
                backup=backup,
                content_location=content_location,
                content_kind=content_kind,
                content_size=content_size,
            )
            operations[target_key] = operation
        elif (
            operation.expected_sha256 != expected_sha256
            or operation.backup != backup
            or operation.content_location != content_location
            or operation.content_kind != content_kind
            or operation.content_size != content_size
        ):
            raise RawRecoveryError(
                f"Operacoes conflitantes para o destino {target_key}."
            )
        operation.collection_ids.append(collection_id)
        recoverable_records += 1
        recoverable_hashes.add(expected_sha256)

    return RecoveryPlan(
        rows=len(rows),
        missing_unique_hashes=len(missing_hashes),
        recoverable_records=recoverable_records,
        recoverable_unique_hashes=len(recoverable_hashes),
        not_found_records=not_found_records,
        invalid_target_records=invalid_target_records,
        operations=tuple(operations.values()),
    )


def _validate_planning_result(
    result: dict[str, object],
    plan: RecoveryPlan,
    plan_name: str,
) -> None:
    if (
        result.get("schema_version") != 1
        or result.get("status") != "completed"
        or result.get("operation") != "raw_recovery_plan"
    ):
        raise RawRecoveryError("O resultado do planejamento nao esta concluido.")
    database = result.get("database")
    coverage = result.get("recovery_coverage")
    inventory = result.get("backup_inventory")
    files = result.get("files")
    safety = result.get("safety")
    if not all(
        isinstance(item, dict)
        for item in (database, coverage, inventory, files, safety)
    ):
        raise RawRecoveryError("O resultado do planejamento esta incompleto.")
    assert isinstance(database, dict)
    assert isinstance(coverage, dict)
    assert isinstance(inventory, dict)
    assert isinstance(files, dict)
    assert isinstance(safety, dict)
    if (
        database.get("unchanged") is not True
        or database.get("sha256_before") != database.get("sha256_after")
    ):
        raise RawRecoveryError("O planejamento nao preservou o SQLite.")
    if inventory.get("errors") != 0:
        raise RawRecoveryError("O planejamento possui erros de backup.")
    if files.get("recovery_plan") != plan_name:
        raise RawRecoveryError("O resultado referencia outro arquivo de plano.")
    expected_coverage = {
        "missing_records": plan.rows,
        "missing_unique_hashes": plan.missing_unique_hashes,
        "records_found_in_backups": plan.recoverable_records,
        "unique_hashes_found_in_backups": plan.recoverable_unique_hashes,
        "records_not_found": plan.not_found_records,
        "records_with_invalid_target": plan.invalid_target_records,
    }
    for key, value in expected_coverage.items():
        if coverage.get(key) != value:
            raise RawRecoveryError(
                f"A cobertura do plano diverge em {key}."
            )
    if (
        safety.get("remote_access") != "read_only"
        or safety.get("remote_files_modified") != 0
        or safety.get("database_replaced") is not False
    ):
        raise RawRecoveryError("As garantias de seguranca do plano sao invalidas.")


def _preflight_targets(
    candidate_data: Path,
    operations: tuple[RecoveryOperation, ...],
) -> None:
    for operation in operations:
        target = candidate_data / operation.target_path.as_posix()
        if target.is_symlink():
            raise RawRecoveryError(
                f"O destino candidato e um link simbolico: {operation.target_path}."
            )
        if not target.exists():
            continue
        if not target.is_file():
            raise RawRecoveryError(
                f"O destino candidato nao e um arquivo: {operation.target_path}."
            )
        if calculate_sha256(target) != operation.expected_sha256:
            raise RawRecoveryError(
                "Arquivo existente diverge do plano e nao sera sobrescrito: "
                f"{operation.target_path}."
            )


def _apply_operation(
    operation: RecoveryOperation,
    *,
    restored_data: Path,
    candidate_data: Path,
) -> dict[str, object]:
    target = candidate_data / operation.target_path.as_posix()
    if target.exists():
        return _operation_result(operation, "already_present")
    target.parent.mkdir(parents=True, exist_ok=True)
    if operation.content_kind == "file":
        relative = PurePosixPath(operation.content_location).relative_to("data")
        source_path = restored_data / relative.as_posix()
        source = _resolve_file(source_path, "raw restaurado")
        with source.open("rb") as stream:
            _copy_verified(stream, target, operation)
    else:
        archive_location, member_name = operation.content_location.split("::", 1)
        relative = PurePosixPath(archive_location).relative_to("data")
        raw_archive = _resolve_file(
            restored_data / relative.as_posix(),
            "ZIP de raw restaurado",
        )
        with zipfile.ZipFile(raw_archive) as package:
            matches = [
                item
                for item in package.infolist()
                if item.filename == member_name and not item.is_dir()
            ]
            if len(matches) != 1:
                raise RawRecoveryError(
                    f"Membro ZIP ambiguo ou ausente: {member_name}."
                )
            with package.open(matches[0]) as stream:
                _copy_verified(stream, target, operation)
    return _operation_result(operation, "recovered")


def _copy_verified(
    source: BinaryIO,
    target: Path,
    operation: RecoveryOperation,
) -> None:
    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(temporary_descriptor, "wb") as destination:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if size != operation.content_size:
            raise RawRecoveryError(
                f"Tamanho divergente para {operation.target_path}."
            )
        if digest.hexdigest() != operation.expected_sha256:
            raise RawRecoveryError(
                f"SHA-256 divergente para {operation.target_path}."
            )
        temporary.chmod(0o644)
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            if (
                not target.is_file()
                or target.is_symlink()
                or calculate_sha256(target) != operation.expected_sha256
            ):
                raise RawRecoveryError(
                    "O destino surgiu com conteudo divergente: "
                    f"{operation.target_path}."
                ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _operation_result(
    operation: RecoveryOperation,
    status: str,
) -> dict[str, object]:
    return {
        "target": operation.target_path.as_posix(),
        "sha256": operation.expected_sha256,
        "status": status,
        "records": len(operation.collection_ids),
        "collection_ids": sorted(operation.collection_ids),
        "backup_remote_path": operation.backup.remote_path,
        "backup_sha256": operation.backup.sha256,
        "manifest": operation.backup.manifest_name,
        "content_location": operation.content_location,
        "content_kind": operation.content_kind,
        "content_size": operation.content_size,
    }


def _build_report(result: dict[str, object]) -> str:
    plan = result["plan"]
    candidate = result["candidate"]
    recovery = result["recovery"]
    assert isinstance(plan, dict)
    assert isinstance(candidate, dict)
    assert isinstance(recovery, dict)
    return "\n".join(
        [
            "# Recuperacao candidata dos arquivos raw",
            "",
            f"- Status: **{result['status']}**",
            "- Dados atuais alterados: **nao**",
            "- Arquivos remotos alterados: **0**",
            "- Pacotes publicados: **0**",
            "- SQLite preservado: "
            f"**{'sim' if candidate['database_unchanged'] else 'nao'}**",
            "",
            "## Plano",
            "",
            f"- Registros recuperaveis: **{plan['recoverable_records']:,}**",
            f"- Hashes recuperaveis: **{plan['recoverable_unique_hashes']:,}**",
            f"- Registros nao encontrados: **{plan['not_found_records']:,}**",
            "",
            "## Resultado",
            "",
            f"- Backups utilizados: **{recovery['backups_required']:,}**",
            f"- Destinos recuperados: **{recovery['operations_recovered']:,}**",
            f"- Destinos ja presentes: **{recovery['operations_already_present']:,}**",
            "- Registros atendidos pela recuperacao: "
            f"**{recovery['records_recovered']:,}**",
            "",
            (
                "A candidata deve passar pela auditoria integral antes de "
                "qualquer publicacao."
            ),
            "",
        ]
    )


def _safe_data_target(value: str) -> PurePosixPath:
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if (
        not value.strip()
        or path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 3
        or path.parts[0] != "raw"
    ):
        raise RawRecoveryError(f"Destino inseguro no plano: {value!r}.")
    return path


def _safe_remote_path(value: str) -> str:
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise RawRecoveryError(f"Caminho remoto inseguro no plano: {value!r}.")
    if path.name in {"", ".", ".."}:
        raise RawRecoveryError(f"Backup remoto invalido no plano: {value!r}.")
    return path.as_posix()


def _safe_content_location(value: str, kind: str) -> str:
    parts = value.split("::")
    if (kind == "file" and len(parts) != 1) or (
        kind == "zip_member" and len(parts) != 2
    ):
        raise RawRecoveryError(f"Local de conteudo invalido: {value!r}.")
    archive_path = PurePosixPath(parts[0].strip().replace("\\", "/"))
    if (
        archive_path.is_absolute()
        or ".." in archive_path.parts
        or len(archive_path.parts) < 3
        or archive_path.parts[0:2] != ("data", "raw")
    ):
        raise RawRecoveryError(f"Local de conteudo inseguro: {value!r}.")
    if kind == "zip_member":
        member = PurePosixPath(parts[1].strip().replace("\\", "/"))
        if (
            not parts[1].strip()
            or member.is_absolute()
            or ".." in member.parts
        ):
            raise RawRecoveryError(f"Membro ZIP inseguro: {parts[1]!r}.")
        return f"{archive_path.as_posix()}::{member.as_posix()}"
    return archive_path.as_posix()


def _safe_file_name(value: str, field_name: str) -> str:
    normalized = value.strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "/" in normalized
        or "\\" in normalized
        or path.name in {".", ".."}
    ):
        raise RawRecoveryError(f"{field_name} invalido no plano.")
    return path.name


def _recovery_layer(path: PurePosixPath) -> str:
    for layer in RECOVERY_LAYERS:
        if path.parent == LAYER_REMOTE_DIRECTORIES[layer]:
            return layer
    raise RawRecoveryError(
        "O plano referencia uma camada nao autorizada para recuperacao: "
        f"{path.as_posix()}."
    )


def _reject_local_name_collision(
    known: dict[str, BackupReference],
    name: str,
    reference: BackupReference,
    label: str,
) -> None:
    previous = known.setdefault(name, reference)
    if previous != reference:
        raise RawRecoveryError(
            f"Mais de um {label} seria salvo localmente como {name}."
        )


def _sha256(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise RawRecoveryError(f"{field_name} invalido no plano.")
    return normalized


def _positive_int(value: str, field_name: str) -> int:
    parsed = _nonnegative_int(value, field_name)
    if parsed < 1:
        raise RawRecoveryError(f"{field_name} deve ser positivo.")
    return parsed


def _nonnegative_int(value: str, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise RawRecoveryError(f"{field_name} invalido no plano.") from error
    if parsed < 0:
        raise RawRecoveryError(f"{field_name} deve ser nao negativo.")
    return parsed


def _resolve_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise RawRecoveryError(f"{label.capitalize()} nao e um arquivo regular.")
    return resolved


def _resolve_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.is_symlink():
        raise RawRecoveryError(f"{label.capitalize()} nao e um diretorio regular.")
    return resolved


def _require_new_directory(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise RawRecoveryError(
            f"O diretorio de {label} ja existe e nao sera sobrescrito: {path}."
        )


def _reject_nested_path(path: Path, source: Path, label: str) -> None:
    try:
        path.relative_to(source)
    except ValueError:
        return
    raise RawRecoveryError(f"O diretorio de {label} nao pode ficar dentro de data/.")


def _validate_sqlite(path: Path) -> None:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        raise RawRecoveryError("O SQLite atual falhou na validacao.") from error
    if not result or result[0] != "ok":
        raise RawRecoveryError("O SQLite atual falhou no quick_check.")


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RawRecoveryError(f"JSON invalido: {path.name}.") from error
    if not isinstance(payload, dict):
        raise RawRecoveryError(f"JSON nao e um objeto: {path.name}.")
    return payload
