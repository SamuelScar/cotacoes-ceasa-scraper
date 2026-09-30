import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from cotacoes_ceasa.storage.sqlite import SQLITE_SCHEMA_VERSION
from cotacoes_ceasa.workflows.provenance import (
    analyze_provenance,
    validate_historical_provenance,
)


DUPLICATE_REPORT_SCHEMA_VERSION = 3
DEDUPLICATED_BASELINE_MIGRATION = "issue6_baseline_deduplicada_v1"


@dataclass(frozen=True)
class SourceDuplicateSummary:
    source_slug: str
    observations: int
    logical_contents: int
    repeated_observations: int
    oldest_quote_date: str | None
    latest_quote_date: str | None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "source_slug": self.source_slug,
            "observations": self.observations,
            "logical_contents": self.logical_contents,
            "repeated_observations": self.repeated_observations,
            "oldest_quote_date": self.oldest_quote_date,
            "latest_quote_date": self.latest_quote_date,
        }


@dataclass
class _SourceTotals:
    observations: int = 0
    logical_contents: int = 0
    repeated_observations: int = 0
    oldest_quote_date: str | None = None
    latest_quote_date: str | None = None


@dataclass(frozen=True)
class DuplicateAnalysis:
    database_path: str
    max_quote_id: int
    observations: int
    logical_contents: int
    repeated_observations: int
    duplicate_groups: int
    duplicate_occurrences: int
    source_date_buckets: int
    source_date_coverage_hash: str
    sources: tuple[SourceDuplicateSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DUPLICATE_REPORT_SCHEMA_VERSION,
            "database_path": self.database_path,
            "max_quote_id": self.max_quote_id,
            "observations": self.observations,
            "logical_contents": self.logical_contents,
            "repeated_observations": self.repeated_observations,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_occurrences": self.duplicate_occurrences,
            "source_date_buckets": self.source_date_buckets,
            "source_date_coverage_hash": self.source_date_coverage_hash,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class BaselineProvenanceSummary:
    rows: int
    max_id: int
    historical_rows: int
    distinct_origin_references: int
    canonical_targets: int
    collections_preserved: int
    current_quotes_covered: int
    current_quotes_missing: int
    orphan_canonical_targets: int
    orphan_collections: int
    content_sha256: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "rows": self.rows,
            "max_id": self.max_id,
            "historical_rows": self.historical_rows,
            "distinct_origin_references": self.distinct_origin_references,
            "canonical_targets": self.canonical_targets,
            "collections_preserved": self.collections_preserved,
            "current_quotes_covered": self.current_quotes_covered,
            "current_quotes_missing": self.current_quotes_missing,
            "orphan_canonical_targets": self.orphan_canonical_targets,
            "orphan_collections": self.orphan_collections,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class CandidateBaselineResult:
    source_database_path: str
    candidate_database_path: str
    source_unchanged: bool
    removed_observations: int
    vacuumed: bool
    migration_applied: bool
    migration_checkpoint: int | None
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    provenance_before: BaselineProvenanceSummary
    provenance_after: BaselineProvenanceSummary
    before: DuplicateAnalysis
    after: DuplicateAnalysis

    @property
    def provenance_rows_before(self) -> int:
        return self.provenance_before.rows

    @property
    def provenance_rows_after(self) -> int:
        return self.provenance_after.rows

    @property
    def valid(self) -> bool:
        return (
            self.source_unchanged
            and self.quick_check == ("ok",)
            and self.foreign_key_violations == 0
            and self.migration_applied
            and self.migration_checkpoint == self.before.max_quote_id
            and self.after.max_quote_id <= self.before.max_quote_id
            and self.after.repeated_observations == 0
            and self.after.duplicate_groups == 0
            and self.after.duplicate_occurrences == 0
            and self.after.observations == self.before.logical_contents
            and self.before.observations - self.after.observations
            == self.removed_observations
            and self.removed_observations == self.before.repeated_observations
            and self.after.source_date_buckets == self.before.source_date_buckets
            and self.after.source_date_coverage_hash
            == self.before.source_date_coverage_hash
            and self.provenance_after.rows == self.provenance_before.rows
            and self.provenance_after.max_id == self.provenance_before.max_id
            and self.provenance_after.historical_rows
            == self.provenance_before.historical_rows
            and self.provenance_after.distinct_origin_references
            == self.provenance_before.distinct_origin_references
            and self.provenance_after.distinct_origin_references
            == self.before.observations
            and self.provenance_after.canonical_targets
            == self.provenance_before.canonical_targets
            and self.provenance_after.canonical_targets
            == self.after.observations
            and self.provenance_after.collections_preserved
            == self.provenance_before.collections_preserved
            and self.provenance_after.current_quotes_covered
            == self.after.observations
            and self.provenance_after.current_quotes_missing == 0
            and self.provenance_after.orphan_canonical_targets == 0
            and self.provenance_after.orphan_collections == 0
            and self.provenance_after.content_sha256
            == self.provenance_before.content_sha256
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DUPLICATE_REPORT_SCHEMA_VERSION,
            "status": "valid" if self.valid else "invalid",
            "source_database_path": self.source_database_path,
            "candidate_database_path": self.candidate_database_path,
            "source_unchanged": self.source_unchanged,
            "removed_observations": self.removed_observations,
            "vacuumed": self.vacuumed,
            "migration": {
                "key": DEDUPLICATED_BASELINE_MIGRATION,
                "applied": self.migration_applied,
                "checkpoint": self.migration_checkpoint,
            },
            "quick_check": list(self.quick_check),
            "foreign_key_violations": self.foreign_key_violations,
            "provenance_rows_before": self.provenance_rows_before,
            "provenance_rows_after": self.provenance_rows_after,
            "provenance_before": self.provenance_before.to_dict(),
            "provenance_after": self.provenance_after.to_dict(),
            "requires_full_supabase_replace": True,
            "requires_manual_publication_transition": (
                self.removed_observations > 0
            ),
            "safety": {
                "source_opened_read_only": True,
                "source_unchanged": self.source_unchanged,
                "rollback": "descartar o SQLite candidato da baseline",
            },
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


def analyze_duplicate_content(database_path: Path) -> DuplicateAnalysis:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"

    with sqlite3.connect(database_uri, uri=True) as connection:
        rows = connection.execute(
            """
            WITH logical_groups AS (
                SELECT
                    col.ceasa_id,
                    co.chave_identidade,
                    co.preco_minimo,
                    co.preco_comum,
                    co.preco_maximo,
                    co.situacao_mercado,
                    co.data_cotacao,
                    COUNT(*) AS observations
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                GROUP BY
                    col.ceasa_id,
                    co.chave_identidade,
                    co.preco_minimo,
                    co.preco_comum,
                    co.preco_maximo,
                    co.situacao_mercado,
                    co.data_cotacao
            )
            SELECT
                ce.slug,
                logical_groups.data_cotacao,
                SUM(logical_groups.observations),
                COUNT(*),
                SUM(logical_groups.observations) - COUNT(*),
                SUM(logical_groups.observations > 1),
                SUM(CASE
                    WHEN logical_groups.observations > 1
                    THEN logical_groups.observations ELSE 0 END)
            FROM logical_groups
            JOIN ceasas ce ON ce.id = logical_groups.ceasa_id
            GROUP BY ce.slug, logical_groups.data_cotacao
            ORDER BY ce.slug, logical_groups.data_cotacao
            """
        ).fetchall()

    source_totals: dict[str, _SourceTotals] = {}
    coverage_lines: list[str] = []

    for row in rows:
        source_slug = str(row[0])
        quote_date = str(row[1])
        observations = int(row[2])
        logical_contents = int(row[3])
        repeated_observations = int(row[4])
        totals = source_totals.setdefault(source_slug, _SourceTotals())
        totals.observations += observations
        totals.logical_contents += logical_contents
        totals.repeated_observations += repeated_observations
        totals.oldest_quote_date = totals.oldest_quote_date or quote_date
        totals.latest_quote_date = quote_date
        coverage_lines.append(f"{source_slug}|{quote_date}|{logical_contents}")

    sources = tuple(
        SourceDuplicateSummary(
            source_slug=source_slug,
            observations=totals.observations,
            logical_contents=totals.logical_contents,
            repeated_observations=totals.repeated_observations,
            oldest_quote_date=totals.oldest_quote_date,
            latest_quote_date=totals.latest_quote_date,
        )
        for source_slug, totals in source_totals.items()
    )
    coverage_content = "\n".join(coverage_lines).encode("utf-8")

    return DuplicateAnalysis(
        database_path=database_path.as_posix(),
        max_quote_id=_read_scalar_from_database(
            database_path,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        ),
        observations=sum(source.observations for source in sources),
        logical_contents=sum(source.logical_contents for source in sources),
        repeated_observations=sum(
            source.repeated_observations for source in sources
        ),
        duplicate_groups=sum(int(row[5]) for row in rows),
        duplicate_occurrences=sum(int(row[6]) for row in rows),
        source_date_buckets=len(rows),
        source_date_coverage_hash=sha256(coverage_content).hexdigest(),
        sources=sources,
    )


def create_candidate_baseline(
    source_database_path: Path,
    candidate_database_path: Path,
    vacuum: bool = False,
) -> CandidateBaselineResult:
    source_database_path = source_database_path.resolve()
    candidate_database_path = candidate_database_path.resolve()

    if not source_database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {source_database_path}")
    if source_database_path == candidate_database_path:
        raise ValueError("A baseline candidata deve usar outro arquivo SQLite.")
    if candidate_database_path.exists():
        raise FileExistsError(
            f"A baseline candidata ja existe: {candidate_database_path}"
        )
    if _load_baseline_marker(source_database_path) is not None:
        raise RuntimeError("O banco de origem ja possui uma baseline consolidada.")

    source_signature_before = _file_signature(source_database_path)
    validate_historical_provenance(source_database_path)
    provenance_analysis = analyze_provenance(source_database_path)
    before = analyze_duplicate_content(source_database_path)
    provenance_before = analyze_baseline_provenance(source_database_path)
    if (
        provenance_analysis.schema_version != SQLITE_SCHEMA_VERSION
        or provenance_analysis.original_quotes_missing > 0
        or not provenance_analysis.provenance_migration_applied
        or provenance_analysis.duplicate_provenances
        != provenance_analysis.duplicate_occurrences
        or provenance_analysis.ambiguous_duplicate_groups > 0
        or before.observations != provenance_analysis.quotes
        or before.duplicate_groups != provenance_analysis.duplicate_groups
        or before.duplicate_occurrences
        != provenance_analysis.duplicate_occurrences
        or before.repeated_observations
        != provenance_analysis.duplicate_excess
        or provenance_before.distinct_origin_references
        != before.observations
        or provenance_before.canonical_targets != before.logical_contents
        or provenance_before.current_quotes_missing != 0
        or provenance_before.orphan_canonical_targets != 0
        or provenance_before.orphan_collections != 0
    ):
        raise RuntimeError(
            "A baseline exige proveniencia completa, consistente e sem "
            "grupos ambiguos antes de remover repeticoes."
        )

    candidate_database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = candidate_database_path.with_name(
        f"{candidate_database_path.name}.tmp"
    )
    if temporary_path.exists():
        raise FileExistsError(f"Arquivo temporario ja existe: {temporary_path}")

    try:
        _backup_database(source_database_path, temporary_path)
        removed_observations = _apply_baseline_migration(
            temporary_path,
            before,
            provenance_before,
            vacuum,
        )

        if vacuum:
            database_uri = f"{temporary_path.resolve().as_uri()}?mode=rw"
            with sqlite3.connect(database_uri, uri=True) as connection:
                connection.execute("VACUUM")

        after = replace(
            analyze_duplicate_content(temporary_path),
            database_path=candidate_database_path.as_posix(),
        )
        provenance_after = analyze_baseline_provenance(temporary_path)
        migration_checkpoint = _validate_baseline_marker(
            temporary_path,
            before,
            after,
            provenance_before,
            provenance_after,
            removed_observations,
            vacuum,
        )
        quick_check, foreign_key_violations = _inspect_candidate(temporary_path)
        result = CandidateBaselineResult(
            source_database_path=source_database_path.as_posix(),
            candidate_database_path=candidate_database_path.as_posix(),
            source_unchanged=(
                source_signature_before == _file_signature(source_database_path)
            ),
            removed_observations=removed_observations,
            vacuumed=vacuum,
            migration_applied=True,
            migration_checkpoint=migration_checkpoint,
            quick_check=quick_check,
            foreign_key_violations=foreign_key_violations,
            provenance_before=provenance_before,
            provenance_after=provenance_after,
            before=before,
            after=after,
        )

        if not result.valid:
            raise RuntimeError("A baseline candidata falhou na validacao estrutural.")

        temporary_path.replace(candidate_database_path)
        return result
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_duplicate_report(
    payload: DuplicateAnalysis | CandidateBaselineResult,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f"{destination.name}.tmp")
    content = json.dumps(payload.to_dict(), ensure_ascii=False, indent=2)

    try:
        temporary_path.write_text(f"{content}\n", encoding="utf-8")
        temporary_path.replace(destination)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _backup_database(source_path: Path, destination_path: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"

    with (
        sqlite3.connect(source_uri, uri=True) as source,
        sqlite3.connect(destination_path) as destination,
    ):
        source.backup(destination)


def analyze_baseline_provenance(
    database_path: Path,
) -> BaselineProvenanceSummary:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        totals = connection.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(MAX(id), 0),
                COALESCE(SUM(origem_registro = 'migracao_historica'), 0),
                COUNT(DISTINCT cotacao_origem_id),
                COUNT(DISTINCT cotacao_id),
                COUNT(DISTINCT coleta_id)
            FROM cotacao_proveniencias
            """
        ).fetchone()
        quotes = _scalar(connection, "SELECT COUNT(*) FROM cotacoes")
        current_quotes_covered = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacoes co
            WHERE EXISTS (
                SELECT 1
                FROM cotacao_proveniencias cp
                WHERE cp.cotacao_origem_id = co.id
            )
            """,
        )
        orphan_canonical_targets = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacao_proveniencias cp
            LEFT JOIN cotacoes co ON co.id = cp.cotacao_id
            WHERE co.id IS NULL
            """,
        )
        orphan_collections = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacao_proveniencias cp
            LEFT JOIN coletas col ON col.id = cp.coleta_id
            WHERE col.id IS NULL
            """,
        )
        content_sha256 = _provenance_content_sha256(connection)

    return BaselineProvenanceSummary(
        rows=int(totals[0]),
        max_id=int(totals[1]),
        historical_rows=int(totals[2]),
        distinct_origin_references=int(totals[3]),
        canonical_targets=int(totals[4]),
        collections_preserved=int(totals[5]),
        current_quotes_covered=current_quotes_covered,
        current_quotes_missing=quotes - current_quotes_covered,
        orphan_canonical_targets=orphan_canonical_targets,
        orphan_collections=orphan_collections,
        content_sha256=content_sha256,
    )


def _apply_baseline_migration(
    database_path: Path,
    before: DuplicateAnalysis,
    provenance_before: BaselineProvenanceSummary,
    vacuum_requested: bool,
) -> int:
    database_uri = f"{database_path.resolve().as_uri()}?mode=rw"
    registered_at = datetime.now().astimezone().isoformat(timespec="seconds")

    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA temp_store = FILE")
        connection.execute("PRAGMA cache_size = -65536")
        if connection.execute(
            """
            SELECT 1
            FROM schema_migrations
            WHERE chave = ?
            """,
            (DEDUPLICATED_BASELINE_MIGRATION,),
        ).fetchone():
            raise RuntimeError("A migracao da baseline ja esta registrada.")

        actual_quotes = _scalar(connection, "SELECT COUNT(*) FROM cotacoes")
        actual_max_quote_id = _scalar(
            connection,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        )
        actual_provenance_rows = _scalar(
            connection,
            "SELECT COUNT(*) FROM cotacao_proveniencias",
        )
        if (
            actual_quotes != before.observations
            or actual_max_quote_id != before.max_quote_id
            or actual_provenance_rows != provenance_before.rows
        ):
            raise RuntimeError(
                "A copia da baseline divergiu da origem antes da consolidacao."
            )

        changes_before = connection.total_changes
        connection.execute(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            chave_identidade,
                            preco_minimo,
                            preco_comum,
                            preco_maximo,
                            situacao_mercado
                        ORDER BY id
                    ) AS content_position
                FROM cotacoes
            )
            DELETE FROM cotacoes
            WHERE id IN (
                SELECT id
                FROM ranked
                WHERE content_position > 1
            )
            """
        )
        removed_observations = connection.total_changes - changes_before
        result_quotes = _scalar(connection, "SELECT COUNT(*) FROM cotacoes")
        result_max_quote_id = _scalar(
            connection,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        )
        result_duplicate_groups = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM cotacoes
                GROUP BY
                    chave_identidade,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado
                HAVING COUNT(*) > 1
            )
            """,
        )
        result_provenance_rows = _scalar(
            connection,
            "SELECT COUNT(*) FROM cotacao_proveniencias",
        )
        orphan_canonical_targets = _scalar(
            connection,
            """
            SELECT COUNT(*)
            FROM cotacao_proveniencias cp
            LEFT JOIN cotacoes co ON co.id = cp.cotacao_id
            WHERE co.id IS NULL
            """,
        )
        if (
            removed_observations != before.repeated_observations
            or result_quotes != before.logical_contents
            or result_max_quote_id > before.max_quote_id
            or result_duplicate_groups != 0
            or result_provenance_rows != provenance_before.rows
            or orphan_canonical_targets != 0
        ):
            raise RuntimeError(
                "A consolidacao da baseline divergiu dos totais planejados."
            )

        details = _baseline_marker_details(
            before,
            provenance_before,
            removed_observations,
            result_quotes,
            result_max_quote_id,
            vacuum_requested,
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
                DEDUPLICATED_BASELINE_MIGRATION,
                registered_at,
                before.max_quote_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
            ),
        )
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    return removed_observations


def _baseline_marker_details(
    before: DuplicateAnalysis,
    provenance_before: BaselineProvenanceSummary,
    removed_observations: int,
    result_quotes: int,
    result_max_quote_id: int,
    vacuum_requested: bool,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_quotes": before.observations,
        "source_max_quote_id": before.max_quote_id,
        "source_logical_contents": before.logical_contents,
        "source_duplicate_excess": before.repeated_observations,
        "source_duplicate_groups": before.duplicate_groups,
        "source_duplicate_occurrences": before.duplicate_occurrences,
        "source_date_buckets": before.source_date_buckets,
        "source_date_coverage_hash": before.source_date_coverage_hash,
        "removed_quotes": removed_observations,
        "result_quotes": result_quotes,
        "result_max_quote_id": result_max_quote_id,
        "provenance_rows": provenance_before.rows,
        "provenance_id_maximo": provenance_before.max_id,
        "historical_provenance_rows": provenance_before.historical_rows,
        "distinct_origin_references": (
            provenance_before.distinct_origin_references
        ),
        "canonical_targets": provenance_before.canonical_targets,
        "collections_preserved": provenance_before.collections_preserved,
        "provenance_content_sha256": provenance_before.content_sha256,
        "vacuum_requested": vacuum_requested,
    }


def _validate_baseline_marker(
    database_path: Path,
    before: DuplicateAnalysis,
    after: DuplicateAnalysis,
    provenance_before: BaselineProvenanceSummary,
    provenance_after: BaselineProvenanceSummary,
    removed_observations: int,
    vacuum_requested: bool,
) -> int:
    marker = _load_baseline_marker(database_path)
    if marker is None:
        raise RuntimeError("O marcador da baseline nao foi registrado.")
    checkpoint, details = marker
    expected_details = _baseline_marker_details(
        before,
        provenance_before,
        removed_observations,
        after.observations,
        after.max_quote_id,
        vacuum_requested,
    )
    if (
        checkpoint != before.max_quote_id
        or details != expected_details
        or provenance_after.content_sha256
        != provenance_before.content_sha256
    ):
        raise RuntimeError("O marcador da baseline divergiu da consolidacao.")
    return checkpoint


def _load_baseline_marker(
    database_path: Path,
) -> tuple[int, dict[str, object]] | None:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        row = connection.execute(
            """
            SELECT cotacao_id_maximo, detalhes
            FROM schema_migrations
            WHERE chave = ?
            """,
            (DEDUPLICATED_BASELINE_MIGRATION,),
        ).fetchone()
    if row is None:
        return None
    try:
        details = json.loads(str(row[1]))
    except json.JSONDecodeError as error:
        raise RuntimeError("Detalhes invalidos no marcador da baseline.") from error
    if not isinstance(details, dict):
        raise RuntimeError("Detalhes invalidos no marcador da baseline.")
    return int(row[0]), details


def _provenance_content_sha256(connection: sqlite3.Connection) -> str:
    digest = sha256()
    cursor = connection.execute(
        """
        SELECT
            id,
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
        FROM cotacao_proveniencias
        ORDER BY id
        """
    )
    while rows := cursor.fetchmany(10_000):
        for row in rows:
            serialized = json.dumps(
                list(row),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            digest.update(serialized.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _read_scalar_from_database(database_path: Path, query: str) -> int:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        return _scalar(connection, query)


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


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
