import argparse
from datetime import date, datetime

from cotacoes_ceasa.cli.collection_mode import COLLECTION_MODES
from cotacoes_ceasa.config import AppConfig


def parse_target_date(value: str | None) -> date | None:
    """Converte a data limite da CLI para date."""
    if not value:
        return None

    for date_format in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    raise ValueError("Data invalida. Use DD/MM/YYYY ou YYYY-MM-DD.")


def parse_quotes_back(value: str) -> int | None:
    """Converte a janela historica da CLI para quantidade ou modo infinito."""
    normalized_value = value.strip().lower()

    if normalized_value in {"infinito", "infinite"}:
        return None

    try:
        return int(normalized_value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use um numero ou infinito.") from error


def parse_positive_int(value: str) -> int:
    """Converte parametros inteiros que exigem valor positivo."""
    try:
        parsed_value = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use um numero inteiro.") from error

    if parsed_value < 1:
        raise argparse.ArgumentTypeError("Use um numero maior ou igual a 1.")

    return parsed_value


def format_quotes_back(value: int | None) -> str:
    return "infinito" if value is None else str(value)


def format_incremental_history(
    enabled: bool,
    target_date: str | None,
    quotes_back: int | None,
) -> str:
    if not enabled:
        return "nao"

    if target_date:
        return "inativo: data limite manual"

    if quotes_back == 0:
        return "inativo: quotes_back=0"

    return "sim"


def build_parser(config: AppConfig) -> argparse.ArgumentParser:
    """Cria o parser de argumentos da CLI."""
    parser = argparse.ArgumentParser(
        description="Coleta cotacoes publicas de CEASAs brasileiras."
    )
    parser.add_argument(
        "--source",
        choices=sorted(config.sources),
        default=None,
        help="Executa somente a fonte informada. O padrao executa todas.",
    )
    parser.add_argument(
        "--raw-dir",
        default=config.raw_dir,
        help="Diretorio onde o HTML bruto sera salvo.",
    )
    parser.add_argument(
        "--pdf-text-cache-dir",
        default=config.pdf_text_cache_dir,
        help="Diretorio onde textos extraidos de PDFs serao cacheados.",
    )
    parser.add_argument(
        "--database-path",
        default=config.database_path,
        help="Arquivo SQLite onde as cotacoes serao salvas.",
    )
    parser.add_argument(
        "--reset-backfill-state",
        action="store_true",
        help=(
            "Reabre o backfill da fonte informada em --source removendo seu "
            "estado persistido."
        ),
    )
    gate_validation = parser.add_mutually_exclusive_group()
    gate_validation.add_argument(
        "--validate-publication",
        action="store_true",
        help=(
            "Valida estrutura, regressao e politicas por fonte antes de "
            "publicar o SQLite."
        ),
    )
    gate_validation.add_argument(
        "--validate-checkpoint",
        action="store_true",
        help=(
            "Valida estrutura e regressao antes de salvar o checkpoint "
            "interno."
        ),
    )
    parser.add_argument(
        "--health-report-path",
        default="data/relatorios/saude_ultima.json",
        help="JSON de saude usado pelo gate de publicacao.",
    )
    parser.add_argument(
        "--publication-gate-report-path",
        default="data/relatorios/gate_publicacao_ultima.json",
        help="JSON estruturado gravado pelo gate de publicacao.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Sobrescreve a URL base da fonte informada em --source.",
    )
    parser.add_argument(
        "--http-timeout-seconds",
        default=config.http_timeout_seconds,
        type=int,
        help="Tempo maximo de espera para requisicoes HTTP.",
    )
    parser.add_argument(
        "--request-delay-seconds",
        default=config.request_delay_seconds,
        type=float,
        help="Intervalo minimo entre requisicoes HTTP.",
    )
    parser.add_argument(
        "--target-date",
        default=config.target_date,
        help=(
            "Data limite da coleta em DD/MM/YYYY ou YYYY-MM-DD. "
            "Quando omitida, busca a ultima cotacao disponivel."
        ),
    )
    parser.add_argument(
        "--collection-mode",
        choices=COLLECTION_MODES,
        default=None,
        help=(
            "Seleciona coleta atual (current) ou historico incremental "
            "controlado (backfill). Omitir preserva o comportamento existente."
        ),
    )
    parser.add_argument(
        "--quotes-back",
        default=config.quotes_back,
        type=parse_quotes_back,
        help=(
            "Quantidade de datas anteriores ou 'infinito' "
            "para buscar todo historico."
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Extrai cotacoes e salva os registros no SQLite.",
    )
    parser.add_argument(
        "--process-raw",
        action="store_true",
        help="Processa HTML bruto salvo em disco e salva os registros no SQLite.",
    )
    parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Reprocessa raws mesmo quando ja existem no SQLite com o mesmo hash.",
    )
    parser.add_argument(
        "--raw-detail-report",
        action="store_true",
        help="Registra cada raw processado no historico completo do relatorio.",
    )
    parser.add_argument(
        "--download-and-process",
        action="store_true",
        help="Baixa e depois processa os raws das fontes selecionadas.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Baixa os raws sem extrair cotacoes.",
    )
    parser.add_argument(
        "--workers",
        default=config.workers,
        type=parse_positive_int,
        help=(
            "Quantidade de fontes baixadas em paralelo no download de todas "
            "as fontes. O padrao preserva execucao sequencial."
        ),
    )
    parser.add_argument(
        "--archive-raw-old",
        action="store_true",
        help="Compacta HTMLs da pasta old de cada fonte e remove os originais.",
    )
    parser.add_argument(
        "--complement-prohort",
        action="store_true",
        help=(
            "Complementa cotacoes ja salvas usando o PROHORT, "
            "sem sobrescrever campos preenchidos."
        ),
    )
    sync_group = parser.add_mutually_exclusive_group()
    sync_group.add_argument(
        "--sync-supabase",
        action="store_true",
        help="Adiciona novos registros do SQLite ao Supabase.",
    )
    sync_group.add_argument(
        "--replace-supabase",
        action="store_true",
        help="Substitui completamente o snapshot do Supabase.",
    )
    parser.add_argument(
        "--supabase-batch-size",
        default=config.supabase_batch_size,
        type=int,
        help="Quantidade de registros enviada por lote ao Supabase.",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Lista categorias descobertas nas fontes selecionadas.",
    )

    return parser
