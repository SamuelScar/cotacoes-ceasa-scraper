import argparse
import sqlite3
from pathlib import Path

from cotacoes_ceasa.storage.sqlite_v4 import detect_sqlite_schema


LEGACY_SUMMARY_QUERY = """
    SELECT
        cs.slug,
        COUNT(c.id),
        MIN(c.data_cotacao),
        MAX(c.data_cotacao)
    FROM ceasas cs
    LEFT JOIN coletas col ON col.ceasa_id = cs.id
    LEFT JOIN cotacoes c ON c.coleta_id = col.id
    GROUP BY cs.slug
    ORDER BY cs.slug
"""

V4_SUMMARY_QUERY = """
    SELECT
        f.slug,
        COUNT(c.id),
        MIN(c.data_cotacao),
        MAX(c.data_cotacao)
    FROM fontes f
    LEFT JOIN coletas col ON col.fonte_id = f.id
    LEFT JOIN cotacoes c ON c.coleta_id = col.id
    GROUP BY f.slug
    ORDER BY f.slug
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resume cobertura e datas das cotacoes por fonte."
    )
    parser.add_argument("database_path", type=Path)
    args = parser.parse_args()

    rows = load_source_summary(args.database_path)
    print("| Fonte | Cotacoes | Menor data | Maior data |")
    print("| --- | ---: | --- | --- |")

    for source_slug, quote_count, oldest_date, latest_date in rows:
        print(
            f"| {source_slug} | {quote_count} | "
            f"{oldest_date or '-'} | {latest_date or '-'} |"
        )


def load_source_summary(
    database_path: Path,
) -> list[tuple[str, int, str | None, str | None]]:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    query = (
        V4_SUMMARY_QUERY
        if detect_sqlite_schema(database_path) == "v4"
        else LEGACY_SUMMARY_QUERY
    )

    with sqlite3.connect(database_uri, uri=True) as connection:
        return connection.execute(query).fetchall()


if __name__ == "__main__":
    main()
