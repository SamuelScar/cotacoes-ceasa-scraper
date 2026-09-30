import csv
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from cotacoes_ceasa.storage.sqlite import (
    HISTORICAL_PROVENANCE_MIGRATION,
    SQLITE_SCHEMA_VERSION,
    SQLiteStorage,
)


PROVENANCE_REPORT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ProvenanceAnalysis:
    database_path: str
    schema_version: int
    quotes: int
    max_quote_id: int
    provenance_migration_applied: bool
    provenance_migration_quote_max_id: int | None
    provenance_rows: int
    historical_provenance_rows: int
    original_quotes_covered: int
    original_quotes_missing: int
    canonical_quotes: int
    duplicate_groups: int
    duplicate_occurrences: int
    duplicate_excess: int
    duplicate_provenances: int
    ambiguous_duplicate_groups: int

    def to_dict(self) -> dict[str, int | str | bool | None]:
        return {
            "database_path": self.database_path,
            "schema_version": self.schema_version,
            "quotes": self.quotes,
            "max_quote_id": self.max_quote_id,
            "provenance_migration_applied": self.provenance_migration_applied,
            "provenance_migration_quote_max_id": (
                self.provenance_migration_quote_max_id
            ),
            "provenance_rows": self.provenance_rows,
            "historical_provenance_rows": self.historical_provenance_rows,
            "original_quotes_covered": self.original_quotes_covered,
            "original_quotes_missing": self.original_quotes_missing,
            "canonical_quotes": self.canonical_quotes,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_occurrences": self.duplicate_occurrences,
            "duplicate_excess": self.duplicate_excess,
            "duplicate_provenances": self.duplicate_provenances,
            "ambiguous_duplicate_groups": self.ambiguous_duplicate_groups,
        }


@dataclass(frozen=True)
class ProvenanceCandidateResult:
    source_database_path: str
    candidate_database_path: str
    source_unchanged: bool
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    expected_duplicate_groups: int | None
    expected_duplicate_occurrences: int | None
    expected_duplicate_excess: int | None
    before: ProvenanceAnalysis
    after: ProvenanceAnalysis

    @property
    def valid(self) -> bool:
        expected_counts_match = (
            (
                self.expected_duplicate_groups is None
                or self.after.duplicate_groups == self.expected_duplicate_groups
            )
            and (
                self.expected_duplicate_occurrences is None
                or self.after.duplicate_occurrences
                == self.expected_duplicate_occurrences
            )
            and (
                self.expected_duplicate_excess is None
                or self.after.duplicate_excess == self.expected_duplicate_excess
            )
        )
        return (
            self.source_unchanged
            and self.quick_check == ("ok",)
            and self.foreign_key_violations == 0
            and self.after.schema_version == SQLITE_SCHEMA_VERSION
            and self.after.quotes == self.before.quotes
            and self.after.max_quote_id == self.before.max_quote_id
            and self.after.provenance_migration_applied
            and self.after.provenance_migration_quote_max_id
            == self.before.max_quote_id
            and self.after.historical_provenance_rows
            == self.before.quotes
            and self.after.duplicate_groups == self.before.duplicate_groups
            and self.after.duplicate_occurrences
            == self.before.duplicate_occurrences
            and self.after.duplicate_excess == self.before.duplicate_excess
            and self.after.original_quotes_missing == 0
            and self.after.original_quotes_covered == self.after.quotes
            and self.after.duplicate_provenances
            == self.after.duplicate_occurrences
            and self.after.ambiguous_duplicate_groups == 0
            and expected_counts_match
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PROVENANCE_REPORT_SCHEMA_VERSION,
            "status": "valid" if self.valid else "invalid",
            "source_database_path": self.source_database_path,
            "candidate_database_path": self.candidate_database_path,
            "source_unchanged": self.source_unchanged,
            "quick_check": list(self.quick_check),
            "foreign_key_violations": self.foreign_key_violations,
            "expected": {
                "duplicate_groups": self.expected_duplicate_groups,
                "duplicate_occurrences": self.expected_duplicate_occurrences,
                "duplicate_excess": self.expected_duplicate_excess,
                "ambiguous_duplicate_groups": 0,
            },
            "safety": {
                "source_opened_read_only": True,
                "historical_quotes_deleted": 0,
                "automatic_consolidation": False,
                "migration_key": HISTORICAL_PROVENANCE_MIGRATION,
                "rollback": "descartar o SQLite candidato",
            },
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


def analyze_provenance(database_path: Path) -> ProvenanceAnalysis:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        _configure_sqlite_memory(connection)
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quotes = _scalar(connection, "SELECT COUNT(*) FROM cotacoes")
        max_quote_id = _scalar(
            connection,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        )
        provenance_migration_applied = False
        provenance_migration_quote_max_id: int | None = None
        has_schema_migrations = bool(
            connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
        )
        if has_schema_migrations:
            migration_row = connection.execute(
                """
                SELECT cotacao_id_maximo
                FROM schema_migrations
                WHERE chave = ?
                """,
                (HISTORICAL_PROVENANCE_MIGRATION,),
            ).fetchone()
            if migration_row is not None:
                provenance_migration_applied = True
                provenance_migration_quote_max_id = int(migration_row[0])
        has_provenance = bool(
            connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'cotacao_proveniencias'
                """
            ).fetchone()
        )
        duplicate_groups, duplicate_occurrences, duplicate_excess = (
            _duplicate_totals(connection)
        )

        if not has_provenance:
            return ProvenanceAnalysis(
                database_path=database_path.as_posix(),
                schema_version=schema_version,
                quotes=quotes,
                max_quote_id=max_quote_id,
                provenance_migration_applied=provenance_migration_applied,
                provenance_migration_quote_max_id=(
                    provenance_migration_quote_max_id
                ),
                provenance_rows=0,
                historical_provenance_rows=0,
                original_quotes_covered=0,
                original_quotes_missing=quotes,
                canonical_quotes=0,
                duplicate_groups=duplicate_groups,
                duplicate_occurrences=duplicate_occurrences,
                duplicate_excess=duplicate_excess,
                duplicate_provenances=0,
                ambiguous_duplicate_groups=duplicate_groups,
            )

        provenance_rows = _scalar(
            connection,
            "SELECT COUNT(*) FROM cotacao_proveniencias",
        )
        historical_provenance_rows = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacao_proveniencias
            WHERE origem_registro = 'migracao_historica'
            """,
        )
        original_quotes_covered = _scalar(
            connection,
            """
            SELECT COUNT(DISTINCT cp.cotacao_origem_id)
            FROM cotacao_proveniencias cp
            JOIN cotacoes co ON co.id = cp.cotacao_origem_id
            """,
        )
        canonical_quotes = _scalar(
            connection,
            "SELECT COUNT(DISTINCT cotacao_id) FROM cotacao_proveniencias",
        )
        duplicate_provenances = _scalar(
            connection,
            """
            WITH ranked AS (
                SELECT
                    id,
                    COUNT(*) OVER (
                        PARTITION BY
                            chave_identidade,
                            preco_minimo,
                            preco_comum,
                            preco_maximo,
                            situacao_mercado
                    ) AS group_size
                FROM cotacoes
            )
            SELECT COUNT(*)
            FROM cotacao_proveniencias cp
            JOIN ranked original
              ON original.id = cp.cotacao_origem_id
            WHERE cp.origem_registro = 'migracao_historica'
              AND original.group_size > 1
            """,
        )
        ambiguous_duplicate_groups = _scalar(
            connection,
            """
            WITH duplicate_originals AS (
                SELECT
                    co.*,
                    MIN(co.id) OVER (
                        PARTITION BY
                            co.chave_identidade,
                            co.preco_minimo,
                            co.preco_comum,
                            co.preco_maximo,
                            co.situacao_mercado
                    ) AS canonical_id,
                    COUNT(*) OVER (
                        PARTITION BY
                            co.chave_identidade,
                            co.preco_minimo,
                            co.preco_comum,
                            co.preco_maximo,
                            co.situacao_mercado
                    ) AS group_size
                FROM cotacoes co
            ), ambiguous AS (
                SELECT
                    original.chave_identidade,
                    original.preco_minimo,
                    original.preco_comum,
                    original.preco_maximo,
                    original.situacao_mercado
                FROM duplicate_originals original
                LEFT JOIN cotacao_proveniencias cp
                  ON cp.cotacao_origem_id = original.id
                 AND cp.origem_registro = 'migracao_historica'
                WHERE original.group_size > 1
                GROUP BY
                    original.chave_identidade,
                    original.preco_minimo,
                    original.preco_comum,
                    original.preco_maximo,
                    original.situacao_mercado
                HAVING COUNT(DISTINCT original.id)
                    != COUNT(DISTINCT cp.cotacao_origem_id)
                    OR COUNT(DISTINCT cp.cotacao_id) != 1
                    OR MIN(cp.cotacao_id) != MIN(original.canonical_id)
            )
            SELECT COUNT(*) FROM ambiguous
            """,
        )

    return ProvenanceAnalysis(
        database_path=database_path.as_posix(),
        schema_version=schema_version,
        quotes=quotes,
        max_quote_id=max_quote_id,
        provenance_migration_applied=provenance_migration_applied,
        provenance_migration_quote_max_id=(
            provenance_migration_quote_max_id
        ),
        provenance_rows=provenance_rows,
        historical_provenance_rows=historical_provenance_rows,
        original_quotes_covered=original_quotes_covered,
        original_quotes_missing=quotes - original_quotes_covered,
        canonical_quotes=canonical_quotes,
        duplicate_groups=duplicate_groups,
        duplicate_occurrences=duplicate_occurrences,
        duplicate_excess=duplicate_excess,
        duplicate_provenances=duplicate_provenances,
        ambiguous_duplicate_groups=ambiguous_duplicate_groups,
    )


def validate_historical_provenance(database_path: Path) -> int:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        _configure_sqlite_memory(connection)
        marker = connection.execute(
            """
            SELECT cotacao_id_maximo
            FROM schema_migrations
            WHERE chave = ?
            """,
            (HISTORICAL_PROVENANCE_MIGRATION,),
        ).fetchone()
        if marker is None:
            raise RuntimeError(
                "O marcador da migracao historica de proveniencia esta ausente."
            )
        checkpoint = int(marker[0])
        _validate_historical_provenance_checkpoint(connection, checkpoint)

    return checkpoint


def create_provenance_candidate(
    source_database_path: Path,
    candidate_database_path: Path,
    expected_duplicate_groups: int | None = None,
    expected_duplicate_occurrences: int | None = None,
    expected_duplicate_excess: int | None = None,
) -> ProvenanceCandidateResult:
    if not source_database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {source_database_path}")
    if source_database_path.resolve() == candidate_database_path.resolve():
        raise ValueError("A candidata de proveniencia deve usar outro arquivo SQLite.")
    if candidate_database_path.exists():
        raise FileExistsError(
            f"A candidata de proveniencia ja existe: {candidate_database_path}"
        )

    source_stat_before = _file_signature(source_database_path)
    before = analyze_provenance(source_database_path)
    candidate_database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = candidate_database_path.with_name(
        f"{candidate_database_path.name}.tmp"
    )
    if temporary_path.exists():
        raise FileExistsError(f"Arquivo temporario ja existe: {temporary_path}")

    try:
        _backup_database(source_database_path, temporary_path)
        SQLiteStorage(temporary_path).ensure_schema()
        apply_historical_provenance_migration(temporary_path)
        after = replace(
            analyze_provenance(temporary_path),
            database_path=candidate_database_path.as_posix(),
        )
        quick_check, foreign_key_violations = _inspect_candidate(temporary_path)
        result = ProvenanceCandidateResult(
            source_database_path=source_database_path.as_posix(),
            candidate_database_path=candidate_database_path.as_posix(),
            source_unchanged=(
                source_stat_before == _file_signature(source_database_path)
            ),
            quick_check=quick_check,
            foreign_key_violations=foreign_key_violations,
            expected_duplicate_groups=expected_duplicate_groups,
            expected_duplicate_occurrences=expected_duplicate_occurrences,
            expected_duplicate_excess=expected_duplicate_excess,
            before=before,
            after=after,
        )
        if not result.valid:
            raise RuntimeError(
                "A candidata de proveniencia falhou na validacao estrutural."
            )

        temporary_path.replace(candidate_database_path)
        return result
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_provenance_report(
    result: ProvenanceCandidateResult,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f"{destination.name}.tmp")
    content = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    try:
        temporary_path.write_text(f"{content}\n", encoding="utf-8")
        temporary_path.replace(destination)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_duplicate_provenance_manifest(
    database_path: Path,
    destination: Path,
) -> int:
    analysis = analyze_provenance(database_path)
    if (
        not analysis.provenance_migration_applied
        or analysis.original_quotes_missing
        or analysis.ambiguous_duplicate_groups
    ):
        raise RuntimeError(
            "O manifesto exige proveniencia completa e sem grupos ambiguos."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f"{destination.name}.tmp")
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    columns = (
        "fonte",
        "cotacao_canonica_id",
        "cotacao_origem_id",
        "proveniencia_id",
        "coleta_id",
        "chave_coleta",
        "arquivo_raw",
        "hash_raw",
        "url_origem",
        "baixado_em",
        "processado_em",
        "chave_identidade",
        "preco_minimo",
        "preco_comum",
        "preco_maximo",
        "situacao_mercado",
        "chave_cotacao_origem",
        "fonte_complemento",
        "url_complemento",
        "data_complemento",
    )
    rows_written = 0

    try:
        with (
            sqlite3.connect(database_uri, uri=True) as connection,
            temporary_path.open("w", encoding="utf-8", newline="") as file,
        ):
            _configure_sqlite_memory(connection)
            writer = csv.writer(file)
            writer.writerow(columns)
            cursor = connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        co.id,
                        co.chave_identidade,
                        co.preco_minimo,
                        co.preco_comum,
                        co.preco_maximo,
                        co.situacao_mercado,
                        COUNT(*) OVER (
                            PARTITION BY
                                co.chave_identidade,
                                co.preco_minimo,
                                co.preco_comum,
                                co.preco_maximo,
                                co.situacao_mercado
                        ) AS group_size
                    FROM cotacoes co
                )
                SELECT
                    ce.slug,
                    cp.cotacao_id,
                    cp.cotacao_origem_id,
                    cp.id,
                    col.id,
                    col.chave_unica,
                    col.arquivo_raw,
                    col.hash_raw,
                    col.url_origem,
                    col.baixado_em,
                    col.processado_em,
                    ranked.chave_identidade,
                    ranked.preco_minimo,
                    ranked.preco_comum,
                    ranked.preco_maximo,
                    ranked.situacao_mercado,
                    cp.chave_cotacao_origem,
                    cp.fonte_complemento,
                    cp.url_complemento,
                    cp.data_complemento
                FROM ranked
                JOIN cotacao_proveniencias cp
                  ON cp.cotacao_origem_id = ranked.id
                 AND cp.origem_registro = 'migracao_historica'
                JOIN coletas col ON col.id = cp.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                WHERE ranked.group_size > 1
                ORDER BY cp.cotacao_id, cp.cotacao_origem_id
                """
            )
            while rows := cursor.fetchmany(10_000):
                writer.writerows(rows)
                rows_written += len(rows)

        if rows_written != analysis.duplicate_occurrences:
            raise RuntimeError(
                "O manifesto nao contem todas as ocorrencias duplicadas."
            )
        temporary_path.replace(destination)
        return rows_written
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def apply_historical_provenance_migration(database_path: Path) -> bool:
    """Aplica a proveniencia historica uma unica vez e valida o checkpoint."""
    registered_at = datetime.now().astimezone().isoformat(timespec="seconds")

    with sqlite3.connect(database_path) as connection:
        _configure_sqlite_memory(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        migration_row = connection.execute(
            """
            SELECT cotacao_id_maximo
            FROM schema_migrations
            WHERE chave = ?
            """,
            (HISTORICAL_PROVENANCE_MIGRATION,),
        ).fetchone()

        if migration_row is not None:
            checkpoint = int(migration_row[0])
            _validate_historical_provenance_checkpoint(connection, checkpoint)
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
            return False

        historical_rows = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacao_proveniencias
            WHERE origem_registro = 'migracao_historica'
            """,
        )
        if historical_rows:
            raise RuntimeError(
                "Existem proveniencias historicas sem o marcador da migracao."
            )

        checkpoint = _scalar(
            connection,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        )
        quotes_in_checkpoint = int(
            connection.execute(
                "SELECT COUNT(*) FROM cotacoes WHERE id <= ?",
                (checkpoint,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            WITH ranked AS (
                SELECT
                    co.*,
                    MIN(co.id) OVER (
                        PARTITION BY
                            co.chave_identidade,
                            co.preco_minimo,
                            co.preco_comum,
                            co.preco_maximo,
                            co.situacao_mercado
                    ) AS canonical_id
                FROM cotacoes co
            )
            INSERT INTO cotacao_proveniencias (
                chave_unica,
                cotacao_id,
                coleta_id,
                cotacao_origem_id,
                chave_cotacao_origem,
                ordem_ocorrencia,
                fonte_complemento,
                url_complemento,
                data_complemento,
                registrada_em,
                origem_registro
            )
            SELECT
                'migracao-historica:' || ranked.id,
                ranked.canonical_id,
                ranked.coleta_id,
                ranked.id,
                ranked.chave_unica,
                1,
                ranked.fonte_complemento,
                ranked.url_complemento,
                ranked.data_complemento,
                ?,
                'migracao_historica'
            FROM ranked
            WHERE ranked.id <= ?
            ON CONFLICT (chave_unica) DO NOTHING
            """,
            (registered_at, checkpoint),
        )
        _validate_historical_provenance_checkpoint(connection, checkpoint)
        details = json.dumps(
            {
                "cotacoes_abrangidas": quotes_in_checkpoint,
                "cotacao_id_maximo": checkpoint,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (
                chave,
                aplicada_em,
                cotacao_id_maximo,
                detalhes
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                HISTORICAL_PROVENANCE_MIGRATION,
                registered_at,
                checkpoint,
                details,
            ),
        )
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    return True


def _validate_historical_provenance_checkpoint(
    connection: sqlite3.Connection,
    checkpoint: int,
) -> None:
    current_max_id = _scalar(
        connection,
        "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
    )
    if checkpoint < 0 or checkpoint > current_max_id:
        raise RuntimeError("Checkpoint invalido para a migracao de proveniencia.")

    marker = connection.execute(
        """
        SELECT detalhes
        FROM schema_migrations
        WHERE chave = ?
        """,
        (HISTORICAL_PROVENANCE_MIGRATION,),
    ).fetchone()
    expected_rows = int(
        connection.execute(
            "SELECT COUNT(*) FROM cotacoes WHERE id <= ?",
            (checkpoint,),
        ).fetchone()[0]
    )
    if marker is not None:
        try:
            details = json.loads(str(marker[0]))
            if not isinstance(details, dict):
                raise TypeError
            expected_rows = int(
                details.get("cotacoes_abrangidas", expected_rows)
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeError(
                "Detalhes invalidos na migracao de proveniencia."
            ) from error

    totals = connection.execute(
        """
        SELECT
            COUNT(*),
            COUNT(DISTINCT cotacao_origem_id),
            COALESCE(MAX(cotacao_origem_id), 0)
        FROM cotacao_proveniencias
        WHERE origem_registro = 'migracao_historica'
        """
    ).fetchone()
    invalid_rows = int(
        connection.execute(
            """
            WITH ranked AS (
                SELECT
                    co.id,
                    co.coleta_id,
                    MIN(co.id) OVER (
                        PARTITION BY
                            co.chave_identidade,
                            co.preco_minimo,
                            co.preco_comum,
                            co.preco_maximo,
                            co.situacao_mercado
                    ) AS canonical_id
                FROM cotacoes co
            )
            SELECT COUNT(*)
            FROM cotacao_proveniencias cp
            LEFT JOIN ranked original
              ON original.id = cp.cotacao_origem_id
            LEFT JOIN cotacoes canonical
              ON canonical.id = cp.cotacao_id
            LEFT JOIN coletas collection
              ON collection.id = cp.coleta_id
            WHERE cp.origem_registro = 'migracao_historica'
              AND (
                    cp.cotacao_origem_id IS NULL
                    OR cp.cotacao_origem_id < 1
                    OR cp.cotacao_origem_id > ?
                    OR cp.chave_unica IS NOT
                       'migracao-historica:' || cp.cotacao_origem_id
                    OR canonical.id IS NULL
                    OR collection.id IS NULL
                    OR (
                        original.id IS NOT NULL
                        AND (
                            cp.coleta_id IS NOT original.coleta_id
                            OR cp.cotacao_id IS NOT original.canonical_id
                        )
                    )
              )
            """,
            (checkpoint,),
        ).fetchone()[0]
    )

    if (
        int(totals[0]) != expected_rows
        or int(totals[1]) != expected_rows
        or int(totals[2]) != checkpoint
        or invalid_rows
    ):
        raise RuntimeError(
            "A migracao de proveniencia esta parcial ou inconsistente."
        )


def _duplicate_totals(connection: sqlite3.Connection) -> tuple[int, int, int]:
    row = connection.execute(
        """
        WITH groups AS (
            SELECT COUNT(*) AS occurrences
            FROM cotacoes
            GROUP BY
                chave_identidade,
                preco_minimo,
                preco_comum,
                preco_maximo,
                situacao_mercado
            HAVING COUNT(*) > 1
        )
        SELECT
            COUNT(*),
            COALESCE(SUM(occurrences), 0),
            COALESCE(SUM(occurrences - 1), 0)
        FROM groups
        """
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def _configure_sqlite_memory(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA temp_store = FILE")
    connection.execute("PRAGMA cache_size = -65536")


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _backup_database(source_path: Path, destination_path: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True) as source,
        sqlite3.connect(destination_path) as destination,
    ):
        source.backup(destination)


def _inspect_candidate(database_path: Path) -> tuple[tuple[str, ...], int]:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        quick_check = tuple(
            str(row[0]) for row in connection.execute("PRAGMA quick_check")
        )
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    return quick_check, foreign_key_violations
