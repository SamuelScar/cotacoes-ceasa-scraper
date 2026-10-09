import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

from cotacoes_ceasa.core.models import ColetaStatus, Cotacao
from cotacoes_ceasa.normalizers.text import slugify
from cotacoes_ceasa.normalizers.unit import normalize_unit


SQLITE_V4_SCHEMA_VERSION = 4
SQLITE_V4_MIGRATION_KEY = "issue9_schema_v1"
SQLiteSchema = Literal["legacy", "v4"]
BACKFILL_TIMEZONE = ZoneInfo("America/Sao_Paulo")
MISSING_CATEGORY_VALUES = {
    "",
    "nao informada",
    "nao-informada",
    "não informada",
    "não-informada",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fontes (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    nome TEXT NOT NULL,
    url_base TEXT NOT NULL,
    uf TEXT CHECK (uf IS NULL OR length(uf) = 2)
);

CREATE TABLE IF NOT EXISTS entrepostos (
    id INTEGER PRIMARY KEY,
    fonte_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    nome TEXT NOT NULL,
    cidade TEXT,
    uf TEXT NOT NULL CHECK (length(uf) = 2),
    UNIQUE (fonte_id, slug),
    FOREIGN KEY (fonte_id) REFERENCES fontes (id)
);

CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    nome TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS produtos (
    id INTEGER PRIMARY KEY,
    categoria_id INTEGER,
    nome TEXT NOT NULL UNIQUE,
    FOREIGN KEY (categoria_id) REFERENCES categorias (id)
);

CREATE TABLE IF NOT EXISTS produto_aliases (
    id INTEGER PRIMARY KEY,
    produto_id INTEGER NOT NULL,
    texto_original TEXT NOT NULL UNIQUE,
    FOREIGN KEY (produto_id) REFERENCES produtos (id)
);

CREATE TABLE IF NOT EXISTS apresentacoes (
    id INTEGER PRIMARY KEY,
    texto_original TEXT NOT NULL UNIQUE,
    unidade_normalizada TEXT,
    embalagem TEXT,
    quantidade_minima NUMERIC,
    quantidade_maxima NUMERIC,
    detalhe TEXT,
    CHECK (
        quantidade_minima IS NULL
        OR quantidade_maxima IS NULL
        OR quantidade_minima <= quantidade_maxima
    )
);

CREATE TABLE IF NOT EXISTS coletas (
    id INTEGER PRIMARY KEY,
    fonte_id INTEGER NOT NULL,
    duplicada_de_id INTEGER,
    url_origem TEXT NOT NULL,
    caminho_relativo_raw TEXT,
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    iniciada_em TEXT NOT NULL,
    baixado_em TEXT,
    processado_em TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pendente',
            'processada',
            'descartada_duplicada',
            'erro_download',
            'erro_processamento'
        )
    ),
    mensagem_erro TEXT,
    FOREIGN KEY (fonte_id) REFERENCES fontes (id),
    FOREIGN KEY (duplicada_de_id) REFERENCES coletas (id),
    CHECK (duplicada_de_id IS NULL OR duplicada_de_id <> id),
    CHECK (
        status <> 'processada'
        OR (
            caminho_relativo_raw IS NOT NULL
            AND sha256 IS NOT NULL
            AND baixado_em IS NOT NULL
            AND processado_em IS NOT NULL
        )
    ),
    CHECK (
        status <> 'descartada_duplicada'
        OR (
            duplicada_de_id IS NOT NULL
            AND caminho_relativo_raw IS NULL
            AND sha256 IS NOT NULL
            AND baixado_em IS NOT NULL
        )
    ),
    CHECK (status = 'descartada_duplicada' OR duplicada_de_id IS NULL),
    CHECK (
        status NOT IN ('erro_download', 'erro_processamento')
        OR mensagem_erro IS NOT NULL
    ),
    CHECK (
        status <> 'erro_processamento'
        OR (
            caminho_relativo_raw IS NOT NULL
            AND sha256 IS NOT NULL
            AND baixado_em IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS cotacoes (
    id INTEGER PRIMARY KEY,
    coleta_id INTEGER NOT NULL,
    entreposto_id INTEGER,
    produto_alias_id INTEGER NOT NULL,
    apresentacao_id INTEGER,
    data_cotacao TEXT NOT NULL,
    variedade TEXT,
    classificacao TEXT,
    procedencia TEXT,
    preco_minimo NUMERIC CHECK (preco_minimo IS NULL OR preco_minimo >= 0),
    preco_comum NUMERIC CHECK (preco_comum IS NULL OR preco_comum >= 0),
    preco_maximo NUMERIC CHECK (preco_maximo IS NULL OR preco_maximo >= 0),
    situacao_mercado TEXT,
    FOREIGN KEY (coleta_id) REFERENCES coletas (id),
    FOREIGN KEY (entreposto_id) REFERENCES entrepostos (id),
    FOREIGN KEY (produto_alias_id) REFERENCES produto_aliases (id),
    FOREIGN KEY (apresentacao_id) REFERENCES apresentacoes (id),
    CHECK (
        preco_minimo IS NOT NULL
        OR preco_comum IS NOT NULL
        OR preco_maximo IS NOT NULL
    )
);

CREATE TABLE IF NOT EXISTS cotacao_complementos (
    id INTEGER PRIMARY KEY,
    cotacao_id INTEGER NOT NULL,
    coleta_id INTEGER NOT NULL,
    campo TEXT NOT NULL,
    valor_anterior TEXT,
    valor_novo TEXT NOT NULL,
    complementado_em TEXT NOT NULL,
    UNIQUE (cotacao_id, campo),
    FOREIGN KEY (cotacao_id) REFERENCES cotacoes (id),
    FOREIGN KEY (coleta_id) REFERENCES coletas (id)
);

CREATE TABLE IF NOT EXISTS controle_backfill (
    id INTEGER PRIMARY KEY,
    fonte_id INTEGER NOT NULL,
    categoria_id INTEGER,
    status TEXT NOT NULL CHECK (
        status IN (
            'complete',
            'partial',
            'paused_for_recheck',
            'exhausted',
            'failed'
        )
    ),
    data_cursor TEXT,
    tentativas_sem_progresso INTEGER NOT NULL DEFAULT 0 CHECK (
        tentativas_sem_progresso >= 0
    ),
    proxima_verificacao_em TEXT,
    ultimo_erro TEXT,
    atualizado_em TEXT NOT NULL,
    FOREIGN KEY (fonte_id) REFERENCES fontes (id),
    FOREIGN KEY (categoria_id) REFERENCES categorias (id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    chave TEXT PRIMARY KEY,
    aplicada_em TEXT NOT NULL,
    checkpoint TEXT,
    detalhes TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_controle_backfill_fonte_geral
    ON controle_backfill (fonte_id)
    WHERE categoria_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_controle_backfill_fonte_categoria
    ON controle_backfill (fonte_id, categoria_id)
    WHERE categoria_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entrepostos_fonte
    ON entrepostos (fonte_id);

CREATE INDEX IF NOT EXISTS idx_produtos_categoria
    ON produtos (categoria_id);

CREATE INDEX IF NOT EXISTS idx_produto_aliases_produto
    ON produto_aliases (produto_id);

CREATE INDEX IF NOT EXISTS idx_coletas_fonte_status
    ON coletas (fonte_id, status);

CREATE INDEX IF NOT EXISTS idx_coletas_fonte_sha256
    ON coletas (fonte_id, sha256);

CREATE INDEX IF NOT EXISTS idx_cotacoes_coleta
    ON cotacoes (coleta_id);

CREATE INDEX IF NOT EXISTS idx_cotacoes_entreposto_data
    ON cotacoes (entreposto_id, data_cotacao);

CREATE INDEX IF NOT EXISTS idx_cotacoes_produto_data
    ON cotacoes (produto_alias_id, data_cotacao);

CREATE INDEX IF NOT EXISTS idx_cotacao_complementos_coleta
    ON cotacao_complementos (coleta_id);
"""


class LegacySQLiteSchemaError(RuntimeError):
    """Indica que o banco precisa da migration da etapa 2."""


@dataclass(frozen=True)
class StoredRaw:
    coleta_id: int
    caminho_relativo_raw: str | None
    sha256: str
    status: ColetaStatus


def detect_sqlite_schema(database_path: Path) -> SQLiteSchema:
    """Mantem bancos existentes no legado e usa v4 para bancos novos."""
    if not database_path.exists() or database_path.stat().st_size == 0:
        return "v4"

    with sqlite3.connect(database_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(coletas)")
        }

    legacy_tables = {
        "estados",
        "ceasas",
        "apresentacoes_unidade",
        "cotacao_proveniencias",
        "backfill_states",
    }

    if tables & legacy_tables:
        return "legacy"

    if not columns:
        return "v4"

    return "v4" if "fonte_id" in columns else "legacy"


def require_legacy_sqlite_schema(
    database_path: Path,
    operation: str,
) -> None:
    if detect_sqlite_schema(database_path) == "legacy":
        return

    raise LegacySQLiteSchemaError(
        f"{operation} pertence ao esquema legado e nao se aplica ao SQLite v4."
    )


@dataclass(frozen=True)
class SQLiteV4Storage:
    """Inicializa o esquema da issue 9 somente em bancos novos ou v4."""

    database_path: Path

    def ensure_schema(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._reject_legacy_schema(connection)
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (
                    chave,
                    aplicada_em,
                    checkpoint,
                    detalhes
                )
                VALUES (?, ?, NULL, ?)
                """,
                (
                    SQLITE_V4_MIGRATION_KEY,
                    datetime.now().isoformat(timespec="seconds"),
                    "Criacao do esquema vazio da issue 9.",
                ),
            )
            connection.execute(f"PRAGMA user_version = {SQLITE_V4_SCHEMA_VERSION}")

    def register_source(
        self,
        slug: str,
        name: str,
        base_url: str,
        uf: str | None,
    ) -> int:
        self.ensure_schema()

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO fontes (slug, nome, url_base, uf)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (slug) DO UPDATE SET
                    nome = excluded.nome,
                    url_base = excluded.url_base,
                    uf = excluded.uf
                """,
                (slug, name, base_url, uf),
            )

            return self._fetch_id(connection, "fontes", "slug", slug)

    def create_coleta(
        self,
        source_slug: str,
        url_origem: str,
        iniciada_em: datetime | None = None,
    ) -> int:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            fonte_id = self._fetch_id(connection, "fontes", "slug", source_slug)
            cursor = connection.execute(
                """
                INSERT INTO coletas (
                    fonte_id,
                    url_origem,
                    iniciada_em,
                    status
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    fonte_id,
                    url_origem,
                    self._format_datetime(iniciada_em or datetime.now()),
                    ColetaStatus.PENDENTE,
                ),
            )

            return int(cursor.lastrowid)

    def mark_coleta_downloaded(
        self,
        coleta_id: int,
        sha256: str,
        caminho_relativo_raw: str,
        baixado_em: datetime | None = None,
    ) -> None:
        self._validate_sha256(sha256)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE coletas
                SET
                    sha256 = ?,
                    caminho_relativo_raw = ?,
                    baixado_em = ?
                WHERE id = ? AND status = ?
                """,
                (
                    sha256,
                    caminho_relativo_raw,
                    self._format_datetime(baixado_em or datetime.now()),
                    coleta_id,
                    ColetaStatus.PENDENTE,
                ),
            )

    def find_collection_for_raw(
        self,
        source_slug: str,
        sha256: str,
        caminho_relativo_raw: str,
    ) -> StoredRaw | None:
        if not self.database_path.exists():
            return None

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT col.id, col.caminho_relativo_raw, col.sha256, col.status
                FROM coletas col
                JOIN fontes f ON f.id = col.fonte_id
                WHERE f.slug = ?
                  AND col.sha256 = ?
                  AND col.caminho_relativo_raw = ?
                ORDER BY col.id DESC
                LIMIT 1
                """,
                (source_slug, sha256, caminho_relativo_raw),
            ).fetchone()

        return self._stored_raw(row)

    def find_pending_collection_for_path(
        self,
        source_slug: str,
        caminho_relativo_raw: str,
    ) -> StoredRaw | None:
        if not self.database_path.exists():
            return None

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT col.id, col.caminho_relativo_raw, col.sha256, col.status
                FROM coletas col
                JOIN fontes f ON f.id = col.fonte_id
                WHERE f.slug = ?
                  AND col.caminho_relativo_raw = ?
                  AND col.status = ?
                ORDER BY col.id DESC
                LIMIT 1
                """,
                (
                    source_slug,
                    caminho_relativo_raw,
                    ColetaStatus.PENDENTE,
                ),
            ).fetchone()

        return self._stored_raw(row)

    def find_duplicate(
        self,
        source_slug: str,
        sha256: str,
        exclude_coleta_id: int | None = None,
    ) -> StoredRaw | None:
        if not self.database_path.exists():
            return None

        query = """
            SELECT col.id, col.caminho_relativo_raw, col.sha256, col.status
            FROM coletas col
            JOIN fontes f ON f.id = col.fonte_id
            WHERE f.slug = ?
              AND col.sha256 = ?
              AND col.status IN (?, ?)
              AND col.caminho_relativo_raw IS NOT NULL
        """
        params: list[object] = [
            source_slug,
            sha256,
            ColetaStatus.PENDENTE,
            ColetaStatus.PROCESSADA,
        ]

        if exclude_coleta_id is not None:
            query += " AND col.id != ?"
            params.append(exclude_coleta_id)

        query += """
            ORDER BY
                CASE WHEN col.status = 'processada' THEN 0 ELSE 1 END,
                col.id
            LIMIT 1
        """

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(query, params).fetchone()

        return self._stored_raw(row)

    def mark_coleta_duplicate(
        self,
        coleta_id: int,
        original_coleta_id: int,
        sha256: str,
        baixado_em: datetime | None = None,
    ) -> None:
        self._validate_sha256(sha256)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                UPDATE coletas
                SET
                    duplicada_de_id = ?,
                    caminho_relativo_raw = NULL,
                    sha256 = ?,
                    baixado_em = ?,
                    processado_em = NULL,
                    status = ?,
                    mensagem_erro = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    original_coleta_id,
                    sha256,
                    self._format_datetime(baixado_em or datetime.now()),
                    ColetaStatus.DESCARTADA_DUPLICADA,
                    coleta_id,
                    ColetaStatus.PENDENTE,
                ),
            )

    def replace_missing_raw(
        self,
        coleta_id: int,
        caminho_relativo_raw: str,
    ) -> None:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                """
                UPDATE coletas
                SET caminho_relativo_raw = ?
                WHERE id = ?
                  AND sha256 IS NOT NULL
                  AND status IN (?, ?)
                """,
                (
                    caminho_relativo_raw,
                    coleta_id,
                    ColetaStatus.PENDENTE,
                    ColetaStatus.PROCESSADA,
                ),
            )

        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Coleta canonica indisponivel para repor o raw: {coleta_id}"
            )

    def mark_coleta_processed(
        self,
        coleta_id: int,
        processado_em: datetime | None = None,
    ) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE coletas
                SET
                    processado_em = ?,
                    status = ?,
                    mensagem_erro = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    self._format_datetime(processado_em or datetime.now()),
                    ColetaStatus.PROCESSADA,
                    coleta_id,
                    ColetaStatus.PENDENTE,
                ),
            )

    def mark_coleta_error(
        self,
        coleta_id: int,
        status: ColetaStatus,
        mensagem_erro: str,
    ) -> None:
        if status not in {
            ColetaStatus.ERRO_DOWNLOAD,
            ColetaStatus.ERRO_PROCESSAMENTO,
        }:
            raise ValueError(f"Status de erro invalido: {status}")

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                UPDATE coletas
                SET status = ?, mensagem_erro = ?
                WHERE id = ? AND status = ?
                """,
                (status, mensagem_erro, coleta_id, ColetaStatus.PENDENTE),
            )

    def save_cotacoes(
        self,
        cotacoes: list[Cotacao],
        source_slug: str,
        default_market: str,
        uf: str,
    ) -> int:
        if not cotacoes:
            return 0

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            fonte_id = self._fetch_id(connection, "fontes", "slug", source_slug)
            inserted_count = 0

            for cotacao in cotacoes:
                categoria_id = self._get_or_create_categoria(
                    connection,
                    self._normalize_category(cotacao.categoria),
                )
                default_market_name = self._default_market(default_market)
                market_name = cotacao.entreposto or default_market_name
                market_city = (
                    default_market
                    if market_name is not None and market_name == default_market_name
                    else None
                )
                entreposto_id = self._get_or_create_entreposto(
                    connection,
                    fonte_id,
                    market_name,
                    market_city,
                    uf,
                )
                inserted_count += self.insert_cotacao(
                    connection,
                    cotacao,
                    entreposto_id,
                    categoria_id,
                )

            processed_at = self._format_datetime(datetime.now())
            coleta_ids = sorted(
                {
                    cotacao.coleta_id
                    for cotacao in cotacoes
                    if cotacao.coleta_id is not None
                }
            )
            connection.executemany(
                """
                UPDATE coletas
                SET
                    processado_em = ?,
                    status = ?,
                    mensagem_erro = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    (
                        processed_at,
                        ColetaStatus.PROCESSADA,
                        coleta_id,
                        ColetaStatus.PENDENTE,
                    )
                    for coleta_id in coleta_ids
                ),
            )

            return inserted_count

    def insert_cotacao(
        self,
        connection: sqlite3.Connection,
        cotacao: Cotacao,
        entreposto_id: int | None,
        categoria_id: int | None,
    ) -> int:
        self._validate_cotacao(cotacao)
        produto_alias_id = self._get_or_create_product_alias(
            connection,
            cotacao.produto,
            categoria_id,
        )
        apresentacao_id = self._get_or_create_presentation(
            connection,
            cotacao.unidade,
        )
        cursor = connection.execute(
            """
            INSERT INTO cotacoes (
                coleta_id,
                entreposto_id,
                produto_alias_id,
                apresentacao_id,
                data_cotacao,
                variedade,
                classificacao,
                procedencia,
                preco_minimo,
                preco_comum,
                preco_maximo,
                situacao_mercado
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cotacao.coleta_id,
                entreposto_id,
                produto_alias_id,
                apresentacao_id,
                cotacao.data_cotacao.isoformat(),
                cotacao.variedade,
                cotacao.classificacao,
                cotacao.procedencia,
                self._decimal_to_db(cotacao.preco_minimo),
                self._decimal_to_db(cotacao.preco_comum),
                self._decimal_to_db(cotacao.preco_maximo),
                cotacao.situacao_mercado,
            ),
        )

        return cursor.rowcount

    def find_oldest_cotacao_date(
        self,
        source_slug: str,
        category_slug: str | None = None,
    ) -> date | None:
        if not self.database_path.exists():
            return None

        query = """
            SELECT MIN(c.data_cotacao)
            FROM cotacoes c
            JOIN coletas col ON col.id = c.coleta_id
            JOIN fontes f ON f.id = col.fonte_id
            JOIN produto_aliases pa ON pa.id = c.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            LEFT JOIN categorias cat ON cat.id = p.categoria_id
            WHERE f.slug = ?
        """
        params: list[object] = [source_slug]

        if category_slug is not None:
            query += " AND cat.slug = ?"
            params.append(category_slug)

        with sqlite3.connect(self.database_path) as connection:
            if not self._table_exists(connection, "cotacoes"):
                return None

            row = connection.execute(query, params).fetchone()

        return date.fromisoformat(str(row[0])) if row and row[0] else None

    def find_latest_cotacao_id(self) -> int:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM cotacoes"
            ).fetchone()

        return int(row[0]) if row else 0

    def count_cotacoes(self) -> int:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()

        return int(row[0]) if row else 0

    def find_latest_cotacao_dates(
        self,
        source_slugs: Iterable[str],
    ) -> dict[str, date | None]:
        requested_slugs = tuple(dict.fromkeys(source_slugs))
        latest_dates = {source_slug: None for source_slug in requested_slugs}

        if not requested_slugs:
            return latest_dates

        placeholders = ", ".join("?" for _ in requested_slugs)
        query = f"""
            SELECT f.slug, MAX(c.data_cotacao)
            FROM cotacoes c
            JOIN coletas col ON col.id = c.coleta_id
            JOIN fontes f ON f.id = col.fonte_id
            WHERE f.slug IN ({placeholders})
            GROUP BY f.slug
        """

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(query, requested_slugs).fetchall()

        for source_slug, latest_date in rows:
            latest_dates[str(source_slug)] = (
                date.fromisoformat(str(latest_date)) if latest_date else None
            )

        return latest_dates

    def summarize_logical_cotacao_delta(self, previous_max_id: int):
        from cotacoes_ceasa.storage.sqlite import LogicalCotacaoDelta

        with sqlite3.connect(self.database_path) as connection:
            current_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM cotacoes"
            ).fetchone()
            inserted_row = connection.execute(
                "SELECT COUNT(*) FROM cotacoes WHERE id > ?",
                (previous_max_id,),
            ).fetchone()

        current_max_id = int(current_row[0]) if current_row else 0
        inserted_count = int(inserted_row[0]) if inserted_row else 0

        return LogicalCotacaoDelta(
            previous_max_id=previous_max_id,
            current_max_id=current_max_id,
            observations_inserted=inserted_count,
            logical_new=inserted_count,
            repeated_observations=0,
        )

    def find_backfill_state(
        self,
        source_slug: str,
        category_slug: str | None = None,
    ):
        if not self.database_path.exists():
            return None

        query = """
            SELECT
                f.slug,
                cat.slug,
                cb.status,
                cb.data_cursor,
                cb.tentativas_sem_progresso,
                cb.proxima_verificacao_em,
                cb.ultimo_erro,
                cb.atualizado_em
            FROM controle_backfill cb
            JOIN fontes f ON f.id = cb.fonte_id
            LEFT JOIN categorias cat ON cat.id = cb.categoria_id
            WHERE f.slug = ?
        """
        params: list[object] = [source_slug]

        if category_slug is None:
            query += " AND cb.categoria_id IS NULL"
        else:
            query += " AND cat.slug = ?"
            params.append(category_slug)

        with sqlite3.connect(self.database_path) as connection:
            if not self._table_exists(connection, "controle_backfill"):
                return None

            row = connection.execute(query, params).fetchone()

        if row is None:
            return None

        from cotacoes_ceasa.storage.sqlite import BackfillState

        return BackfillState(
            source_slug=str(row[0]),
            category_slug=str(row[1]) if row[1] else None,
            status=str(row[2]),
            cursor_date=date.fromisoformat(str(row[3])) if row[3] else None,
            consecutive_no_progress=int(row[4]),
            next_check_date=date.fromisoformat(str(row[5])) if row[5] else None,
            last_error=str(row[6]) if row[6] else None,
            updated_at=datetime.fromisoformat(str(row[7])),
        )

    def save_backfill_state(
        self,
        source_slug: str,
        status: str,
        cursor_date: date | None,
        consecutive_no_progress: int = 0,
        next_check_date: date | None = None,
        last_error: str | None = None,
        category_slug: str | None = None,
    ):
        allowed_statuses = {
            "complete",
            "partial",
            "paused_for_recheck",
            "exhausted",
            "failed",
        }

        if status not in allowed_statuses:
            raise ValueError(f"Status de backfill invalido: {status}")

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            fonte_id = self._fetch_id(connection, "fontes", "slug", source_slug)
            categoria_id = self._get_or_create_categoria(
                connection,
                category_slug,
            )
            row = connection.execute(
                """
                SELECT id
                FROM controle_backfill
                WHERE fonte_id = ? AND categoria_id IS ?
                """,
                (fonte_id, categoria_id),
            ).fetchone()
            values = (
                status,
                cursor_date.isoformat() if cursor_date else None,
                consecutive_no_progress,
                next_check_date.isoformat() if next_check_date else None,
                last_error,
                datetime.now(BACKFILL_TIMEZONE).isoformat(timespec="seconds"),
            )

            if row is None:
                connection.execute(
                    """
                    INSERT INTO controle_backfill (
                        fonte_id,
                        categoria_id,
                        status,
                        data_cursor,
                        tentativas_sem_progresso,
                        proxima_verificacao_em,
                        ultimo_erro,
                        atualizado_em
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (fonte_id, categoria_id, *values),
                )
            else:
                connection.execute(
                    """
                    UPDATE controle_backfill
                    SET
                        status = ?,
                        data_cursor = ?,
                        tentativas_sem_progresso = ?,
                        proxima_verificacao_em = ?,
                        ultimo_erro = ?,
                        atualizado_em = ?
                    WHERE id = ?
                    """,
                    (*values, int(row[0])),
                )

        state = self.find_backfill_state(source_slug, category_slug)

        if state is None:
            raise RuntimeError("Estado do backfill nao foi persistido.")

        return state

    def reset_backfill_state(self, source_slug: str) -> int:
        if not self.database_path.exists():
            return 0

        with sqlite3.connect(self.database_path) as connection:
            if not self._table_exists(connection, "controle_backfill"):
                return 0

            cursor = connection.execute(
                """
                DELETE FROM controle_backfill
                WHERE fonte_id = (SELECT id FROM fontes WHERE slug = ?)
                """,
                (source_slug,),
            )

        return max(0, cursor.rowcount)

    def _reject_legacy_schema(self, connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(coletas)")
        }

        legacy_tables = {
            "estados",
            "ceasas",
            "apresentacoes_unidade",
            "cotacao_proveniencias",
            "backfill_states",
        }

        if tables & legacy_tables or (columns and "fonte_id" not in columns):
            raise LegacySQLiteSchemaError(
                "O banco usa o esquema legado e deve ser migrado na etapa 2."
            )

    def _get_or_create_categoria(
        self,
        connection: sqlite3.Connection,
        category_slug: str | None,
    ) -> int | None:
        if category_slug is None:
            return None

        connection.execute(
            """
            INSERT INTO categorias (slug, nome)
            VALUES (?, ?)
            ON CONFLICT (slug) DO NOTHING
            """,
            (category_slug, category_slug),
        )

        return self._fetch_id(connection, "categorias", "slug", category_slug)

    def _get_or_create_product_alias(
        self,
        connection: sqlite3.Connection,
        product_name: str,
        categoria_id: int | None,
    ) -> int:
        canonical_name = self._normalize_name(product_name)
        connection.execute(
            """
            INSERT INTO produtos (categoria_id, nome)
            VALUES (?, ?)
            ON CONFLICT (nome) DO UPDATE SET
                categoria_id = COALESCE(produtos.categoria_id, excluded.categoria_id)
            """,
            (categoria_id, canonical_name),
        )
        produto_id = self._fetch_id(
            connection,
            "produtos",
            "nome",
            canonical_name,
        )
        connection.execute(
            """
            INSERT INTO produto_aliases (produto_id, texto_original)
            VALUES (?, ?)
            ON CONFLICT (texto_original) DO NOTHING
            """,
            (produto_id, product_name),
        )

        return self._fetch_id(
            connection,
            "produto_aliases",
            "texto_original",
            product_name,
        )

    def _get_or_create_presentation(
        self,
        connection: sqlite3.Connection,
        unit: str | None,
    ) -> int | None:
        normalized = normalize_unit(unit)

        if normalized.original is None:
            return None

        connection.execute(
            """
            INSERT INTO apresentacoes (
                texto_original,
                unidade_normalizada,
                embalagem,
                quantidade_minima,
                quantidade_maxima,
                detalhe
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (texto_original) DO UPDATE SET
                unidade_normalizada = excluded.unidade_normalizada,
                embalagem = excluded.embalagem,
                quantidade_minima = excluded.quantidade_minima,
                quantidade_maxima = excluded.quantidade_maxima,
                detalhe = excluded.detalhe
            """,
            (
                normalized.original,
                normalized.normalized,
                normalized.packaging,
                self._decimal_to_db(normalized.quantity_min),
                self._decimal_to_db(normalized.quantity_max),
                normalized.detail,
            ),
        )

        return self._fetch_id(
            connection,
            "apresentacoes",
            "texto_original",
            normalized.original,
        )

    def _get_or_create_entreposto(
        self,
        connection: sqlite3.Connection,
        fonte_id: int,
        name: str | None,
        city: str | None,
        uf: str,
    ) -> int | None:
        if name is None:
            return None

        market_slug = slugify(name)
        connection.execute(
            """
            INSERT INTO entrepostos (fonte_id, slug, nome, cidade, uf)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (fonte_id, slug) DO UPDATE SET
                nome = excluded.nome,
                cidade = COALESCE(excluded.cidade, entrepostos.cidade),
                uf = excluded.uf
            """,
            (fonte_id, market_slug, name, city, uf),
        )
        row = connection.execute(
            """
            SELECT id
            FROM entrepostos
            WHERE fonte_id = ? AND slug = ?
            """,
            (fonte_id, market_slug),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Entreposto nao encontrado apos insert: {name}")

        return int(row[0])

    def _validate_cotacao(self, cotacao: Cotacao) -> None:
        if cotacao.coleta_id is None:
            raise ValueError(f"Cotacao sem coleta: {cotacao.produto}")

        if cotacao.data_cotacao is None:
            raise ValueError(f"Cotacao sem data: {cotacao.produto}")

        prices = (cotacao.preco_minimo, cotacao.preco_comum, cotacao.preco_maximo)

        if all(price is None for price in prices):
            raise ValueError(f"Cotacao sem preco: {cotacao.produto}")

        if any(price is not None and price < 0 for price in prices):
            raise ValueError(f"Cotacao com preco negativo: {cotacao.produto}")

    def _fetch_id(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        value: str,
    ) -> int:
        row = connection.execute(
            f"SELECT id FROM {table_name} WHERE {column_name} = ?",
            (value,),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Registro nao encontrado em {table_name}: {value}")

        return int(row[0])

    def _table_exists(
        self,
        connection: sqlite3.Connection,
        table_name: str,
    ) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()

        return row is not None

    def _stored_raw(self, row: tuple | None) -> StoredRaw | None:
        if row is None:
            return None

        return StoredRaw(
            coleta_id=int(row[0]),
            caminho_relativo_raw=str(row[1]) if row[1] is not None else None,
            sha256=str(row[2]),
            status=ColetaStatus(str(row[3])),
        )

    def _validate_sha256(self, value: str) -> None:
        if len(value) != 64:
            raise ValueError("SHA-256 deve possuir 64 caracteres hexadecimais.")

        try:
            int(value, 16)
        except ValueError as error:
            raise ValueError("SHA-256 invalido.") from error

    def _format_datetime(self, value: datetime) -> str:
        return value.isoformat(timespec="seconds")

    def _decimal_to_db(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    def _normalize_name(self, value: str) -> str:
        return " ".join(value.lower().split())

    def _normalize_category(self, value: str | None) -> str | None:
        if value is None:
            return None

        normalized = value.strip()

        return (
            None
            if normalized.lower() in MISSING_CATEGORY_VALUES
            else normalized
        )

    def _default_market(self, city: str) -> str | None:
        return None if city.lower() == "varias cidades" else city
