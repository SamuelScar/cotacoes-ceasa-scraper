#!/usr/bin/env python3
"""Planeja e aplica com seguranca a migracao dos backups legados."""

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

from cotacoes_ceasa.backups.legacy_migration import (  # noqa: E402
    HISTORY_MIGRATION_BATCH_COUNT,
    LegacyMigrationApplyCheckRequest,
    LegacyMigrationConversionError,
    LegacyMigrationConversionRequest,
    LegacyHistoryBatchRequest,
    LegacyHistoryFinalizeRequest,
    LegacyHistoryMigrationPlanRequest,
    LegacyMigrationPlanError,
    LegacyMigrationPlanRequest,
    create_legacy_history_migration_plan,
    create_legacy_migration_plan,
    execute_legacy_history_batch,
    execute_legacy_history_finalization,
    execute_legacy_migration_conversion,
    validate_legacy_migration_apply,
)
from cotacoes_ceasa.execution import sanitize_log_text, write_json_atomic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gerencia a migracao segura dos backups legados."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Gera um plano somente leitura a partir do estado remoto.",
    )
    plan_parser.add_argument(
        "--remote",
        default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
    )
    plan_parser.add_argument(
        "--root",
        default=os.getenv("DATA_ONEDRIVE_DIR", "cotacoes-ceasa"),
    )
    plan_parser.add_argument("--resultado", type=Path, required=True)
    plan_parser.add_argument("--relatorio", type=Path, required=True)
    plan_parser.add_argument(
        "--workspace",
        type=Path,
        default=PROJECT_ROOT,
    )
    plan_parser.add_argument("--data-referencia", type=date.fromisoformat)
    plan_parser.add_argument(
        "--deep-retention-days",
        type=_positive_integer,
        default=_environment_positive_integer(
            "DATA_ONEDRIVE_DEEP_RETENTION_DAYS",
            30,
        ),
    )

    history_plan_parser = subparsers.add_parser(
        "history-plan",
        help="Planeja a importacao dos historicos legados por data.",
    )
    _add_common_arguments(history_plan_parser)
    history_plan_parser.add_argument(
        "--daily-retention-days",
        type=_positive_integer,
        default=_environment_positive_integer(
            "DATA_ONEDRIVE_DAILY_RETENTION_DAYS",
            7,
        ),
    )
    history_plan_parser.add_argument(
        "--lotes",
        type=_positive_integer,
        default=HISTORY_MIGRATION_BATCH_COUNT,
    )

    history_batch_parser = subparsers.add_parser(
        "history-batch",
        help="Executa um lote aprovado da importacao historica.",
    )
    history_batch_parser.add_argument(
        "--plano-aprovado",
        type=Path,
        required=True,
    )
    history_batch_parser.add_argument("--plan-sha256", required=True)
    history_batch_parser.add_argument("--confirmacao", required=True)
    history_batch_parser.add_argument(
        "--lote",
        type=_positive_integer,
        required=True,
    )
    history_batch_parser.add_argument("--execution-id", required=True)
    history_batch_parser.add_argument("--trabalho", type=Path, required=True)
    history_batch_parser.add_argument(
        "--workspace",
        type=Path,
        default=PROJECT_ROOT,
    )
    history_batch_parser.add_argument("--resultado", type=Path, required=True)
    history_batch_parser.add_argument("--relatorio", type=Path, required=True)

    history_finalize_parser = subparsers.add_parser(
        "history-finalize",
        help="Valida as camadas historicas e remove os legados aprovados.",
    )
    history_finalize_parser.add_argument(
        "--plano-aprovado",
        type=Path,
        required=True,
    )
    history_finalize_parser.add_argument("--plan-sha256", required=True)
    history_finalize_parser.add_argument("--confirmacao", required=True)
    history_finalize_parser.add_argument("--execution-id", required=True)
    history_finalize_parser.add_argument(
        "--workspace",
        type=Path,
        default=PROJECT_ROOT,
    )
    history_finalize_parser.add_argument(
        "--resultado",
        type=Path,
        required=True,
    )
    history_finalize_parser.add_argument(
        "--relatorio",
        type=Path,
        required=True,
    )

    apply_check_parser = subparsers.add_parser(
        "apply-check",
        help="Revalida o plano e os recursos sem executar mutacoes.",
    )
    _add_common_arguments(apply_check_parser)
    _add_apply_check_arguments(apply_check_parser)

    apply_parser = subparsers.add_parser(
        "apply",
        help=(
            "Converte, publica e remove somente os legados aprovados "
            "pelo plano."
        ),
    )
    _add_common_arguments(apply_parser)
    _add_apply_check_arguments(apply_parser)
    apply_parser.add_argument("--validacao", type=Path, required=True)
    apply_parser.add_argument("--relatorio-validacao", type=Path, required=True)
    apply_parser.add_argument("--execution-id", required=True)
    apply_parser.add_argument("--trabalho", type=Path, required=True)
    apply_parser.add_argument("--resultado-backup", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "plan":
            result = create_legacy_migration_plan(_plan_request(args))
        elif args.command == "history-plan":
            result = create_legacy_history_migration_plan(
                _history_plan_request(args)
            )
        elif args.command == "history-batch":
            result = execute_legacy_history_batch(
                LegacyHistoryBatchRequest(
                    approved_plan_path=args.plano_aprovado,
                    expected_plan_sha256=args.plan_sha256,
                    confirmation=args.confirmacao,
                    batch_number=args.lote,
                    execution_id=args.execution_id,
                    operation_root=args.trabalho,
                    workspace_path=args.workspace,
                    result_path=args.resultado,
                    report_path=args.relatorio,
                )
            )
        elif args.command == "history-finalize":
            result = execute_legacy_history_finalization(
                LegacyHistoryFinalizeRequest(
                    approved_plan_path=args.plano_aprovado,
                    expected_plan_sha256=args.plan_sha256,
                    confirmation=args.confirmacao,
                    execution_id=args.execution_id,
                    workspace_path=args.workspace,
                    result_path=args.resultado,
                    report_path=args.relatorio,
                )
            )
        elif args.command == "apply-check":
            result = validate_legacy_migration_apply(
                _apply_check_request(
                    args,
                    output_path=args.resultado,
                    report_path=args.relatorio,
                )
            )
        elif args.command == "apply":
            result = execute_legacy_migration_conversion(
                LegacyMigrationConversionRequest(
                    apply_check_request=_apply_check_request(
                        args,
                        output_path=args.validacao,
                        report_path=args.relatorio_validacao,
                    ),
                    execution_id=args.execution_id,
                    operation_root=args.trabalho,
                    result_path=args.resultado,
                    report_path=args.relatorio,
                    backup_result_path=args.resultado_backup,
                )
            )
        else:
            raise RuntimeError("Comando de migracao nao implementado.")
    except LegacyMigrationConversionError as error:
        print(
            json.dumps(error.result, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, LegacyMigrationPlanError) as error:
        result = {
            "schema_version": 1,
            "status": "failed",
            "mode": args.command,
            "error": sanitize_log_text(error),
        }
        write_json_atomic(args.resultado, result)
        _write_failure_report(args.relatorio, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--remote",
        default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
    )
    parser.add_argument(
        "--root",
        default=os.getenv("DATA_ONEDRIVE_DIR", "cotacoes-ceasa"),
    )
    parser.add_argument("--resultado", type=Path, required=True)
    parser.add_argument("--relatorio", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--data-referencia", type=date.fromisoformat)
    parser.add_argument(
        "--deep-retention-days",
        type=_positive_integer,
        default=_environment_positive_integer(
            "DATA_ONEDRIVE_DEEP_RETENTION_DAYS",
            30,
        ),
    )


def _add_apply_check_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--confirmacao", required=True)
    parser.add_argument("--plano-atual", type=Path, required=True)
    parser.add_argument("--relatorio-plano-atual", type=Path, required=True)


def _plan_request(
    args: argparse.Namespace,
    *,
    output_path: Path | None = None,
    report_path: Path | None = None,
) -> LegacyMigrationPlanRequest:
    return LegacyMigrationPlanRequest(
        remote=args.remote,
        remote_root=args.root,
        output_path=output_path or args.resultado,
        report_path=report_path or args.relatorio,
        workspace_path=args.workspace,
        reference_date=args.data_referencia,
        deep_retention_days=args.deep_retention_days,
    )


def _apply_check_request(
    args: argparse.Namespace,
    *,
    output_path: Path,
    report_path: Path,
) -> LegacyMigrationApplyCheckRequest:
    return LegacyMigrationApplyCheckRequest(
        plan_request=_plan_request(
            args,
            output_path=args.plano_atual,
            report_path=args.relatorio_plano_atual,
        ),
        expected_plan_sha256=args.plan_sha256,
        confirmation=args.confirmacao,
        output_path=output_path,
        report_path=report_path,
    )


def _history_plan_request(
    args: argparse.Namespace,
) -> LegacyHistoryMigrationPlanRequest:
    return LegacyHistoryMigrationPlanRequest(
        remote=args.remote,
        remote_root=args.root,
        output_path=args.resultado,
        report_path=args.relatorio,
        workspace_path=args.workspace,
        reference_date=args.data_referencia,
        daily_retention_days=args.daily_retention_days,
        deep_retention_days=args.deep_retention_days,
        batch_count=args.lotes,
    )


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "o valor deve ser um inteiro maior ou igual a 1"
        ) from error
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            "o valor deve ser um inteiro maior ou igual a 1"
        )
    return parsed


def _environment_positive_integer(name: str, default: int) -> int:
    return _positive_integer(os.getenv(name, str(default)))


def _write_failure_report(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Migracao dos backups legados",
        "",
        f"- Status: `{result['status']}`",
        f"- Modo: `{result['mode']}`",
        f"- Erro: {result['error']}",
        "",
        "## Proximas acoes manuais",
        "",
        "- Manter `DATA_MIGRATION_LOCKED=true`.",
        "- Revisar o erro e os artifacts antes de repetir a operacao.",
        "- Gerar um novo plano caso o inventario remoto tenha mudado.",
        "",
        "O workflow nao altera a trava automaticamente.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
