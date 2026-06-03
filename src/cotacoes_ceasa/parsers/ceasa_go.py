import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from html import unescape
from io import BytesIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import clean_text


BR_DATE_PATTERN = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
LINK_TEXT_DATE_PATTERN = re.compile(
    r"\b(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(20\d{2})\b",
    re.IGNORECASE,
)
URL_DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[-_/](\d{1,2})[-_/](20\d{2})(?!\d)")
PDF_URL_PATTERN = re.compile(r"https?://[^\"' <>\n\r]+\.pdf", re.IGNORECASE)
PRICE_PATTERN = re.compile(r"(?<!\d)\d+(?:\.\d{3})*,\d{2}(?!\d)")
SECTION_PATTERN = re.compile(r"^\d{2}\s*-\s*(.+)$")
UNIT_CODES = {
    "BD",
    "BDJ",
    "CX",
    "DZ",
    "ENG",
    "KG",
    "MAO",
    "MC",
    "MOL",
    "PCT",
    "PE",
    "PLT",
    "SC",
    "UN",
    "UNI",
}
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
HEADER_KEYS = {
    "centraisdeabastecimentodegoias",
    "cotacaodepreco",
    "pagina",
    "emitido",
    "todosprodutos",
    "periodo",
    "hora",
    "modelo",
    "nomeprodutoqtdeem",
    "kg",
    "embalagem",
    "padrao",
    "class",
    "precoemr",
    "comummaximominimokg",
    "linksiminformatica",
}


@dataclass(frozen=True)
class CeasaGoQuoteLink:
    quote_date: date
    url: str


@dataclass(frozen=True)
class CeasaGoMonthLink:
    month: int
    url: str


class CeasaGoParser:
    """Extrai cotacoes dos PDFs diarios da CEASA-GO."""

    def parse_categories(self, html: str, base_url: str) -> tuple[Category, ...]:
        return (Category(slug="cotacao-diaria", name="Cotacao diaria"),)

    def find_month_url(self, html: str, page_url: str, target_date: date) -> str:
        candidates = [
            month_link
            for month_link in self.parse_month_links(html, page_url, target_date.year)
            if month_link.month <= target_date.month
        ]

        if candidates:
            return max(candidates, key=lambda month_link: month_link.month).url

        raise ValueError(
            "Pagina mensal da CEASA-GO nao encontrada para "
            f"{target_date.year}-{target_date.month:02d}."
        )

    def parse_month_links(
        self,
        html: str,
        page_url: str,
        target_year: int,
    ) -> list[CeasaGoMonthLink]:
        soup = BeautifulSoup(html, "lxml")
        month_links: list[CeasaGoMonthLink] = []
        seen_urls: set[str] = set()

        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = clean_text(link.get_text(" ", strip=True)) or ""
            normalized_value = _strip_accents(f"{text} {href}").lower()

            if str(target_year) not in normalized_value:
                continue

            month = self._extract_month(normalized_value)

            if month is None:
                continue

            url = urljoin(page_url, href)

            if url in seen_urls:
                continue

            month_links.append(CeasaGoMonthLink(month=month, url=url))
            seen_urls.add(url)

        return month_links

    def parse_quote_links(self, html: str, page_url: str) -> list[CeasaGoQuoteLink]:
        soup = BeautifulSoup(html, "lxml")
        quote_links: list[CeasaGoQuoteLink] = []
        seen_urls: set[str] = set()

        for link in soup.find_all("a", href=True):
            href = link["href"]

            if not href.lower().split("?", 1)[0].endswith(".pdf"):
                continue

            text = clean_text(link.get_text(" ", strip=True)) or ""
            url = urljoin(page_url, href)
            quote_date = self._extract_quote_link_date(text, url)

            if quote_date is None or url in seen_urls:
                continue

            quote_links.append(CeasaGoQuoteLink(quote_date=quote_date, url=url))
            seen_urls.add(url)

        for url_match in PDF_URL_PATTERN.finditer(html):
            url = unescape(url_match.group(0))
            quote_date = self._extract_date_from_url(url)

            if quote_date is None or url in seen_urls:
                continue

            quote_links.append(CeasaGoQuoteLink(quote_date=quote_date, url=url))
            seen_urls.add(url)

        return sorted(quote_links, key=lambda item: item.quote_date, reverse=True)

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        text = self._extract_pdf_text(content) if isinstance(content, bytes) else content
        data_cotacao = self._extract_quote_date(text, url_origem)
        current_category = category_slug
        current_product: str | None = None
        current_unit: str | None = None
        cotacoes: list[Cotacao] = []

        for raw_line in text.splitlines():
            line = self._clean_pdf_line(raw_line)

            if not line:
                continue

            section = self._extract_section(line)

            if section is not None:
                current_category = _slugify(section)
                current_product = None
                current_unit = None
                continue

            if not PRICE_PATTERN.search(line):
                continue

            cotacao = self._parse_price_line(
                line=line,
                category_slug=current_category,
                current_product=current_product,
                current_unit=current_unit,
                data_cotacao=data_cotacao,
                url_origem=url_origem,
            )

            if cotacao is None:
                continue

            cotacoes.append(cotacao)
            current_product = cotacao.produto
            current_unit = cotacao.unidade

        return cotacoes

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
                page_text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                page_text = page.extract_text() or ""

            texts.append(page_text)

        return "\n".join(texts)

    def _extract_quote_date(self, text: str, url_origem: str) -> date | None:
        normalized_text = _strip_accents(text)
        period_match = re.search(r"Periodo\s*:\s*(\d{2}/\d{2}/\d{4})", normalized_text)

        if period_match is not None:
            return parse_br_date(period_match.group(1))

        date_match = BR_DATE_PATTERN.search(normalized_text)

        if date_match is not None:
            return parse_br_date(date_match.group(0))

        return self._extract_date_from_url(url_origem)

    def _parse_price_line(
        self,
        line: str,
        category_slug: str,
        current_product: str | None,
        current_unit: str | None,
        data_cotacao: date | None,
        url_origem: str,
    ) -> Cotacao | None:
        price_matches = list(PRICE_PATTERN.finditer(line))

        if len(price_matches) < 3:
            return None

        prefix = line[: price_matches[0].start()].strip()
        product, unit, classification = self._split_prefix(
            prefix,
            current_product,
            current_unit,
        )

        if product is None:
            return None

        return Cotacao(
            fonte="CEASA-GO",
            categoria=category_slug,
            produto=product,
            unidade=unit,
            procedencia=None,
            classificacao=classification,
            preco_minimo=parse_brl_money(price_matches[2].group(0)),
            preco_comum=parse_brl_money(price_matches[0].group(0)),
            preco_maximo=parse_brl_money(price_matches[1].group(0)),
            situacao_mercado=None,
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _split_prefix(
        self,
        value: str,
        current_product: str | None,
        current_unit: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        parts = value.split()

        if not parts:
            return None, None, None

        if current_product is not None and len(parts) <= 2:
            return current_product, current_unit, clean_text(value)

        if parts[0].isdigit() and len(parts) > 1:
            parts = parts[1:]

        if not parts:
            return current_product, current_unit, None

        classification = parts[-1] if self._is_classification(parts[-1]) else None
        description_parts = parts[:-1] if classification is not None else parts
        unit_index = self._find_unit_index(description_parts)

        if unit_index is None:
            product = clean_text(" ".join(description_parts))
            return product, None, classification

        product = clean_text(" ".join(description_parts[:unit_index]))
        unit = clean_text(" ".join(description_parts[unit_index:]))

        return product, unit, classification

    def _find_unit_index(self, parts: list[str]) -> int | None:
        for index in range(len(parts) - 1, -1, -1):
            if _normalize_key(parts[index]).upper() in UNIT_CODES:
                return index

        return None

    def _extract_section(self, value: str) -> str | None:
        match = SECTION_PATTERN.match(value)

        if match is None:
            return None

        return clean_text(match.group(1))

    def _clean_pdf_line(self, value: str) -> str | None:
        line = clean_text(value)

        if line is None:
            return None

        line_key = _normalize_key(line)

        if any(line_key.startswith(header_key) for header_key in HEADER_KEYS):
            return None

        return line

    def _extract_quote_link_date(self, text: str, url: str) -> date | None:
        normalized_text = _strip_accents(text).lower()
        text_match = LINK_TEXT_DATE_PATTERN.search(normalized_text)

        if text_match is not None:
            day = int(text_match.group(1))
            month = MONTHS.get(text_match.group(2))
            year = int(text_match.group(3))

            if month is not None:
                return date(year, month, day)

        br_date_match = BR_DATE_PATTERN.search(text)

        if br_date_match is not None:
            return parse_br_date(br_date_match.group(0))

        return self._extract_date_from_url(url)

    def _extract_date_from_url(self, url: str) -> date | None:
        url_match = URL_DATE_PATTERN.search(url)

        if url_match is None:
            return None

        return date(
            int(url_match.group(3)),
            int(url_match.group(2)),
            int(url_match.group(1)),
        )

    def _extract_month(self, value: str) -> int | None:
        for month_name, month_number in MONTHS.items():
            if month_name in value:
                return month_number

        return None

    def _is_classification(self, value: str) -> bool:
        return bool(re.fullmatch(r"\d+", value))


def _normalize_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(value or "").lower())


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _strip_accents(value).lower()).strip("-")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)

    return normalized.encode("ascii", "ignore").decode("ascii")
