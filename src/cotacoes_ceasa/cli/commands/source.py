from pathlib import Path

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import parse_target_date
from cotacoes_ceasa.config import AppConfig, SourceConfig
from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.sources.registry import (
    build_registered_collector,
    build_source_parser,
)
from cotacoes_ceasa.storage.sqlite import SQLiteStorage
from cotacoes_ceasa.workflows.collection import collect_and_report, download_and_report
from cotacoes_ceasa.workflows.raw_processing import process_raw_and_report


def run_source(
    args,
    config: AppConfig,
    output: TerminalOutput,
    show_summary: bool = True,
) -> None:
    """Executa a operacao solicitada para uma fonte."""
    source_config = config.sources[args.source]
    operation = resolve_source_operation(args)
    output.header(
        operation,
        build_source_execution_details(args, source_config),
    )
    collector = build_collector(
        args=args,
        config=config,
        source_config=source_config,
    )
    source_parser = build_source_parser(args.source)

    if args.list_categories:
        output.section("Categorias")
        output.info("Descobrindo categorias disponiveis.")
        categories = collector.discover_categories()
        output.success(f"{len(categories)} categoria(s) descoberta(s).")

        for category in categories:
            output.success(f"{category.slug} | {category.name}")

        if show_summary:
            output.summary((("Categorias", len(categories)),))
        return

    if args.process_raw:
        cotacoes = process_raw_and_report(
            parser=source_parser,
            raw_dir=Path(args.raw_dir),
            source_slug=args.source,
            base_url=args.base_url or source_config.base_url,
            output=output,
        )
        output.section("Persistencia")
        output.info(f"Salvando cotacoes em {args.database_path}.")
        inserted_count = save_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
        )
        output.success(f"{inserted_count} registro(s) novo(s) salvo(s).")
        if show_summary:
            output.summary(
                (
                    ("Cotacoes processadas", len(cotacoes)),
                    ("Registros novos", inserted_count),
                    ("Banco", args.database_path),
                )
            )
        return

    if args.save:
        cotacoes = collect_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
            output=output,
        )
        output.section("Persistencia")
        output.info(f"Salvando cotacoes em {args.database_path}.")
        inserted_count = save_cotacoes(
            args=args,
            cotacoes=cotacoes,
            source_config=source_config,
        )
        output.success(f"{inserted_count} registro(s) novo(s) salvo(s).")
        if show_summary:
            output.summary(
                (
                    ("Cotacoes extraidas", len(cotacoes)),
                    ("Registros novos", inserted_count),
                    ("Banco", args.database_path),
                )
            )
        return

    if args.download_only:
        saved_files = download_and_report(
            collector=collector,
            target_date=parse_target_date(args.target_date),
            quotes_back=args.quotes_back,
            output=output,
        )
        if show_summary:
            output.summary((("Arquivos salvos", len(saved_files)),))
        return

    cotacoes = collect_and_report(
        collector=collector,
        target_date=parse_target_date(args.target_date),
        quotes_back=args.quotes_back,
        output=output,
    )
    if show_summary:
        output.summary((("Cotacoes extraidas", len(cotacoes)),))


def build_collector(args, config: AppConfig, source_config: SourceConfig):
    return build_registered_collector(
        source_slug=args.source,
        base_url=args.base_url or source_config.base_url,
        raw_dir=Path(args.raw_dir),
        http_timeout_seconds=args.http_timeout_seconds,
        request_delay_seconds=args.request_delay_seconds,
        reuse_raw_before_request=config.reuse_raw_before_request,
        target_date=(
            parse_target_date(args.target_date)
            if args.source == "ceasa-pr"
            else None
        ),
        quotes_back=args.quotes_back,
    )


def save_cotacoes(args, cotacoes: list[Cotacao], source_config: SourceConfig) -> int:
    storage = SQLiteStorage(Path(args.database_path))

    return storage.save_cotacoes(
        cotacoes=cotacoes,
        source_slug=args.source,
        source_name=source_config.name,
        state_name=source_config.state,
        uf=source_config.uf,
        city=source_config.city,
        source_url=source_config.base_url,
    )


def resolve_source_operation(args) -> str:
    if args.list_categories:
        return "Listar categorias"

    if args.process_raw:
        return "Processar arquivos brutos"

    if args.save:
        return "Coletar e salvar cotacoes"

    if args.download_only:
        return "Baixar arquivos brutos"

    return "Coletar e extrair cotacoes"


def build_source_execution_details(
    args,
    source_config: SourceConfig,
) -> tuple[tuple[str, object], ...]:
    details: list[tuple[str, object]] = [
        ("Fonte", f"{source_config.name} ({args.source})"),
    ]

    if not args.list_categories and not args.process_raw:
        details.extend(
            [
                ("Data limite", args.target_date or "ultima disponivel"),
                ("Cotacoes anteriores", args.quotes_back),
            ]
        )

    if not args.list_categories:
        details.append(("Diretorio raw", args.raw_dir))

    if args.save or args.process_raw:
        details.append(("Banco", args.database_path))

    return tuple(details)
