import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from cotacoes_ceasa.cli.collection_mode import prepare_collection_mode
from cotacoes_ceasa.config import AppConfig, SourceConfig


class CollectionModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = self._build_config()

    def test_legacy_mode_preserves_existing_configuration(self) -> None:
        args = self._build_args(collection_mode=None, quotes_back=10)

        effective_config = prepare_collection_mode(args, self.config)

        self.assertIs(self.config, effective_config)
        self.assertEqual("legacy", args.effective_collection_mode)
        self.assertEqual(10, args.quotes_back)
        self.assertTrue(args.incremental_history)

    def test_current_mode_forces_latest_publication(self) -> None:
        args = self._build_args(
            collection_mode="current",
            target_date="2026-07-01",
            quotes_back=10,
        )

        effective_config = prepare_collection_mode(args, self.config)

        self.assertIs(self.config, effective_config)
        self.assertIsNone(args.target_date)
        self.assertEqual(0, args.quotes_back)
        self.assertFalse(args.incremental_history)
        self.assertEqual("2026-07-01", args.requested_target_date)
        self.assertEqual(10, args.requested_quotes_back)

    def test_backfill_filters_sources_and_enables_incremental_cursor(self) -> None:
        args = self._build_args(collection_mode="backfill", quotes_back=10)

        effective_config = prepare_collection_mode(args, self.config)

        self.assertEqual(("ceasa-pe",), tuple(effective_config.sources))
        self.assertTrue(args.incremental_history)
        self.assertEqual(("ceasa-go", "ceasa-mg"), args.backfill_excluded_sources)

    def test_backfill_rejects_disabled_explicit_source(self) -> None:
        args = self._build_args(
            collection_mode="backfill",
            quotes_back=10,
            source="ceasa-go",
        )

        with self.assertRaisesRegex(ValueError, "nao esta habilitada"):
            prepare_collection_mode(args, self.config)

    def test_backfill_requires_historical_window(self) -> None:
        args = self._build_args(collection_mode="backfill", quotes_back=0)

        with self.assertRaisesRegex(ValueError, "exige --quotes-back"):
            prepare_collection_mode(args, self.config)

    @staticmethod
    def _build_args(
        collection_mode: str | None,
        quotes_back: int | None,
        target_date: str | None = None,
        source: str | None = None,
    ) -> Namespace:
        return Namespace(
            collection_mode=collection_mode,
            target_date=target_date,
            quotes_back=quotes_back,
            source=source,
            archive_raw_old=False,
            complement_prohort=False,
            sync_supabase=False,
            replace_supabase=False,
            process_raw=False,
            list_categories=False,
            reset_backfill_state=False,
            validate_publication=False,
            validate_checkpoint=False,
            database_path="data/test-cotacoes.sqlite",
        )

    def test_backfill_defers_paused_source_until_recheck_date(self) -> None:
        from cotacoes_ceasa.storage.sqlite import SQLiteStorage

        with TemporaryDirectory() as temporary_dir:
            database_path = Path(temporary_dir) / "cotacoes.sqlite"
            SQLiteStorage(database_path).save_backfill_state(
                source_slug="ceasa-pe",
                status="paused_for_recheck",
                cursor_date=None,
                next_check_date=date.max,
            )
            args = self._build_args(collection_mode="backfill", quotes_back=10)
            args.database_path = str(database_path)

            effective_config = prepare_collection_mode(args, self.config)

        self.assertEqual((), tuple(effective_config.sources))
        self.assertEqual(("ceasa-pe",), args.backfill_deferred_sources)

    @staticmethod
    def _build_config() -> AppConfig:
        sources = {
            "ceasa-pe": SourceConfig(
                name="CEASA-PE",
                state="Pernambuco",
                uf="PE",
                city="Recife",
                base_url="https://example.com/pe",
                backfill_enabled=True,
            ),
            "ceasa-go": SourceConfig(
                name="CEASA-GO",
                state="Goias",
                uf="GO",
                city="Goiania",
                base_url="https://example.com/go",
            ),
            "ceasa-mg": SourceConfig(
                name="CEASA-MG",
                state="Minas Gerais",
                uf="MG",
                city="Belo Horizonte",
                base_url="https://example.com/mg",
                backfill_enabled=True,
            ),
        }

        return AppConfig(
            sources_file="config/fontes.json",
            raw_dir="data/raw",
            pdf_text_cache_dir="data/cache/pdf-text",
            database_path="data/cotacoes.sqlite",
            supabase_database_url=None,
            supabase_batch_size=5_000,
            http_timeout_seconds=30,
            request_delay_seconds=2.0,
            workers=1,
            reuse_raw_before_request=False,
            incremental_history=True,
            complement_prohort=False,
            prohort_file="config/prohort.json",
            prohort_url="https://example.com/prohort.zip",
            target_date=None,
            quotes_back=10,
            sources=sources,
        )


if __name__ == "__main__":
    unittest.main()
