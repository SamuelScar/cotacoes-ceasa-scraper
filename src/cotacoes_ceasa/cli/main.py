import shlex
import sys
from pathlib import Path

from cotacoes_ceasa.cli.commands.batch import run_all_sources
from cotacoes_ceasa.cli.commands.maintenance import (
    run_archive_command,
    run_prohort_command,
    run_supabase_sync_command,
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
from cotacoes_ceasa.workflows.collection import PartialDownloadError


REPORT_DIR = Path("data/relatorios")


def main() -> None:
    """Executa os comandos disponiveis no projeto."""
    output = TerminalOutput()
    output.enable_execution_report(build_initial_report_configuration())

    try:
        try:
            run(output)
        except KeyboardInterrupt:
            output.set_execution_status("Interrompida pelo usuario")
            output.error("Execucao interrompida pelo usuario.")
            output.summary()
            raise SystemExit(130)
        except SystemExit as error:
            if error.code not in {None, 0}:
                output.set_execution_status("Encerrada com erro")
                output.error(f"CLI encerrada com codigo {error.code}.")
                output.summary()
            raise
        except PartialDownloadError as error:
            output.set_execution_status("Encerrada com erro")
            output.error(
                f"{type(error.original_error).__name__}: {error.original_error}"
            )
            output.summary()
            raise SystemExit(1)
        except Exception as error:
            output.set_execution_status("Encerrada com erro")
            output.error(f"{type(error).__name__}: {error}")
            output.summary()
            raise SystemExit(1)
    finally:
        save_execution_report(output)


def run(output: TerminalOutput) -> None:
    """Seleciona e executa o fluxo solicitado pela CLI."""
    config = load_config()
    args = build_parser(config).parse_args()

    output.configure_execution_report(
        report_name=resolve_report_name(args),
        report_title=resolve_report_flow(args),
        configuration=build_report_configuration(args, config),
    )

    if args.base_url and args.source is None:
        raise ValueError("--base-url exige --source.")

    if args.archive_raw_old:
        run_archive_command(args, output)
        return

    if args.complement_prohort:
        run_prohort_command(args, config, output)
        return

    if args.sync_supabase:
        run_supabase_sync_command(args, config, output, mode="incremental")
        return

    if args.replace_supabase:
        run_supabase_sync_command(args, config, output, mode="full")
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
    rows = [
        *build_initial_report_configuration(),
        ("Fluxo solicitado", resolve_report_flow(args)),
        (
            "Origem da configuracao",
            ".env, arquivos de configuracao e argumentos CLI",
        ),
    ]

    if args.archive_raw_old:
        rows.extend(
            [
                ("Escopo solicitado", "arquivos antigos de todas as fontes"),
                ("COTACOES_RAW_DIR", args.raw_dir),
                ("Acesso HTTP", "nao"),
                ("Persistencia SQLite", "nao"),
            ]
        )
        return tuple(rows)

    if args.complement_prohort:
        rows.extend(
            [
                ("Escopo solicitado", "complemento PROHORT"),
                ("COTACOES_DATABASE_PATH", args.database_path),
                ("Configuracao PROHORT", config.prohort_file),
                ("URL PROHORT", config.prohort_url),
                ("COTACOES_HTTP_TIMEOUT_SECONDS", args.http_timeout_seconds),
                ("Acesso HTTP", "sim"),
                ("Persistencia SQLite", "sim"),
            ]
        )
        return tuple(rows)

    if args.sync_supabase or args.replace_supabase:
        rows.extend(
            [
                (
                    "Escopo solicitado",
                    (
                        "adicionar novos registros ao Supabase"
                        if args.sync_supabase
                        else "substituir snapshot do Supabase"
                    ),
                ),
                (
                    "Modo de sincronizacao",
                    "incremental" if args.sync_supabase else "completa",
                ),
                ("COTACOES_DATABASE_PATH", args.database_path),
                (
                    "COTACOES_SUPABASE_DATABASE_URL configurada",
                    "sim" if config.supabase_database_url else "nao",
                ),
                ("COTACOES_SUPABASE_BATCH_SIZE", args.supabase_batch_size),
                ("Leitura SQLite", "sim"),
                ("Persistencia SQLite", "nao"),
                ("Conexao externa", "Supabase PostgreSQL"),
            ]
        )
        return tuple(rows)

    all_sources = args.source is None
    saves_database = args.save or args.process_raw or args.download_and_process
    automatic_prohort = config.complement_prohort and saves_database
    accesses_source_http = not args.process_raw
    accesses_http = accesses_source_http or automatic_prohort
    source_slugs = ", ".join(config.sources) if all_sources else args.source
    rows.extend(
        [
            ("Escopo solicitado", "todas as fontes" if all_sources else args.source),
            ("Fontes solicitadas", source_slugs),
            ("Fontes configuradas", len(config.sources)),
            ("COTACOES_SOURCES_FILE", config.sources_file),
            ("Acesso HTTP", "sim" if accesses_http else "nao"),
            ("Persistencia SQLite", "sim" if saves_database else "nao"),
        ]
    )

    if not args.list_categories:
        rows.append(("COTACOES_RAW_DIR", args.raw_dir))

    if saves_database:
        rows.append(("COTACOES_DATABASE_PATH", args.database_path))
        rows.append(("COTACOES_PDF_TEXT_CACHE_DIR", args.pdf_text_cache_dir))
        rows.append(("Reprocessamento forcado", "sim" if args.force_reprocess else "nao"))
        rows.append(
            (
                "Detalhe de raw no relatorio",
                "sim" if args.raw_detail_report else "nao",
            )
        )
        rows.append(("COTACOES_COMPLEMENT_PROHORT", config.complement_prohort))
        rows.append(
            (
                "Complemento PROHORT automatico efetivo",
                "sim" if automatic_prohort else "nao",
            )
        )

    if args.download_and_process:
        rows.append(("Escopo do processamento raw", "somente raws desta coleta"))
    elif args.process_raw:
        rows.append(("Escopo do processamento raw", "todos os raws ativos"))

    if automatic_prohort:
        rows.append(("Configuracao PROHORT", config.prohort_file))
        rows.append(("URL PROHORT", config.prohort_url))

        if not accesses_source_http:
            rows.append(("COTACOES_HTTP_TIMEOUT_SECONDS", args.http_timeout_seconds))

    if accesses_source_http:
        rows.extend(
            [
                ("COTACOES_HTTP_TIMEOUT_SECONDS", args.http_timeout_seconds),
                ("COTACOES_REQUEST_DELAY_SECONDS", args.request_delay_seconds),
                (
                    "COTACOES_REUSE_RAW_BEFORE_REQUEST",
                    config.reuse_raw_before_request,
                ),
            ]
        )

        if not args.list_categories:
            rows.extend(
                [
                    ("COTACOES_TARGET_DATE", args.target_date or "ultima disponivel"),
                    ("COTACOES_QUOTES_BACK", format_quotes_back(args.quotes_back)),
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

        if all_sources and (args.download_only or args.download_and_process):
            effective_workers = min(args.workers, len(config.sources))
            rows.extend(
                [
                    ("COTACOES_WORKERS", args.workers),
                    ("Workers de download efetivos", effective_workers),
                    (
                        "Download paralelo",
                        "sim" if effective_workers > 1 else "nao",
                    ),
                ]
            )

        if not args.list_categories and args.quotes_back is None:
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
    if args.archive_raw_old:
        return "Compactar arquivos antigos"

    if args.complement_prohort:
        return "Complementar cotacoes com PROHORT"

    if args.sync_supabase:
        return "Adicionar novos registros ao Supabase"

    if args.replace_supabase:
        return "Substituir snapshot do Supabase"

    if args.list_categories:
        return "Listar categorias"

    if args.download_and_process:
        return "Baixar raws, processar e salvar cotacoes"

    if args.download_only:
        return "Baixar raws"

    if args.process_raw:
        return "Processar raws e salvar cotacoes"

    if args.save:
        return "Coletar e salvar cotacoes"

    return "Coletar e extrair cotacoes"


def resolve_report_name(args) -> str:
    if args.archive_raw_old:
        return "compactacao"

    if args.complement_prohort:
        return "complemento_prohort"

    if args.sync_supabase:
        return "sincronizacao_supabase_incremental"

    if args.replace_supabase:
        return "sincronizacao_supabase_completa"

    if args.list_categories:
        return "consulta_categorias"

    if args.download_and_process:
        return "download_e_persistencia"

    if args.download_only:
        return "download"

    if args.process_raw:
        return "persistencia"

    if args.save:
        return "coleta_e_persistencia"

    return "coleta"


def build_initial_report_configuration() -> tuple[tuple[str, object], ...]:
    arguments = _sanitize_arguments(sys.argv[1:])
    command = shlex.join([sys.argv[0], *arguments])

    return (
        ("Comando executado", command),
        ("Ponto de entrada", sys.argv[0]),
        ("Argumentos recebidos", shlex.join(arguments) if arguments else "(nenhum)"),
    )


def _sanitize_arguments(arguments: list[str]) -> list[str]:
    sensitive_names = ("password", "token", "secret", "api-key", "database-url")
    sanitized: list[str] = []
    mask_next = False

    for argument in arguments:
        if mask_next:
            sanitized.append("***")
            mask_next = False
            continue

        option, separator, _ = argument.partition("=")
        is_sensitive = option.startswith("--") and any(
            name in option.lower() for name in sensitive_names
        )

        if is_sensitive and separator:
            sanitized.append(f"{option}=***")
        else:
            sanitized.append(argument)
            mask_next = is_sensitive

    return sanitized


def save_execution_report(output: TerminalOutput) -> None:
    try:
        report_path = output.write_execution_report(REPORT_DIR)
    except Exception as error:
        output.error(f"Nao foi possivel salvar o relatorio: {error}")
        return

    output.report_saved(report_path)


if __name__ == "__main__":
    main()
