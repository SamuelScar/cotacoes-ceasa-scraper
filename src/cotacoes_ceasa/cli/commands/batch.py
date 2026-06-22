from copy import copy
from pathlib import Path

from cotacoes_ceasa.cli.commands.source import resolve_source_operation, run_source
from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import format_incremental_history, format_quotes_back
from cotacoes_ceasa.cli.progress import ProgressReporter
from cotacoes_ceasa.config import AppConfig
from cotacoes_ceasa.sources.registry import history_requested, source_supports_history
from cotacoes_ceasa.workflows.collection import PartialDownloadError


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
        (
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
        ),
    )
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

    output.summary(
        (
            ("Fontes concluidas", completed_count),
            ("Fontes com falha", failed_count),
            ("Fontes ignoradas", skipped_count),
        ),
        report_title=f"{phase_name} de todas as fontes",
    )
    return downloaded_files
