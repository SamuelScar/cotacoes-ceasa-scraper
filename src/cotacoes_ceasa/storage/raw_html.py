from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


@dataclass(frozen=True)
class RawArchiveResult:
    """Resultado de uma compactacao de HTMLs antigos."""

    source: str
    archive_path: Path
    archived_count: int


@dataclass(frozen=True)
class RawHtmlStorage:
    """Salva arquivos brutos para tratamento posterior."""

    base_dir: Path

    def save(self, source: str, category: str, html: str) -> Path:
        """Salva o HTML em um arquivo identificado por fonte, categoria e horario."""
        return self.save_text(source, category, html, "html")

    def save_text(
        self,
        source: str,
        category: str,
        content: str,
        extension: str,
    ) -> Path:
        """Salva conteudo textual bruto com a extensao informada."""
        directory = self.base_dir / source
        directory.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        self._archive_current_day_files(directory, category, now.date(), extension)

        timestamp = now.strftime("%Y%m%d_%H%M%S")
        file_path = directory / f"{category}_{timestamp}.{extension}"
        file_path.write_text(content, encoding="utf-8")

        return file_path

    def save_bytes(
        self,
        source: str,
        category: str,
        content: bytes,
        extension: str,
    ) -> Path:
        """Salva conteudo binario bruto com a extensao informada."""
        directory = self.base_dir / source
        directory.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        self._archive_current_day_files(directory, category, now.date(), extension)

        timestamp = now.strftime("%Y%m%d_%H%M%S")
        file_path = directory / f"{category}_{timestamp}.{extension}"
        file_path.write_bytes(content)

        return file_path

    def find_latest(self, source: str, category: str) -> Path | None:
        """Busca o HTML ativo mais recente da fonte e categoria."""
        return self.find_latest_file(source, category, "html")

    def find_latest_file(
        self,
        source: str,
        category: str,
        extension: str,
    ) -> Path | None:
        """Busca o arquivo ativo mais recente da fonte, categoria e extensao."""
        directory = self.base_dir / source

        if not directory.exists():
            return None

        raw_files = sorted(
            file_path
            for file_path in directory.glob(f"{category}_*.{extension}")
            if file_path.is_file()
        )

        if not raw_files:
            return None

        return raw_files[-1]

    def archive_old_html_files(self) -> list[RawArchiveResult]:
        """Compacta HTMLs soltos de `old` por fonte e remove os originais."""
        results: list[RawArchiveResult] = []

        if not self.base_dir.exists():
            return results

        for old_directory in sorted(self.base_dir.glob("*/old")):
            if not old_directory.is_dir():
                continue

            html_files = sorted(
                file_path
                for file_path in old_directory.glob("*.html")
                if file_path.is_file()
            )

            if not html_files:
                continue

            archive_path = self._build_archive_path(old_directory)

            with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
                for file_path in html_files:
                    archive.write(file_path, arcname=file_path.name)

            for file_path in html_files:
                file_path.unlink()

            results.append(
                RawArchiveResult(
                    source=old_directory.parent.name,
                    archive_path=archive_path,
                    archived_count=len(html_files),
                )
            )

        return results

    def _archive_current_day_files(
        self,
        directory: Path,
        category: str,
        current_date: date,
        extension: str,
    ) -> None:
        """Move raws anteriores do mesmo dia para a pasta `old`."""
        day_prefix = current_date.strftime("%Y%m%d")
        old_directory = directory / "old"

        for file_path in directory.glob(f"{category}_{day_prefix}_*.{extension}"):
            old_directory.mkdir(parents=True, exist_ok=True)
            file_path.rename(self._build_old_path(old_directory, file_path.name))

    def _build_old_path(self, old_directory: Path, file_name: str) -> Path:
        old_path = old_directory / file_name

        if not old_path.exists():
            return old_path

        stem = old_path.stem
        suffix = old_path.suffix
        counter = 1

        while True:
            candidate_path = old_directory / f"{stem}_{counter}{suffix}"

            if not candidate_path.exists():
                return candidate_path

            counter += 1

    def _build_archive_path(self, old_directory: Path) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = old_directory / f"htmls_{timestamp}.zip"

        if not archive_path.exists():
            return archive_path

        counter = 1

        while True:
            candidate_path = old_directory / f"htmls_{timestamp}_{counter}.zip"

            if not candidate_path.exists():
                return candidate_path

            counter += 1
