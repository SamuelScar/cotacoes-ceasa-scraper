from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_rj import CeasaRjParser, CeasaRjQuoteLink
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_RJ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaRjCollector:
    """Coleta PDFs diarios da CEASA-RJ."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaRjParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True

    def _download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        quote_link = self._find_quote_link(cotacao_date or date.today())
        storage_category = self._build_storage_category(
            category_slug,
            quote_link.quote_date,
        )
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        pdf_content = self.http_client.get_bytes(quote_link.url)

        return self.raw_storage.save_bytes(
            "ceasa-rj", storage_category, pdf_content, "pdf"
        )

    def _collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        quote_link = self._find_quote_link(cotacao_date or date.today())
        storage_category = self._build_storage_category(
            category_slug,
            quote_link.quote_date,
        )
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            pdf_content = self.http_client.get_bytes(quote_link.url)

            if save_raw:
                self.raw_storage.save_bytes(
                    "ceasa-rj", storage_category, pdf_content, "pdf"
                )
        else:
            pdf_content = raw_file.read_bytes()

        return self.parser.parse_category(pdf_content, category_slug, quote_link.url)

    def discover_categories(self) -> tuple[Category, ...]:
        return (Category(slug="cotacao-diaria", name="Cotacao diaria"),)

    def _find_quote_link(self, target_date: date) -> CeasaRjQuoteLink:
        root_html = self.http_client.get_text(self.base_url)
        year_url = self.parser.find_year_url(root_html, self.base_url, target_date.year)
        year_html = self.http_client.get_text(year_url)
        month_links = [
            item
            for item in self.parser.parse_month_links(year_html, year_url)
            if item.month <= target_date.month
        ]

        for month_link in sorted(
            month_links,
            key=lambda item: item.month,
            reverse=True,
        ):
            month_html = self.http_client.get_text(month_link.url)
            candidates = [
                item
                for item in self.parser.parse_quote_links(
                    month_html, month_link.url, target_date.year, month_link.month
                )
                if item.quote_date <= target_date
            ]

            if candidates:
                return max(candidates, key=lambda item: item.quote_date)

        raise ValueError(
            f"PDF da CEASA-RJ nao encontrado ate {target_date.isoformat()}."
        )

    def _build_storage_category(self, category_slug: str, quote_date: date) -> str:
        return f"{category_slug}_{quote_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest_file("ceasa-rj", storage_category, "pdf")
