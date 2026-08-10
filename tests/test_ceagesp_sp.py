import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.collectors.ceagesp_sp import CeagespSpCollector
from cotacoes_ceasa.core.errors import QuotationNotFoundError
from cotacoes_ceasa.parsers.ceagesp_sp import (
    CeagespCalendarUnavailableError,
    CeagespCategoryLayoutError,
    CeagespDiscoveryLayoutError,
    CeagespServiceUnavailableError,
    CeagespSpParser,
)
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


BASE_URL = "https://ceagesp.gov.br/cotacoes/"
VALID_DISCOVERY_HTML = """
<html>
  <body>
    <script>
      var Grupos = {
        "DIVERSOS":["05/08/2026","07/08/2026"],
        "FLORES":["05/08/2026","07/08/2026"],
        "FRUTAS":["05/08/2026","07/08/2026"],
        "LEGUMES":["05/08/2026","07/08/2026"],
        "ORG\u00c2NICOS":null,
        "PESCADOS":["05/08/2026","07/08/2026"],
        "VERDURAS":["05/08/2026","07/08/2026"]
      };
    </script>
  </body>
</html>
"""
SERVICE_UNAVAILABLE_HTML = """
<html>
  <body>
    <div>Serviço encontra-se indisponível no momento. Tente mais tarde!</div>
    <script>var Grupos = '';</script>
  </body>
</html>
"""
VALID_CATEGORY_HTML = """
<table class="contacao_lista">
  <tr>
    <td colspan="7"><b>Categoria:</b> FRUTAS <b>Data:</b> 07/08/2026</td>
  </tr>
  <tr>
    <td>Produto</td><td>Classificação</td><td>Uni/Peso</td>
    <td>Menor</td><td>Comum</td><td>Maior</td><td>Quilo</td>
  </tr>
  <tr>
    <td>ABACATE AVOCADO/HASS/FUERTE</td><td>A</td><td>KG</td>
    <td>5,36</td><td>6,47</td><td>7,55</td><td>1</td>
  </tr>
</table>
"""


class _SequencedHttpClient:
    def __init__(
        self,
        get_responses: list[str],
        post_responses: list[str] | None = None,
    ) -> None:
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses or [VALID_CATEGORY_HTML])
        self.get_calls: list[tuple[str, bool]] = []
        self.post_calls: list[
            tuple[str, dict[str, str | list[str]], bool]
        ] = []

    def get_text(
        self,
        url: str,
        encoding: str = "utf-8",
        *,
        force_refresh: bool = False,
    ) -> str:
        del encoding
        self.get_calls.append((url, force_refresh))

        if not self.get_responses:
            raise AssertionError("Resposta GET nao configurada para o teste.")

        return self.get_responses.pop(0)

    def post_form(
        self,
        url: str,
        data: dict[str, str | list[str]],
        *,
        force_refresh: bool = False,
    ) -> str:
        self.post_calls.append((url, data, force_refresh))

        if not self.post_responses:
            raise AssertionError("Resposta POST nao configurada para o teste.")

        return self.post_responses.pop(0)


class CeagespSpParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = CeagespSpParser()

    def test_parses_discovery_contract_and_selects_date(self) -> None:
        categories = self.parser.parse_categories(VALID_DISCOVERY_HTML)

        self.assertEqual(
            ("diversos", "flores", "frutas", "legumes", "pescados", "verduras"),
            tuple(category.slug for category in categories),
        )
        self.assertEqual(
            date(2026, 8, 5),
            self.parser.find_quote_date(
                VALID_DISCOVERY_HTML,
                "FRUTAS",
                date(2026, 8, 6),
            ),
        )

    def test_distinguishes_service_unavailability_from_layout_change(self) -> None:
        with self.assertRaises(CeagespServiceUnavailableError):
            self.parser.parse_categories(SERVICE_UNAVAILABLE_HTML)

        with self.assertRaises(CeagespDiscoveryLayoutError):
            self.parser.parse_categories("<html><body>pagina parcial</body></html>")

    def test_distinguishes_calendar_without_publications(self) -> None:
        pages = (
            "<script>var Grupos = {};</script>",
            '<script>var Grupos = {"ORGÂNICOS":null};</script>',
        )

        for html in pages:
            with self.subTest(html=html):
                with self.assertRaises(CeagespCalendarUnavailableError):
                    self.parser.parse_categories(html)

    def test_rejects_invalid_calendar_date(self) -> None:
        html = '<script>var Grupos = {"FRUTAS":["data-invalida"]};</script>'

        with self.assertRaises(CeagespDiscoveryLayoutError):
            self.parser.parse_categories(html)

    def test_reports_missing_publication_as_quotation_not_found(self) -> None:
        with self.assertRaises(QuotationNotFoundError):
            self.parser.find_quote_date(
                VALID_DISCOVERY_HTML,
                "FRUTAS",
                date(2026, 8, 4),
            )

    def test_parses_category_table_contract(self) -> None:
        cotacoes = self.parser.parse_category(
            VALID_CATEGORY_HTML,
            "frutas",
            BASE_URL,
        )

        self.assertEqual(1, len(cotacoes))
        cotacao = cotacoes[0]
        self.assertEqual("ABACATE AVOCADO/HASS/FUERTE", cotacao.produto)
        self.assertEqual("A", cotacao.classificacao)
        self.assertEqual("KG", cotacao.unidade)
        self.assertEqual(date(2026, 8, 7), cotacao.data_cotacao)
        self.assertEqual(Decimal("5.36"), cotacao.preco_minimo)
        self.assertEqual(Decimal("6.47"), cotacao.preco_comum)
        self.assertEqual(Decimal("7.55"), cotacao.preco_maximo)

    def test_validates_category_unavailability_and_layout(self) -> None:
        with self.assertRaises(CeagespServiceUnavailableError):
            self.parser.validate_category_response(
                SERVICE_UNAVAILABLE_HTML,
                "FRUTAS",
                date(2026, 8, 7),
            )

        with self.assertRaises(CeagespCategoryLayoutError):
            self.parser.validate_category_response(
                "<html>pagina parcial</html>",
                "FRUTAS",
                date(2026, 8, 7),
            )

    def test_rejects_category_table_without_expected_date_or_rows(self) -> None:
        without_date = VALID_CATEGORY_HTML.replace("Data:</b> 07/08/2026", "")
        without_rows = """
        <table class="contacao_lista">
          <tr><td colspan="7">Categoria: FRUTAS Data: 07/08/2026</td></tr>
          <tr>
            <td>Produto</td><td>Classificação</td><td>Uni/Peso</td>
            <td>Menor</td><td>Comum</td><td>Maior</td><td>Quilo</td>
          </tr>
        </table>
        """

        for html in (without_date, without_rows):
            with self.subTest(html=html):
                with self.assertRaises(CeagespCategoryLayoutError):
                    self.parser.validate_category_response(
                        html,
                        "FRUTAS",
                        date(2026, 8, 7),
                    )

    def test_rejects_category_table_from_another_date(self) -> None:
        with self.assertRaises(CeagespCategoryLayoutError):
            self.parser.validate_category_response(
                VALID_CATEGORY_HTML,
                "FRUTAS",
                date(2026, 8, 5),
            )

    def test_rejects_category_table_with_invalid_date(self) -> None:
        invalid_date_html = VALID_CATEGORY_HTML.replace(
            "07/08/2026",
            "99/99/9999",
        )

        with self.assertRaises(CeagespCategoryLayoutError):
            self.parser.validate_category_response(
                invalid_date_html,
                "FRUTAS",
                date(2026, 8, 7),
            )

    def test_rejects_category_table_from_another_category(self) -> None:
        with self.assertRaises(CeagespCategoryLayoutError):
            self.parser.validate_category_response(
                VALID_CATEGORY_HTML,
                "LEGUMES",
                date(2026, 8, 7),
            )


class CeagespSpCollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.raw_dir = Path(self.temporary_directory.name)

    def _build_collector(
        self,
        http_client: _SequencedHttpClient,
    ) -> CeagespSpCollector:
        return CeagespSpCollector(
            http_client=http_client,
            raw_storage=RawHtmlStorage(self.raw_dir),
            parser=CeagespSpParser(),
            base_url=BASE_URL,
        )

    def test_retries_invalid_discovery_without_reusing_http_cache(self) -> None:
        http_client = _SequencedHttpClient(
            [SERVICE_UNAVAILABLE_HTML, VALID_DISCOVERY_HTML]
        )
        collector = self._build_collector(http_client)

        categories = collector.discover_categories()

        self.assertEqual(6, len(categories))
        self.assertEqual(
            [(BASE_URL, False), (BASE_URL, True)],
            http_client.get_calls,
        )
        diagnostic_files = list(
            (self.raw_dir / "ceagesp-sp-diagnostics").glob("*.html")
        )
        self.assertEqual(2, len(diagnostic_files))
        self.assertEqual(
            {"discovery-service-unavailable", "discovery-success"},
            {file_path.name.split("_", 1)[0] for file_path in diagnostic_files},
        )
        self.assertFalse((self.raw_dir / "ceagesp-sp").exists())

    def test_keeps_failure_visible_after_three_invalid_responses(self) -> None:
        http_client = _SequencedHttpClient([SERVICE_UNAVAILABLE_HTML] * 3)
        collector = self._build_collector(http_client)

        with self.assertRaisesRegex(
            CeagespServiceUnavailableError,
            "3 tentativa",
        ):
            collector.discover_categories()

        self.assertEqual(
            [(BASE_URL, False), (BASE_URL, True), (BASE_URL, True)],
            http_client.get_calls,
        )
        self.assertEqual(
            1,
            len(list((self.raw_dir / "ceagesp-sp-diagnostics").glob("*.html"))),
        )
        self.assertFalse((self.raw_dir / "ceagesp-sp").exists())

    def test_downloads_category_with_expected_post_payload(self) -> None:
        http_client = _SequencedHttpClient([VALID_DISCOVERY_HTML])
        collector = self._build_collector(http_client)

        raw_path = collector.download_category("frutas", date(2026, 8, 7))

        self.assertEqual(
            [
                (
                    BASE_URL,
                    {"cot_grupo": "FRUTAS", "cot_data": "07/08/2026"},
                    False,
                )
            ],
            http_client.post_calls,
        )
        self.assertEqual(VALID_CATEGORY_HTML, raw_path.read_text(encoding="utf-8"))
        self.assertIn("frutas_2026-08-07_", raw_path.name)

    def test_retries_invalid_category_response_without_reusing_cache(self) -> None:
        http_client = _SequencedHttpClient(
            [VALID_DISCOVERY_HTML],
            [SERVICE_UNAVAILABLE_HTML, VALID_CATEGORY_HTML],
        )
        collector = self._build_collector(http_client)

        raw_path = collector.download_category("frutas", date(2026, 8, 7))

        self.assertTrue(raw_path.exists())
        self.assertEqual(
            [False, True],
            [force_refresh for _, _, force_refresh in http_client.post_calls],
        )
        diagnostic_names = [
            file_path.name
            for file_path in (
                self.raw_dir / "ceagesp-sp-diagnostics"
            ).glob("*.html")
        ]
        self.assertTrue(
            any(name.startswith("discovery-success_") for name in diagnostic_names)
        )
        self.assertTrue(
            any(
                name.startswith("frutas_2026-08-07-service-unavailable_")
                for name in diagnostic_names
            )
        )

    def test_keeps_category_failure_visible_after_three_responses(self) -> None:
        http_client = _SequencedHttpClient(
            [VALID_DISCOVERY_HTML],
            [SERVICE_UNAVAILABLE_HTML] * 3,
        )
        collector = self._build_collector(http_client)

        with self.assertRaisesRegex(
            CeagespServiceUnavailableError,
            "3 tentativa",
        ):
            collector.download_category("frutas", date(2026, 8, 7))

        self.assertEqual(
            [False, True, True],
            [force_refresh for _, _, force_refresh in http_client.post_calls],
        )
        self.assertFalse((self.raw_dir / "ceagesp-sp").exists())


if __name__ == "__main__":
    unittest.main()
