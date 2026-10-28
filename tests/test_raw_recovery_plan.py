import csv
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.backups.restore import collect_data_inventory
from scripts import planejar_recuperacao_raws as planner


class RawRecoveryPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.current_data = self.root / "current" / "data"
        self.current_data.mkdir(parents=True)
        self.database = self.current_data / "cotacoes.sqlite"
        self.direct_content = b"raw direto preservado"
        self.archived_content = b"raw preservado dentro do zip"
        self.direct_hash = self._sha256(self.direct_content)
        self.archived_hash = self._sha256(self.archived_content)
        self._create_v4_database(self.database)

        self.remote_directory = self.root / "remote"
        self.remote_directory.mkdir()
        self.archive_name = "ceasa-data-20261028-120000.zip"
        self.archive_path = self.remote_directory / self.archive_name
        self.manifest_name = "backup-20261028-120000.json"
        self.manifest_path = self.remote_directory / self.manifest_name
        self._create_backup_archive()
        self._write_manifest(status="completed")

    def test_builds_plan_from_validated_backup_without_changing_current_data(
        self,
    ) -> None:
        database_hash_before = calculate_sha256(self.database)
        output = self.root / "output"
        work = self.root / "work"
        rclone_operations: list[str] = []

        def download_manifests(
            remote: str,
            remote_root: PurePosixPath,
            destination: Path,
        ) -> None:
            self.assertEqual("onedrive", remote)
            self.assertEqual(PurePosixPath("cotacoes-ceasa"), remote_root)
            shutil.copy2(self.manifest_path, destination / self.manifest_name)

        def run_rclone(
            *arguments: str,
            capture_output: bool = False,
        ) -> str:
            self.assertFalse(capture_output)
            rclone_operations.append(arguments[0])
            self.assertEqual("copyto", arguments[0])
            self.assertEqual(
                "onedrive:cotacoes-ceasa/backups/staging/"
                + self.archive_name,
                arguments[1],
            )
            shutil.copy2(self.archive_path, Path(arguments[2]))
            return ""

        inventory = [
            {
                "Path": f"staging/{self.archive_name}",
                "Size": self.archive_path.stat().st_size,
            }
        ]
        with (
            patch.object(
                planner,
                "_download_manifests",
                side_effect=download_manifests,
            ),
            patch.object(
                planner,
                "_list_remote_backups",
                return_value=inventory,
            ),
            patch.object(planner, "_run_rclone", side_effect=run_rclone),
        ):
            result, exit_code = planner.build_plan(
                database_path=self.database,
                output_directory=output,
                work_directory=work,
                remote="onedrive",
                remote_root="cotacoes-ceasa",
            )

        self.assertEqual(0, exit_code)
        self.assertEqual("completed", result["status"])
        self.assertEqual(database_hash_before, calculate_sha256(self.database))
        self.assertTrue(result["database"]["unchanged"])
        self.assertEqual(["copyto"], rclone_operations)

        coverage = result["recovery_coverage"]
        self.assertEqual(2, coverage["missing_records"])
        self.assertEqual(2, coverage["missing_unique_hashes"])
        self.assertEqual(2, coverage["records_found_in_backups"])
        self.assertEqual(2, coverage["unique_hashes_found_in_backups"])
        self.assertEqual(0, coverage["records_not_found"])

        with (output / "plano-recuperacao-raws.csv").open(
            encoding="utf-8",
            newline="",
        ) as plan_file:
            rows = list(csv.DictReader(plan_file))
        self.assertEqual(2, len(rows))
        by_id = {row["coleta_id"]: row for row in rows}
        self.assertEqual("encontrado", by_id["1"]["status"])
        self.assertEqual("file", by_id["1"]["tipo_conteudo"])
        self.assertEqual(
            "data/raw/fonte/direct.html",
            by_id["1"]["conteudo_no_backup"],
        )
        self.assertEqual("encontrado", by_id["2"]["status"])
        self.assertEqual("zip_member", by_id["2"]["tipo_conteudo"])
        self.assertEqual(
            "data/raw/fonte/old/htmls_20261028.zip::archived.html",
            by_id["2"]["conteudo_no_backup"],
        )

        self.assertFalse(
            (self.current_data / "raw" / "fonte" / "direct.html").exists()
        )
        self.assertFalse(
            (self.current_data / "raw" / "fonte" / "archived.html").exists()
        )
        self.assertFalse((work / "downloads" / self.archive_name).exists())
        self.assertTrue(
            (output / "classificacao-atual" / "raws_ausentes.csv").is_file()
        )

    def test_accepts_valid_layer_from_failed_manifest(self) -> None:
        self._write_manifest(status="failed")

        index, warnings = planner._index_manifests(self.remote_directory)

        managed_path = f"backups/staging/{self.archive_name}"
        self.assertEqual([], warnings)
        self.assertIn(managed_path, index)
        self.assertEqual(1, len(index[managed_path]))

        candidates, candidate_warnings = planner._select_candidates(
            [
                {
                    "Path": f"staging/{self.archive_name}",
                    "Size": self.archive_path.stat().st_size,
                }
            ],
            index,
            PurePosixPath("cotacoes-ceasa"),
        )
        self.assertEqual([], candidate_warnings)
        self.assertEqual(1, len(candidates))
        self.assertEqual(
            f"cotacoes-ceasa/{managed_path}",
            candidates[0].remote_path,
        )

    def test_lists_only_authorized_backup_layers(self) -> None:
        responses = {
            "onedrive:cotacoes-ceasa/backups/history/deep": [
                {"Path": "ceasa-data-20261028.zpaq", "Size": 30}
            ],
            "onedrive:cotacoes-ceasa/backups/history/daily": [
                {"Path": "ceasa-data-20261028.tar.xz", "Size": 20}
            ],
            "onedrive:cotacoes-ceasa/backups/staging": [
                {
                    "Path": "ceasa-data-20261028-120000.zip",
                    "Size": 10,
                }
            ],
        }
        calls: list[tuple[str, ...]] = []

        def run_rclone(
            *arguments: str,
            capture_output: bool = False,
        ) -> str:
            self.assertTrue(capture_output)
            self.assertEqual("lsjson", arguments[0])
            self.assertEqual("--files-only", arguments[2])
            self.assertEqual(3, len(arguments))
            calls.append(arguments)
            return json.dumps(responses[arguments[1]])

        with patch.object(planner, "_run_rclone", side_effect=run_rclone):
            inventory = planner._list_remote_backups(
                "onedrive",
                PurePosixPath("cotacoes-ceasa"),
            )

        self.assertEqual(set(responses), {call[1] for call in calls})
        self.assertEqual(
            {
                "history/deep/ceasa-data-20261028.zpaq",
                "history/daily/ceasa-data-20261028.tar.xz",
                "staging/ceasa-data-20261028-120000.zip",
            },
            {str(item["Path"]) for item in inventory},
        )

    def test_refuses_remote_modification_commands(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "nao autorizada"):
            planner._run_rclone(
                "deletefile",
                "onedrive:cotacoes-ceasa/backups/staging/arquivo.zip",
            )

    def _create_v4_database(self, path: Path) -> None:
        with sqlite3.connect(path) as connection:
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
            connection.executemany(
                """
                INSERT INTO coletas (
                    id,
                    fonte_id,
                    caminho_relativo_raw,
                    sha256,
                    status
                ) VALUES (?, 1, ?, ?, 'processada')
                """,
                (
                    (1, "raw/fonte/direct.html", self.direct_hash),
                    (2, "raw/fonte/archived.html", self.archived_hash),
                ),
            )

    def _create_backup_archive(self) -> None:
        backup_data = self.root / "backup-source" / "data"
        backup_data.mkdir(parents=True)
        shutil.copy2(self.database, backup_data / "cotacoes.sqlite")

        direct_path = backup_data / "raw" / "fonte" / "direct.html"
        direct_path.parent.mkdir(parents=True)
        direct_path.write_bytes(self.direct_content)

        raw_zip = backup_data / "raw" / "fonte" / "old" / "htmls_20261028.zip"
        raw_zip.parent.mkdir(parents=True)
        with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("archived.html", self.archived_content)

        self.backup_inventory = collect_data_inventory(backup_data)
        with zipfile.ZipFile(
            self.archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for source in sorted(backup_data.rglob("*")):
                if source.is_file():
                    archive.write(
                        source,
                        source.relative_to(backup_data.parent).as_posix(),
                    )

    def _write_manifest(self, *, status: str) -> None:
        payload = {
            "schema_version": 1,
            "status": status,
            "backup_date": "2026-10-28",
            "file_name": self.manifest_name,
            "layers": {
                "staging": {
                    "status": "uploaded",
                    "file_name": self.archive_name,
                    "remote_directory": "backups/staging",
                    "package": {
                        "status": "completed",
                        "sha256": calculate_sha256(self.archive_path),
                        "size_bytes": self.archive_path.stat().st_size,
                        "source": {
                            "files": self.backup_inventory.files,
                            "bytes": self.backup_inventory.bytes,
                        },
                    },
                    "validation": {"status": "passed"},
                    "upload": {
                        "status": "completed",
                        "final_path": (
                            "cotacoes-ceasa/backups/staging/"
                            + self.archive_name
                        ),
                    },
                }
            },
        }
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    unittest.main()
