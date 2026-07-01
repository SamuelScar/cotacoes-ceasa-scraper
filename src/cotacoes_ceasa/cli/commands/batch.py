from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

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
    download_started_at: float | None = None
    download_finished_at: float | None = None


@dataclass
class PipelineStats:
    download_completed: int = 0
    download_partial: int = 0
    download_failed: int = 0
    persistence_started: int = 0
    persistence_completed: int = 0
    persistence_failed: int = 0
    persistence_skipped: int = 0


@dataclass
class PipelineSourceMetric:
    source_slug: str
    status: str
    persistence_status: str
    files: int
    download_started_at: float | None = None
    download_finished_at: float | None = None
    download_seconds: float | None = None
    queue_wait_seconds: float | None = None
    persistence_seconds: float | None = None


@dataclass
class PipelineMetrics:
    started_at: float = field(default_factory=perf_counter)
    max_backlog: int = 0
    sources: list[PipelineSourceMetric] = field(default_factory=list)
    _finished_at: float | None = field(default=None, init=False, repr=False)

    def record_backlog(self, backlog_size: int) -> None:
        self.max_backlog = max(self.max_backlog, backlog_size)

    def record_source(
        self,
        result: SourceRunResult,
        persistence_status: str,
        queue_wait_seconds: float | None = None,
        persistence_seconds: float | None = None,
    ) -> None:
        self.sources.append(
            PipelineSourceMetric(
                source_slug=result.source_slug,
                status=result.status,
                persistence_status=persistence_status,
                files=len(result.files),
                download_started_at=result.download_started_at,
                download_finished_at=result.download_finished_at,
                download_seconds=_elapsed_seconds(
                    result.download_started_at,
                    result.download_finished_at,
                ),
                queue_wait_seconds=queue_wait_seconds,
                persistence_seconds=persistence_seconds,
            )
        )

    @property
    def download_span_seconds(self) -> float:
        starts = [
            source.download_started_at
            for source in self.sources
            if source.download_started_at is not None
        ]
        finishes = [
            source.download_finished_at
            for source in self.sources
            if source.download_finished_at is not None
        ]

        if not starts or not finishes:
            return 0.0

        return max(finishes) - min(starts)

    @property
    def total_download_seconds(self) -> float:
        return sum(source.download_seconds or 0.0 for source in self.sources)

    @property
    def total_queue_wait_seconds(self) -> float:
        return sum(source.queue_wait_seconds or 0.0 for source in self.sources)

    @property
    def max_queue_wait_seconds(self) -> float:
        waits = [
            source.queue_wait_seconds
            for source in self.sources
            if source.queue_wait_seconds is not None
        ]

        return max(waits) if waits else 0.0

    @property
    def total_persistence_seconds(self) -> float:
        return sum(source.persistence_seconds or 0.0 for source in self.sources)

    @property
    def files_sent_to_persistence(self) -> int:
        return sum(
            source.files
            for source in self.sources
            if source.persistence_seconds is not None
        )

    @property
    def total_seconds(self) -> float:
        finished_at = self.finished_at or perf_counter()

        return finished_at - self.started_at

    @property
    def finished_at(self) -> float | None:
        return self._finished_at

    def finish(self) -> None:
        self._finished_at = perf_counter()

    @property
    def no_overlap_estimate_seconds(self) -> float:
        return self.download_span_seconds + self.total_persistence_seconds

    @property
    def overlap_gain_seconds(self) -> float:
        return max(0.0, self.no_overlap_estimate_seconds - self.total_seconds)

    @property
    def files_per_minute(self) -> float:
        if self.total_seconds <= 0:
            return 0.0

        return self.files_sent_to_persistence / self.total_seconds * 60


def _elapsed_seconds(
    started_at: float | None,
    finished_at: float | None,
) -> float | None:
    if started_at is None or finished_at is None:
        return None

    return max(0.0, finished_at - started_at)


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.2f}"


def _format_optional_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "nao medido"

    return _format_seconds(seconds)


def _format_rate(value: float) -> str:
    return f"{value:.2f}"


def run_all_sources(args, config: AppConfig, output: TerminalOutput) -> None:
    """Executa a operacao solicitada para todas as fontes configuradas."""
    if args.download_and_process:
        if _should_run_download_and_process_pipeline(args, config):
            _run_download_and_process_pipeline(args, config, output)
            return

        download_args = _build_download_phase_args(args)
        downloaded_files = run_all_sources_phase(
            download_args,
            config,
            output,
            "Download",
        )

        process_args = _build_process_phase_args(args)
        run_all_sources_phase(
            process_args,
            config,
            output,
            "Persistencia",
            raw_files_by_source=downloaded_files,
        )
        return

    run_all_sources_phase(args, config, output, resolve_source_operation(args))


def _build_download_phase_args(args):
    download_args = copy(args)
    download_args.download_and_process = False
    download_args.download_only = True
    download_args.process_raw = False
    download_args.save = False

    return download_args


def _build_process_phase_args(args):
    process_args = copy(args)
    process_args.download_and_process = False
    process_args.download_only = False
    process_args.process_raw = True
    process_args.save = False

    return process_args


def _should_run_download_and_process_pipeline(args, config: AppConfig) -> bool:
    return args.workers > 1 and len(config.sources) > 1


def _run_download_and_process_pipeline(
    args,
    config: AppConfig,
    output: TerminalOutput,
) -> None:
    download_args = _build_download_phase_args(args)
    process_args = _build_process_phase_args(args)
    effective_workers = min(args.workers, len(config.sources))
    stats = PipelineStats()
    metrics = PipelineMetrics()

    output.header(
        "Download e persistencia em pipeline",
        _build_pipeline_details(args, config, effective_workers),
    )
    output.info(
        "Pipeline iniciado: downloads em paralelo e persistencia sequencial "
        "conforme cada fonte termina."
    )

    with ThreadPoolExecutor(max_workers=effective_workers) as executor:
        futures = {
            executor.submit(
                _run_source_download_worker,
                source_slug,
                download_args,
                config,
            ): source_slug
            for source_slug in config.sources
        }
        pending_futures = set(futures)

        try:
            for future in as_completed(futures):
                source_slug = futures[future]
                pending_futures.remove(future)
                metrics.record_backlog(_count_pipeline_backlog(pending_futures))
                result = _resolve_source_future(future, source_slug)
                _consume_pipeline_result(
                    result,
                    process_args,
                    config,
                    output,
                    stats,
                    metrics,
                )
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            raise

    metrics.finish()
    output.summary(
        (
            ("Fontes com download concluido", stats.download_completed),
            ("Fontes com download parcial", stats.download_partial),
            ("Fontes com download falho", stats.download_failed),
            ("Fontes enviadas para persistencia", stats.persistence_started),
            ("Fontes persistidas", stats.persistence_completed),
            ("Fontes com falha na persistencia", stats.persistence_failed),
            ("Fontes ignoradas na persistencia", stats.persistence_skipped),
        ),
        report_title="Download e persistencia em pipeline",
    )
    _report_pipeline_metrics(output, metrics)


def _build_pipeline_details(
    args,
    config: AppConfig,
    effective_workers: int,
) -> tuple[tuple[str, object], ...]:
    return (
        ("Fontes configuradas", len(config.sources)),
        ("Data limite", args.target_date or "ultima disponivel"),
        ("Cotacoes anteriores", format_quotes_back(args.quotes_back)),
        (
            "Historico incremental",
            format_incremental_history(
                config.incremental_history,
                args.target_date,
                args.quotes_back,
            ),
        ),
        (
            "Workers de download",
            (
                f"{effective_workers} de {args.workers} solicitado(s)"
                if effective_workers != args.workers
                else args.workers
            ),
        ),
        ("Consumidores de persistencia", 1),
        ("Fila de persistencia", "resultados concluidos em memoria"),
        ("Persistencia SQLite concorrente", "nao"),
    )


def _count_pipeline_backlog(pending_futures: set) -> int:
    return 1 + sum(1 for future in pending_futures if future.done())


def _report_pipeline_metrics(
    output: TerminalOutput,
    metrics: PipelineMetrics,
) -> None:
    output.report_summary(
        _build_pipeline_performance_rows(metrics),
        report_title="Desempenho do pipeline",
    )
    output.report_summary(
        _build_pipeline_source_rows(metrics),
        report_title="Desempenho por fonte no pipeline",
    )


def _build_pipeline_performance_rows(
    metrics: PipelineMetrics,
) -> tuple[tuple[str, object], ...]:
    return (
        ("Fontes metricadas", len(metrics.sources)),
        ("Tempo total pipeline (s)", _format_seconds(metrics.total_seconds)),
        ("Janela de downloads (s)", _format_seconds(metrics.download_span_seconds)),
        (
            "Tempo acumulado downloads (s)",
            _format_seconds(metrics.total_download_seconds),
        ),
        (
            "Tempo acumulado persistencia (s)",
            _format_seconds(metrics.total_persistence_seconds),
        ),
        (
            "Tempo estimado sem sobrepor persistencia (s)",
            _format_seconds(metrics.no_overlap_estimate_seconds),
        ),
        (
            "Ganho estimado por sobreposicao (s)",
            _format_seconds(metrics.overlap_gain_seconds),
        ),
        (
            "Espera acumulada na fila (s)",
            _format_seconds(metrics.total_queue_wait_seconds),
        ),
        (
            "Maior espera de fonte na fila (s)",
            _format_seconds(metrics.max_queue_wait_seconds),
        ),
        ("Backlog maximo da fila", metrics.max_backlog),
        ("Raws enviados para persistencia", metrics.files_sent_to_persistence),
        ("Raws enviados por minuto", _format_rate(metrics.files_per_minute)),
    )


def _build_pipeline_source_rows(
    metrics: PipelineMetrics,
) -> tuple[tuple[str, object], ...]:
    rows: list[tuple[str, object]] = []

    for metric in metrics.sources:
        prefix = f"{metric.source_slug} | "
        rows.extend(
            (
                (f"{prefix}status download", _format_download_status(metric.status)),
                (f"{prefix}status persistencia", metric.persistence_status),
                (f"{prefix}raws", metric.files),
                (
                    f"{prefix}download (s)",
                    _format_optional_seconds(metric.download_seconds),
                ),
                (
                    f"{prefix}espera fila (s)",
                    _format_optional_seconds(metric.queue_wait_seconds),
                ),
                (
                    f"{prefix}persistencia (s)",
                    _format_optional_seconds(metric.persistence_seconds),
                ),
            )
        )

    return tuple(rows)


def _format_download_status(status: str) -> str:
    return {
        "completed": "concluido",
        "partial": "parcial",
        "failed": "falhou",
    }.get(status, status)


def _consume_pipeline_result(
    result: SourceRunResult,
    process_args,
    config: AppConfig,
    output: TerminalOutput,
    stats: PipelineStats,
    metrics: PipelineMetrics,
) -> None:
    _replay_buffered_events(output, result.events)
    _record_pipeline_download_status(stats, result)
    handling_started_at = perf_counter()

    if result.status == "failed":
        if not result.events:
            output.warning(
                f"{result.source_slug} | {result.error_type}: "
                f"{result.error_message}"
            )

        stats.persistence_skipped += 1
        output.warning(
            f"{result.source_slug} | persistencia ignorada porque o download falhou."
        )
        metrics.record_source(
            result,
            persistence_status="ignorada",
            queue_wait_seconds=_elapsed_seconds(
                result.download_finished_at,
                handling_started_at,
            ),
        )
        return

    raw_files = list(result.files)

    if result.status == "partial" and not raw_files:
        stats.persistence_skipped += 1
        output.warning(
            f"{result.source_slug} | persistencia ignorada porque nao ha raw parcial."
        )
        metrics.record_source(
            result,
            persistence_status="ignorada",
            queue_wait_seconds=_elapsed_seconds(
                result.download_finished_at,
                handling_started_at,
            ),
        )
        return

    output.info(
        f"{result.source_slug} | {len(raw_files)} arquivo(s) enviado(s) "
        "para a fila de persistencia."
    )
    stats.persistence_started += 1
    persistence_started_at = perf_counter()
    queue_wait_seconds = _elapsed_seconds(
        result.download_finished_at,
        persistence_started_at,
    )

    if _run_pipeline_persistence(
        result.source_slug,
        raw_files,
        process_args,
        config,
        output,
    ):
        stats.persistence_completed += 1
        persistence_status = "concluida"
    else:
        stats.persistence_failed += 1
        persistence_status = "falhou"

    metrics.record_source(
        result,
        persistence_status=persistence_status,
        queue_wait_seconds=queue_wait_seconds,
        persistence_seconds=perf_counter() - persistence_started_at,
    )


def _record_pipeline_download_status(
    stats: PipelineStats,
    result: SourceRunResult,
) -> None:
    if result.status == "completed":
        stats.download_completed += 1
    elif result.status == "partial":
        stats.download_partial += 1
    else:
        stats.download_failed += 1


def _run_pipeline_persistence(
    source_slug: str,
    raw_files: list[Path],
    process_args,
    config: AppConfig,
    output: TerminalOutput,
) -> bool:
    source_args = _build_source_args(process_args, source_slug, output)

    try:
        run_source(
            source_args,
            config,
            output,
            show_summary=False,
            raw_files=raw_files,
        )
    except Exception as error:
        output.warning(
            f"{source_slug} | persistencia falhou: "
            f"{type(error).__name__}: {error}"
        )
        return False

    return True


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
    download_started_at = perf_counter()

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
            download_started_at=download_started_at,
            download_finished_at=perf_counter(),
        )
    except Exception as error:
        output.warning(f"{source_slug} | {type(error).__name__}: {error}")
        return SourceRunResult(
            source_slug=source_slug,
            status="failed",
            events=tuple(output.events),
            error_type=type(error).__name__,
            error_message=str(error),
            download_started_at=download_started_at,
            download_finished_at=perf_counter(),
        )

    return SourceRunResult(
        source_slug=source_slug,
        status="completed",
        files=tuple(source_files or ()),
        events=tuple(output.events),
        download_started_at=download_started_at,
        download_finished_at=perf_counter(),
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
            download_finished_at=perf_counter(),
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
