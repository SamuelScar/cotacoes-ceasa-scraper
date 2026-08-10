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
from cotacoes_ceasa.cli.collection_mode import (
    COLLECTION_MODE_BACKFILL,
    COLLECTION_MODE_LEGACY,
    format_collection_mode,
    prepare_collection_mode,
)
from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import (
    build_parser,
    format_incremental_history,
    format_quotes_back,
)
from cotacoes_ceasa.config import AppConfig, load_config
from cotacoes_ceasa.storage.sqlite import SQLiteStorage
from cotacoes_ceasa.workflows.collection import PartialDownloadError
from cotacoes_ceasa.workflows.backfill import (
    BackfillBaseline,
    capture_backfill_baselines,
    finalize_backfill_state,
    format_backfill_state,
)
from cotacoes_ceasa.workflows.health import (
    BatchRunResult,
    HealthBaseline,
    build_unavailable_run_health,
    capture_health_baseline,
    evaluate_run_health,
    write_health_assessment,
)
from cotacoes_ceasa.workflows.publication_gate import (
    evaluate_publication_gate,
    write_publication_gate_result,
)


REPORT_DIR = Path("data/relatorios")
HEALTH_REPORT_PATH = REPORT_DIR / "saude_ultima.json"


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
    config = prepare_collection_mode(args, config)

    output.configure_execution_report(
        report_name=resolve_report_name(args),
        report_title=resolve_report_flow(args),
        configuration=build_report_configuration(args, config),
    )

    if args.base_url and args.source is None:
        raise ValueError("--base-url exige --source.")

    if args.reset_backfill_state:
        run_backfill_state_reset(args, output)
        return

    if args.validate_publication:
        run_publication_gate_command(args, config, output)
        return

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

    if report_deferred_backfill(args, config, output):
        return

    if args.source is None:
        health_baseline = (
            capture_health_baseline(
                Path(args.database_path),
                tuple(config.sources),
            )
            if args.download_and_process
            else None
        )
        batch_result = None
        run_error = None
        run_interrupted = False
        backfill_baselines = capture_run_backfill_baselines(args, config)

        try:
            batch_result = run_all_sources(args, config, output)
            run_automatic_prohort(args, config, output)
        except KeyboardInterrupt as error:
            run_error = f"{type(error).__name__}: {error}"
            run_interrupted = True
            raise
        except (Exception, SystemExit) as error:
            run_error = f"{type(error).__name__}: {error}"
            raise
        finally:
            record_batch_backfill_states(
                args,
                output,
                batch_result,
                backfill_baselines,
                run_error,
            )
            record_run_health(
                args,
                config,
                output,
                batch_result,
                health_baseline,
                run_error,
                run_interrupted,
            )
        return

    backfill_baselines = capture_run_backfill_baselines(args, config)

    try:
        if args.download_and_process:
            run_source_download_and_process(args, config, output)
        else:
            run_source(args, config, output)
    except (Exception, SystemExit) as error:
        record_source_backfill_state(
            args,
            output,
            backfill_baselines,
            download_status="failed",
            persistence_status="failed",
            error=f"{type(error).__name__}: {error}",
        )
        raise
    else:
        record_source_backfill_state(
            args,
            output,
            backfill_baselines,
            download_status="completed",
            persistence_status="completed",
        )

    run_automatic_prohort(args, config, output)


def run_backfill_state_reset(args, output: TerminalOutput) -> None:
    if args.source is None:
        raise ValueError("--reset-backfill-state exige --source.")

    invalid_operation = any(
        (
            args.save,
            args.process_raw,
            args.download_and_process,
            args.download_only,
            args.archive_raw_old,
            args.complement_prohort,
            args.sync_supabase,
            args.replace_supabase,
            args.list_categories,
            args.base_url,
            args.validate_publication,
        )
    )
    if invalid_operation:
        raise ValueError(
            "--reset-backfill-state nao pode ser combinado com outra operacao."
        )

    output.header(
        "Reabrir estado do backfill",
        (
            ("Fonte", args.source),
            ("Banco", args.database_path),
        ),
    )
    removed_count = SQLiteStorage(
        Path(args.database_path)
    ).reset_backfill_state(args.source)
    output.success(f"{removed_count} estado(s) removido(s).")
    output.summary(
        (
            ("Fonte reaberta", args.source),
            ("Estados removidos", removed_count),
        )
    )


def run_publication_gate_command(
    args,
    config: AppConfig,
    output: TerminalOutput,
) -> None:
    invalid_operation = any(
        (
            args.source,
            args.save,
            args.process_raw,
            args.download_and_process,
            args.download_only,
            args.archive_raw_old,
            args.complement_prohort,
            args.sync_supabase,
            args.replace_supabase,
            args.list_categories,
            args.base_url,
            args.reset_backfill_state,
        )
    )
    if invalid_operation:
        raise ValueError(
            "--validate-publication nao pode ser combinado com outra operacao."
        )

    health_report_path = Path(args.health_report_path)
    gate_report_path = Path(args.publication_gate_report_path)
    output.header(
        "Validar gate de publicacao",
        (
            ("Banco", args.database_path),
            ("Saude da rodada", health_report_path),
            ("Resultado estruturado", gate_report_path),
        ),
    )
    result = evaluate_publication_gate(
        database_path=Path(args.database_path),
        health_report_path=health_report_path,
        source_configs=config.sources,
    )
    write_publication_gate_result(result, gate_report_path)

    for reason in result.reasons:
        message = f"{reason.code} | {reason.message}"
        if reason.blocking:
            output.error(message)
        else:
            output.warning(message)

    output.summary(
        (
            ("Decisao", result.status),
            ("Motivos bloqueantes", result.blocking_reasons),
            ("Avisos", result.warnings),
            ("PRAGMA quick_check", ", ".join(result.quick_check) or "indisponivel"),
            ("Violacoes de chave estrangeira", result.foreign_key_violations),
            ("Cotacoes", result.total_quotes),
            ("JSON do gate", gate_report_path),
        ),
        report_title="Gate de publicacao",
    )

    if result.status == "rejected":
        raise SystemExit(2)


def report_deferred_backfill(
    args,
    config: AppConfig,
    output: TerminalOutput,
) -> bool:
    if args.effective_collection_mode != COLLECTION_MODE_BACKFILL:
        return False

    deferred_sources = args.backfill_deferred_sources
    if not deferred_sources:
        return False

    storage = SQLiteStorage(Path(args.database_path))
    rows: list[tuple[str, object]] = []

    for source_slug in deferred_sources:
        state = storage.find_backfill_state(source_slug)
        state_description = (
            format_backfill_state(state) if state is not None else "adiado"
        )
        output.info(f"{source_slug} | backfill {state_description}.")
        rows.append((source_slug, state_description))

    should_stop = args.source is not None or not config.sources
    if not should_stop:
        output.report_summary(tuple(rows), report_title="Backfills adiados")
        return False

    output.header(
        "Backfill sem fontes liberadas",
        (("Fontes adiadas", ", ".join(deferred_sources)),),
    )
    output.summary(tuple(rows), report_title="Backfills adiados")
    return True


def capture_run_backfill_baselines(
    args,
    config: AppConfig,
) -> dict[str, BackfillBaseline]:
    if (
        args.effective_collection_mode != COLLECTION_MODE_BACKFILL
        or not args.download_and_process
    ):
        return {}

    source_slugs = (args.source,) if args.source is not None else config.sources

    return capture_backfill_baselines(
        Path(args.database_path),
        source_slugs,
    )


def record_batch_backfill_states(
    args,
    output: TerminalOutput,
    batch_result: BatchRunResult | None,
    baselines: dict[str, BackfillBaseline],
    run_error: str | None,
) -> None:
    if not baselines:
        return

    observations = {
        source.source_slug: source
        for source in (batch_result.sources if batch_result is not None else ())
    }

    for source_slug, baseline in baselines.items():
        observation = observations.get(source_slug)
        record_source_backfill_state(
            args,
            output,
            {source_slug: baseline},
            download_status=(
                observation.download_status if observation is not None else "failed"
            ),
            persistence_status=(
                observation.persistence_status
                if observation is not None
                else "failed"
            ),
            error=(
                _source_observation_error(observation)
                if observation is not None
                else run_error
            ),
        )


def record_source_backfill_state(
    args,
    output: TerminalOutput,
    baselines: dict[str, BackfillBaseline],
    download_status: str,
    persistence_status: str,
    error: str | None = None,
) -> None:
    if not baselines:
        return

    for baseline in baselines.values():
        try:
            state = finalize_backfill_state(
                database_path=Path(args.database_path),
                baseline=baseline,
                download_status=download_status,
                persistence_status=persistence_status,
                error=error,
            )
        except Exception as state_error:
            output.warning(
                f"{baseline.source_slug} | falha ao persistir estado do backfill: "
                f"{type(state_error).__name__}: {state_error}"
            )
            continue

        output.report_summary(
            (
                ("Fonte", baseline.source_slug),
                ("Estado", format_backfill_state(state)),
                (
                    "Cursor",
                    state.cursor_date.isoformat() if state.cursor_date else "ausente",
                ),
                ("Rodadas sem progresso", state.consecutive_no_progress),
            ),
            report_title=f"Estado do backfill {baseline.source_slug}",
        )
        output.info(
            f"{baseline.source_slug} | estado do backfill: "
            f"{format_backfill_state(state)}."
        )


def _source_observation_error(source) -> str | None:
    if source.error_type is None and source.error_message is None:
        return None

    return ": ".join(
        part for part in (source.error_type, source.error_message) if part
    )


def run_automatic_prohort(args, config: AppConfig, output: TerminalOutput) -> None:
    """Executa o complemento automatico depois de fluxos que salvam no SQLite."""
    saves_database = args.save or args.process_raw or args.download_and_process

    if not config.complement_prohort or not saves_database:
        return

    run_prohort_command(args, config, output)


def record_run_health(
    args,
    config: AppConfig,
    output: TerminalOutput,
    batch_result: BatchRunResult | None,
    baseline: HealthBaseline | None,
    run_error: str | None,
    run_interrupted: bool,
) -> None:
    if baseline is None:
        return

    effective_batch_result = batch_result or BatchRunResult(sources=())

    if run_interrupted:
        assessment = build_unavailable_run_health(
            batch_result=effective_batch_result,
            source_configs=config.sources,
            database_path=Path(args.database_path),
            error="assessment_skipped_after_interruption",
            run_error=run_error,
            collection_mode=args.effective_collection_mode,
        )
    else:
        try:
            assessment = evaluate_run_health(
                batch_result=effective_batch_result,
                source_configs=config.sources,
                database_path=Path(args.database_path),
                baseline=baseline,
                run_error=run_error,
                collection_mode=args.effective_collection_mode,
            )
        except Exception as error:
            assessment_error = f"{type(error).__name__}: {error}"
            output.progress(
                "Avaliacao observacional de saude indisponivel: "
                f"{assessment_error}",
            )
            assessment = build_unavailable_run_health(
                batch_result=effective_batch_result,
                source_configs=config.sources,
                database_path=Path(args.database_path),
                error=assessment_error,
                run_error=run_error,
                collection_mode=args.effective_collection_mode,
            )

    json_error = None

    try:
        write_health_assessment(assessment, HEALTH_REPORT_PATH)
    except OSError as error:
        json_error = f"{type(error).__name__}: {error}"

        try:
            HEALTH_REPORT_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError as invalidation_error:
            json_error += (
                "; falha ao invalidar arquivo anterior: "
                f"{type(invalidation_error).__name__}: {invalidation_error}"
            )

        output.progress(
            f"Nao foi possivel salvar {HEALTH_REPORT_PATH}: {json_error}",
        )

    output.record_health_assessment(
        assessment,
        HEALTH_REPORT_PATH,
        json_error=json_error,
    )


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

    if args.reset_backfill_state:
        rows.extend(
            [
                ("Escopo solicitado", args.source or "fonte ausente"),
                ("COTACOES_DATABASE_PATH", args.database_path),
                ("Acesso HTTP", "nao"),
                ("Persistencia SQLite", "sim"),
            ]
        )
        return tuple(rows)

    if args.validate_publication:
        rows.extend(
            [
                ("Escopo solicitado", "gate de publicacao"),
                ("COTACOES_DATABASE_PATH", args.database_path),
                ("Relatorio de saude", args.health_report_path),
                ("Resultado do gate", args.publication_gate_report_path),
                ("Acesso HTTP", "nao"),
                ("Leitura SQLite", "sim"),
                ("Persistencia SQLite", "nao"),
            ]
        )
        return tuple(rows)

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
            rows.append(
                (
                    "Modo de coleta efetivo",
                    format_collection_mode(args.effective_collection_mode),
                )
            )
            rows.extend(
                [
                    (
                        "COTACOES_TARGET_DATE",
                        args.requested_target_date or "ultima disponivel",
                    ),
                    (
                        "COTACOES_QUOTES_BACK",
                        format_quotes_back(args.requested_quotes_back),
                    ),
                    (
                        "COTACOES_INCREMENTAL_HISTORY",
                        config.incremental_history,
                    ),
                    (
                        "Historico incremental efetivo",
                        format_incremental_history(
                            args.incremental_history,
                            args.target_date,
                            args.quotes_back,
                        ),
                    ),
                ]
            )

            if args.effective_collection_mode != COLLECTION_MODE_LEGACY:
                rows.extend(
                    [
                        (
                            "Data limite efetiva",
                            args.target_date or "ultima disponivel",
                        ),
                        (
                            "Cotacoes anteriores efetivas",
                            format_quotes_back(args.quotes_back),
                        ),
                    ]
                )

            if args.backfill_excluded_sources:
                rows.append(
                    (
                        "Fontes excluidas do backfill",
                        ", ".join(args.backfill_excluded_sources),
                    )
                )

            if args.backfill_deferred_sources:
                rows.append(
                    (
                        "Fontes adiadas pelo estado do backfill",
                        ", ".join(args.backfill_deferred_sources),
                    )
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
    if args.reset_backfill_state:
        return "Reabrir estado do backfill"

    if args.validate_publication:
        return "Validar gate de publicacao"

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
    if args.reset_backfill_state:
        return "reset_backfill"

    if args.validate_publication:
        return "gate_publicacao"

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
