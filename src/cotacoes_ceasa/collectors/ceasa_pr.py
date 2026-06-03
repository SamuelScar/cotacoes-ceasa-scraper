from dataclasses import dataclass
from datetime import date
from pathlib import Path

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_pr import CeasaPrParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_PR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaPrCollector:
    """Coleta PDFs diarios da CEASA-PR e extrai registros da tabela."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaPrParser
    base_url: str
    target_date: date | None = None
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True

    def _download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        latest_when_missing = cotacao_date is None
        target_date = self._resolve_target_date(cotacao_date)
        storage_category = self._build_storage_category(category_slug, target_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        pdf_url = self._find_pdf_url(category_slug, target_date, latest_when_missing)
        pdf_content = self.http_client.get_bytes(pdf_url)

        return self.raw_storage.save_bytes(
            "ceasa-pr",
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
        latest_when_missing = cotacao_date is None
        target_date = self._resolve_target_date(cotacao_date)
        storage_category = self._build_storage_category(category_slug, target_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            pdf_url = self._find_pdf_url(category_slug, target_date, latest_when_missing)
            pdf_content = self.http_client.get_bytes(pdf_url)

            if save_raw:
                self.raw_storage.save_bytes(
                    "ceasa-pr",
                    storage_category,
                    pdf_content,
                    "pdf",
                )
        else:
            pdf_url = self._build_year_url(target_date)
            pdf_content = raw_file.read_bytes()

        return self.parser.parse_category(pdf_content, category_slug, pdf_url)

    def discover_categories(self) -> tuple[Category, ...]:
        target_date = self._resolve_target_date(self.target_date)
        year_url = self._build_year_url(target_date)
        html = self.http_client.get_text(year_url)
        categories = self.parser.parse_categories(html, year_url)

        if not categories:
            raise ValueError("Nenhuma cidade da CEASA-PR foi encontrada.")

        return categories

    def _find_pdf_url(
        self,
        category_slug: str,
        target_date: date,
        latest_when_missing: bool = False,
    ) -> str:
        year_url = self._build_year_url(target_date)
        html = self.http_client.get_text(year_url)

        return self.parser.find_pdf_url(
            html,
            year_url,
            category_slug,
            target_date,
            latest_when_missing=latest_when_missing,
        )

    def _build_year_url(self, target_date: date) -> str:
        if target_date.year < 2022:
            raise ValueError("CEASA-PR unificada por ano so esta disponivel a partir de 2022.")

        base_url = self.base_url.rstrip("/")

        if base_url.endswith(str(target_date.year)):
            return base_url

        return f"{base_url}-{target_date.year}"

    def _build_storage_category(self, category_slug: str, target_date: date) -> str:
        return f"{category_slug}_{target_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest_file("ceasa-pr", storage_category, "pdf")

    def _resolve_target_date(self, cotacao_date: date | None) -> date:
        return cotacao_date or date.today()
