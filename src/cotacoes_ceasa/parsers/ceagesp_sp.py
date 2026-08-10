import json
import re
from datetime import date

from bs4 import BeautifulSoup
from bs4.element import Tag

from cotacoes_ceasa.core.errors import QuotationNotFoundError
from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import (
    clean_text,
    normalize_key as _normalize_key,
    slugify as _slugify,
)


GROUPS_PATTERN = re.compile(r"var\s+Grupos\s*=\s*(\{.*?\});", re.DOTALL)
RESULT_DATE_PATTERN = re.compile(r"Data:\s*(\d{2}/\d{2}/\d{4})")
RESULT_CATEGORY_PATTERN = re.compile(r"Categoria:\s*(.+?)\s+Data:", re.IGNORECASE)
SERVICE_UNAVAILABLE_KEY = "servicoencontraseindisponivelnomomento"


class CeagespResponseError(ValueError):
    """Indica que uma resposta da CEAGESP-SP nao pode ser utilizada."""


class CeagespDiscoveryError(CeagespResponseError):
    """Indica que a pagina de descoberta nao pode ser usada nesta tentativa."""


class CeagespServiceUnavailableError(CeagespResponseError):
    """Indica indisponibilidade informada pela propria CEAGESP-SP."""


class CeagespDiscoveryLayoutError(CeagespDiscoveryError):
    """Indica quebra do contrato HTML usado para descobrir as cotacoes."""


class CeagespCalendarUnavailableError(CeagespDiscoveryError):
    """Indica que o calendario foi carregado sem nenhuma publicacao."""


class CeagespCategoryLayoutError(CeagespResponseError):
    """Indica quebra do contrato HTML da resposta de uma categoria."""


class CeagespSpParser:
    """Extrai cotacoes HTML da capital publicadas pela CEAGESP-SP."""

    def parse_categories(self, html: str) -> tuple[Category, ...]:
        categories = tuple(
            Category(slug=_slugify(name), name=name)
            for name, dates in self._extract_groups(html).items()
            if dates
        )

        if categories:
            return categories

        raise CeagespCalendarUnavailableError(
            "Calendario da CEAGESP-SP carregado sem publicacoes disponiveis."
        )

    def find_category(self, html: str, category_slug: str) -> Category:
        for category in self.parse_categories(html):
            if category.slug == category_slug:
                return category

        raise ValueError(f"Categoria da CEAGESP-SP nao encontrada: {category_slug}.")

    def find_quote_date(
        self,
        html: str,
        category_name: str,
        target_date: date | None,
    ) -> date:
        dates = [
            parsed_date
            for value in self._extract_groups(html).get(category_name, [])
            if (parsed_date := parse_br_date(value)) is not None
        ]
        limit_date = target_date or date.today()
        candidates = [quote_date for quote_date in dates if quote_date <= limit_date]

        if candidates:
            return max(candidates)

        raise QuotationNotFoundError(
            f"Cotacao da CEAGESP-SP nao encontrada para {category_name} "
            f"ate {limit_date.isoformat()}."
        )

    def parse_category(
        self,
        html: str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        table = self._find_quote_table(html)

        data_cotacao = self._extract_result_date(table.get_text(" ", strip=True))
        cotacoes: list[Cotacao] = []

        for row in table.find_all("tr"):
            cells = self._extract_row_cells(row)

            if not self._is_quote_row(cells):
                continue

            product, classification, unit = cells[:3]

            cotacoes.append(
                Cotacao(
                    fonte="CEAGESP-SP",
                    categoria=category_slug,
                    produto=product,
                    unidade=unit,
                    procedencia=None,
                    classificacao=classification,
                    preco_minimo=parse_brl_money(cells[3]),
                    preco_comum=parse_brl_money(cells[4]),
                    preco_maximo=parse_brl_money(cells[5]),
                    situacao_mercado=None,
                    data_cotacao=data_cotacao,
                    url_origem=url_origem,
                )
            )

        return cotacoes

    def validate_category_response(
        self,
        html: str,
        expected_category: str,
        expected_date: date,
    ) -> None:
        table = self._find_quote_table(html)
        table_text = table.get_text(" ", strip=True)
        result_category = self._extract_result_category(table_text)
        result_date = self._extract_result_date(table_text)

        if (
            result_category is None
            or _normalize_key(result_category) != _normalize_key(expected_category)
        ):
            raise CeagespCategoryLayoutError(
                "Tabela da CEAGESP-SP sem a categoria solicitada "
                f"({expected_category})."
            )

        if result_date is None:
            raise CeagespCategoryLayoutError(
                "Tabela da CEAGESP-SP sem a data da cotacao."
            )

        if result_date != expected_date:
            raise CeagespCategoryLayoutError(
                "Tabela da CEAGESP-SP retornou a data "
                f"{result_date.isoformat()} em vez de {expected_date.isoformat()}."
            )

        if not any(
            self._is_quote_row(self._extract_row_cells(row))
            for row in table.find_all("tr")
        ):
            raise CeagespCategoryLayoutError(
                "Tabela da CEAGESP-SP sem linhas de cotacao."
            )

    def _extract_groups(self, html: str) -> dict[str, list[str] | None]:
        match = GROUPS_PATTERN.search(html)

        if match is None:
            if self._is_service_unavailable(html):
                raise CeagespServiceUnavailableError(
                    "Servico de cotacoes da CEAGESP-SP indisponivel no momento."
                )

            raise CeagespDiscoveryLayoutError(
                "Pagina da CEAGESP-SP sem o calendario de cotacoes esperado."
            )

        try:
            groups = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise CeagespDiscoveryLayoutError(
                "Calendario de cotacoes da CEAGESP-SP em formato invalido."
            ) from error

        if not self._is_valid_groups(groups):
            raise CeagespDiscoveryLayoutError(
                "Calendario de cotacoes da CEAGESP-SP com estrutura invalida."
            )

        return groups

    def _is_service_unavailable(self, html: str) -> bool:
        page_text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

        return SERVICE_UNAVAILABLE_KEY in _normalize_key(page_text)

    def _is_valid_groups(self, groups: object) -> bool:
        if not isinstance(groups, dict):
            return False

        return all(
            isinstance(name, str)
            and (
                dates is None
                or (
                    isinstance(dates, list)
                    and all(self._is_valid_group_date(value) for value in dates)
                )
            )
            for name, dates in groups.items()
        )

    def _is_valid_group_date(self, value: object) -> bool:
        if not isinstance(value, str):
            return False

        try:
            return parse_br_date(value) is not None
        except ValueError:
            return False

    def _find_quote_table(self, html: str) -> Tag:
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="contacao_lista")

        if isinstance(table, Tag):
            return table

        if self._is_service_unavailable(html):
            raise CeagespServiceUnavailableError(
                "Servico de cotacoes da CEAGESP-SP indisponivel no momento."
            )

        raise CeagespCategoryLayoutError(
            "Resposta da CEAGESP-SP sem a tabela de cotacoes esperada."
        )

    def _extract_row_cells(self, row: Tag) -> list[str | None]:
        return [
            clean_text(cell.get_text(" ", strip=True))
            for cell in row.find_all("td", recursive=False)
        ]

    def _is_quote_row(self, cells: list[str | None]) -> bool:
        return (
            len(cells) == 7
            and bool(cells[0])
            and _normalize_key(cells[0]) != "produto"
        )

    def _extract_result_date(self, value: str) -> date | None:
        match = RESULT_DATE_PATTERN.search(value)

        if match is None:
            return None

        try:
            return parse_br_date(match.group(1))
        except ValueError:
            return None

    def _extract_result_category(self, value: str) -> str | None:
        match = RESULT_CATEGORY_PATTERN.search(value)

        return clean_text(match.group(1)) if match is not None else None
