#!/usr/bin/env python3
"""Cria uma copia candidata com os raws localizados pelo planejamento."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
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
from cotacoes_ceasa.raw_recovery import (  # noqa: E402
    create_raw_recovery_candidate,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recupera raws em uma copia candidata sem alterar data/ nem "
            "os backups de origem."
        )
    )
    parser.add_argument("--data-atual", type=Path, required=True)
    parser.add_argument("--plano", type=Path, required=True)
    parser.add_argument(
        "--resultado-planejamento",
        type=Path,
        required=True,
    )
    parser.add_argument("--arquivos-backup", type=Path, required=True)
    parser.add_argument("--manifestos", type=Path, required=True)
    parser.add_argument("--candidata", type=Path, required=True)
    parser.add_argument("--trabalho", type=Path, required=True)
    parser.add_argument("--resultado", type=Path, required=True)
    parser.add_argument("--relatorio", type=Path, required=True)
    args = parser.parse_args()

    try:
        result = create_raw_recovery_candidate(
            current_data_directory=args.data_atual,
            plan_path=args.plano,
            planning_result_path=args.resultado_planejamento,
            archives_directory=args.arquivos_backup,
            manifests_directory=args.manifestos,
            candidate_root=args.candidata,
            work_directory=args.trabalho,
            result_path=args.resultado,
            report_path=args.relatorio,
        )
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as error:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "operation": "raw_recovery_candidate",
            "error": sanitize_log_text(error),
            "candidate_root": args.candidata.resolve().as_posix(),
            "safety": {
                "current_data_modified": False,
                "remote_access": "none",
                "remote_files_modified": 0,
                "existing_candidate_files_overwritten": 0,
                "package_published": False,
            },
        }
        write_json_atomic(args.resultado, failure)
        write_text_atomic(
            args.relatorio,
            "\n".join(
                [
                    "# Recuperacao candidata dos arquivos raw",
                    "",
                    "- Status: **failed**",
                    f"- Erro: {failure['error']}",
                    "- Dados atuais alterados: **nao**",
                    "- Arquivos remotos alterados: **0**",
                    "- Pacotes publicados: **0**",
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
