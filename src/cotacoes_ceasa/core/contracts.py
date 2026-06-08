from datetime import date
from pathlib import Path
from typing import Protocol

from cotacoes_ceasa.core.models import Category, Cotacao


class SourceCollector(Protocol):
    """Contrato usado pela orquestracao para operar qualquer fonte."""

    supports_target_dates: bool

    def discover_categories(self) -> tuple[Category, ...]: ...

    def collect_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
        save_raw: bool = True,
    ) -> list[Cotacao]: ...

    def download_category(
        self,
        category_slug: str,
        cotacao_date: date | None = None,
    ) -> Path: ...


class SourceParser(Protocol):
    """Contrato minimo para processar um arquivo bruto de qualquer fonte."""

    def parse_category(
        self,
        content: bytes | str,
        category_slug: str,
        url_origem: str,
    ) -> list[Cotacao]: ...
