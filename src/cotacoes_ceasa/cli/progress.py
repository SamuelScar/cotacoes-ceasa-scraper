from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from cotacoes_ceasa.cli.output import TerminalOutput


@dataclass
class ProgressReporter:
    output: TerminalOutput
    _progress: Progress | None = field(default=None, init=False, repr=False)
    _owns_progress: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> ProgressReporter:
        if not self.output.supports_live_progress:
            return self

        active_progress = getattr(self.output, "_active_rich_progress", None)

        if active_progress is not None:
            self._progress = active_progress
            return self

        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=self.output.console,
            transient=True,
        )
        self._progress.start()
        self._owns_progress = True
        setattr(self.output, "_active_rich_progress", self._progress)

        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if not self._owns_progress or self._progress is None:
            return

        self._progress.stop()
        setattr(self.output, "_active_rich_progress", None)

    def task(self, label: str, total: int | None, unit: str) -> ProgressTask:
        task = ProgressTask(
            output=self.output,
            progress=self._progress,
            label=label,
            total=total,
            unit=unit,
            live=self._progress is not None,
        )
        task.start()

        return task


@dataclass
class ProgressTask:
    output: TerminalOutput
    progress: Progress | None
    label: str
    total: int | None
    unit: str
    live: bool
    completed: int = 0
    current: str | None = None
    _started_at: float = field(default_factory=monotonic, init=False, repr=False)
    _task_id: TaskID | None = field(default=None, init=False, repr=False)
    _last_logged_percent: int = field(default=-1, init=False, repr=False)

    def start(self) -> None:
        if self.progress is not None:
            self._task_id = self.progress.add_task(
                self._description(),
                total=self.total,
            )

        self._log_progress(force=True)

    def update(self, current: str | None = None) -> None:
        if current is not None:
            self.current = current

        if self.progress is not None and self._task_id is not None:
            self.progress.update(self._task_id, description=self._description())

    def advance(self, step: int = 1, current: str | None = None) -> None:
        if current is not None:
            self.current = current

        self.completed += step

        if self.progress is not None and self._task_id is not None:
            self.progress.update(
                self._task_id,
                advance=step,
                description=self._description(),
            )

        self._log_progress()

    def finish(self) -> None:
        if self.total is not None and self.completed < self.total:
            remaining = self.total - self.completed
            self.advance(remaining)

        self._log_progress(force=True)

        if self.progress is not None and self._task_id is not None:
            self.progress.remove_task(self._task_id)

    def _description(self) -> str:
        if self.current:
            return f"{self.label}: {self.current}"

        return self.label

    def _log_progress(self, force: bool = False) -> None:
        if not self._should_log_milestone(force):
            return

        self.output.progress(self._message(), visible=not self.live)

    def _should_log_milestone(self, force: bool) -> bool:
        if self.total is None or self.total <= 0:
            return force

        milestone = self._current_milestone()

        if milestone <= self._last_logged_percent:
            return False

        self._last_logged_percent = milestone

        return force or milestone % 10 == 0

    def _current_milestone(self) -> int:
        if self.total is None or self.total <= 0:
            return 0

        percent = int((self.completed / self.total) * 100)

        return (percent // 10) * 10

    def _message(self) -> str:
        parts = [f"Progresso | {self.label}", self._count_label()]

        if self.current:
            parts.append(f"atual: {self.current}")

        parts.append(f"decorrido: {_format_duration(self._elapsed_seconds())}")

        estimated_seconds = self._estimated_remaining_seconds()

        if estimated_seconds is not None:
            parts.append(f"estimado: {_format_duration(estimated_seconds)}")

        return " | ".join(parts)

    def _count_label(self) -> str:
        if self.total is None:
            return f"{self.completed} {self.unit}"

        percent = (self.completed / self.total * 100) if self.total else 100

        return f"{self.completed}/{self.total} {self.unit} ({percent:.0f}%)"

    def _elapsed_seconds(self) -> float:
        return monotonic() - self._started_at

    def _estimated_remaining_seconds(self) -> float | None:
        if self.total is None or self.completed <= 0 or self.completed >= self.total:
            return None

        elapsed = self._elapsed_seconds()
        rate = elapsed / self.completed

        return rate * (self.total - self.completed)


def _format_duration(seconds: float) -> str:
    total_seconds = max(int(seconds), 0)

    if total_seconds < 60:
        return f"{total_seconds}s"

    minutes, remaining_seconds = divmod(total_seconds, 60)

    if minutes < 60:
        return f"{minutes}min{remaining_seconds:02d}s"

    hours, remaining_minutes = divmod(minutes, 60)

    return f"{hours}h{remaining_minutes:02d}min"
