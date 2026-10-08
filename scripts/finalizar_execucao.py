#!/usr/bin/env python3
"""Consolida os artefatos produzidos pelo workflow do crawler."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if SOURCE_ROOT.as_posix() not in sys.path:
    sys.path.insert(0, SOURCE_ROOT.as_posix())

from cotacoes_ceasa.execution import (  # noqa: E402
    ExecutionContext,
    sanitize_log_text,
    write_json_atomic,
    write_text_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Finaliza os logs do crawler.")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument(
        "--status",
        choices=("completed", "failed", "cancelled"),
        required=True,
    )
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--error")
    parser.add_argument("--defer-finalization", action="store_true")
    args = parser.parse_args()

    context = ExecutionContext(
        execution_id=args.execution_id,
        directory=args.directory,
        externally_managed=True,
        step_name="workflow",
    )
    context.initialize()
    context.record_stage("consolidating", "running")

    publication = build_publication_result(args.directory)
    publication_path = args.directory / "etapas/publicacao.json"
    write_json_atomic(publication_path, publication)
    context.register_file(publication_path)

    backup_result_path = resolve_backup_result_path(args.directory)
    if backup_result_path.is_file():
        context.register_file(backup_result_path)

    result = build_result(args, publication)
    result_path = args.directory / "resultado.json"
    write_json_atomic(result_path, result)
    context.register_file(result_path)

    summary_path = args.directory / "resumo.md"
    write_text_atomic(summary_path, build_summary(args, publication, result))
    context.register_file(summary_path)

    console_path = args.directory / "console.log"
    if console_path.is_file():
        context.register_file(console_path)

    if args.defer_finalization:
        context.record_stage(
            "awaiting-deliveries",
            "running",
            "Resumo preparado; aguardando entregas finais.",
        )
        return 0

    context.finish_step(args.status, args.exit_code, args.error)
    context.finalize(
        args.status,
        args.exit_code,
        args.error,
        required_files=(publication_path, result_path, summary_path, console_path),
    )
    return 0


def build_publication_result(directory: Path) -> dict[str, Any]:
    gate_path = directory / "etapas/gate-publicacao.json"
    gate = load_json(gate_path)
    previous = load_json(directory / "etapas/publicacao.json") or {}
    previous_gate = mapping(previous.get("publication_gate"))
    evaluated_decision = (
        gate.get("status")
        if gate
        else previous_value(previous_gate, "evaluated_decision")
    )
    effective_decision = env_or_previous(
        "COTACOES_RESULT_GATE_DECISION",
        previous_gate,
        "effective_decision",
    )
    bypass_value = os.getenv("COTACOES_RESULT_GATE_BYPASSED", "").strip()
    bypassed = (
        bypass_value.lower() == "true"
        if bypass_value
        else bool(previous_gate.get("bypassed", False))
    )
    backup_result_path = resolve_backup_result_path(directory)
    backup_result = load_json(backup_result_path)
    layered_backup = backup_result
    if isinstance(backup_result, dict) and isinstance(
        backup_result.get("layered_backup"), dict
    ):
        layered_backup = backup_result["layered_backup"]
    return {
        "schema_version": 1,
        "restore": {
            "outcome": env_or_previous(
                "COTACOES_RESULT_RESTORE_OUTCOME", previous, "restore", "outcome"
            ),
            "source": env_or_previous(
                "COTACOES_RESULT_RESTORE_SOURCE", previous, "restore", "source"
            ),
            "asset": env_or_previous(
                "COTACOES_RESULT_RESTORE_ASSET", previous, "restore", "asset"
            ),
            "duration_seconds": env_or_previous(
                "COTACOES_RESULT_RESTORE_DURATION",
                previous,
                "restore",
                "duration_seconds",
            ),
            "data_size": env_or_previous(
                "COTACOES_RESULT_RESTORE_SIZE", previous, "restore", "data_size"
            ),
            "validation_status": env_or_previous(
                "COTACOES_RESULT_RESTORE_VALIDATION",
                previous,
                "restore",
                "validation_status",
            ),
            "checksum_validation": env_or_previous(
                "COTACOES_RESULT_RESTORE_CHECKSUM",
                previous,
                "restore",
                "checksum_validation",
            ),
            "manifest_status": env_or_previous(
                "COTACOES_RESULT_RESTORE_MANIFEST",
                previous,
                "restore",
                "manifest_status",
            ),
        },
        "checkpoint": {
            "outcome": env_or_previous(
                "COTACOES_RESULT_CHECKPOINT_OUTCOME",
                previous,
                "checkpoint",
                "outcome",
            ),
        },
        "publication_gate": {
            "outcome": env_or_previous(
                "COTACOES_RESULT_GATE_OUTCOME",
                previous,
                "publication_gate",
                "outcome",
            ),
            "evaluated_decision": evaluated_decision,
            "effective_decision": effective_decision,
            "bypassed": bypassed,
        },
        "onedrive": {
            "layered_backup": layered_backup,
            "package_outcome": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_PACKAGE_OUTCOME",
                previous,
                "onedrive",
                "package_outcome",
            ),
            "package_duration_seconds": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_PACKAGE_DURATION",
                previous,
                "onedrive",
                "package_duration_seconds",
            ),
            "package_size": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_PACKAGE_SIZE",
                previous,
                "onedrive",
                "package_size",
            ),
            "upload_outcome": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_UPLOAD_OUTCOME",
                previous,
                "onedrive",
                "upload_outcome",
            ),
            "upload_duration_seconds": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_UPLOAD_DURATION",
                previous,
                "onedrive",
                "upload_duration_seconds",
            ),
            "latest_path": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_LATEST_PATH",
                previous,
                "onedrive",
                "latest_path",
            ),
            "history_path": env_or_previous(
                "COTACOES_RESULT_ONEDRIVE_HISTORY_PATH",
                previous,
                "onedrive",
                "history_path",
            ),
        },
        "github_release": {
            "package_outcome": env_or_previous(
                "COTACOES_RESULT_GITHUB_PACKAGE_OUTCOME",
                previous,
                "github_release",
                "package_outcome",
            ),
            "package_duration_seconds": env_or_previous(
                "COTACOES_RESULT_GITHUB_PACKAGE_DURATION",
                previous,
                "github_release",
                "package_duration_seconds",
            ),
            "database_size": env_or_previous(
                "COTACOES_RESULT_GITHUB_DATABASE_SIZE",
                previous,
                "github_release",
                "database_size",
            ),
            "upload_outcome": env_or_previous(
                "COTACOES_RESULT_GITHUB_UPLOAD_OUTCOME",
                previous,
                "github_release",
                "upload_outcome",
            ),
            "upload_duration_seconds": env_or_previous(
                "COTACOES_RESULT_GITHUB_UPLOAD_DURATION",
                previous,
                "github_release",
                "upload_duration_seconds",
            ),
            "removed_legacy_assets": env_or_previous(
                "COTACOES_RESULT_REMOVED_LEGACY_ASSETS",
                previous,
                "github_release",
                "removed_legacy_assets",
            ),
        },
        "save_validation": {
            "outcome": env_or_previous(
                "COTACOES_RESULT_SAVE_OUTCOME",
                previous,
                "save_validation",
                "outcome",
            ),
        },
        "delivery": {
            "email_outcome": env_or_previous(
                "COTACOES_RESULT_EMAIL_OUTCOME",
                previous,
                "delivery",
                "email_outcome",
            ),
            "artifact_outcome": env_or_previous(
                "COTACOES_RESULT_ARTIFACT_OUTCOME",
                previous,
                "delivery",
                "artifact_outcome",
            ),
        },
    }


def build_result(args, publication: dict[str, Any]) -> dict[str, Any]:
    directory = args.directory
    previous = load_json(directory / "resultado.json") or {}
    scraper_outcome = env(
        "COTACOES_RESULT_SCRAPER_OUTCOME",
        str(previous.get("scraper_outcome") or "not_run"),
    )
    return {
        "schema_version": 1,
        "execution_id": args.execution_id,
        "status": args.status,
        "exit_code": args.exit_code,
        "error": sanitize_log_text(args.error) if args.error else None,
        "scraper_outcome": scraper_outcome,
        "scraper": load_json(directory / "etapas/scraper.relatorio.json"),
        "health": load_json(directory / "etapas/saude.json"),
        "checkpoint_gate": load_json(directory / "etapas/gate-checkpoint.json"),
        "publication_gate": load_json(directory / "etapas/gate-publicacao.json"),
        "publication": publication,
    }


def build_summary(
    args, publication: dict[str, Any], result: dict[str, Any]
) -> str:
    scraper_summary = args.directory / "etapas/scraper.md"
    if scraper_summary.is_file():
        content = scraper_summary.read_text(encoding="utf-8").rstrip()
    elif (args.directory / "resumo.md").is_file():
        content = (args.directory / "resumo.md").read_text(encoding="utf-8").rstrip()
        content = content.split("\n## Resultado do workflow\n", 1)[0].rstrip()
    else:
        content = "\n".join(
            [
                "# Relatorio de execucao do crawler",
                "",
                "O scraper nao gerou seu relatorio principal. O contexto parcial da ",
                "rodada foi preservado pelo workflow.",
            ]
        )

    restore = publication["restore"]
    gate = publication["publication_gate"]
    onedrive = publication["onedrive"]
    github = publication["github_release"]
    delivery = publication["delivery"]
    lines = [
        content,
        "",
        "## Resultado do workflow",
        "",
        "| Item | Valor |",
        "| --- | --- |",
        f"| Identificador | {escape(args.execution_id)} |",
        f"| Status final | {escape(args.status)} |",
        f"| Codigo de saida | {args.exit_code} |",
        f"| Repositorio | {escape(env('GITHUB_REPOSITORY'))} |",
        f"| Run ID | {escape(env('GITHUB_RUN_ID'))} |",
        f"| Run attempt | {escape(env('GITHUB_RUN_ATTEMPT'))} |",
        f"| Evento | {escape(env('GITHUB_EVENT_NAME'))} |",
        f"| Ref | {escape(env('GITHUB_REF'))} |",
        f"| Commit | {escape(env('GITHUB_SHA'))} |",
        f"| Origem restaurada | {escape(restore['source'])} |",
        f"| Asset restaurado | {escape(restore['asset'])} |",
        f"| Validacao da restauracao | {escape(restore['validation_status'])} |",
        f"| SHA-256 da restauracao | {escape(restore['checksum_validation'])} |",
        f"| Manifesto da restauracao | {escape(restore['manifest_status'])} |",
        f"| Resultado do scraper | {escape(result['scraper_outcome'])} |",
        f"| Gate do checkpoint | {escape(publication['checkpoint']['outcome'])} |",
        f"| Decisao avaliada do gate | {escape(gate['evaluated_decision'])} |",
        f"| Decisao efetiva do gate | {escape(gate['effective_decision'])} |",
        f"| Excecao manual do gate | {str(gate['bypassed']).lower()} |",
        f"| Pacote completo OneDrive | {escape(onedrive['package_outcome'])} |",
        f"| Upload OneDrive | {escape(onedrive['upload_outcome'])} |",
        f"| Pacote do banco GitHub | {escape(github['package_outcome'])} |",
        f"| Publicacao GitHub | {escape(github['upload_outcome'])} |",
        f"| Validacao do salvamento | {escape(publication['save_validation']['outcome'])} |",
        f"| Envio do e-mail | {escape(delivery['email_outcome'])} |",
        f"| Preservacao do artifact | {escape(delivery['artifact_outcome'])} |",
        "",
    ]
    layered_backup = onedrive.get("layered_backup")
    if isinstance(layered_backup, dict):
        layers = mapping(layered_backup.get("layers"))
        quota = mapping(layered_backup.get("quota"))
        quota_before = mapping(quota.get("before"))
        quota_after = mapping(quota.get("after"))
        retention = mapping(layered_backup.get("retention"))
        audit = mapping(layered_backup.get("audit_publication"))
        lines.extend(
            [
                "## Backups do OneDrive",
                "",
                "| Item | Valor |",
                "| --- | --- |",
                f"| Resultado geral | {escape(layered_backup.get('status'))} |",
            ]
        )
        for layer_name, label in (
            ("staging", "Staging"),
            ("latest", "Latest"),
            ("daily", "Daily"),
            ("deep", "Deep"),
        ):
            if layer_name in layers:
                lines.append(
                    f"| {label} | "
                    f"{escape(mapping(layers.get(layer_name)).get('status'))} |"
                )
        lines.extend(
            [
                f"| Manifesto audit | {escape(audit.get('status'))} |",
                f"| Retencao | {escape(retention.get('status'))} |",
                f"| Arquivos removidos | {len(retention.get('removed_files', [])) if isinstance(retention.get('removed_files'), list) else 0} |",
                f"| OneDrive usado antes (bytes) | {escape(quota_before.get('used'))} |",
                f"| OneDrive livre antes (bytes) | {escape(quota_before.get('free'))} |",
                f"| OneDrive usado depois (bytes) | {escape(quota_after.get('used'))} |",
                f"| OneDrive livre depois (bytes) | {escape(quota_after.get('free'))} |",
                "",
            ]
        )
    if args.error:
        lines.extend(["## Erro final", "", sanitize_log_text(args.error), ""])
    return "\n".join(lines).rstrip() + "\n"


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve_backup_result_path(directory: Path) -> Path:
    configured = os.getenv(
        "COTACOES_RESULT_ONEDRIVE_BACKUP_RESULT", ""
    ).strip()
    if configured:
        return Path(configured)
    candidates = (
        directory / "etapas/backup-coleta.json",
        directory / "etapas/consolidacao-diaria.json",
        directory / "etapas/backup-workflow.json",
    )
    return next((path for path in candidates if path.is_file()), candidates[-1])


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def previous_value(
    payload: dict[str, Any], key: str, default: str = "not_run"
) -> str:
    return sanitize_log_text(payload.get(key) or default)


def env_or_previous(
    name: str,
    previous: dict[str, Any],
    section: str,
    key: str | None = None,
    default: str = "not_run",
) -> str:
    configured = os.getenv(name, "").strip()
    if configured:
        return sanitize_log_text(configured)
    if key is None:
        return previous_value(previous, section, default)
    return previous_value(mapping(previous.get(section)), key, default)


def env(name: str, default: str = "not_run") -> str:
    return sanitize_log_text(os.getenv(name, "").strip() or default)


def escape(value: object) -> str:
    return sanitize_log_text(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
