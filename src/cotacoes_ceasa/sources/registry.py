from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from cotacoes_ceasa.collectors.ceasa_ba import CEASA_BA_HEADERS, CeasaBaCollector
from cotacoes_ceasa.collectors.ceasa_campinas import (
    CEASA_CAMPINAS_HEADERS,
    CeasaCampinasCollector,
)
from cotacoes_ceasa.collectors.ceasa_ce import CEASA_CE_HEADERS, CeasaCeCollector
from cotacoes_ceasa.collectors.ceasa_df import CEASA_DF_HEADERS, CeasaDfCollector
from cotacoes_ceasa.collectors.ceasa_es import CEASA_ES_HEADERS, CeasaEsCollector
from cotacoes_ceasa.collectors.ceasa_go import CEASA_GO_HEADERS, CeasaGoCollector
from cotacoes_ceasa.collectors.ceasa_mg import CeasaMgCollector
from cotacoes_ceasa.collectors.ceasa_pe import CeasaPeCollector
from cotacoes_ceasa.collectors.ceasa_pr import CEASA_PR_HEADERS, CeasaPrCollector
from cotacoes_ceasa.collectors.ceasa_rj import CEASA_RJ_HEADERS, CeasaRjCollector
from cotacoes_ceasa.collectors.ceagesp_sp import (
    CEAGESP_SP_HEADERS,
    CeagespSpCollector,
)
from cotacoes_ceasa.core.contracts import SourceCollector, SourceParser
from cotacoes_ceasa.http.client import HttpClient
from cotacoes_ceasa.parsers.ceasa_ba import CeasaBaParser
from cotacoes_ceasa.parsers.ceasa_campinas import CeasaCampinasParser
from cotacoes_ceasa.parsers.ceasa_ce import CeasaCeParser
from cotacoes_ceasa.parsers.ceasa_df import CeasaDfParser
from cotacoes_ceasa.parsers.ceasa_es import CeasaEsParser
from cotacoes_ceasa.parsers.ceasa_go import CeasaGoParser
from cotacoes_ceasa.parsers.ceasa_mg import CeasaMgParser
from cotacoes_ceasa.parsers.ceasa_pe import CeasaPeParser
from cotacoes_ceasa.parsers.ceasa_pr import CeasaPrParser
from cotacoes_ceasa.parsers.ceasa_rj import CeasaRjParser
from cotacoes_ceasa.parsers.ceagesp_sp import CeagespSpParser
from cotacoes_ceasa.sources.history import (
    history_requested,
    resolve_unsupported_history_error,
    source_supports_history as supports_source_history,
)
from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


@dataclass(frozen=True)
class SourceDefinition:
    """Dependencias e particularidades necessarias para construir uma fonte."""

    collector_type: type
    parser_type: type
    headers: dict[str, str] | None = None
    receives_target_date: bool = False


SOURCE_DEFINITIONS = {
    "ceasa-pe": SourceDefinition(CeasaPeCollector, CeasaPeParser),
    "ceasa-mg": SourceDefinition(CeasaMgCollector, CeasaMgParser),
    "ceasa-pr": SourceDefinition(
        CeasaPrCollector,
        CeasaPrParser,
        headers=CEASA_PR_HEADERS,
        receives_target_date=True,
    ),
    "ceasa-campinas": SourceDefinition(
        CeasaCampinasCollector,
        CeasaCampinasParser,
        headers=CEASA_CAMPINAS_HEADERS,
    ),
    "ceasa-go": SourceDefinition(
        CeasaGoCollector,
        CeasaGoParser,
        headers=CEASA_GO_HEADERS,
    ),
    "ceasa-ce": SourceDefinition(
        CeasaCeCollector,
        CeasaCeParser,
        headers=CEASA_CE_HEADERS,
    ),
    "ceasa-rj": SourceDefinition(
        CeasaRjCollector,
        CeasaRjParser,
        headers=CEASA_RJ_HEADERS,
    ),
    "ceasa-ba": SourceDefinition(
        CeasaBaCollector,
        CeasaBaParser,
        headers=CEASA_BA_HEADERS,
    ),
    "ceasa-df": SourceDefinition(
        CeasaDfCollector,
        CeasaDfParser,
        headers=CEASA_DF_HEADERS,
    ),
    "ceagesp-sp": SourceDefinition(
        CeagespSpCollector,
        CeagespSpParser,
        headers=CEAGESP_SP_HEADERS,
    ),
    "ceasa-es": SourceDefinition(
        CeasaEsCollector,
        CeasaEsParser,
        headers=CEASA_ES_HEADERS,
    ),
}


def build_registered_collector(
    source_slug: str,
    base_url: str,
    raw_dir: Path,
    http_timeout_seconds: int,
    request_delay_seconds: float,
    reuse_raw_before_request: bool,
    target_date: date | None,
    quotes_back: int | None,
) -> SourceCollector:
    """Constroi o coletor registrado para a fonte informada."""
    definition = _get_source_definition(source_slug)
    unsupported_history_error = resolve_unsupported_history_error(source_slug)

    if history_requested(quotes_back) and unsupported_history_error:
        raise ValueError(unsupported_history_error)

    http_client_options: dict[str, Any] = {
        "timeout_seconds": http_timeout_seconds,
        "request_delay_seconds": request_delay_seconds,
    }

    if definition.headers is not None:
        http_client_options["headers"] = definition.headers

    collector_options: dict[str, Any] = {
        "http_client": HttpClient(**http_client_options),
        "raw_storage": RawHtmlStorage(raw_dir),
        "parser": definition.parser_type(),
        "base_url": base_url,
        "reuse_raw_before_request": reuse_raw_before_request,
    }

    if definition.receives_target_date:
        collector_options["target_date"] = target_date

    return definition.collector_type(**collector_options)


def build_source_parser(source_slug: str) -> SourceParser:
    """Constroi o parser registrado para a fonte informada."""
    return _get_source_definition(source_slug).parser_type()


def source_supports_history(source_slug: str) -> bool:
    """Informa se a fonte registrada aceita cotacoes anteriores."""
    _get_source_definition(source_slug)
    return supports_source_history(source_slug)


def _get_source_definition(source_slug: str) -> SourceDefinition:
    try:
        return SOURCE_DEFINITIONS[source_slug]
    except KeyError as error:
        raise ValueError(f"Fonte nao suportada: {source_slug}") from error
