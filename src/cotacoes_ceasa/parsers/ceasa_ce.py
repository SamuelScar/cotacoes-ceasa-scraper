import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.text import clean_text


DATE_PATTERN = re.compile(
    r"(?:Emissao|BOLETIM INFORMATIVO DIARIO DE)\s*:?\s*(\d{2}/\d{2}/\d{4})",
    re.IGNORECASE,
)
PRICE_PATTERN = re.compile(r"(?<!\d)\d{1,3}(?:\.\d{3})*,\d{2}(?!\d)")
SITUATION_KEYS = {"ENT", "SINF", "EST", "FIR", "FRA", "AUS", "SIN"}
UNIT_PATTERN = re.compile(
    r"\b(?:"
    r"CX\.?\s*\d*(?:\s*KG)?|"
    r"SC\.?\s*\d*(?:\s*KG)?|"
    r"KG|CENTO|MOLHO|PE|UND|UMA|UNIDADE"
    r")\b",
    re.IGNORECASE,
)
HEADER_KEYS = {
    "centraisdeabastecimentodocearasa",
    "nucleodeeconomiaeestatistica",
    "sistemadeinformacaodemercadoagricola",
    "preconoatacado",
    "frutasundsit",
    "hortalicasundsit",
    "cereaisundsit",
    "carnesundsit",
    "outrosundsit",
    "emissao",
    "legenda",
    "fonte",
    "email",
    "fone",
    "av",
}


@dataclass(frozen=True)
class CeasaCeQuoteLink:
    slug: str
    name: str
    url: str


class CeasaCeParser:
    """Extrai cotacoes dos boletins PDF da CEASA-CE."""

    def parse_categories(self, html: str, base_url: str) -> tuple[Category, ...]:
        return tuple(
            Category(slug=quote_link.slug, name=quote_link.name)
            for quote_link in self.parse_quote_links(html, base_url)
        )

    def find_quote_link(
        self,
        html: str,
        base_url: str,
        category_slug: str,
    ) -> CeasaCeQuoteLink:
        for quote_link in self.parse_quote_links(html, base_url):
            if quote_link.slug == category_slug:
                return quote_link

        raise ValueError(f"Boletim da CEASA-CE nao encontrado: {category_slug}.")

    def parse_quote_links(self, html: str, base_url: str) -> list[CeasaCeQuoteLink]:
        soup = BeautifulSoup(html, "lxml")
        quote_links: list[CeasaCeQuoteLink] = []
        seen_slugs: set[str] = set()

        for link in soup.find_all("a", href=True):
            href = link["href"]

            if not href.lower().split("?", 1)[0].endswith(".pdf"):
                continue

            url = urljoin(base_url, href)
            slug = self._build_slug_from_url(url)

            if not slug or slug in seen_slugs:
                continue

            name = self._build_name(slug, link.get_text(" ", strip=True))
            quote_links.append(CeasaCeQuoteLink(slug=slug, name=name, url=url))
            seen_slugs.add(slug)

        return quote_links

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        text = self._extract_pdf_text(content) if isinstance(content, bytes) else content
        data_cotacao = self._extract_quote_date(text)
        cotacoes: list[Cotacao] = []
        current_product: str | None = None

        for raw_line in text.splitlines():
            line = self._clean_pdf_line(raw_line)

            if not line:
                continue

            if not PRICE_PATTERN.search(line):
                if self._is_product_line(line):
                    current_product = line
                continue

            cotacao = self._parse_price_line(
                line=line,
                category_slug=category_slug,
                current_product=current_product,
                data_cotacao=data_cotacao,
                url_origem=url_origem,
            )

            if cotacao is not None:
                cotacoes.append(cotacao)

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

    def _extract_quote_date(self, text: str) -> date | None:
        normalized_text = _strip_accents(text)
        match = DATE_PATTERN.search(normalized_text)

        if match is None:
            return None

        return parse_br_date(match.group(1))

    def _parse_price_line(
        self,
        line: str,
        category_slug: str,
        current_product: str | None,
        data_cotacao: date | None,
        url_origem: str,
    ) -> Cotacao | None:
        price_matches = list(PRICE_PATTERN.finditer(line))

        if len(price_matches) < 3:
            return None

        prefix = line[: price_matches[0].start()].strip()
        suffix = line[price_matches[2].end() :].strip()
        description, unit, market_status = self._split_prefix(prefix)
        product, classification = self._split_product(description, current_product)

        if product is None:
            return None

        return Cotacao(
            fonte="CEASA-CE",
            categoria=category_slug,
            produto=product,
            unidade=unit,
            procedencia=self._extract_procedencia(suffix),
            classificacao=classification,
            preco_minimo=_parse_price(price_matches[0].group(0)),
            preco_comum=_parse_price(price_matches[1].group(0)),
            preco_maximo=_parse_price(price_matches[2].group(0)),
            situacao_mercado=market_status,
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _split_prefix(self, value: str) -> tuple[str, str | None, str | None]:
        parts = value.split()

        if not parts:
            return "", None, None

        situation_index = self._find_situation_index(parts)

        if situation_index is None:
            return value, None, None

        description_with_unit = " ".join(parts[:situation_index])
        situation = parts[situation_index]
        ascii_description_with_unit = _strip_accents(description_with_unit)
        unit_match = UNIT_PATTERN.search(ascii_description_with_unit)

        if unit_match is None:
            return description_with_unit, None, situation

        description = clean_text(description_with_unit[: unit_match.start()]) or ""
        unit = clean_text(description_with_unit[unit_match.start() :])

        return description, unit, situation

    def _find_situation_index(self, parts: list[str]) -> int | None:
        for index in range(len(parts) - 1, -1, -1):
            if _normalize_key(parts[index]).upper() in SITUATION_KEYS:
                return index

        return None

    def _split_product(
        self,
        description: str,
        current_product: str | None,
    ) -> tuple[str | None, str | None]:
        cleaned_description = clean_text(description)

        if not cleaned_description:
            return current_product, None

        if current_product is None:
            return cleaned_description, None

        description_key = _normalize_key(cleaned_description)
        current_key = _normalize_key(current_product)

        if description_key.startswith(current_key):
            classification = cleaned_description[len(current_product) :].strip()

            return current_product, clean_text(classification)

        return current_product, cleaned_description

    def _extract_procedencia(self, value: str) -> str | None:
        procedencia = clean_text(value)

        if not procedencia or set(procedencia) <= {"-", "."}:
            return None

        return procedencia

    def _clean_pdf_line(self, value: str) -> str | None:
        line = clean_text(value)

        if line is None:
            return None

        line_key = _normalize_key(line)

        if any(line_key.startswith(header_key) for header_key in HEADER_KEYS):
            return None

        return line

    def _is_product_line(self, line: str) -> bool:
        if PRICE_PATTERN.search(line):
            return False

        if any(char.isdigit() for char in line):
            return False

        line_key = _normalize_key(line)

        if not line_key or any(line_key.startswith(header_key) for header_key in HEADER_KEYS):
            return False

        letters = [char for char in line if char.isalpha()]

        return bool(letters) and line.upper() == line

    def _build_slug_from_url(self, url: str) -> str | None:
        parsed_url = urlparse(url)
        parts = [part for part in parsed_url.path.split("/") if part]

        if len(parts) < 2:
            return None

        market_slug = parts[-2]
        file_slug = parts[-1].rsplit(".", 1)[0]
        category_slug = file_slug.split("_", 1)[0]

        if not market_slug or not category_slug:
            return None

        return f"{_slugify(market_slug)}-{_slugify(category_slug)}"

    def _build_name(self, slug: str, link_text: str | None) -> str:
        market, _, category = slug.rpartition("-")
        category_name = clean_text(link_text) or category

        return f"{market.replace('-', ' ').title()} - {category_name.title()}"


def _parse_price(value: str) -> Decimal | None:
    cleaned_value = value.strip().replace(".", "").replace(",", ".")

    try:
        return Decimal(cleaned_value)
    except InvalidOperation:
        return None


def _normalize_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", _strip_accents(value or "").lower())


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _strip_accents(value).lower()).strip("-")


def _strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)

    return normalized.encode("ascii", "ignore").decode("ascii")
