from copy import copy

from cotacoes_ceasa.cli.commands.source import resolve_source_operation, run_source
from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.config import AppConfig
from cotacoes_ceasa.sources.registry import source_supports_history


def run_all_sources(args, config: AppConfig, output: TerminalOutput) -> None:
    """Executa a operacao solicitada para todas as fontes configuradas."""
    if args.download_and_process:
        download_args = copy(args)
        download_args.download_and_process = False
        run_all_sources_phase(download_args, config, output, "Download")

        process_args = copy(args)
        process_args.download_and_process = False
        process_args.process_raw = True
        process_args.save = False
        run_all_sources_phase(process_args, config, output, "Persistencia")
        return

    run_all_sources_phase(args, config, output, resolve_source_operation(args))


def run_all_sources_phase(
    args,
    config: AppConfig,
    output: TerminalOutput,
    phase_name: str,
) -> None:
    """Executa uma fase para todas as fontes sem interromper o lote."""
    output.header(
        f"{phase_name} de todas as fontes",
        (
            ("Fontes configuradas", len(config.sources)),
            ("Data limite", args.target_date or "ultima disponivel"),
            ("Cotacoes anteriores", args.quotes_back),
        ),
    )
    completed_count = 0
    failed_count = 0

    for source_slug in config.sources:
        source_args = copy(args)
        source_args.source = source_slug

        if source_args.quotes_back and not source_supports_history(source_slug):
            source_args.quotes_back = 0

            if not source_args.process_raw:
                output.info(
                    f"{source_slug} | fonte sem historico; coletando somente a atual."
                )

        try:
            run_source(source_args, config, output, show_summary=False)
        except Exception as error:
            failed_count += 1
            output.warning(f"{source_slug} | {type(error).__name__}: {error}")
            continue

        completed_count += 1

    output.summary(
        (
            ("Fontes concluidas", completed_count),
            ("Fontes com falha", failed_count),
        )
    )
