from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path

from cotacoes_ceasa.cli.commands.source import resolve_source_operation, run_source
from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import format_incremental_history, format_quotes_back
from cotacoes_ceasa.cli.progress import ProgressReporter
from cotacoes_ceasa.config import AppConfig
from cotacoes_ceasa.sources.registry import history_requested, source_supports_history
from cotacoes_ceasa.workflows.collection import PartialDownloadError


@dataclass(frozen=True)
class BufferedOutputEvent:
    kind: str
    message: str = ""
    level: str = ""
    counted: bool = False
    report: bool = True
    rows: tuple[tuple[str, object], ...] = ()
    report_title: str | None = None


@dataclass
class BufferedOutput:
    """Captura a saida de uma fonte executada em worker."""

    events: list[BufferedOutputEvent] = field(default_factory=list)
    use_colors: bool = False

    @property
    def supports_live_progress(self) -> bool:
        return False

    def header(
        self,
        operation: str,
        details: tuple[tuple[str, object], ...] = (),
    ) -> None:
        self.events.append(
            BufferedOutputEvent("header", message=operation, rows=tuple(details))
        )

    def section(self, title: str) -> None:
        self.events.append(BufferedOutputEvent("section", message=title))

    def info(self, message: str) -> None:
        self._message("INFO", message, counted=True)

    def success(self, message: str) -> None:
        self._message("OK", message, counted=True)

    def detail_success(self, message: str, report: bool = False) -> None:
        self._message("OK", message, counted=False, report=report)

    def warning(self, message: str) -> None:
        self._message("AVISO", message, counted=True)

    def error(self, message: str) -> None:
        self._message("ERRO", message, counted=True)

    def progress(self, message: str, visible: bool = True) -> None:
        return

    def summary(
        self,
        rows: tuple[tuple[str, object], ...] = (),
        report_title: str | None = None,
    ) -> None:
        self.report_summary(rows, report_title)

    def report_summary(
        self,
        rows: tuple[tuple[str, object], ...],
        report_title: str | None = None,
    ) -> None:
        self.events.append(
            BufferedOutputEvent(
                "summary",
                rows=tuple(rows),
                report_title=report_title,
            )
        )

    def _message(
        self,
        level: str,
        message: str,
        counted: bool,
        report: bool = True,
    ) -> None:
        self.events.append(
            BufferedOutputEvent(
                "message",
                message=message,
                level=level,
                counted=counted,
                report=report,
            )
        )


@dataclass(frozen=True)
class SourceRunResult:
    source_slug: str
    status: str
    files: tuple[Path, ...] = ()
    events: tuple[BufferedOutputEvent, ...] = ()
    error_type: str | None = None
    error_message: str | None = None


def run_all_sources(args, config: AppConfig, output: TerminalOutput) -> None:
    """Executa a operacao solicitada para todas as fontes configuradas."""
    if args.download_and_process:
        download_args = copy(args)
        download_args.download_and_process = False
        download_args.download_only = True
        download_args.process_raw = False
        download_args.save = False
        downloaded_files = run_all_sources_phase(
            download_args,
            config,
            output,
            "Download",
        )

        process_args = copy(args)
        process_args.download_and_process = False
        process_args.download_only = False
        process_args.process_raw = True
        process_args.save = False
        run_all_sources_phase(
            process_args,
            config,
            output,
            "Persistencia",
            raw_files_by_source=downloaded_files,
        )
        return

    run_all_sources_phase(args, config, output, resolve_source_operation(args))


def run_all_sources_phase(
    args,
    config: AppConfig,
    output: TerminalOutput,
    phase_name: str,
    raw_files_by_source: dict[str, list[Path]] | None = None,
) -> dict[str, list[Path]]:
    """Executa uma fase para todas as fontes sem interromper o lote."""
    output.header(
        f"{phase_name} de todas as fontes",
        _build_phase_details(
            args,
            config,
        ),
    )
    completed_count = 0
    failed_count = 0
    skipped_count = 0
    downloaded_files: dict[str, list[Path]] = {}

    if _should_run_parallel_download(args, config, raw_files_by_source):
        (
            completed_count,
            failed_count,
            skipped_count,
            downloaded_files,
        ) = _run_parallel_download_phase(args, config, output, phase_name)
    else:
        (
            completed_count,
            failed_count,
            skipped_count,
            downloaded_files,
        ) = _run_sequential_sources_phase(
            args,
            config,
            output,
            phase_name,
            raw_files_by_source,
        )

    output.summary(
        (
            ("Fontes concluidas", completed_count),
            ("Fontes com falha", failed_count),
            ("Fontes ignoradas", skipped_count),
        ),
        report_title=f"{phase_name} de todas as fontes",
    )
    return downloaded_files


def _build_phase_details(
    args,
    config: AppConfig,
) -> tuple[tuple[str, object], ...]:
    details: list[tuple[str, object]] = [
        ("Fontes configuradas", len(config.sources)),
        ("Data limite", args.target_date or "ultima disponivel"),
        ("Cotacoes anteriores", format_quotes_back(args.quotes_back)),
        (
            "Historico incremental",
            format_incremental_history(
                config.incremental_history and not args.process_raw,
                args.target_date,
                args.quotes_back,
            ),
        ),
    ]

    if args.download_only:
        effective_workers = min(args.workers, len(config.sources))
        details.append(
            (
                "Workers de download",
                (
                    f"{effective_workers} de {args.workers} solicitado(s)"
                    if effective_workers != args.workers
                    else args.workers
                ),
            )
        )
        details.append(
            (
                "Download paralelo",
                "sim" if effective_workers > 1 else "nao",
            )
        )

    return tuple(details)


def _should_run_parallel_download(
    args,
    config: AppConfig,
    raw_files_by_source: dict[str, list[Path]] | None,
) -> bool:
    return (
        raw_files_by_source is None
        and args.download_only
        and args.workers > 1
        and len(config.sources) > 1
    )


def _run_sequential_sources_phase(
    args,
    config: AppConfig,
    output: TerminalOutput,
    phase_name: str,
    raw_files_by_source: dict[str, list[Path]] | None,
) -> tuple[int, int, int, dict[str, list[Path]]]:
    completed_count = 0
    failed_count = 0
    skipped_count = 0
    downloaded_files: dict[str, list[Path]] = {}

    with ProgressReporter(output) as progress:
        progress_task = progress.task(
            label=phase_name,
            total=len(config.sources),
            unit="fonte(s)",
        )

        for source_slug in config.sources:
            progress_task.update(current=source_slug)

            if (
                raw_files_by_source is not None
                and source_slug not in raw_files_by_source
            ):
                skipped_count += 1
                output.warning(
                    f"{source_slug} | persistencia ignorada porque o download falhou."
                )
                progress_task.advance(current=source_slug)
                continue

            source_args = _build_source_args(args, source_slug, output)

            try:
                source_files = run_source(
                    source_args,
                    config,
                    output,
                    show_summary=False,
                    raw_files=(
                        raw_files_by_source.get(source_slug, [])
                        if raw_files_by_source is not None
                        else None
                    ),
                )
            except PartialDownloadError as error:
                failed_count += 1
                output.warning(
                    f"{source_slug} | {type(error.original_error).__name__}: "
                    f"{error.original_error}"
                )
                downloaded_files[source_slug] = error.saved_files
                output.warning(
                    f"{source_slug} | {len(error.saved_files)} arquivo(s) baixado(s) "
                    "antes da falha serao processados na persistencia."
                )
            except Exception as error:
                failed_count += 1
                output.warning(f"{source_slug} | {type(error).__name__}: {error}")
            else:
                completed_count += 1
                if source_files is not None:
                    downloaded_files[source_slug] = source_files
            finally:
                progress_task.advance(current=source_slug)

        progress_task.finish()

    return completed_count, failed_count, skipped_count, downloaded_files


def _run_parallel_download_phase(
    args,
    config: AppConfig,
    output: TerminalOutput,
    phase_name: str,
) -> tuple[int, int, int, dict[str, list[Path]]]:
    completed_count = 0
    failed_count = 0
    downloaded_files: dict[str, list[Path]] = {}
    effective_workers = min(args.workers, len(config.sources))

    output.info(
        f"{phase_name} paralelo iniciado com "
        f"{effective_workers} worker(s) para {len(config.sources)} fonte(s)."
    )

    with ProgressReporter(output) as progress:
        progress_task = progress.task(
            label=phase_name,
            total=len(config.sources),
            unit="fonte(s)",
        )

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            futures = {
                executor.submit(
                    _run_source_download_worker,
                    source_slug,
                    args,
                    config,
                ): source_slug
                for source_slug in config.sources
            }

            try:
                for future in as_completed(futures):
                    source_slug = futures[future]
                    progress_task.update(current=source_slug)
                    result = _resolve_source_future(future, source_slug)
                    _replay_buffered_events(output, result.events)

                    if result.status == "completed":
                        completed_count += 1
                        downloaded_files[result.source_slug] = list(result.files)
                    elif result.status == "partial":
                        failed_count += 1
                        downloaded_files[result.source_slug] = list(result.files)
                    else:
                        failed_count += 1
                        if not result.events:
                            output.warning(
                                f"{result.source_slug} | {result.error_type}: "
                                f"{result.error_message}"
                            )

                    progress_task.advance(current=result.source_slug)
            except KeyboardInterrupt:
                for future in futures:
                    future.cancel()
                raise

        progress_task.finish()

    return completed_count, failed_count, 0, downloaded_files


def _run_source_download_worker(
    source_slug: str,
    args,
    config: AppConfig,
) -> SourceRunResult:
    output = BufferedOutput()

    try:
        source_args = _build_source_args(args, source_slug, output)
        source_files = run_source(
            source_args,
            config,
            output,
            show_summary=False,
        )
    except PartialDownloadError as error:
        output.warning(
            f"{source_slug} | {type(error.original_error).__name__}: "
            f"{error.original_error}"
        )
        output.warning(
            f"{source_slug} | {len(error.saved_files)} arquivo(s) baixado(s) "
            "antes da falha serao processados na persistencia."
        )
        return SourceRunResult(
            source_slug=source_slug,
            status="partial",
            files=tuple(error.saved_files),
            events=tuple(output.events),
            error_type=type(error.original_error).__name__,
            error_message=str(error.original_error),
        )
    except Exception as error:
        output.warning(f"{source_slug} | {type(error).__name__}: {error}")
        return SourceRunResult(
            source_slug=source_slug,
            status="failed",
            events=tuple(output.events),
            error_type=type(error).__name__,
            error_message=str(error),
        )

    return SourceRunResult(
        source_slug=source_slug,
        status="completed",
        files=tuple(source_files or ()),
        events=tuple(output.events),
    )


def _resolve_source_future(future, source_slug: str) -> SourceRunResult:
    try:
        return future.result()
    except Exception as error:
        return SourceRunResult(
            source_slug=source_slug,
            status="failed",
            error_type=type(error).__name__,
            error_message=str(error),
        )


def _build_source_args(args, source_slug: str, output) -> object:
    source_args = copy(args)
    source_args.source = source_slug

    if (
        history_requested(source_args.quotes_back)
        and not source_supports_history(source_slug)
    ):
        source_args.quotes_back = 0

        if not source_args.process_raw:
            output.info(
                f"{source_slug} | fonte sem historico; coletando somente a atual."
            )

    return source_args


def _replay_buffered_events(
    output: TerminalOutput,
    events: tuple[BufferedOutputEvent, ...],
) -> None:
    for event in events:
        if event.kind == "header":
            output.header(event.message, event.rows)
        elif event.kind == "section":
            output.section(event.message)
        elif event.kind == "summary":
            output.report_summary(event.rows, event.report_title)
        elif event.kind == "message":
            _replay_buffered_message(output, event)


def _replay_buffered_message(
    output: TerminalOutput,
    event: BufferedOutputEvent,
) -> None:
    if event.level == "INFO":
        output.info(event.message)
    elif event.level == "OK" and event.counted:
        output.success(event.message)
    elif event.level == "OK":
        output.detail_success(event.message, report=event.report)
    elif event.level == "AVISO":
        output.warning(event.message)
    else:
        output.error(event.message)
