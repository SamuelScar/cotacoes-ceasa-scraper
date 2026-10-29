#!/usr/bin/env python3
"""Planeja a recuperacao de raws ausentes sem alterar dados ou backups."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
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
    if len(sys.argv) > 1 and sys.argv[1] in {
        "prepare",
        "process",
        "consolidate",
    }:
        return _run_phase(sys.argv[1], sys.argv[2:])

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


def _run_phase(phase: str, arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Executa uma fase do planejamento paralelo dos raws."
    )
    if phase == "prepare":
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
    elif phase == "process":
        parser.add_argument("--context", type=Path, required=True)
        parser.add_argument("--candidate-position", type=int, required=True)
        parser.add_argument("--output", type=Path, required=True)
        parser.add_argument("--work", type=Path, required=True)
        parser.add_argument(
            "--remote",
            default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
        )
    else:
        parser.add_argument("--context", type=Path, required=True)
        parser.add_argument("--evidence", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(arguments)
    try:
        if phase == "prepare":
            result, exit_code = prepare_parallel_context(
                database_path=args.database,
                output_directory=args.output,
                work_directory=args.work,
                remote=args.remote,
                remote_root=args.root,
            )
        elif phase == "process":
            result, exit_code = process_parallel_candidate(
                context_path=args.context,
                candidate_position=args.candidate_position,
                output_directory=args.output,
                work_directory=args.work,
                remote=args.remote,
            )
        else:
            result, exit_code = consolidate_parallel_results(
                context_path=args.context,
                evidence_directory=args.evidence,
                output_directory=args.output,
            )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "operation": f"raw_recovery_{phase}",
            "error": sanitize_log_text(error),
            "safety": _safety_summary(),
        }
        output_directory = args.output.resolve()
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


def prepare_parallel_context(
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
    missing_hashes = sorted(
        {
            item.expected_sha256
            for item in missing_raws
            if item.target_path is not None
        }
    )

    candidates: list[BackupCandidate] = []
    skipped_backups: list[dict[str, object]] = []
    manifests_directory = work / "manifests"
    manifests_directory.mkdir()
    if missing_hashes:
        print("[recuperacao] Baixando manifestos de auditoria.", flush=True)
        _download_manifests(safe_remote, safe_root, manifests_directory)
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

    database_sha256_after = calculate_sha256(database)
    if database_sha256_after != database_sha256_before:
        raise RuntimeError(
            "O SHA-256 do SQLite mudou durante a preparacao; contexto recusado."
        )

    context_manifests = output / "manifests"
    context_manifests.mkdir()
    candidate_payloads: list[dict[str, object]] = []
    for position, candidate in enumerate(candidates, start=1):
        source_manifest = candidate.evidence.manifest_path
        destination_manifest = context_manifests / source_manifest.name
        if destination_manifest.exists():
            if calculate_sha256(destination_manifest) != calculate_sha256(
                source_manifest
            ):
                raise RuntimeError(
                    f"Manifestos com mesmo nome divergem: {source_manifest.name}."
                )
        else:
            shutil.copy2(source_manifest, destination_manifest)
        candidate_payloads.append(
            _candidate_to_payload(
                candidate,
                position=position,
                manifest_path=PurePosixPath("manifests") / source_manifest.name,
                manifest_sha256=calculate_sha256(destination_manifest),
            )
        )

    context = {
        "schema_version": 1,
        "status": "completed",
        "operation": "raw_recovery_context",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": execution_now().isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started_monotonic, 3),
        "database": {
            "path": database.as_posix(),
            "model": model,
            "sha256_before": database_sha256_before,
            "sha256_after": database_sha256_after,
            "unchanged": True,
        },
        "current_traceability": traceability,
        "missing_raws": [_missing_raw_to_payload(item) for item in missing_raws],
        "missing_hashes": missing_hashes,
        "candidates": candidate_payloads,
        "skipped_backups": skipped_backups,
        "safety": _safety_summary(),
    }
    context_path = output / "contexto.json"
    write_json_atomic(context_path, context)
    matrix_positions = [
        {"position": position}
        for position in range(1, len(candidates) + 1)
    ]
    if not matrix_positions:
        matrix_positions.append({"position": 0})
    write_json_atomic(output / "matriz.json", {"include": matrix_positions})

    result = {
        "schema_version": 1,
        "status": "completed",
        "operation": "raw_recovery_prepare",
        "missing_records": len(missing_raws),
        "missing_unique_hashes": len(missing_hashes),
        "validated_candidates": len(candidates),
        "skipped_backups": len(skipped_backups),
        "database_unchanged": True,
        "files": {
            "context": context_path.name,
            "matrix": "matriz.json",
            "classification_directory": "classificacao-atual",
            "manifests_directory": "manifests",
        },
        "safety": _safety_summary(),
    }
    write_json_atomic(output / "resultado-preparacao.json", result)
    return result, 0


def process_parallel_candidate(
    *,
    context_path: Path,
    candidate_position: int,
    output_directory: Path,
    work_directory: Path,
    remote: str,
) -> tuple[dict[str, object], int]:
    context_file = context_path.resolve(strict=True)
    context = _load_parallel_context(context_file)
    context_sha256 = calculate_sha256(context_file)
    candidates = context["candidates"]
    assert isinstance(candidates, list)

    output = output_directory.resolve()
    if output.exists():
        raise RuntimeError(f"O diretorio de saida ja existe: {output}.")
    output.mkdir(parents=True)
    work = work_directory.resolve()
    if work.exists():
        raise RuntimeError(f"O diretorio de trabalho ja existe: {work}.")
    work.mkdir(parents=True)

    if candidate_position == 0:
        if candidates:
            raise RuntimeError(
                "A posicao sentinela so e valida quando nao ha candidatos."
            )
        result = {
            "schema_version": 1,
            "status": "skipped_no_candidates",
            "operation": "raw_recovery_candidate",
            "context_sha256": context_sha256,
            "candidate_position": 0,
            "candidate_count": 0,
            "found": [],
            "processed_backups": [],
            "backup_errors": [],
            "safety": _safety_summary(),
        }
        write_json_atomic(output / "evidencia-0000.json", result)
        return result, 0

    if candidate_position < 1 or candidate_position > len(candidates):
        raise RuntimeError(
            f"Posicao de candidato invalida: {candidate_position}."
        )
    candidate_payload = candidates[candidate_position - 1]
    if not isinstance(candidate_payload, dict):
        raise RuntimeError("Candidato invalido no contexto.")
    if candidate_payload.get("position") != candidate_position:
        raise RuntimeError("A posicao do candidato diverge do contexto.")

    candidate = _candidate_from_payload(
        candidate_payload,
        context_directory=context_file.parent,
    )
    expected_hashes_payload = context["missing_hashes"]
    assert isinstance(expected_hashes_payload, list)
    expected_hashes = {str(item) for item in expected_hashes_payload}
    safe_remote = validate_remote_name(remote)

    found, processed, errors = _search_backups(
        [candidate],
        expected_hashes,
        safe_remote,
        work,
    )
    result = {
        "schema_version": 1,
        "status": "completed_with_backup_errors" if errors else "completed",
        "operation": "raw_recovery_candidate",
        "context_sha256": context_sha256,
        "candidate_position": candidate_position,
        "candidate_count": len(candidates),
        "candidate_remote_path": candidate.remote_path,
        "candidate_sha256": candidate.evidence.sha256,
        "found": [
            _raw_evidence_to_payload(found[digest])
            for digest in sorted(found)
        ],
        "processed_backups": processed,
        "backup_errors": errors,
        "safety": _safety_summary(),
    }
    write_json_atomic(
        output / f"evidencia-{candidate_position:04d}.json",
        result,
    )
    return result, 2 if errors else 0


def consolidate_parallel_results(
    *,
    context_path: Path,
    evidence_directory: Path,
    output_directory: Path,
) -> tuple[dict[str, object], int]:
    context_file = context_path.resolve(strict=True)
    context = _load_parallel_context(context_file)
    context_sha256 = calculate_sha256(context_file)
    candidates = context["candidates"]
    assert isinstance(candidates, list)

    evidence_root = evidence_directory.resolve(strict=True)
    evidence_paths = sorted(evidence_root.rglob("evidencia-*.json"))
    expected_positions = set(range(1, len(candidates) + 1)) or {0}
    evidence_by_position: dict[int, dict[str, object]] = {}
    for evidence_path in evidence_paths:
        payload = _read_json_object(evidence_path)
        if payload.get("schema_version") != 1:
            raise RuntimeError(
                f"Versao invalida na evidencia {evidence_path.name}."
            )
        if payload.get("operation") != "raw_recovery_candidate":
            raise RuntimeError(
                f"Operacao invalida na evidencia {evidence_path.name}."
            )
        if payload.get("context_sha256") != context_sha256:
            raise RuntimeError(
                f"Contexto divergente na evidencia {evidence_path.name}."
            )
        position = payload.get("candidate_position")
        if isinstance(position, bool) or not isinstance(position, int):
            raise RuntimeError(
                f"Posicao invalida na evidencia {evidence_path.name}."
            )
        if position in evidence_by_position:
            raise RuntimeError(f"Evidencia duplicada para a posicao {position}.")
        evidence_by_position[position] = payload

    received_positions = set(evidence_by_position)
    if received_positions != expected_positions:
        missing = sorted(expected_positions - received_positions)
        unexpected = sorted(received_positions - expected_positions)
        raise RuntimeError(
            "Conjunto de evidencias incompleto ou inesperado: "
            f"ausentes={missing}, inesperadas={unexpected}."
        )

    found_by_hash: dict[str, RawEvidence] = {}
    processed_backups: list[dict[str, object]] = []
    backup_errors: list[dict[str, object]] = []
    for position in sorted(evidence_by_position):
        payload = evidence_by_position[position]
        if position == 0:
            if payload.get("status") != "skipped_no_candidates":
                raise RuntimeError("A evidencia sentinela possui status invalido.")
            continue

        candidate_payload = candidates[position - 1]
        if not isinstance(candidate_payload, dict):
            raise RuntimeError("Candidato invalido no contexto.")
        if (
            payload.get("candidate_remote_path")
            != candidate_payload.get("remote_path")
            or payload.get("candidate_sha256")
            != candidate_payload.get("sha256")
        ):
            raise RuntimeError(
                f"A evidencia da posicao {position} diverge do candidato."
            )

        processed = payload.get("processed_backups")
        errors = payload.get("backup_errors")
        found = payload.get("found")
        if not all(isinstance(item, list) for item in (processed, errors, found)):
            raise RuntimeError(f"Evidencia invalida na posicao {position}.")
        assert isinstance(processed, list)
        assert isinstance(errors, list)
        assert isinstance(found, list)
        if len(processed) + len(errors) != 1:
            raise RuntimeError(
                f"A posicao {position} nao possui resultado unico de backup."
            )
        processed_backups.extend(processed)
        backup_errors.extend(errors)
        for raw_payload in found:
            if not isinstance(raw_payload, dict):
                raise RuntimeError(
                    f"Raw invalido na evidencia da posicao {position}."
                )
            raw_evidence = _raw_evidence_from_payload(raw_payload)
            found_by_hash.setdefault(
                raw_evidence.expected_sha256,
                raw_evidence,
            )

    missing_hashes_payload = context["missing_hashes"]
    assert isinstance(missing_hashes_payload, list)
    expected_hashes = {str(item) for item in missing_hashes_payload}
    unexpected_hashes = set(found_by_hash) - expected_hashes
    if unexpected_hashes:
        raise RuntimeError("As evidencias contem hashes nao solicitados.")

    missing_raws_payload = context["missing_raws"]
    assert isinstance(missing_raws_payload, list)
    missing_raws = [
        _missing_raw_from_payload(item)
        for item in missing_raws_payload
        if isinstance(item, dict)
    ]
    if len(missing_raws) != len(missing_raws_payload):
        raise RuntimeError("O contexto contem raws ausentes invalidos.")

    output = output_directory.resolve()
    if output.exists():
        raise RuntimeError(f"O diretorio de saida ja existe: {output}.")
    output.mkdir(parents=True)
    classification_source = context_file.parent / "classificacao-atual"
    if not classification_source.is_dir():
        raise RuntimeError("A classificacao atual nao acompanha o contexto.")
    shutil.copytree(
        classification_source,
        output / "classificacao-atual",
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
    invalid_targets = sum(
        1 for row in plan_rows if row[5] == "destino_invalido"
    )
    not_found_records = len(plan_rows) - found_records - invalid_targets
    database = context["database"]
    traceability = context["current_traceability"]
    skipped_backups = context["skipped_backups"]
    assert isinstance(database, dict)
    assert isinstance(traceability, dict)
    assert isinstance(skipped_backups, list)
    status = "completed_with_backup_errors" if backup_errors else "completed"
    result: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "operation": "raw_recovery_plan",
        "started_at": context["started_at"],
        "finished_at": execution_now().isoformat(timespec="seconds"),
        "duration_seconds": round(
            float(context["duration_seconds"])
            + sum(
                float(item.get("duration_seconds", 0))
                for item in processed_backups
                if isinstance(item, dict)
            ),
            3,
        ),
        "database": database,
        "current_traceability": traceability,
        "recovery_coverage": {
            "missing_records": len(missing_raws),
            "missing_unique_hashes": len(expected_hashes),
            "records_found_in_backups": found_records,
            "unique_hashes_found_in_backups": len(found_by_hash),
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
        "parallel_execution": {
            "candidate_jobs": len(candidates),
            "evidences_received": len(evidence_by_position),
            "context_sha256": context_sha256,
        },
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


def _candidate_to_payload(
    candidate: BackupCandidate,
    *,
    position: int,
    manifest_path: PurePosixPath,
    manifest_sha256: str,
) -> dict[str, object]:
    evidence = candidate.evidence
    return {
        "position": position,
        "remote_path": candidate.remote_path,
        "manifest_path": manifest_path.as_posix(),
        "manifest_sha256": manifest_sha256,
        "manifest_name": evidence.manifest_name,
        "layer": evidence.layer,
        "backup_date": evidence.backup_date.isoformat(),
        "managed_remote_path": evidence.managed_remote_path,
        "archive_name": evidence.archive_name,
        "sha256": evidence.sha256,
        "size_bytes": evidence.size_bytes,
        "source_files": evidence.source_files,
        "source_bytes": evidence.source_bytes,
    }


def _candidate_from_payload(
    payload: dict[str, object],
    *,
    context_directory: Path,
) -> BackupCandidate:
    relative_manifest = PurePosixPath(str(payload.get("manifest_path", "")))
    if (
        relative_manifest.is_absolute()
        or ".." in relative_manifest.parts
        or relative_manifest.parent != PurePosixPath("manifests")
    ):
        raise RuntimeError("Caminho de manifesto invalido no contexto.")
    manifest_path = context_directory / relative_manifest.as_posix()
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RuntimeError(f"Manifesto ausente no contexto: {relative_manifest}.")
    manifest_sha256 = str(payload.get("manifest_sha256", ""))
    if (
        not SHA256_PATTERN.fullmatch(manifest_sha256)
        or calculate_sha256(manifest_path) != manifest_sha256
    ):
        raise RuntimeError(f"Manifesto adulterado no contexto: {relative_manifest}.")

    layer = str(payload.get("layer", ""))
    if layer not in RECOVERY_LAYERS:
        raise RuntimeError("Camada de backup invalida no contexto.")
    backup_date = date.fromisoformat(str(payload.get("backup_date", "")))
    managed_remote_path = validate_layer_remote_path(
        layer,
        PurePosixPath(str(payload.get("managed_remote_path", ""))),
    ).as_posix()
    sha256 = str(payload.get("sha256", "")).lower()
    if not SHA256_PATTERN.fullmatch(sha256):
        raise RuntimeError("SHA-256 de backup invalido no contexto.")

    numeric_fields: dict[str, int] = {}
    for key in ("size_bytes", "source_files", "source_bytes"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"Campo numerico invalido no contexto: {key}.")
        numeric_fields[key] = value

    remote_path = str(payload.get("remote_path", ""))
    if not remote_path or PurePosixPath(remote_path).name != str(
        payload.get("archive_name", "")
    ):
        raise RuntimeError("Caminho remoto invalido no contexto.")
    return BackupCandidate(
        evidence=ManifestEvidence(
            manifest_path=manifest_path,
            manifest_name=str(payload.get("manifest_name", "")),
            layer=layer,
            backup_date=backup_date,
            managed_remote_path=managed_remote_path,
            archive_name=str(payload.get("archive_name", "")),
            sha256=sha256,
            size_bytes=numeric_fields["size_bytes"],
            source_files=numeric_fields["source_files"],
            source_bytes=numeric_fields["source_bytes"],
        ),
        remote_path=remote_path,
    )


def _missing_raw_to_payload(item: MissingRaw) -> dict[str, object]:
    return {
        "collection_id": item.collection_id,
        "source": item.source,
        "stored_path": item.stored_path,
        "target_path": item.target_path,
        "expected_sha256": item.expected_sha256,
    }


def _missing_raw_from_payload(payload: dict[str, object]) -> MissingRaw:
    collection_id = payload.get("collection_id")
    if isinstance(collection_id, bool) or not isinstance(collection_id, int):
        raise RuntimeError("ID de coleta invalido no contexto.")
    expected_sha256 = str(payload.get("expected_sha256", "")).lower()
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise RuntimeError("Hash de raw invalido no contexto.")
    target_value = payload.get("target_path")
    if target_value is not None and not isinstance(target_value, str):
        raise RuntimeError("Destino de raw invalido no contexto.")
    return MissingRaw(
        collection_id=collection_id,
        source=str(payload.get("source", "")),
        stored_path=str(payload.get("stored_path", "")),
        target_path=target_value,
        expected_sha256=expected_sha256,
    )


def _raw_evidence_to_payload(item: RawEvidence) -> dict[str, object]:
    return {
        "expected_sha256": item.expected_sha256,
        "backup_remote_path": item.backup_remote_path,
        "backup_sha256": item.backup_sha256,
        "backup_date": item.backup_date,
        "manifest_name": item.manifest_name,
        "content_location": item.content_location,
        "content_kind": item.content_kind,
        "content_size": item.content_size,
    }


def _raw_evidence_from_payload(payload: dict[str, object]) -> RawEvidence:
    expected_sha256 = str(payload.get("expected_sha256", "")).lower()
    backup_sha256 = str(payload.get("backup_sha256", "")).lower()
    if not SHA256_PATTERN.fullmatch(expected_sha256) or not SHA256_PATTERN.fullmatch(
        backup_sha256
    ):
        raise RuntimeError("Hash invalido em evidencia de raw.")
    content_size = payload.get("content_size")
    if (
        isinstance(content_size, bool)
        or not isinstance(content_size, int)
        or content_size < 0
    ):
        raise RuntimeError("Tamanho invalido em evidencia de raw.")
    return RawEvidence(
        expected_sha256=expected_sha256,
        backup_remote_path=str(payload.get("backup_remote_path", "")),
        backup_sha256=backup_sha256,
        backup_date=date.fromisoformat(
            str(payload.get("backup_date", ""))
        ).isoformat(),
        manifest_name=str(payload.get("manifest_name", "")),
        content_location=str(payload.get("content_location", "")),
        content_kind=str(payload.get("content_kind", "")),
        content_size=content_size,
    )


def _load_parallel_context(path: Path) -> dict[str, object]:
    context = _read_json_object(path)
    if context.get("schema_version") != 1:
        raise RuntimeError("Versao invalida do contexto paralelo.")
    if context.get("status") != "completed":
        raise RuntimeError("O contexto paralelo nao esta concluido.")
    if context.get("operation") != "raw_recovery_context":
        raise RuntimeError("Operacao invalida no contexto paralelo.")
    for key in (
        "database",
        "current_traceability",
        "missing_raws",
        "missing_hashes",
        "candidates",
        "skipped_backups",
    ):
        if key not in context:
            raise RuntimeError(f"Campo ausente no contexto paralelo: {key}.")
    if not isinstance(context["missing_raws"], list):
        raise RuntimeError("Lista de raws invalida no contexto paralelo.")
    if not isinstance(context["missing_hashes"], list):
        raise RuntimeError("Lista de hashes invalida no contexto paralelo.")
    if not isinstance(context["candidates"], list):
        raise RuntimeError("Lista de candidatos invalida no contexto paralelo.")
    if not isinstance(context["skipped_backups"], list):
        raise RuntimeError("Lista de ignorados invalida no contexto paralelo.")
    for digest in context["missing_hashes"]:
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise RuntimeError("Hash invalido no contexto paralelo.")
    return context


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"JSON invalido em {path.name}.") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"O arquivo {path.name} nao contem um objeto JSON.")
    return payload


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
    inventory: list[dict[str, object]] = []
    backups_root = PurePosixPath("backups")
    for layer in RECOVERY_LAYERS:
        managed_directory = LAYER_DIRECTORIES[layer]
        remote_directory = join_remote_root(remote_root, managed_directory)
        output = _run_rclone(
            "lsjson",
            _remote_spec(remote, remote_directory),
            "--files-only",
            capture_output=True,
        )
        payload = json.loads(output)
        if not isinstance(payload, list):
            raise RuntimeError(
                f"O inventario remoto da camada {layer} nao e uma lista."
            )

        relative_directory = managed_directory.relative_to(backups_root)
        for item in payload:
            if not isinstance(item, dict):
                continue
            normalized_item = dict(item)
            item_path = str(item.get("Path", "")).replace("\\", "/")
            normalized_item["Path"] = (
                relative_directory / PurePosixPath(item_path)
            ).as_posix()
            inventory.append(normalized_item)
    return inventory


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
