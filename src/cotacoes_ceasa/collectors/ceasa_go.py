from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_go import CeasaGoParser, CeasaGoQuoteLink
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_GO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaGoCollector:
    """Coleta PDFs diarios da CEASA-GO."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaGoParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True

    def download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        quote_link = self._find_quote_link(self._resolve_target_date(cotacao_date))
        storage_category = self._build_storage_category(
            category_slug,
            quote_link.quote_date,
        )
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        pdf_content = self.http_client.get_bytes(quote_link.url)

        return self.raw_storage.save_bytes(
            "ceasa-go",
            storage_category,
            pdf_content,
            "pdf",
        )

    def collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        quote_link = self._find_quote_link(self._resolve_target_date(cotacao_date))
        storage_category = self._build_storage_category(
            category_slug,
            quote_link.quote_date,
        )
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            pdf_content = self.http_client.get_bytes(quote_link.url)

            if save_raw:
                self.raw_storage.save_bytes(
                    "ceasa-go",
                    storage_category,
                    pdf_content,
                    "pdf",
                )
        else:
            pdf_content = raw_file.read_bytes()

        return self.parser.parse_category(pdf_content, category_slug, quote_link.url)

    def discover_categories(self) -> tuple[Category, ...]:
        html = self.http_client.get_text(self.base_url)
        categories = self.parser.parse_categories(html, self.base_url)

        if not categories:
            raise ValueError("Nenhuma categoria da CEASA-GO foi encontrada.")

        return categories

    def _find_quote_link(self, target_date: date) -> CeasaGoQuoteLink:
        year_url = self._build_year_url(target_date)
        year_html = self.http_client.get_text(year_url)
        month_links = [
            month_link
            for month_link in self.parser.parse_month_links(
                year_html,
                year_url,
                target_date.year,
            )
            if month_link.month <= target_date.month
        ]

        for month_link in sorted(
            month_links,
            key=lambda item: item.month,
            reverse=True,
        ):
            month_html = self.http_client.get_text(month_link.url)
            quote_links = self.parser.parse_quote_links(month_html, month_link.url)
            candidates = [
                quote_link
                for quote_link in quote_links
                if quote_link.quote_date <= target_date
            ]

            if candidates:
                return max(candidates, key=lambda quote_link: quote_link.quote_date)

        raise ValueError(
            "PDF da CEASA-GO nao encontrado ate "
            f"{target_date.isoformat()}."
        )

    def _build_year_url(self, target_date: date) -> str:
        base_url = self.base_url.rstrip("/")

        if base_url.endswith(str(target_date.year)):
            return f"{base_url}/"

        if base_url.endswith("cotacoes-diarias"):
            return f"{base_url}-{target_date.year}/"

        return f"{base_url}/{target_date.year}/"

    def _build_storage_category(self, category_slug: str, quote_date: date) -> str:
        return f"{category_slug}_{quote_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest_file("ceasa-go", storage_category, "pdf")

    def _resolve_target_date(self, cotacao_date: date | None) -> date:
        return cotacao_date or date.today()
