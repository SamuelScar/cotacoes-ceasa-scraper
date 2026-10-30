import json
import tempfile
import unittest
from pathlib import Path

from cotacoes_ceasa.raw_recovery_report import (
    RawRecoveryReportError,
    consolidate_raw_recovery,
)


class RawRecoveryReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.planning_path = self.root / "planning.json"
        self.recovery_path = self.root / "recovery.json"
        self.audit_path = self.root / "audit.json"
        self.result_path = self.root / "result.json"
        self.report_path = self.root / "report.md"
        self.planning_sha256 = "a" * 64
        self.current_sha256 = "b" * 64
        self._write_payloads()

    def test_accepts_new_collections_when_planned_collections_are_validated(
        self,
    ) -> None:
        result = consolidate_raw_recovery(
            planning_result_path=self.planning_path,
            recovery_result_path=self.recovery_path,
            audit_result_path=self.audit_path,
            result_path=self.result_path,
            report_path=self.report_path,
        )

        self.assertEqual(
            "completed_with_expected_unresolved_raws",
            result["status"],
        )
        self.assertFalse(result["traceability"]["exact_planning_snapshot"])
        self.assertEqual(
            2,
            result["traceability"]["collections_added_since_planning"],
        )
        self.assertEqual(
            self.current_sha256,
            result["candidate"]["database_sha256"],
        )

    def test_refuses_recovery_without_all_planned_collections_validated(
        self,
    ) -> None:
        recovery = json.loads(self.recovery_path.read_text(encoding="utf-8"))
        recovery["source"]["plan_collections_validated"] = 3
        self.recovery_path.write_text(
            json.dumps(recovery),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            RawRecoveryReportError,
            "Nem todas as coletas do plano",
        ):
            consolidate_raw_recovery(
                planning_result_path=self.planning_path,
                recovery_result_path=self.recovery_path,
                audit_result_path=self.audit_path,
                result_path=self.result_path,
                report_path=self.report_path,
            )

    def test_refuses_new_missing_raws_outside_plan(self) -> None:
        audit = json.loads(self.audit_path.read_text(encoding="utf-8"))
        audit["raw_traceability"]["exact_matches"] = 10
        audit["raw_traceability"]["missing_files"] = 2
        audit["findings"][0]["occurrences"] = 2
        self.audit_path.write_text(json.dumps(audit), encoding="utf-8")

        with self.assertRaisesRegex(
            RawRecoveryReportError,
            "nao estavam cobertos pelo plano",
        ):
            consolidate_raw_recovery(
                planning_result_path=self.planning_path,
                recovery_result_path=self.recovery_path,
                audit_result_path=self.audit_path,
                result_path=self.result_path,
                report_path=self.report_path,
            )

    def _write_payloads(self) -> None:
        before = {
            "checked": 10,
            "exact_match": 6,
            "relocated_match": 0,
            "ambiguous_candidates": 0,
            "missing": 4,
            "hash_mismatch": 0,
            "unreadable": 0,
        }
        planning = {
            "schema_version": 1,
            "status": "completed",
            "operation": "raw_recovery_plan",
            "database": {
                "model": "v4",
                "sha256_before": self.planning_sha256,
                "sha256_after": self.planning_sha256,
                "unchanged": True,
            },
            "current_traceability": before,
            "recovery_coverage": {
                "missing_records": 4,
                "missing_unique_hashes": 3,
                "records_found_in_backups": 3,
                "unique_hashes_found_in_backups": 2,
                "records_not_found": 1,
                "records_with_invalid_target": 0,
            },
        }
        recovery = {
            "schema_version": 1,
            "status": "completed",
            "operation": "raw_recovery_candidate",
            "plan": {
                "recoverable_records": 3,
                "recoverable_unique_hashes": 2,
                "not_found_records": 1,
                "invalid_target_records": 0,
            },
            "recovery": {
                "records_recovered": 3,
                "records_already_present": 0,
                "operations_recovered": 3,
                "operations_already_present": 0,
                "target_operations": 3,
            },
            "source": {
                "planning_database_sha256": self.planning_sha256,
                "database_sha256_before": self.current_sha256,
                "database_sha256_after": self.current_sha256,
                "exact_planning_snapshot": False,
                "plan_collections_validated": 4,
                "database_unchanged": True,
                "tree_unchanged": True,
                "inventory_before": {"files": 10, "bytes": 100},
                "inventory_after": {"files": 10, "bytes": 100},
            },
            "candidate": {
                "root": "candidate",
                "database_sha256_before": self.current_sha256,
                "database_sha256_after": self.current_sha256,
                "database_unchanged": True,
            },
        }
        audit = {
            "schema_version": 6,
            "database_model": "sqlite_v4_issue9",
            "execution_status": "completed",
            "parameters": {
                "verify_raw": True,
                "full_integrity_check": True,
            },
            "safety": {
                "audit_only": True,
                "database_opened_read_only": True,
            },
            "verdict": {
                "status": "inconsistente",
                "error_findings": 1,
            },
            "raw_traceability": {
                "canonical_raws_checked": 12,
                "exact_matches": 11,
                "relocated_matches": 0,
                "ambiguous_candidates": 0,
                "missing_files": 1,
                "hash_mismatches": 0,
                "unreadable_files": 0,
            },
            "findings": [
                {
                    "code": "raw_files_missing",
                    "severity": "erro",
                    "occurrences": 1,
                }
            ],
        }
        for path, payload in (
            (self.planning_path, planning),
            (self.recovery_path, recovery),
            (self.audit_path, audit),
        ):
            path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
