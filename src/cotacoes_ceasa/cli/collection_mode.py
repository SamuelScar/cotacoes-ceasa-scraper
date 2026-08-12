from argparse import Namespace
from dataclasses import replace
from pathlib import Path

from cotacoes_ceasa.config import AppConfig
from cotacoes_ceasa.sources.history import (
    history_requested,
    source_supports_history,
)
from cotacoes_ceasa.workflows.backfill import find_deferred_backfill_states


COLLECTION_MODE_LEGACY = "legacy"
COLLECTION_MODE_CURRENT = "current"
COLLECTION_MODE_BACKFILL = "backfill"
COLLECTION_MODES = (COLLECTION_MODE_CURRENT, COLLECTION_MODE_BACKFILL)


def prepare_collection_mode(args: Namespace, config: AppConfig) -> AppConfig:
    """Resolve os parametros efetivos sem alterar o modo legado."""
    args.effective_collection_mode = args.collection_mode or COLLECTION_MODE_LEGACY
    args.incremental_history = config.incremental_history
    args.requested_target_date = args.target_date
    args.requested_quotes_back = args.quotes_back
    args.backfill_excluded_sources = ()
    args.backfill_deferred_sources = ()

    if args.collection_mode is None:
        return config

    _validate_collection_operation(args)

    if args.collection_mode == COLLECTION_MODE_CURRENT:
        args.target_date = None
        args.quotes_back = 0
        args.incremental_history = False
        return config

    return _prepare_backfill(args, config)


def format_collection_mode(mode: str) -> str:
    return {
        COLLECTION_MODE_LEGACY: "legacy (configuracao existente)",
        COLLECTION_MODE_CURRENT: "current (coleta atual)",
        COLLECTION_MODE_BACKFILL: "backfill (historico)",
    }.get(mode, mode)


def _prepare_backfill(args: Namespace, config: AppConfig) -> AppConfig:
    if args.target_date:
        raise ValueError(
            "--collection-mode backfill nao aceita --target-date; "
            "use o cursor incremental."
        )

    if not history_requested(args.quotes_back):
        raise ValueError(
            "--collection-mode backfill exige --quotes-back maior que zero "
            "ou infinito."
        )

    args.incremental_history = True
    eligible_sources = {
        source_slug: source_config
        for source_slug, source_config in config.sources.items()
        if source_config.backfill_enabled
        and source_supports_history(source_slug)
    }
    args.backfill_excluded_sources = tuple(
        source_slug
        for source_slug in config.sources
        if source_slug not in eligible_sources
    )

    if args.source is not None:
        if args.source not in eligible_sources:
            raise ValueError(
                f"{args.source} nao esta habilitada para o modo backfill."
            )

        deferred_states = find_deferred_backfill_states(
            Path(args.database_path),
            (args.source,),
        )
        args.backfill_deferred_sources = tuple(deferred_states)
        return config

    if not eligible_sources:
        raise ValueError("Nenhuma fonte esta habilitada para o modo backfill.")

    deferred_states = find_deferred_backfill_states(
        Path(args.database_path),
        eligible_sources,
    )
    args.backfill_deferred_sources = tuple(deferred_states)
    runnable_sources = {
        source_slug: source_config
        for source_slug, source_config in eligible_sources.items()
        if source_slug not in deferred_states
    }

    return replace(config, sources=runnable_sources)


def _validate_collection_operation(args: Namespace) -> None:
    invalid_operation = any(
        (
            args.archive_raw_old,
            args.complement_prohort,
            args.sync_supabase,
            args.replace_supabase,
            args.process_raw,
            args.list_categories,
            args.reset_backfill_state,
            args.validate_publication,
            args.validate_checkpoint,
        )
    )

    if invalid_operation:
        raise ValueError(
            "--collection-mode so pode ser usado em operacoes de coleta ou download."
        )
