import csv
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auditar_banco import DatabaseAuditor
from cotacoes_ceasa.audit_v4 import V4DatabaseAuditor


class RawAuditHarness:
    def __init__(
        self,
        root: Path,
        model: str,
        sample_limit: int,
    ) -> None:
        self.model = model
        self.project_root = root / model
        self.data_directory = self.project_root / "data"
        self.database_path = self.data_directory / "cotacoes.sqlite"
        self.output_directory = self.project_root / "audit-output"
        self.data_directory.mkdir(parents=True)
        self.output_directory.mkdir()
        self._create_database()
        self.auditor = self._create_auditor(sample_limit)

    def add_collection(
        self,
        collection_id: int,
        file_name: str,
        expected_content: bytes,
    ) -> None:
        expected_hash = self._hash(expected_content)
        stored_path = self._stored_path(file_name)
        with sqlite3.connect(self.database_path) as connection:
            if self.model == "v4":
                connection.execute(
                    """
                    INSERT INTO coletas (
                        id,
                        fonte_id,
                        caminho_relativo_raw,
                        sha256,
                        status
                    ) VALUES (?, 1, ?, ?, 'processada')
                    """,
                    (collection_id, stored_path, expected_hash),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO coletas (
                        id,
                        ceasa_id,
                        arquivo_raw,
                        hash_raw,
                        processado_em
                    ) VALUES (?, 1, ?, ?, '2026-10-28T12:00:00')
                    """,
                    (collection_id, stored_path, expected_hash),
                )

    def write_exact(self, file_name: str, content: bytes) -> Path:
        path = self.data_directory / "raw" / "fonte" / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def write_alternative(self, file_name: str, content: bytes) -> Path:
        old_directory = self.data_directory / "raw" / "fonte" / "old"
        old_directory.mkdir(parents=True, exist_ok=True)
        path = Path(file_name)
        alternative_name = (
            file_name
            if self.model == "v4"
            else f"{path.stem}_20261028{path.suffix}"
        )
        alternative = old_directory / alternative_name
        alternative.write_bytes(content)
        return alternative

    def audit(self) -> dict[str, object]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            return self.auditor._audit_raw_files(connection)

    def report_rows(self, file_name: str) -> list[dict[str, str]]:
        with (self.output_directory / file_name).open(
            encoding="utf-8",
            newline="",
        ) as report:
            return list(csv.DictReader(report))

    def _create_database(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            if self.model == "v4":
                connection.executescript(
                    """
                    CREATE TABLE fontes (
                        id INTEGER PRIMARY KEY,
                        slug TEXT NOT NULL
                    );
                    CREATE TABLE coletas (
                        id INTEGER PRIMARY KEY,
                        fonte_id INTEGER NOT NULL,
                        caminho_relativo_raw TEXT,
                        sha256 TEXT,
                        status TEXT NOT NULL
                    );
                    INSERT INTO fontes (id, slug) VALUES (1, 'fonte');
                    """
                )
            else:
                connection.executescript(
                    """
                    CREATE TABLE ceasas (
                        id INTEGER PRIMARY KEY,
                        slug TEXT NOT NULL
                    );
                    CREATE TABLE coletas (
                        id INTEGER PRIMARY KEY,
                        ceasa_id INTEGER NOT NULL,
                        arquivo_raw TEXT,
                        hash_raw TEXT,
                        processado_em TEXT
                    );
                    CREATE TABLE cotacoes (
                        id INTEGER PRIMARY KEY,
                        coleta_id INTEGER NOT NULL
                    );
                    INSERT INTO ceasas (id, slug) VALUES (1, 'fonte');
                    """
                )

    def _create_auditor(
        self,
        sample_limit: int,
    ) -> DatabaseAuditor | V4DatabaseAuditor:
        if self.model == "v4":
            return V4DatabaseAuditor(
                database_path=self.database_path,
                output_directory=self.output_directory,
                sample_limit=sample_limit,
                gap_days=14,
                verify_raw=True,
                full_integrity_check=False,
                skip_semantic=True,
                execution_context={},
                auditor_path=Path(__file__),
            )

        auditor = DatabaseAuditor(
            database_path=self.database_path,
            output_directory=self.output_directory,
            sample_limit=sample_limit,
            gap_days=14,
            verify_raw=True,
            full_integrity_check=False,
            skip_semantic=True,
        )
        auditor.project_root = self.project_root
        return auditor

    def _stored_path(self, file_name: str) -> str:
        relative = Path("raw") / "fonte" / file_name
        if self.model == "v4":
            return relative.as_posix()
        return (Path("data") / relative).as_posix()

    def _hash(self, content: bytes) -> str:
        if self.model == "legacy" and not content.startswith(b"%PDF"):
            text = content.decode("utf-8")
            normalized = text.replace("\r\n", "\n").replace("\r", "\n")
            content = normalized.encode("utf-8")
        return hashlib.sha256(content).hexdigest()


class RawTraceabilityAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_classifies_raw_locations_by_path_and_hash_evidence(self) -> None:
        for model in ("legacy", "v4"):
            with self.subTest(model=model):
                harness = RawAuditHarness(self.root, model, sample_limit=10)

                harness.add_collection(1, "exact.html", b"exact")
                harness.write_exact("exact.html", b"exact")

                harness.add_collection(2, "mismatch.html", b"expected")
                harness.write_exact("mismatch.html", b"different")
                harness.write_alternative("mismatch.html", b"expected")

                harness.add_collection(3, "relocated.html", b"relocated")
                harness.write_alternative("relocated.html", b"relocated")

                harness.add_collection(4, "ambiguous.html", b"expected")
                harness.write_alternative("ambiguous.html", b"different")

                harness.add_collection(5, "missing.html", b"missing")

                summary = harness.audit()

                if model == "v4":
                    self.assertEqual(1, summary["exact_matches"])
                    self.assertEqual(1, summary["relocated_matches"])
                    self.assertEqual(1, summary["ambiguous_candidates"])
                    self.assertEqual(1, summary["missing_files"])
                    self.assertEqual(1, summary["hash_mismatches"])
                else:
                    self.assertEqual(1, summary["exact_match"])
                    self.assertEqual(1, summary["relocated_match"])
                    self.assertEqual(1, summary["ambiguous_candidates"])
                    self.assertEqual(1, summary["missing"])
                    self.assertEqual(1, summary["hash_mismatch"])

                expected_reports = {
                    "raws_caminho_exato.csv": "exact_match",
                    "raws_realocados.csv": "relocated_match",
                    "raws_candidatos_ambiguos.csv": "ambiguous_candidates",
                    "raws_ausentes.csv": "missing",
                    "raws_hash_divergentes.csv": "hash_mismatch",
                }
                for report_file, classification in expected_reports.items():
                    rows = harness.report_rows(report_file)
                    self.assertEqual(1, len(rows))
                    self.assertEqual(classification, rows[0]["classificacao"])

                mismatch = harness.report_rows("raws_hash_divergentes.csv")[0]
                self.assertEqual("2", mismatch["coleta_id"])
                self.assertEqual("1", mismatch["quantidade_candidatos"])

    def test_applies_sample_limit_independently_to_each_status(self) -> None:
        for model in ("legacy", "v4"):
            with self.subTest(model=model):
                harness = RawAuditHarness(self.root, model, sample_limit=1)

                harness.add_collection(1, "missing-1.html", b"missing-1")
                harness.add_collection(2, "missing-2.html", b"missing-2")

                harness.add_collection(3, "mismatch-1.html", b"expected-1")
                harness.write_exact("mismatch-1.html", b"different-1")
                harness.add_collection(4, "mismatch-2.html", b"expected-2")
                harness.write_exact("mismatch-2.html", b"different-2")

                summary = harness.audit()

                if model == "v4":
                    self.assertEqual(2, summary["missing_files"])
                    self.assertEqual(2, summary["hash_mismatches"])
                else:
                    self.assertEqual(2, summary["missing"])
                    self.assertEqual(2, summary["hash_mismatch"])

                missing_rows = harness.report_rows("raws_ausentes.csv")
                mismatch_rows = harness.report_rows(
                    "raws_hash_divergentes.csv"
                )
                self.assertEqual(1, len(missing_rows))
                self.assertEqual(1, len(mismatch_rows))
                self.assertEqual("1", missing_rows[0]["coleta_id"])
                self.assertEqual("3", mismatch_rows[0]["coleta_id"])


if __name__ == "__main__":
    unittest.main()
