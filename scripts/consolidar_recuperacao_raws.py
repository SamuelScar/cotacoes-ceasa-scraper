#!/usr/bin/env python3
"""Consolida as evidencias da recuperacao e da auditoria candidata."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for import_path in (PROJECT_ROOT, SOURCE_ROOT):
    if import_path.as_posix() not in sys.path:
        sys.path.insert(0, import_path.as_posix())

from cotacoes_ceasa.execution import (  # noqa: E402
    sanitize_log_text,
    write_json_atomic,
    write_text_atomic,
)
from cotacoes_ceasa.raw_recovery_report import (  # noqa: E402
    consolidate_raw_recovery,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Valida e compara o planejamento, a recuperacao candidata e "
            "a auditoria integral."
        )
    )
    parser.add_argument("--planejamento", type=Path, required=True)
    parser.add_argument("--recuperacao", type=Path, required=True)
    parser.add_argument("--auditoria", type=Path, required=True)
    parser.add_argument("--resultado", type=Path, required=True)
    parser.add_argument("--relatorio", type=Path, required=True)
    args = parser.parse_args()

    try:
        result = consolidate_raw_recovery(
            planning_result_path=args.planejamento,
            recovery_result_path=args.recuperacao,
            audit_result_path=args.auditoria,
            result_path=args.resultado,
            report_path=args.relatorio,
        )
    except (OSError, RuntimeError, ValueError) as error:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "operation": "raw_recovery_validation",
            "error": sanitize_log_text(error),
            "safety": {
                "current_package_modified": False,
                "remote_files_modified": 0,
                "package_published": False,
            },
        }
        write_json_atomic(args.resultado, failure)
        write_text_atomic(
            args.relatorio,
            "\n".join(
                [
                    "# Validacao da recuperacao dos raws historicos",
                    "",
                    "- Status: **failed**",
                    f"- Erro: {failure['error']}",
                    "- Pacote atual alterado: **nao**",
                    "- Backups remotos alterados: **0**",
                    "- Pacote publicado: **nao**",
                    "",
                ]
            ),
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
