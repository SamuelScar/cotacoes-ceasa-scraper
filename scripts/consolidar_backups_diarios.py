#!/usr/bin/env python3
"""Consolida o ultimo backup intradiario valido nas camadas historicas."""

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

from cotacoes_ceasa.backups.daily_consolidation import (  # noqa: E402
    DailyConsolidationRequest,
    run_daily_consolidation,
)
from cotacoes_ceasa.execution import (  # noqa: E402
    sanitize_log_text,
    write_json_atomic,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Consolida staging validado em daily e deep."
    )
    parser.add_argument(
        "--execution-id",
        default=os.getenv("COTACOES_EXECUTION_ID"),
    )
    parser.add_argument(
        "--diretorio-execucao",
        type=Path,
        default=_environment_path("COTACOES_EXECUTION_DIR"),
    )
    parser.add_argument("--trabalho", type=Path, required=True)
    parser.add_argument("--resultado", type=Path, required=True)
    parser.add_argument("--data", type=date.fromisoformat)
    parser.add_argument(
        "--remote",
        default=os.getenv("DATA_ONEDRIVE_REMOTE", "onedrive"),
    )
    parser.add_argument(
        "--root",
        default=os.getenv("DATA_ONEDRIVE_DIR", "cotacoes-ceasa"),
    )
    args = parser.parse_args()

    if not args.execution_id or args.diretorio_execucao is None:
        parser.error(
            "Informe --execution-id e --diretorio-execucao, ou configure "
            "COTACOES_EXECUTION_ID e COTACOES_EXECUTION_DIR."
        )

    try:
        result = run_daily_consolidation(
            DailyConsolidationRequest(
                execution_id=args.execution_id,
                execution_directory=args.diretorio_execucao,
                work_directory=args.trabalho,
                result_path=args.resultado,
                remote=args.remote,
                remote_root=args.root,
                consolidation_date=args.data,
            )
        )
    except Exception as error:
        result = {
            "schema_version": 1,
            "status": "failed",
            "mode": "daily_consolidation",
            "execution_id": args.execution_id,
            "error": sanitize_log_text(error),
        }
        write_json_atomic(args.resultado, result)

    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(
        output,
        file=sys.stdout if result.get("status") == "completed" else sys.stderr,
    )
    return 0 if result.get("status") == "completed" else 1


def _environment_path(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    return Path(value) if value else None


if __name__ == "__main__":
    raise SystemExit(main())
