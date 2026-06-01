from decimal import Decimal


def parse_brl_money(value: str | None) -> Decimal | None:
    """Converte valor monetario brasileiro para Decimal."""
    if not value:
        return None

    cleaned_value = (
        value.replace("R$", "")
        .replace(".", "")
        .replace(",", ".")
        .strip()
    )

    return Decimal(cleaned_value) if cleaned_value else None
