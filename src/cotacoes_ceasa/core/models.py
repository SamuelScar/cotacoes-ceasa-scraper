from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Category:
    """Categoria de cotacao descoberta em uma fonte."""

    slug: str
    name: str


@dataclass(frozen=True)
class Cotacao:
    """Registro de cotacao normalizado extraido de uma fonte."""

    fonte: str
    categoria: str
    produto: str
    unidade: str | None
    procedencia: str | None
    classificacao: str | None
    data_cotacao: date | None
    preco_minimo: Decimal | None
    preco_comum: Decimal | None
    preco_maximo: Decimal | None
    situacao_mercado: str | None
    url_origem: str
    entreposto: str | None = None
    arquivo_raw: str | None = None
    hash_raw: str | None = None
    baixado_em: datetime | None = None
    fonte_complemento: str | None = None
    url_complemento: str | None = None
    data_complemento: datetime | None = None
