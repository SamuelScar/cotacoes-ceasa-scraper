import re
import unicodedata
from datetime import date
from io import BytesIO
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from cotacoes_ceasa.models import Category, Cotacao
from cotacoes_ceasa.normalizers.date import parse_br_date
from cotacoes_ceasa.normalizers.money import parse_brl_money
from cotacoes_ceasa.normalizers.text import clean_text


DATE_PATTERN = re.compile(r"Data da Coleta:\s*(\d{2}/\d{2}/\d{4})")
MONEY_PATTERN = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")
UNIT_START_PATTERN = re.compile(
    r"\b(?:"
    r"cx|sc|un|pct|bdj|kg|fardo|saco|caixa|bandeja|molho|ma.o|"
    r"\d+(?:,\d+)?\s*(?:kg|g|l|ml|un|unid)|"
    r"\d+\s*UNID"
    r")\b",
    re.IGNORECASE,
)
HEADER_PREFIXES = ("Centrais de Abastecimentos", "Mercado do Produtor")
HEADER_KEYS = {
    "centraisdeabastecimentos",
    "mercadodoprodutor",
    "coletadeprecos",
    "datadacoleta",
    "produtotipounidade",
    "embalagem",
    "situacao",
    "mercadomin",
    "diamax",
    "anteriorvar",
    "pagina",
}
SITUATION_KEYS = {"estavel", "firme", "fraco", "ausente", "alta", "baixa", "normal"}
COMMON_CLASSIFICATION_STARTS = {
    "comum",
    "grauda",
    "graudo",
    "importada",
    "importado",
    "media",
    "medio",
    "nacional",
    "pequena",
    "pequeno",
}


class CeasaPrParser:
    """Extrai cotacoes dos PDFs diarios da CEASA-PR."""

    def parse_categories(self, html: str, base_url: str) -> tuple[Category, ...]:
        soup = BeautifulSoup(html, "lxml")
        categories: list[Category] = []
        seen_slugs: set[str] = set()

        for spoiler in soup.find_all("div", class_="spoiler"):
            if not self._is_category_spoiler(spoiler):
                continue

            name = self._get_direct_spoiler_title(spoiler)
            slug = _slugify(name)

            if not slug or slug in seen_slugs:
                continue

            categories.append(Category(slug=slug, name=name))
            seen_slugs.add(slug)

        return tuple(categories)

    def find_pdf_url(
        self,
        html: str,
        year_url: str,
        city_slug: str,
        target_date: date,
        latest_when_missing: bool = False,
    ) -> str:
        soup = BeautifulSoup(html, "lxml")
        city_spoiler = self._find_category_spoiler(soup, city_slug)
        pdf_url = self._find_pdf_link_by_href_month(
            city_spoiler,
            year_url,
            target_date,
        )

        if pdf_url is not None:
            return pdf_url

        month_spoiler = self._find_month_spoiler_by_order(
            city_spoiler,
            target_date.month,
        )
        pdf_url = self._find_pdf_link_in_spoiler(month_spoiler, year_url, target_date)

        if pdf_url is not None:
            return pdf_url

        if latest_when_missing:
            pdf_url = self._find_latest_pdf_link(city_spoiler, year_url, target_date)

            if pdf_url is not None:
                return pdf_url

        city_name = self._get_direct_spoiler_title(city_spoiler) or city_slug

        raise ValueError(
            "PDF da CEASA-PR nao encontrado para "
            f"{city_name} em {target_date.isoformat()}."
        )

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
            line = raw_line.rstrip()
            stripped_line = clean_text(line)

            if not stripped_line or self._is_header_line(stripped_line):
                continue

            if not MONEY_PATTERN.search(stripped_line):
                current_product = stripped_line
                continue

            cotacao = self._parse_price_line(
                raw_line=line,
                cleaned_line=stripped_line,
                current_product=current_product,
                category_slug=category_slug,
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
        match = DATE_PATTERN.search(text)

        if match is None:
            return None

        return parse_br_date(match.group(1))

    def _parse_price_line(
        self,
        raw_line: str,
        cleaned_line: str,
        current_product: str | None,
        category_slug: str,
        data_cotacao: date | None,
        url_origem: str,
    ) -> Cotacao | None:
        money_matches = list(MONEY_PATTERN.finditer(cleaned_line))

        if len(money_matches) < 4:
            return None

        prefix = cleaned_line[: money_matches[0].start()].strip()
        suffix_start = (
            money_matches[4].end()
            if len(money_matches) >= 5
            else money_matches[3].end()
        )
        procedencia = self._clean_procedencia(cleaned_line[suffix_start:])
        description, situacao_mercado = self._split_situation(prefix)
        classification_text, unidade = self._split_unit(description)
        starts_with_indent = bool(raw_line[:1].isspace())
        produto, classificacao = self._split_product_classification(
            classification_text,
            current_product,
            starts_with_indent,
        )

        if not produto:
            return None

        return Cotacao(
            fonte="CEASA-PR",
            categoria=category_slug,
            produto=produto,
            unidade=unidade,
            procedencia=procedencia,
            classificacao=classificacao,
            preco_minimo=parse_brl_money(money_matches[0].group(0)),
            preco_comum=parse_brl_money(money_matches[1].group(0)),
            preco_maximo=parse_brl_money(money_matches[2].group(0)),
            situacao_mercado=situacao_mercado,
            data_cotacao=data_cotacao,
            url_origem=url_origem,
        )

    def _split_situation(self, value: str) -> tuple[str, str | None]:
        description, separator, situation = value.rpartition(" ")

        if not separator:
            return value, None

        if _normalize_key(situation) not in SITUATION_KEYS:
            return value, None

        return description.strip(), situation

    def _split_unit(self, value: str) -> tuple[str, str | None]:
        match = UNIT_START_PATTERN.search(value)

        if match is None:
            return value, None

        description = clean_text(value[: match.start()])
        unidade = clean_text(value[match.start() :])

        return description or "", unidade

    def _split_product_classification(
        self,
        value: str,
        current_product: str | None,
        starts_with_indent: bool,
    ) -> tuple[str | None, str | None]:
        cleaned_value = clean_text(value)

        if cleaned_value is None:
            return current_product, None

        if self._should_use_current_product(
            cleaned_value,
            current_product,
            starts_with_indent,
        ):
            return current_product, cleaned_value

        product, _, classification = cleaned_value.partition(" ")

        return product, clean_text(classification)

    def _should_use_current_product(
        self,
        value: str,
        current_product: str | None,
        starts_with_indent: bool,
    ) -> bool:
        if current_product is None:
            return False

        if starts_with_indent:
            return True

        words = [_normalize_key(word) for word in value.split()]

        if not words:
            return True

        if len(words) >= 2 and words[0] == words[1]:
            return True

        return words[0] in COMMON_CLASSIFICATION_STARTS

    def _clean_procedencia(self, value: str) -> str | None:
        cleaned_value = value

        for marker in HEADER_PREFIXES:
            cleaned_value = cleaned_value.split(marker, 1)[0]

        return clean_text(cleaned_value)

    def _find_category_spoiler(self, soup: BeautifulSoup, city_slug: str):
        for spoiler in soup.find_all("div", class_="spoiler"):
            if not self._is_category_spoiler(spoiler):
                continue

            title = self._get_direct_spoiler_title(spoiler)

            if _slugify(title) == city_slug:
                return spoiler

        raise ValueError(f"Cidade da CEASA-PR nao encontrada: {city_slug}.")

    def _find_pdf_link_by_href_month(
        self,
        city_spoiler,
        year_url: str,
        target_date: date,
    ) -> str | None:
        expected_folder = f"/{target_date.year}-{target_date.month:02d}/"

        for link in city_spoiler.find_all("a", href=True):
            href = link["href"]

            if expected_folder not in href:
                continue

            if self._is_pdf_link_for_day(link, target_date.day):
                return urljoin(year_url, href)

        return None

    def _find_month_spoiler_by_order(self, city_spoiler, month: int):
        month_spoilers = [
            spoiler
            for spoiler in city_spoiler.find_all("div", class_="spoiler")
            if not self._is_category_spoiler(spoiler)
        ]

        if len(month_spoilers) < month:
            city_name = self._get_direct_spoiler_title(city_spoiler)
            raise ValueError(f"Mes {month:02d} nao encontrado para {city_name}.")

        return month_spoilers[month - 1]

    def _find_pdf_link_in_spoiler(
        self,
        spoiler,
        year_url: str,
        target_date: date,
    ) -> str | None:
        for link in spoiler.find_all("a", href=True):
            if self._is_pdf_link_for_day(link, target_date.day):
                return urljoin(year_url, link["href"])

        return None

    def _find_latest_pdf_link(
        self,
        city_spoiler,
        year_url: str,
        target_date: date,
    ) -> str | None:
        candidates: list[tuple[date, str]] = []

        for link in city_spoiler.find_all("a", href=True):
            link_date = self._parse_pdf_link_date(link)

            if link_date is None or link_date > target_date:
                continue

            candidates.append((link_date, urljoin(year_url, link["href"])))

        if not candidates:
            return None

        return max(candidates, key=lambda item: item[0])[1]

    def _is_pdf_link_for_day(self, link, day: int) -> bool:
        href = link["href"].split("?", 1)[0].lower()

        if not href.endswith(".pdf"):
            return False

        label = clean_text(link.get_text(" ", strip=True)) or ""
        day_match = re.search(r"\b(\d{1,2})\b", label)

        return day_match is not None and int(day_match.group(1)) == day

    def _parse_pdf_link_date(self, link) -> date | None:
        href = link["href"].split("?", 1)[0].lower()

        if not href.endswith(".pdf"):
            return None

        month_match = re.search(r"/(20\d{2})-(\d{2})/", href)

        if month_match is None:
            return None

        label = clean_text(link.get_text(" ", strip=True)) or ""
        day_match = re.search(r"\b(\d{1,2})\b", label)

        if day_match is None:
            return None

        return date(
            int(month_match.group(1)),
            int(month_match.group(2)),
            int(day_match.group(1)),
        )

    def _get_direct_spoiler_title(self, spoiler) -> str:
        for child in spoiler.children:
            if getattr(child, "name", None) != "div":
                continue

            if "spoiler-title" not in child.get("class", []):
                continue

            return clean_text(child.get_text(" ", strip=True)) or ""

        return ""

    def _get_direct_spoiler_content(self, spoiler):
        for child in spoiler.children:
            if getattr(child, "name", None) != "div":
                continue

            if "spoiler-content" in child.get("class", []):
                return child

        return None

    def _is_category_spoiler(self, spoiler) -> bool:
        title = self._get_direct_spoiler_title(spoiler)
        content = self._get_direct_spoiler_content(spoiler)

        return bool(title and content is not None and content.find("div", class_="btgrid"))

    def _is_header_line(self, line: str) -> bool:
        line_key = _normalize_key(line)

        return any(line_key.startswith(prefix) for prefix in HEADER_KEYS)


def _normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")

    return re.sub(r"[^a-z0-9]+", "", ascii_value.lower())


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()

    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
