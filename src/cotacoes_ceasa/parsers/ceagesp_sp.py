import json
import re
from datetime import date

from bs4 import BeautifulSoup

from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import (
    clean_text,
    normalize_key as _normalize_key,
    slugify as _slugify,
)


GROUPS_PATTERN = re.compile(r"var\s+Grupos\s*=\s*(\{.+?\});", re.DOTALL)
RESULT_DATE_PATTERN = re.compile(r"Data:\s*(\d{2}/\d{2}/\d{4})")


class CeagespSpParser:
    """Extrai cotacoes HTML da capital publicadas pela CEAGESP-SP."""

    def parse_categories(self, html: str) -> tuple[Category, ...]:
        return tuple(
            Category(slug=_slugify(name), name=name)
            for name, dates in self._extract_groups(html).items()
            if dates
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

        raise ValueError(
            f"Cotacao da CEAGESP-SP nao encontrada para {category_name} "
            f"ate {limit_date.isoformat()}."
        )

    def parse_category(
        self,
        html: str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="contacao_lista")

        if table is None:
            raise ValueError("Tabela de cotacoes da CEAGESP-SP nao encontrada.")

        data_cotacao = self._extract_result_date(table.get_text(" ", strip=True))
        cotacoes: list[Cotacao] = []

        for row in table.find_all("tr"):
            cells = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in row.find_all("td", recursive=False)
            ]

            if len(cells) != 7 or _normalize_key(cells[0]) == "produto":
                continue

            product, classification, unit = cells[:3]

            if not product:
                continue

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

    def _extract_groups(self, html: str) -> dict[str, list[str] | None]:
        match = GROUPS_PATTERN.search(html)

        if match is None:
            raise ValueError("Datas disponiveis da CEAGESP-SP nao encontradas.")

        return json.loads(match.group(1))

    def _extract_result_date(self, value: str) -> date | None:
        match = RESULT_DATE_PATTERN.search(value)

        return parse_br_date(match.group(1)) if match is not None else None
