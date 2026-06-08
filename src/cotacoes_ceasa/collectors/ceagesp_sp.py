from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceagesp_sp import CeagespSpParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEAGESP_SP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeagespSpCollector:
    """Coleta cotacoes da capital publicadas pela CEAGESP-SP."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeagespSpParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True

    def download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        category, quote_date = self._resolve_category_date(
            category_slug,
            cotacao_date,
        )
        storage_category = self._build_storage_category(category_slug, quote_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        html = self._request_category(category.name, quote_date)

        return self.raw_storage.save("ceagesp-sp", storage_category, html)

    def collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        category, quote_date = self._resolve_category_date(
            category_slug,
            cotacao_date,
        )
        storage_category = self._build_storage_category(category_slug, quote_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            html = self._request_category(category.name, quote_date)

            if save_raw:
                self.raw_storage.save("ceagesp-sp", storage_category, html)
        else:
            html = raw_file.read_text(encoding="utf-8")

        return self.parser.parse_category(html, category_slug, self.base_url)

    def discover_categories(self) -> tuple[Category, ...]:
        html = self.http_client.get_text(self.base_url)
        categories = self.parser.parse_categories(html)

        if not categories:
            raise ValueError("Nenhuma categoria da CEAGESP-SP foi encontrada.")

        return categories

    def _resolve_category_date(
        self,
        category_slug: str,
        target_date: date | None,
    ) -> tuple[Category, date]:
        html = self.http_client.get_text(self.base_url)
        category = self.parser.find_category(html, category_slug)
        quote_date = self.parser.find_quote_date(html, category.name, target_date)

        return category, quote_date

    def _request_category(self, category_name: str, quote_date: date) -> str:
        return self.http_client.post_form(
            self.base_url,
            {
                "cot_grupo": category_name,
                "cot_data": quote_date.strftime("%d/%m/%Y"),
            },
        )

    def _build_storage_category(self, category_slug: str, quote_date: date) -> str:
        return f"{category_slug}_{quote_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest("ceagesp-sp", storage_category)
