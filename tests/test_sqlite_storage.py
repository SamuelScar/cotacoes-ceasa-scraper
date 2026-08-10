import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.storage.sqlite import SQLITE_SCHEMA_VERSION, SQLiteStorage
from cotacoes_ceasa.workflows.prohort import ProhortComplementer


class SQLiteStorageDeduplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "cotacoes.sqlite"
        self.storage = SQLiteStorage(self.database_path)

    def test_same_content_from_another_collection_is_not_inserted(self) -> None:
        first = self._quote("primeiro.html", "hash-1", Decimal("10.00"))
        repeated = replace(
            first,
            arquivo_raw="segundo.html",
            hash_raw="hash-2",
            baixado_em=datetime(2026, 8, 11, 9, 0),
            preco_comum=Decimal("10"),
        )

        self.assertEqual(1, self._save([first]))
        self.assertEqual(0, self._save([repeated]))

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), MIN(col.arquivo_raw)
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                """
            ).fetchone()
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]

        self.assertEqual((1, "primeiro.html"), row)
        self.assertEqual(SQLITE_SCHEMA_VERSION, schema_version)

    def test_same_content_repeated_inside_batch_is_inserted_once(self) -> None:
        first = self._quote("primeiro.html", "hash-1")
        repeated = replace(
            first,
            arquivo_raw="segundo.html",
            hash_raw="hash-2",
            baixado_em=datetime(2026, 8, 11, 9, 0),
        )

        self.assertEqual(1, self._save([first, repeated]))
        self.assertEqual(1, self._count_quotes())

    def test_price_and_market_status_changes_are_preserved(self) -> None:
        first = self._quote("primeiro.html", "hash-1")
        changed_price = replace(
            first,
            arquivo_raw="preco.html",
            hash_raw="hash-2",
            preco_comum=Decimal("11"),
        )
        changed_status = replace(
            first,
            arquivo_raw="situacao.html",
            hash_raw="hash-3",
            situacao_mercado="FIRME",
        )

        self.assertEqual(1, self._save([first]))
        self.assertEqual(1, self._save([changed_price]))
        self.assertEqual(1, self._save([changed_status]))
        self.assertEqual(3, self._count_quotes())

    def test_legacy_collection_key_does_not_allow_transition_duplicate(self) -> None:
        first = self._quote("primeiro.html", "hash-1")
        repeated = replace(
            first,
            arquivo_raw="segundo.html",
            hash_raw="hash-2",
            baixado_em=datetime(2026, 8, 11, 9, 0),
        )
        self.assertEqual(1, self._save([first]))

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE cotacoes SET chave_unica = 'chave-legada'"
            )

        self.assertEqual(0, self._save([repeated]))
        self.assertEqual(1, self._count_quotes())

    def test_identity_change_is_preserved(self) -> None:
        first = self._quote("primeiro.html", "hash-1")
        next_date = replace(
            first,
            arquivo_raw="outra-data.html",
            hash_raw="hash-2",
            data_cotacao=date(2026, 8, 11),
        )

        self.assertEqual(1, self._save([first]))
        self.assertEqual(1, self._save([next_date]))
        self.assertEqual(2, self._count_quotes())

    def test_prohort_update_recalculates_content_key(self) -> None:
        target = self._quote("primeiro.html", "hash-1", None)
        self.assertEqual(1, self._save([target]))
        complementer = ProhortComplementer(
            self.database_path,
            "https://example.com/prohort.csv",
        )

        with sqlite3.connect(self.database_path) as connection:
            target_id = int(connection.execute("SELECT id FROM cotacoes").fetchone()[0])
            self.assertEqual(
                1,
                complementer._update_target(connection, target_id, Decimal("10")),
            )
            row = connection.execute(
                """
                SELECT
                    chave_unica,
                    chave_identidade,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado
                FROM cotacoes
                WHERE id = ?
                """,
                (target_id,),
            ).fetchone()

        expected_key = self.storage.build_content_key(
            identity_key=row[1],
            preco_minimo=row[2],
            preco_comum=row[3],
            preco_maximo=row[4],
            situacao_mercado=row[5],
        )
        self.assertEqual(expected_key, row[0])

    def test_prohort_update_does_not_create_exact_duplicate(self) -> None:
        target = self._quote("primeiro.html", "hash-1", None)
        complemented = replace(
            target,
            arquivo_raw="segundo.html",
            hash_raw="hash-2",
            preco_comum=Decimal("10"),
        )
        self.assertEqual(1, self._save([target]))
        self.assertEqual(1, self._save([complemented]))
        complementer = ProhortComplementer(
            self.database_path,
            "https://example.com/prohort.csv",
        )

        with sqlite3.connect(self.database_path) as connection:
            target_id = int(
                connection.execute(
                    "SELECT id FROM cotacoes WHERE preco_comum IS NULL"
                ).fetchone()[0]
            )
            updated = complementer._update_target(
                connection,
                target_id,
                Decimal("10"),
            )
            common_price = connection.execute(
                "SELECT preco_comum FROM cotacoes WHERE id = ?",
                (target_id,),
            ).fetchone()[0]

        self.assertEqual(0, updated)
        self.assertIsNone(common_price)

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

    def _count_quotes(self) -> int:
        with sqlite3.connect(self.database_path) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()[0])

    @staticmethod
    def _quote(
        raw_file: str,
        raw_hash: str,
        common_price: Decimal | None = Decimal("10"),
    ) -> Cotacao:
        return Cotacao(
            fonte="Fonte",
            categoria="categoria",
            produto="Produto",
            unidade="kg",
            procedencia="Origem",
            classificacao="Tipo",
            data_cotacao=date(2026, 8, 10),
            preco_minimo=Decimal("9"),
            preco_comum=common_price,
            preco_maximo=Decimal("12"),
            situacao_mercado="ESTAVEL",
            url_origem="https://example.com/cotacao",
            arquivo_raw=raw_file,
            hash_raw=raw_hash,
            baixado_em=datetime(2026, 8, 10, 9, 0),
        )


if __name__ == "__main__":
    unittest.main()
