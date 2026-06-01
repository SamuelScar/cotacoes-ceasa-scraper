from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class RawHtmlStorage:
    """Salva paginas HTML brutas para tratamento posterior."""

    base_dir: Path

    def save(self, source: str, category: str, html: str) -> Path:
        """Salva o HTML em um arquivo identificado por fonte, categoria e horario."""
        directory = self.base_dir / source
        directory.mkdir(parents=True, exist_ok=True)

        now = datetime.now()
        self._archive_current_day_files(directory, category, now.date())

        timestamp = now.strftime("%Y%m%d_%H%M%S")
        file_path = directory / f"{category}_{timestamp}.html"
        file_path.write_text(html, encoding="utf-8")

        return file_path

    def _archive_current_day_files(
        self,
        directory: Path,
        category: str,
        current_date: date,
    ) -> None:
        """Move raws anteriores do mesmo dia para a pasta `old`."""
        day_prefix = current_date.strftime("%Y%m%d")
        old_directory = directory / "old"

        for file_path in directory.glob(f"{category}_{day_prefix}_*.html"):
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
