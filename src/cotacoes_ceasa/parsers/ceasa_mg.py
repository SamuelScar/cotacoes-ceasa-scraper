import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.text import clean_text, normalize_key as _normalize_key


PRICE_CATEGORY = Category(slug="preco-mais-comum", name="Preco mais comum")

DATE_PATTERN = re.compile(r"\d{2}/\d{2}/\d{4}")


class CeasaMgParser:
    """Extrai cotacoes da tabela de preco mais comum da CEASA-MG."""

    def parse_categories(self, html: str, base_url: str) -> tuple[Category, ...]:
        return (PRICE_CATEGORY,)

    def parse_category(
        self,
        html: str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        soup = BeautifulSoup(html, "lxml")
        table = self._find_prices_table(soup)
        rows = self._extract_rows(table)
        city_dates = self._extract_city_dates(soup)
        header = self._extract_header(rows)
        last_city_column = _last_city_column(header)
        cotacoes: list[Cotacao] = []

        for row in rows:
            cells = self._extract_cells(row)

            if len(cells) <= last_city_column:
                continue

            product = cells[header.product_column]
            unit = cells[header.unit_column]

            if not product or _normalize_key(product) == "produtos":
                continue

            for city_column in header.city_columns:
                price = _parse_price(cells[city_column.column_index])

                if price is None:
                    continue

                cotacoes.append(
                    Cotacao(
                        fonte="CEASA-MG",
                        categoria="nao-informada",
                        produto=product,
                        unidade=unit,
                        procedencia=None,
                        classificacao=None,
                        preco_minimo=None,
                        preco_comum=price,
                        preco_maximo=None,
                        situacao_mercado=None,
                        data_cotacao=city_dates.get(city_column.key),
                        url_origem=url_origem,
                        entreposto=city_column.name,
                    )
                )

        return cotacoes

    def _find_prices_table(self, soup: BeautifulSoup):
        for table in soup.find_all("table"):
            rows = self._extract_rows(table)

            try:
                header = self._extract_header(rows)
            except ValueError:
                continue

            if self._has_price_rows(rows, header):
                return table

        raise ValueError("Tabela de cotacoes da CEASA-MG nao encontrada.")

    def _extract_rows(self, table) -> list:
        rows = table.find_all("tr", recursive=False)

        return rows or table.find_all("tr")

    def _extract_header(self, rows: list) -> "_Header":
        for row in rows:
            cells = self._extract_cells(row)
            columns_by_key = {
                _normalize_key(value): index
                for index, value in enumerate(cells)
            }

            if "produtos" not in columns_by_key or "embalagens" not in columns_by_key:
                continue

            fixed_columns = {
                columns_by_key["produtos"],
                columns_by_key["embalagens"],
            }
            city_columns = [
                _CityColumn(
                    key=_normalize_key(value),
                    name=value,
                    column_index=index,
                )
                for index, value in enumerate(cells)
                if value and index not in fixed_columns
            ]

            if city_columns:
                return _Header(
                    product_column=columns_by_key["produtos"],
                    unit_column=columns_by_key["embalagens"],
                    city_columns=city_columns,
                )

        raise ValueError("Cabecalho de cotacoes da CEASA-MG nao encontrado.")

    def _has_price_rows(self, rows: list, header: "_Header") -> bool:
        last_city_column = _last_city_column(header)

        for row in rows:
            cells = self._extract_cells(row)

            if len(cells) <= last_city_column:
                continue

            product = cells[header.product_column]

            if not product or _normalize_key(product) == "produtos":
                continue

            if any(
                _parse_price(cells[city_column.column_index]) is not None
                for city_column in header.city_columns
            ):
                return True

        return False

    def _extract_cells(self, row) -> list[str]:
        return [
            text or ""
            for text in (
                clean_text(cell.get_text(" ", strip=True))
                for cell in row.find_all(["td", "th"], recursive=False)
            )
        ]

    def _extract_city_dates(self, soup: BeautifulSoup) -> dict[str, date]:
        dates: dict[str, date] = {}

        for line in soup.get_text("\n", strip=True).splitlines():
            city_text, separator, date_text = line.partition("-")

            if not separator:
                continue

            city_key = _normalize_key(city_text)
            date_match = DATE_PATTERN.search(date_text)

            if not city_key or date_match is None:
                continue

            parsed_date = parse_br_date(date_match.group(0))

            if parsed_date is not None:
                dates[city_key] = parsed_date

        return dates


@dataclass(frozen=True)
class _CityColumn:
    key: str
    name: str
    column_index: int


@dataclass(frozen=True)
class _Header:
    product_column: int
    unit_column: int
    city_columns: list[_CityColumn]


def _last_city_column(header: _Header) -> int:
    return max(
        (city_column.column_index for city_column in header.city_columns),
        default=0,
    )


def _parse_price(value: str | None) -> Decimal | None:
    if not value:
        return None

    cleaned_value = value.replace("R$", "").strip()

    if not cleaned_value or set(cleaned_value) <= {"-"}:
        return None

    if "," in cleaned_value and "." in cleaned_value:
        cleaned_value = cleaned_value.replace(".", "").replace(",", ".")
    else:
        cleaned_value = cleaned_value.replace(",", ".")

    try:
        return Decimal(cleaned_value)
    except InvalidOperation:
        return None
