from io import BytesIO


def extract_pdf_pages(content: bytes) -> list[str]:
    """Extrai o texto de cada pagina preservando o layout quando disponivel."""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as error:
        raise RuntimeError(
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


def extract_pdf_text(content: bytes) -> str:
    """Extrai e concatena o texto de todas as paginas de um PDF."""
    return "\n".join(extract_pdf_pages(content))
