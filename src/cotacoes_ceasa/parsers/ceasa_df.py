import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import (
    clean_text,
    normalize_key as _normalize_key,
    slugify as _slugify,
    strip_accents as _strip_accents,
)
from cotacoes_ceasa.parsers.pdf import extract_pdf_pages


PRICE_CATEGORY = Category(slug="sima", name="SIMA")
DATE_PATTERN = re.compile(r"DATA:\s*(\d{2}\.\d{2}\.\d{4})")
PRICE_PATTERN = re.compile(r"(?<!\d)\d+(?:\.\d{3})*,\d{2}(?!\d)")
MARKET_STATUS_PATTERN = re.compile(r"\b(?:AUS|ENT|EST|FIR|FRA|SINF)\b")
PROCEDENCE_PATTERN = re.compile(r"\bPROC\.?\s*-?\s*([A-Z/. -]+)", re.IGNORECASE)
UNIT_PATTERN = re.compile(r"\(([^()]*(?:CX|DZ|KG|PCT|SC|UND)[^()]*)\)", re.IGNORECASE)
INLINE_UNIT_PATTERN = re.compile(
    r"\s+-\s+((?:CX|DZ|KG|PCT|SC|UND)\.?(?:\s+[^-]+)?)\s+PROC",
    re.IGNORECASE,
)
CATEGORY_KEYS = {"hortalicas", "frutas", "ovos"}
DEFAULT_COLUMN_BREAK = 118


class CeasaDfParser:
    """Extrai cotacoes do boletim SIMA da CEASA-DF."""

    def find_sima_url(self, html: str, base_url: str) -> str:
        soup = BeautifulSoup(html, "lxml")

        for link in soup.find_all("a", href=True):
            text = clean_text(link.get_text(" ", strip=True)) or ""
            url = urljoin(base_url, link["href"])

            if "sima" in text.lower() and url.lower().endswith(".pdf"):
                return url

        raise ValueError("PDF SIMA da CEASA-DF nao encontrado.")

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]:
        page_texts = (
            extract_pdf_pages(content)
            if isinstance(content, bytes)
            else [content]
        )
        text = "\n".join(page_texts)
        data_cotacao = self._extract_quote_date(text)
        left_lines: list[str] = []
        right_lines: list[str] = []

        for page_text in page_texts:
            page_left_lines, page_right_lines = self._split_columns(page_text)
            left_lines.extend(page_left_lines)
            right_lines.extend(page_right_lines)

        return self._parse_lines(
            left_lines,
            category_slug,
            data_cotacao,
            url_origem,
        ) + self._parse_lines(
            right_lines,
            category_slug,
            data_cotacao,
            url_origem,
        )

    def _parse_lines(
        self,
        lines: list[str],
        category_slug: str,
        data_cotacao: date | None,
        url_origem: str,
    ) -> list[Cotacao]:
        current_category = category_slug
        current_product: str | None = None
        current_unit: str | None = None
        current_procedencia: str | None = None
        cotacoes: list[Cotacao] = []

        for line in lines:
            category = self._extract_category(line)

            if category is not None:
                current_category = category
                continue

            if self._is_product_header(line):
                current_product = self._extract_product(line)
                current_unit = self._extract_unit(line)
                current_procedencia = self._extract_procedencia(line)

            cotacao = self._parse_price_line(
                line=line,
                category_slug=current_category,
                current_product=current_product,
                current_unit=current_unit,
                current_procedencia=current_procedencia,
                data_cotacao=data_cotacao,
                url_origem=url_origem,
            )

            if cotacao is not None:
                cotacoes.append(cotacao)

        return cotacoes

    def _parse_price_line(
        self,
        line: str,
        category_slug: str,
        current_product: str | None,
        current_unit: str | None,
        current_procedencia: str | None,
        data_cotacao: date | None,
        url_origem: str,
    ) -> Cotacao | None:
        prices = list(PRICE_PATTERN.finditer(line))

        if current_product is None or len(prices) < 3:
            return None

        selected_prices = prices[-3:]
        prefix = line[: selected_prices[0].start()].strip()
        status_match = MARKET_STATUS_PATTERN.search(prefix)

        if status_match is None:
            return None

        classification = clean_text(prefix[: status_match.start()])

        if self._is_product_header(line):
            classification = None

        return Cotacao(
            fonte="CEASA-DF",
            categoria=category_slug,
            produto=current_product,
            unidade=current_unit,
            procedencia=current_procedencia,
            classificacao=classification,
            preco_minimo=parse_brl_money(selected_prices[0].group(0)),
            preco_comum=parse_brl_money(selected_prices[1].group(0)),
            preco_maximo=parse_brl_money(selected_prices[2].group(0)),
            situacao_mercado=status_match.group(0),
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _split_columns(self, text: str) -> tuple[list[str], list[str]]:
        left_lines: list[str] = []
        right_lines: list[str] = []
        column_break = self._find_column_break(text)

        for raw_line in text.splitlines():
            left = clean_text(raw_line[:column_break])
            right = clean_text(raw_line[column_break:])

            if left:
                left_lines.append(left)

            if right:
                right_lines.append(right)

        return left_lines, right_lines

    def _find_column_break(self, text: str) -> int:
        candidates = [
            len(line) - len(line.lstrip())
            for line in text.splitlines()
            if self._is_product_header(line) and len(line) - len(line.lstrip()) > 80
        ]

        return min(candidates, default=DEFAULT_COLUMN_BREAK)

    def _extract_quote_date(self, text: str) -> date | None:
        match = DATE_PATTERN.search(text)

        if match is None:
            return None

        return parse_br_date(match.group(1).replace(".", "/"))

    def _extract_category(self, line: str) -> str | None:
        key = _normalize_key(line)

        return _slugify(line) if key in CATEGORY_KEYS else None

    def _is_product_header(self, line: str) -> bool:
        normalized_line = _strip_accents(line).upper()

        return "PROC." in normalized_line or "PROC " in normalized_line

    def _extract_product(self, line: str) -> str | None:
        product = re.split(
            r"\s+-\s+(?=\(|CX|DZ|KG|PCT|SC|UND|PROC)",
            line,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]

        return clean_text(product)

    def _extract_unit(self, line: str) -> str | None:
        match = UNIT_PATTERN.search(line)

        if match is not None:
            return clean_text(match.group(1))

        inline_match = INLINE_UNIT_PATTERN.search(line)

        return clean_text(inline_match.group(1)) if inline_match is not None else None

    def _extract_procedencia(self, line: str) -> str | None:
        match = PROCEDENCE_PATTERN.search(_strip_accents(line).upper())

        return clean_text(match.group(1).strip(" .-")) if match is not None else None
