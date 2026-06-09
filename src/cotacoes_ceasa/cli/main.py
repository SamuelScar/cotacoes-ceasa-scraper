from pathlib import Path

from cotacoes_ceasa.cli.commands.batch import run_all_sources
from cotacoes_ceasa.cli.commands.maintenance import (
    run_archive_command,
    run_prohort_command,
)
from cotacoes_ceasa.cli.commands.source import (
    run_source,
    run_source_download_and_process,
)
from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import (
    build_parser,
    format_incremental_history,
    format_quotes_back,
)
from cotacoes_ceasa.config import AppConfig, load_config


REPORT_DIR = Path("data/relatorios")


def main() -> None:
    """Executa comandos de coleta disponiveis no projeto."""
    output = TerminalOutput()

    try:
        try:
            run(output)
        except KeyboardInterrupt:
            output.error("Execucao interrompida pelo usuario.")
            output.summary()
            raise SystemExit(130)
        except Exception as error:
            output.error(f"{type(error).__name__}: {error}")
            output.summary()
            raise SystemExit(1)
    finally:
        save_collection_report(output)


def run(output: TerminalOutput) -> None:
    """Seleciona e executa o fluxo solicitado pela CLI."""
    config = load_config()
    args = build_parser(config).parse_args()

    if args.base_url and args.source is None:
        raise ValueError("--base-url exige --source.")

    if not (args.archive_raw_old or args.complement_prohort or args.list_categories):
        output.enable_collection_report(build_report_configuration(args, config))

    if args.archive_raw_old:
        run_archive_command(args, output)
        return

    if args.complement_prohort:
        run_prohort_command(args, config, output)
        return

    if args.source is None:
        run_all_sources(args, config, output)
        run_automatic_prohort(args, config, output)
        return

    if args.download_and_process:
        run_source_download_and_process(args, config, output)
    else:
        run_source(args, config, output)

    run_automatic_prohort(args, config, output)


def run_automatic_prohort(args, config: AppConfig, output: TerminalOutput) -> None:
    """Executa o complemento automatico depois de fluxos que salvam no SQLite."""
    saves_database = args.save or args.process_raw or args.download_and_process

    if not config.complement_prohort or not saves_database:
        return

    run_prohort_command(args, config, output)


def build_report_configuration(
    args,
    config: AppConfig,
) -> tuple[tuple[str, object], ...]:
    all_sources = args.source is None
    saves_database = args.save or args.process_raw or args.download_and_process
    automatic_prohort = config.complement_prohort and saves_database
    accesses_source_http = not args.process_raw
    accesses_http = accesses_source_http or automatic_prohort
    source_slugs = (
        ", ".join(config.sources) if all_sources else args.source
    )
    rows: list[tuple[str, object]] = [
        ("Origem", ".env, arquivos de configuracao e argumentos CLI"),
        ("Fluxo", resolve_report_flow(args)),
        ("Escopo", "todas as fontes" if all_sources else args.source),
        ("Fontes executadas", source_slugs),
        ("Fontes configuradas", len(config.sources)),
        ("COTACOES_SOURCES_FILE", config.sources_file),
        ("COTACOES_RAW_DIR", args.raw_dir),
        ("Acesso HTTP", "sim" if accesses_http else "nao"),
        ("Persistencia SQLite", "sim" if saves_database else "nao"),
        ("COTACOES_COMPLEMENT_PROHORT", config.complement_prohort),
        (
            "Complemento PROHORT automatico efetivo",
            "sim" if automatic_prohort else "nao",
        ),
        ("Configuracao PROHORT", config.prohort_file),
    ]

    if saves_database:
        rows.append(("COTACOES_DATABASE_PATH", args.database_path))

    if args.download_and_process:
        rows.append(("Escopo do processamento raw", "somente raws desta coleta"))
    elif args.process_raw:
        rows.append(("Escopo do processamento raw", "todos os raws ativos"))

    if automatic_prohort:
        rows.append(("URL PROHORT", config.prohort_url))

    if accesses_source_http:
        rows.extend(
            [
                ("COTACOES_TARGET_DATE", args.target_date or "ultima disponivel"),
                ("COTACOES_QUOTES_BACK", format_quotes_back(args.quotes_back)),
                ("COTACOES_HTTP_TIMEOUT_SECONDS", args.http_timeout_seconds),
                ("COTACOES_REQUEST_DELAY_SECONDS", args.request_delay_seconds),
                (
                    "COTACOES_REUSE_RAW_BEFORE_REQUEST",
                    config.reuse_raw_before_request,
                ),
                (
                    "COTACOES_INCREMENTAL_HISTORY",
                    config.incremental_history,
                ),
                (
                    "Historico incremental efetivo",
                    format_incremental_history(
                        config.incremental_history,
                        args.target_date,
                        args.quotes_back,
                    ),
                ),
            ]
        )

        if args.quotes_back is None:
            rows.append(
                (
                    "Encerramento do modo infinito",
                    "366 tentativas consecutivas sem data mais antiga",
                )
            )

    if not all_sources:
        source_config = config.sources[args.source]
        rows.append(("Fonte selecionada via CLI", args.source))
        rows.append(("URL base efetiva", args.base_url or source_config.base_url))

    return tuple(rows)


def resolve_report_flow(args) -> str:
    if args.download_and_process:
        return "baixar e processar"

    if args.download_only:
        return "baixar raws"

    if args.process_raw:
        return "processar raws e salvar"

    if args.save:
        return "coletar e salvar"

    return "coletar e extrair"


def save_collection_report(output: TerminalOutput) -> None:
    if not output.collection_report_enabled:
        return

    try:
        report_path = output.write_collection_report(REPORT_DIR)
    except Exception as error:
        output.error(f"Nao foi possivel salvar o relatorio: {error}")
        return

    output.report_saved(report_path)


if __name__ == "__main__":
    main()
