from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ReportEvent:
    occurred_at: datetime
    event_type: str
    label: str
    detail: str
    counted: bool = False


@dataclass(frozen=True)
class ReportSummary:
    title: str
    details: tuple[tuple[str, str], ...]
    rows: tuple[tuple[str, str], ...]


@dataclass
class CollectionReport:
    """Registra uma execucao de coleta e gera seu relatorio detalhado."""

    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    events: list[ReportEvent] = field(default_factory=list)
    summaries: list[ReportSummary] = field(default_factory=list)
    configuration: tuple[tuple[str, str], ...] = ()
    message_counts: dict[str, int] = field(
        default_factory=lambda: {
            "INFO": 0,
            "OK": 0,
            "AVISO": 0,
            "ERRO": 0,
        }
    )
    _current_operation: str = field(default="Coleta", init=False, repr=False)
    _current_details: tuple[tuple[str, str], ...] = field(
        default=(),
        init=False,
        repr=False,
    )

    def record_operation(
        self,
        operation: str,
        details: Iterable[tuple[str, object]],
    ) -> None:
        prepared_details = _prepare_rows(details)
        detail = "; ".join(f"{label}: {value}" for label, value in prepared_details)
        self._current_operation = operation
        self._current_details = prepared_details
        self._record_event("OPERACAO", operation, detail)

    def record_configuration(self, rows: Iterable[tuple[str, object]]) -> None:
        self.configuration = _prepare_rows(rows)

    def record_section(self, title: str) -> None:
        self._record_event("SECAO", title, "")

    def record_message(self, level: str, message: str, counted: bool) -> None:
        if counted:
            self.message_counts[level] += 1

        self._record_event("MENSAGEM", level, message, counted)

    def record_summary(
        self,
        rows: Iterable[tuple[str, object]],
        title: str | None = None,
    ) -> None:
        prepared_rows = _prepare_rows(rows)

        if not prepared_rows:
            return

        summary_title = title or self._current_operation
        summary_details = () if title else self._current_details
        self.summaries.append(
            ReportSummary(summary_title, summary_details, prepared_rows)
        )

        for label, value in prepared_rows:
            self._record_event("RESUMO", label, value)

    def write(self, report_dir: Path) -> Path:
        finished_at = datetime.now().astimezone()
        report_dir.mkdir(parents=True, exist_ok=True)
        file_path = report_dir / (
            f"coleta_{self.started_at.strftime('%Y%m%d_%H%M%S_%f')}.md"
        )
        file_path.write_text(
            self._build_markdown(finished_at),
            encoding="utf-8",
        )

        return file_path

    def _record_event(
        self,
        event_type: str,
        label: str,
        detail: str,
        counted: bool = False,
    ) -> None:
        self.events.append(
            ReportEvent(
                occurred_at=datetime.now().astimezone(),
                event_type=event_type,
                label=label,
                detail=detail,
                counted=counted,
            )
        )

    def _build_markdown(self, finished_at: datetime) -> str:
        duration_seconds = (finished_at - self.started_at).total_seconds()
        warning_count = self.message_counts["AVISO"]
        error_count = self.message_counts["ERRO"]
        status = (
            "Encerrada com erro"
            if error_count
            else "Concluida com avisos"
            if warning_count
            else "Concluida sem avisos"
        )
        operation_count = sum(
            event.event_type == "OPERACAO" for event in self.events
        )
        lines = [
            "# Relatorio de coleta",
            "",
            "## Resumo executivo",
            "",
            f"- Inicio: `{self.started_at.isoformat(timespec='seconds')}`",
            f"- Fim: `{finished_at.isoformat(timespec='seconds')}`",
            f"- Duracao: `{duration_seconds:.2f} segundos`",
            f"- Status: **{status}**",
            f"- Operacoes iniciadas: **{operation_count}**",
            f"- Informacoes: **{self.message_counts['INFO']}**",
            f"- Acertos (mensagens OK): **{self.message_counts['OK']}**",
            f"- Avisos: **{warning_count}**",
            f"- Erros: **{error_count}**",
            "",
        ]
        self._append_consolidated_results(lines)
        self._append_main_alerts(lines)
        self._append_configuration(lines)
        self._append_summaries(lines)
        self._append_messages(lines, "Avisos", "AVISO")
        self._append_messages(lines, "Erros", "ERRO")
        self._append_history(lines)

        return "\n".join(lines).rstrip() + "\n"

    def _append_consolidated_results(self, lines: list[str]) -> None:
        results = self._build_consolidated_results()
        lines.extend(["### Resultados consolidados", ""])

        if not results:
            lines.extend(["Nenhum resultado numerico foi concluido.", ""])
            return

        lines.extend(["| Metrica | Total |", "| --- | ---: |"])

        for label, value in results:
            lines.append(f"| {_escape_table(label)} | {value} |")

        lines.extend(
            [
                "",
                "Os totais acima somam os resultados registrados por operacao.",
                "",
            ]
        )

    def _build_consolidated_results(self) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}

        for summary in self.summaries:
            for label, value in summary.rows:
                try:
                    numeric_value = int(value)
                except ValueError:
                    continue

                totals[label] = totals.get(label, 0) + numeric_value

        return list(totals.items())

    def _append_main_alerts(self, lines: list[str]) -> None:
        alerts = [
            event
            for event in self.events
            if event.event_type == "MENSAGEM"
            and event.label in {"AVISO", "ERRO"}
            and event.counted
        ]
        lines.extend(["### Alertas principais", ""])

        if not alerts:
            lines.extend(["Nenhum aviso ou erro registrado.", ""])
            return

        alert_counts = Counter(
            (event.label, _summarize_alert_reason(event.detail))
            for event in alerts
        )
        grouped_alerts: dict[tuple[str, str], list[str]] = {}

        for event in alerts:
            reason = _summarize_alert_reason(event.detail)
            scopes = grouped_alerts.setdefault((event.label, reason), [])
            scope = event.detail.split(" | ", 1)[0]

            if scope not in scopes and len(scopes) < 3:
                scopes.append(scope)

        sorted_alerts = sorted(
            alert_counts.items(),
            key=lambda item: (
                item[0][0] != "ERRO",
                -item[1],
                item[0][1],
            ),
        )
        lines.extend(
            [
                "| Tipo | Motivo | Ocorrencias | Exemplos |",
                "| --- | --- | ---: | --- |",
            ]
        )

        for (level, reason), occurrence_count in sorted_alerts:
            examples = ", ".join(grouped_alerts[(level, reason)])
            lines.append(
                f"| {level} | {_escape_table(reason)} | {occurrence_count} | "
                f"{_escape_table(examples)} |"
            )

        lines.extend(
            [
                "",
                "Os alertas completos permanecem nas secoes detalhadas abaixo.",
                "",
            ]
        )

    def _append_configuration(self, lines: list[str]) -> None:
        lines.extend(["## Configuracao utilizada", ""])

        if not self.configuration:
            lines.extend(["Nenhuma configuracao foi registrada.", ""])
            return

        lines.extend(["| Configuracao | Valor efetivo |", "| --- | --- |"])

        for label, value in self.configuration:
            lines.append(f"| {_escape_table(label)} | `{_escape_table(value)}` |")

        lines.append("")

    def _append_summaries(self, lines: list[str]) -> None:
        lines.extend(["## Resultados por operacao", ""])

        if not self.summaries:
            lines.extend(["Nenhum resultado numerico foi registrado.", ""])
            return

        for index, summary in enumerate(self.summaries, start=1):
            lines.extend([f"### {index}. {summary.title}", ""])

            for label, value in summary.details:
                lines.append(f"- {label}: `{value}`")

            if summary.details:
                lines.append("")

            lines.extend(["| Metrica | Valor |", "| --- | ---: |"])

            for label, value in summary.rows:
                lines.append(f"| {_escape_table(label)} | {_escape_table(value)} |")

            lines.append("")

    def _append_messages(self, lines: list[str], title: str, level: str) -> None:
        messages = [
            event
            for event in self.events
            if event.event_type == "MENSAGEM"
            and event.label == level
            and event.counted
        ]
        lines.extend([f"## {title}", ""])

        if not messages:
            lines.extend([f"Nenhum {title.lower()[:-1]} registrado.", ""])
            return

        for event in messages:
            lines.append(
                f"- `{event.occurred_at.isoformat(timespec='seconds')}` "
                f"{event.detail}"
            )

        lines.append("")

    def _append_history(self, lines: list[str]) -> None:
        lines.extend(["## Historico completo", ""])

        for event in self.events:
            detail = f" | {event.detail}" if event.detail else ""
            lines.append(
                f"- `{event.occurred_at.isoformat(timespec='seconds')}` "
                f"**{event.event_type} - {event.label}**{detail}"
            )


def _prepare_rows(
    rows: Iterable[tuple[str, object]],
) -> tuple[tuple[str, str], ...]:
    return tuple((label, str(value)) for label, value in rows)


def _escape_table(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")


def _summarize_alert_reason(detail: str) -> str:
    reason = detail.rsplit(" | ", 1)[-1]

    if reason.startswith("Invalid Elementary Object"):
        return "PDF invalido: Invalid Elementary Object"

    return reason
