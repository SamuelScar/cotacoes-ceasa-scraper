import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.normalizers.text import slugify
from cotacoes_ceasa.normalizers.unit import NormalizedUnit, normalize_unit


BACKFILL_STATE_TIMEZONE = ZoneInfo("America/Sao_Paulo")
SQLITE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class LogicalCotacaoDelta:
    previous_max_id: int
    current_max_id: int
    observations_inserted: int
    logical_new: int
    repeated_observations: int


@dataclass(frozen=True)
class BackfillState:
    source_slug: str
    category_slug: str | None
    status: str
    cursor_date: date | None
    consecutive_no_progress: int
    next_check_date: date | None
    last_error: str | None
    updated_at: datetime

    def is_deferred(self, reference_date: date) -> bool:
        if self.status == "exhausted":
            return True

        return (
            self.status == "paused_for_recheck"
            and self.next_check_date is not None
            and self.next_check_date > reference_date
        )


@dataclass(frozen=True)
class SQLiteStorage:
    """Persiste cotacoes normalizadas em um arquivo SQLite."""

    database_path: Path

    def ensure_schema(self) -> None:
        """Garante que o banco exista com o schema atual."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema(connection)

    def save_cotacoes(
        self,
        cotacoes: list[Cotacao],
        source_slug: str,
        source_name: str,
        state_name: str,
        uf: str,
        city: str,
        source_url: str,
    ) -> int:
        """Salva cotacoes no SQLite e retorna quantos registros novos entraram."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema(connection)
            ceasa_id = self._get_or_create_ceasa(
                connection=connection,
                source_slug=source_slug,
                source_name=source_name,
                state_name=state_name,
                uf=uf,
                source_url=source_url,
            )

            return self.insert_cotacoes(
                connection=connection,
                cotacoes=cotacoes,
                ceasa_id=ceasa_id,
                source_slug=source_slug,
                default_market=city,
            )

    def find_processed_raw_hashes(
        self,
        raw_paths: Iterable[Path],
    ) -> set[tuple[str, str]]:
        """Retorna pares arquivo/hash ja registrados em coletas."""
        raw_path_values = sorted({raw_path.as_posix() for raw_path in raw_paths})

        if not raw_path_values or not self.database_path.exists():
            return set()

        processed_raws: set[tuple[str, str]] = set()

        with sqlite3.connect(self.database_path) as connection:
            for index in range(0, len(raw_path_values), 900):
                chunk = raw_path_values[index : index + 900]
                placeholders = ", ".join("?" for _ in chunk)
                try:
                    rows = connection.execute(
                        f"""
                        SELECT arquivo_raw, hash_raw
                        FROM coletas
                        WHERE arquivo_raw IN ({placeholders})
                            AND hash_raw IS NOT NULL
                        """,
                        chunk,
                    ).fetchall()
                except sqlite3.OperationalError:
                    return set()

                processed_raws.update(
                    (str(arquivo_raw), str(hash_raw))
                    for arquivo_raw, hash_raw in rows
                    if arquivo_raw is not None and hash_raw is not None
                )

        return processed_raws

    def find_oldest_cotacao_date(
        self,
        source_slug: str,
        category_slug: str | None = None,
    ) -> date | None:
        """Retorna a menor data de cotacao registrada para a fonte e categoria."""
        if not self.database_path.exists():
            return None

        query = """
            SELECT MIN(c.data_cotacao)
            FROM cotacoes c
            JOIN coletas col ON c.coleta_id = col.id
            JOIN ceasas cs ON col.ceasa_id = cs.id
        """
        params = [source_slug]

        if category_slug is not None:
            query += """
                JOIN categorias cat ON c.categoria_id = cat.id
                WHERE cs.slug = ? AND cat.slug = ?
            """
            params.append(category_slug)
        else:
            query += " WHERE cs.slug = ?"

        with sqlite3.connect(self.database_path) as connection:
            try:
                row = connection.execute(query, params).fetchone()
                if row and row[0]:
                    return date.fromisoformat(row[0])
            except sqlite3.OperationalError:
                return None
        return None

    def find_backfill_state(
        self,
        source_slug: str,
        category_slug: str | None = None,
    ) -> BackfillState | None:
        """Consulta o estado do cursor sem criar ou alterar o banco."""
        if not self.database_path.exists():
            return None

        with sqlite3.connect(self.database_path) as connection:
            try:
                row = connection.execute(
                    """
                    SELECT
                        source_slug,
                        category_slug,
                        status,
                        cursor_date,
                        consecutive_no_progress,
                        next_check_date,
                        last_error,
                        updated_at
                    FROM backfill_states
                    WHERE source_slug = ? AND category_slug = ?
                    """,
                    (source_slug, category_slug or ""),
                ).fetchone()
            except sqlite3.OperationalError:
                return None

        if row is None:
            return None

        return BackfillState(
            source_slug=str(row[0]),
            category_slug=str(row[1]) or None,
            status=str(row[2]),
            cursor_date=date.fromisoformat(row[3]) if row[3] else None,
            consecutive_no_progress=int(row[4]),
            next_check_date=date.fromisoformat(row[5]) if row[5] else None,
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
    ) -> BackfillState:
        """Persiste o estado operacional do backfill de forma aditiva."""
        updated_at = datetime.now(BACKFILL_STATE_TIMEZONE)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema(connection)
            connection.execute(
                """
                INSERT INTO backfill_states (
                    source_slug,
                    category_slug,
                    status,
                    cursor_date,
                    consecutive_no_progress,
                    next_check_date,
                    last_error,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_slug, category_slug) DO UPDATE SET
                    status = excluded.status,
                    cursor_date = excluded.cursor_date,
                    consecutive_no_progress = excluded.consecutive_no_progress,
                    next_check_date = excluded.next_check_date,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    source_slug,
                    category_slug or "",
                    status,
                    cursor_date.isoformat() if cursor_date else None,
                    consecutive_no_progress,
                    next_check_date.isoformat() if next_check_date else None,
                    last_error,
                    updated_at.isoformat(timespec="seconds"),
                ),
            )

        state = self.find_backfill_state(source_slug, category_slug)
        if state is None:
            raise RuntimeError("Estado do backfill nao foi persistido.")

        return state

    def reset_backfill_state(self, source_slug: str) -> int:
        """Remove todos os estados de backfill da fonte informada."""
        if not self.database_path.exists():
            return 0

        with sqlite3.connect(self.database_path) as connection:
            try:
                cursor = connection.execute(
                    "DELETE FROM backfill_states WHERE source_slug = ?",
                    (source_slug,),
                )
            except sqlite3.OperationalError:
                return 0

        return max(0, cursor.rowcount)

    def find_latest_cotacao_id(self) -> int:
        """Retorna o maior id atual sem criar ou alterar o banco."""
        with self._connect_read_only() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM cotacoes"
            ).fetchone()

        return int(row[0]) if row else 0

    def count_cotacoes(self) -> int:
        """Retorna a quantidade total de cotacoes sem alterar o banco."""
        with self._connect_read_only() as connection:
            row = connection.execute("SELECT COUNT(*) FROM cotacoes").fetchone()

        return int(row[0]) if row else 0

    def find_latest_cotacao_dates(
        self,
        source_slugs: Iterable[str],
    ) -> dict[str, date | None]:
        """Retorna a maior data persistida de cada fonte em uma unica consulta."""
        requested_slugs = tuple(dict.fromkeys(source_slugs))
        latest_dates = {source_slug: None for source_slug in requested_slugs}

        if not requested_slugs:
            return latest_dates

        placeholders = ", ".join("?" for _ in requested_slugs)
        query = f"""
            SELECT cs.slug, MAX(c.data_cotacao)
            FROM cotacoes c
            JOIN coletas col ON c.coleta_id = col.id
            JOIN ceasas cs ON col.ceasa_id = cs.id
            WHERE cs.slug IN ({placeholders})
            GROUP BY cs.slug
        """

        with self._connect_read_only() as connection:
            rows = connection.execute(query, requested_slugs).fetchall()

        for source_slug, latest_date in rows:
            latest_dates[str(source_slug)] = (
                date.fromisoformat(str(latest_date)) if latest_date else None
            )

        return latest_dates

    def summarize_logical_cotacao_delta(
        self,
        previous_max_id: int,
    ) -> LogicalCotacaoDelta:
        """Classifica as observacoes inseridas depois do inicio da rodada."""
        with self._connect_read_only() as connection:
            current_row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM cotacoes"
            ).fetchone()
            current_max_id = int(current_row[0]) if current_row else 0
            row = connection.execute(
                """
                WITH new_rows AS (
                    SELECT
                        id,
                        chave_identidade,
                        preco_minimo,
                        preco_comum,
                        preco_maximo,
                        situacao_mercado
                    FROM cotacoes
                    WHERE id > ?
                ),
                new_groups AS (
                    SELECT
                        chave_identidade,
                        preco_minimo,
                        preco_comum,
                        preco_maximo,
                        situacao_mercado,
                        COUNT(*) AS observation_count
                    FROM new_rows
                    GROUP BY
                        chave_identidade,
                        preco_minimo,
                        preco_comum,
                        preco_maximo,
                        situacao_mercado
                ),
                classified_groups AS (
                    SELECT
                        observation_count,
                        CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM cotacoes previous
                                WHERE previous.id <= ?
                                    AND previous.chave_identidade =
                                        new_groups.chave_identidade
                                    AND previous.preco_minimo IS
                                        new_groups.preco_minimo
                                    AND previous.preco_comum IS
                                        new_groups.preco_comum
                                    AND previous.preco_maximo IS
                                        new_groups.preco_maximo
                                    AND previous.situacao_mercado IS
                                        new_groups.situacao_mercado
                            )
                            THEN 0
                            ELSE 1
                        END AS is_logical_new
                    FROM new_groups
                )
                SELECT
                    COALESCE(SUM(observation_count), 0),
                    COALESCE(SUM(is_logical_new), 0),
                    COALESCE(SUM(observation_count - is_logical_new), 0)
                FROM classified_groups
                """,
                (previous_max_id, previous_max_id),
            ).fetchone()

        observations_inserted, logical_new, repeated_observations = row or (
            0,
            0,
            0,
        )

        return LogicalCotacaoDelta(
            previous_max_id=previous_max_id,
            current_max_id=current_max_id,
            observations_inserted=int(observations_inserted),
            logical_new=int(logical_new),
            repeated_observations=int(repeated_observations),
        )

    def _connect_read_only(self) -> sqlite3.Connection:
        database_uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        return sqlite3.connect(database_uri, uri=True)

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS estados (
                id INTEGER PRIMARY KEY,
                nome TEXT NOT NULL,
                uf TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS ceasas (
                id INTEGER PRIMARY KEY,
                estado_id INTEGER NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                nome TEXT NOT NULL,
                url_origem TEXT NOT NULL,
                FOREIGN KEY (estado_id) REFERENCES estados (id)
            );

            CREATE TABLE IF NOT EXISTS entrepostos (
                id INTEGER PRIMARY KEY,
                ceasa_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                nome TEXT NOT NULL,
                UNIQUE (ceasa_id, slug),
                FOREIGN KEY (ceasa_id) REFERENCES ceasas (id)
            );

            CREATE TABLE IF NOT EXISTS categorias (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY,
                nome_normalizado TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS produto_aliases (
                id INTEGER PRIMARY KEY,
                produto_id INTEGER NOT NULL,
                nome_original TEXT NOT NULL,
                UNIQUE (produto_id, nome_original),
                FOREIGN KEY (produto_id) REFERENCES produtos (id)
            );

            CREATE TABLE IF NOT EXISTS unidades (
                id INTEGER PRIMARY KEY,
                sigla TEXT NOT NULL UNIQUE,
                descricao TEXT
            );

            CREATE TABLE IF NOT EXISTS apresentacoes_unidade (
                id INTEGER PRIMARY KEY,
                chave_unica TEXT NOT NULL UNIQUE,
                unidade_id INTEGER,
                unidade_original TEXT,
                unidade_normalizada TEXT,
                embalagem TEXT,
                quantidade_minima NUMERIC,
                quantidade_maxima NUMERIC,
                detalhe_unidade TEXT,
                FOREIGN KEY (unidade_id) REFERENCES unidades (id)
            );

            CREATE TABLE IF NOT EXISTS coletas (
                id INTEGER PRIMARY KEY,
                chave_unica TEXT NOT NULL UNIQUE,
                ceasa_id INTEGER NOT NULL,
                arquivo_raw TEXT,
                hash_raw TEXT,
                url_origem TEXT NOT NULL,
                baixado_em TEXT,
                processado_em TEXT NOT NULL,
                FOREIGN KEY (ceasa_id) REFERENCES ceasas (id)
            );

            CREATE TABLE IF NOT EXISTS cotacoes (
                id INTEGER PRIMARY KEY,
                chave_unica TEXT NOT NULL UNIQUE,
                chave_identidade TEXT NOT NULL,
                coleta_id INTEGER NOT NULL,
                entreposto_id INTEGER,
                categoria_id INTEGER NOT NULL,
                produto_alias_id INTEGER NOT NULL,
                apresentacao_unidade_id INTEGER,
                data_cotacao TEXT NOT NULL,
                preco_minimo NUMERIC CHECK (preco_minimo IS NULL OR preco_minimo >= 0),
                preco_comum NUMERIC CHECK (preco_comum IS NULL OR preco_comum >= 0),
                preco_maximo NUMERIC CHECK (preco_maximo IS NULL OR preco_maximo >= 0),
                procedencia TEXT,
                classificacao TEXT,
                situacao_mercado TEXT,
                fonte_complemento TEXT,
                url_complemento TEXT,
                data_complemento TEXT,
                FOREIGN KEY (coleta_id) REFERENCES coletas (id),
                FOREIGN KEY (entreposto_id) REFERENCES entrepostos (id),
                FOREIGN KEY (categoria_id) REFERENCES categorias (id),
                FOREIGN KEY (produto_alias_id) REFERENCES produto_aliases (id),
                FOREIGN KEY (apresentacao_unidade_id) REFERENCES apresentacoes_unidade (id)
            );

            CREATE TABLE IF NOT EXISTS backfill_states (
                source_slug TEXT NOT NULL,
                category_slug TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                cursor_date TEXT,
                consecutive_no_progress INTEGER NOT NULL DEFAULT 0,
                next_check_date TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_slug, category_slug)
            );

            CREATE INDEX IF NOT EXISTS idx_coletas_ceasa
                ON coletas (ceasa_id);
            CREATE INDEX IF NOT EXISTS idx_cotacoes_identidade
                ON cotacoes (chave_identidade);
            CREATE INDEX IF NOT EXISTS idx_cotacoes_coleta
                ON cotacoes (coleta_id);
            CREATE INDEX IF NOT EXISTS idx_cotacoes_entreposto_data
                ON cotacoes (entreposto_id, data_cotacao);
            CREATE INDEX IF NOT EXISTS idx_cotacoes_categoria_data
                ON cotacoes (categoria_id, data_cotacao);
            CREATE INDEX IF NOT EXISTS idx_cotacoes_produto_data
                ON cotacoes (produto_alias_id, data_cotacao);
            CREATE INDEX IF NOT EXISTS idx_produto_aliases_produto
                ON produto_aliases (produto_id);
            """
        )
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version < SQLITE_SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")

    def insert_cotacoes(
        self,
        connection: sqlite3.Connection,
        cotacoes: list[Cotacao],
        ceasa_id: int,
        source_slug: str,
        default_market: str,
    ) -> int:
        processado_em = datetime.now().isoformat(timespec="seconds")
        coletas_cache: dict[str, tuple[int, str]] = {}
        entrepostos_cache: dict[
            tuple[int, str | None],
            tuple[int | None, str | None, str | None],
        ] = {}
        categorias_cache: dict[str, int] = {}
        produtos_aliases_cache: dict[tuple[str, str], tuple[int, str]] = {}
        unidades_cache: dict[str, tuple[int, str | None]] = {}
        apresentacoes_cache: dict[str | None, tuple[int | None, str | None]] = {}
        rows: list[tuple[object | None, ...]] = []

        for cotacao in cotacoes:
            self._validate_cotacao(cotacao)
            coleta_key = self._build_collection_key(
                cotacao=cotacao,
                source_slug=source_slug,
                processado_em=processado_em,
            )
            coleta = coletas_cache.get(coleta_key)
            if coleta is None:
                coleta = self._get_or_create_coleta(
                    connection,
                    cotacao,
                    ceasa_id,
                    source_slug,
                    processado_em,
                )
                coletas_cache[coleta_key] = coleta
            coleta_id, _ = coleta

            market_name = cotacao.entreposto or self._default_market(default_market)
            market_slug = slugify(market_name) if market_name is not None else None
            entreposto_cache_key = (ceasa_id, market_slug)
            entreposto = entrepostos_cache.get(entreposto_cache_key)
            if entreposto is None or entreposto[2] != market_name:
                entreposto_id, entreposto_slug = self._get_or_create_entreposto(
                    connection,
                    ceasa_id,
                    market_name,
                )
                entreposto = (entreposto_id, entreposto_slug, market_name)
                entrepostos_cache[entreposto_cache_key] = entreposto
            entreposto_id, entreposto_slug, _ = entreposto

            categoria_id = categorias_cache.get(cotacao.categoria)
            if categoria_id is None:
                categoria_id = self._get_or_create_categoria(
                    connection,
                    cotacao.categoria,
                )
                categorias_cache[cotacao.categoria] = categoria_id

            product_cache_key = (
                self._normalize_name(cotacao.produto),
                cotacao.produto,
            )
            product_alias = produtos_aliases_cache.get(product_cache_key)
            if product_alias is None:
                product_alias = self._get_or_create_product_alias(
                    connection,
                    cotacao.produto,
                )
                produtos_aliases_cache[product_cache_key] = product_alias
            produto_alias_id, product_key = product_alias

            normalized_unit = normalize_unit(cotacao.unidade)
            unidade_id = None
            if normalized_unit.original is not None:
                unidade = (
                    unidades_cache.get(normalized_unit.symbol)
                    if normalized_unit.symbol is not None
                    else None
                )
                if unidade is None or unidade[1] != normalized_unit.description:
                    unidade_id = self._get_or_create_unidade(
                        connection,
                        normalized_unit,
                    )
                    if normalized_unit.symbol is not None and unidade_id is not None:
                        unidades_cache[normalized_unit.symbol] = (
                            unidade_id,
                            normalized_unit.description,
                        )
                else:
                    unidade_id = unidade[0]

            presentation_key = self._build_presentation_key(normalized_unit)
            presentation = apresentacoes_cache.get(presentation_key)
            if presentation is None:
                presentation = self._get_or_create_presentation(
                    connection,
                    normalized_unit,
                    unidade_id,
                )
                apresentacoes_cache[presentation_key] = presentation
            presentation_id, presentation_key = presentation

            identity_key = self._build_identity_key(
                source_slug=source_slug,
                market_slug=entreposto_slug,
                category_slug=cotacao.categoria,
                product_key=product_key,
                presentation_key=presentation_key,
                cotacao=cotacao,
            )
            unique_key = self.build_content_key(
                identity_key=identity_key,
                preco_minimo=cotacao.preco_minimo,
                preco_comum=cotacao.preco_comum,
                preco_maximo=cotacao.preco_maximo,
                situacao_mercado=cotacao.situacao_mercado,
            )
            preco_minimo = self._decimal_to_db(cotacao.preco_minimo)
            preco_comum = self._decimal_to_db(cotacao.preco_comum)
            preco_maximo = self._decimal_to_db(cotacao.preco_maximo)
            rows.append(
                (
                    unique_key,
                    identity_key,
                    coleta_id,
                    entreposto_id,
                    categoria_id,
                    produto_alias_id,
                    presentation_id,
                    cotacao.data_cotacao.isoformat(),
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    cotacao.procedencia,
                    cotacao.classificacao,
                    cotacao.situacao_mercado,
                    cotacao.fonte_complemento,
                    cotacao.url_complemento,
                    (
                        cotacao.data_complemento.isoformat(timespec="seconds")
                        if cotacao.data_complemento is not None
                        else None
                    ),
                    identity_key,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    cotacao.situacao_mercado,
                )
            )

        changes_before = connection.total_changes
        connection.executemany(
            """
            INSERT INTO cotacoes (
                chave_unica,
                chave_identidade,
                coleta_id,
                entreposto_id,
                categoria_id,
                produto_alias_id,
                apresentacao_unidade_id,
                data_cotacao,
                preco_minimo,
                preco_comum,
                preco_maximo,
                procedencia,
                classificacao,
                situacao_mercado,
                fonte_complemento,
                url_complemento,
                data_complemento
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1
                FROM cotacoes existing
                WHERE existing.chave_identidade = ?
                  AND existing.preco_minimo IS ?
                  AND existing.preco_comum IS ?
                  AND existing.preco_maximo IS ?
                  AND existing.situacao_mercado IS ?
            )
            ON CONFLICT (chave_unica) DO NOTHING
            """,
            rows,
        )

        return connection.total_changes - changes_before

    def _validate_cotacao(self, cotacao: Cotacao) -> None:
        if cotacao.data_cotacao is None:
            raise ValueError(f"Cotacao sem data: {cotacao.produto}")

        prices = (cotacao.preco_minimo, cotacao.preco_comum, cotacao.preco_maximo)

        if all(price is None for price in prices):
            raise ValueError(f"Cotacao sem preco: {cotacao.produto}")

        if any(price is not None and price < 0 for price in prices):
            raise ValueError(f"Cotacao com preco negativo: {cotacao.produto}")

    def _get_or_create_coleta(
        self,
        connection: sqlite3.Connection,
        cotacao: Cotacao,
        ceasa_id: int,
        source_slug: str,
        processado_em: str,
    ) -> tuple[int, str]:
        baixado_em = self._format_datetime(cotacao.baixado_em)
        collection_key = self._build_collection_key(
            cotacao=cotacao,
            source_slug=source_slug,
            processado_em=processado_em,
        )
        connection.execute(
            """
            INSERT INTO coletas (
                chave_unica,
                ceasa_id,
                arquivo_raw,
                hash_raw,
                url_origem,
                baixado_em,
                processado_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chave_unica) DO NOTHING
            """,
            (
                collection_key,
                ceasa_id,
                cotacao.arquivo_raw,
                cotacao.hash_raw,
                cotacao.url_origem,
                baixado_em,
                processado_em,
            ),
        )

        return (
            self._fetch_id(connection, "coletas", "chave_unica", collection_key),
            collection_key,
        )

    def _get_or_create_entreposto(
        self,
        connection: sqlite3.Connection,
        ceasa_id: int,
        name: str | None,
    ) -> tuple[int | None, str | None]:
        if name is None:
            return None, None

        market_slug = slugify(name)
        connection.execute(
            """
            INSERT INTO entrepostos (ceasa_id, slug, nome)
            VALUES (?, ?, ?)
            ON CONFLICT (ceasa_id, slug) DO UPDATE SET nome = excluded.nome
            """,
            (ceasa_id, market_slug, name),
        )
        row = connection.execute(
            "SELECT id FROM entrepostos WHERE ceasa_id = ? AND slug = ?",
            (ceasa_id, market_slug),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Entreposto nao encontrado apos insert: {name}")

        return int(row[0]), market_slug

    def _get_or_create_categoria(
        self,
        connection: sqlite3.Connection,
        category_slug: str,
    ) -> int:
        connection.execute(
            """
            INSERT INTO categorias (slug)
            VALUES (?)
            ON CONFLICT (slug) DO NOTHING
            """,
            (category_slug,),
        )

        return self._fetch_id(connection, "categorias", "slug", category_slug)

    def _get_or_create_product_alias(
        self,
        connection: sqlite3.Connection,
        product_name: str,
    ) -> tuple[int, str]:
        normalized_name = self._normalize_name(product_name)
        connection.execute(
            """
            INSERT INTO produtos (nome_normalizado)
            VALUES (?)
            ON CONFLICT (nome_normalizado) DO NOTHING
            """,
            (normalized_name,),
        )
        product_id = self._fetch_id(
            connection,
            "produtos",
            "nome_normalizado",
            normalized_name,
        )
        connection.execute(
            """
            INSERT INTO produto_aliases (produto_id, nome_original)
            VALUES (?, ?)
            ON CONFLICT (produto_id, nome_original) DO NOTHING
            """,
            (product_id, product_name),
        )
        row = connection.execute(
            """
            SELECT id
            FROM produto_aliases
            WHERE produto_id = ? AND nome_original = ?
            """,
            (product_id, product_name),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Alias de produto nao encontrado: {product_name}")

        return int(row[0]), normalized_name

    def _get_or_create_presentation(
        self,
        connection: sqlite3.Connection,
        unit: NormalizedUnit,
        unidade_id: int | None,
    ) -> tuple[int | None, str | None]:
        if unit.original is None:
            return None, None

        presentation_key = self._build_presentation_key(unit)
        connection.execute(
            """
            INSERT INTO apresentacoes_unidade (
                chave_unica,
                unidade_id,
                unidade_original,
                unidade_normalizada,
                embalagem,
                quantidade_minima,
                quantidade_maxima,
                detalhe_unidade
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (chave_unica) DO NOTHING
            """,
            (
                presentation_key,
                unidade_id,
                unit.original,
                unit.normalized,
                unit.packaging,
                self._decimal_to_db(unit.quantity_min),
                self._decimal_to_db(unit.quantity_max),
                unit.detail,
            ),
        )

        return (
            self._fetch_id(
                connection,
                "apresentacoes_unidade",
                "chave_unica",
                presentation_key,
            ),
            presentation_key,
        )

    def _build_collection_key(
        self,
        cotacao: Cotacao,
        source_slug: str,
        processado_em: str,
    ) -> str:
        return self._hash_values(
            (
                source_slug,
                cotacao.arquivo_raw,
                cotacao.hash_raw,
                cotacao.url_origem,
                self._format_datetime(cotacao.baixado_em) or processado_em,
            )
        )

    def _build_presentation_key(self, unit: NormalizedUnit) -> str | None:
        if unit.original is None:
            return None

        return self._hash_values(
            (
                unit.original,
                unit.normalized,
                unit.symbol,
                unit.packaging,
                self._decimal_to_db(unit.quantity_min),
                self._decimal_to_db(unit.quantity_max),
                unit.detail,
            )
        )

    def _get_or_create_ceasa(
        self,
        connection: sqlite3.Connection,
        source_slug: str,
        source_name: str,
        state_name: str,
        uf: str,
        source_url: str,
    ) -> int:
        estado_id = self._get_or_create_estado(connection, state_name, uf)
        connection.execute(
            """
            INSERT INTO ceasas (estado_id, slug, nome, url_origem)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (slug) DO UPDATE SET
                estado_id = excluded.estado_id,
                nome = excluded.nome,
                url_origem = excluded.url_origem
            """,
            (estado_id, source_slug, source_name, source_url),
        )

        return self._fetch_id(connection, "ceasas", "slug", source_slug)

    def _get_or_create_estado(
        self,
        connection: sqlite3.Connection,
        state_name: str,
        uf: str,
    ) -> int:
        connection.execute(
            """
            INSERT INTO estados (nome, uf)
            VALUES (?, ?)
            ON CONFLICT (uf) DO UPDATE SET nome = excluded.nome
            """,
            (state_name, uf),
        )

        return self._fetch_id(connection, "estados", "uf", uf)

    def _get_or_create_unidade(
        self,
        connection: sqlite3.Connection,
        unit: NormalizedUnit,
    ) -> int | None:
        if unit.symbol is None:
            return None

        connection.execute(
            """
            INSERT INTO unidades (sigla, descricao)
            VALUES (?, ?)
            ON CONFLICT (sigla) DO UPDATE SET descricao = excluded.descricao
            """,
            (unit.symbol, unit.description),
        )

        return self._fetch_id(connection, "unidades", "sigla", unit.symbol)

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

    def _build_identity_key(
        self,
        source_slug: str,
        market_slug: str | None,
        category_slug: str,
        product_key: str,
        presentation_key: str | None,
        cotacao: Cotacao,
    ) -> str:
        return self._hash_values(
            (
                source_slug,
                market_slug,
                category_slug,
                product_key,
                presentation_key,
                cotacao.procedencia,
                cotacao.classificacao,
                cotacao.data_cotacao.isoformat() if cotacao.data_cotacao else None,
            )
        )

    def build_content_key(
        self,
        identity_key: str,
        preco_minimo: Decimal | int | float | str | None,
        preco_comum: Decimal | int | float | str | None,
        preco_maximo: Decimal | int | float | str | None,
        situacao_mercado: str | None,
    ) -> str:
        """Gera a chave logica da cotacao sem incluir a coleta de origem."""
        return self._hash_values(
            (
                identity_key,
                self._decimal_to_key(preco_minimo),
                self._decimal_to_key(preco_comum),
                self._decimal_to_key(preco_maximo),
                situacao_mercado,
            )
        )

    def _hash_values(self, values: tuple[object | None, ...]) -> str:
        raw_key = "|".join(str(value) if value is not None else "" for value in values)

        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _decimal_to_db(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    def _decimal_to_key(
        self,
        value: Decimal | int | float | str | None,
    ) -> str | None:
        if value is None:
            return None

        normalized = Decimal(str(value)).normalize()

        return "0" if normalized == 0 else format(normalized, "f")

    def _format_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat(timespec="seconds") if value is not None else None

    def _normalize_name(self, value: str) -> str:
        return " ".join(value.lower().split())

    def _default_market(self, city: str) -> str | None:
        return None if city.lower() == "varias cidades" else city
