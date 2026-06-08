from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_ce import CeasaCeParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_CE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaCeCollector:
    """Coleta boletins atuais da CEASA-CE."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaCeParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = False

    def download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        quote_link = self._find_quote_link(category_slug)
        storage_category = self._build_storage_category(category_slug)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        pdf_content = self.http_client.get_bytes(quote_link.url)

        return self.raw_storage.save_bytes(
            "ceasa-ce",
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
        quote_link = self._find_quote_link(category_slug)
        storage_category = self._build_storage_category(category_slug)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            pdf_content = self.http_client.get_bytes(quote_link.url)

            if save_raw:
                self.raw_storage.save_bytes(
                    "ceasa-ce",
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
            raise ValueError("Nenhum boletim da CEASA-CE foi encontrado.")

        return categories

    def _find_quote_link(self, category_slug: str):
        html = self.http_client.get_text(self.base_url)

        return self.parser.find_quote_link(html, self.base_url, category_slug)

    def _build_storage_category(self, category_slug: str) -> str:
        return category_slug

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest_file("ceasa-ce", storage_category, "pdf")
