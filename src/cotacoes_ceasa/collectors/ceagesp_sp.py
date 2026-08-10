from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NoReturn

from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.parsers.ceagesp_sp import (
    CeagespCalendarUnavailableError,
    CeagespDiscoveryError,
    CeagespResponseError,
    CeagespServiceUnavailableError,
    CeagespSpParser,
)
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
CEAGESP_SP_RESPONSE_ATTEMPTS = 3
CEAGESP_SP_DIAGNOSTIC_RAW_SOURCE = "ceagesp-sp-diagnostics"
CEAGESP_SP_DISCOVERY_SUCCESS_CATEGORY = "discovery-success"


@dataclass(frozen=True)
class CeagespSpCollector:
    """Coleta cotacoes da capital publicadas pela CEAGESP-SP."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeagespSpParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True
    response_attempts: int = CEAGESP_SP_RESPONSE_ATTEMPTS

    def __post_init__(self) -> None:
        if self.response_attempts < 1:
            raise ValueError("response_attempts deve ser maior que zero.")

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

        html = self._request_category(category_slug, category.name, quote_date)

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
            html = self._request_category(category_slug, category.name, quote_date)

            if save_raw:
                self.raw_storage.save("ceagesp-sp", storage_category, html)
        else:
            html = raw_file.read_text(encoding="utf-8")

        return self.parser.parse_category(html, category_slug, self.base_url)

    def discover_categories(self) -> tuple[Category, ...]:
        html = self._load_discovery_html()

        return self.parser.parse_categories(html)

    def _resolve_category_date(
        self,
        category_slug: str,
        target_date: date | None,
    ) -> tuple[Category, date]:
        html = self._load_discovery_html()
        category = self.parser.find_category(html, category_slug)
        quote_date = self.parser.find_quote_date(html, category.name, target_date)

        return category, quote_date

    def _request_category(
        self,
        category_slug: str,
        category_name: str,
        quote_date: date,
    ) -> str:
        payload = {
            "cot_grupo": category_name,
            "cot_data": quote_date.strftime("%d/%m/%Y"),
        }
        storage_category = self._build_storage_category(category_slug, quote_date)
        last_error: CeagespResponseError | None = None
        diagnostic_path: Path | None = None

        for attempt in range(self.response_attempts):
            html = self.http_client.post_form(
                self.base_url,
                payload,
                force_refresh=attempt > 0,
            )

            try:
                self.parser.validate_category_response(
                    html,
                    category_name,
                    quote_date,
                )
            except CeagespResponseError as error:
                diagnostic_path = self.raw_storage.save(
                    CEAGESP_SP_DIAGNOSTIC_RAW_SOURCE,
                    f"{storage_category}-{self._diagnostic_reason(error)}",
                    html,
                )
                last_error = error
                continue

            return html

        self._raise_persistent_response_error(last_error, diagnostic_path)

    def _build_storage_category(self, category_slug: str, quote_date: date) -> str:
        return f"{category_slug}_{quote_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest("ceagesp-sp", storage_category)

    def _load_discovery_html(self) -> str:
        last_error: CeagespResponseError | None = None
        diagnostic_path: Path | None = None

        for attempt in range(self.response_attempts):
            html = self.http_client.get_text(
                self.base_url,
                force_refresh=attempt > 0,
            )

            try:
                self.parser.parse_categories(html)
            except (CeagespDiscoveryError, CeagespServiceUnavailableError) as error:
                diagnostic_path = self.raw_storage.save(
                    CEAGESP_SP_DIAGNOSTIC_RAW_SOURCE,
                    f"discovery-{self._diagnostic_reason(error)}",
                    html,
                )
                last_error = error
                continue

            self.raw_storage.save(
                CEAGESP_SP_DIAGNOSTIC_RAW_SOURCE,
                CEAGESP_SP_DISCOVERY_SUCCESS_CATEGORY,
                html,
            )

            return html

        self._raise_persistent_response_error(last_error, diagnostic_path)

    def _raise_persistent_response_error(
        self,
        error: CeagespResponseError | None,
        diagnostic_path: Path | None,
    ) -> NoReturn:
        if error is None or diagnostic_path is None:
            raise RuntimeError("Falha inesperada ao consultar a CEAGESP-SP.")

        raise type(error)(
            f"{error} Falha persistiu apos {self.response_attempts} "
            f"tentativa(s). Ultima resposta salva em {diagnostic_path}."
        ) from error

    def _diagnostic_reason(
        self,
        error: CeagespResponseError,
    ) -> str:
        if isinstance(error, CeagespServiceUnavailableError):
            return "service-unavailable"

        if isinstance(error, CeagespCalendarUnavailableError):
            return "calendar-empty"

        return "invalid-layout"
