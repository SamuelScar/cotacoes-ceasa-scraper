"""Criacao e validacao local dos pacotes completos de dados."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from cotacoes_ceasa.execution import execution_now, sanitize_log_text


ZIP_COMPRESSION_LEVEL = 1
XZ_COMPRESSION_LEVEL = 3
ZPAQ_COMPRESSION_METHOD = 4
ZPAQ_THREADS = 0

# Decisao registrada pelo benchmark de 2026-10-05 da issue #5.
DEEP_BACKUP_FORMAT = "zpaq"


class BackupPackagingError(RuntimeError):
    """Indica falha segura durante a criacao ou validacao de um pacote."""


class BackupLayer(StrEnum):
    """Camadas independentes de backup previstas pela politica."""

    LATEST = "latest"
    DAILY = "daily"
    DEEP = "deep"


@dataclass(frozen=True)
class BackupLayerSpec:
    """Regras fixas de formato, nome e destino de uma camada."""

    layer: BackupLayer
    format: str
    file_name: str
    extension: str
    remote_directory: PurePosixPath
    algorithm: str
    parameters: tuple[str, ...]
    executable: str

    @classmethod
    def for_layer(
        cls,
        layer: BackupLayer,
        backup_date: date | None = None,
    ) -> "BackupLayerSpec":
        local_date = backup_date or execution_now().date()
        compact_date = local_date.strftime("%Y%m%d")
        if layer is BackupLayer.LATEST:
            return cls(
                layer=layer,
                format="zip",
                file_name="ceasa-data-latest.zip",
                extension=".zip",
                remote_directory=PurePosixPath("backups/latest"),
                algorithm="ZIP/Deflate",
                parameters=(
                    f"-mx={ZIP_COMPRESSION_LEVEL}",
                    "-mmt=on",
                ),
                executable="7z",
            )
        if layer is BackupLayer.DAILY:
            return cls(
                layer=layer,
                format="tar.xz",
                file_name=f"ceasa-data-{compact_date}.tar.xz",
                extension=".tar.xz",
                remote_directory=PurePosixPath("backups/history/daily"),
                algorithm="TAR+XZ",
                parameters=(f"-{XZ_COMPRESSION_LEVEL}", "-T0"),
                executable="tar",
            )
        if layer is BackupLayer.DEEP:
            return cls.for_deep_format(DEEP_BACKUP_FORMAT, local_date)
        raise BackupPackagingError(f"Camada de backup desconhecida: {layer}.")

    @classmethod
    def for_deep_format(
        cls,
        archive_format: str,
        backup_date: date | None = None,
    ) -> "BackupLayerSpec":
        """Monta a camada profunda para o formato decidido ou em avaliacao."""
        local_date = backup_date or execution_now().date()
        compact_date = local_date.strftime("%Y%m%d")
        if archive_format == "tar.xz":
            return cls(
                layer=BackupLayer.DEEP,
                format="tar.xz",
                file_name=f"ceasa-data-{compact_date}.tar.xz",
                extension=".tar.xz",
                remote_directory=PurePosixPath("backups/history/deep"),
                algorithm="TAR+XZ",
                parameters=(f"-{XZ_COMPRESSION_LEVEL}", "-T0"),
                executable="tar",
            )
        if archive_format == "zpaq":
            return cls(
                layer=BackupLayer.DEEP,
                format="zpaq",
                file_name=f"ceasa-data-{compact_date}.zpaq",
                extension=".zpaq",
                remote_directory=PurePosixPath("backups/history/deep"),
                algorithm="ZPAQ",
                parameters=(
                    f"-method={ZPAQ_COMPRESSION_METHOD}",
                    f"-threads={ZPAQ_THREADS}",
                    "incremental=false",
                ),
                executable="zpaq",
            )
        raise BackupPackagingError(
            f"Formato profundo desconhecido: {archive_format}."
        )


@dataclass(frozen=True)
class SourceMetrics:
    path: str
    files: int
    bytes: int


@dataclass(frozen=True)
class BackupArtifact:
    schema_version: int
    layer: str
    format: str
    algorithm: str
    parameters: tuple[str, ...]
    source: SourceMetrics
    file_name: str
    local_path: str
    remote_directory: str
    size_bytes: int
    reduction_percent: float | None
    compression_duration_seconds: float
    sha256: str
    validation: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_backup(
    spec: BackupLayerSpec,
    source_directory: Path,
    output_directory: Path,
) -> BackupArtifact:
    """Cria uma camada, valida o temporario e somente entao o promove."""
    source = source_directory.resolve(strict=True)
    if not source.is_dir():
        raise BackupPackagingError(
            f"A origem do backup nao e um diretorio: {source}."
        )
    if source.name.startswith("-"):
        raise BackupPackagingError(
            "O nome do diretorio de origem nao pode iniciar com hifen."
        )

    output = output_directory.resolve(strict=False)
    if output == source or output.is_relative_to(source):
        raise BackupPackagingError(
            "O diretorio de saida deve ficar fora da origem do backup."
        )
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)

    _require_executable(spec.executable)
    if spec.format == "tar.xz":
        _require_executable("xz")

    source_metrics = collect_source_metrics(source)
    final_path = output / spec.file_name
    temporary_path = _temporary_path(output, spec)
    command = _creation_command(spec, source, temporary_path)

    started_at = time.monotonic()
    try:
        _run_command(command, working_directory=source.parent)
        compression_duration = time.monotonic() - started_at
        _validate_archive(spec, temporary_path)
        final_size = temporary_path.stat().st_size
        digest = calculate_sha256(temporary_path)
        temporary_path.replace(final_path)
    except (OSError, subprocess.SubprocessError, BackupPackagingError) as error:
        temporary_path.unlink(missing_ok=True)
        if isinstance(error, BackupPackagingError):
            raise
        raise BackupPackagingError(
            sanitize_log_text(
                f"Falha ao criar o backup {spec.layer.value}: {error}"
            )
        ) from error

    reduction = None
    if source_metrics.bytes > 0:
        reduction = round(
            (1 - (final_size / source_metrics.bytes)) * 100,
            2,
        )

    return BackupArtifact(
        schema_version=1,
        layer=spec.layer.value,
        format=spec.format,
        algorithm=spec.algorithm,
        parameters=spec.parameters,
        source=source_metrics,
        file_name=spec.file_name,
        local_path=final_path.as_posix(),
        remote_directory=spec.remote_directory.as_posix(),
        size_bytes=final_size,
        reduction_percent=reduction,
        compression_duration_seconds=round(compression_duration, 3),
        sha256=digest,
        validation="passed",
        created_at=execution_now().isoformat(timespec="seconds"),
    )


def collect_source_metrics(source_directory: Path) -> SourceMetrics:
    """Conta arquivos regulares e bytes, rejeitando entradas inseguras."""
    files = 0
    total_bytes = 0
    for entry in sorted(source_directory.rglob("*")):
        if entry.is_symlink():
            raise BackupPackagingError(
                f"A origem contem link simbolico nao permitido: {entry}."
            )
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise BackupPackagingError(
                f"A origem contem uma entrada especial nao permitida: {entry}."
            )
        files += 1
        total_bytes += entry.stat().st_size

    return SourceMetrics(
        path=source_directory.as_posix(),
        files=files,
        bytes=total_bytes,
    )


def calculate_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as backup_file:
        while chunk := backup_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_path(output: Path, spec: BackupLayerSpec) -> Path:
    base_name = spec.file_name.removesuffix(spec.extension)
    token = uuid.uuid4().hex
    return output / f".{base_name}.{token}.tmp{spec.extension}"


def _creation_command(
    spec: BackupLayerSpec,
    source: Path,
    temporary_path: Path,
) -> list[str]:
    source_name = source.name
    archive = temporary_path.as_posix()
    if spec.format == "zip":
        return [
            "7z",
            "a",
            "-tzip",
            f"-mx={ZIP_COMPRESSION_LEVEL}",
            "-mmt=on",
            "-bd",
            archive,
            source_name,
        ]
    if spec.format == "tar.xz":
        return [
            "tar",
            "-I",
            f"xz -T0 -{XZ_COMPRESSION_LEVEL}",
            "-cf",
            archive,
            source_name,
        ]
    if spec.format == "zpaq":
        return [
            "zpaq",
            "add",
            archive,
            source_name,
            "-method",
            str(ZPAQ_COMPRESSION_METHOD),
            "-threads",
            str(ZPAQ_THREADS),
        ]
    raise BackupPackagingError(f"Formato de backup desconhecido: {spec.format}.")


def _validate_archive(spec: BackupLayerSpec, archive: Path) -> None:
    if not archive.is_file():
        raise BackupPackagingError(
            f"O compactador nao produziu o arquivo esperado: {archive}."
        )
    if spec.format == "zip":
        command = ["7z", "t", "-bd", archive.as_posix()]
    elif spec.format == "tar.xz":
        command = ["tar", "-tf", archive.as_posix()]
    elif spec.format == "zpaq":
        command = [
            "zpaq",
            "extract",
            archive.as_posix(),
            "-test",
            "-threads",
            str(ZPAQ_THREADS),
        ]
    else:
        raise BackupPackagingError(
            f"Formato de backup desconhecido: {spec.format}."
        )
    _run_command(command, working_directory=archive.parent)


def _require_executable(executable: str) -> None:
    if shutil.which(executable) is None:
        raise BackupPackagingError(
            f"Ferramenta obrigatoria nao encontrada: {executable}."
        )


def _run_command(command: list[str], working_directory: Path) -> None:
    try:
        subprocess.run(
            command,
            cwd=working_directory,
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError as error:
        detail = sanitize_log_text(error.stderr.strip() or error.stdout.strip())
        suffix = f": {detail}" if detail else ""
        raise BackupPackagingError(
            f"Comando {command[0]} encerrou com codigo {error.returncode}{suffix}."
        ) from error
