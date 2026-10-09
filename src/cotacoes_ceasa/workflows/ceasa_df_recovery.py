import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Mapping

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.normalizers.text import normalize_key
from cotacoes_ceasa.normalizers.unit import normalize_unit
from cotacoes_ceasa.parsers.ceasa_df import PRICE_CATEGORY, CeasaDfParser
from cotacoes_ceasa.parsers.pdf import (
    PDF_TEXT_CACHE_VERSION,
    configure_pdf_text_cache,
    get_pdf_text_cache_stats,
    reset_pdf_text_cache_stats,
)
from cotacoes_ceasa.storage.sqlite import (
    HISTORICAL_PROVENANCE_MIGRATION,
    SQLITE_SCHEMA_VERSION,
    SQLiteStorage,
)
from cotacoes_ceasa.storage.sqlite_v4 import require_legacy_sqlite_schema


CEASA_DF_RECOVERY_PLAN_SCHEMA_VERSION = 2
CEASA_DF_RECOVERY_MIGRATION = "issue6_ceasa_df_variedades_v1"
SOURCE_SLUG = "ceasa-df"
APPLE_PRODUCT_KEY = "maca"
TARGET_CLASSIFICATIONS = {
    "FUJI CAT-1 TP.150 A 175": "CAT-1 TP.150 A 175",
    "GALA CAT-1 TP.150 A 175": "CAT-1 TP.150 A 175",
    "FUJI COMERCIAL - SOLTA": "COMERCIAL - SOLTA",
    "GALA COMERCIAL - SOLTA": "COMERCIAL - SOLTA",
}
ISSUE_6_HISTORICAL_FLOOR = {
    "collections": 143,
    "unique_pdfs": 45,
    "stored_target_rows": 450,
    "parsed_target_occurrences": 572,
    "rows_to_update": 450,
    "existing_provenance_rows": 450,
    "recovered_occurrences": 122,
    "unique_collision_documents": 35,
    "unique_distinctions": 40,
    "logical_inserts": 40,
}

RECOVERY_SUMMARY_FIELDS = (
    "collections",
    "unique_pdfs",
    "stored_target_rows",
    "parsed_target_occurrences",
    "rows_to_update",
    "existing_provenance_rows",
    "existing_provenance_reassignments",
    "recovered_occurrences",
    "unique_collision_documents",
    "unique_distinctions",
    "logical_inserts",
    "recovery_provenance_rows",
    "canonical_key_updates",
    "legacy_duplicate_keys_preserved_until_baseline",
)


def build_recovery_summary_checks(
    summary: Mapping[str, object],
) -> dict[str, bool]:
    if not all(
        isinstance(summary.get(field), int)
        and not isinstance(summary.get(field), bool)
        for field in RECOVERY_SUMMARY_FIELDS
    ):
        return {"summary_fields_complete": False}

    values = {field: int(summary[field]) for field in RECOVERY_SUMMARY_FIELDS}
    checks = {
        "summary_fields_complete": True,
        "summary_values_non_negative": all(
            value >= 0 for value in values.values()
        ),
        "unique_pdfs_within_collections": (
            0 < values["unique_pdfs"] <= values["collections"]
        ),
        "target_occurrences_match_collections": (
            values["parsed_target_occurrences"]
            == values["collections"] * len(TARGET_CLASSIFICATIONS)
        ),
        "stored_rows_match_updates": (
            values["stored_target_rows"] == values["rows_to_update"]
        ),
        "updates_have_historical_provenance": (
            values["existing_provenance_rows"] == values["rows_to_update"]
        ),
        "existing_provenance_remains_on_canonical_target": (
            values["existing_provenance_reassignments"] == 0
        ),
        "recoveries_match_parser_deficit": (
            values["recovered_occurrences"]
            == values["parsed_target_occurrences"] - values["rows_to_update"]
        ),
        "recoveries_have_provenance": (
            values["recovery_provenance_rows"]
            == values["recovered_occurrences"]
        ),
        "collision_documents_within_unique_pdfs": (
            values["unique_collision_documents"] <= values["unique_pdfs"]
        ),
        "distinctions_cover_collision_documents": (
            values["unique_distinctions"]
            >= values["unique_collision_documents"]
        ),
        "logical_inserts_within_distinctions": (
            0 < values["logical_inserts"] <= values["unique_distinctions"]
        ),
        "updates_partitioned_by_canonical_role": (
            values["canonical_key_updates"]
            + values["legacy_duplicate_keys_preserved_until_baseline"]
            == values["rows_to_update"]
        ),
    }
    checks.update(
        {
            f"historical_floor_{field}": values[field] >= minimum
            for field, minimum in ISSUE_6_HISTORICAL_FLOOR.items()
        }
    )
    return checks


def create_ceasa_df_recovery_plan(
    database_path: Path,
    project_root: Path,
    raw_directory: Path,
    pdf_cache_directory: Path,
) -> dict[str, object]:
    database_path = database_path.resolve()
    project_root = project_root.resolve()
    raw_directory = raw_directory.resolve()
    pdf_cache_directory = pdf_cache_directory.resolve()

    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite candidato nao encontrado: {database_path}")
    require_legacy_sqlite_schema(
        database_path,
        "O planejamento da recuperacao da CEASA-DF",
    )
    if not raw_directory.is_dir():
        raise FileNotFoundError(f"Diretorio de raws nao encontrado: {raw_directory}")
    if not pdf_cache_directory.is_dir():
        raise FileNotFoundError(
            f"Diretorio do cache de PDF nao encontrado: {pdf_cache_directory}"
        )

    database_stat = database_path.stat()
    database_signature_before = _file_signature(database_path)
    database_uri = f"{database_path.as_uri()}?mode=ro"
    storage = SQLiteStorage(database_path)
    parser = CeasaDfParser()
    raw_index = _build_raw_index(raw_directory)
    configure_pdf_text_cache(pdf_cache_directory)
    reset_pdf_text_cache_stats()

    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        preflight = _validate_candidate(connection)
        collections = _load_collections(connection)
        stored_by_collection = _load_stored_targets(connection)

        resolved_raws: dict[int, Path] = {}
        parsed_by_hash: dict[str, list[Cotacao]] = {}
        representative_raw_by_hash: dict[str, Path] = {}
        raw_signatures: dict[Path, tuple[int, int]] = {}
        cache_signatures: dict[Path, tuple[int, int]] = {}

        for collection in collections:
            collection_id = int(collection["id"])
            raw_hash = str(collection["hash_raw"])
            raw_path = _resolve_raw_path(
                project_root=project_root,
                raw_directory=raw_directory,
                stored_path=str(collection["arquivo_raw"]),
                expected_hash=raw_hash,
                raw_index=raw_index,
            )
            resolved_raws[collection_id] = raw_path
            raw_signatures.setdefault(raw_path, _file_signature(raw_path))

            if raw_hash not in parsed_by_hash:
                cache_path = _validate_pdf_cache(pdf_cache_directory, raw_hash)
                cache_signatures[cache_path] = _file_signature(cache_path)
                content = raw_path.read_bytes()
                actual_hash = hashlib.sha256(content).hexdigest()
                if actual_hash != raw_hash:
                    raise RuntimeError(
                        f"Hash divergente no raw {raw_path}: "
                        f"esperado {raw_hash}, obtido {actual_hash}"
                    )
                parsed = parser.parse_category(
                    content,
                    PRICE_CATEGORY.slug,
                    str(collection["url_origem"]),
                )
                targets = [
                    quote
                    for quote in parsed
                    if normalize_key(quote.produto) == APPLE_PRODUCT_KEY
                    and quote.classificacao in TARGET_CLASSIFICATIONS
                ]
                if len(targets) != 4:
                    raise RuntimeError(
                        f"O PDF {raw_hash} gerou {len(targets)} linhas alvo; "
                        "eram esperadas 4."
                    )
                parsed_by_hash[raw_hash] = targets
                representative_raw_by_hash[raw_hash] = raw_path

        updates: list[dict[str, object]] = []
        recoveries: list[dict[str, object]] = []
        deficit_signature_by_hash: dict[str, tuple[tuple[str, ...], ...]] = {}
        parsed_target_occurrences = 0

        for collection in collections:
            collection_id = int(collection["id"])
            raw_hash = str(collection["hash_raw"])
            stored_rows = stored_by_collection.get(collection_id, [])
            if not stored_rows:
                raise RuntimeError(
                    f"A coleta {collection_id} nao possui linhas alvo armazenadas."
                )

            market_slugs = {str(row["market_slug"]) for row in stored_rows}
            if len(market_slugs) != 1:
                raise RuntimeError(
                    f"A coleta {collection_id} possui entrepostos ambiguos: "
                    f"{sorted(market_slugs)}"
                )
            market_slug = next(iter(market_slugs))
            expected_rows = [
                _build_expected_row(storage, quote, market_slug, position)
                for position, quote in enumerate(parsed_by_hash[raw_hash], start=1)
            ]
            parsed_target_occurrences += len(expected_rows)

            expected_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
            for expected in expected_rows:
                expected_groups[str(expected["legacy_content_key"])].append(expected)

            stored_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for stored in stored_rows:
                legacy_content_key = storage.build_content_key(
                    identity_key=str(stored["chave_identidade"]),
                    preco_minimo=stored["preco_minimo"],
                    preco_comum=stored["preco_comum"],
                    preco_maximo=stored["preco_maximo"],
                    situacao_mercado=stored["situacao_mercado"],
                )
                stored_groups[legacy_content_key].append(stored)

            unexpected_keys = sorted(set(stored_groups) - set(expected_groups))
            missing_entire_groups = sorted(set(expected_groups) - set(stored_groups))
            if unexpected_keys or missing_entire_groups:
                raise RuntimeError(
                    f"Pareamento incompleto na coleta {collection_id}: "
                    f"inesperadas={len(unexpected_keys)}, "
                    f"ausentes={len(missing_entire_groups)}"
                )

            collection_deficits: list[tuple[str, ...]] = []
            for legacy_content_key in sorted(expected_groups):
                expected_group = sorted(
                    expected_groups[legacy_content_key],
                    key=_expected_sort_key,
                )
                stored_group = sorted(
                    stored_groups[legacy_content_key],
                    key=lambda row: int(row["id"]),
                )
                if len(stored_group) > len(expected_group):
                    raise RuntimeError(
                        f"A coleta {collection_id} possui mais linhas armazenadas "
                        "que linhas corrigidas para a chave legada "
                        f"{legacy_content_key}."
                    )

                for stored, expected in zip(stored_group, expected_group):
                    updates.append(
                        _build_update_action(
                            collection=collection,
                            raw_path=resolved_raws[collection_id],
                            stored=stored,
                            expected=expected,
                        )
                    )

                missing = expected_group[len(stored_group) :]
                if missing:
                    collection_deficits.append(
                        tuple(str(item["new_content_key"]) for item in missing)
                    )
                for expected in missing:
                    recoveries.append(
                        _build_recovery_action(
                            collection=collection,
                            raw_path=resolved_raws[collection_id],
                            template=stored_group[0],
                            expected=expected,
                        )
                    )

            signature = tuple(sorted(collection_deficits))
            previous_signature = deficit_signature_by_hash.get(raw_hash)
            if previous_signature is None:
                deficit_signature_by_hash[raw_hash] = signature
            elif previous_signature != signature:
                raise RuntimeError(
                    f"As coletas do hash {raw_hash} possuem deficits divergentes."
                )

        logical_inserts = _assign_canonical_keys(
            connection,
            storage,
            updates,
            recoveries,
        )

    cache_stats = get_pdf_text_cache_stats()

    unique_distinctions = sum(
        sum(len(group) for group in signature)
        for signature in deficit_signature_by_hash.values()
    )
    unique_collision_documents = sum(
        bool(signature) for signature in deficit_signature_by_hash.values()
    )
    summary = {
        "collections": len(collections),
        "unique_pdfs": len(parsed_by_hash),
        "stored_target_rows": sum(len(rows) for rows in stored_by_collection.values()),
        "parsed_target_occurrences": parsed_target_occurrences,
        "rows_to_update": len(updates),
        "existing_provenance_rows": len(updates),
        "existing_provenance_reassignments": sum(
            action["old_canonical_quote_id"]
            != action["new_canonical_quote_id"]
            for action in updates
        ),
        "recovered_occurrences": len(recoveries),
        "unique_collision_documents": unique_collision_documents,
        "unique_distinctions": unique_distinctions,
        "logical_inserts": len(logical_inserts),
        "recovery_provenance_rows": len(recoveries),
        "canonical_key_updates": sum(
            bool(action["is_canonical"])
            and action["old_unique_key"] != action["new_unique_key"]
            for action in updates
        ),
        "legacy_duplicate_keys_preserved_until_baseline": sum(
            not bool(action["is_canonical"]) for action in updates
        ),
    }
    checks = build_recovery_summary_checks(summary)
    checks["all_raw_hashes_verified"] = len(resolved_raws) == len(collections)
    checks["all_pdf_caches_verified"] = len(parsed_by_hash) == len(
        representative_raw_by_hash
    ) == len(cache_signatures)
    checks["all_update_quote_ids_unique"] = len(
        {int(action["quote_id"]) for action in updates}
    ) == len(updates)
    checks["all_existing_provenance_ids_unique"] = len(
        {int(action["provenance_id"]) for action in updates}
    ) == len(updates)
    checks["all_recovery_provenance_keys_unique"] = len(
        {str(action["provenance_key"]) for action in recoveries}
    ) == len(recoveries)
    checks["source_database_unchanged"] = (
        database_signature_before == _file_signature(database_path)
    )
    checks["raw_files_unchanged"] = all(
        signature == _file_signature(path)
        for path, signature in raw_signatures.items()
    )
    checks["pdf_cache_files_unchanged"] = all(
        signature == _file_signature(path)
        for path, signature in cache_signatures.items()
    )
    checks["pdf_cache_writes_zero"] = cache_stats.writes == 0
    checks["source_schema_version"] = (
        preflight["schema_version"] == SQLITE_SCHEMA_VERSION
    )
    checks["source_migration_marker"] = bool(preflight["migration_applied"])
    checks["source_checkpoint_complete"] = (
        preflight["migration_checkpoint"] == preflight["max_quote_id"]
    )
    status = "valid" if all(checks.values()) else "invalid"

    return {
        "schema_version": CEASA_DF_RECOVERY_PLAN_SCHEMA_VERSION,
        "status": status,
        "migration_key": CEASA_DF_RECOVERY_MIGRATION,
        "source": {
            "database_path": database_path.as_posix(),
            "size_bytes": database_stat.st_size,
            "mtime_ns": database_stat.st_mtime_ns,
            **preflight,
        },
        "historical_floor": ISSUE_6_HISTORICAL_FLOOR,
        "summary": summary,
        "checks": checks,
        "safety": {
            "database_opened_read_only": True,
            "database_unchanged": checks["source_database_unchanged"],
            "database_rows_changed": 0,
            "raws_changed": 0,
            "pdf_cache_changed": 0,
            "pdf_cache_hits": cache_stats.hits,
            "pdf_cache_misses": cache_stats.misses,
            "pdf_cache_writes": cache_stats.writes,
            "next_candidate": (
                "auditoria_cotacoes/cotacoes-ceasa-df-corrigida.sqlite"
            ),
            "rollback": "descartar a futura candidata da etapa 3",
        },
        "unique_pdf_deficits": [
            {
                "hash_raw": raw_hash,
                "raw_path": representative_raw_by_hash[raw_hash].as_posix(),
                "missing_groups": [list(group) for group in signature],
                "missing_distinctions": sum(len(group) for group in signature),
            }
            for raw_hash, signature in sorted(deficit_signature_by_hash.items())
            if signature
        ],
        "updates": sorted(
            updates,
            key=lambda item: (
                int(item["collection_id"]),
                int(item["quote_id"]),
            ),
        ),
        "logical_inserts": logical_inserts,
        "recovery_occurrences": sorted(
            recoveries,
            key=lambda item: (
                str(item["new_content_key"]),
                int(item["collection_id"]),
            ),
        ),
    }


def write_ceasa_df_recovery_plan(
    plan: dict[str, object],
    json_path: Path,
    csv_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if json_path.exists():
        raise FileExistsError(f"Manifesto JSON ja existe: {json_path}")
    if csv_path.exists():
        raise FileExistsError(f"Manifesto CSV ja existe: {csv_path}")

    json_temporary = json_path.with_name(f"{json_path.name}.tmp")
    csv_temporary = csv_path.with_name(f"{csv_path.name}.tmp")
    actions = list(plan["updates"]) + list(plan["recovery_occurrences"])
    columns = (
        "action",
        "hash_raw",
        "raw_path",
        "collection_id",
        "quote_id",
        "template_quote_id",
        "old_classification",
        "new_classification",
        "old_identity_key",
        "new_identity_key",
        "old_unique_key",
        "new_unique_key",
        "new_content_key",
        "is_canonical",
        "provenance_id",
        "provenance_key",
        "old_canonical_quote_id",
        "new_canonical_quote_id",
        "provenance_origin_quote_id",
        "provenance_origin_quote_key",
        "provenance_registration_origin",
        "provenance_order",
        "creates_logical_quote",
        "quote_date",
        "price_min",
        "price_common",
        "price_max",
        "market_status",
    )

    try:
        json_temporary.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with csv_temporary.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for action in actions:
                writer.writerow({column: action.get(column) for column in columns})
        json_temporary.replace(json_path)
        csv_temporary.replace(csv_path)
    except Exception:
        for temporary in (json_temporary, csv_temporary):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def _validate_candidate(connection: sqlite3.Connection) -> dict[str, object]:
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    max_quote_id = int(
        connection.execute("SELECT COALESCE(MAX(id), 0) FROM cotacoes").fetchone()[0]
    )
    migration_row = connection.execute(
        """
        SELECT cotacao_id_maximo
        FROM schema_migrations
        WHERE chave = ?
        """,
        (HISTORICAL_PROVENANCE_MIGRATION,),
    ).fetchone()
    migration_applied = migration_row is not None
    migration_checkpoint = int(migration_row[0]) if migration_row else None

    if schema_version != SQLITE_SCHEMA_VERSION:
        raise RuntimeError(
            f"Schema SQLite inesperado: {schema_version}; "
            f"esperado {SQLITE_SCHEMA_VERSION}."
        )
    if not migration_applied or migration_checkpoint != max_quote_id:
        raise RuntimeError(
            "A candidata de origem nao possui migracao de proveniencia completa."
        )

    return {
        "schema_version": schema_version,
        "max_quote_id": max_quote_id,
        "migration_applied": migration_applied,
        "migration_checkpoint": migration_checkpoint,
    }


def _load_collections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            col.id,
            col.chave_unica AS collection_key,
            col.arquivo_raw,
            col.hash_raw,
            col.url_origem
        FROM coletas col
        JOIN ceasas ce ON ce.id = col.ceasa_id
        WHERE ce.slug = ?
          AND col.arquivo_raw IS NOT NULL
          AND col.hash_raw IS NOT NULL
        ORDER BY col.id
        """,
        (SOURCE_SLUG,),
    ).fetchall()
    if not rows:
        raise RuntimeError("Nenhuma coleta da CEASA-DF foi encontrada.")
    return rows


def _load_stored_targets(
    connection: sqlite3.Connection,
) -> dict[int, list[sqlite3.Row]]:
    rows = connection.execute(
        """
        SELECT
            co.id,
            co.chave_unica,
            co.chave_identidade,
            co.coleta_id,
            co.data_cotacao,
            co.preco_minimo,
            co.preco_comum,
            co.preco_maximo,
            co.procedencia,
            co.classificacao,
            co.situacao_mercado,
            ent.slug AS market_slug,
            cat.slug AS category_slug,
            p.nome_normalizado AS product_key,
            au.chave_unica AS presentation_key,
            cp.id AS provenance_id,
            cp.chave_unica AS provenance_key,
            cp.cotacao_id AS old_canonical_quote_id,
            cp.coleta_id AS provenance_collection_id,
            cp.cotacao_origem_id AS provenance_origin_quote_id,
            cp.chave_cotacao_origem AS provenance_origin_quote_key,
            cp.origem_registro AS provenance_registration_origin
        FROM cotacoes co
        JOIN coletas col ON col.id = co.coleta_id
        JOIN ceasas ce ON ce.id = col.ceasa_id
        JOIN categorias cat ON cat.id = co.categoria_id
        JOIN produto_aliases pa ON pa.id = co.produto_alias_id
        JOIN produtos p ON p.id = pa.produto_id
        LEFT JOIN entrepostos ent ON ent.id = co.entreposto_id
        LEFT JOIN apresentacoes_unidade au
          ON au.id = co.apresentacao_unidade_id
        JOIN cotacao_proveniencias cp
          ON cp.chave_unica = 'migracao-historica:' || co.id
        WHERE ce.slug = ?
          AND p.nome_normalizado = 'maçã'
          AND co.classificacao IN (
              'CAT-1 TP.150 A 175',
              'COMERCIAL - SOLTA'
          )
        ORDER BY co.coleta_id, co.id
        """,
        (SOURCE_SLUG,),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[int(row["coleta_id"])].append(row)
    return grouped


def _build_expected_row(
    storage: SQLiteStorage,
    quote: Cotacao,
    market_slug: str,
    position: int,
) -> dict[str, object]:
    classification = str(quote.classificacao)
    legacy_classification = TARGET_CLASSIFICATIONS[classification]
    presentation_key = storage._build_presentation_key(normalize_unit(quote.unidade))
    product_key = storage._normalize_name(quote.produto)
    legacy_quote = replace(quote, classificacao=legacy_classification)
    legacy_identity_key = storage._build_identity_key(
        source_slug=SOURCE_SLUG,
        market_slug=market_slug,
        category_slug=quote.categoria,
        product_key=product_key,
        presentation_key=presentation_key,
        cotacao=legacy_quote,
    )
    legacy_content_key = storage.build_content_key(
        identity_key=legacy_identity_key,
        preco_minimo=quote.preco_minimo,
        preco_comum=quote.preco_comum,
        preco_maximo=quote.preco_maximo,
        situacao_mercado=quote.situacao_mercado,
    )
    new_identity_key = storage._build_identity_key(
        source_slug=SOURCE_SLUG,
        market_slug=market_slug,
        category_slug=quote.categoria,
        product_key=product_key,
        presentation_key=presentation_key,
        cotacao=quote,
    )
    new_content_key = storage.build_content_key(
        identity_key=new_identity_key,
        preco_minimo=quote.preco_minimo,
        preco_comum=quote.preco_comum,
        preco_maximo=quote.preco_maximo,
        situacao_mercado=quote.situacao_mercado,
    )
    return {
        "position": position,
        "old_classification": legacy_classification,
        "new_classification": classification,
        "legacy_identity_key": legacy_identity_key,
        "legacy_content_key": legacy_content_key,
        "new_identity_key": new_identity_key,
        "new_content_key": new_content_key,
        "quote_date": quote.data_cotacao.isoformat() if quote.data_cotacao else None,
        "price_min": _value_text(quote.preco_minimo),
        "price_common": _value_text(quote.preco_comum),
        "price_max": _value_text(quote.preco_maximo),
        "market_status": quote.situacao_mercado,
    }


def _build_update_action(
    collection: sqlite3.Row,
    raw_path: Path,
    stored: sqlite3.Row,
    expected: dict[str, object],
) -> dict[str, object]:
    if str(stored["chave_identidade"]) != expected["legacy_identity_key"]:
        raise RuntimeError(
            f"A cotacao {stored['id']} nao corresponde a identidade legada "
            "calculada pelo parser."
        )
    if (
        int(stored["provenance_collection_id"]) != int(collection["id"])
        or int(stored["provenance_origin_quote_id"]) != int(stored["id"])
        or str(stored["provenance_origin_quote_key"])
        != str(stored["chave_unica"])
        or str(stored["provenance_registration_origin"])
        != "migracao_historica"
    ):
        raise RuntimeError(
            f"A proveniencia historica da cotacao {stored['id']} "
            "esta incompleta ou inconsistente."
        )
    return {
        "action": "update_existing",
        "hash_raw": str(collection["hash_raw"]),
        "raw_path": raw_path.as_posix(),
        "collection_id": int(collection["id"]),
        "quote_id": int(stored["id"]),
        "template_quote_id": int(stored["id"]),
        "old_classification": str(stored["classificacao"]),
        "new_classification": expected["new_classification"],
        "old_identity_key": str(stored["chave_identidade"]),
        "new_identity_key": expected["new_identity_key"],
        "old_unique_key": str(stored["chave_unica"]),
        "new_unique_key": str(stored["chave_unica"]),
        "new_content_key": expected["new_content_key"],
        "is_canonical": False,
        "provenance_id": int(stored["provenance_id"]),
        "provenance_key": str(stored["provenance_key"]),
        "old_canonical_quote_id": int(stored["old_canonical_quote_id"]),
        "new_canonical_quote_id": None,
        "provenance_origin_quote_id": int(stored["id"]),
        "provenance_origin_quote_key": str(stored["chave_unica"]),
        "provenance_registration_origin": "migracao_historica",
        "creates_logical_quote": False,
        "quote_date": expected["quote_date"],
        "price_min": expected["price_min"],
        "price_common": expected["price_common"],
        "price_max": expected["price_max"],
        "market_status": expected["market_status"],
    }


def _build_recovery_action(
    collection: sqlite3.Row,
    raw_path: Path,
    template: sqlite3.Row,
    expected: dict[str, object],
) -> dict[str, object]:
    return {
        "action": "recover_missing",
        "hash_raw": str(collection["hash_raw"]),
        "raw_path": raw_path.as_posix(),
        "collection_id": int(collection["id"]),
        "collection_key": str(collection["collection_key"]),
        "quote_id": None,
        "template_quote_id": int(template["id"]),
        "old_classification": expected["old_classification"],
        "new_classification": expected["new_classification"],
        "old_identity_key": expected["legacy_identity_key"],
        "new_identity_key": expected["new_identity_key"],
        "old_unique_key": None,
        "new_unique_key": expected["new_content_key"],
        "new_content_key": expected["new_content_key"],
        "is_canonical": True,
        "provenance_id": None,
        "provenance_key": None,
        "old_canonical_quote_id": None,
        "new_canonical_quote_id": None,
        "provenance_origin_quote_id": None,
        "provenance_origin_quote_key": expected["new_content_key"],
        "provenance_registration_origin": "recuperacao_ceasa_df_issue6",
        "creates_logical_quote": False,
        "quote_date": expected["quote_date"],
        "price_min": expected["price_min"],
        "price_common": expected["price_common"],
        "price_max": expected["price_max"],
        "market_status": expected["market_status"],
    }


def _assign_canonical_keys(
    connection: sqlite3.Connection,
    storage: SQLiteStorage,
    updates: list[dict[str, object]],
    recoveries: list[dict[str, object]],
) -> list[dict[str, object]]:
    updates_by_content: dict[str, list[dict[str, object]]] = defaultdict(list)
    for update in updates:
        updates_by_content[str(update["new_content_key"])].append(update)

    recoveries_by_content: dict[str, list[dict[str, object]]] = defaultdict(list)
    for recovery in recoveries:
        recoveries_by_content[str(recovery["new_content_key"])].append(recovery)

    desired_keys = sorted(set(updates_by_content) | set(recoveries_by_content))
    holders: dict[str, int] = {}
    for chunk in _chunks(desired_keys, 900):
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT chave_unica, id
            FROM cotacoes
            WHERE chave_unica IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        holders.update({str(row["chave_unica"]): int(row["id"]) for row in rows})

    for content_key, group in updates_by_content.items():
        quote_ids = {int(item["quote_id"]) for item in group}
        holder = holders.get(content_key)
        if holder is not None and holder not in quote_ids:
            raise RuntimeError(
                f"A chave corrigida {content_key} ja pertence a cotacao {holder}."
            )
        canonical_id = holder if holder is not None else min(quote_ids)
        for update in group:
            if int(update["quote_id"]) == canonical_id:
                update["is_canonical"] = True
                update["new_unique_key"] = content_key
            update["new_canonical_quote_id"] = canonical_id

    logical_inserts: list[dict[str, object]] = []
    provenance_occurrences: dict[tuple[str, str], int] = defaultdict(int)
    for content_key, occurrences in sorted(recoveries_by_content.items()):
        occurrences.sort(key=lambda item: int(item["collection_id"]))
        existing_updates = updates_by_content.get(content_key, [])
        if existing_updates:
            canonical_id = int(existing_updates[0]["new_canonical_quote_id"])
            for occurrence in occurrences:
                occurrence["new_canonical_quote_id"] = canonical_id
        elif content_key in holders:
            raise RuntimeError(
                f"A distincao ausente {content_key} ja existe na candidata."
            )
        else:
            occurrences[0]["creates_logical_quote"] = True
            representative = occurrences[0]
            logical_inserts.append(
                {
                    "new_content_key": content_key,
                    "new_identity_key": representative["new_identity_key"],
                    "new_classification": representative["new_classification"],
                    "template_quote_id": representative["template_quote_id"],
                    "quote_date": representative["quote_date"],
                    "price_min": representative["price_min"],
                    "price_common": representative["price_common"],
                    "price_max": representative["price_max"],
                    "market_status": representative["market_status"],
                    "occurrence_count": len(occurrences),
                    "collection_ids": [
                        int(item["collection_id"]) for item in occurrences
                    ],
                    "raw_hashes": sorted(
                        {str(item["hash_raw"]) for item in occurrences}
                    ),
                }
            )

        for occurrence in occurrences:
            provenance_identity = (
                str(occurrence["collection_key"]),
                content_key,
            )
            provenance_occurrences[provenance_identity] += 1
            order = provenance_occurrences[provenance_identity]
            digest = storage._hash_values(
                (
                    CEASA_DF_RECOVERY_MIGRATION,
                    *provenance_identity,
                    order,
                )
            )
            occurrence["provenance_key"] = f"issue6-ceasa-df:{digest}"
            occurrence["provenance_order"] = order

    recovery_keys = [str(item["provenance_key"]) for item in recoveries]
    for chunk in _chunks(recovery_keys, 900):
        placeholders = ", ".join("?" for _ in chunk)
        if connection.execute(
            f"""
            SELECT 1
            FROM cotacao_proveniencias
            WHERE chave_unica IN ({placeholders})
            LIMIT 1
            """,
            chunk,
        ).fetchone():
            raise RuntimeError(
                "Uma chave de proveniencia recuperada ja existe na candidata."
            )

    return logical_inserts


def _build_raw_index(raw_directory: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = defaultdict(list)
    for path in raw_directory.rglob("*.pdf"):
        index[path.name].append(path.resolve())
    return index


def _resolve_raw_path(
    project_root: Path,
    raw_directory: Path,
    stored_path: str,
    expected_hash: str,
    raw_index: dict[str, list[Path]],
) -> Path:
    raw_path = Path(stored_path)
    candidates: list[Path] = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        candidates.extend(
            (
                project_root / raw_path,
                raw_directory / raw_path.name,
            )
        )
    candidates.extend(raw_index.get(raw_path.name, []))

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual_hash == expected_hash:
            return candidate

    raise FileNotFoundError(
        f"Raw nao encontrado para {stored_path} com hash {expected_hash}."
    )


def _validate_pdf_cache(cache_directory: Path, raw_hash: str) -> Path:
    cache_path = cache_directory / raw_hash[:2] / f"{raw_hash}.json"
    if not cache_path.is_file():
        raise FileNotFoundError(f"Cache de PDF nao encontrado: {cache_path}")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    if payload.get("version") != PDF_TEXT_CACHE_VERSION:
        raise RuntimeError(f"Versao inesperada no cache {cache_path}.")
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages or not all(
        isinstance(page, str) for page in pages
    ):
        raise RuntimeError(f"Cache de PDF invalido: {cache_path}")
    return cache_path


def _expected_sort_key(item: dict[str, object]) -> int:
    return int(item["position"])


def _value_text(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns
