"""Consolida planejamento, recuperacao candidata e auditoria integral."""

from __future__ import annotations

import json
from pathlib import Path

from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.execution import (
    execution_now,
    write_json_atomic,
    write_text_atomic,
)


class RawRecoveryReportError(RuntimeError):
    """Indica divergencia entre as evidencias da recuperacao."""


def consolidate_raw_recovery(
    *,
    planning_result_path: Path,
    recovery_result_path: Path,
    audit_result_path: Path,
    result_path: Path,
    report_path: Path,
) -> dict[str, object]:
    """Valida as tres etapas e grava o resultado final da candidata."""
    planning_file = _resolve_file(
        planning_result_path,
        "resultado do planejamento",
    )
    recovery_file = _resolve_file(
        recovery_result_path,
        "resultado da recuperacao",
    )
    audit_file = _resolve_file(audit_result_path, "resultado da auditoria")
    planning = _read_json(planning_file)
    recovery = _read_json(recovery_file)
    audit = _read_json(audit_file)
    _validate_statuses(planning, recovery, audit)

    coverage = _object(planning, "recovery_coverage", "planejamento")
    before = _traceability(
        _object(planning, "current_traceability", "planejamento")
    )
    recovery_plan = _object(recovery, "plan", "recuperacao")
    recovery_counts = _object(recovery, "recovery", "recuperacao")
    source = _object(recovery, "source", "recuperacao")
    candidate = _object(recovery, "candidate", "recuperacao")
    after = _traceability(
        _object(audit, "raw_traceability", "auditoria")
    )
    verdict = _object(audit, "verdict", "auditoria")
    audit_parameters = _object(audit, "parameters", "auditoria")
    audit_safety = _object(audit, "safety", "auditoria")

    _require_equal_counts(
        coverage,
        recovery_plan,
        (
            ("records_found_in_backups", "recoverable_records"),
            ("unique_hashes_found_in_backups", "recoverable_unique_hashes"),
            ("records_not_found", "not_found_records"),
            ("records_with_invalid_target", "invalid_target_records"),
        ),
    )
    recoverable_records = _integer(coverage, "records_found_in_backups")
    recovered_records = _integer(recovery_counts, "records_recovered")
    ignored_records = _integer(recovery_counts, "records_already_present")
    if recovered_records + ignored_records != recoverable_records:
        raise RawRecoveryReportError(
            "A recuperacao nao atendeu todos os registros recuperaveis."
        )
    recovered_operations = _integer(
        recovery_counts,
        "operations_recovered",
    )
    ignored_operations = _integer(
        recovery_counts,
        "operations_already_present",
    )
    target_operations = _integer(recovery_counts, "target_operations")
    if recovered_operations + ignored_operations != target_operations:
        raise RawRecoveryReportError(
            "A recuperacao nao concluiu todos os destinos planejados."
        )

    if (
        source.get("database_unchanged") is not True
        or source.get("tree_unchanged") is not True
        or source.get("database_sha256_before")
        != source.get("database_sha256_after")
        or source.get("inventory_before") != source.get("inventory_after")
    ):
        raise RawRecoveryReportError("O pacote atual mudou durante a recuperacao.")
    if (
        candidate.get("database_unchanged") is not True
        or candidate.get("database_sha256_before")
        != candidate.get("database_sha256_after")
    ):
        raise RawRecoveryReportError("O SQLite candidato foi alterado.")
    planning_database = _object(planning, "database", "planejamento")
    planning_database_sha256 = planning_database.get("sha256_before")
    source_database_sha256 = source.get("database_sha256_after")
    if source.get("planning_database_sha256") != planning_database_sha256:
        raise RawRecoveryReportError(
            "A recuperacao referencia outro SQLite de planejamento."
        )
    if candidate.get("database_sha256_after") != source_database_sha256:
        raise RawRecoveryReportError(
            "O SQLite candidato diverge do pacote atual validado."
        )
    exact_planning_snapshot = source.get("exact_planning_snapshot")
    if not isinstance(exact_planning_snapshot, bool) or (
        exact_planning_snapshot
        != (source_database_sha256 == planning_database_sha256)
    ):
        raise RawRecoveryReportError(
            "A compatibilidade entre os SQLite possui evidencia invalida."
        )
    planned_collections = _integer(coverage, "missing_records")
    if _integer(source, "plan_collections_validated") != planned_collections:
        raise RawRecoveryReportError(
            "Nem todas as coletas do plano foram validadas no SQLite atual."
        )

    if (
        audit_parameters.get("verify_raw") is not True
        or audit_parameters.get("full_integrity_check") is not True
    ):
        raise RawRecoveryReportError("A auditoria integral nao foi executada.")
    if (
        audit_safety.get("audit_only") is not True
        or audit_safety.get("database_opened_read_only") is not True
    ):
        raise RawRecoveryReportError(
            "A auditoria nao confirmou o modo somente leitura."
        )

    _validate_traceability(
        before,
        after,
        recoverable_records,
        exact_planning_snapshot=exact_planning_snapshot,
    )
    _validate_audit_findings(audit, after["missing"])
    not_found_records = _integer(coverage, "records_not_found")
    rejected_records = _integer(coverage, "records_with_invalid_target")
    status = (
        "completed_with_expected_unresolved_raws"
        if after["missing"]
        else "completed"
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "operation": "raw_recovery_validation",
        "finished_at": execution_now().isoformat(timespec="seconds"),
        "inputs": {
            "planning_result_sha256": calculate_sha256(planning_file),
            "recovery_result_sha256": calculate_sha256(recovery_file),
            "audit_result_sha256": calculate_sha256(audit_file),
        },
        "records": {
            "recoverable": recoverable_records,
            "recovered": recovered_records,
            "ignored_already_present": ignored_records,
            "not_found_in_backups": not_found_records,
            "rejected_invalid_target": rejected_records,
            "rejected_during_recovery": 0,
        },
        "unique_hashes": {
            "recoverable": _integer(
                coverage,
                "unique_hashes_found_in_backups",
            ),
        },
        "operations": {
            "planned": target_operations,
            "recovered": recovered_operations,
            "ignored_already_present": ignored_operations,
            "rejected": 0,
        },
        "traceability": {
            "before": before,
            "after": after,
            "missing_reduction": before["missing"] - after["missing"],
            "exact_planning_snapshot": exact_planning_snapshot,
            "collections_added_since_planning": max(
                0,
                after["checked"] - before["checked"],
            ),
        },
        "audit": {
            "status": verdict.get("status"),
            "error_findings": verdict.get("error_findings"),
            "expected_remaining_missing": after["missing"],
            "unexpected_error_findings": 0,
        },
        "candidate": {
            "database_sha256": candidate["database_sha256_after"],
            "database_unchanged": True,
            "root": candidate.get("root"),
        },
        "safety": {
            "current_package_modified": False,
            "remote_files_modified": 0,
            "existing_candidate_files_overwritten": 0,
            "package_published": False,
            "manual_approval_required_before_publication": True,
        },
    }
    write_json_atomic(result_path, result)
    write_text_atomic(report_path, _build_report(result))
    return result


def _validate_statuses(
    planning: dict[str, object],
    recovery: dict[str, object],
    audit: dict[str, object],
) -> None:
    expected = (
        (planning, "completed", "raw_recovery_plan", "planejamento"),
        (recovery, "completed", "raw_recovery_candidate", "recuperacao"),
    )
    for payload, status, operation, label in expected:
        if payload.get("status") != status or payload.get("operation") != operation:
            raise RawRecoveryReportError(f"O resultado de {label} e invalido.")
    if audit.get("execution_status") != "completed":
        raise RawRecoveryReportError("A auditoria nao foi concluida.")


def _validate_traceability(
    before: dict[str, int],
    after: dict[str, int],
    recovered_records: int,
    *,
    exact_planning_snapshot: bool,
) -> None:
    if after["checked"] < before["checked"]:
        raise RawRecoveryReportError(
            "A quantidade de raws verificados diminuiu desde o planejamento."
        )
    expected_missing = before["missing"] - recovered_records
    if expected_missing < 0:
        raise RawRecoveryReportError(
            "A reducao de raws ausentes diverge da recuperacao planejada."
        )
    matched_before = before["exact_match"] + before["relocated_match"]
    matched_after = after["exact_match"] + after["relocated_match"]
    if exact_planning_snapshot:
        if after["checked"] != before["checked"]:
            raise RawRecoveryReportError(
                "A quantidade de raws verificados mudou entre as auditorias."
            )
        if after["missing"] != expected_missing:
            raise RawRecoveryReportError(
                "A reducao de raws ausentes diverge da recuperacao planejada."
            )
        if matched_after != matched_before + recovered_records:
            raise RawRecoveryReportError(
                "O aumento de raws localizados diverge da recuperacao."
            )
        for key in (
            "ambiguous_candidates",
            "hash_mismatch",
            "unreadable",
            "missing_hash",
        ):
            if before.get(key, 0) != after.get(key, 0):
                raise RawRecoveryReportError(
                    f"A classificacao {key} mudou inesperadamente."
                )
        return

    added_collections = after["checked"] - before["checked"]
    if after["missing"] > expected_missing:
        raise RawRecoveryReportError(
            "Surgiram raws ausentes que nao estavam cobertos pelo plano."
        )
    if matched_after < matched_before + recovered_records + added_collections:
        raise RawRecoveryReportError(
            "As coletas recuperadas ou adicionadas nao aparecem na auditoria final."
        )


def _validate_audit_findings(
    audit: dict[str, object],
    remaining_missing: int,
) -> None:
    findings = audit.get("findings")
    if not isinstance(findings, list):
        raise RawRecoveryReportError("A auditoria nao possui lista de achados.")
    allowed_missing = 0
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("severity") != "erro":
            continue
        if finding.get("code") == "raw_files_missing":
            allowed_missing += _integer(finding, "occurrences")
            continue
        raise RawRecoveryReportError(
            "A auditoria encontrou erro inesperado: "
            f"{finding.get('code', 'sem_codigo')}."
        )
    expected_missing_error = (
        remaining_missing
        if audit.get("database_model") == "sqlite_v4_issue9"
        else 0
    )
    if allowed_missing != expected_missing_error:
        raise RawRecoveryReportError(
            "O achado de raws ausentes diverge da contagem final."
        )


def _traceability(payload: dict[str, object]) -> dict[str, int]:
    aliases = {
        "checked": ("checked", "canonical_raws_checked"),
        "exact_match": ("exact_match", "exact_matches"),
        "relocated_match": ("relocated_match", "relocated_matches"),
        "ambiguous_candidates": (
            "ambiguous_candidates",
            "ambiguous_candidates",
        ),
        "missing": ("missing", "missing_files"),
        "hash_mismatch": ("hash_mismatch", "hash_mismatches"),
        "unreadable": ("unreadable", "unreadable_files"),
        "missing_hash": ("missing_hash", "missing_hash"),
    }
    normalized: dict[str, int] = {}
    for target, options in aliases.items():
        value = next(
            (payload[name] for name in options if name in payload),
            0 if target == "missing_hash" else None,
        )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RawRecoveryReportError(
                f"Contagem de rastreabilidade invalida: {target}."
            )
        normalized[target] = value
    return normalized


def _require_equal_counts(
    left: dict[str, object],
    right: dict[str, object],
    pairs: tuple[tuple[str, str], ...],
) -> None:
    for left_key, right_key in pairs:
        if _integer(left, left_key) != _integer(right, right_key):
            raise RawRecoveryReportError(
                f"As evidencias divergem entre {left_key} e {right_key}."
            )


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RawRecoveryReportError(f"Contagem invalida: {key}.")
    return value


def _object(
    payload: dict[str, object],
    key: str,
    label: str,
) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RawRecoveryReportError(f"Campo {key} ausente em {label}.")
    return value


def _build_report(result: dict[str, object]) -> str:
    records = _object(result, "records", "resultado")
    operations = _object(result, "operations", "resultado")
    traceability = _object(result, "traceability", "resultado")
    before = _object(traceability, "before", "rastreabilidade")
    after = _object(traceability, "after", "rastreabilidade")
    exact_snapshot = traceability.get("exact_planning_snapshot") is True
    added_collections = traceability.get("collections_added_since_planning", 0)
    return "\n".join(
        [
            "# Validacao da recuperacao dos raws historicos",
            "",
            f"- Status: **{result['status']}**",
            "- Pacote atual alterado: **nao**",
            "- Backups remotos alterados: **0**",
            "- Pacote publicado: **nao**",
            "- SQLite identico ao usado no planejamento: "
            f"**{'sim' if exact_snapshot else 'nao; coletas do plano validadas'}**",
            "- Novas coletas desde o planejamento: "
            f"**{added_collections:,}**",
            "",
            "## Resultado da recuperacao",
            "",
            f"- Registros recuperados: **{records['recovered']:,}**",
            (
                "- Registros ignorados por ja existirem: "
                f"**{records['ignored_already_present']:,}**"
            ),
            (
                "- Registros nao encontrados nos backups: "
                f"**{records['not_found_in_backups']:,}**"
            ),
            (
                "- Registros rejeitados por destino invalido: "
                f"**{records['rejected_invalid_target']:,}**"
            ),
            f"- Destinos recuperados: **{operations['recovered']:,}**",
            "",
            "## Comparacao da auditoria",
            "",
            "| Classificacao | Antes | Depois |",
            "| --- | ---: | ---: |",
            f"| Verificados | {before['checked']:,} | {after['checked']:,} |",
            (
                f"| No caminho exato | {before['exact_match']:,} | "
                f"{after['exact_match']:,} |"
            ),
            (
                f"| Realocados | {before['relocated_match']:,} | "
                f"{after['relocated_match']:,} |"
            ),
            f"| Ausentes | {before['missing']:,} | {after['missing']:,} |",
            (
                f"| Hash divergente | {before['hash_mismatch']:,} | "
                f"{after['hash_mismatch']:,} |"
            ),
            f"| Ilegiveis | {before['unreadable']:,} | {after['unreadable']:,} |",
            "",
            "A candidata nao foi publicada. Qualquer substituicao do pacote "
            "atual exige aprovacao manual posterior.",
            "",
        ]
    )


def _resolve_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise RawRecoveryReportError(
            f"{label.capitalize()} nao e um arquivo regular."
        )
    return resolved


def _read_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RawRecoveryReportError(f"JSON invalido: {path.name}.") from error
    if not isinstance(payload, dict):
        raise RawRecoveryReportError(f"JSON nao e um objeto: {path.name}.")
    return payload
