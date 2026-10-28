import io
import sqlite3
import ssl
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from cotacoes_ceasa.cli.commands.maintenance import run_prohort_command
from cotacoes_ceasa.cli.main import run_automatic_prohort
from cotacoes_ceasa.workflows.prohort import (
    ProhortComplementResult,
    ProhortComplementer,
    ProhortUnavailableError,
)


PROHORT_URL = "https://example.com/ProhortDiario.txt"
VALID_PROHORT_CONTENT = (
    b"dsc_ceasa;dsc_produto;sig_unidade_medida;data_preco;preco_diario\n"
    b"CEASA TESTE;BATATA;KG;2026/10/28 00:00:00.000;10.50\n"
)


class ProhortAvailabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.complementer = ProhortComplementer(
            database_path=Path("cotacoes.sqlite"),
            prohort_url=PROHORT_URL,
            timeout_seconds=30,
        )

    def test_downloads_prohort_content_successfully(self) -> None:
        with patch(
            "cotacoes_ceasa.workflows.prohort.urlopen",
            return_value=io.BytesIO(VALID_PROHORT_CONTENT),
        ):
            content = self.complementer._download_prohort_content()

        self.assertEqual(VALID_PROHORT_CONTENT, content)

    def test_classifies_timeout_as_unavailable(self) -> None:
        error = self._capture_unavailable(TimeoutError("timed out"))

        self.assertEqual("TimeoutError", error.error_type)
        self.assertEqual("timed out", error.error_message)

    def test_classifies_http_error_as_unavailable(self) -> None:
        http_error = HTTPError(
            PROHORT_URL,
            503,
            "Service Unavailable",
            hdrs=None,
            fp=None,
        )

        error = self._capture_unavailable(http_error)

        self.assertEqual("HTTPError", error.error_type)
        self.assertIn("503", error.error_message)

    def test_classifies_certificate_error_as_unavailable(self) -> None:
        certificate_error = ssl.SSLCertVerificationError(
            1,
            "hostname mismatch",
        )

        error = self._capture_unavailable(URLError(certificate_error))

        self.assertEqual("SSLCertVerificationError", error.error_type)
        self.assertIn("hostname mismatch", error.error_message)

    def test_classifies_connection_error_as_unavailable(self) -> None:
        error = self._capture_unavailable(
            ConnectionError("connection refused")
        )

        self.assertEqual("ConnectionError", error.error_type)
        self.assertEqual("connection refused", error.error_message)

    def test_accepts_expected_prohort_columns(self) -> None:
        rows = list(self.complementer._read_prohort_rows(VALID_PROHORT_CONTENT))

        self.assertEqual(1, len(rows))
        self.assertEqual("BATATA", rows[0]["dsc_produto"])

    def test_rejects_unexpected_content_as_processing_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "colunas obrigatorias"):
            self.complementer._read_prohort_rows(b"<html>indisponivel</html>")

    def _capture_unavailable(self, cause: Exception) -> ProhortUnavailableError:
        with patch(
            "cotacoes_ceasa.workflows.prohort.urlopen",
            side_effect=cause,
        ):
            with self.assertRaises(ProhortUnavailableError) as context:
                self.complementer._download_prohort_content()

        return context.exception


class AutomaticProhortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.args = SimpleNamespace(
            save=True,
            process_raw=False,
            download_and_process=False,
            database_path="data/cotacoes.sqlite",
            http_timeout_seconds=30,
            raw_dir="data/raw",
        )
        self.config = SimpleNamespace(
            complement_prohort=True,
            prohort_file="config/prohort.json",
            prohort_url=PROHORT_URL,
        )
        self.output = Mock()

    def test_records_successful_automatic_complement(self) -> None:
        result = ProhortComplementResult(
            database_found=True,
            updated_count=2,
            inserted_count=1,
        )

        with patch(
            "cotacoes_ceasa.cli.main.run_prohort_command",
            return_value=result,
        ):
            observation = run_automatic_prohort(
                self.args,
                self.config,
                self.output,
            )

        self.assertIsNotNone(observation)
        self.assertEqual("completed", observation.status)
        self.assertTrue(observation.applied)
        self.assertEqual(2, observation.updated_count)
        self.assertEqual(1, observation.inserted_count)

    def test_keeps_main_run_after_external_unavailability(self) -> None:
        unavailable = ProhortUnavailableError(
            PROHORT_URL,
            TimeoutError("timed out"),
        )

        with patch(
            "cotacoes_ceasa.cli.main.run_prohort_command",
            side_effect=unavailable,
        ):
            observation = run_automatic_prohort(
                self.args,
                self.config,
                self.output,
            )

        self.assertIsNotNone(observation)
        self.assertEqual("unavailable", observation.status)
        self.assertFalse(observation.applied)
        self.assertEqual("TimeoutError", observation.error_type)
        self.output.warning.assert_called_once()

    def test_does_not_suppress_parsing_error(self) -> None:
        with patch(
            "cotacoes_ceasa.cli.main.run_prohort_command",
            side_effect=ValueError("invalid content"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid content"):
                run_automatic_prohort(self.args, self.config, self.output)

    def test_does_not_suppress_persistence_error(self) -> None:
        with patch(
            "cotacoes_ceasa.cli.main.run_prohort_command",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "database is locked",
            ):
                run_automatic_prohort(self.args, self.config, self.output)

    def test_manual_command_propagates_external_unavailability(self) -> None:
        unavailable = ProhortUnavailableError(
            PROHORT_URL,
            TimeoutError("timed out"),
        )

        with patch(
            "cotacoes_ceasa.cli.commands.maintenance.ProhortComplementer.complement",
            side_effect=unavailable,
        ):
            with self.assertRaises(ProhortUnavailableError):
                run_prohort_command(self.args, self.config, self.output)


if __name__ == "__main__":
    unittest.main()
