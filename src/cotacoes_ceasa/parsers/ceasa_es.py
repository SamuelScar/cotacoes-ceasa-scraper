import json
import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import clean_text, slugify as _slugify


RESULT_DATE_PATTERN = re.compile(r"Data Pesquisada:\s*(\d{2}/\d{2}/\d{4})")
SAJAX_RESPONSE_PATTERN = re.compile(r"var res = '(.*)'; res;", re.DOTALL)


@dataclass(frozen=True)
class CeasaEsFormState:
    script_case_init: str
    script_case_session: str
    csrf_token: str


@dataclass(frozen=True)
class CeasaEsRedirect:
    action: str
    parameters: str
    script_case_init: str


class CeasaEsParser:
    """Extrai mercados, datas e cotacoes do sistema legado da CEASA-ES."""

    def parse_categories(self, html: str) -> tuple[Category, ...]:
        soup = BeautifulSoup(html, "lxml")
        market_select = soup.find("select", attrs={"name": "mercado"})

        if market_select is None:
            raise ValueError("Mercados da CEASA-ES nao encontrados.")

        return tuple(
            Category(slug=_slugify(name), name=name)
            for option in market_select.find_all("option")
            if option.get("value", "").strip() != "0"
            and (name := clean_text(option.get_text(" ", strip=True)))
        )

    def find_market_id(self, html: str, category_slug: str) -> str:
        soup = BeautifulSoup(html, "lxml")

        for option in soup.select('select[name="mercado"] option'):
            name = clean_text(option.get_text(" ", strip=True))

            if name and _slugify(name) == category_slug:
                return option.get("value", "").strip()

        raise ValueError(f"Mercado da CEASA-ES nao encontrado: {category_slug}.")

    def parse_form_state(self, html: str) -> CeasaEsFormState:
        soup = BeautifulSoup(html, "lxml")

        return CeasaEsFormState(
            script_case_init=self._find_input_value(soup, "script_case_init"),
            script_case_session=self._find_input_value(soup, "script_case_session"),
            csrf_token=self._find_input_value(soup, "csrf_token"),
        )

    def find_quote_date_value(
        self,
        html: str,
        target_date: date | None,
    ) -> tuple[date, str]:
        soup = BeautifulSoup(html, "lxml")
        limit_date = target_date or date.today()
        candidates: list[tuple[date, str]] = []

        for option in soup.select('select[name="datas"] option'):
            value = option.get("value", "")
            parsed_date = parse_br_date(value)

            if parsed_date is not None and parsed_date <= limit_date:
                candidates.append((parsed_date, value))

        if candidates:
            return max(candidates, key=lambda item: item[0])

        raise ValueError(
            "Cotacao da CEASA-ES nao encontrada ate "
            f"{limit_date.isoformat()}."
        )

    def parse_redirect(self, response: str) -> CeasaEsRedirect:
        match = SAJAX_RESPONSE_PATTERN.search(response)

        if match is None:
            raise ValueError("Resposta de consulta da CEASA-ES invalida.")

        response_json = json.loads(f'"{match.group(1)}"')
        payload = json.loads(response_json)

        if payload.get("result") != "OK" or "redirInfo" not in payload:
            raise ValueError("A CEASA-ES rejeitou os parametros da consulta.")

        redirect = payload["redirInfo"]

        return CeasaEsRedirect(
            action=redirect["action"],
            parameters=redirect["nmgp_parms"],
            script_case_init=redirect["script_case_init"],
        )

    def parse_category(
        self,
        html: str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        soup = BeautifulSoup(html, "lxml")
        data_cotacao = self._extract_quote_date(soup.get_text(" ", strip=True))
        cotacoes: list[Cotacao] = []

        for row in soup.select("tr.scGridFieldOdd, tr.scGridFieldEven"):
            product = self._find_field_text(row, "prdnom")

            if product is None:
                continue

            cotacoes.append(
                Cotacao(
                    fonte="CEASA-ES",
                    categoria="nao-informada",
                    produto=product,
                    unidade=self._find_field_text(row, "embdesresu"),
                    procedencia=None,
                    classificacao=None,
                    preco_minimo=parse_brl_money(
                        self._find_field_text(row, "pboprcmin")
                    ),
                    preco_comum=parse_brl_money(
                        self._find_field_text(row, "pboprccomum")
                    ),
                    preco_maximo=parse_brl_money(
                        self._find_field_text(row, "pboprcmax")
                    ),
                    situacao_mercado=self._find_field_text(row, "mersit"),
                    data_cotacao=data_cotacao,
                    url_origem=url_origem,
                    entreposto=category_slug,
                )
            )

        if not cotacoes:
            raise ValueError("Tabela de cotacoes da CEASA-ES nao encontrada.")

        return cotacoes

    def _find_input_value(self, soup: BeautifulSoup, name: str) -> str:
        for field in soup.find_all("input", attrs={"name": name}):
            value = field.get("value")

            if value:
                return value

        raise ValueError(f"Campo {name} da CEASA-ES nao encontrado.")

    def _extract_quote_date(self, text: str) -> date | None:
        match = RESULT_DATE_PATTERN.search(text)

        return parse_br_date(match.group(1)) if match is not None else None

    def _find_field_text(self, row, field_name: str) -> str | None:
        field = row.select_one(f".css_{field_name}_grid_line")

        return clean_text(field.get_text(" ", strip=True)) if field is not None else None
