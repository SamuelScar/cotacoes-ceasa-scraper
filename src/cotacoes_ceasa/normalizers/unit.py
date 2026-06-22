import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

from cotacoes_ceasa.normalizers.text import clean_text


@dataclass(frozen=True)
class NormalizedUnit:
    original: str | None
    normalized: str | None
    symbol: str | None
    description: str | None
    packaging: str | None
    quantity_min: Decimal | None
    quantity_max: Decimal | None
    detail: str | None


MEASURES = (
    (
        "kg",
        "Quilograma",
        r"(?<![a-z])(?:kg|kgs|kgr|quilo|quilos|quilograma|quilogramas)(?![a-z])",
    ),
    ("ml", "Mililitro", r"(?<![a-z])(?:ml|mililitro|mililitros)(?![a-z])"),
    ("l", "Litro", r"(?<![a-z])(?:l|lt|litro|litros)(?![a-z])"),
    ("g", "Grama", r"(?<![a-z])(?:g|gr|grama|gramas)(?![a-z])"),
    ("un", "Unidade", r"(?<![a-z])(?:un|und|unid|unidade|unidades)\.?(?![a-z])"),
    ("dz", "Duzia", r"(?<![a-z])(?:dz|duzia|duzias)(?![a-z])"),
    ("cento", "Cento", r"(?<![a-z])cento(?![a-z])"),
    ("espiga", "Espiga", r"(?<![a-z])espigas?(?![a-z])"),
    ("maco", "Maco", r"(?<![a-z])(?:maco|mc)(?![a-z])"),
    ("molho", "Molho", r"(?<![a-z])molhos?(?![a-z])"),
    ("pe", "Pe", r"(?<![a-z])pes?(?![a-z])"),
)

PACKAGINGS = (
    ("caixa", "cx", "Caixa", r"(?<![a-z])(?:cx|caixa)(?![a-z])"),
    ("saco", "sc", "Saco", r"(?<![a-z])(?:sc|sco|saco)(?![a-z])"),
    ("fardo", "fardo", "Fardo", r"(?<![a-z])(?:frd|fardo)(?![a-z])"),
    ("engradado", "eng", "Engradado", r"(?<![a-z])(?:eng|engradado)(?![a-z])"),
    ("bandeja", "bj", "Bandeja", r"(?<![a-z])(?:bj|bandeja)(?![a-z])"),
    ("pacote", "pct", "Pacote", r"(?<![a-z])(?:pct|pacote)(?![a-z])"),
    ("lata", "lata", "Lata", r"(?<![a-z])latas?(?![a-z])"),
    ("vidro", "vidro", "Vidro", r"(?<![a-z])(?:vd|vidro)(?![a-z])"),
    ("vasilhame", "vasilhame", "Vasilhame", r"(?<![a-z])(?:vs|vasilhame)(?![a-z])"),
    ("maco", "maco", "Maco", r"(?<![a-z])(?:maco|mc)(?![a-z])"),
    ("molho", "molho", "Molho", r"(?<![a-z])molhos?(?![a-z])"),
)

NUMBER_PATTERN = r"\d+(?:\.\d+)?"
RANGE_BEFORE_PATTERN = re.compile(
    rf"(?P<min>{NUMBER_PATTERN})(?:\s*a\s*(?P<max>{NUMBER_PATTERN}))?\s*$"
)
RANGE_AFTER_PATTERN = re.compile(
    rf"^\s*(?P<min>{NUMBER_PATTERN})(?:\s*a\s*(?P<max>{NUMBER_PATTERN}))?"
)


@lru_cache(maxsize=4096)
def normalize_unit(value: str | None) -> NormalizedUnit:
    original = clean_text(value)

    if original is None:
        return NormalizedUnit(None, None, None, None, None, None, None, None)

    normalized_text = _normalize_text(original)
    packaging_matches = _find_packagings(normalized_text)
    measure_match = _find_last_match(normalized_text, MEASURES)

    if measure_match is None:
        return _normalize_without_measure(
            original=original,
            normalized_text=normalized_text,
            packaging_matches=packaging_matches,
        )

    symbol, description, match = measure_match
    quantity_min, quantity_max, quantity_span = _find_quantity(normalized_text, match)
    explicit_quantity = quantity_span is not None

    if quantity_min is None:
        quantity_min = Decimal("1")

    packaging = packaging_matches[0][0] if packaging_matches else None
    removal_spans = [match.span()]

    if quantity_span is not None:
        removal_spans.append(quantity_span)

    removal_spans.extend(item[3].span() for item in packaging_matches)
    detail = _build_detail(normalized_text, removal_spans)
    normalized = _build_normalized_display(
        symbol=symbol,
        packaging=packaging,
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        explicit_quantity=explicit_quantity,
        detail=detail,
    )

    return NormalizedUnit(
        original=original,
        normalized=normalized,
        symbol=symbol,
        description=description,
        packaging=packaging,
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        detail=detail,
    )


def _normalize_without_measure(
    original: str,
    normalized_text: str,
    packaging_matches: list[tuple[str, str, str, re.Match[str]]],
) -> NormalizedUnit:
    if not packaging_matches:
        detail = _clean_detail(normalized_text)

        return NormalizedUnit(
            original=original,
            normalized=detail,
            symbol=None,
            description=None,
            packaging=None,
            quantity_min=None,
            quantity_max=None,
            detail=detail,
        )

    packaging, _, _, match = packaging_matches[0]
    quantity_min, quantity_max, quantity_span = _find_quantity(
        normalized_text,
        match,
    )
    explicit_quantity = quantity_span is not None

    if quantity_min is None:
        quantity_min = Decimal("1")

    removal_spans = [item[3].span() for item in packaging_matches]

    if quantity_span is not None:
        removal_spans.append(quantity_span)

    detail = _build_detail(normalized_text, removal_spans)
    normalized = _build_normalized_display(
        symbol=None,
        packaging=packaging,
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        explicit_quantity=explicit_quantity,
        detail=detail,
    )

    return NormalizedUnit(
        original=original,
        normalized=normalized,
        symbol=None,
        description=None,
        packaging=packaging,
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        detail=detail,
    )


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    decimal_value = re.sub(r"(?<=\d),(?=\d)", ".", ascii_value)

    return re.sub(r"\s+", " ", decimal_value).strip()


def _find_packagings(value: str) -> list[tuple[str, str, str, re.Match[str]]]:
    matches: list[tuple[str, str, str, re.Match[str]]] = []

    for packaging, symbol, description, pattern in PACKAGINGS:
        for match in re.finditer(pattern, value):
            matches.append((packaging, symbol, description, match))

    return sorted(matches, key=lambda item: item[3].start())


def _find_last_match(
    value: str,
    definitions: tuple[tuple[str, str, str], ...],
) -> tuple[str, str, re.Match[str]] | None:
    matches = [
        (symbol, description, match)
        for symbol, description, pattern in definitions
        for match in re.finditer(pattern, value)
    ]

    return max(matches, key=lambda item: item[2].start(), default=None)


def _find_quantity(
    value: str,
    unit_match: re.Match[str],
) -> tuple[Decimal | None, Decimal | None, tuple[int, int] | None]:
    prefix = value[: unit_match.start()].rstrip(" .-/")
    before_match = RANGE_BEFORE_PATTERN.search(prefix)

    if before_match is not None:
        return _parse_quantity_match(before_match, 0)

    suffix = value[unit_match.end() :].lstrip(" .-/")
    after_match = RANGE_AFTER_PATTERN.match(suffix)

    if after_match is None:
        return None, None, None

    suffix_start = unit_match.end() + len(value[unit_match.end() :]) - len(suffix)

    return _parse_quantity_match(after_match, suffix_start)


def _parse_quantity_match(
    match: re.Match[str],
    offset: int,
) -> tuple[Decimal, Decimal | None, tuple[int, int]]:
    quantity_min = Decimal(match.group("min"))
    quantity_max = Decimal(match.group("max")) if match.group("max") else None
    span = (offset + match.start(), offset + match.end())

    return quantity_min, quantity_max, span


def _build_detail(value: str, removal_spans: list[tuple[int, int]]) -> str | None:
    remaining = list(value)

    for start, end in removal_spans:
        for index in range(start, end):
            remaining[index] = " "

    return _clean_detail("".join(remaining))


def _clean_detail(value: str) -> str | None:
    cleaned = value.replace("c/", "com ")
    cleaned = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", cleaned)
    cleaned = re.sub(r"[\s._/,;-]+", " ", cleaned)

    return clean_text(cleaned)


def _build_normalized_display(
    symbol: str | None,
    packaging: str | None,
    quantity_min: Decimal,
    quantity_max: Decimal | None,
    explicit_quantity: bool,
    detail: str | None,
) -> str:
    parts: list[str] = []

    if packaging:
        parts.append(packaging)

    if explicit_quantity:
        quantity = _format_decimal(quantity_min)

        if quantity_max is not None:
            quantity = f"{quantity} a {_format_decimal(quantity_max)}"

        parts.append(quantity)

    if symbol and symbol not in _packaging_symbols():
        parts.append(symbol)

    normalized = " ".join(parts)

    return f"{normalized} ({detail})" if detail else normalized


@lru_cache(maxsize=1)
def _packaging_symbols() -> frozenset[str]:
    return frozenset(symbol for _, symbol, _, _ in PACKAGINGS)


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
