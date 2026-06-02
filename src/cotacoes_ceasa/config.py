import json
import os
from dataclasses import dataclass
from pathlib import Path


ENV_FILE = Path(".env")
SOURCES_FILE = Path("config/fontes.json")


@dataclass(frozen=True)
class AppConfig:
    """Configuracoes padrao usadas pela CLI do projeto."""

    source: str
    sources_file: str
    raw_dir: str
    database_path: str
    http_timeout_seconds: int
    request_delay_seconds: float
    reuse_raw_before_request: bool
    target_date: str | None
    quotes_back: int
    sources: dict[str, "SourceConfig"]


@dataclass(frozen=True)
class SourceConfig:
    """Configuracao de uma fonte de cotacoes."""

    name: str
    state: str
    uf: str
    city: str
    base_url: str


def load_config(env_file: Path = ENV_FILE) -> AppConfig:
    """Carrega configuracoes do `.env` local e permite sobrescrita pelo ambiente."""
    load_env_file(env_file)
    sources_file = os.getenv("COTACOES_SOURCES_FILE", str(SOURCES_FILE))

    return AppConfig(
        source=os.getenv("COTACOES_SOURCE", "ceasa-pe"),
        sources_file=sources_file,
        raw_dir=os.getenv("COTACOES_RAW_DIR", "data/raw"),
        database_path=os.getenv("COTACOES_DATABASE_PATH", "data/cotacoes.sqlite"),
        http_timeout_seconds=_get_int_env("COTACOES_HTTP_TIMEOUT_SECONDS", 30),
        request_delay_seconds=_get_float_env("COTACOES_REQUEST_DELAY_SECONDS", 2.0),
        reuse_raw_before_request=_get_bool_env(
            "COTACOES_REUSE_RAW_BEFORE_REQUEST",
            False,
        ),
        target_date=_get_optional_env("COTACOES_TARGET_DATE"),
        quotes_back=_get_int_env("COTACOES_QUOTES_BACK", 0),
        sources=load_sources(Path(sources_file)),
    )


def load_sources(sources_file: Path) -> dict[str, SourceConfig]:
    """Carrega as fontes disponiveis a partir de um arquivo JSON."""
    data = json.loads(sources_file.read_text(encoding="utf-8"))

    return {
        slug: SourceConfig(
            name=source["name"],
            state=source["state"],
            uf=source["uf"],
            city=source["city"],
            base_url=source["base_url"],
        )
        for slug, source in data.items()
    }


def load_env_file(env_file: Path) -> None:
    """Carrega pares `CHAVE=valor` de um arquivo `.env` simples."""
    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()

        if not stripped_line or stripped_line.startswith("#"):
            continue

        key, separator, value = stripped_line.partition("=")

        if not separator:
            continue

        os.environ.setdefault(key.strip(), _clean_env_value(value))


def _clean_env_value(value: str) -> str:
    cleaned_value = value.strip()

    if len(cleaned_value) >= 2 and cleaned_value[0] == cleaned_value[-1]:
        if cleaned_value[0] in {"'", '"'}:
            return cleaned_value[1:-1]

    return cleaned_value


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"Valor invalido para {name}: {value}") from error


def _get_float_env(name: str, default: float) -> float:
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"Valor invalido para {name}: {value}") from error


def _get_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    normalized_value = value.strip().lower()

    if normalized_value in {"1", "true", "yes", "y", "sim", "s"}:
        return True

    if normalized_value in {"0", "false", "no", "n", "nao", "não"}:
        return False

    raise ValueError(f"Valor invalido para {name}: {value}")


def _get_optional_env(name: str) -> str | None:
    value = os.getenv(name)

    if value is None:
        return None

    cleaned_value = value.strip()

    return cleaned_value or None
