import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.text import (
    clean_text,
    normalize_key as _normalize_key,
    slugify as _slugify,
    strip_accents as _strip_accents,
)
from cotacoes_ceasa.parsers.pdf import extract_pdf_text


DATE_LINK_PATTERN = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
QUOTE_DATE_PATTERN = re.compile(r"Cotacao Realizada em:\s*(\d{2}/\d{2}/\d{4})")
PRICE_PATTERN = re.compile(r"(?<!\d)\d+(?:[.,]\d{2})(?!\d)")
HEADER_KEYS = {
    "centraisdeabastecimentodecampinassa",
    "formulariodecotacaoceasacampinasboletimnumero",
    "cotacaorealizadaem",
    "produtovariedade",
    "produtovariedadesubvariedadeclassificacaovalor",
    "minimocomummaximo",
    "minimo",
    "valor",
    "comum",
    "maximo",
    "mercado",
}
HEADER_MARKERS = (
    "Produto Variedade",
    "SubVariedade",
    "Classificacao",
    "Valor",
    "Minimo",
    "Maximo",
    "Mercado",
)


@dataclass(frozen=True)
class CampinasQuoteLink:
    quote_date: date
    url: str


class CeasaCampinasParser:
    """Extrai cotacoes dos PDFs publicados pela CEASA Campinas."""

    def parse_categories(self, html: str, base_url: str) -> tuple[Category, ...]:
        soup = BeautifulSoup(html, "lxml")
        title = self._extract_page_title(soup, base_url)

        return (Category(slug=_slugify(title), name=title),)

    def parse_quote_links(
        self,
        html: str,
        page_url: str,
    ) -> list[CampinasQuoteLink]:
        soup = BeautifulSoup(html, "lxml")
        quote_links: list[CampinasQuoteLink] = []

        for link in soup.find_all("a", href=True):
            text = clean_text(link.get_text(" ", strip=True)) or ""
            date_match = DATE_LINK_PATTERN.search(text)

            if date_match is None:
                continue

            href = link["href"]

            if not href.lower().split("?", 1)[0].endswith(".pdf"):
                continue

            quote_date = parse_br_date(date_match.group(0))

            if quote_date is None:
                continue

            quote_links.append(
                CampinasQuoteLink(
                    quote_date=quote_date,
                    url=urljoin(page_url, href),
                )
            )

        return sorted(quote_links, key=lambda item: item.quote_date, reverse=True)

    def parse_pagination_urls(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        urls: list[str] = []
        seen_urls: set[str] = set()

        for link in soup.find_all("a", href=True):
            href = link["href"]

            if "page=" not in href:
                continue

            url = urljoin(page_url, href)

            if url in seen_urls:
                continue

            urls.append(url)
            seen_urls.add(url)

        return urls

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        text = extract_pdf_text(content) if isinstance(content, bytes) else content
        data_cotacao = self._extract_quote_date(text)

        if data_cotacao is None:
            raise ValueError(
                "Layout antigo da CEASA Campinas sem suporte confiavel."
            )

        current_category = category_slug
        cotacoes: list[Cotacao] = []
        pending_parts: list[str] = []
        preceded_by_blank = True
        previous_line_had_price = False

        for raw_line in text.splitlines():
            line = self._clean_pdf_line(raw_line)

            if not line:
                preceded_by_blank = True
                continue

            if PRICE_PATTERN.search(line):
                full_line = clean_text(" ".join([*pending_parts, line])) or ""
                pending_parts.clear()
                cotacao = self._parse_price_line(
                    line=full_line,
                    category_slug=current_category,
                    data_cotacao=data_cotacao,
                    url_origem=url_origem,
                )

                if cotacao is not None:
                    cotacoes.append(cotacao)

                preceded_by_blank = False
                previous_line_had_price = True
                continue

            if previous_line_had_price and not preceded_by_blank:
                previous_line_had_price = False
                preceded_by_blank = False
                continue

            previous_line_had_price = False

            if self._is_header_line(line):
                pending_parts.clear()
            elif preceded_by_blank and self._is_section_line(line):
                current_category = _slugify(line)
                pending_parts.clear()
            else:
                pending_parts.append(line)

            preceded_by_blank = False

        return cotacoes

    def _extract_page_title(self, soup: BeautifulSoup, base_url: str) -> str:
        for tag_name in ("h1", "h2"):
            title_tag = soup.find(tag_name)
            title = clean_text(title_tag.get_text(" ", strip=True)) if title_tag else None

            if title:
                return title

        return base_url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()

    def _extract_quote_date(self, text: str) -> date | None:
        normalized_text = _strip_accents(text)
        match = QUOTE_DATE_PATTERN.search(normalized_text)

        if match is None:
            return None

        return parse_br_date(match.group(1))

    def _parse_price_line(
        self,
        line: str,
        category_slug: str,
        data_cotacao: date | None,
        url_origem: str,
    ) -> Cotacao | None:
        price_matches = list(PRICE_PATTERN.finditer(line))

        if len(price_matches) < 3:
            return None

        prefix = line[: price_matches[0].start()].strip()
        market = self._extract_market(line[price_matches[2].end() :])
        product, unit, classification = self._split_product(prefix)

        if product is None:
            return None

        return Cotacao(
            fonte="CEASA Campinas",
            categoria=category_slug,
            produto=product,
            unidade=unit,
            procedencia=None,
            classificacao=classification,
            preco_minimo=_parse_price(price_matches[0].group(0)),
            preco_comum=_parse_price(price_matches[1].group(0)),
            preco_maximo=_parse_price(price_matches[2].group(0)),
            situacao_mercado=market,
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _split_product(self, value: str) -> tuple[str | None, str | None, str | None]:
        product, separator, details = value.partition(" - ")

        if not separator:
            return clean_text(value), None, None

        detail_parts = (clean_text(details) or "").split(maxsplit=1)
        unit = detail_parts[0] if detail_parts else None
        classification = detail_parts[1] if len(detail_parts) > 1 else None

        return clean_text(product), unit, clean_text(classification)

    def _extract_market(self, value: str) -> str | None:
        cleaned_value = _strip_accents(value)

        for marker in HEADER_MARKERS:
            cleaned_value = cleaned_value.split(marker, 1)[0]

        parts = (clean_text(cleaned_value) or "").split()

        return parts[0] if parts else None

    def _clean_pdf_line(self, value: str) -> str | None:
        line = clean_text(value)

        if line is None:
            return None

        ascii_line = _strip_accents(line)

        for marker in HEADER_MARKERS:
            marker_index = ascii_line.find(marker)
            if marker_index > 0:
                line = line[:marker_index]
                ascii_line = ascii_line[:marker_index]

        return clean_text(line)

    def _is_section_line(self, line: str) -> bool:
        if self._is_header_line(line):
            return False

        if PRICE_PATTERN.search(line) or " - " in line:
            return False

        if any(char.isdigit() for char in line):
            return False

        letters = [char for char in line if char.isalpha()]

        return bool(letters) and line.upper() == line

    def _is_header_line(self, line: str) -> bool:
        line_key = _normalize_key(line)

        return any(line_key.startswith(header_key) for header_key in HEADER_KEYS)


def _parse_price(value: str) -> Decimal | None:
    cleaned_value = value.strip()

    if "," in cleaned_value:
        cleaned_value = cleaned_value.replace(".", "").replace(",", ".")

    try:
        return Decimal(cleaned_value)
    except InvalidOperation:
        return None
