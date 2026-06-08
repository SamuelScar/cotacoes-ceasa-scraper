import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.core.contracts import SourceParser
from cotacoes_ceasa.core.models import Cotacao


RAW_FILE_PATTERN = re.compile(
    r"^(?P<storage_category>.+)_(?P<downloaded_at>\d{8}_\d{6})\.(?:html|pdf)$"
)
RAW_CATEGORY_DATE_PATTERN = re.compile(r"_(?P<target_date>\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True)
class RawFileMetadata:
    category_slug: str
    target_date: date | None
    downloaded_at: datetime


def process_raw_and_report(
    parser: SourceParser,
    raw_dir: Path,
    source_slug: str,
    base_url: str,
    output: TerminalOutput | None = None,
) -> list[Cotacao]:
    """Processa arquivos brutos salvos em disco e retorna cotacoes normalizadas."""
    output = output or TerminalOutput()
    raw_files = list_raw_files(raw_dir, source_slug)

    if not raw_files:
        raise RuntimeError(f"Nenhum arquivo bruto encontrado em {raw_dir / source_slug}.")

    cotacoes: list[Cotacao] = []
    output.section("Processamento de arquivos brutos")
    output.info(f"{len(raw_files)} arquivo(s) encontrado(s) em {raw_dir / source_slug}.")

    for file_path in raw_files:
        try:
            metadata = parse_raw_document_metadata(file_path)
            url_origem = build_raw_source_url(
                source_slug,
                base_url,
                metadata.category_slug,
                metadata.target_date,
            )
            raw_content = read_raw_file(file_path)
            parsed_cotacoes = parser.parse_category(
                raw_content,
                metadata.category_slug,
                url_origem,
            )
            raw_hash = build_raw_hash(raw_content)
            parsed_cotacoes = [
                replace(
                    cotacao,
                    arquivo_raw=file_path.as_posix(),
                    hash_raw=raw_hash,
                    baixado_em=metadata.downloaded_at,
                )
                for cotacao in parsed_cotacoes
            ]
        except Exception as error:
            output.warning(f"{file_path.name} | {error}")
            continue

        cotacoes.extend(parsed_cotacoes)
        output.success(f"{file_path.name} | {len(parsed_cotacoes)} cotacoes.")

    return cotacoes


def read_raw_file(file_path: Path) -> bytes | str:
    """Le arquivo bruto em texto ou bytes conforme a extensao."""
    if file_path.suffix.lower() == ".pdf":
        return file_path.read_bytes()

    return file_path.read_text(encoding="utf-8")


def list_raw_files(raw_dir: Path, source_slug: str) -> list[Path]:
    """Lista os raws ativos da fonte, ignorando arquivos arquivados em `old`."""
    source_raw_dir = raw_dir / source_slug

    if not source_raw_dir.exists():
        return []

    return sorted(
        file_path
        for file_path in source_raw_dir.glob("*.*")
        if file_path.is_file() and file_path.suffix.lower() in {".html", ".pdf"}
    )


def parse_raw_file_metadata(file_path: Path) -> tuple[str, date | None]:
    """Extrai categoria e data limite a partir do nome do arquivo bruto."""
    metadata = parse_raw_document_metadata(file_path)

    return metadata.category_slug, metadata.target_date


def parse_raw_document_metadata(file_path: Path) -> RawFileMetadata:
    """Extrai metadados de origem representados pelo nome do arquivo bruto."""
    match = RAW_FILE_PATTERN.match(file_path.name)

    if match is None:
        raise ValueError("Nome de arquivo bruto fora do padrao esperado.")

    storage_category = match.group("storage_category")
    date_match = RAW_CATEGORY_DATE_PATTERN.search(storage_category)
    downloaded_at = datetime.strptime(match.group("downloaded_at"), "%Y%m%d_%H%M%S")

    if date_match is None:
        return RawFileMetadata(storage_category, None, downloaded_at)

    category_slug = storage_category[: date_match.start()]
    target_date = datetime.strptime(date_match.group("target_date"), "%Y-%m-%d").date()

    return RawFileMetadata(category_slug, target_date, downloaded_at)


def build_raw_hash(content: bytes | str) -> str:
    raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")

    return sha256(raw_bytes).hexdigest()


def build_category_url(
    base_url: str,
    category_slug: str,
    target_date: date | None,
) -> str:
    """Monta a URL original da categoria usada na coleta."""
    url = f"{base_url.rstrip('/')}/{category_slug}"

    if target_date is None:
        return url

    params = urlencode({"data": target_date.strftime("%d/%m/%Y")})

    return f"{url}?{params}"


def build_raw_source_url(
    source_slug: str,
    base_url: str,
    category_slug: str,
    target_date: date | None,
) -> str:
    """Reconstroi a URL de origem representada por um arquivo bruto."""
    if source_slug == "ceasa-pe":
        return build_category_url(base_url, category_slug, target_date)

    if source_slug == "ceasa-pr" and target_date is not None:
        return f"{base_url.rstrip('/')}-{target_date.year}"

    return base_url
