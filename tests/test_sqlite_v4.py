import sqlite3
import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from cotacoes_ceasa.cli.commands.source import save_valid_cotacoes
from cotacoes_ceasa.core.models import ColetaStatus, Cotacao
from cotacoes_ceasa.storage.sqlite import SQLiteStorage
from cotacoes_ceasa.storage.sqlite_v4 import (
    SQLITE_V4_SCHEMA_VERSION,
    LegacySQLiteSchemaError,
    SQLiteV4Storage,
)
from cotacoes_ceasa.workflows.collection import _download_category
from cotacoes_ceasa.workflows.prohort import ProhortComplementer
from cotacoes_ceasa.workflows.raw_processing import process_raw_and_report


class _FakeCollector:
    def __init__(self, raw_file: Path) -> None:
        self.raw_file = raw_file

    def download_category(
        self,
        category_slug: str,
        target_date: date | None = None,
    ) -> Path:
        del category_slug, target_date
        return self.raw_file


class _FakeParser:
    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        del content
        return [
            Cotacao(
                fonte="CEASA-PE",
                categoria=category_slug,
                produto="Maca",
                unidade="kg",
                procedencia="PE",
                classificacao="Extra",
                data_cotacao=date(2026, 10, 9),
                preco_minimo=Decimal("9.00"),
                preco_comum=Decimal("10.00"),
                preco_maximo=Decimal("12.00"),
                situacao_mercado="ESTAVEL",
                url_origem=url_origem,
            )
        ]


class _SilentOutput:
    supports_live_progress = False

    def section(self, title: str) -> None:
        del title

    def info(self, message: str) -> None:
        del message

    def warning(self, message: str) -> None:
        del message

    def detail_success(self, message: str, report: bool = False) -> None:
        del message, report

    def progress(self, message: str, visible: bool = True) -> None:
        del message, visible

    def report_summary(
        self,
        rows: tuple[tuple[str, object], ...],
        report_title: str | None = None,
    ) -> None:
        del rows, report_title


class SQLiteV4StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "cotacoes.sqlite"
        self.storage = SQLiteV4Storage(self.database_path)
        self.storage.register_source(
            slug="ceasa-pe",
            name="CEASA-PE",
            base_url="https://example.com",
            uf="PE",
        )

    def test_creates_the_v4_schema_without_legacy_tables(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )

        self.assertEqual(SQLITE_V4_SCHEMA_VERSION, schema_version)
        self.assertIn("fontes", tables)
        self.assertIn("coletas", tables)
        self.assertIn("cotacoes", tables)
        self.assertIn("cotacao_complementos", tables)
        self.assertNotIn("ceasas", tables)
        self.assertNotIn("cotacao_proveniencias", tables)

    def test_document_duplicate_is_scoped_by_source_and_hash(self) -> None:
        raw_hash = "a" * 64
        original_id = self._downloaded_collection(
            "ceasa-pe",
            raw_hash,
            "raw/ceasa-pe/original.html",
        )
        duplicate = self.storage.find_duplicate("ceasa-pe", raw_hash)

        self.storage.register_source(
            slug="ceasa-ba",
            name="CEASA-BA",
            base_url="https://example.com/ba",
            uf="BA",
        )

        self.assertIsNotNone(duplicate)
        self.assertEqual(original_id, duplicate.coleta_id)
        self.assertIsNone(self.storage.find_duplicate("ceasa-ba", raw_hash))

    def test_duplicate_attempt_keeps_only_the_reference(self) -> None:
        raw_hash = "b" * 64
        original_id = self._downloaded_collection(
            "ceasa-pe",
            raw_hash,
            "raw/ceasa-pe/original.html",
        )
        duplicate_id = self.storage.create_coleta(
            "ceasa-pe",
            "https://example.com/repetido",
        )

        self.storage.mark_coleta_duplicate(
            duplicate_id,
            original_id,
            raw_hash,
        )

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT status, duplicada_de_id, caminho_relativo_raw, sha256
                FROM coletas
                WHERE id = ?
                """,
                (duplicate_id,),
            ).fetchone()

        self.assertEqual(
            (
                ColetaStatus.DESCARTADA_DUPLICADA,
                original_id,
                None,
                raw_hash,
            ),
            row,
        )

    def test_processed_document_is_preferred_as_duplicate_reference(self) -> None:
        raw_hash = "f" * 64
        self._downloaded_collection(
            "ceasa-pe",
            raw_hash,
            "raw/ceasa-pe/pendente.html",
        )
        processed_id = self._downloaded_collection(
            "ceasa-pe",
            raw_hash,
            "raw/ceasa-pe/processado.html",
        )
        self.storage.mark_coleta_processed(processed_id)

        duplicate = self.storage.find_duplicate("ceasa-pe", raw_hash)

        self.assertIsNotNone(duplicate)
        self.assertEqual(processed_id, duplicate.coleta_id)

    def test_equal_quotes_from_different_documents_are_preserved(self) -> None:
        first_collection_id = self._downloaded_collection(
            "ceasa-pe",
            "c" * 64,
            "raw/ceasa-pe/primeiro.html",
        )
        second_collection_id = self._downloaded_collection(
            "ceasa-pe",
            "d" * 64,
            "raw/ceasa-pe/segundo.html",
        )
        first_quote = self._quote(first_collection_id)
        second_quote = replace(first_quote, coleta_id=second_collection_id)

        self.assertEqual(
            1,
            self.storage.save_cotacoes(
                [first_quote],
                source_slug="ceasa-pe",
                default_market="Recife",
                uf="PE",
            ),
        )
        self.assertEqual(
            1,
            self.storage.save_cotacoes(
                [second_quote],
                source_slug="ceasa-pe",
                default_market="Recife",
                uf="PE",
            ),
        )

        with sqlite3.connect(self.database_path) as connection:
            quote_count = int(
                connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()[0]
            )
            alias_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM produto_aliases"
                ).fetchone()[0]
            )
            processed_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM coletas WHERE status = 'processada'"
                ).fetchone()[0]
            )

        self.assertEqual(2, quote_count)
        self.assertEqual(1, alias_count)
        self.assertEqual(2, processed_count)

    def test_empty_v4_database_supports_download_process_and_duplicate(self) -> None:
        raw_dir = Path(self.temporary_directory.name) / "raw"
        raw_file = raw_dir / "ceasa-pe" / "frutas_20261009_120000.html"
        raw_file.parent.mkdir(parents=True)
        raw_file.write_text("cotacoes", encoding="utf-8")
        collector = _FakeCollector(raw_file)

        downloaded_file = _download_category(
            collector=collector,
            category_slug="frutas",
            target_date=None,
            raw_dir=raw_dir,
            source_slug="ceasa-pe",
            source_url="https://example.com",
            collection_storage=self.storage,
            output=None,
        )
        duplicate_file = _download_category(
            collector=collector,
            category_slug="frutas",
            target_date=None,
            raw_dir=raw_dir,
            source_slug="ceasa-pe",
            source_url="https://example.com",
            collection_storage=self.storage,
            output=None,
        )

        quotes = process_raw_and_report(
            parser=_FakeParser(),
            raw_dir=raw_dir,
            source_slug="ceasa-pe",
            base_url="https://example.com",
            database_path=self.database_path,
            pdf_text_cache_dir=Path(self.temporary_directory.name) / "cache",
            output=_SilentOutput(),
            raw_files=[raw_file],
            collection_storage=self.storage,
        )
        inserted_count = self.storage.save_cotacoes(
            quotes,
            source_slug="ceasa-pe",
            default_market="Recife",
            uf="PE",
        )

        with sqlite3.connect(self.database_path) as connection:
            statuses = connection.execute(
                "SELECT status FROM coletas ORDER BY id"
            ).fetchall()
            quote_count = int(
                connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()[0]
            )

        self.assertEqual(raw_file, downloaded_file)
        self.assertIsNone(duplicate_file)
        self.assertEqual(1, inserted_count)
        self.assertEqual(1, quote_count)
        self.assertEqual(
            [
                (ColetaStatus.PROCESSADA,),
                (ColetaStatus.DESCARTADA_DUPLICADA,),
            ],
            statuses,
        )

    def test_persistence_failure_finishes_collection_with_error(self) -> None:
        collection_id = self._downloaded_collection(
            "ceasa-pe",
            "2" * 64,
            "raw/ceasa-pe/falha.html",
        )
        args = Namespace(
            database_path=self.database_path.as_posix(),
            source="ceasa-pe",
        )

        with patch(
            "cotacoes_ceasa.cli.commands.source.save_cotacoes",
            side_effect=sqlite3.OperationalError("falha simulada"),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "falha simulada"):
                save_valid_cotacoes(
                    args=args,
                    cotacoes=[self._quote(collection_id)],
                    source_config=object(),
                    output=_SilentOutput(),
                )

        with sqlite3.connect(self.database_path) as connection:
            status, error_message = connection.execute(
                "SELECT status, mensagem_erro FROM coletas WHERE id = ?",
                (collection_id,),
            ).fetchone()

        self.assertEqual(ColetaStatus.ERRO_PROCESSAMENTO, status)
        self.assertEqual(
            "Falha ao persistir cotacoes: falha simulada",
            error_message,
        )

    def test_missing_category_placeholder_is_stored_as_null(self) -> None:
        collection_id = self._downloaded_collection(
            "ceasa-pe",
            "1" * 64,
            "raw/ceasa-pe/sem-categoria.html",
        )
        quote = replace(
            self._quote(collection_id),
            categoria="nao-informada",
        )
        self.storage.save_cotacoes(
            [quote],
            source_slug="ceasa-pe",
            default_market="Recife",
            uf="PE",
        )

        with sqlite3.connect(self.database_path) as connection:
            category_id = connection.execute(
                "SELECT categoria_id FROM produtos"
            ).fetchone()[0]

        self.assertIsNone(category_id)

    def test_does_not_infer_city_from_an_explicit_market_name(self) -> None:
        collection_id = self._downloaded_collection(
            "ceasa-pe",
            "3" * 64,
            "raw/ceasa-pe/entreposto.html",
        )
        quote = replace(
            self._quote(collection_id),
            entreposto="Unidade atacadista",
        )

        self.storage.save_cotacoes(
            [quote],
            source_slug="ceasa-pe",
            default_market="Recife",
            uf="PE",
        )

        with sqlite3.connect(self.database_path) as connection:
            name, city = connection.execute(
                "SELECT nome, cidade FROM entrepostos"
            ).fetchone()

        self.assertEqual("Unidade atacadista", name)
        self.assertIsNone(city)

    def test_refuses_to_initialize_v4_over_legacy_database(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legado.sqlite"
        SQLiteStorage(legacy_path).ensure_schema()

        with self.assertRaises(LegacySQLiteSchemaError):
            SQLiteV4Storage(legacy_path).ensure_schema()

    def test_persists_general_and_category_backfill_separately(self) -> None:
        general = self.storage.save_backfill_state(
            source_slug="ceasa-pe",
            status="partial",
            cursor_date=date(2026, 10, 1),
        )
        category = self.storage.save_backfill_state(
            source_slug="ceasa-pe",
            category_slug="frutas",
            status="complete",
            cursor_date=date(2026, 9, 1),
        )

        self.assertEqual("partial", general.status)
        self.assertEqual("complete", category.status)
        self.assertEqual("frutas", category.category_slug)
        self.assertEqual(2, self.storage.reset_backfill_state("ceasa-pe"))

    def test_prohort_records_the_change_before_filling_empty_price(self) -> None:
        collection_id = self._downloaded_collection(
            "ceasa-pe",
            "e" * 64,
            "raw/ceasa-pe/cotacao.html",
        )
        target = replace(self._quote(collection_id), preco_comum=None)
        self.storage.save_cotacoes(
            [target],
            source_slug="ceasa-pe",
            default_market="Recife",
            uf="PE",
        )
        complementer = ProhortComplementer(
            self.database_path,
            "https://example.com/prohort.csv",
            raw_dir=Path(self.temporary_directory.name) / "raw",
        )
        content = (
            "data_preco;preco_diario;sig_unidade_medida;dsc_ceasa;dsc_produto\n"
            "2026-10-09;10,50;KG;CEASA/PE - RECIFE;Maca\n"
            "2026-10-09;8,00;KG;CEASA/PE - RECIFE;Banana\n"
        ).encode("latin-1")

        with patch.object(
            complementer,
            "_download_prohort_content",
            return_value=content,
        ):
            result = complementer.complement()

        with sqlite3.connect(self.database_path) as connection:
            quote = connection.execute(
                "SELECT id, preco_comum FROM cotacoes"
            ).fetchone()
            complement = connection.execute(
                """
                SELECT cotacao_id, campo, valor_anterior, valor_novo
                FROM cotacao_complementos
                """
            ).fetchone()
            prohort_status = connection.execute(
                """
                SELECT col.status
                FROM coletas col
                JOIN fontes f ON f.id = col.fonte_id
                WHERE f.slug = 'prohort'
                """
            ).fetchone()[0]
            inserted_category_id = connection.execute(
                """
                SELECT p.categoria_id
                FROM produtos p
                JOIN produto_aliases pa ON pa.produto_id = p.id
                WHERE pa.texto_original = 'Banana'
                """
            ).fetchone()[0]

        self.assertEqual(1, result.updated_count)
        self.assertEqual(1, result.inserted_count)
        self.assertEqual(Decimal("10.50"), Decimal(str(quote[1])))
        self.assertEqual((quote[0], "preco_comum", None, "10.50"), complement)
        self.assertEqual(ColetaStatus.PROCESSADA, prohort_status)
        self.assertIsNone(inserted_category_id)

    def _downloaded_collection(
        self,
        source_slug: str,
        raw_hash: str,
        relative_path: str,
    ) -> int:
        coleta_id = self.storage.create_coleta(
            source_slug,
            "https://example.com/cotacao",
        )
        self.storage.mark_coleta_downloaded(
            coleta_id,
            raw_hash,
            relative_path,
        )

        return coleta_id

    @staticmethod
    def _quote(coleta_id: int) -> Cotacao:
        return Cotacao(
            fonte="CEASA-PE",
            categoria="frutas",
            produto="Maca",
            unidade="kg",
            procedencia="PE",
            classificacao="Extra",
            data_cotacao=date(2026, 10, 9),
            preco_minimo=Decimal("9.00"),
            preco_comum=Decimal("10.00"),
            preco_maximo=Decimal("12.00"),
            situacao_mercado="ESTAVEL",
            url_origem="https://example.com/cotacao",
            coleta_id=coleta_id,
            variedade="Fuji",
        )


if __name__ == "__main__":
    unittest.main()
