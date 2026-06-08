from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from cotacoes_ceasa.core.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import clean_text


class CeasaPeParser:
    """Extrai cotacoes das tabelas HTML da CEASA-PE."""

    def parse_categories(self, html: str, base_url: str) -> tuple[Category, ...]:
        """Descobre categorias de cotacao a partir da pagina base da CEASA-PE."""
        soup = BeautifulSoup(html, "lxml")
        parsed_base_url = urlparse(base_url)
        base_path = parsed_base_url.path.rstrip("/")
        categories_by_slug: dict[str, Category] = {}

        for link in soup.find_all("a", href=True):
            url = urljoin(base_url, link["href"])
            parsed_url = urlparse(url)
            path = parsed_url.path.rstrip("/")

            if parsed_url.netloc != parsed_base_url.netloc:
                continue

            if not path.startswith(f"{base_path}/"):
                continue

            slug = path.removeprefix(f"{base_path}/").strip("/")

            if not slug or "/" in slug:
                continue

            name = self._build_category_name(link.get_text(" ", strip=True), slug)
            categories_by_slug.setdefault(slug, Category(slug=slug, name=name))

        return tuple(categories_by_slug.values())

    def parse_category(
        self,
        html: str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        """Converte o HTML de uma categoria em registros de cotacao."""
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")

        if table is None:
            raise ValueError("Tabela de cotacoes da CEASA-PE nao encontrada.")

        cotacoes: list[Cotacao] = []

        for row in table.select("tbody tr.tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]

            if len(cells) < 8:
                continue

            data_cotacao = self._extract_row_date(row)

            cotacoes.append(
                Cotacao(
                    fonte="CEASA-PE",
                    categoria=category_slug,
                    produto=cells[0] or "",
                    unidade=cells[1],
                    procedencia=cells[2],
                    classificacao=cells[3],
                    preco_minimo=parse_brl_money(cells[4]),
                    preco_comum=parse_brl_money(cells[5]),
                    preco_maximo=parse_brl_money(cells[6]),
                    situacao_mercado=cells[7],
                    data_cotacao=parse_br_date(data_cotacao),
                    url_origem=url_origem,
                )
            )

        return cotacoes

    def _extract_row_date(self, row) -> str | None:
        button = row.select_one(".btn-grafico")

        if button is None:
            return None

        return button.get("data-date")

    def _build_category_name(self, link_text: str | None, slug: str) -> str:
        name = clean_text(link_text)

        if name and name.lower() != "saiba mais":
            return name

        return slug.replace("-", " ").title()
