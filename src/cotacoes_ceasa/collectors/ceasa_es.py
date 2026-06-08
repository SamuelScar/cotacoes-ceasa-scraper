from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.parsers.ceasa_es import CeasaEsFormState, CeasaEsParser
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


CEASA_ES_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


@dataclass(frozen=True)
class CeasaEsCollector:
    """Coleta boletins por mercado e data no sistema legado da CEASA-ES."""

    http_client: HttpClient
    raw_storage: RawHtmlStorage
    parser: CeasaEsParser
    base_url: str
    reuse_raw_before_request: bool = False
    supports_target_dates: bool = True
    category_specific_dates: bool = True

    def download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path:
        state, market_id, date_value, quote_date = self._prepare_query(
            category_slug,
            cotacao_date,
        )
        storage_category = self._build_storage_category(category_slug, quote_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is not None:
            return raw_file

        html = self._request_report(state, market_id, date_value)

        return self.raw_storage.save("ceasa-es", storage_category, html)

    def collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]:
        state, market_id, date_value, quote_date = self._prepare_query(
            category_slug,
            cotacao_date,
        )
        storage_category = self._build_storage_category(category_slug, quote_date)
        raw_file = self._find_reusable_raw(storage_category)

        if raw_file is None:
            html = self._request_report(state, market_id, date_value)

            if save_raw:
                self.raw_storage.save("ceasa-es", storage_category, html)
        else:
            html = raw_file.read_text(encoding="utf-8")

        return self.parser.parse_category(html, category_slug, self.base_url)

    def discover_categories(self) -> tuple[Category, ...]:
        html = self.http_client.get_text(self.base_url)
        categories = self.parser.parse_categories(html)

        if not categories:
            raise ValueError("Nenhum mercado da CEASA-ES foi encontrado.")

        return categories

    def _prepare_query(
        self,
        category_slug: str,
        target_date: date | None,
    ) -> tuple[CeasaEsFormState, str, str, date]:
        form_html = self.http_client.get_text(self.base_url)
        market_id = self.parser.find_market_id(form_html, category_slug)
        state = self.parser.parse_form_state(form_html)
        market_html = self.http_client.post_form(
            self.base_url,
            self._build_reload_data(state, market_id),
        )
        updated_state = self.parser.parse_form_state(market_html)
        quote_date, date_value = self.parser.find_quote_date_value(
            market_html,
            target_date,
        )

        return updated_state, market_id, date_value, quote_date

    def _request_report(
        self,
        state: CeasaEsFormState,
        market_id: str,
        date_value: str,
    ) -> str:
        ajax_response = self.http_client.post_form(
            self.base_url,
            {
                "rs": "ajax_filtro_boletim_es_submit_form",
                "rst": "",
                "rsrnd": "1",
                "rsargs[]": [
                    market_id,
                    date_value,
                    "1",
                    "",
                    "alterar",
                    "",
                    "",
                    "",
                    state.script_case_init,
                    state.csrf_token,
                ],
            },
        )
        redirect = self.parser.parse_redirect(ajax_response)
        report_url = urljoin(self.base_url, redirect.action)

        return self.http_client.post_form(
            report_url,
            {
                "nmgp_parms": redirect.parameters,
                "nmgp_url_saida": "",
                "script_case_init": redirect.script_case_init,
                "script_case_session": state.script_case_session,
            },
        )

    def _build_reload_data(
        self,
        state: CeasaEsFormState,
        market_id: str,
    ) -> dict[str, str]:
        return {
            "nm_form_submit": "1",
            "nmgp_idioma_novo": "",
            "nmgp_schema_f": "",
            "nmgp_url_saida": "",
            "bok": "OK",
            "nmgp_opcao": "recarga",
            "nmgp_ancora": "bloco_0",
            "nmgp_num_form": "0",
            "nmgp_parms": "",
            "script_case_init": state.script_case_init,
            "NM_cancel_return_new": "",
            "csrf_token": state.csrf_token,
            "_sc_force_mobile": "",
            "mercado": market_id,
            "datas": "",
        }

    def _build_storage_category(self, category_slug: str, quote_date: date) -> str:
        return f"{category_slug}_{quote_date.isoformat()}"

    def _find_reusable_raw(self, storage_category: str) -> Path | None:
        if not self.reuse_raw_before_request:
            return None

        return self.raw_storage.find_latest("ceasa-es", storage_category)
