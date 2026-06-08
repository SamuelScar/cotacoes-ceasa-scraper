import os
import sys
from dataclasses import dataclass, field
from typing import Iterable


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

    warning_count: int = 0
    error_count: int = 0
    use_colors: bool = field(default_factory=lambda: _supports_colors(sys.stdout))
    _section_count: int = field(default=0, init=False, repr=False)

    def header(
        self,
        operation: str,
        details: Iterable[tuple[str, object]] = (),
    ) -> None:
        self._print(self._color("=" * LINE_WIDTH, CYAN))
        self._print(self._color("COTACOES CEASA", BOLD + CYAN))
        self._print(self._color("=" * LINE_WIDTH, CYAN))
        self._print_rows((("Operacao", operation), *details))
        self._print()

    def section(self, title: str) -> None:
        if self._section_count:
            self._print()

        line = f"-- {title} " + "-" * max(LINE_WIDTH - len(title) - 4, 0)
        self._print(self._color(line, BOLD + BLUE))
        self._section_count += 1

    def info(self, message: str) -> None:
        self._message("INFO", message)

    def success(self, message: str) -> None:
        self._message("OK", message)

    def warning(self, message: str) -> None:
        self.warning_count += 1
        self._message("AVISO", message)

    def error(self, message: str) -> None:
        self.error_count += 1
        self._message("ERRO", message)

    def summary(self, rows: Iterable[tuple[str, object]] = ()) -> None:
        if not self._section_count:
            self._print()

        self.section("Resumo")

        if self.error_count:
            self._message("ERRO", "Execucao encerrada com erro.")
        elif self.warning_count:
            self._message(
                "AVISO",
                f"Execucao concluida com {self.warning_count} aviso(s).",
            )
        else:
            self._message("OK", "Execucao concluida sem avisos.")

        self._print_rows(rows)

    def _message(self, level: str, message: str) -> None:
        label = self._color(f"[{level:<5}]", LEVEL_COLORS[level] + BOLD)
        self._print(f"{label} {message}")

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
