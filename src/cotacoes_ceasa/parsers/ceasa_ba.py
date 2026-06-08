import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import (
    clean_text,
    normalize_key as _normalize_key,
    slugify as _slugify,
)
from cotacoes_ceasa.parsers.pdf import extract_pdf_text


DATE_PATTERN = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
PRICE_PATTERN = re.compile(r"(?<!\d)\d+(?:\.\d{3})*,\d{2}(?!\d)")
PROCEDENCE_PATTERN = re.compile(r"^[A-Z]{2}(?:[/;][A-Z]{2})*$")
UNIT_PATTERN = re.compile(
    r"\b(?:CENTO|CX\.?|FRD\.?|KG|MOL\.?|PCT\.?|SC\.?|UND\.?)\b",
    re.IGNORECASE,
)
CATEGORY_KEYS = {
    "cereais",
    "frutas",
    "hortalicas",
    "outrosgenerosalimenticios",
    "ovos",
}


@dataclass(frozen=True)
class CeasaBaQuoteLink:
    quote_date: date
    url: str


class CeasaBaParser:
    """Extrai cotacoes dos boletins PDF da CEASA-BA."""

    def parse_quote_links(self, html: str, base_url: str) -> list[CeasaBaQuoteLink]:
        soup = BeautifulSoup(html, "lxml")
        quote_links: list[CeasaBaQuoteLink] = []
        seen_urls: set[str] = set()

        for link in soup.find_all("a", href=True):
            text = clean_text(link.get_text(" ", strip=True)) or ""
            date_match = DATE_PATTERN.search(text)

            if date_match is None:
                continue

            url = urljoin(base_url, link["href"])

            if url in seen_urls or not url.lower().split("?", 1)[0].endswith(".pdf"):
                continue

            quote_date = parse_br_date(date_match.group(0))

            if quote_date is not None:
                quote_links.append(CeasaBaQuoteLink(quote_date, url))
                seen_urls.add(url)

        return quote_links

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        text = (
            extract_pdf_text(content)
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

            category = self._extract_category(line)

            if category is not None:
                current_category = category
                continue

            cotacao = self._parse_price_line(
                line,
                current_category,
                data_cotacao,
                url_origem,
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
        prefix = line[: selected_prices[0].start()].strip()
        suffix = line[selected_prices[2].end() :].strip()
        prefix_parts = prefix.split()

        if not prefix_parts or not PROCEDENCE_PATTERN.fullmatch(prefix_parts[-1]):
            return None

        procedencia = prefix_parts[-1]
        description = " ".join(prefix_parts[:-1])
        unit_match = UNIT_PATTERN.search(description)

        if unit_match is None:
            return None

        product = clean_text(description[: unit_match.start()])
        unit = clean_text(description[unit_match.start() :])

        if not product:
            return None

        return Cotacao(
            fonte="CEASA-BA",
            categoria=category_slug,
            produto=product,
            unidade=unit,
            procedencia=procedencia,
            classificacao=None,
            preco_minimo=parse_brl_money(selected_prices[0].group(0)),
            preco_comum=parse_brl_money(selected_prices[1].group(0)),
            preco_maximo=parse_brl_money(selected_prices[2].group(0)),
            situacao_mercado=clean_text(suffix),
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _extract_quote_date(self, text: str) -> date | None:
        match = re.search(r"EMISS[AÃ]O\s*:\s*(\d{2}/\d{2}/\d{4})", text)

        return parse_br_date(match.group(1)) if match is not None else None

    def _extract_category(self, value: str) -> str | None:
        key = _normalize_key(value)

        return _slugify(value) if key in CATEGORY_KEYS else None
