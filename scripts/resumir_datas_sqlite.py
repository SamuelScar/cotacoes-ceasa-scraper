import argparse
import sqlite3
from pathlib import Path


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

    with sqlite3.connect(database_uri, uri=True) as connection:
        return connection.execute(
            """
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
        ).fetchall()


if __name__ == "__main__":
    main()
