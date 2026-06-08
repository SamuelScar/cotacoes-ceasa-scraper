import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.normalizers.text import slugify
from cotacoes_ceasa.normalizers.unit import NormalizedUnit, normalize_unit


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

    def insert_cotacoes(
        self,
        connection: sqlite3.Connection,
        cotacoes: list[Cotacao],
        ceasa_id: int,
        source_slug: str,
        default_market: str,
    ) -> int:
        inserted_count = 0
        processado_em = datetime.now().isoformat(timespec="seconds")

        for cotacao in cotacoes:
            self._validate_cotacao(cotacao)
            coleta_id, coleta_key = self._get_or_create_coleta(
                connection,
                cotacao,
                ceasa_id,
                source_slug,
                processado_em,
            )
            entreposto_id, entreposto_slug = self._get_or_create_entreposto(
                connection,
                ceasa_id,
                cotacao.entreposto or self._default_market(default_market),
            )
            categoria_id = self._get_or_create_categoria(connection, cotacao.categoria)
            produto_alias_id, product_key = self._get_or_create_product_alias(
                connection,
                cotacao.produto,
            )
            presentation_id, presentation_key = self._get_or_create_presentation(
                connection,
                normalize_unit(cotacao.unidade),
            )
            identity_key = self._build_identity_key(
                source_slug=source_slug,
                market_slug=entreposto_slug,
                category_slug=cotacao.categoria,
                product_key=product_key,
                presentation_key=presentation_key,
                cotacao=cotacao,
            )
            unique_key = self._build_unique_key(identity_key, coleta_key, cotacao)
            cursor = connection.execute(
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (chave_unica) DO NOTHING
                """,
                (
                    unique_key,
                    identity_key,
                    coleta_id,
                    entreposto_id,
                    categoria_id,
                    produto_alias_id,
                    presentation_id,
                    cotacao.data_cotacao.isoformat(),
                    self._decimal_to_db(cotacao.preco_minimo),
                    self._decimal_to_db(cotacao.preco_comum),
                    self._decimal_to_db(cotacao.preco_maximo),
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
                ),
            )
            inserted_count += cursor.rowcount

        return inserted_count

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
        baixado_em = (
            cotacao.baixado_em.isoformat(timespec="seconds")
            if cotacao.baixado_em is not None
            else None
        )
        collection_key = self._hash_values(
            (
                source_slug,
                cotacao.arquivo_raw,
                cotacao.hash_raw,
                cotacao.url_origem,
                baixado_em or processado_em,
            )
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
    ) -> tuple[int | None, str | None]:
        if unit.original is None:
            return None, None

        unidade_id = self._get_or_create_unidade(connection, unit)
        presentation_key = self._hash_values(
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

    def _build_unique_key(
        self,
        identity_key: str,
        collection_key: str,
        cotacao: Cotacao,
    ) -> str:
        return self._hash_values(
            (
                identity_key,
                collection_key,
                self._decimal_to_db(cotacao.preco_minimo),
                self._decimal_to_db(cotacao.preco_comum),
                self._decimal_to_db(cotacao.preco_maximo),
                cotacao.situacao_mercado,
            )
        )

    def _hash_values(self, values: tuple[object | None, ...]) -> str:
        raw_key = "|".join(str(value) if value is not None else "" for value in values)

        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def _decimal_to_db(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    def _normalize_name(self, value: str) -> str:
        return " ".join(value.lower().split())

    def _default_market(self, city: str) -> str | None:
        return None if city.lower() == "varias cidades" else city
