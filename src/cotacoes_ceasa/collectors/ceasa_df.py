from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_df import PRICE_CATEGORY, CeasaDfParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_DF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaDfCollector:
    """Coleta o boletim SIMA atual da CEASA-DF."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaDfParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = False

    def _download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        quote_url = self._find_quote_url()
        raw_file = self._find_reusable_raw()

        if raw_file is not None:
            return raw_file

        pdf_content = self.http_client.get_bytes(quote_url)

        return self.raw_storage.save_bytes(
            "ceasa-df",
            PRICE_CATEGORY.slug,
            pdf_content,
            "pdf",
        )

    def _collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        quote_url = self._find_quote_url()
        raw_file = self._find_reusable_raw()

        if raw_file is None:
            pdf_content = self.http_client.get_bytes(quote_url)

            if save_raw:
                self.raw_storage.save_bytes(
                    "ceasa-df",
                    PRICE_CATEGORY.slug,
                    pdf_content,
                    "pdf",
                )
        else:
            pdf_content = raw_file.read_bytes()

        return self.parser.parse_category(pdf_content, PRICE_CATEGORY.slug, quote_url)

    def discover_categories(self) -> tuple[Category, ...]:
        return (PRICE_CATEGORY,)

    def _find_quote_url(self) -> str:
        html = self.http_client.get_text(self.base_url)

        return self.parser.find_sima_url(html, self.base_url)

    def _find_reusable_raw(self) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest_file(
            "ceasa-df",
            PRICE_CATEGORY.slug,
            "pdf",
        )
