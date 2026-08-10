UNSUPPORTED_HISTORY_ERRORS = {
    "ceasa-mg": "CEASA-MG nao suporta cotacoes anteriores.",
    "ceasa-ce": "CEASA-CE nao suporta cotacoes anteriores.",
    "ceasa-df": "CEASA-DF nao suporta cotacoes anteriores.",
}


def history_requested(quotes_back: int | None) -> bool:
    return quotes_back is None or quotes_back > 0


def source_supports_history(source_slug: str) -> bool:
    return source_slug not in UNSUPPORTED_HISTORY_ERRORS


def resolve_unsupported_history_error(source_slug: str) -> str | None:
    return UNSUPPORTED_HISTORY_ERRORS.get(source_slug)
