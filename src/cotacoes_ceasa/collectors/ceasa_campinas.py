from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_campinas import (
    CampinasQuoteLink,
    CeasaCampinasParser,
)
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_CAMPINAS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaCampinasCollector:
    """Coleta PDFs de cotacao da CEASA Campinas."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaCampinasParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True

    def _download_category(
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
            "ceasa-campinas",
            storage_category,
            pdf_content,
            "pdf",
        )

    def _collect_category(
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
                    "ceasa-campinas",
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
            raise ValueError("Nenhuma categoria da CEASA Campinas foi encontrada.")

        return categories

    def _find_quote_link(self, target_date: date) -> CampinasQuoteLink:
        pending_urls = deque([self.base_url])
        visited_urls: set[str] = set()

        while pending_urls:
            page_url = pending_urls.popleft()

            if page_url in visited_urls:
                continue

            visited_urls.add(page_url)
            html = self.http_client.get_text(page_url)
            quote_links = self.parser.parse_quote_links(html, page_url)
            candidate = self._find_best_candidate(quote_links, target_date)

            if candidate is not None:
                return candidate

            for pagination_url in self.parser.parse_pagination_urls(html, page_url):
                if pagination_url not in visited_urls:
                    pending_urls.append(pagination_url)

        raise ValueError(
            "PDF da CEASA Campinas nao encontrado ate "
            f"{target_date.isoformat()}."
        )

    def _find_best_candidate(
        self,
        quote_links: list[CampinasQuoteLink],
        target_date: date,
    ) -> CampinasQuoteLink | None:
        candidates = [
            quote_link
            for quote_link in quote_links
            if quote_link.quote_date <= target_date
        ]

        if not candidates:
            return None

        return max(candidates, key=lambda quote_link: quote_link.quote_date)

    def _build_storage_category(self, category_slug: str, quote_date: date) -> str:
        return f"{category_slug}_{quote_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest_file(
            "ceasa-campinas",
            storage_category,
            "pdf",
        )

    def _resolve_target_date(self, cotacao_date: date | None) -> date:
        return cotacao_date or date.today()
