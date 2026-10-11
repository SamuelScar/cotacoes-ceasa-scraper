import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cotacoes_ceasa.core.errors import QuotationNotFoundError
from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.storage.sqlite import SQLiteStorage
from cotacoes_ceasa.workflows.backfill import (
    BACKFILL_RECHECK_DAYS,
    BackfillBaseline,
    finalize_backfill_state,
    find_deferred_backfill_states,
)
from cotacoes_ceasa.workflows.collection import resolve_quotation_dates


class BackfillStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_dir = TemporaryDirectory()
        self.database_path = Path(self.temporary_dir.name) / "cotacoes.sqlite"

    def tearDown(self) -> None:
        self.temporary_dir.cleanup()

    def test_progress_advances_cursor_and_keeps_source_runnable(self) -> None:
        baseline = BackfillBaseline("ceasa-pe", date(2018, 2, 19))

        with patch.object(
            SQLiteStorage,
            "find_oldest_cotacao_date",
            return_value=date(2018, 1, 30),
        ):
            state = finalize_backfill_state(
                database_path=self.database_path,
                baseline=baseline,
                download_status="completed",
                persistence_status="completed",
                reference_date=date(2026, 8, 10),
            )

        self.assertEqual("complete", state.status)
        self.assertEqual(date(2018, 1, 30), state.cursor_date)
        self.assertFalse(state.is_deferred(date(2026, 8, 10)))

    def test_no_progress_pauses_source_for_thirty_days(self) -> None:
        baseline = BackfillBaseline("ceasa-pe", date(2018, 1, 30))

        with patch.object(
            SQLiteStorage,
            "find_oldest_cotacao_date",
            return_value=baseline.oldest_date,
        ):
            state = finalize_backfill_state(
                database_path=self.database_path,
                baseline=baseline,
                download_status="completed",
                persistence_status="completed",
                reference_date=date(2026, 8, 10),
            )

        self.assertEqual("paused_for_recheck", state.status)
        self.assertEqual(1, state.consecutive_no_progress)
        self.assertEqual(
            date(2026, 9, 9),
            state.next_check_date,
        )
        self.assertEqual(
            {"ceasa-pe"},
            set(
                find_deferred_backfill_states(
                    self.database_path,
                    ("ceasa-pe",),
                    reference_date=date(2026, 8, 11),
                )
            ),
        )
        self.assertEqual(30, BACKFILL_RECHECK_DAYS)

    def test_real_failure_remains_available_for_retry(self) -> None:
        baseline = BackfillBaseline("ceasa-pe", date(2018, 1, 30))

        with patch.object(
            SQLiteStorage,
            "find_oldest_cotacao_date",
            return_value=baseline.oldest_date,
        ):
            state = finalize_backfill_state(
                database_path=self.database_path,
                baseline=baseline,
                download_status="failed",
                persistence_status="skipped",
                error="HttpRequestError: timeout",
                reference_date=date(2026, 8, 10),
            )

        self.assertEqual("failed", state.status)
        self.assertEqual("HttpRequestError: timeout", state.last_error)
        self.assertFalse(state.is_deferred(date(2026, 8, 11)))

    def test_reset_reopens_all_scopes_of_source(self) -> None:
        storage = SQLiteStorage(self.database_path)
        storage.save_backfill_state(
            source_slug="ceasa-pe",
            category_slug=None,
            status="exhausted",
            cursor_date=date(2018, 1, 1),
        )
        storage.save_backfill_state(
            source_slug="ceasa-pe",
            category_slug="frutas",
            status="paused_for_recheck",
            cursor_date=date(2018, 1, 2),
            next_check_date=date.max,
        )

        removed_count = storage.reset_backfill_state("ceasa-pe")

        self.assertEqual(2, removed_count)
        self.assertIsNone(storage.find_backfill_state("ceasa-pe"))
        self.assertIsNone(storage.find_backfill_state("ceasa-pe", "frutas"))


class QuotationDateResolutionTest(unittest.TestCase):
    def test_date_after_candidate_is_not_accepted_as_progress(self) -> None:
        collector = _FixedResultCollector(date(2025, 3, 6))

        dates = resolve_quotation_dates(
            collector=collector,
            probe_category_slug="cotacao-diaria",
            target_date=date(2025, 3, 4),
            quotes_back=1,
            allow_empty_history=True,
            strict_history_errors=True,
        )

        self.assertEqual([], dates)

    def test_generic_parser_error_is_not_treated_as_exhausted_history(self) -> None:
        collector = _ErrorCollector(ValueError("layout inesperado"))

        with self.assertRaisesRegex(ValueError, "layout inesperado"):
            resolve_quotation_dates(
                collector=collector,
                probe_category_slug="cotacao-diaria",
                target_date=date(2025, 3, 4),
                quotes_back=1,
                allow_empty_history=True,
                strict_history_errors=True,
            )

    def test_known_missing_publication_can_finish_without_progress(self) -> None:
        collector = _ErrorCollector(QuotationNotFoundError("sem publicacao"))

        dates = resolve_quotation_dates(
            collector=collector,
            probe_category_slug="cotacao-diaria",
            target_date=date(2025, 3, 4),
            quotes_back=1,
            allow_empty_history=True,
            strict_history_errors=True,
        )

        self.assertEqual([], dates)


class _FixedResultCollector:
    supports_target_dates = True

    def __init__(self, quotation_date: date) -> None:
        self.quotation_date = quotation_date

    def collect_category(self, category_slug, cotacao_date, save_raw=True):
        return [_build_cotacao(self.quotation_date)]


class _ErrorCollector:
    supports_target_dates = True

    def __init__(self, error: Exception) -> None:
        self.error = error

    def collect_category(self, category_slug, cotacao_date, save_raw=True):
        raise self.error


def _build_cotacao(quotation_date: date) -> Cotacao:
    return Cotacao(
        fonte="CEASA-GO",
        categoria="cotacao-diaria",
        produto="Produto",
        unidade="kg",
        procedencia=None,
        classificacao=None,
        data_cotacao=quotation_date,
        preco_minimo=Decimal("1.00"),
        preco_comum=None,
        preco_maximo=None,
        situacao_mercado=None,
        url_origem="https://example.com/cotacao.pdf",
    )


if __name__ == "__main__":
    unittest.main()
