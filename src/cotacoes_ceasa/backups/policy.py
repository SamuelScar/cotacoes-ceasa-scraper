"""Limites de seguranca para os caminhos remotos dos backups."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import PurePosixPath


BACKUP_LAYOUT_ROOT = PurePosixPath("backups")
MANAGED_REMOTE_DIRECTORIES = (
    BACKUP_LAYOUT_ROOT / "latest",
    BACKUP_LAYOUT_ROOT / "staging",
    BACKUP_LAYOUT_ROOT / "history" / "daily",
    BACKUP_LAYOUT_ROOT / "history" / "deep",
    BACKUP_LAYOUT_ROOT / "audit",
)
LAYER_REMOTE_DIRECTORIES = {
    "latest": BACKUP_LAYOUT_ROOT / "latest",
    "staging": BACKUP_LAYOUT_ROOT / "staging",
    "daily": BACKUP_LAYOUT_ROOT / "history" / "daily",
    "deep": BACKUP_LAYOUT_ROOT / "history" / "deep",
    "audit": BACKUP_LAYOUT_ROOT / "audit",
}

# A retencao exige --apply e prova de publicacao valida. O modo padrao apenas
# calcula o plano sem remover objetos.
REMOTE_CLEANUP_ENABLED = True

_REMOTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LAYER_FILE_PATTERNS = {
    "latest": re.compile(r"^ceasa-data-latest\.zip$"),
    "staging": re.compile(
        r"^ceasa-data-[0-9]{8}-[0-9]{6}\.zip$"
    ),
    "daily": re.compile(r"^ceasa-data-[0-9]{8}\.tar\.xz$"),
    "deep": re.compile(r"^ceasa-data-[0-9]{8}\.(?:zpaq|tar\.xz)$"),
    "audit": re.compile(r"^backup-[0-9]{8}-[0-9]{6}\.json$"),
}


class BackupPolicyError(ValueError):
    """Indica que um remote ou caminho viola a politica de backup."""


def validate_remote_name(value: str) -> str:
    """Valida somente o nome do remote, sem permitir uma especificacao rclone."""
    remote_name = value.strip()
    if not remote_name or not _REMOTE_NAME_PATTERN.fullmatch(remote_name):
        raise BackupPolicyError(
            "Nome de remote invalido. Use somente letras, numeros, ponto, "
            "hifen e sublinhado."
        )
    return remote_name


def normalize_remote_root(value: str | PurePosixPath) -> PurePosixPath:
    """Normaliza um caminho relativo, impedindo escape da raiz configurada."""
    raw_value = str(value).strip().replace("\\", "/")
    if not raw_value:
        raise BackupPolicyError("A raiz remota nao pode ser vazia.")
    if raw_value.startswith("/") or ":" in raw_value:
        raise BackupPolicyError("A raiz remota deve ser um caminho relativo.")
    if any(ord(character) < 32 for character in raw_value):
        raise BackupPolicyError("A raiz remota contem caracteres de controle.")

    raw_value = raw_value.rstrip("/")
    if not raw_value:
        raise BackupPolicyError("A raiz remota nao pode ser vazia.")
    if any(part in {"", ".", ".."} for part in raw_value.split("/")):
        raise BackupPolicyError("A raiz remota contem um segmento inseguro.")
    return PurePosixPath(raw_value)


def managed_remote_path(directory: str, file_name: str) -> PurePosixPath:
    """Monta um caminho dentro de um dos diretorios gerenciados."""
    directory_path = normalize_remote_root(directory)
    if directory_path not in MANAGED_REMOTE_DIRECTORIES:
        raise BackupPolicyError(
            f"Diretorio remoto nao gerenciado: {directory_path.as_posix()}."
        )

    normalized_name = normalize_remote_root(file_name)
    if len(normalized_name.parts) != 1:
        raise BackupPolicyError("O nome do arquivo nao pode conter diretorios.")

    return directory_path / normalized_name


def validate_managed_remote_path(
    value: str | PurePosixPath,
    *,
    allow_directory: bool = False,
) -> PurePosixPath:
    """Confirma que o caminho pertence estritamente a uma area gerenciada."""
    path = normalize_remote_root(value)
    for directory in MANAGED_REMOTE_DIRECTORIES:
        if path == directory:
            if allow_directory:
                return path
            raise BackupPolicyError(
                "A operacao exige um arquivo dentro do diretorio gerenciado."
            )
        if path.is_relative_to(directory):
            return path

    raise BackupPolicyError(f"Caminho remoto nao gerenciado: {path.as_posix()}.")


def validate_layer_remote_path(
    layer: str,
    value: str | PurePosixPath,
) -> PurePosixPath:
    """Valida diretorio e nome definitivo esperados para uma camada."""
    if layer not in LAYER_REMOTE_DIRECTORIES:
        raise BackupPolicyError(f"Camada remota desconhecida: {layer}.")
    path = validate_managed_remote_path(value)
    expected_directory = LAYER_REMOTE_DIRECTORIES[layer]
    if path.parent != expected_directory:
        raise BackupPolicyError(
            f"Destino invalido para a camada {layer}: {path.as_posix()}."
        )
    if not _LAYER_FILE_PATTERNS[layer].fullmatch(path.name):
        raise BackupPolicyError(
            f"Nome definitivo invalido para a camada {layer}: {path.name}."
        )
    return path


def layer_file_date(layer: str, file_name: str) -> date | None:
    """Valida um nome definitivo e extrai sua data local quando aplicavel."""
    if layer not in _LAYER_FILE_PATTERNS:
        raise BackupPolicyError(f"Camada remota desconhecida: {layer}.")
    if not _LAYER_FILE_PATTERNS[layer].fullmatch(file_name):
        raise BackupPolicyError(
            f"Nome definitivo invalido para a camada {layer}: {file_name}."
        )
    if layer == "latest":
        return None
    if layer == "staging":
        try:
            return datetime.strptime(
                file_name[11:26],
                "%Y%m%d-%H%M%S",
            ).date()
        except ValueError as error:
            raise BackupPolicyError(
                f"Data ou hora invalida no arquivo da camada {layer}: "
                f"{file_name}."
            ) from error
    date_text = (
        file_name[11:19]
        if layer in {"daily", "deep"}
        else file_name[7:15]
    )
    try:
        return datetime.strptime(date_text, "%Y%m%d").date()
    except ValueError as error:
        raise BackupPolicyError(
            f"Data invalida no arquivo da camada {layer}: {file_name}."
        ) from error


def join_remote_root(
    configured_root: str | PurePosixPath,
    managed_path: str | PurePosixPath,
) -> PurePosixPath:
    """Combina a base configurada com um caminho previamente autorizado."""
    root = normalize_remote_root(configured_root)
    safe_path = validate_managed_remote_path(managed_path, allow_directory=True)
    return root / safe_path


def ensure_remote_cleanup_enabled() -> None:
    """Confirma que a implementacao de retencao esta liberada."""
    if not REMOTE_CLEANUP_ENABLED:
        raise BackupPolicyError(
            "A limpeza remota esta bloqueada durante a fase de avaliacao."
        )
