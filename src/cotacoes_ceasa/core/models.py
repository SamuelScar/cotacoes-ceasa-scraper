from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class ColetaStatus(StrEnum):
    """Estados persistidos para uma tentativa de coleta."""

    PENDENTE = "pendente"
    PROCESSADA = "processada"
    DESCARTADA_DUPLICADA = "descartada_duplicada"
    ERRO_DOWNLOAD = "erro_download"
    ERRO_PROCESSAMENTO = "erro_processamento"


@dataclass(frozen=True)
class Category:
    """Categoria de cotacao descoberta em uma fonte."""

    slug: str
    name: str


@dataclass(frozen=True)
class Cotacao:
    """Registro de cotacao normalizado extraido de uma fonte."""

    fonte: str
    categoria: str | None
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
    coleta_id: int | None = None
    variedade: str | None = None
    entreposto: str | None = None
    arquivo_raw: str | None = None
    hash_raw: str | None = None
    baixado_em: datetime | None = None
    fonte_complemento: str | None = None
    url_complemento: str | None = None
    data_complemento: datetime | None = None


@dataclass(frozen=True)
class Coleta:
    """Tentativa de obter e processar um documento de uma fonte."""

    fonte: str
    url_origem: str
    iniciada_em: datetime
    status: ColetaStatus = ColetaStatus.PENDENTE
    sha256: str | None = None
    caminho_relativo_raw: str | None = None
    baixado_em: datetime | None = None
    processado_em: datetime | None = None
    duplicada_de_id: int | None = None
    mensagem_erro: str | None = None


@dataclass(frozen=True)
class CotacaoComplemento:
    """Alteracao feita por uma coleta complementar em uma cotacao existente."""

    cotacao_id: int
    coleta_id: int
    campo: str
    valor_anterior: str | None
    valor_novo: str
    complementado_em: datetime
