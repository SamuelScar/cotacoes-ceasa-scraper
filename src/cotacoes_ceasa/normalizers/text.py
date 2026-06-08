import re
import unicodedata


def clean_text(value: str | None) -> str | None:
    """Remove espacos duplicados e normaliza texto vazio para None."""
    if value is None:
        return None

    cleaned_value = re.sub(r"\s+", " ", value).strip()

    return cleaned_value or None


def normalize_key(value: str | None) -> str:
    """Normaliza texto para comparacoes que ignoram formatacao."""
    return re.sub(r"[^a-z0-9]+", "", strip_accents(value or "").lower())


def slugify(value: str) -> str:
    """Converte texto em um identificador simples separado por hifens."""
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(value).lower()).strip("-")


def strip_accents(value: str) -> str:
    """Remove acentos sem alterar os demais caracteres do texto."""
    normalized = unicodedata.normalize("NFKD", value)

    return normalized.encode("ascii", "ignore").decode("ascii")
