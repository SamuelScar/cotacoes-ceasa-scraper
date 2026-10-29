import csv
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from cotacoes_ceasa.backups.packaging import calculate_sha256
from cotacoes_ceasa.backups.restore import collect_data_inventory
from cotacoes_ceasa.raw_recovery import (
    INCOMPLETE_MARKER,
    RawRecoveryError,
    build_recovery_input_inventory,
    create_raw_recovery_candidate,
)


class RawRecoveryCandidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.current_data = self.root / "current" / "data"
        self.current_data.mkdir(parents=True)
        self.database = self.current_data / "cotacoes.sqlite"
        self._create_database(self.database)
        present = self.current_data / "raw" / "fonte" / "presente.html"
        present.parent.mkdir(parents=True)
        present.write_bytes(b"raw que ja existia")

        self.direct_content = b"raw recuperado diretamente"
        self.archived_content = b"raw recuperado do zip"
        self.missing_content = b"raw ainda ausente"
        self.direct_hash = self._sha256(self.direct_content)
        self.archived_hash = self._sha256(self.archived_content)
        self.missing_hash = self._sha256(self.missing_content)

        self.archives = self.root / "archives"
        self.manifests = self.root / "manifests"
        self.archives.mkdir()
        self.manifests.mkdir()
        self.archive_name = "ceasa-data-20261029-120000.zip"
        self.manifest_name = "backup-20261030-120000.json"
        self.archive_path = self.archives / self.archive_name
        self.manifest_path = self.manifests / self.manifest_name
        self._create_backup()
        self._write_manifest()

        self.plan_path = self.root / "plano-recuperacao-raws.csv"
        self.planning_result_path = self.root / "resultado.json"
        self._write_plan()
        self._write_planning_result()

    def test_builds_input_inventory_for_manifest_created_later(self) -> None:
        inventory = build_recovery_input_inventory(
            plan_path=self.plan_path,
            planning_result_path=self.planning_result_path,
            remote_root="cotacoes-ceasa",
        )

        self.assertEqual("validated", inventory["status"])
        self.assertEqual(1, inventory["backups_required"])
        self.assertEqual(
            "cotacoes-ceasa/backups/audit/" + self.manifest_name,
            inventory["backups"][0]["manifest_remote_path"],
        )

    def test_recovers_direct_and_zipped_raws_only_in_candidate(self) -> None:
        database_hash_before = calculate_sha256(self.database)
        source_inventory_before = collect_data_inventory(self.current_data)
        archive_hash_before = calculate_sha256(self.archive_path)
        candidate = self.root / "candidate"
        result_path = self.root / "recovery-result.json"
        report_path = self.root / "recovery-report.md"

        result = create_raw_recovery_candidate(
            current_data_directory=self.current_data,
            plan_path=self.plan_path,
            planning_result_path=self.planning_result_path,
            archives_directory=self.archives,
            manifests_directory=self.manifests,
            candidate_root=candidate,
            work_directory=self.root / "work",
            result_path=result_path,
            report_path=report_path,
        )

        candidate_data = candidate / "data"
        self.assertEqual(
            self.direct_content,
            (candidate_data / "raw" / "fonte" / "recuperado.html").read_bytes(),
        )
        self.assertEqual(
            self.archived_content,
            (candidate_data / "raw" / "fonte" / "arquivo-a.html").read_bytes(),
        )
        self.assertEqual(
            self.archived_content,
            (candidate_data / "raw" / "fonte" / "arquivo-b.html").read_bytes(),
        )
        self.assertFalse(
            (self.current_data / "raw" / "fonte" / "recuperado.html").exists()
        )
        self.assertFalse(
            (self.current_data / "raw" / "fonte" / "arquivo-a.html").exists()
        )
        self.assertEqual(database_hash_before, calculate_sha256(self.database))
        self.assertEqual(
            source_inventory_before,
            collect_data_inventory(self.current_data),
        )
        self.assertEqual(archive_hash_before, calculate_sha256(self.archive_path))
        self.assertEqual(
            database_hash_before,
            calculate_sha256(candidate_data / "cotacoes.sqlite"),
        )
        self.assertFalse((candidate / INCOMPLETE_MARKER).exists())
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["recovery"]["backups_required"])
        self.assertEqual(3, result["recovery"]["target_operations"])
        self.assertEqual(3, result["recovery"]["operations_recovered"])
        self.assertEqual(3, result["recovery"]["records_recovered"])
        self.assertTrue(result["candidate"]["database_unchanged"])
        self.assertTrue(result_path.is_file())
        self.assertTrue(report_path.is_file())

    def test_refuses_to_overwrite_divergent_candidate_file(self) -> None:
        conflicting = self.current_data / "raw" / "fonte" / "recuperado.html"
        conflicting.write_bytes(b"conteudo divergente")
        source_hash_before = calculate_sha256(conflicting)
        candidate = self.root / "conflicting-candidate"

        with self.assertRaisesRegex(
            RawRecoveryError,
            "nao sera sobrescrito",
        ):
            create_raw_recovery_candidate(
                current_data_directory=self.current_data,
                plan_path=self.plan_path,
                planning_result_path=self.planning_result_path,
                archives_directory=self.archives,
                manifests_directory=self.manifests,
                candidate_root=candidate,
                work_directory=self.root / "conflicting-work",
                result_path=self.root / "conflicting-result.json",
                report_path=self.root / "conflicting-report.md",
            )

        self.assertEqual(source_hash_before, calculate_sha256(conflicting))
        self.assertTrue((candidate / INCOMPLETE_MARKER).is_file())
        self.assertFalse(
            (candidate / "data" / "raw" / "fonte" / "arquivo-a.html").exists()
        )

    def test_refuses_manifest_not_linked_to_planned_backup(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["layers"]["staging"]["file_name"] = (
            "ceasa-data-20261029-130000.zip"
        )
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "manifesto nao identifica exatamente uma camada",
        ):
            create_raw_recovery_candidate(
                current_data_directory=self.current_data,
                plan_path=self.plan_path,
                planning_result_path=self.planning_result_path,
                archives_directory=self.archives,
                manifests_directory=self.manifests,
                candidate_root=self.root / "unlinked-manifest-candidate",
                work_directory=self.root / "unlinked-manifest-work",
                result_path=self.root / "unlinked-manifest-result.json",
                report_path=self.root / "unlinked-manifest-report.md",
            )

    def test_refuses_plan_with_coverage_divergent_from_result(self) -> None:
        result = json.loads(
            self.planning_result_path.read_text(encoding="utf-8")
        )
        result["recovery_coverage"]["records_found_in_backups"] = 2
        self.planning_result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        candidate = self.root / "invalid-plan-candidate"

        with self.assertRaisesRegex(
            RawRecoveryError,
            "cobertura do plano diverge",
        ):
            create_raw_recovery_candidate(
                current_data_directory=self.current_data,
                plan_path=self.plan_path,
                planning_result_path=self.planning_result_path,
                archives_directory=self.archives,
                manifests_directory=self.manifests,
                candidate_root=candidate,
                work_directory=self.root / "invalid-plan-work",
                result_path=self.root / "invalid-plan-result.json",
                report_path=self.root / "invalid-plan-report.md",
            )

        self.assertFalse(candidate.exists())

    def _create_database(self, path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE coletas (id INTEGER PRIMARY KEY, sha256 TEXT)"
            )

    def _create_backup(self) -> None:
        backup_data = self.root / "backup-source" / "data"
        backup_data.mkdir(parents=True)
        shutil.copy2(self.database, backup_data / "cotacoes.sqlite")
        direct = backup_data / "raw" / "fonte" / "origem.html"
        direct.parent.mkdir(parents=True)
        direct.write_bytes(self.direct_content)
        raw_zip = backup_data / "raw" / "fonte" / "old" / "htmls.zip"
        raw_zip.parent.mkdir(parents=True)
        with zipfile.ZipFile(
            raw_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as package:
            package.writestr("historico.html", self.archived_content)

        self.backup_inventory = collect_data_inventory(backup_data)
        with zipfile.ZipFile(
            self.archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as package:
            for source in sorted(backup_data.rglob("*")):
                if source.is_file():
                    package.write(
                        source,
                        source.relative_to(backup_data.parent).as_posix(),
                    )

    def _write_manifest(self) -> None:
        payload = {
            "schema_version": 1,
            "status": "completed",
            "backup_date": "2026-10-29",
            "file_name": self.manifest_name,
            "layers": {
                "staging": {
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

    def _write_plan(self) -> None:
        headers = (
            "coleta_id",
            "fonte",
            "caminho_registrado",
            "destino_em_data",
            "sha256_esperado",
            "status",
            "acao_proposta",
            "backup_remoto",
            "sha256_backup",
            "data_backup",
            "manifesto",
            "conteudo_no_backup",
            "tipo_conteudo",
            "tamanho_conteudo",
        )
        backup_remote = (
            "cotacoes-ceasa/backups/staging/" + self.archive_name
        )
        backup_sha256 = calculate_sha256(self.archive_path)
        found_common = (
            "encontrado",
            "copiar_sem_sobrescrever",
            backup_remote,
            backup_sha256,
            "2026-10-29",
            self.manifest_name,
        )
        rows = (
            (
                1,
                "fonte",
                "raw/fonte/recuperado.html",
                "raw/fonte/recuperado.html",
                self.direct_hash,
                *found_common,
                "data/raw/fonte/origem.html",
                "file",
                len(self.direct_content),
            ),
            (
                2,
                "fonte",
                "raw/fonte/arquivo-a.html",
                "raw/fonte/arquivo-a.html",
                self.archived_hash,
                *found_common,
                "data/raw/fonte/old/htmls.zip::historico.html",
                "zip_member",
                len(self.archived_content),
            ),
            (
                3,
                "fonte",
                "raw/fonte/arquivo-b.html",
                "raw/fonte/arquivo-b.html",
                self.archived_hash,
                *found_common,
                "data/raw/fonte/old/htmls.zip::historico.html",
                "zip_member",
                len(self.archived_content),
            ),
            (
                4,
                "fonte",
                "raw/fonte/ausente.html",
                "raw/fonte/ausente.html",
                self.missing_hash,
                "nao_encontrado",
                "nenhuma",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
        )
        with self.plan_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(headers)
            writer.writerows(rows)

    def _write_planning_result(self) -> None:
        database_sha256 = calculate_sha256(self.database)
        payload = {
            "schema_version": 1,
            "status": "completed",
            "operation": "raw_recovery_plan",
            "database": {
                "sha256_before": database_sha256,
                "sha256_after": database_sha256,
                "unchanged": True,
            },
            "recovery_coverage": {
                "missing_records": 4,
                "missing_unique_hashes": 3,
                "records_found_in_backups": 3,
                "unique_hashes_found_in_backups": 2,
                "records_not_found": 1,
                "records_with_invalid_target": 0,
            },
            "backup_inventory": {
                "validated_candidates": 1,
                "processed": 1,
                "skipped": 0,
                "errors": 0,
            },
            "files": {
                "recovery_plan": self.plan_path.name,
            },
            "safety": {
                "remote_access": "read_only",
                "remote_files_modified": 0,
                "database_replaced": False,
            },
        }
        self.planning_result_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    unittest.main()
