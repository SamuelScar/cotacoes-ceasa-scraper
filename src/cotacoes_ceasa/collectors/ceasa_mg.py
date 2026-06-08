from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_mg import PRICE_CATEGORY, CeasaMgParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


@dataclass(frozen=True)
class CeasaMgCollector:
    """Coleta a ultima cotacao de preco mais comum da CEASA-MG."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaMgParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = False

    def download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        raw_file = self._find_reusable_raw()

        if raw_file is not None:
            return raw_file

        html = self.http_client.get_text(self.base_url)

        return self.raw_storage.save("ceasa-mg", PRICE_CATEGORY.slug, html)

    def collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        raw_file = self._find_reusable_raw()

        if raw_file is None:
            html = self.http_client.get_text(self.base_url)

            if save_raw:
                self.raw_storage.save("ceasa-mg", PRICE_CATEGORY.slug, html)
        else:
            html = raw_file.read_text(encoding="utf-8")

        return self.parser.parse_category(html, PRICE_CATEGORY.slug, self.base_url)

    def discover_categories(self) -> tuple[Category, ...]:
        return (PRICE_CATEGORY,)

    def _find_reusable_raw(self) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest("ceasa-mg", PRICE_CATEGORY.slug)
