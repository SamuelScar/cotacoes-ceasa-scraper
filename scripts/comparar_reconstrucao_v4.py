#!/usr/bin/env python3
"""Compara um SQLite legado com uma reconstrucao no esquema v4."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseInfo:
    path: Path
    user_version: int
    tables: frozenset[str]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compara cobertura por fonte e data entre o SQLite legado e o v4."
        )
    )
    parser.add_argument("--legacy", type=Path, required=True)
    parser.add_argument("--v4", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    result = compare_databases(args.legacy, args.v4)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.report.write_text(render_report(result), encoding="utf-8")

    print(f"Resultado JSON: {args.output}")
    print(f"Relatorio: {args.report}")
    print(f"Status: {result['status']}")
    return 0 if result["status"] == "equivalent" else 2


def compare_databases(legacy_path: Path, v4_path: Path) -> dict[str, Any]:
    legacy_info = inspect_database(legacy_path)
    v4_info = inspect_database(v4_path)

    require_tables(
        legacy_info,
        {"ceasas", "coletas", "cotacoes"},
        "legado",
    )
    require_tables(
        v4_info,
        {"fontes", "coletas", "cotacoes", "schema_migrations"},
        "v4",
    )

    with connect_read_only(legacy_path) as legacy, connect_read_only(v4_path) as v4:
        provenance_available = "cotacao_proveniencias" in legacy_info.tables
        legacy_quote_total = scalar(legacy, "SELECT COUNT(*) FROM cotacoes")
        legacy_provenance_total = (
            scalar(legacy, "SELECT COUNT(*) FROM cotacao_proveniencias")
            if provenance_available
            else None
        )
        expected_mode = "provenance_occurrences" if provenance_available else "quotes"
        legacy_by_source_date = load_legacy_coverage(
            legacy,
            use_provenance=provenance_available,
        )
        v4_by_source_date = load_v4_coverage(v4)
        differences = build_differences(legacy_by_source_date, v4_by_source_date)
        legacy_by_document = load_legacy_document_coverage(legacy)
        v4_by_document = load_v4_document_coverage(v4)
        document_differences = build_document_differences(
            legacy_by_document,
            v4_by_document,
        )
        source_summary = summarize_sources(
            legacy_by_source_date,
            v4_by_source_date,
        )
        v4_statuses = {
            str(status): int(count)
            for status, count in v4.execute(
                "SELECT status, COUNT(*) FROM coletas GROUP BY status"
            )
        }
        v4_errors = [
            {
                "id": int(row[0]),
                "source": str(row[1]),
                "status": str(row[2]),
                "raw_path": str(row[3]) if row[3] is not None else None,
                "message": str(row[4]) if row[4] is not None else None,
            }
            for row in v4.execute(
                """
                SELECT col.id, f.slug, col.status, col.caminho_relativo_raw,
                       col.mensagem_erro
                FROM coletas col
                JOIN fontes f ON f.id = col.fonte_id
                WHERE col.status IN ('erro_download', 'erro_processamento')
                ORDER BY f.slug, col.id
                """
            )
        ]
        legacy_prohort = load_legacy_prohort(legacy)
        v4_prohort = load_v4_prohort(v4)

    equivalent = not differences
    return {
        "schema_version": 1,
        "status": "equivalent" if equivalent else "different",
        "legacy": {
            "path": legacy_info.path.as_posix(),
            "user_version": legacy_info.user_version,
            "quotes": legacy_quote_total,
            "provenance_rows": legacy_provenance_total,
        },
        "v4": {
            "path": v4_info.path.as_posix(),
            "user_version": v4_info.user_version,
            "quotes": sum(v4_by_source_date.values()),
            "collection_statuses": v4_statuses,
            "errors": v4_errors,
        },
        "comparison": {
            "expected_mode": expected_mode,
            "expected_rows": sum(legacy_by_source_date.values()),
            "actual_rows": sum(v4_by_source_date.values()),
            "difference": (
                sum(v4_by_source_date.values())
                - sum(legacy_by_source_date.values())
            ),
            "sources": source_summary,
            "different_source_dates": len(differences),
            "largest_source_date_differences": differences[:200],
            "different_documents": len(document_differences),
            "largest_document_differences": document_differences[:200],
        },
        "prohort": {
            "legacy": legacy_prohort,
            "v4": v4_prohort,
        },
    }


def inspect_database(path: Path) -> DatabaseInfo:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {path}")
    with connect_read_only(path) as connection:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        )
    return DatabaseInfo(path=path.resolve(), user_version=user_version, tables=tables)


def require_tables(info: DatabaseInfo, expected: set[str], label: str) -> None:
    missing = expected - info.tables
    if missing:
        raise RuntimeError(
            f"O banco {label} nao possui as tabelas: {', '.join(sorted(missing))}."
        )


def connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def scalar(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    return int(row[0]) if row else 0


def load_legacy_coverage(
    connection: sqlite3.Connection,
    *,
    use_provenance: bool,
) -> dict[tuple[str, str], int]:
    if use_provenance:
        query = """
            WITH canonical_collections AS (
                SELECT MIN(id) AS id
                FROM coletas
                WHERE hash_raw IS NOT NULL
                GROUP BY ceasa_id, hash_raw
            ),
            expected_quotes AS (
                SELECT
                    ce.slug AS source_slug,
                    co.data_cotacao
                FROM cotacao_proveniencias cp
                JOIN cotacoes co ON co.id = cp.cotacao_id
                JOIN coletas col ON col.id = cp.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                JOIN canonical_collections canonical ON canonical.id = col.id
                WHERE co.fonte_complemento IS NULL

                UNION ALL

                SELECT 'prohort', co.data_cotacao
                FROM cotacoes co
                WHERE co.fonte_complemento = 'prohort'
            )
            SELECT source_slug, data_cotacao, COUNT(*)
            FROM expected_quotes
            GROUP BY source_slug, data_cotacao
        """
    else:
        query = """
            SELECT ce.slug, co.data_cotacao, COUNT(*)
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            GROUP BY ce.slug, co.data_cotacao
        """
    return {
        (str(source), str(quote_date)): int(count)
        for source, quote_date, count in connection.execute(query)
    }


def load_v4_coverage(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], int]:
    return {
        (str(source), str(quote_date)): int(count)
        for source, quote_date, count in connection.execute(
            """
            SELECT f.slug, co.data_cotacao, COUNT(*)
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN fontes f ON f.id = col.fonte_id
            GROUP BY f.slug, co.data_cotacao
            """
        )
    }


def load_legacy_document_coverage(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[int, str | None]]:
    return {
        (str(source), str(raw_hash)): (
            int(count),
            str(raw_path) if raw_path is not None else None,
        )
        for source, raw_hash, raw_path, count in connection.execute(
            """
            WITH canonical_collections AS (
                SELECT ceasa_id, hash_raw, MIN(id) AS id
                FROM coletas
                WHERE hash_raw IS NOT NULL
                GROUP BY ceasa_id, hash_raw
            )
            SELECT ce.slug, col.hash_raw, col.arquivo_raw, COUNT(co.id)
            FROM canonical_collections canonical
            JOIN coletas col ON col.id = canonical.id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            LEFT JOIN cotacao_proveniencias cp ON cp.coleta_id = col.id
            LEFT JOIN cotacoes co
              ON co.id = cp.cotacao_id
             AND co.fonte_complemento IS NULL
            GROUP BY ce.slug, col.hash_raw, col.arquivo_raw
            """
        )
    }


def load_v4_document_coverage(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[int, str | None]]:
    return {
        (str(source), str(raw_hash)): (
            int(count),
            str(raw_path) if raw_path is not None else None,
        )
        for source, raw_hash, raw_path, count in connection.execute(
            """
            SELECT f.slug, col.sha256, col.caminho_relativo_raw, COUNT(co.id)
            FROM coletas col
            JOIN fontes f ON f.id = col.fonte_id
            LEFT JOIN cotacoes co ON co.coleta_id = col.id
            WHERE col.status IN ('processada', 'erro_processamento')
              AND col.sha256 IS NOT NULL
              AND f.slug <> 'prohort'
            GROUP BY f.slug, col.sha256, col.caminho_relativo_raw
            """
        )
    }


def build_document_differences(
    expected: dict[tuple[str, str], tuple[int, str | None]],
    actual: dict[tuple[str, str], tuple[int, str | None]],
) -> list[dict[str, Any]]:
    result = []
    for source, raw_hash in sorted(set(expected) | set(actual)):
        expected_count, expected_path = expected.get((source, raw_hash), (0, None))
        actual_count, actual_path = actual.get((source, raw_hash), (0, None))
        difference = actual_count - expected_count
        if difference:
            result.append(
                {
                    "source": source,
                    "sha256": raw_hash,
                    "raw_path": actual_path or expected_path,
                    "expected": expected_count,
                    "actual": actual_count,
                    "difference": difference,
                }
            )
    return sorted(
        result,
        key=lambda item: (-abs(int(item["difference"])), item["source"], item["sha256"]),
    )


def build_differences(
    expected: dict[tuple[str, str], int],
    actual: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    result = []
    for source, quote_date in sorted(set(expected) | set(actual)):
        expected_count = expected.get((source, quote_date), 0)
        actual_count = actual.get((source, quote_date), 0)
        difference = actual_count - expected_count
        if difference:
            result.append(
                {
                    "source": source,
                    "date": quote_date,
                    "expected": expected_count,
                    "actual": actual_count,
                    "difference": difference,
                }
            )
    return sorted(
        result,
        key=lambda item: (-abs(int(item["difference"])), item["source"], item["date"]),
    )


def summarize_sources(
    expected: dict[tuple[str, str], int],
    actual: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    sources = sorted({key[0] for key in expected} | {key[0] for key in actual})
    result = []
    for source in sources:
        expected_count = sum(
            count for (item_source, _), count in expected.items() if item_source == source
        )
        actual_count = sum(
            count for (item_source, _), count in actual.items() if item_source == source
        )
        result.append(
            {
                "source": source,
                "expected": expected_count,
                "actual": actual_count,
                "difference": actual_count - expected_count,
            }
        )
    return result


def load_legacy_prohort(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "quotes_marked_as_complemented": scalar(
            connection,
            "SELECT COUNT(*) FROM cotacoes WHERE fonte_complemento = 'prohort'",
        ),
        "collections_without_raw": scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM coletas
            WHERE arquivo_raw IS NULL OR hash_raw IS NULL OR baixado_em IS NULL
            """,
        ),
    }


def load_v4_prohort(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "direct_quotes": scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN fontes f ON f.id = col.fonte_id
            WHERE f.slug = 'prohort'
            """,
        ),
        "complements": scalar(connection, "SELECT COUNT(*) FROM cotacao_complementos"),
    }


def render_report(result: dict[str, Any]) -> str:
    comparison = result["comparison"]
    lines = [
        "# Comparacao da reconstrucao SQLite v4",
        "",
        f"- Status: `{result['status']}`",
        f"- Modo esperado: `{comparison['expected_mode']}`",
        f"- Registros esperados: {comparison['expected_rows']}",
        f"- Registros no v4: {comparison['actual_rows']}",
        f"- Diferenca: {comparison['difference']:+d}",
        "",
        "## Cobertura por fonte",
        "",
        "| Fonte | Esperado | V4 | Diferenca |",
        "| --- | ---: | ---: | ---: |",
    ]
    for source in comparison["sources"]:
        lines.append(
            f"| {source['source']} | {source['expected']} | "
            f"{source['actual']} | {source['difference']:+d} |"
        )
    lines.extend(
        [
            "",
            "## Maiores diferencas por fonte e data",
            "",
            "| Fonte | Data | Esperado | V4 | Diferenca |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    differences = comparison["largest_source_date_differences"]
    if differences:
        for item in differences:
            lines.append(
                f"| {item['source']} | {item['date']} | {item['expected']} | "
                f"{item['actual']} | {item['difference']:+d} |"
            )
    else:
        lines.append("| - | - | 0 | 0 | 0 |")
    lines.extend(
        [
            "",
            "## Maiores diferencas por documento",
            "",
            "| Fonte | Raw | Esperado | V4 | Diferenca |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    document_differences = comparison["largest_document_differences"]
    if document_differences:
        for item in document_differences:
            lines.append(
                f"| {item['source']} | {item['raw_path'] or item['sha256']} | "
                f"{item['expected']} | {item['actual']} | "
                f"{item['difference']:+d} |"
            )
    else:
        lines.append("| - | - | 0 | 0 | 0 |")
    lines.extend(
        [
            "",
            "## PROHORT",
            "",
            f"- Legado marcado como complemento: "
            f"{result['prohort']['legacy']['quotes_marked_as_complemented']}",
            f"- V4 direto: {result['prohort']['v4']['direct_quotes']}",
            f"- V4 complementos: {result['prohort']['v4']['complements']}",
            "",
            "## Erros registrados no v4",
            "",
        ]
    )
    errors = result["v4"]["errors"]
    if errors:
        for error in errors:
            lines.append(
                f"- `{error['source']}` coleta {error['id']}: {error['message']}"
            )
    else:
        lines.append("- Nenhum.")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
