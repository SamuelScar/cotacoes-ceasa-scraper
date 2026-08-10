import json
import sqlite3
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path

from cotacoes_ceasa.storage.sqlite import SQLITE_SCHEMA_VERSION


DUPLICATE_REPORT_SCHEMA_VERSION = 1


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
    observations: int
    logical_contents: int
    repeated_observations: int
    source_date_buckets: int
    source_date_coverage_hash: str
    sources: tuple[SourceDuplicateSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DUPLICATE_REPORT_SCHEMA_VERSION,
            "database_path": self.database_path,
            "observations": self.observations,
            "logical_contents": self.logical_contents,
            "repeated_observations": self.repeated_observations,
            "source_date_buckets": self.source_date_buckets,
            "source_date_coverage_hash": self.source_date_coverage_hash,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class CandidateBaselineResult:
    source_database_path: str
    candidate_database_path: str
    removed_observations: int
    vacuumed: bool
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    before: DuplicateAnalysis
    after: DuplicateAnalysis

    @property
    def valid(self) -> bool:
        return (
            self.quick_check == ("ok",)
            and self.foreign_key_violations == 0
            and self.after.repeated_observations == 0
            and self.after.observations == self.before.logical_contents
            and self.removed_observations == self.before.repeated_observations
            and self.after.source_date_buckets == self.before.source_date_buckets
            and self.after.source_date_coverage_hash
            == self.before.source_date_coverage_hash
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DUPLICATE_REPORT_SCHEMA_VERSION,
            "status": "valid" if self.valid else "invalid",
            "source_database_path": self.source_database_path,
            "candidate_database_path": self.candidate_database_path,
            "removed_observations": self.removed_observations,
            "vacuumed": self.vacuumed,
            "quick_check": list(self.quick_check),
            "foreign_key_violations": self.foreign_key_violations,
            "requires_full_supabase_replace": True,
            "requires_manual_publication_transition": (
                self.removed_observations > 0
            ),
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
                SUM(logical_groups.observations) - COUNT(*)
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
        observations=sum(source.observations for source in sources),
        logical_contents=sum(source.logical_contents for source in sources),
        repeated_observations=sum(
            source.repeated_observations for source in sources
        ),
        source_date_buckets=len(rows),
        source_date_coverage_hash=sha256(coverage_content).hexdigest(),
        sources=sources,
    )


def create_candidate_baseline(
    source_database_path: Path,
    candidate_database_path: Path,
    vacuum: bool = False,
) -> CandidateBaselineResult:
    if not source_database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {source_database_path}")

    if source_database_path.resolve() == candidate_database_path.resolve():
        raise ValueError("A baseline candidata deve usar outro arquivo SQLite.")

    if candidate_database_path.exists():
        raise FileExistsError(
            f"A baseline candidata ja existe: {candidate_database_path}"
        )

    candidate_database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = candidate_database_path.with_name(
        f"{candidate_database_path.name}.tmp"
    )
    if temporary_path.exists():
        raise FileExistsError(f"Arquivo temporario ja existe: {temporary_path}")

    try:
        _backup_database(source_database_path, temporary_path)
        before = replace(
            analyze_duplicate_content(temporary_path),
            database_path=source_database_path.as_posix(),
        )
        removed_observations = _remove_duplicate_content(temporary_path)

        if vacuum:
            with sqlite3.connect(temporary_path) as connection:
                connection.execute("VACUUM")

        after = replace(
            analyze_duplicate_content(temporary_path),
            database_path=candidate_database_path.as_posix(),
        )
        quick_check, foreign_key_violations = _inspect_candidate(temporary_path)
        result = CandidateBaselineResult(
            source_database_path=source_database_path.as_posix(),
            candidate_database_path=candidate_database_path.as_posix(),
            removed_observations=removed_observations,
            vacuumed=vacuum,
            quick_check=quick_check,
            foreign_key_violations=foreign_key_violations,
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


def _remove_duplicate_content(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
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
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version < SQLITE_SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    return removed_observations


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
