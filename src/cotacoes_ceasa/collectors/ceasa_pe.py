from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_pe import CeasaPeParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


@dataclass(frozen=True)
class CeasaPeCollector:
    """Coleta paginas de cotacao da CEASA-PE e extrai registros da tabela."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaPeParser
    base_url: str
    reuse_raw_before_request: bool = False

    def _download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        """Baixa uma categoria da CEASA-PE e salva o HTML bruto em disco."""
        storage_category = self._build_storage_category(category_slug, cotacao_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        url = self._build_category_url(category_slug, cotacao_date)
        html = self.http_client.get_text(url)

        return self.raw_storage.save("ceasa-pe", storage_category, html)

    def _collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        """Baixa uma categoria da CEASA-PE e retorna cotacoes normalizadas."""
        url = self._build_category_url(category_slug, cotacao_date)
        storage_category = self._build_storage_category(category_slug, cotacao_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            html = self.http_client.get_text(url)

            if save_raw:
                self.raw_storage.save("ceasa-pe", storage_category, html)
        else:
            html = raw_file.read_text(encoding="utf-8")

        return self.parser.parse_category(html, category_slug, url)

    def discover_categories(self) -> tuple[Category, ...]:
        """Baixa a pagina base da CEASA-PE e descobre categorias disponiveis."""
        html = self.http_client.get_text(self.base_url)
        categories = self.parser.parse_categories(html, self.base_url)

        if not categories:
            raise ValueError("Nenhuma categoria da CEASA-PE foi encontrada.")

        return categories

    def _build_category_url(
        self,
        category_slug: str,
        cotacao_date: date | None,
    ) -> str:
        url = f"{self.base_url.rstrip('/')}/{category_slug}"

        if cotacao_date is None:
            return url

        params = urlencode({"data": cotacao_date.strftime("%d/%m/%Y")})

        return f"{url}?{params}"

    def _build_storage_category(
        self,
        category_slug: str,
        cotacao_date: date | None,
    ) -> str:
        if cotacao_date is None:
            return category_slug

        return f"{category_slug}_{cotacao_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest("ceasa-pe", storage_category)
