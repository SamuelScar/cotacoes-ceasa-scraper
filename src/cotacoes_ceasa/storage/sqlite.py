import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.models import Cotacao
from cotacoes_ceasa.normalizers.unit import NormalizedUnit, normalize_unit


@dataclass(frozen=True)
class UnitNormalizationResult:
    quotation_count: int
    canonical_unit_count: int
    removed_unit_count: int
    unrecognized_count: int


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
                city=city,
                source_url=source_url,
            )
            return self._insert_cotacoes(connection, cotacoes, ceasa_id)

    def normalize_units(self) -> UnitNormalizationResult:
        """Normaliza unidades existentes e remove variacoes brutas sem uso."""
        if not self.database_path.exists():
            raise FileNotFoundError(f"Banco SQLite nao encontrado: {self.database_path}")

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self._create_schema(connection)
            rows = connection.execute(
                """
                SELECT co.id, co.unidade_original, u.sigla
                FROM cotacoes co
                LEFT JOIN unidades u ON u.id = co.unidade_id
                """
            ).fetchall()
            unrecognized_count = 0

            for quotation_id, original_unit, saved_unit in rows:
                normalized_unit = normalize_unit(original_unit or saved_unit)
                unidade_id = self._get_or_create_unidade(connection, normalized_unit)

                if (
                    normalized_unit.original is not None
                    and normalized_unit.symbol is None
                    and normalized_unit.packaging is None
                ):
                    unrecognized_count += 1

                connection.execute(
                    """
                    UPDATE cotacoes
                    SET
                        unidade_id = ?,
                        unidade_original = ?,
                        unidade_normalizada = ?,
                        embalagem = ?,
                        quantidade_minima = ?,
                        quantidade_maxima = ?,
                        detalhe_unidade = ?
                    WHERE id = ?
                    """,
                    (
                        unidade_id,
                        normalized_unit.original,
                        normalized_unit.normalized,
                        normalized_unit.packaging,
                        self._decimal_to_db(normalized_unit.quantity_min),
                        self._decimal_to_db(normalized_unit.quantity_max),
                        normalized_unit.detail,
                        quotation_id,
                    ),
                )

            delete_cursor = connection.execute(
                """
                DELETE FROM unidades
                WHERE id NOT IN (
                    SELECT DISTINCT unidade_id
                    FROM cotacoes
                    WHERE unidade_id IS NOT NULL
                )
                """
            )
            canonical_unit_count = connection.execute(
                "SELECT COUNT(*) FROM unidades"
            ).fetchone()[0]

            return UnitNormalizationResult(
                quotation_count=len(rows),
                canonical_unit_count=canonical_unit_count,
                removed_unit_count=delete_cursor.rowcount,
                unrecognized_count=unrecognized_count,
            )

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        self._ensure_compatible_schema(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS estados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                uf TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ceasas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                estado_id INTEGER NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                nome TEXT NOT NULL,
                cidade TEXT,
                url_origem TEXT NOT NULL,
                FOREIGN KEY (estado_id) REFERENCES estados (id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS categorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ceasa_id INTEGER NOT NULL,
                slug TEXT NOT NULL,
                nome TEXT NOT NULL,
                UNIQUE (ceasa_id, slug),
                FOREIGN KEY (ceasa_id) REFERENCES ceasas (id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome_original TEXT NOT NULL,
                nome_normalizado TEXT NOT NULL,
                UNIQUE (nome_original, nome_normalizado)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS unidades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sigla TEXT NOT NULL UNIQUE,
                descricao TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cotacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chave_unica TEXT NOT NULL UNIQUE,
                ceasa_id INTEGER NOT NULL,
                categoria_id INTEGER NOT NULL,
                produto_id INTEGER NOT NULL,
                unidade_id INTEGER,
                unidade_original TEXT,
                unidade_normalizada TEXT,
                embalagem TEXT,
                quantidade_minima NUMERIC,
                quantidade_maxima NUMERIC,
                detalhe_unidade TEXT,
                data_cotacao TEXT,
                preco_minimo NUMERIC,
                preco_comum NUMERIC,
                preco_maximo NUMERIC,
                procedencia TEXT,
                classificacao TEXT,
                situacao_mercado TEXT,
                fonte_complemento TEXT,
                url_complemento TEXT,
                data_complemento TEXT,
                data_coleta TEXT NOT NULL,
                url_origem TEXT NOT NULL,
                FOREIGN KEY (ceasa_id) REFERENCES ceasas (id),
                FOREIGN KEY (categoria_id) REFERENCES categorias (id),
                FOREIGN KEY (produto_id) REFERENCES produtos (id),
                FOREIGN KEY (unidade_id) REFERENCES unidades (id)
            )
            """
        )
        self._ensure_complement_columns(connection)
        self._ensure_unit_columns(connection)

    def _insert_cotacoes(
        self,
        connection: sqlite3.Connection,
        cotacoes: list[Cotacao],
        ceasa_id: int,
    ) -> int:
        inserted_count = 0
        data_coleta = datetime.now().isoformat(timespec="seconds")

        for cotacao in cotacoes:
            categoria_id = self._get_or_create_categoria(connection, ceasa_id, cotacao.categoria)
            produto_id = self._get_or_create_produto(connection, cotacao.produto)
            normalized_unit = normalize_unit(cotacao.unidade)
            unidade_id = self._get_or_create_unidade(connection, normalized_unit)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO cotacoes (
                    chave_unica,
                    ceasa_id,
                    categoria_id,
                    produto_id,
                    unidade_id,
                    unidade_original,
                    unidade_normalizada,
                    embalagem,
                    quantidade_minima,
                    quantidade_maxima,
                    detalhe_unidade,
                    data_cotacao,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    procedencia,
                    classificacao,
                    situacao_mercado,
                    data_coleta,
                    url_origem
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._build_unique_key(cotacao, ceasa_id),
                    ceasa_id,
                    categoria_id,
                    produto_id,
                    unidade_id,
                    normalized_unit.original,
                    normalized_unit.normalized,
                    normalized_unit.packaging,
                    self._decimal_to_db(normalized_unit.quantity_min),
                    self._decimal_to_db(normalized_unit.quantity_max),
                    normalized_unit.detail,
                    cotacao.data_cotacao.isoformat() if cotacao.data_cotacao else None,
                    self._decimal_to_db(cotacao.preco_minimo),
                    self._decimal_to_db(cotacao.preco_comum),
                    self._decimal_to_db(cotacao.preco_maximo),
                    cotacao.procedencia,
                    cotacao.classificacao,
                    cotacao.situacao_mercado,
                    data_coleta,
                    cotacao.url_origem,
                ),
            )
            inserted_count += cursor.rowcount

        return inserted_count

    def _ensure_compatible_schema(self, connection: sqlite3.Connection) -> None:
        columns = self._get_table_columns(connection, "cotacoes")

        if not columns or "ceasa_id" in columns:
            return

        raise RuntimeError(
            "Banco SQLite antigo com tabela cotacoes flat detectado. "
            "Exclua o arquivo configurado em COTACOES_DATABASE_PATH para recriar "
            "o banco com o schema relacional."
        )

    def _ensure_complement_columns(self, connection: sqlite3.Connection) -> None:
        columns = self._get_table_columns(connection, "cotacoes")

        if not columns:
            return

        for column_name in (
            "fonte_complemento",
            "url_complemento",
            "data_complemento",
        ):
            if column_name not in columns:
                connection.execute(f"ALTER TABLE cotacoes ADD COLUMN {column_name} TEXT")

    def _ensure_unit_columns(self, connection: sqlite3.Connection) -> None:
        columns = self._get_table_columns(connection, "cotacoes")
        definitions = {
            "unidade_original": "TEXT",
            "unidade_normalizada": "TEXT",
            "embalagem": "TEXT",
            "quantidade_minima": "NUMERIC",
            "quantidade_maxima": "NUMERIC",
            "detalhe_unidade": "TEXT",
        }

        for column_name, column_type in definitions.items():
            if column_name not in columns:
                connection.execute(
                    f"ALTER TABLE cotacoes ADD COLUMN {column_name} {column_type}"
                )

    def _get_table_columns(
        self,
        connection: sqlite3.Connection,
        table_name: str,
    ) -> set[str]:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()

        return {row[1] for row in rows}

    def _get_or_create_ceasa(
        self,
        connection: sqlite3.Connection,
        source_slug: str,
        source_name: str,
        state_name: str,
        uf: str,
        city: str,
        source_url: str,
    ) -> int:
        estado_id = self._get_or_create_estado(connection, state_name, uf)
        connection.execute(
            """
            INSERT OR IGNORE INTO ceasas (
                estado_id,
                slug,
                nome,
                cidade,
                url_origem
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (estado_id, source_slug, source_name, city, source_url),
        )

        return self._fetch_id(connection, "ceasas", "slug", source_slug)

    def _get_or_create_estado(
        self,
        connection: sqlite3.Connection,
        state_name: str,
        uf: str,
    ) -> int:
        connection.execute(
            "INSERT OR IGNORE INTO estados (nome, uf) VALUES (?, ?)",
            (state_name, uf),
        )

        return self._fetch_id(connection, "estados", "uf", uf)

    def _get_or_create_categoria(
        self,
        connection: sqlite3.Connection,
        ceasa_id: int,
        slug: str,
    ) -> int:
        name = slug.replace("-", " ").title()
        connection.execute(
            """
            INSERT OR IGNORE INTO categorias (
                ceasa_id,
                slug,
                nome
            )
            VALUES (?, ?, ?)
            """,
            (ceasa_id, slug, name),
        )

        row = connection.execute(
            "SELECT id FROM categorias WHERE ceasa_id = ? AND slug = ?",
            (ceasa_id, slug),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Categoria nao encontrada apos insert: {slug}")

        return int(row[0])

    def _get_or_create_produto(
        self,
        connection: sqlite3.Connection,
        product_name: str,
    ) -> int:
        normalized_name = self._normalize_name(product_name)
        connection.execute(
            """
            INSERT OR IGNORE INTO produtos (
                nome_original,
                nome_normalizado
            )
            VALUES (?, ?)
            """,
            (product_name, normalized_name),
        )

        row = connection.execute(
            """
            SELECT id
            FROM produtos
            WHERE nome_original = ? AND nome_normalizado = ?
            """,
            (product_name, normalized_name),
        ).fetchone()

        if row is None:
            raise RuntimeError(f"Produto nao encontrado apos insert: {product_name}")

        return int(row[0])

    def _get_or_create_unidade(
        self,
        connection: sqlite3.Connection,
        unit: NormalizedUnit,
    ) -> int | None:
        if unit.symbol is None:
            return None

        connection.execute(
            "INSERT OR IGNORE INTO unidades (sigla, descricao) VALUES (?, ?)",
            (unit.symbol, unit.description),
        )
        connection.execute(
            "UPDATE unidades SET descricao = ? WHERE sigla = ?",
            (unit.description, unit.symbol),
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

    def _build_unique_key(self, cotacao: Cotacao, ceasa_id: int) -> str:
        values = (
            str(ceasa_id),
            cotacao.categoria,
            cotacao.produto,
            cotacao.unidade,
            cotacao.procedencia,
            cotacao.classificacao,
            cotacao.data_cotacao.isoformat() if cotacao.data_cotacao else None,
            self._decimal_to_db(cotacao.preco_minimo),
            self._decimal_to_db(cotacao.preco_comum),
            self._decimal_to_db(cotacao.preco_maximo),
            cotacao.situacao_mercado,
            cotacao.url_origem,
        )
        raw_key = "|".join(value or "" for value in values)

        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _decimal_to_db(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    def _normalize_name(self, value: str) -> str:
        return " ".join(value.lower().split())
