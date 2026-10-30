#!/usr/bin/env python3
"""Baixa somente os backups exigidos pelo plano de recuperacao validado."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for import_path in (PROJECT_ROOT, SOURCE_ROOT):
    if import_path.as_posix() not in sys.path:
        sys.path.insert(0, import_path.as_posix())

from cotacoes_ceasa.backups.policy import validate_remote_name  # noqa: E402
from cotacoes_ceasa.execution import (  # noqa: E402
    sanitize_log_text,
    write_json_atomic,
)
from cotacoes_ceasa.raw_recovery import (  # noqa: E402
    build_recovery_input_inventory,
    validate_raw_recovery_source,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Valida o plano e baixa somente seus backups e manifestos, "
            "sem modificar o remote."
        )
    )
    parser.add_argument("--plano", type=Path, required=True)
    parser.add_argument(
        "--resultado-planejamento",
        type=Path,
        required=True,
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--raiz-remota", required=True)
    parser.add_argument("--arquivos-backup", type=Path, required=True)
    parser.add_argument("--manifestos", type=Path, required=True)
    parser.add_argument("--inventario", type=Path, required=True)
    args = parser.parse_args()

    archives = args.arquivos_backup.resolve()
    manifests = args.manifestos.resolve()
    inventory_path = args.inventario.resolve()
    try:
        _require_new_directory(archives, "arquivos de backup")
        _require_new_directory(manifests, "manifestos")
        remote = validate_remote_name(args.remote)
        source_compatibility = validate_raw_recovery_source(
            current_database_path=args.database,
            plan_path=args.plano,
            planning_result_path=args.resultado_planejamento,
        )
        inventory = build_recovery_input_inventory(
            plan_path=args.plano,
            planning_result_path=args.resultado_planejamento,
            remote_root=args.raiz_remota,
        )
        inventory["source_compatibility"] = source_compatibility
        write_json_atomic(inventory_path, inventory)
        archives.mkdir(parents=True)
        manifests.mkdir(parents=True)
        downloaded_manifests: set[str] = set()
        backups = inventory["backups"]
        assert isinstance(backups, list)
        for position, item in enumerate(backups, start=1):
            if not isinstance(item, dict):
                raise RuntimeError("Entrada de backup invalida no inventario.")
            archive_name = str(item["archive_name"])
            manifest_name = str(item["manifest_name"])
            print(
                "[recuperacao] Baixando backup "
                f"{position}/{len(backups)}: {archive_name}",
                flush=True,
            )
            if manifest_name not in downloaded_manifests:
                _download(
                    remote,
                    str(item["manifest_remote_path"]),
                    manifests / manifest_name,
                )
                downloaded_manifests.add(manifest_name)
            _download(
                remote,
                str(item["remote_path"]),
                archives / archive_name,
            )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        failure = {
            "schema_version": 1,
            "status": "failed",
            "operation": "raw_recovery_input_download",
            "error": sanitize_log_text(error),
            "safety": {
                "remote_access": "read_only",
                "remote_files_modified": 0,
                "package_published": False,
            },
        }
        write_json_atomic(inventory_path, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    inventory["status"] = "completed"
    inventory["operation"] = "raw_recovery_input_download"
    inventory["downloaded_backups"] = len(backups)
    inventory["downloaded_manifests"] = len(downloaded_manifests)
    inventory["safety"] = {
        "remote_access": "read_only",
        "allowed_remote_operations": ["copyto_download"],
        "remote_files_modified": 0,
        "package_published": False,
    }
    write_json_atomic(inventory_path, inventory)
    print(json.dumps(inventory, ensure_ascii=False, indent=2))
    return 0


def _download(remote: str, remote_path: str, destination: Path) -> None:
    subprocess.run(
        (
            "rclone",
            "copyto",
            f"{remote}:{remote_path}",
            destination.as_posix(),
            "--progress",
        ),
        check=True,
    )
    if not destination.is_file() or destination.is_symlink():
        raise RuntimeError(f"O download nao gerou {destination.name}.")


def _require_new_directory(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(
            f"O diretorio de {label} ja existe e nao sera sobrescrito: {path}."
        )


if __name__ == "__main__":
    raise SystemExit(main())
