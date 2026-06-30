#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_PACKAGE_FILE = PROJECT_ROOT / "ceasa-data-latest.tar.gz"
LOCK_FILE = PROJECT_ROOT / ".cotacoes-data.lock"


class CommandError(RuntimeError):
    pass


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    package_file = args.arquivo.resolve()
    ensure_package_tools()
    lock_fd = acquire_lock()

    try:
        if args.comando == "compactar":
            compact_data(package_file)
        elif args.comando == "descompactar":
            extract_data(package_file)
        else:
            parser.error(f"comando invalido: {args.comando}")
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
    finally:
        release_lock(lock_fd)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compacta ou descompacta a pasta data/ com tar e pigz."
    )
    parser.add_argument(
        "comando",
        choices=("compactar", "descompactar"),
        help="Operacao desejada para o pacote de dados.",
    )
    parser.add_argument(
        "--arquivo",
        type=Path,
        default=DEFAULT_PACKAGE_FILE,
        help=(
            "Arquivo .tar.gz usado na operacao. "
            "Padrao: ceasa-data-latest.tar.gz."
        ),
    )
    return parser


def ensure_package_tools() -> None:
    if not running_inside_container():
        raise CommandError(
            "Execute este script pelo container app com Docker Compose."
        )

    if command_exists(["tar", "--version"]) and command_exists(["pigz", "--version"]):
        return

    raise CommandError(
        "tar ou pigz nao encontrados. Execute este script dentro do container app."
    )


def running_inside_container() -> bool:
    return Path("/.dockerenv").exists() or os.getenv("container") is not None


def command_exists(command: list[str]) -> bool:
    try:
        subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

    return True


def acquire_lock() -> int:
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise CommandError(
            f"Ja existe uma operacao usando o pacote de dados: {LOCK_FILE}. "
            "Se nao houver outro processo rodando, remova esse arquivo manualmente."
        ) from error

    lock_message = f"pid={os.getpid()}\n"
    os.write(lock_fd, lock_message.encode("utf-8"))

    return lock_fd


def release_lock(lock_fd: int) -> None:
    try:
        os.close(lock_fd)
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def compact_data(package_file: Path) -> None:
    if not DATA_DIR.exists():
        raise CommandError("data/ nao existe para compactar.")

    temp_package_file = package_file.with_name(f"{package_file.name}.tmp")
    remove_file(temp_package_file)

    print(f"Compactando data/ em {temp_package_file.name} com pigz.")
    run_tar(
        ["-I", "pigz", "-cf", temp_package_file.name, "data"],
    )

    print(f"Validando {temp_package_file.name}.")
    run_tar(
        ["-I", "pigz", "-tf", temp_package_file.name],
        stdout=subprocess.DEVNULL,
    )

    temp_package_file.replace(package_file)
    print(f"Pacote atualizado: {package_file.name}")


def extract_data(package_file: Path) -> None:
    if not package_file.exists():
        raise CommandError(f"Arquivo nao encontrado: {package_file}")

    print(f"Descompactando {package_file.name} com pigz.")
    run_tar(
        ["-I", "pigz", "-xf", package_file.name],
    )
    print("Pasta data/ restaurada.")


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def run_tar(tar_args: list[str], stdout=None) -> None:
    subprocess.run(
        ["tar", *tar_args],
        cwd=PROJECT_ROOT,
        stdout=stdout,
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
