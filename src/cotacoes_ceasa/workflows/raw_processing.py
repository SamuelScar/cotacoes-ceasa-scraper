import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from urllib.parse import urlencode

from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.progress import ProgressReporter
from cotacoes_ceasa.core.contracts import SourceParser
from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.parsers.pdf import (
    configure_pdf_text_cache,
    get_pdf_text_cache_stats,
    reset_pdf_text_cache_stats,
)
from cotacoes_ceasa.storage.sqlite import SQLiteStorage


RAW_FILE_PATTERN = re.compile(
    r"^(?P<storage_category>.+)_(?P<downloaded_at>\d{8}_\d{6})\.(?:html|pdf)$"
)
RAW_CATEGORY_DATE_PATTERN = re.compile(r"_(?P<target_date>\d{4}-\d{2}-\d{2})$")


@dataclass(frozen=True)
class RawFileMetadata:
    category_slug: str
    target_date: date | None
    downloaded_at: datetime


@dataclass
class RawProcessingStats:
    selected_files: int = 0
    processed_files: int = 0
    quoted_files: int = 0
    empty_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    parsed_quotes: int = 0
    read_seconds: float = 0.0
    hash_seconds: float = 0.0
    skip_lookup_seconds: float = 0.0
    parser_seconds: float = 0.0
    metadata_seconds: float = 0.0


def process_raw_and_report(
    parser: SourceParser,
    raw_dir: Path,
    source_slug: str,
    base_url: str,
    database_path: Path,
    pdf_text_cache_dir: Path,
    force_reprocess: bool = False,
    raw_detail_report: bool = False,
    output: TerminalOutput | None = None,
    raw_files: list[Path] | None = None,
) -> list[Cotacao]:
    """Processa arquivos brutos salvos em disco e retorna cotacoes normalizadas."""
    output = output or TerminalOutput()
    processing_started_at = perf_counter()
    selected_raw_files = (
        list_raw_files(raw_dir, source_slug)
        if raw_files is None
        else sorted(raw_files)
    )

    if not selected_raw_files and raw_files is None:
        raise RuntimeError(f"Nenhum arquivo bruto encontrado em {raw_dir / source_slug}.")

    cotacoes: list[Cotacao] = []
    output.section("Processamento de arquivos brutos")
    if raw_files is None:
        output.info(
            f"{len(selected_raw_files)} arquivo(s) encontrado(s) "
            f"em {raw_dir / source_slug}."
        )
    else:
        output.info(
            f"{len(selected_raw_files)} arquivo(s) selecionado(s) nesta coleta."
        )

    stats = RawProcessingStats(selected_files=len(selected_raw_files))
    lookup_started_at = perf_counter()
    processed_raws = (
        set()
        if force_reprocess
        else SQLiteStorage(database_path).find_processed_raw_hashes(selected_raw_files)
    )
    stats.skip_lookup_seconds += perf_counter() - lookup_started_at
    configure_pdf_text_cache(pdf_text_cache_dir)
    reset_pdf_text_cache_stats()

    with ProgressReporter(output) as progress:
        progress_task = progress.task(
            label=f"Processamento {source_slug}",
            total=len(selected_raw_files),
            unit="arquivo(s)",
        )

        for file_path in selected_raw_files:
            progress_task.update(current=file_path.name)

            try:
                metadata_started_at = perf_counter()
                metadata = parse_raw_document_metadata(file_path)
                stats.metadata_seconds += perf_counter() - metadata_started_at
                url_origem = build_raw_source_url(
                    source_slug,
                    base_url,
                    metadata.category_slug,
                    metadata.target_date,
                )
                read_started_at = perf_counter()
                raw_content = read_raw_file(file_path)
                stats.read_seconds += perf_counter() - read_started_at
                hash_started_at = perf_counter()
                raw_hash = build_raw_hash(raw_content)
                stats.hash_seconds += perf_counter() - hash_started_at

                lookup_started_at = perf_counter()
                already_processed = (
                    file_path.as_posix(),
                    raw_hash,
                ) in processed_raws
                stats.skip_lookup_seconds += perf_counter() - lookup_started_at

                if already_processed:
                    stats.skipped_files += 1
                    progress_task.advance(current=file_path.name)
                    continue

                parser_started_at = perf_counter()
                parsed_cotacoes = parser.parse_category(
                    raw_content,
                    metadata.category_slug,
                    url_origem,
                )
                stats.parser_seconds += perf_counter() - parser_started_at
                parsed_cotacoes = [
                    replace(
                        fill_missing_quote_date(
                            cotacao,
                            source_slug,
                            metadata.target_date,
                        ),
                        arquivo_raw=file_path.as_posix(),
                        hash_raw=raw_hash,
                        baixado_em=metadata.downloaded_at,
                    )
                    for cotacao in parsed_cotacoes
                ]
            except Exception as error:
                stats.failed_files += 1
                output.warning(f"{file_path.name} | {error}")
                progress_task.advance(current=file_path.name)
                continue

            cotacoes.extend(parsed_cotacoes)
            stats.processed_files += 1
            if parsed_cotacoes:
                stats.quoted_files += 1
            else:
                stats.empty_files += 1
            stats.parsed_quotes += len(parsed_cotacoes)
            raw_message = (
                f"{file_path.name} | {len(parsed_cotacoes)} cotacoes."
                if parsed_cotacoes
                else f"{file_path.name} | sem cotacao."
            )
            output.detail_success(raw_message, report=raw_detail_report)
            progress_task.advance(current=file_path.name)

        progress_task.finish()

    pdf_cache_stats = get_pdf_text_cache_stats()
    total_seconds = perf_counter() - processing_started_at
    output.report_summary(
        (
            ("Raws selecionados", stats.selected_files),
            ("Raws processados", stats.processed_files),
            ("Raws com cotacao", stats.quoted_files),
            ("Raws sem cotacao", stats.empty_files),
            ("Raws ignorados", stats.skipped_files),
            ("Raws com falha", stats.failed_files),
            ("Cotacoes extraidas dos raws", stats.parsed_quotes),
            ("Tempo total processamento raws (s)", f"{total_seconds:.2f}"),
            ("Tempo lendo raws (s)", f"{stats.read_seconds:.2f}"),
            ("Tempo calculando hashes (s)", f"{stats.hash_seconds:.2f}"),
            ("Tempo consultando cache SQLite (s)", f"{stats.skip_lookup_seconds:.2f}"),
            ("Tempo lendo metadados (s)", f"{stats.metadata_seconds:.2f}"),
            ("Tempo em parsers (s)", f"{stats.parser_seconds:.2f}"),
            ("Cache PDF acertos", pdf_cache_stats.hits),
            ("Cache PDF misses", pdf_cache_stats.misses),
            ("Cache PDF gravacoes", pdf_cache_stats.writes),
        ),
        report_title=f"Desempenho do processamento {source_slug}",
    )

    return cotacoes


def fill_missing_quote_date(
    cotacao: Cotacao,
    source_slug: str,
    target_date: date | None,
) -> Cotacao:
    if source_slug != "ceasa-pr":
        return cotacao

    if cotacao.data_cotacao is not None or target_date is None:
        return cotacao

    return replace(cotacao, data_cotacao=target_date)


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


def find_oldest_raw_target_date(
    raw_dir: Path,
    source_slug: str,
    category_slug: str | None = None,
) -> date | None:
    """Busca a data historica mais antiga representada nos raws ativos."""
    target_dates: list[date] = []

    for file_path in list_raw_files(raw_dir, source_slug):
        try:
            metadata = parse_raw_document_metadata(file_path)
        except ValueError:
            continue

        if metadata.target_date is None:
            continue

        if category_slug is not None and metadata.category_slug != category_slug:
            continue

        target_dates.append(metadata.target_date)

    return min(target_dates) if target_dates else None


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
