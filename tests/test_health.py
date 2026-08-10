import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cotacoes_ceasa.config import SourceConfig
from cotacoes_ceasa.workflows.health import (
    BatchRunResult,
    HealthBaseline,
    SourceRunObservation,
    capture_health_baseline,
    evaluate_run_health,
    write_health_assessment,
)


ASSESSMENT_TIME = datetime(
    2026,
    8,
    7,
    12,
    tzinfo=ZoneInfo("America/Sao_Paulo"),
)


class RunHealthAssessmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "cotacoes.sqlite"
        self.sources = {
            slug: SourceConfig(
                name=f"Fonte {slug.upper()}",
                state="Estado",
                uf="UF",
                city="Cidade",
                base_url=f"https://example.com/{slug}",
                max_staleness_days=7,
            )
            for slug in ("a", "b", "c")
        }
        self._create_database()

    def test_classifies_healthy_round(self) -> None:
        assessment = self._evaluate(("completed", "completed", "completed"))

        self.assertEqual("healthy", assessment.status)
        self.assertEqual(3, assessment.download_completed)
        self.assertEqual(0, assessment.download_failed)
        self.assertEqual(0, assessment.stale_sources)
        self.assertEqual((), assessment.reasons)

    def test_classifies_partial_round(self) -> None:
        assessment = self._evaluate(("completed", "partial", "completed"))

        self.assertEqual("partial", assessment.status)
        self.assertEqual(1, assessment.download_partial)
        self.assertIn("partial_downloads", self._reason_codes(assessment))

    def test_classifies_inadequate_round(self) -> None:
        assessment = self._evaluate(("completed", "failed", "failed"))

        self.assertEqual("inadequate", assessment.status)
        self.assertEqual(2, assessment.download_failed)
        self.assertIn(
            "insufficient_completed_sources",
            self._reason_codes(assessment),
        )

    def test_observes_freshness_and_logical_delta(self) -> None:
        baseline = HealthBaseline(previous_max_id=3)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE cotacoes SET data_cotacao = ? WHERE id = 3",
                ("2026-07-30",),
            )
            connection.executemany(
                """
                INSERT INTO cotacoes (
                    id,
                    chave_identidade,
                    coleta_id,
                    data_cotacao,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (4, "key-a", 1, "2026-08-07", 1, 2, 3, "estavel"),
                    (5, "key-new", 2, "2026-08-07", 4, 5, 6, "alta"),
                ),
            )

        assessment = evaluate_run_health(
            batch_result=self._batch(("completed", "completed", "completed")),
            source_configs=self.sources,
            database_path=self.database_path,
            baseline=baseline,
            assessed_at=ASSESSMENT_TIME,
        )

        self.assertEqual("partial", assessment.status)
        self.assertEqual(1, assessment.stale_sources)
        self.assertEqual(2, assessment.logical_delta.observations_inserted)
        self.assertEqual(1, assessment.logical_delta.logical_new)
        self.assertEqual(1, assessment.logical_delta.repeated_observations)

    def test_writes_machine_readable_json_atomically(self) -> None:
        assessment = self._evaluate(
            ("completed", "completed", "completed"),
            collection_mode="current",
        )
        destination = Path(self.temporary_directory.name) / "reports" / "health.json"
        destination.parent.mkdir()
        destination.write_text('{"status": "old"}\n', encoding="utf-8")

        write_health_assessment(assessment, destination)

        payload = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(2, payload["schema_version"])
        self.assertEqual("observation", payload["mode"])
        self.assertEqual("current", payload["collection_mode"])
        self.assertFalse(payload["blocking"])
        self.assertEqual("healthy", payload["status"])
        self.assertIn("baseline", payload)
        self.assertFalse(destination.with_suffix(".json.tmp").exists())

    def test_captures_structural_baseline_by_source(self) -> None:
        baseline = capture_health_baseline(
            self.database_path,
            tuple(self.sources),
        )

        self.assertEqual(3, baseline.previous_max_id)
        self.assertEqual(3, baseline.previous_total_quotes)
        self.assertEqual(
            {
                "a": "2026-08-07",
                "b": "2026-08-07",
                "c": "2026-08-07",
            },
            {
                source_slug: latest_date.isoformat() if latest_date else None
                for source_slug, latest_date in baseline.previous_latest_dates
            },
        )

    def _evaluate(
        self,
        statuses: tuple[str, str, str],
        collection_mode: str = "legacy",
    ):
        return evaluate_run_health(
            batch_result=self._batch(statuses),
            source_configs=self.sources,
            database_path=self.database_path,
            baseline=HealthBaseline(previous_max_id=3),
            assessed_at=ASSESSMENT_TIME,
            collection_mode=collection_mode,
        )

    def _batch(self, statuses: tuple[str, str, str]) -> BatchRunResult:
        return BatchRunResult(
            sources=tuple(
                SourceRunObservation(
                    source_slug=slug,
                    download_status=status,
                    persistence_status=(
                        "skipped" if status == "failed" else "completed"
                    ),
                    raw_files=0 if status == "failed" else 1,
                )
                for slug, status in zip(self.sources, statuses, strict=True)
            )
        )

    def _create_database(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE ceasas (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE
                );
                CREATE TABLE coletas (
                    id INTEGER PRIMARY KEY,
                    ceasa_id INTEGER NOT NULL
                );
                CREATE TABLE cotacoes (
                    id INTEGER PRIMARY KEY,
                    chave_identidade TEXT NOT NULL,
                    coleta_id INTEGER NOT NULL,
                    data_cotacao TEXT NOT NULL,
                    preco_minimo NUMERIC,
                    preco_comum NUMERIC,
                    preco_maximo NUMERIC,
                    situacao_mercado TEXT
                );
                CREATE INDEX idx_cotacoes_identidade
                    ON cotacoes (chave_identidade);
                """
            )
            connection.executemany(
                "INSERT INTO ceasas (id, slug) VALUES (?, ?)",
                ((1, "a"), (2, "b"), (3, "c")),
            )
            connection.executemany(
                "INSERT INTO coletas (id, ceasa_id) VALUES (?, ?)",
                ((1, 1), (2, 2), (3, 3)),
            )
            connection.executemany(
                """
                INSERT INTO cotacoes (
                    id,
                    chave_identidade,
                    coleta_id,
                    data_cotacao,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (1, "key-a", 1, "2026-08-07", 1, 2, 3, "estavel"),
                    (2, "key-b", 2, "2026-08-07", 2, 3, 4, "estavel"),
                    (3, "key-c", 3, "2026-08-07", 3, 4, 5, "estavel"),
                ),
            )

    @staticmethod
    def _reason_codes(assessment) -> set[str]:
        return {reason.code for reason in assessment.reasons}


if __name__ == "__main__":
    unittest.main()
