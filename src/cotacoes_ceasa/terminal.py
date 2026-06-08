from dataclasses import dataclass, field
from typing import Iterable


LINE_WIDTH = 72


@dataclass
class TerminalOutput:
    """Padroniza mensagens e resumos exibidos pela CLI."""

    warning_count: int = 0
    error_count: int = 0
    _section_count: int = field(default=0, init=False, repr=False)

    def header(
        self,
        operation: str,
        details: Iterable[tuple[str, object]] = (),
    ) -> None:
        print("=" * LINE_WIDTH)
        print("COTACOES CEASA")
        print("=" * LINE_WIDTH)
        self._print_rows((("Operacao", operation), *details))
        print()

    def section(self, title: str) -> None:
        if self._section_count:
            print()

        print(f"-- {title} " + "-" * max(LINE_WIDTH - len(title) - 4, 0))
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
            print()

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
        print(f"[{level:<5}] {message}")

    def _print_rows(self, rows: Iterable[tuple[str, object]]) -> None:
        prepared_rows = [(label, str(value)) for label, value in rows]

        if not prepared_rows:
            return

        label_width = max(len(label) for label, _ in prepared_rows)

        for label, value in prepared_rows:
            print(f"{label:<{label_width}} : {value}")
