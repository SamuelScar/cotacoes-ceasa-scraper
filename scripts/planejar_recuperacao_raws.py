#!/usr/bin/env python3
"""Planeja a recuperacao de raws ausentes sem alterar dados ou backups."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for import_path in (PROJECT_ROOT, SOURCE_ROOT):
    if import_path.as_posix() not in sys.path:
        sys.path.insert(0, import_path.as_posix())

from auditar_banco import DatabaseAuditor, uses_v4_schema  # noqa: E402
from cotacoes_ceasa.audit_v4 import V4DatabaseAuditor  # noqa: E402
from cotacoes_ceasa.backups.packaging import calculate_sha256  # noqa: E402
from cotacoes_ceasa.backups.policy import (  # noqa: E402
    join_remote_root,
    layer_file_date,
    normalize_remote_root,
    validate_layer_remote_path,
    validate_remote_name,
)
from cotacoes_ceasa.backups.restore import (  # noqa: E402
    RestoreRequest,
    restore_backup,
)
from cotacoes_ceasa.execution import (  # noqa: E402
    execution_now,
    sanitize_log_text,
    write_json_atomic,
)


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RECOVERY_LAYERS = ("deep", "daily", "staging")
LAYER_DIRECTORIES = {
    "staging": PurePosixPath("backups/staging"),
    "daily": PurePosixPath("backups/history/daily"),
    "deep": PurePosixPath("backups/history/deep"),
}
LAYER_PRIORITY = {"deep": 0, "daily": 1, "staging": 2}


@dataclass(frozen=True)
class MissingRaw:
    collection_id: int
    source: str
    stored_path: str
    target_path: str | None
    expected_sha256: str


@dataclass(frozen=True)
class ManifestEvidence:
    manifest_path: Path
    manifest_name: str
    layer: str
    backup_date: date
    managed_remote_path: str
    archive_name: str
    sha256: str
    size_bytes: int
    source_files: int
    source_bytes: int


@dataclass(frozen=True)
class BackupCandidate:
    evidence: ManifestEvidence
    remote_path: str


@dataclass(frozen=True)
class RawEvidence:
    expected_sha256: str
    backup_remote_path: str
    backup_sha256: str
    backup_date: str
    manifest_name: str
    content_location: str
    content_kind: str
    content_size: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Localiza raws ausentes em backups historicos e gera um plano "
            "somente leitura."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/cotacoes.sqlite"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument(
        "--remote",
        default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
    )
    parser.add_argument(
        "--root",
        default=os.getenv("DATA_ONEDRIVE_DIR", "cotacoes-ceasa"),
    )
    args = parser.parse_args()
    output_existed_before = args.output.exists()

    try:
        result, exit_code = build_plan(
            database_path=args.database,
            output_directory=args.output,
            work_directory=args.work,
            remote=args.remote,
            remote_root=args.root,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        output_directory = args.output.resolve()
        failure = {
            "schema_version": 1,
            "status": "failed",
            "operation": "raw_recovery_plan",
            "error": sanitize_log_text(error),
            "safety": _safety_summary(),
        }
        if not output_existed_before:
            output_directory.mkdir(parents=True, exist_ok=True)
            write_json_atomic(output_directory / "resultado.json", failure)
            _write_failure_report(output_directory / "resumo.md", failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def build_plan(
    *,
    database_path: Path,
    output_directory: Path,
    work_directory: Path,
    remote: str,
    remote_root: str,
) -> tuple[dict[str, object], int]:
    started_at = execution_now()
    started_monotonic = time.monotonic()
    database = database_path.resolve(strict=True)
    if not database.is_file() or database.is_symlink():
        raise RuntimeError(f"SQLite invalido: {database}.")

    output = output_directory.resolve()
    if output.exists():
        raise RuntimeError(f"O diretorio de saida ja existe: {output}.")
    output.mkdir(parents=True)

    work = work_directory.resolve()
    if work.exists():
        raise RuntimeError(f"O diretorio de trabalho ja existe: {work}.")
    work.mkdir(parents=True)

    safe_remote = validate_remote_name(remote)
    safe_root = normalize_remote_root(remote_root)
    database_sha256_before = calculate_sha256(database)

    print("[recuperacao] Classificando raws registrados no SQLite.", flush=True)
    model, traceability, missing_raws = _classify_missing_raws(
        database,
        output / "classificacao-atual",
    )
    missing_hashes = {
        item.expected_sha256
        for item in missing_raws
        if item.target_path is not None
    }

    manifests_directory = work / "manifests"
    manifests_directory.mkdir()
    skipped_backups: list[dict[str, object]] = []
    processed_backups: list[dict[str, object]] = []
    backup_errors: list[dict[str, object]] = []
    found_by_hash: dict[str, RawEvidence] = {}

    if missing_hashes:
        print("[recuperacao] Baixando manifestos de auditoria.", flush=True)
        _download_manifests(
            safe_remote,
            safe_root,
            manifests_directory,
        )
        manifest_index, manifest_warnings = _index_manifests(
            manifests_directory
        )
        skipped_backups.extend(manifest_warnings)

        print("[recuperacao] Listando backups disponiveis.", flush=True)
        remote_inventory = _list_remote_backups(safe_remote, safe_root)
        candidates, candidate_warnings = _select_candidates(
            remote_inventory,
            manifest_index,
            safe_root,
        )
        skipped_backups.extend(candidate_warnings)
        if not candidates:
            raise RuntimeError(
                "Nenhum backup historico validado por manifesto foi encontrado."
            )

        found_by_hash, processed_backups, backup_errors = _search_backups(
            candidates,
            missing_hashes,
            safe_remote,
            work,
        )
    else:
        candidates = []

    database_sha256_after = calculate_sha256(database)
    if database_sha256_after != database_sha256_before:
        raise RuntimeError(
            "O SHA-256 do SQLite mudou durante o planejamento; resultado recusado."
        )

    plan_rows = _build_plan_rows(missing_raws, found_by_hash)
    plan_path = output / "plano-recuperacao-raws.csv"
    _write_csv(
        plan_path,
        (
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
        ),
        plan_rows,
    )

    found_records = sum(1 for row in plan_rows if row[5] == "encontrado")
    invalid_targets = sum(1 for row in plan_rows if row[5] == "destino_invalido")
    not_found_records = len(plan_rows) - found_records - invalid_targets
    found_hashes = set(found_by_hash)
    finished_at = execution_now()
    status = "completed_with_backup_errors" if backup_errors else "completed"
    result: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "operation": "raw_recovery_plan",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        "database": {
            "path": database.as_posix(),
            "model": model,
            "sha256_before": database_sha256_before,
            "sha256_after": database_sha256_after,
            "unchanged": True,
        },
        "current_traceability": traceability,
        "recovery_coverage": {
            "missing_records": len(missing_raws),
            "missing_unique_hashes": len(missing_hashes),
            "records_found_in_backups": found_records,
            "unique_hashes_found_in_backups": len(found_hashes),
            "records_not_found": not_found_records,
            "records_with_invalid_target": invalid_targets,
        },
        "backup_inventory": {
            "validated_candidates": len(candidates),
            "processed": len(processed_backups),
            "skipped": len(skipped_backups),
            "errors": len(backup_errors),
        },
        "processed_backups": processed_backups,
        "skipped_backups": skipped_backups,
        "backup_errors": backup_errors,
        "files": {
            "recovery_plan": plan_path.name,
            "classification_directory": "classificacao-atual",
            "report": "resumo.md",
        },
        "safety": _safety_summary(),
    }
    write_json_atomic(output / "resultado.json", result)
    _write_report(output / "resumo.md", result)
    return result, 2 if backup_errors else 0


def _classify_missing_raws(
    database: Path,
    output_directory: Path,
) -> tuple[str, dict[str, object], list[MissingRaw]]:
    is_v4 = uses_v4_schema(database)
    model = "v4" if is_v4 else "legacy"
    output_directory.mkdir()

    database_uri = f"{database.as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        if is_v4:
            registered = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM coletas
                    WHERE caminho_relativo_raw IS NOT NULL
                      AND sha256 IS NOT NULL
                    """
                ).fetchone()[0]
            )
        else:
            registered = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM coletas
                    WHERE arquivo_raw IS NOT NULL
                      AND hash_raw IS NOT NULL
                    """
                ).fetchone()[0]
            )

    sample_limit = max(registered, 1)
    if is_v4:
        auditor: DatabaseAuditor | V4DatabaseAuditor = V4DatabaseAuditor(
            database_path=database,
            output_directory=output_directory,
            sample_limit=sample_limit,
            gap_days=14,
            verify_raw=True,
            full_integrity_check=False,
            skip_semantic=True,
            execution_context={},
            auditor_path=Path(__file__),
        )
    else:
        auditor = DatabaseAuditor(
            database_path=database,
            output_directory=output_directory,
            sample_limit=sample_limit,
            gap_days=14,
            verify_raw=True,
            full_integrity_check=False,
            skip_semantic=True,
        )
        auditor.project_root = database.parent.parent

    with auditor._connect() as connection:
        summary = auditor._audit_raw_files(connection)

    missing_count = int(
        summary["missing_files"] if is_v4 else summary["missing"]
    )
    report_path = output_directory / "raws_ausentes.csv"
    if missing_count == 0:
        return model, _normalize_traceability(summary, is_v4), []
    if not report_path.is_file():
        raise RuntimeError("A classificacao nao gerou raws_ausentes.csv.")

    missing: list[MissingRaw] = []
    with report_path.open(encoding="utf-8", newline="") as report:
        for row in csv.DictReader(report):
            stored_path = str(
                row[
                    "caminho_relativo_raw"
                    if is_v4
                    else "arquivo_registrado"
                ]
            )
            expected_hash = str(
                row["sha256_registrado" if is_v4 else "hash_esperado"]
            ).lower()
            if not SHA256_PATTERN.fullmatch(expected_hash):
                raise RuntimeError(
                    f"Hash invalido na coleta {row['coleta_id']}."
                )
            missing.append(
                MissingRaw(
                    collection_id=int(row["coleta_id"]),
                    source=str(row["fonte"]),
                    stored_path=stored_path,
                    target_path=_target_path(stored_path, is_v4),
                    expected_sha256=expected_hash,
                )
            )

    if len(missing) != missing_count:
        raise RuntimeError(
            "O relatorio integral de ausentes diverge da contagem da auditoria."
        )
    return model, _normalize_traceability(summary, is_v4), missing


def _normalize_traceability(
    summary: dict[str, object],
    is_v4: bool,
) -> dict[str, object]:
    if is_v4:
        return {
            "checked": summary["canonical_raws_checked"],
            "exact_match": summary["exact_matches"],
            "relocated_match": summary["relocated_matches"],
            "ambiguous_candidates": summary["ambiguous_candidates"],
            "missing": summary["missing_files"],
            "hash_mismatch": summary["hash_mismatches"],
            "unreadable": summary["unreadable_files"],
        }
    return {
        key: summary[key]
        for key in (
            "checked",
            "exact_match",
            "relocated_match",
            "ambiguous_candidates",
            "missing",
            "hash_mismatch",
            "missing_hash",
            "unreadable",
        )
    }


def _target_path(stored_path: str, is_v4: bool) -> str | None:
    normalized = stored_path.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return None
    parts = path.parts
    if not is_v4 and parts and parts[0] == "data":
        parts = parts[1:]
    if len(parts) < 3 or parts[0] != "raw":
        return None
    return PurePosixPath(*parts).as_posix()


def _download_manifests(
    remote: str,
    remote_root: PurePosixPath,
    destination: Path,
) -> None:
    audit_path = join_remote_root(remote_root, "backups/audit")
    _run_rclone(
        "copy",
        _remote_spec(remote, audit_path),
        destination.as_posix(),
        "--include",
        "backup-*.json",
    )


def _index_manifests(
    directory: Path,
) -> tuple[dict[str, list[ManifestEvidence]], list[dict[str, object]]]:
    index: dict[str, list[ManifestEvidence]] = {}
    warnings: list[dict[str, object]] = []
    for manifest_path in sorted(directory.glob("backup-*.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("o conteudo nao e um objeto")
            if payload.get("schema_version") != 1:
                raise ValueError("schema_version nao suportado")
            if payload.get("status") not in {"completed", "failed"}:
                continue
            if payload.get("file_name") != manifest_path.name:
                raise ValueError("file_name diverge do arquivo")
            backup_date = date.fromisoformat(str(payload["backup_date"]))
            layers = payload.get("layers")
            if not isinstance(layers, dict):
                raise ValueError("camadas ausentes ou invalidas")
            for layer_name in RECOVERY_LAYERS:
                layer = layers.get(layer_name)
                if not isinstance(layer, dict):
                    continue
                evidence = _manifest_evidence(
                    manifest_path,
                    layer_name,
                    layer,
                    backup_date,
                )
                if evidence is not None:
                    index.setdefault(evidence.managed_remote_path, []).append(
                        evidence
                    )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            warnings.append(
                {
                    "path": manifest_path.name,
                    "reason": f"manifesto_invalido: {sanitize_log_text(error)}",
                }
            )
    return index, warnings


def _manifest_evidence(
    manifest_path: Path,
    layer_name: str,
    layer: dict[str, object],
    backup_date: date,
) -> ManifestEvidence | None:
    package = layer.get("package")
    validation = layer.get("validation")
    upload = layer.get("upload")
    if not all(isinstance(item, dict) for item in (package, validation, upload)):
        return None
    assert isinstance(package, dict)
    assert isinstance(validation, dict)
    assert isinstance(upload, dict)
    if (
        package.get("status") != "completed"
        or validation.get("status") != "passed"
        or upload.get("status") != "completed"
    ):
        return None

    archive_name = str(layer.get("file_name", ""))
    remote_directory = str(layer.get("remote_directory", ""))
    managed_path = validate_layer_remote_path(
        layer_name,
        PurePosixPath(remote_directory) / archive_name,
    )
    if layer_file_date(layer_name, archive_name) != backup_date:
        return None
    final_path = PurePosixPath(str(upload.get("final_path", "")))
    if len(final_path.parts) < len(managed_path.parts) or final_path.parts[
        -len(managed_path.parts) :
    ] != managed_path.parts:
        return None

    sha256 = str(package.get("sha256", "")).lower()
    size_bytes = package.get("size_bytes")
    source = package.get("source")
    if (
        not SHA256_PATTERN.fullmatch(sha256)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or not isinstance(source, dict)
    ):
        return None
    source_files = source.get("files")
    source_bytes = source.get("bytes")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (source_files, source_bytes)
    ):
        return None
    return ManifestEvidence(
        manifest_path=manifest_path,
        manifest_name=manifest_path.name,
        layer=layer_name,
        backup_date=backup_date,
        managed_remote_path=managed_path.as_posix(),
        archive_name=archive_name,
        sha256=sha256,
        size_bytes=size_bytes,
        source_files=source_files,
        source_bytes=source_bytes,
    )


def _list_remote_backups(
    remote: str,
    remote_root: PurePosixPath,
) -> list[dict[str, object]]:
    backups_path = join_remote_root(remote_root, "backups")
    output = _run_rclone(
        "lsjson",
        _remote_spec(remote, backups_path),
        "--recursive",
        "--files-only",
        capture_output=True,
    )
    payload = json.loads(output)
    if not isinstance(payload, list):
        raise RuntimeError("O inventario remoto do rclone nao e uma lista.")
    return [item for item in payload if isinstance(item, dict)]


def _select_candidates(
    inventory: list[dict[str, object]],
    manifest_index: dict[str, list[ManifestEvidence]],
    remote_root: PurePosixPath,
) -> tuple[list[BackupCandidate], list[dict[str, object]]]:
    candidates: list[BackupCandidate] = []
    warnings: list[dict[str, object]] = []
    for item in inventory:
        item_path = str(item.get("Path", "")).replace("\\", "/")
        managed_path = PurePosixPath("backups") / item_path
        layer = _layer_for_path(managed_path)
        if layer is None:
            continue
        try:
            validated_path = validate_layer_remote_path(layer, managed_path)
        except ValueError as error:
            warnings.append(
                {"path": managed_path.as_posix(), "reason": sanitize_log_text(error)}
            )
            continue

        evidences = manifest_index.get(validated_path.as_posix(), [])
        if not evidences:
            warnings.append(
                {
                    "path": validated_path.as_posix(),
                    "reason": "sem_manifesto_validado",
                }
            )
            continue
        signatures = {(item.sha256, item.size_bytes) for item in evidences}
        if len(signatures) != 1:
            warnings.append(
                {
                    "path": validated_path.as_posix(),
                    "reason": "manifestos_conflitantes",
                }
            )
            continue
        evidence = sorted(evidences, key=lambda item: item.manifest_name)[-1]
        remote_size = item.get("Size")
        if remote_size != evidence.size_bytes:
            warnings.append(
                {
                    "path": validated_path.as_posix(),
                    "reason": "tamanho_remoto_diverge_manifesto",
                }
            )
            continue
        candidates.append(
            BackupCandidate(
                evidence=evidence,
                remote_path=join_remote_root(
                    remote_root,
                    validated_path,
                ).as_posix(),
            )
        )

    candidates.sort(
        key=lambda item: (
            item.evidence.backup_date,
            LAYER_PRIORITY[item.evidence.layer],
            item.remote_path,
        )
    )
    deduplicated: list[BackupCandidate] = []
    seen_snapshots: set[tuple[str, int, int]] = set()
    for candidate in candidates:
        evidence = candidate.evidence
        snapshot = (
            evidence.manifest_name,
            evidence.source_files,
            evidence.source_bytes,
        )
        if snapshot in seen_snapshots:
            warnings.append(
                {
                    "path": evidence.managed_remote_path,
                    "reason": "mesma_arvore_ja_representada_por_outra_camada",
                }
            )
            continue
        seen_snapshots.add(snapshot)
        deduplicated.append(candidate)
    return deduplicated, warnings


def _layer_for_path(path: PurePosixPath) -> str | None:
    for layer, directory in LAYER_DIRECTORIES.items():
        if path.parent == directory:
            return layer
    return None


def _search_backups(
    candidates: list[BackupCandidate],
    expected_hashes: set[str],
    remote: str,
    work_directory: Path,
) -> tuple[
    dict[str, RawEvidence],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    downloads = work_directory / "downloads"
    restores = work_directory / "restores"
    downloads.mkdir()
    restores.mkdir()
    pending = set(expected_hashes)
    found: dict[str, RawEvidence] = {}
    processed: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    for position, candidate in enumerate(candidates, start=1):
        if not pending:
            break
        evidence = candidate.evidence
        archive_path = downloads / evidence.archive_name
        print(
            "[recuperacao] Backup "
            f"{position}/{len(candidates)}: {candidate.remote_path}",
            flush=True,
        )
        started = time.monotonic()
        found_before = len(found)
        try:
            _run_rclone(
                "copyto",
                _remote_spec(remote, PurePosixPath(candidate.remote_path)),
                archive_path.as_posix(),
                "--progress",
            )
            if archive_path.stat().st_size != evidence.size_bytes:
                raise RuntimeError("Tamanho baixado diverge do manifesto.")
            archive_sha256 = calculate_sha256(archive_path)
            if archive_sha256 != evidence.sha256:
                raise RuntimeError("SHA-256 baixado diverge do manifesto.")

            with tempfile.TemporaryDirectory(
                prefix="backup-",
                dir=restores,
            ) as temporary_root:
                restored_root = Path(temporary_root)
                restore_backup(
                    RestoreRequest(
                        archive_path=archive_path,
                        project_root=restored_root,
                        manifest_path=evidence.manifest_path,
                    )
                )
                raw_directory = restored_root / "data" / "raw"
                if raw_directory.is_dir():
                    _scan_raw_directory(
                        raw_directory,
                        restored_root / "data",
                        pending,
                        found,
                        candidate,
                    )
            processed.append(
                {
                    "path": candidate.remote_path,
                    "layer": evidence.layer,
                    "backup_date": evidence.backup_date.isoformat(),
                    "sha256": evidence.sha256,
                    "manifest": evidence.manifest_name,
                    "new_unique_hashes_found": len(found) - found_before,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "status": "completed",
                }
            )
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
            errors.append(
                {
                    "path": candidate.remote_path,
                    "layer": evidence.layer,
                    "backup_date": evidence.backup_date.isoformat(),
                    "manifest": evidence.manifest_name,
                    "error": sanitize_log_text(error),
                }
            )
        finally:
            archive_path.unlink(missing_ok=True)

    return found, processed, errors


def _scan_raw_directory(
    raw_directory: Path,
    data_directory: Path,
    pending: set[str],
    found: dict[str, RawEvidence],
    candidate: BackupCandidate,
) -> None:
    for path in sorted(raw_directory.rglob("*")):
        if not pending:
            return
        if path.is_symlink():
            raise RuntimeError(
                f"Backup contem link simbolico em raw: {path}."
            )
        if not path.is_file():
            continue
        if path.suffix.lower() == ".zip":
            _scan_raw_zip(path, data_directory, pending, found, candidate)
            continue
        digest = _hash_file(path)
        if digest in pending:
            _register_evidence(
                digest=digest,
                location=(
                    "data/" + path.relative_to(data_directory).as_posix()
                ),
                kind="file",
                size=path.stat().st_size,
                pending=pending,
                found=found,
                candidate=candidate,
            )


def _scan_raw_zip(
    archive_path: Path,
    data_directory: Path,
    pending: set[str],
    found: dict[str, RawEvidence],
    candidate: BackupCandidate,
) -> None:
    archive_location = "data/" + archive_path.relative_to(data_directory).as_posix()
    with zipfile.ZipFile(archive_path) as archive:
        for member in sorted(archive.infolist(), key=lambda item: item.filename):
            if not pending:
                return
            if member.is_dir():
                continue
            with archive.open(member) as source:
                digest = _hash_stream(source)
            if digest in pending:
                _register_evidence(
                    digest=digest,
                    location=f"{archive_location}::{member.filename}",
                    kind="zip_member",
                    size=member.file_size,
                    pending=pending,
                    found=found,
                    candidate=candidate,
                )


def _register_evidence(
    *,
    digest: str,
    location: str,
    kind: str,
    size: int,
    pending: set[str],
    found: dict[str, RawEvidence],
    candidate: BackupCandidate,
) -> None:
    evidence = candidate.evidence
    found[digest] = RawEvidence(
        expected_sha256=digest,
        backup_remote_path=candidate.remote_path,
        backup_sha256=evidence.sha256,
        backup_date=evidence.backup_date.isoformat(),
        manifest_name=evidence.manifest_name,
        content_location=location,
        content_kind=kind,
        content_size=size,
    )
    pending.remove(digest)


def _hash_file(path: Path) -> str:
    with path.open("rb") as source:
        return _hash_stream(source)


def _hash_stream(source: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _build_plan_rows(
    missing_raws: list[MissingRaw],
    found_by_hash: dict[str, RawEvidence],
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for item in missing_raws:
        evidence = found_by_hash.get(item.expected_sha256)
        if item.target_path is None:
            status = "destino_invalido"
            action = "revisao_manual"
        elif evidence is None:
            status = "nao_encontrado"
            action = "nenhuma"
        else:
            status = "encontrado"
            action = "copiar_sem_sobrescrever"
        rows.append(
            (
                item.collection_id,
                item.source,
                item.stored_path,
                item.target_path,
                item.expected_sha256,
                status,
                action,
                evidence.backup_remote_path if evidence else None,
                evidence.backup_sha256 if evidence else None,
                evidence.backup_date if evidence else None,
                evidence.manifest_name if evidence else None,
                evidence.content_location if evidence else None,
                evidence.content_kind if evidence else None,
                evidence.content_size if evidence else None,
            )
        )
    return rows


def _write_csv(
    path: Path,
    headers: Iterable[str],
    rows: Iterable[Iterable[object]],
) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(headers)
            writer.writerows(rows)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _write_report(path: Path, result: dict[str, object]) -> None:
    database = result["database"]
    coverage = result["recovery_coverage"]
    inventory = result["backup_inventory"]
    assert isinstance(database, dict)
    assert isinstance(coverage, dict)
    assert isinstance(inventory, dict)
    lines = [
        "# Plano de recuperacao dos arquivos raw",
        "",
        f"- Status: **{result['status']}**",
        "- Operacao remota: **somente leitura**",
        "- Arquivos recuperados nesta etapa: **0**",
        "- SQLite alterado: **nao**",
        f"- SHA-256 do SQLite: `{database['sha256_before']}`",
        "",
        "## Cobertura",
        "",
        "| Item | Quantidade |",
        "| --- | ---: |",
        f"| Raws ausentes | {coverage['missing_records']:,} |",
        f"| Hashes ausentes unicos | {coverage['missing_unique_hashes']:,} |",
        f"| Registros encontrados nos backups | {coverage['records_found_in_backups']:,} |",
        f"| Hashes unicos encontrados | {coverage['unique_hashes_found_in_backups']:,} |",
        f"| Registros ainda nao encontrados | {coverage['records_not_found']:,} |",
        f"| Destinos invalidos | {coverage['records_with_invalid_target']:,} |",
        "",
        "## Backups",
        "",
        "| Item | Quantidade |",
        "| --- | ---: |",
        f"| Candidatos validados por manifesto | {inventory['validated_candidates']:,} |",
        f"| Backups processados | {inventory['processed']:,} |",
        f"| Backups ignorados | {inventory['skipped']:,} |",
        f"| Falhas de processamento | {inventory['errors']:,} |",
        "",
        "## Proxima etapa",
        "",
        "O arquivo `plano-recuperacao-raws.csv` deve ser revisado antes de criar uma copia candidata. Este workflow nao copia raws, nao substitui o SQLite e nao publica pacotes.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_failure_report(path: Path, result: dict[str, object]) -> None:
    path.write_text(
        "\n".join(
            [
                "# Plano de recuperacao dos arquivos raw",
                "",
                "- Status: **failed**",
                f"- Erro: {result['error']}",
                "- Arquivos recuperados nesta etapa: **0**",
                "- Publicacoes realizadas: **0**",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _safety_summary() -> dict[str, object]:
    return {
        "remote_access": "read_only",
        "allowed_rclone_operations": ["lsjson", "copy", "copyto_download"],
        "sqlite_access": "read_only",
        "raw_files_written": 0,
        "database_replaced": False,
        "package_published": False,
        "remote_files_modified": 0,
    }


def _run_rclone(
    *arguments: str,
    capture_output: bool = False,
) -> str:
    if not arguments or arguments[0] not in {"copy", "copyto", "lsjson"}:
        raise RuntimeError("Operacao rclone nao autorizada pelo planejador.")
    completed = subprocess.run(
        ("rclone", *arguments),
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() if completed.stderr else "sem detalhe"
        raise RuntimeError(f"Falha no rclone: {sanitize_log_text(detail)}")
    return completed.stdout if capture_output and completed.stdout else ""


def _remote_spec(remote: str, path: PurePosixPath) -> str:
    return f"{remote}:{path.as_posix()}"


if __name__ == "__main__":
    raise SystemExit(main())
