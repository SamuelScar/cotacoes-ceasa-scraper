import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from cotacoes_ceasa.cli.report import CollectionReport


LINE_WIDTH = 72
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

LEVEL_COLORS = {
    "INFO": CYAN,
    "OK": GREEN,
    "AVISO": YELLOW,
    "ERRO": RED,
}


@dataclass
class TerminalOutput:
    """Padroniza mensagens e resumos exibidos pela CLI."""

    info_count: int = 0
    success_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    use_colors: bool = field(default_factory=lambda: _supports_colors(sys.stdout))
    _section_count: int = field(default=0, init=False, repr=False)
    _collection_report: CollectionReport | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def enable_collection_report(
        self,
        configuration: Iterable[tuple[str, object]] = (),
    ) -> None:
        self._collection_report = CollectionReport()
        self._collection_report.record_configuration(configuration)

    @property
    def collection_report_enabled(self) -> bool:
        return self._collection_report is not None

    def write_collection_report(self, report_dir: Path) -> Path:
        if self._collection_report is None:
            raise RuntimeError("Relatorio de coleta nao foi habilitado.")

        return self._collection_report.write(report_dir)

    def report_saved(self, file_path: Path) -> None:
        label = self._color(f"[{'OK':<5}]", GREEN + BOLD)
        self._print(f"{label} Relatorio salvo em {file_path}.")

    def header(
        self,
        operation: str,
        details: Iterable[tuple[str, object]] = (),
    ) -> None:
        prepared_details = tuple(details)

        if self._collection_report is not None:
            self._collection_report.record_operation(operation, prepared_details)

        self._print(self._color("=" * LINE_WIDTH, CYAN))
        self._print(self._color("COTACOES CEASA", BOLD + CYAN))
        self._print(self._color("=" * LINE_WIDTH, CYAN))
        self._print_rows((("Operacao", operation), *prepared_details))
        self._print()

    def section(self, title: str) -> None:
        if self._collection_report is not None:
            self._collection_report.record_section(title)

        if self._section_count:
            self._print()

        line = f"-- {title} " + "-" * max(LINE_WIDTH - len(title) - 4, 0)
        self._print(self._color(line, BOLD + BLUE))
        self._section_count += 1

    def info(self, message: str) -> None:
        self._message("INFO", message, counted=True)

    def success(self, message: str) -> None:
        self._message("OK", message, counted=True)

    def warning(self, message: str) -> None:
        self._message("AVISO", message, counted=True)

    def error(self, message: str) -> None:
        self._message("ERRO", message, counted=True)

    def summary(
        self,
        rows: Iterable[tuple[str, object]] = (),
        report_title: str | None = None,
    ) -> None:
        prepared_rows = tuple(rows)

        if not self._section_count:
            self._print()

        self.section("Resumo")

        if self.error_count:
            self._message("ERRO", "Execucao encerrada com erro.", counted=False)
        elif self.warning_count:
            self._message(
                "AVISO",
                f"Execucao concluida com {self.warning_count} aviso(s).",
                counted=False,
            )
        else:
            self._message("OK", "Execucao concluida sem avisos.", counted=False)

        self.report_summary(prepared_rows, report_title)
        self._print_rows(prepared_rows)

    def report_summary(
        self,
        rows: Iterable[tuple[str, object]],
        report_title: str | None = None,
    ) -> None:
        if self._collection_report is not None:
            self._collection_report.record_summary(rows, report_title)

    def _message(self, level: str, message: str, counted: bool) -> None:
        if counted:
            self._increment_count(level)

        if self._collection_report is not None:
            self._collection_report.record_message(level, message, counted)

        label = self._color(f"[{level:<5}]", LEVEL_COLORS[level] + BOLD)
        self._print(f"{label} {message}")

    def _increment_count(self, level: str) -> None:
        if level == "INFO":
            self.info_count += 1
        elif level == "OK":
            self.success_count += 1
        elif level == "AVISO":
            self.warning_count += 1
        else:
            self.error_count += 1

    def _print_rows(self, rows: Iterable[tuple[str, object]]) -> None:
        prepared_rows = [(label, str(value)) for label, value in rows]

        if not prepared_rows:
            return

        label_width = max(len(label) for label, _ in prepared_rows)

        for label, value in prepared_rows:
            padded_label = self._color(f"{label:<{label_width}}", BOLD)
            separator = self._color(":", DIM)
            self._print(f"{padded_label} {separator} {value}")

    def _color(self, text: str, color: str) -> str:
        return f"{color}{text}{RESET}" if self.use_colors else text

    def _print(self, text: str = "") -> None:
        print(text, flush=True)


def _supports_colors(stream) -> bool:
    return (
        stream.isatty()
        and os.getenv("TERM") != "dumb"
        and "NO_COLOR" not in os.environ
    )
