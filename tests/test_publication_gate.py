import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cotacoes_ceasa.config import SourceConfig
from cotacoes_ceasa.workflows.publication_gate import (
    evaluate_publication_gate,
    write_publication_gate_result,
)


class PublicationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_path = Path(self.temporary_directory.name)
        self.database_path = temporary_path / "cotacoes.sqlite"
        self.health_report_path = temporary_path / "saude.json"
        self.sources = {
            "required": self._source_config("required"),
            "optional": self._source_config("optional"),
        }
        self._create_database()

    def test_approves_valid_database_with_optional_source_failure(self) -> None:
        payload = self._health_payload()
        payload["sources"][1]["download_status"] = "failed"
        payload["sources"][1]["persistence_status"] = "skipped"
        self._write_health(payload)

        result = self._evaluate()

        self.assertEqual("approved", result.status)
        self.assertEqual(0, result.blocking_reasons)
        self.assertEqual(2, result.warnings)
        self.assertEqual(("ok",), result.quick_check)
        self.assertEqual(0, result.foreign_key_violations)

    def test_rejects_required_source_failure(self) -> None:
        payload = self._health_payload()
        payload["sources"][0]["download_status"] = "failed"
        payload["sources"][0]["persistence_status"] = "skipped"
        self._write_health(payload)

        result = self._evaluate()

        self.assertEqual("rejected", result.status)
        self.assertIn("required_download_failed", self._reason_codes(result))
        self.assertIn("required_persistence_failed", self._reason_codes(result))

    def test_rejects_total_count_regression(self) -> None:
        payload = self._health_payload()
        payload["baseline"]["previous_total_quotes"] = 3
        self._write_health(payload)

        result = self._evaluate()

        self.assertIn("database_total_regression", self._reason_codes(result))

    def test_rejects_latest_date_regression(self) -> None:
        payload = self._health_payload()
        payload["baseline"]["previous_latest_quote_dates"][
            "required"
        ] = "2026-08-08"
        self._write_health(payload)

        result = self._evaluate()

        self.assertIn("source_latest_date_regression", self._reason_codes(result))

    def test_rejects_foreign_key_violation(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE coletas SET ceasa_id = 999 WHERE id = 1"
            )
        self._write_health(self._health_payload())

        result = self._evaluate()

        self.assertIn(
            "database_foreign_key_violations",
            self._reason_codes(result),
        )

    def test_rejects_health_report_without_structural_baseline(self) -> None:
        payload = self._health_payload()
        payload["schema_version"] = 1
        payload.pop("baseline")
        self._write_health(payload)

        result = self._evaluate()

        self.assertIn("health_schema_unsupported", self._reason_codes(result))
        self.assertIn("baseline_missing", self._reason_codes(result))

    def test_backfill_does_not_block_required_source_outside_scope(self) -> None:
        payload = self._health_payload()
        payload["collection_mode"] = "backfill"
        payload["sources"] = payload["sources"][:1]
        self.sources["outside"] = self._source_config("required")
        self._write_health(payload)

        result = self._evaluate()

        self.assertEqual("approved", result.status)
        outside = next(
            source for source in result.sources if source.source_slug == "outside"
        )
        self.assertEqual("out_of_scope", outside.status)

    def test_writes_structured_result_atomically(self) -> None:
        self._write_health(self._health_payload())
        result = self._evaluate()
        destination = Path(self.temporary_directory.name) / "gate.json"

        write_publication_gate_result(result, destination)

        payload = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual("approved", payload["status"])
        self.assertFalse(payload["blocking"])
        self.assertEqual(1, payload["schema_version"])
        self.assertFalse(destination.with_suffix(".json.tmp").exists())

    def _evaluate(self):
        return evaluate_publication_gate(
            database_path=self.database_path,
            health_report_path=self.health_report_path,
            source_configs=self.sources,
        )

    def _health_payload(self) -> dict:
        return {
            "schema_version": 2,
            "mode": "observation",
            "collection_mode": "current",
            "blocking": False,
            "status": "healthy",
            "reasons": [],
            "baseline": {
                "measurement_status": "available",
                "previous_max_id": 2,
                "previous_total_quotes": 2,
                "previous_latest_quote_dates": {
                    "required": "2026-08-07",
                    "optional": "2026-08-07",
                },
                "error": None,
            },
            "sources": [
                self._health_source("required"),
                self._health_source("optional"),
            ],
        }

    @staticmethod
    def _health_source(source_slug: str) -> dict:
        return {
            "source_slug": source_slug,
            "download_status": "completed",
            "persistence_status": "completed",
            "latest_quote_date": "2026-08-07",
            "freshness_status": "current",
        }

    def _write_health(self, payload: dict) -> None:
        self.health_report_path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    def _create_database(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE ceasas (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE
                );
                CREATE TABLE coletas (
                    id INTEGER PRIMARY KEY,
                    ceasa_id INTEGER NOT NULL,
                    FOREIGN KEY (ceasa_id) REFERENCES ceasas (id)
                );
                CREATE TABLE cotacoes (
                    id INTEGER PRIMARY KEY,
                    coleta_id INTEGER NOT NULL,
                    data_cotacao TEXT NOT NULL,
                    FOREIGN KEY (coleta_id) REFERENCES coletas (id)
                );
                """
            )
            connection.executemany(
                "INSERT INTO ceasas (id, slug) VALUES (?, ?)",
                ((1, "required"), (2, "optional")),
            )
            connection.executemany(
                "INSERT INTO coletas (id, ceasa_id) VALUES (?, ?)",
                ((1, 1), (2, 2)),
            )
            connection.executemany(
                "INSERT INTO cotacoes (id, coleta_id, data_cotacao) VALUES (?, ?, ?)",
                ((1, 1, "2026-08-07"), (2, 2, "2026-08-07")),
            )

    @staticmethod
    def _source_config(policy: str) -> SourceConfig:
        return SourceConfig(
            name="Fonte",
            state="Estado",
            uf="UF",
            city="Cidade",
            base_url="https://example.com",
            publication_policy=policy,
        )

    @staticmethod
    def _reason_codes(result) -> set[str]:
        return {reason.code for reason in result.reasons}


if __name__ == "__main__":
    unittest.main()
