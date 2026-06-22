#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
PACKAGE_FILE = PROJECT_ROOT / "data.tar.gz"
TEMP_PACKAGE_FILE = PROJECT_ROOT / "data.tar.gz.tmp"
LOCK_FILE = PROJECT_ROOT / ".cotacoes-data.lock"


class CommandError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Executa comandos do scraper mantendo data.tar.gz seguro."
    )
    parser.add_argument(
        "compose_args",
        nargs=argparse.REMAINDER,
        help="Servico e argumentos repassados ao docker compose run --rm.",
    )
    args = parser.parse_args()

    if not args.compose_args:
        parser.error(
            "informe o servico, por exemplo: tudo, salvar ou app --source ceasa-pe"
        )

    compose_command = resolve_compose_command()
    lock_fd = acquire_lock()
    command_exit_code = 0

    try:
        prepare_data(compose_command)

        try:
            run_compose_service(compose_command, args.compose_args)
        except subprocess.CalledProcessError as error:
            command_exit_code = error.returncode or 1
        except KeyboardInterrupt:
            command_exit_code = 130
            print("Execucao interrompida; compactando dados atuais antes de sair.")

        package_data(compose_command)
        remove_data_dir()
    except Exception as error:
        print(f"Erro: {error}", file=sys.stderr)
        return command_exit_code or 1
    finally:
        release_lock(lock_fd)

    return command_exit_code


def resolve_compose_command() -> list[str]:
    docker_compose = ["docker", "compose"]

    if command_exists([*docker_compose, "version"]):
        return docker_compose

    legacy_compose = ["docker-compose"]

    if command_exists([*legacy_compose, "version"]):
        return legacy_compose

    raise CommandError("Docker Compose nao encontrado.")


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
            f"Ja existe uma execucao usando o pacote de dados: {LOCK_FILE}. "
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


def prepare_data(compose_command: list[str]) -> None:
    if DATA_DIR.exists():
        print("Usando pasta data/ existente.")
        return

    if not PACKAGE_FILE.exists():
        print("data.tar.gz nao encontrado; criando data/ vazia.")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return

    print("Descompactando data.tar.gz com pigz.")
    run_compose(
        compose_command,
        [
            "run",
            "--rm",
            "--entrypoint",
            "tar",
            "app",
            "-I",
            "pigz",
            "-xf",
            "data.tar.gz",
        ],
    )


def run_compose_service(compose_command: list[str], compose_args: list[str]) -> None:
    print(f"Executando: docker compose run --rm {' '.join(compose_args)}")
    run_compose(compose_command, ["run", "--rm", *compose_args])


def package_data(compose_command: list[str]) -> None:
    if not DATA_DIR.exists():
        raise CommandError("data/ nao existe ao final da execucao.")

    remove_temp_package()
    print("Compactando data/ em data.tar.gz.tmp com pigz.")
    run_compose(
        compose_command,
        [
            "run",
            "--rm",
            "--entrypoint",
            "tar",
            "app",
            "-I",
            "pigz",
            "-cf",
            "data.tar.gz.tmp",
            "data",
        ],
    )

    print("Validando data.tar.gz.tmp.")
    run_compose(
        compose_command,
        [
            "run",
            "--rm",
            "--entrypoint",
            "tar",
            "app",
            "-I",
            "pigz",
            "-tf",
            "data.tar.gz.tmp",
        ],
        stdout=subprocess.DEVNULL,
    )

    TEMP_PACKAGE_FILE.replace(PACKAGE_FILE)
    print("data.tar.gz atualizado com sucesso.")


def remove_temp_package() -> None:
    try:
        TEMP_PACKAGE_FILE.unlink()
    except FileNotFoundError:
        pass


def remove_data_dir() -> None:
    shutil.rmtree(DATA_DIR)
    print("Pasta data/ removida; o estado versionavel ficou em data.tar.gz.")


def run_compose(
    compose_command: list[str],
    args: list[str],
    stdout=None,
) -> None:
    subprocess.run(
        [*compose_command, *args],
        cwd=PROJECT_ROOT,
        stdout=stdout,
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
