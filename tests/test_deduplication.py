import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.storage.sqlite import SQLITE_SCHEMA_VERSION, SQLiteStorage
from cotacoes_ceasa.workflows.deduplication import (
    analyze_duplicate_content,
    create_candidate_baseline,
    write_duplicate_report,
)


class CandidateBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        temporary_path = Path(self.temporary_directory.name)
        self.database_path = temporary_path / "origem.sqlite"
        self.candidate_path = temporary_path / "candidata.sqlite"
        self.storage = SQLiteStorage(self.database_path)
        self._create_legacy_duplicate()

    def test_analyzes_exact_repetitions(self) -> None:
        analysis = analyze_duplicate_content(self.database_path)

        self.assertEqual(2, analysis.observations)
        self.assertEqual(1, analysis.logical_contents)
        self.assertEqual(1, analysis.repeated_observations)
        self.assertEqual(1, analysis.source_date_buckets)
        self.assertEqual(1, len(analysis.sources))

    def test_creates_valid_candidate_without_changing_source(self) -> None:
        result = create_candidate_baseline(
            self.database_path,
            self.candidate_path,
        )

        self.assertTrue(result.valid)
        self.assertEqual(1, result.removed_observations)
        self.assertEqual(0, result.after.repeated_observations)
        self.assertEqual(
            result.before.source_date_coverage_hash,
            result.after.source_date_coverage_hash,
        )
        self.assertEqual(2, self._count_quotes(self.database_path))
        self.assertEqual(1, self._count_quotes(self.candidate_path))

        with sqlite3.connect(self.candidate_path) as connection:
            raw_file = connection.execute(
                """
                SELECT col.arquivo_raw
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                """
            ).fetchone()[0]
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual("primeiro.html", raw_file)
        self.assertEqual(SQLITE_SCHEMA_VERSION, schema_version)

    def test_writes_structured_candidate_report(self) -> None:
        result = create_candidate_baseline(
            self.database_path,
            self.candidate_path,
        )
        report_path = Path(self.temporary_directory.name) / "baseline.json"

        write_duplicate_report(result, report_path)

        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("valid", payload["status"])
        self.assertEqual(1, payload["removed_observations"])
        self.assertTrue(payload["requires_full_supabase_replace"])

    def test_refuses_to_overwrite_existing_candidate(self) -> None:
        self.candidate_path.write_bytes(b"existente")

        with self.assertRaises(FileExistsError):
            create_candidate_baseline(self.database_path, self.candidate_path)

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

        with sqlite3.connect(self.database_path) as connection:
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
    def _count_quotes(database_path: Path) -> int:
        with sqlite3.connect(database_path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()[0])

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
