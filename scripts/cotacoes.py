#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_FILE = DATA_DIR / "cotacoes.sqlite"
DEFAULT_PACKAGE_FILE = PROJECT_ROOT / "ceasa-data-latest.tar.xz"
DEFAULT_DATABASE_PACKAGE_FILE = PROJECT_ROOT / "cotacoes.sqlite.xz"
LOCK_FILE = PROJECT_ROOT / ".cotacoes-data.lock"
PACKAGE_EXCLUDES = (
    "data/cotacoes.sqlite",
    "data/cotacoes.sqlite-shm",
    "data/cotacoes.sqlite-wal",
)


class CommandError(RuntimeError):
    pass


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    package_file = resolve_package_file(args).resolve()
    ensure_package_tools()
    lock_fd = acquire_lock()

    try:
        if args.comando == "compactar":
            compact_data(package_file, include_sqlite=args.incluir_sqlite)
        elif args.comando == "descompactar":
            extract_data(package_file, include_sqlite=args.incluir_sqlite)
        elif args.comando == "compactar-banco":
            compact_database(package_file)
        elif args.comando == "descompactar-banco":
            extract_database(package_file)
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
        description="Compacta ou descompacta dados do projeto com xz ou gzip."
    )
    parser.add_argument(
        "comando",
        choices=("compactar", "descompactar", "compactar-banco", "descompactar-banco"),
        help="Operacao desejada para o pacote de dados.",
    )
    parser.add_argument(
        "--arquivo",
        type=Path,
        help=(
            "Arquivo usado na operacao. Padrao: ceasa-data-latest.tar.xz "
            "para data/ ou cotacoes.sqlite.xz para o banco isolado. "
            "Arquivos .tar.gz legados tambem sao aceitos para restauracao."
        ),
    )
    parser.add_argument(
        "--incluir-sqlite",
        action="store_true",
        help="Inclui ou restaura o SQLite no pacote completo de dados.",
    )
    return parser


def resolve_package_file(args) -> Path:
    if args.arquivo:
        return args.arquivo

    if args.comando in {"compactar-banco", "descompactar-banco"}:
        return DEFAULT_DATABASE_PACKAGE_FILE

    return DEFAULT_PACKAGE_FILE


def ensure_package_tools() -> None:
    if not running_inside_container():
        raise CommandError(
            "Execute este script pelo container app com Docker Compose."
        )

    required_commands = (["tar", "--version"], ["xz", "--version"], ["pigz", "--version"])
    if all(command_exists(command) for command in required_commands):
        return

    raise CommandError(
        "tar, xz ou pigz nao encontrados. Execute este script dentro do container app."
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


def compact_data(package_file: Path, include_sqlite: bool) -> None:
    if not DATA_DIR.exists():
        raise CommandError("data/ nao existe para compactar.")

    temp_package_file = package_file.with_name(f"{package_file.name}.tmp")
    remove_file(temp_package_file)
    exclude_args = [] if include_sqlite else build_exclude_args(PACKAGE_EXCLUDES)
    tar_filter = resolve_tar_filter(package_file, operation="compress")

    print(f"Compactando data/ em {temp_package_file.name} com {tar_filter}.")
    run_tar(
        [
            "-I",
            tar_filter,
            *exclude_args,
            "-cf",
            temp_package_file.name,
            "data",
        ],
    )

    print(f"Validando {temp_package_file.name}.")
    run_tar(
        ["-I", resolve_tar_filter(package_file, operation="decompress"), "-tf", temp_package_file.name],
        stdout=subprocess.DEVNULL,
    )

    temp_package_file.replace(package_file)
    print(f"Pacote atualizado: {package_file.name}")


def extract_data(package_file: Path, include_sqlite: bool) -> None:
    if not package_file.exists():
        raise CommandError(f"Arquivo nao encontrado: {package_file}")

    remove_package_excluded_files()
    tar_filter = resolve_tar_filter(package_file, operation="decompress")
    print(f"Descompactando {package_file.name} com {tar_filter}.")
    run_tar(
        ["-I", tar_filter, "-xf", package_file.name],
    )

    if not include_sqlite:
        remove_package_excluded_files()

    print("Pasta data/ restaurada.")


def resolve_tar_filter(package_file: Path, operation: str) -> str:
    file_name = package_file.name

    if file_name.endswith(".tar.xz"):
        return "xz -T0 -9" if operation == "compress" else "xz -T0"

    if file_name.endswith(".tar.gz"):
        return "pigz -9" if operation == "compress" else "pigz"

    raise CommandError(
        f"Formato de pacote nao suportado: {package_file.name}. "
        "Use .tar.xz ou .tar.gz."
    )


def compact_database(package_file: Path) -> None:
    if not DATABASE_FILE.exists():
        raise CommandError(f"Banco SQLite nao encontrado: {DATABASE_FILE}")

    temp_package_file = package_file.with_name(f"{package_file.name}.tmp")
    snapshot_file = package_file.with_name(f"{package_file.name}.snapshot.sqlite")
    remove_file(temp_package_file)
    remove_file(snapshot_file)

    try:
        print(f"Gerando snapshot consistente de {DATABASE_FILE}.")
        create_database_snapshot(DATABASE_FILE, snapshot_file)
        check_database(snapshot_file)

        print(f"Compactando banco em {temp_package_file.name}.")
        run_database_compress(snapshot_file, temp_package_file)
        run_compressed_file_test(temp_package_file)

        temp_package_file.replace(package_file)
        print(f"Banco compactado atualizado: {package_file.name}")
    finally:
        remove_file(snapshot_file)
        remove_file(temp_package_file)


def extract_database(package_file: Path) -> None:
    if not package_file.exists():
        raise CommandError(f"Arquivo nao encontrado: {package_file}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_database_file = DATABASE_FILE.with_name(f"{DATABASE_FILE.name}.tmp")
    remove_file(temp_database_file)

    try:
        print(f"Descompactando {package_file.name} para {DATABASE_FILE}.")
        run_database_decompress(package_file, temp_database_file)
        check_database(temp_database_file)
        remove_package_excluded_files()
        temp_database_file.replace(DATABASE_FILE)
        print("Banco SQLite restaurado.")
    finally:
        remove_file(temp_database_file)


def create_database_snapshot(source_path: Path, target_path: Path) -> None:
    source_uri = source_path.as_posix()
    source = sqlite3.connect(f"file:{source_uri}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)

    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def check_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()

    if not result or result[0] != "ok":
        raise CommandError(f"Banco SQLite invalido: {database_path}")


def run_database_compress(source_path: Path, output_path: Path) -> None:
    with output_path.open("wb") as output_file:
        subprocess.run(
            build_database_compress_command(output_path, source_path),
            cwd=PROJECT_ROOT,
            stdout=output_file,
            check=True,
        )


def run_database_decompress(source_path: Path, output_path: Path) -> None:
    with output_path.open("wb") as output_file:
        subprocess.run(
            build_database_decompress_command(source_path),
            cwd=PROJECT_ROOT,
            stdout=output_file,
            check=True,
        )


def run_compressed_file_test(path: Path) -> None:
    subprocess.run(
        build_compressed_file_test_command(path),
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        check=True,
    )


def build_database_compress_command(output_path: Path, source_path: Path) -> list[str]:
    if output_path.name.endswith(".xz"):
        return ["xz", "-T0", "-9", "-c", str(source_path)]

    if output_path.name.endswith(".gz"):
        return ["pigz", "-9", "-c", str(source_path)]

    raise CommandError(
        f"Formato de banco compactado nao suportado: {output_path.name}. "
        "Use .xz ou .gz."
    )


def build_database_decompress_command(source_path: Path) -> list[str]:
    if source_path.name.endswith(".xz"):
        return ["xz", "-T0", "-d", "-c", str(source_path)]

    if source_path.name.endswith(".gz"):
        return ["pigz", "-d", "-c", str(source_path)]

    raise CommandError(
        f"Formato de banco compactado nao suportado: {source_path.name}. "
        "Use .xz ou .gz."
    )


def build_compressed_file_test_command(path: Path) -> list[str]:
    if path.name.endswith(".xz"):
        return ["xz", "-T0", "-t", str(path)]

    if path.name.endswith(".gz"):
        return ["pigz", "-t", str(path)]

    raise CommandError(
        f"Formato compactado nao suportado: {path.name}. Use .xz ou .gz."
    )


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def build_exclude_args(patterns: tuple[str, ...]) -> list[str]:
    return [f"--exclude={pattern}" for pattern in patterns]


def remove_package_excluded_files() -> None:
    for pattern in PACKAGE_EXCLUDES:
        remove_file(PROJECT_ROOT / pattern)


def run_tar(tar_args: list[str], stdout=None) -> None:
    subprocess.run(
        ["tar", *tar_args],
        cwd=PROJECT_ROOT,
        stdout=stdout,
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
