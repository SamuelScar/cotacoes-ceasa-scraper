import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cotacoes_ceasa.models import Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import clean_text


DATE_PATTERN = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
DAY_PATTERN = re.compile(r"^\s*(\d{1,2})\s+de\s+", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"(?<!\d)\d+(?:\.\d{3})*,\d{2}(?!\d)")
SECTION_PATTERN = re.compile(r"^\d+\.\s+(.+?)(?:\s+S/C)?$")
UNIT_PATTERN = re.compile(r"\b(?:Ama|Cx|kg|Mol|Pct|Preg|Sc|Unid)\b", re.IGNORECASE)
MONTHS = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


@dataclass(frozen=True)
class CeasaRjMonthLink:
    month: int
    url: str


@dataclass(frozen=True)
class CeasaRjQuoteLink:
    quote_date: date
    url: str


class CeasaRjParser:
    """Extrai cotacoes dos PDFs diarios da CEASA-RJ."""

    def find_year_url(self, html: str, base_url: str, year: int) -> str:
        soup = BeautifulSoup(html, "lxml")

        for link in soup.find_all("a", href=True):
            if clean_text(link.get_text(" ", strip=True)) == str(year):
                return urljoin(base_url, link["href"])

        raise ValueError(f"Pagina da CEASA-RJ nao encontrada para {year}.")

    def parse_month_links(self, html: str, page_url: str) -> list[CeasaRjMonthLink]:
        soup = BeautifulSoup(html, "lxml")
        links: list[CeasaRjMonthLink] = []

        for link in soup.find_all("a", href=True):
            month = MONTHS.get(_strip_accents(link.get_text(" ", strip=True)).lower())

            if month is not None:
                links.append(CeasaRjMonthLink(month, urljoin(page_url, link["href"])))

        return links

    def parse_quote_links(
        self,
        html: str,
        page_url: str,
        year: int,
        month: int,
    ) -> list[CeasaRjQuoteLink]:
        soup = BeautifulSoup(html, "lxml")
        links: list[CeasaRjQuoteLink] = []

        for link in soup.find_all("a", href=True):
            day_match = DAY_PATTERN.match(link.get_text(" ", strip=True))

            if day_match is None:
                continue

            url = urljoin(page_url, link["href"])

            if not url.lower().split("?", 1)[0].endswith(".pdf"):
                continue

            quote_date = date(year, month, int(day_match.group(1)))
            links.append(CeasaRjQuoteLink(quote_date, url))

        return links

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        text = (
            self._extract_pdf_text(content)
            if isinstance(content, bytes)
            else content
        )
        data_cotacao = self._extract_quote_date(text)
        current_category = category_slug
        cotacoes: list[Cotacao] = []

        for raw_line in text.splitlines():
            line = clean_text(raw_line)

            if not line:
                continue

            section = self._extract_section(line)

            if section is not None:
                current_category = _slugify(section)
                continue

            cotacao = self._parse_price_line(
                line, current_category, data_cotacao, url_origem
            )

            if cotacao is not None:
                cotacoes.append(cotacao)

        return cotacoes

    def _parse_price_line(
        self,
        line: str,
        category_slug: str,
        data_cotacao: date | None,
        url_origem: str,
    ) -> Cotacao | None:
        prices = list(PRICE_PATTERN.finditer(line))

        if len(prices) < 3:
            return None

        selected_prices = prices[-3:]
        prefix = line[: selected_prices[0].start()]
        prefix = re.sub(r"(?:S/C|[-+]?\d+(?:,\d+)?%?)\s*$", "", prefix).strip()
        unit_match = UNIT_PATTERN.search(prefix)

        if unit_match is None:
            return None

        product = clean_text(prefix[: unit_match.start()])
        unit = clean_text(prefix[unit_match.start() :])

        if not product:
            return None

        return Cotacao(
            fonte="CEASA-RJ",
            categoria=category_slug,
            produto=product,
            unidade=unit,
            procedencia=None,
            classificacao=None,
            preco_minimo=parse_brl_money(selected_prices[0].group(0)),
            preco_comum=parse_brl_money(selected_prices[1].group(0)),
            preco_maximo=parse_brl_money(selected_prices[2].group(0)),
            situacao_mercado=None,
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _extract_pdf_text(self, content: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "Dependencia pypdf nao instalada. "
                "Instale as dependencias atualizadas do projeto."
            ) from error

        reader = PdfReader(BytesIO(content))
        texts: list[str] = []

        for page in reader.pages:
            try:
                texts.append(page.extract_text(extraction_mode="layout") or "")
            except TypeError:
                texts.append(page.extract_text() or "")

        return "\n".join(texts)

    def _extract_quote_date(self, text: str) -> date | None:
        match = DATE_PATTERN.search(text)

        return parse_br_date(match.group(0)) if match is not None else None

    def _extract_section(self, value: str) -> str | None:
        match = SECTION_PATTERN.match(value)

        return clean_text(match.group(1)) if match is not None else None


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _strip_accents(value).lower()).strip("-")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)

    return normalized.encode("ascii", "ignore").decode("ascii")
