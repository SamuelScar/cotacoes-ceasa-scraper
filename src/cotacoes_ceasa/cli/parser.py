import argparse
from datetime import date, datetime

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


def build_parser(config: AppConfig) -> argparse.ArgumentParser:
    """Cria o parser de argumentos da CLI."""
    parser = argparse.ArgumentParser(
        description="Coleta cotacoes publicas de CEASAs brasileiras."
    )
    parser.add_argument(
        "--source",
        choices=sorted(config.sources),
        default=config.source,
        help="Fonte que sera coletada.",
    )
    parser.add_argument(
        "--all-sources",
        action="store_true",
        help="Executa a operacao para todas as fontes configuradas.",
    )
    parser.add_argument(
        "--raw-dir",
        default=config.raw_dir,
        help="Diretorio onde o HTML bruto sera salvo.",
    )
    parser.add_argument(
        "--database-path",
        default=config.database_path,
        help="Arquivo SQLite onde as cotacoes serao salvas.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Sobrescreve a URL base da fonte informada.",
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
        "--prohort-url",
        default=config.prohort_url,
        help="URL do arquivo ProhortDiario.txt usado no complemento.",
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
        "--quotes-back",
        default=config.quotes_back,
        type=int,
        help="Quantidade de datas de cotacao anteriores para coletar.",
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
        "--download-and-process",
        action="store_true",
        help="Baixa e depois processa os raws de todas as fontes.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Baixa os raws sem extrair cotacoes.",
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
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="Lista categorias descobertas na fonte sem baixar as tabelas.",
    )

    return parser
