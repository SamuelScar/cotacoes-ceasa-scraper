import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.storage.sqlite import (
    HISTORICAL_PROVENANCE_MIGRATION,
    SQLITE_SCHEMA_VERSION,
    SQLiteStorage,
)
from cotacoes_ceasa.workflows.provenance import (
    analyze_provenance,
    apply_historical_provenance_migration,
    create_provenance_candidate,
    write_duplicate_provenance_manifest,
    write_provenance_report,
)


class ProvenanceMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_path = Path(self.temporary_directory.name)
        self.source_path = temporary_path / "origem.sqlite"
        self.candidate_path = temporary_path / "candidata.sqlite"
        self.storage = SQLiteStorage(self.source_path)
        self._create_legacy_duplicate()

    def test_creates_additive_candidate_without_changing_source(self) -> None:
        source_bytes_before = self.source_path.read_bytes()

        result = create_provenance_candidate(
            self.source_path,
            self.candidate_path,
            expected_duplicate_groups=1,
            expected_duplicate_occurrences=2,
            expected_duplicate_excess=1,
        )

        self.assertTrue(result.valid)
        self.assertEqual(source_bytes_before, self.source_path.read_bytes())
        self.assertEqual(2, result.after.quotes)
        self.assertEqual(2, result.after.original_quotes_covered)
        self.assertEqual(0, result.after.original_quotes_missing)
        self.assertEqual(1, result.after.duplicate_groups)
        self.assertEqual(2, result.after.duplicate_occurrences)
        self.assertEqual(2, result.after.duplicate_provenances)
        self.assertEqual(0, result.after.ambiguous_duplicate_groups)
        self.assertEqual(SQLITE_SCHEMA_VERSION, result.after.schema_version)

        self.assertTrue(result.after.provenance_migration_applied)
        self.assertEqual(2, result.after.provenance_migration_quote_max_id)

        self.assertFalse(
            apply_historical_provenance_migration(self.candidate_path)
        )
        repeated = analyze_provenance(self.candidate_path)
        self.assertEqual(result.after.provenance_rows, repeated.provenance_rows)

    def test_maps_duplicate_origins_to_same_canonical_quote(self) -> None:
        create_provenance_candidate(
            self.source_path,
            self.candidate_path,
            expected_duplicate_groups=1,
            expected_duplicate_occurrences=2,
            expected_duplicate_excess=1,
        )

        with sqlite3.connect(self.candidate_path) as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    COUNT(DISTINCT cotacao_origem_id),
                    COUNT(DISTINCT cotacao_id),
                    MIN(cotacao_id)
                FROM cotacao_proveniencias
                WHERE origem_registro = 'migracao_historica'
                """
            ).fetchone()

        self.assertEqual((2, 2, 1, 1), row)

    def test_writes_auditable_manifest(self) -> None:
        result = create_provenance_candidate(
            self.source_path,
            self.candidate_path,
            expected_duplicate_groups=1,
            expected_duplicate_occurrences=2,
            expected_duplicate_excess=1,
        )
        report_path = Path(self.temporary_directory.name) / "manifesto.json"

        write_provenance_report(result, report_path)

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual("valid", payload["status"])
        self.assertEqual(0, payload["safety"]["historical_quotes_deleted"])
        self.assertFalse(payload["safety"]["automatic_consolidation"])
        self.assertEqual(
            HISTORICAL_PROVENANCE_MIGRATION,
            payload["safety"]["migration_key"],
        )
        self.assertEqual(2, payload["after"]["duplicate_provenances"])

    def test_writes_one_manifest_row_per_duplicate_origin(self) -> None:
        create_provenance_candidate(
            self.source_path,
            self.candidate_path,
            expected_duplicate_groups=1,
            expected_duplicate_occurrences=2,
            expected_duplicate_excess=1,
        )
        manifest_path = Path(self.temporary_directory.name) / "manifesto.csv"

        rows = write_duplicate_provenance_manifest(
            self.candidate_path,
            manifest_path,
        )

        lines = manifest_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, rows)
        self.assertEqual(3, len(lines))

    def test_refuses_to_overwrite_candidate(self) -> None:
        self.candidate_path.write_bytes(b"existente")

        with self.assertRaises(FileExistsError):
            create_provenance_candidate(self.source_path, self.candidate_path)

    def test_analysis_reports_missing_historical_origin_before_migration(self) -> None:
        analysis = analyze_provenance(self.source_path)

        self.assertEqual(2, analysis.quotes)
        self.assertEqual(2, analysis.original_quotes_missing)
        self.assertEqual(1, analysis.ambiguous_duplicate_groups)

    def _create_legacy_duplicate(self) -> None:
        first = self._quote("primeiro.html", "hash-1")
        repeated = replace(
            first,
            arquivo_raw="segundo.html",
            hash_raw="hash-2",
            baixado_em=datetime(2026, 8, 11, 9, 0),
        )
        self.assertEqual(1, self._save([first]))
        self.assertEqual(0, self._save([repeated]))

        with sqlite3.connect(self.source_path) as connection:
            second_collection_id = connection.execute(
                "SELECT MAX(id) FROM coletas"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO cotacoes (
                    chave_unica,
                    chave_identidade,
                    coleta_id,
                    entreposto_id,
                    categoria_id,
                    produto_alias_id,
                    apresentacao_unidade_id,
                    data_cotacao,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    procedencia,
                    classificacao,
                    situacao_mercado,
                    fonte_complemento,
                    url_complemento,
                    data_complemento
                )
                SELECT
                    'chave-legada-duplicada',
                    chave_identidade,
                    ?,
                    entreposto_id,
                    categoria_id,
                    produto_alias_id,
                    apresentacao_unidade_id,
                    data_cotacao,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    procedencia,
                    classificacao,
                    situacao_mercado,
                    fonte_complemento,
                    url_complemento,
                    data_complemento
                FROM cotacoes
                WHERE id = 1
                """,
                (second_collection_id,),
            )
            connection.execute("DROP TABLE cotacao_proveniencias")
            connection.execute("DROP TABLE schema_migrations")
            connection.execute("PRAGMA user_version = 2")

    def _save(self, quotes: list[Cotacao]) -> int:
        return self.storage.save_cotacoes(
            cotacoes=quotes,
            source_slug="fonte",
            source_name="Fonte",
            state_name="Estado",
            uf="UF",
            city="Cidade",
            source_url="https://example.com",
        )

    @staticmethod
    def _quote(raw_file: str, raw_hash: str) -> Cotacao:
        return Cotacao(
            fonte="Fonte",
            categoria="categoria",
            produto="Produto",
            unidade="kg",
            procedencia="Origem",
            classificacao="Tipo",
            data_cotacao=date(2026, 8, 10),
            preco_minimo=Decimal("9"),
            preco_comum=Decimal("10"),
            preco_maximo=Decimal("12"),
            situacao_mercado="ESTAVEL",
            url_origem="https://example.com/cotacao",
            arquivo_raw=raw_file,
            hash_raw=raw_hash,
            baixado_em=datetime(2026, 8, 10, 9, 0),
        )


if __name__ == "__main__":
    unittest.main()
