import re


def clean_text(value: str | None) -> str | None:
    """Remove espacos duplicados e normaliza texto vazio para None."""
    if value is None:
        return None

    cleaned_value = re.sub(r"\s+", " ", value).strip()

    return cleaned_value or None
