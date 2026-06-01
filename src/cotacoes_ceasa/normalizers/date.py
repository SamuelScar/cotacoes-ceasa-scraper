from datetime import date, datetime


def parse_br_date(value: str | None) -> date | None:
    """Converte uma data no formato brasileiro para `date`."""
    if not value:
        return None

    return datetime.strptime(value.strip(), "%d/%m/%Y").date()
