from io import BytesIO
import json
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


PDF_TEXT_CACHE_VERSION = "pypdf-layout-v1"


class PdfTextDependencyError(RuntimeError):
    """Indica que uma dependencia de extracao de PDF nao esta instalada."""


@dataclass
class PdfTextCacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    fallbacks: int = 0


_pdf_text_cache_dir: Path | None = None
_pdf_text_cache_stats = PdfTextCacheStats()


def configure_pdf_text_cache(cache_dir: Path | None) -> None:
    """Configura o diretorio usado para reutilizar textos extraidos de PDFs."""
    global _pdf_text_cache_dir
    _pdf_text_cache_dir = cache_dir


def reset_pdf_text_cache_stats() -> None:
    global _pdf_text_cache_stats
    _pdf_text_cache_stats = PdfTextCacheStats()


def get_pdf_text_cache_stats() -> PdfTextCacheStats:
    return PdfTextCacheStats(
        hits=_pdf_text_cache_stats.hits,
        misses=_pdf_text_cache_stats.misses,
        writes=_pdf_text_cache_stats.writes,
        fallbacks=_pdf_text_cache_stats.fallbacks,
    )


def extract_pdf_pages(content: bytes) -> list[str]:
    """Extrai o texto de cada pagina preservando o layout quando disponivel."""
    cache_key = sha256(content).hexdigest()
    cached_pages = _read_cached_pages(cache_key)

    if cached_pages is not None:
        _pdf_text_cache_stats.hits += 1
        return cached_pages

    _pdf_text_cache_stats.misses += 1

    try:
        texts = _extract_pdf_pages_with_pypdf(content)
    except PdfTextDependencyError:
        raise
    except Exception as error:
        texts = _extract_pdf_pages_with_pdftotext(content, error)
        _pdf_text_cache_stats.fallbacks += 1

    _write_cached_pages(cache_key, texts)

    return texts


def _extract_pdf_pages_with_pypdf(content: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as error:
        raise PdfTextDependencyError(
            "Dependencia pypdf nao instalada. "
            "Instale as dependencias atualizadas do projeto."
        ) from error

    reader = PdfReader(BytesIO(content))
    texts: list[str] = []

    for page in reader.pages:
        try:
            page_text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            page_text = page.extract_text() or ""

        texts.append(page_text)

    return texts


def _extract_pdf_pages_with_pdftotext(
    content: bytes,
    original_error: Exception,
) -> list[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "input.pdf"
        pdf_path.write_bytes(content)
        result = _run_pdftotext(pdf_path)

    text = result.stdout.decode("utf-8", errors="replace")
    pages = _split_pdftotext_pages(text)

    if pages:
        return pages

    raise original_error


def _run_pdftotext(pdf_path: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise PdfTextDependencyError(
            "Dependencia pdftotext nao instalada. "
            "Instale poppler-utils na imagem do projeto."
        ) from error


def _split_pdftotext_pages(text: str) -> list[str]:
    pages = text.split("\f")

    if pages and not pages[-1].strip():
        pages.pop()

    return [page for page in pages if page.strip()]


def extract_pdf_text(content: bytes) -> str:
    """Extrai e concatena o texto de todas as paginas de um PDF."""
    return "\n".join(extract_pdf_pages(content))


def _read_cached_pages(cache_key: str) -> list[str] | None:
    cache_path = _build_cache_path(cache_key)

    if cache_path is None or not cache_path.exists():
        return None

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if data.get("version") != PDF_TEXT_CACHE_VERSION:
        return None

    pages = data.get("pages")

    if not isinstance(pages, list) or not all(isinstance(page, str) for page in pages):
        return None

    return pages


def _write_cached_pages(cache_key: str, pages: list[str]) -> None:
    cache_path = _build_cache_path(cache_key)

    if cache_path is None:
        return

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(".tmp")
    payload = {
        "version": PDF_TEXT_CACHE_VERSION,
        "pages": pages,
    }

    try:
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(cache_path)
    except OSError:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        return

    _pdf_text_cache_stats.writes += 1


def _build_cache_path(cache_key: str) -> Path | None:
    if _pdf_text_cache_dir is None:
        return None

    return _pdf_text_cache_dir / cache_key[:2] / f"{cache_key}.json"
