from pathlib import Path

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.config import AppConfig
from cotacoes_ceasa.storage.raw_html import RawArchiveResult, RawHtmlStorage
from cotacoes_ceasa.storage.supabase import SupabaseSynchronizer
from cotacoes_ceasa.workflows.prohort import (
    ProhortComplementer,
    ProhortComplementResult,
)


def run_archive_command(args, output: TerminalOutput) -> None:
    output.header(
        "Compactar arquivos antigos",
        (("Diretorio raw", args.raw_dir),),
    )
    archive_raw_old_and_report(RawHtmlStorage(Path(args.raw_dir)), output)


def run_prohort_command(args, config: AppConfig, output: TerminalOutput) -> None:
    output.header(
        "Complementar cotacoes com PROHORT",
        (
            ("Banco", args.database_path),
            ("Configuracao", config.prohort_file),
        ),
    )
    complement_prohort_and_report(args, config.prohort_url, output)


def run_supabase_sync_command(
    args,
    config: AppConfig,
    output: TerminalOutput,
) -> None:
    if not config.supabase_database_url:
        raise ValueError("COTACOES_SUPABASE_DATABASE_URL nao configurada no .env.")

    output.header(
        "Sincronizar SQLite com Supabase",
        (("Banco local", args.database_path),),
    )
    output.section("Sincronizacao")
    output.info("Criando schema e enviando registros para o Supabase.")
    result = SupabaseSynchronizer(
        sqlite_path=Path(args.database_path),
        database_url=config.supabase_database_url,
    ).sync()

    for table_name, count in result.table_counts.items():
        output.success(f"{table_name}: {count} registro(s) sincronizado(s).")

    output.summary((("Registros sincronizados", result.total_count),))


def archive_raw_old_and_report(
    raw_storage: RawHtmlStorage,
    output: TerminalOutput | None = None,
) -> None:
    output = output or TerminalOutput()
    results = raw_storage.archive_old_html_files()
    output.section("Compactacao")

    if not results:
        output.info("Nenhum HTML antigo encontrado para compactar.")
        output.summary((("Arquivos compactados", 0),))
        return

    for result in results:
        output.success(format_archive_result(result))

    output.summary(
        (("Arquivos compactados", sum(result.archived_count for result in results)),)
    )


def format_archive_result(result: RawArchiveResult) -> str:
    return (
        f"{result.source}: {result.archived_count} HTMLs compactados em "
        f"{result.archive_path}"
    )


def complement_prohort_and_report(
    args,
    prohort_url: str,
    output: TerminalOutput | None = None,
) -> None:
    output = output or TerminalOutput()
    output.section("Complemento PROHORT")
    output.info("Lendo cotacoes salvas e buscando correspondencias confiaveis.")
    result = ProhortComplementer(
        database_path=Path(args.database_path),
        prohort_url=prohort_url,
        timeout_seconds=args.http_timeout_seconds,
    ).complement()

    if not result.database_found:
        output.warning(format_prohort_complement_result(result, args.database_path))
    elif result.candidate_count == 0 and result.fallback_scope_count == 0:
        output.info(format_prohort_complement_result(result, args.database_path))
    else:
        output.success(format_prohort_complement_result(result, args.database_path))

    output.summary(
        (
            ("Linhas lidas", result.scanned_rows),
            ("Cotacoes complementadas", result.updated_count),
            ("Cotacoes inseridas", result.inserted_count),
            ("Sem mapeamento", result.unmapped_count),
            ("Ambiguas", result.ambiguous_count),
        )
    )


def format_prohort_complement_result(
    result: ProhortComplementResult,
    database_path: str,
) -> str:
    if not result.database_found:
        return f"Banco SQLite nao encontrado em {database_path}."

    if result.candidate_count == 0 and result.fallback_scope_count == 0:
        return "Nenhuma cotacao com preco comum vazio encontrada para complementar."

    return (
        f"prohort: {result.scanned_rows} linhas lidas, "
        f"{result.candidate_count} cotacoes candidatas, "
        f"{result.fallback_scope_count} datas/CEASAs com fallback, "
        f"{result.matched_rows} correspondencias confiaveis, "
        f"{result.updated_count} cotacoes complementadas, "
        f"{result.inserted_count} cotacoes faltantes inseridas, "
        f"{result.unmapped_count} sem mapeamento e "
        f"{result.ambiguous_count} ambiguas."
    )
