import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.models import Cotacao


@dataclass(frozen=True)
class SQLiteStorage:
    """Persiste cotacoes normalizadas em um arquivo SQLite."""

    database_path: Path

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
                data_cotacao TEXT,
                preco_minimo NUMERIC,
                preco_comum NUMERIC,
                preco_maximo NUMERIC,
                procedencia TEXT,
                classificacao TEXT,
                situacao_mercado TEXT,
                data_coleta TEXT NOT NULL,
                url_origem TEXT NOT NULL,
                FOREIGN KEY (ceasa_id) REFERENCES ceasas (id),
                FOREIGN KEY (categoria_id) REFERENCES categorias (id),
                FOREIGN KEY (produto_id) REFERENCES produtos (id),
                FOREIGN KEY (unidade_id) REFERENCES unidades (id)
            )
            """
        )

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
            unidade_id = self._get_or_create_unidade(connection, cotacao.unidade)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO cotacoes (
                    chave_unica,
                    ceasa_id,
                    categoria_id,
                    produto_id,
                    unidade_id,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._build_unique_key(cotacao, ceasa_id),
                    ceasa_id,
                    categoria_id,
                    produto_id,
                    unidade_id,
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
        unit: str | None,
    ) -> int | None:
        if unit is None:
            return None

        connection.execute(
            "INSERT OR IGNORE INTO unidades (sigla, descricao) VALUES (?, ?)",
            (unit, unit),
        )

        return self._fetch_id(connection, "unidades", "sigla", unit)

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
