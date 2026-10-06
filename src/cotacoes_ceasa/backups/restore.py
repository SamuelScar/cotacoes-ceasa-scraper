"""Restauracao local, validada e atomica dos backups completos."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.execution import sanitize_log_text


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ZPAQ_LIST_ENTRY = re.compile(
    r"^-\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+"
    r"-?\d+\s+\S+\s+(?P<name>.+)$"
)


class BackupRestoreError(RuntimeError):
    """Indica que o pacote nao pode ser restaurado com seguranca."""


@dataclass(frozen=True)
class RestoreRequest:
    archive_path: Path
    project_root: Path
    manifest_path: Path | None = None
    expected_sha256: str | None = None
    compare_with: Path | None = None
    preserve_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class DataInventory:
    files: int
    bytes: int
    tree_sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RestoreResult:
    schema_version: int
    status: str
    archive_path: str
    format: str
    archive_sha256: str
    checksum_validation: str
    archive_validation: str
    manifest_status: str
    structure_validation: str
    sqlite_validation: str
    inventory_validation: str
    restored_inventory: DataInventory
    rollback_created: bool
    rollback_restored: bool
    duration_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def restore_backup(request: RestoreRequest) -> RestoreResult:
    """Valida o pacote e substitui ``data/`` sem exposicao parcial."""
    started_at = time.monotonic()
    archive = _resolve_regular_file(request.archive_path, "pacote")
    project_root = request.project_root.resolve(strict=True)
    if not project_root.is_dir():
        raise BackupRestoreError(
            f"A raiz do projeto nao e um diretorio: {project_root}."
        )
    archive_format = detect_archive_format(archive)
    archive_sha256 = calculate_sha256(archive)
    manifest_status, manifest_sha256, manifest_source = _manifest_expectations(
        request.manifest_path,
        archive.name,
    )
    expected_sha256 = _resolve_expected_sha256(
        request.expected_sha256,
        manifest_sha256,
    )
    checksum_validation = "calculated"
    if expected_sha256 is not None:
        if archive_sha256 != expected_sha256:
            raise BackupRestoreError(
                "O SHA-256 do pacote diverge do valor esperado."
            )
        checksum_validation = "passed"

    staging_root = Path(
        tempfile.mkdtemp(prefix=".data-restore-", dir=project_root)
    )
    rollback_path = project_root / f".data-rollback-{uuid.uuid4().hex}"
    rollback_created = False
    rollback_restored = False
    target_data = project_root / "data"
    try:
        _extract_validated_archive(archive, archive_format, staging_root)
        restored_data = staging_root / "data"
        _validate_restored_structure(staging_root, restored_data)
        _validate_sqlite(restored_data / "cotacoes.sqlite")
        restored_inventory = collect_data_inventory(restored_data)
        inventory_validation = _validate_inventory(
            restored_inventory,
            manifest_source,
            request.compare_with,
        )
        _preserve_current_paths(
            request.preserve_paths,
            target_data,
            restored_data,
        )

        if target_data.exists() or target_data.is_symlink():
            if target_data.is_symlink() or not target_data.is_dir():
                raise BackupRestoreError(
                    "O destino data/ atual nao e um diretorio regular."
                )
            target_data.replace(rollback_path)
            rollback_created = True
        try:
            restored_data.replace(target_data)
        except OSError as error:
            if rollback_created and rollback_path.exists():
                if target_data.exists() or target_data.is_symlink():
                    _remove_controlled_tree(target_data, project_root, "data")
                rollback_path.replace(target_data)
                rollback_restored = True
            raise BackupRestoreError(
                f"Falha ao publicar a restauracao: {sanitize_log_text(error)}"
            ) from error

        if rollback_created:
            _remove_controlled_tree(
                rollback_path,
                project_root,
                ".data-rollback-",
            )
        return RestoreResult(
            schema_version=1,
            status="completed",
            archive_path=archive.as_posix(),
            format=archive_format,
            archive_sha256=archive_sha256,
            checksum_validation=checksum_validation,
            archive_validation="passed",
            manifest_status=manifest_status,
            structure_validation="passed",
            sqlite_validation="passed",
            inventory_validation=inventory_validation,
            restored_inventory=restored_inventory,
            rollback_created=rollback_created,
            rollback_restored=rollback_restored,
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
    finally:
        if staging_root.exists():
            _remove_controlled_tree(
                staging_root,
                project_root,
                ".data-restore-",
            )


def detect_archive_format(archive: Path) -> str:
    name = archive.name.lower()
    for suffix, archive_format in (
        (".tar.xz", "tar.xz"),
        (".tar.gz", "tar.gz"),
        (".zpaq", "zpaq"),
        (".zip", "zip"),
    ):
        if name.endswith(suffix):
            return archive_format
    raise BackupRestoreError(
        f"Formato nao suportado: {archive.name}. "
        "Use .zip, .tar.xz, .zpaq ou .tar.gz."
    )


def collect_data_inventory(data_directory: Path) -> DataInventory:
    """Calcula inventario integral e hash deterministico da arvore."""
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for entry in sorted(data_directory.rglob("*")):
        relative = entry.relative_to(data_directory).as_posix()
        if entry.is_symlink():
            raise BackupRestoreError(
                f"A restauracao contem link simbolico: {relative}."
            )
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise BackupRestoreError(
                f"A restauracao contem entrada especial: {relative}."
            )
        file_size = entry.stat().st_size
        file_digest = calculate_sha256(entry)
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(str(file_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
        files += 1
        total_bytes += file_size
    return DataInventory(
        files=files,
        bytes=total_bytes,
        tree_sha256=digest.hexdigest(),
    )


def _extract_validated_archive(
    archive: Path,
    archive_format: str,
    staging_root: Path,
) -> None:
    if archive_format == "zip":
        _extract_zip(archive, staging_root)
    elif archive_format in {"tar.xz", "tar.gz"}:
        _extract_tar(archive, archive_format, staging_root)
    elif archive_format == "zpaq":
        _extract_zpaq(archive, staging_root)
    else:
        raise BackupRestoreError(f"Formato nao suportado: {archive_format}.")


def _extract_zip(archive: Path, staging_root: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as package:
            bad_member = package.testzip()
            if bad_member is not None:
                raise BackupRestoreError(
                    f"O ZIP falhou na validacao: {bad_member}."
                )
            seen: set[str] = set()
            for member in package.infolist():
                relative = _safe_archive_member(member.filename)
                _reject_duplicate_member(relative, seen)
                unix_mode = member.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise BackupRestoreError(
                        f"O ZIP contem link simbolico: {relative}."
                    )
                if member.flag_bits & 0x1:
                    raise BackupRestoreError(
                        f"O ZIP contem arquivo criptografado: {relative}."
                    )
                destination = staging_root.joinpath(*relative.parts)
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, BackupRestoreError):
            raise
        raise BackupRestoreError(
            f"Falha ao validar ou extrair ZIP: {sanitize_log_text(error)}"
        ) from error


def _extract_tar(
    archive: Path,
    archive_format: str,
    staging_root: Path,
) -> None:
    mode = "r:xz" if archive_format == "tar.xz" else "r:gz"
    try:
        with tarfile.open(archive, mode=mode) as package:
            seen: set[str] = set()
            for member in package:
                relative = _safe_archive_member(member.name)
                _reject_duplicate_member(relative, seen)
                if not (member.isdir() or member.isfile()):
                    raise BackupRestoreError(
                        f"O TAR contem link ou entrada especial: {relative}."
                    )
                destination = staging_root.joinpath(*relative.parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                source = package.extractfile(member)
                if source is None:
                    raise BackupRestoreError(
                        f"Nao foi possivel ler o membro TAR: {relative}."
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target)
    except (OSError, tarfile.TarError) as error:
        if isinstance(error, BackupRestoreError):
            raise
        raise BackupRestoreError(
            f"Falha ao validar ou extrair TAR: {sanitize_log_text(error)}"
        ) from error


def _extract_zpaq(archive: Path, staging_root: Path) -> None:
    executable = shutil.which("zpaq")
    if executable is None:
        raise BackupRestoreError("Ferramenta obrigatoria nao encontrada: zpaq.")
    listing = _run_zpaq([executable, "list", archive.as_posix()])
    names: list[str] = []
    for line in listing.splitlines():
        if not line.startswith("- "):
            continue
        match = ZPAQ_LIST_ENTRY.match(line)
        if match is None:
            raise BackupRestoreError(
                "Nao foi possivel validar uma entrada da listagem ZPAQ."
            )
        names.append(_safe_archive_member(match.group("name")).as_posix())
    if not names:
        raise BackupRestoreError("O ZPAQ nao possui arquivos restauraveis.")
    if len(names) != len(set(names)):
        raise BackupRestoreError("O ZPAQ possui caminhos duplicados.")
    _run_zpaq(
        [
            executable,
            "extract",
            archive.as_posix(),
            "-test",
            "-threads",
            "0",
        ]
    )
    _run_zpaq(
        [
            executable,
            "extract",
            archive.as_posix(),
            "-to",
            staging_root.as_posix(),
            "-threads",
            "0",
        ]
    )


def _run_zpaq(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = ""
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr.strip() or error.stdout.strip()
        suffix = f": {sanitize_log_text(detail)}" if detail else ""
        raise BackupRestoreError(f"Falha ao processar ZPAQ{suffix}.") from error
    return completed.stdout


def _safe_archive_member(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise BackupRestoreError("O pacote contem um caminho invalido.")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupRestoreError(f"Caminho inseguro no pacote: {name!r}.")
    if not path.parts or path.parts[0] != "data":
        raise BackupRestoreError(
            f"Entrada fora do diretorio data/: {name!r}."
        )
    return path


def _reject_duplicate_member(path: PurePosixPath, seen: set[str]) -> None:
    normalized = path.as_posix().rstrip("/")
    if normalized in seen:
        raise BackupRestoreError(f"Caminho duplicado no pacote: {normalized}.")
    seen.add(normalized)


def _validate_restored_structure(staging_root: Path, restored_data: Path) -> None:
    entries = list(staging_root.iterdir())
    if len(entries) != 1 or entries[0].name != "data":
        raise BackupRestoreError(
            "O pacote deve conter somente o diretorio raiz data/."
        )
    if restored_data.is_symlink() or not restored_data.is_dir():
        raise BackupRestoreError("O pacote nao contem um diretorio data/ valido.")
    database = restored_data / "cotacoes.sqlite"
    if database.is_symlink() or not database.is_file():
        raise BackupRestoreError(
            "O pacote nao contem data/cotacoes.sqlite regular."
        )
    collect_data_inventory(restored_data)


def _validate_sqlite(database: Path) -> None:
    database_uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(database_uri, uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise BackupRestoreError(
            f"Falha ao validar o SQLite restaurado: {sanitize_log_text(error)}"
        ) from error
    if not result or result[0] != "ok":
        raise BackupRestoreError("O SQLite restaurado falhou no integrity_check.")


def _manifest_expectations(
    manifest_path: Path | None,
    archive_name: str,
) -> tuple[str, str | None, dict[str, int] | None]:
    if manifest_path is None:
        return "not_provided", None, None
    manifest = _read_json(_resolve_regular_file(manifest_path, "manifesto"))
    layers = manifest.get("layers")
    if not isinstance(layers, dict):
        raise BackupRestoreError("O manifesto nao possui camadas validas.")
    matches: list[dict[str, Any]] = []
    for layer in layers.values():
        if not isinstance(layer, dict):
            continue
        upload_data = layer.get("upload")
        final_path = (
            upload_data.get("final_path")
            if isinstance(upload_data, dict)
            else None
        )
        if (
            layer.get("file_name") == archive_name
            and isinstance(final_path, str)
            and PurePosixPath(final_path).name == archive_name
        ):
            matches.append(layer)
    if len(matches) != 1:
        raise BackupRestoreError(
            "O manifesto nao identifica exatamente uma camada para o pacote."
        )
    layer = matches[0]
    package = layer.get("package")
    validation = layer.get("validation")
    upload = layer.get("upload")
    if not all(isinstance(item, dict) for item in (package, validation, upload)):
        raise BackupRestoreError("A camada do manifesto esta incompleta.")
    remote_directory = layer.get("remote_directory")
    if (
        not isinstance(remote_directory, str)
        or not _matches_manifest_remote_path(
            upload.get("final_path"),
            remote_directory,
            archive_name,
        )
    ):
        raise BackupRestoreError(
            "O caminho final da camada diverge do manifesto."
        )
    if (
        package.get("status") != "completed"
        or validation.get("status") != "passed"
        or upload.get("status") != "completed"
    ):
        raise BackupRestoreError(
            "A camada do manifesto nao foi publicada com sucesso."
        )
    sha256 = _normalize_sha256(package.get("sha256"), "manifesto")
    source = package.get("source") or manifest.get("source")
    if not isinstance(source, dict):
        raise BackupRestoreError("O manifesto nao possui metricas da origem.")
    source_files = source.get("files")
    source_bytes = source.get("bytes")
    if not _is_nonnegative_int(source_files) or not _is_nonnegative_int(
        source_bytes
    ):
        raise BackupRestoreError("As metricas da origem no manifesto sao invalidas.")
    return "validated", sha256, {"files": source_files, "bytes": source_bytes}


def _matches_manifest_remote_path(
    final_path: object,
    remote_directory: str,
    archive_name: str,
) -> bool:
    if not isinstance(final_path, str) or not final_path.strip():
        return False
    path = PurePosixPath(final_path)
    expected = PurePosixPath(remote_directory) / archive_name
    if path.is_absolute() or ".." in path.parts:
        return False
    if len(path.parts) < len(expected.parts):
        return False
    return path.parts[-len(expected.parts) :] == expected.parts


def _resolve_expected_sha256(
    explicit: str | None,
    manifest: str | None,
) -> str | None:
    explicit_sha = _normalize_sha256(explicit, "informado") if explicit else None
    if explicit_sha and manifest and explicit_sha != manifest:
        raise BackupRestoreError("O SHA-256 informado diverge do manifesto.")
    return explicit_sha or manifest


def _validate_inventory(
    restored: DataInventory,
    manifest_source: dict[str, int] | None,
    compare_with: Path | None,
) -> str:
    validations = 0
    if manifest_source is not None:
        if (
            restored.files != manifest_source["files"]
            or restored.bytes != manifest_source["bytes"]
        ):
            raise BackupRestoreError(
                "O inventario restaurado diverge das metricas do manifesto."
            )
        validations += 1
    if compare_with is not None:
        expected_directory = compare_with.resolve(strict=True)
        if not expected_directory.is_dir():
            raise BackupRestoreError(
                f"A origem de comparacao nao e um diretorio: {expected_directory}."
            )
        expected = collect_data_inventory(expected_directory)
        if restored != expected:
            raise BackupRestoreError(
                "A restauracao integral diverge da arvore de comparacao."
            )
        validations += 1
    return "passed" if validations else "calculated"


def _preserve_current_paths(
    paths: tuple[Path, ...],
    current_data: Path,
    restored_data: Path,
) -> None:
    """Copia artefatos da execucao atual sem alterar dados do pacote validado."""
    if not paths:
        return
    current_data_resolved = current_data.resolve(strict=True)
    for configured_path in paths:
        source = configured_path.resolve(strict=True)
        try:
            relative = source.relative_to(current_data_resolved)
        except ValueError as error:
            raise BackupRestoreError(
                f"O caminho preservado esta fora de data/: {configured_path}."
            ) from error
        if source.is_symlink() or not source.is_dir():
            raise BackupRestoreError(
                f"O caminho preservado nao e um diretorio regular: {source}."
            )
        collect_data_inventory(source)
        destination = restored_data / relative
        if destination.exists() or destination.is_symlink():
            raise BackupRestoreError(
                f"O pacote ja contem o caminho que seria preservado: {relative}."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=False)


def _resolve_regular_file(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_file():
        raise BackupRestoreError(f"O {label} nao e um arquivo regular: {path}.")
    return resolved


def _normalize_sha256(value: object, source: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.lower()):
        raise BackupRestoreError(f"O SHA-256 {source} e invalido.")
    return value.lower()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackupRestoreError(
            f"Manifesto invalido: {sanitize_log_text(error)}"
        ) from error
    if not isinstance(payload, dict):
        raise BackupRestoreError("O manifesto deve conter um objeto JSON.")
    return payload


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _remove_controlled_tree(path: Path, parent: Path, expected_name: str) -> None:
    if path.parent != parent:
        raise BackupRestoreError(f"Recusa ao remover caminho fora da raiz: {path}.")
    if expected_name == "data":
        allowed = path.name == expected_name
    else:
        allowed = path.name.startswith(expected_name)
    if not allowed or path.is_symlink():
        raise BackupRestoreError(f"Recusa ao remover caminho inseguro: {path}.")
    shutil.rmtree(path)
