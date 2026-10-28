"""Auditoria somente leitura para o esquema SQLite v4 da issue 9."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Iterable
from zoneinfo import ZoneInfo

from cotacoes_ceasa.storage.sqlite_v4 import (
    SQLITE_V4_MIGRATION_KEY,
    SQLITE_V4_SCHEMA_VERSION,
)


AUDIT_TIMEZONE = ZoneInfo("America/Sao_Paulo")
V4_TABLES = (
    "fontes",
    "entrepostos",
    "categorias",
    "produtos",
    "produto_aliases",
    "apresentacoes",
    "coletas",
    "cotacoes",
    "cotacao_complementos",
    "controle_backfill",
    "schema_migrations",
)
V4_REQUIRED_SCHEMA = {
    "fontes": {"id", "slug", "nome", "url_base", "uf"},
    "entrepostos": {"id", "fonte_id", "slug", "nome", "cidade", "uf"},
    "categorias": {"id", "slug", "nome"},
    "produtos": {"id", "categoria_id", "nome"},
    "produto_aliases": {"id", "produto_id", "texto_original"},
    "apresentacoes": {
        "id",
        "texto_original",
        "unidade_normalizada",
        "embalagem",
        "quantidade_minima",
        "quantidade_maxima",
        "detalhe",
    },
    "coletas": {
        "id",
        "fonte_id",
        "duplicada_de_id",
        "url_origem",
        "caminho_relativo_raw",
        "sha256",
        "iniciada_em",
        "baixado_em",
        "processado_em",
        "status",
        "mensagem_erro",
    },
    "cotacoes": {
        "id",
        "coleta_id",
        "entreposto_id",
        "produto_alias_id",
        "apresentacao_id",
        "data_cotacao",
        "variedade",
        "classificacao",
        "procedencia",
        "preco_minimo",
        "preco_comum",
        "preco_maximo",
        "situacao_mercado",
    },
    "cotacao_complementos": {
        "id",
        "cotacao_id",
        "coleta_id",
        "campo",
        "valor_anterior",
        "valor_novo",
        "complementado_em",
    },
    "controle_backfill": {
        "id",
        "fonte_id",
        "categoria_id",
        "status",
        "data_cursor",
        "tentativas_sem_progresso",
        "proxima_verificacao_em",
        "ultimo_erro",
        "atualizado_em",
    },
    "schema_migrations": {"chave", "aplicada_em", "checkpoint", "detalhes"},
}
V4_REQUIRED_INDEXES = {
    "uq_controle_backfill_fonte_geral",
    "uq_controle_backfill_fonte_categoria",
    "idx_entrepostos_fonte",
    "idx_produtos_categoria",
    "idx_produto_aliases_produto",
    "idx_coletas_fonte_status",
    "idx_coletas_fonte_sha256",
    "idx_cotacoes_coleta",
    "idx_cotacoes_entreposto_data",
    "idx_cotacoes_produto_data",
    "idx_cotacao_complementos_coleta",
}


@dataclass(frozen=True)
class V4Finding:
    code: str
    title: str
    severity: str
    groups: int
    occurrences: int
    report_file: str | None
    explanation: str


class V4DatabaseAuditor:
    """Valida a modelagem v4 sem aplicar inferencias comerciais aos dados."""

    def __init__(
        self,
        database_path: Path,
        output_directory: Path,
        sample_limit: int,
        gap_days: int,
        verify_raw: bool,
        full_integrity_check: bool,
        skip_semantic: bool,
        execution_context: dict[str, object],
        auditor_path: Path,
    ) -> None:
        self.database_path = database_path.resolve()
        self.output_directory = output_directory.resolve()
        self.sample_limit = sample_limit
        self.gap_days = gap_days
        self.verify_raw = verify_raw
        self.full_integrity_check = full_integrity_check
        self.skip_semantic = skip_semantic
        self.execution_context = execution_context
        self.auditor_path = auditor_path.resolve()
        self.findings: list[V4Finding] = []
        self.generated_files: list[str] = []
        self.stage_history: list[dict[str, str]] = []
        self.current_stage = "Inicializando auditoria v4"
        self.started_at: datetime | None = None
        self.initial_database_stat: dict[str, object] = {}
        self.environment: dict[str, object] = {}
        self._zip_indexes: dict[Path, dict[str, list[tuple[Path, str]]]] = {}

    def run(self) -> dict[str, object]:
        self._validate_paths()
        self.output_directory.mkdir(parents=True)
        self.started_at = self._now()
        self.initial_database_stat = self._database_stat()
        marker = self.output_directory / "_INCOMPLETA"
        self._write_atomic(
            marker,
            "Auditoria em andamento ou interrompida. Nao use estes arquivos "
            "para remover dados.\n",
        )
        self._write_execution_state("running")

        try:
            self._step("Validando esquema SQLite v4")
            self.environment = self._build_environment()
            with self._connect() as connection:
                initial_data_version = int(
                    connection.execute("PRAGMA data_version").fetchone()[0]
                )
                self.environment["sqlite_data_version_initial"] = initial_data_version
                schema_summary = self._validate_schema(connection)

                self._step("Validando integridade estrutural")
                structural = self._audit_structure(connection)

                self._step("Resumindo tabelas e fontes")
                table_counts = self._audit_table_counts(connection)
                source_summary = self._audit_source_coverage(connection)

                self._step("Validando documentos e estados das coletas")
                self._audit_collections(connection)

                self._step("Validando cotações conforme a regra da issue 9")
                self._audit_quotes(connection)

                self._step("Validando complementos do PROHORT")
                self._audit_complements(connection)

                self._step("Validando controle de backfill")
                self._audit_backfill(connection)

                self._step("Calculando lacunas temporais")
                self._audit_date_gaps(connection)

                raw_summary: dict[str, object] = {"status": "not_checked"}
                if self.verify_raw:
                    self._step("Conferindo raws preservados e SHA-256")
                    raw_summary = self._audit_raw_files(connection)

                final_data_version = int(
                    connection.execute("PRAGMA data_version").fetchone()[0]
                )
                self.environment["sqlite_data_version_final"] = final_data_version
                if final_data_version != initial_data_version:
                    self._add_finding(
                        "sqlite_changed_during_audit",
                        "SQLite recebeu escrita durante a auditoria",
                        "erro",
                        1,
                        1,
                        None,
                        "O PRAGMA data_version mudou durante a conexão de leitura.",
                    )

            self._audit_input_stability()
            self._write_json("ambiente_auditoria.json", self.environment)
            finished_at = self._now()
            result = self._build_result(
                started_at=self.started_at,
                finished_at=finished_at,
                schema_summary=schema_summary,
                structural=structural,
                table_counts=table_counts,
                source_summary=source_summary,
                raw_summary=raw_summary,
            )
            self._write_json("resultado.json", result)
            self._write_markdown(result)
            self._write_execution_state("completed", finished_at=finished_at)
            marker.unlink(missing_ok=True)
            return result
        except BaseException as error:
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            self._write_incomplete_summary(status, error)
            self._write_execution_state(status, finished_at=self._now(), error=error)
            raise

    def _validate_paths(self) -> None:
        if not self.database_path.is_file():
            raise RuntimeError(f"SQLite nao encontrado: {self.database_path}")
        if self.output_directory.exists():
            raise RuntimeError(
                f"O diretorio de saida ja existe: {self.output_directory}"
            )
        if self.database_path == self.output_directory:
            raise RuntimeError("A saida da auditoria nao pode substituir o SQLite.")

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.create_function("audit_valid_date", 1, self._valid_date)
        connection.create_function("audit_valid_datetime", 1, self._valid_datetime)
        connection.create_function("audit_valid_sha256", 1, self._valid_sha256)
        connection.create_function(
            "audit_valid_nonnegative_decimal",
            1,
            self._valid_nonnegative_decimal,
        )
        return connection

    def _validate_schema(self, connection: sqlite3.Connection) -> dict[str, object]:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(set(V4_REQUIRED_SCHEMA) - existing_tables)
        missing_columns: dict[str, list[str]] = {}
        for table, expected_columns in V4_REQUIRED_SCHEMA.items():
            if table not in existing_tables:
                continue
            actual_columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            missing = sorted(expected_columns - actual_columns)
            if missing:
                missing_columns[table] = missing

        existing_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        missing_indexes = sorted(V4_REQUIRED_INDEXES - existing_indexes)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        migration_present = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE chave = ?",
            (SQLITE_V4_MIGRATION_KEY,),
        ).fetchone() is not None if "schema_migrations" in existing_tables else False

        if missing_tables or missing_columns:
            raise RuntimeError(
                "Esquema SQLite v4 incompleto: "
                f"tabelas={missing_tables}, colunas={missing_columns}"
            )
        if user_version != SQLITE_V4_SCHEMA_VERSION:
            self._add_finding(
                "unexpected_user_version",
                "Versao declarada do SQLite diferente da v4",
                "erro",
                1,
                1,
                None,
                f"Esperado {SQLITE_V4_SCHEMA_VERSION}; encontrado {user_version}.",
            )
        if not migration_present:
            self._add_finding(
                "missing_v4_migration_marker",
                "Marcador da migração v4 ausente",
                "erro",
                1,
                1,
                None,
                f"A chave {SQLITE_V4_MIGRATION_KEY} não existe em schema_migrations.",
            )
        if missing_indexes:
            self._add_finding(
                "missing_v4_indexes",
                "Índices esperados do modelo v4 ausentes",
                "erro",
                len(missing_indexes),
                len(missing_indexes),
                None,
                ", ".join(missing_indexes),
            )
        return {
            "model": "sqlite_v4_issue9",
            "user_version": user_version,
            "migration_key": SQLITE_V4_MIGRATION_KEY,
            "migration_present": migration_present,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "missing_indexes": missing_indexes,
        }

    def _audit_structure(self, connection: sqlite3.Connection) -> dict[str, object]:
        pragma = "integrity_check" if self.full_integrity_check else "quick_check"
        integrity_rows = [
            str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")
        ]
        integrity_ok = integrity_rows == ["ok"]
        if not integrity_ok:
            self._add_finding(
                "sqlite_integrity_failed",
                "Falha na integridade interna do SQLite",
                "erro",
                len(integrity_rows),
                len(integrity_rows),
                None,
                "; ".join(integrity_rows[:10]),
            )

        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_keys:
            self._write_csv(
                "chaves_estrangeiras_invalidas.csv",
                ("tabela", "rowid", "tabela_referenciada", "indice_fk"),
                foreign_keys[: self.sample_limit],
            )
            self._add_finding(
                "foreign_key_violations",
                "Chaves estrangeiras inválidas",
                "erro",
                len(foreign_keys),
                len(foreign_keys),
                "chaves_estrangeiras_invalidas.csv",
                "Há relacionamentos apontando para registros inexistentes.",
            )
        return {
            "integrity_pragma": pragma,
            "integrity_ok": integrity_ok,
            "integrity_messages": integrity_rows,
            "foreign_key_violations": len(foreign_keys),
        }

    def _audit_table_counts(self, connection: sqlite3.Connection) -> dict[str, int]:
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in V4_TABLES
        }
        self._write_csv(
            "contagens_tabelas.csv",
            ("tabela", "registros"),
            ((table, count) for table, count in counts.items()),
        )
        return counts

    def _audit_source_coverage(
        self, connection: sqlite3.Connection
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """
            WITH resumo_coletas AS (
                SELECT
                    fonte_id,
                    COUNT(*) AS coletas,
                    SUM(status = 'processada') AS coletas_processadas,
                    SUM(status = 'descartada_duplicada') AS coletas_duplicadas,
                    SUM(status IN ('erro_download', 'erro_processamento'))
                        AS coletas_com_erro
                FROM coletas
                GROUP BY fonte_id
            ), resumo_cotacoes AS (
                SELECT
                    col.fonte_id,
                    COUNT(c.id) AS cotacoes,
                    MIN(c.data_cotacao) AS primeira_data,
                    MAX(c.data_cotacao) AS ultima_data
                FROM coletas col
                LEFT JOIN cotacoes c ON c.coleta_id = col.id
                GROUP BY col.fonte_id
            )
            SELECT
                f.slug AS fonte,
                COALESCE(rc.coletas, 0) AS coletas,
                COALESCE(rc.coletas_processadas, 0) AS coletas_processadas,
                COALESCE(rc.coletas_duplicadas, 0) AS coletas_duplicadas,
                COALESCE(rc.coletas_com_erro, 0) AS coletas_com_erro,
                COALESCE(rq.cotacoes, 0) AS cotacoes,
                rq.primeira_data,
                rq.ultima_data
            FROM fontes f
            LEFT JOIN resumo_coletas rc ON rc.fonte_id = f.id
            LEFT JOIN resumo_cotacoes rq ON rq.fonte_id = f.id
            ORDER BY f.slug
            """
        ).fetchall()
        headers = tuple(rows[0].keys()) if rows else (
            "fonte", "coletas", "coletas_processadas", "coletas_duplicadas",
            "coletas_com_erro", "cotacoes", "primeira_data", "ultima_data",
        )
        self._write_csv("cobertura_fontes.csv", headers, rows)
        return [dict(row) for row in rows]

    def _audit_collections(self, connection: sqlite3.Connection) -> None:
        self._audit_query(
            connection,
            "coletas_campos_inconsistentes",
            "Coletas incompatíveis com o próprio status",
            "erro",
            """
            SELECT id, status, fonte_id, duplicada_de_id, caminho_relativo_raw,
                   sha256, baixado_em, processado_em, mensagem_erro,
                   1 AS ocorrencias
            FROM coletas
            WHERE
                (status = 'processada' AND (
                    caminho_relativo_raw IS NULL OR sha256 IS NULL
                    OR baixado_em IS NULL OR processado_em IS NULL
                ))
                OR (status = 'descartada_duplicada' AND (
                    duplicada_de_id IS NULL OR caminho_relativo_raw IS NOT NULL
                    OR sha256 IS NULL OR baixado_em IS NULL
                    OR processado_em IS NOT NULL
                ))
                OR (status <> 'descartada_duplicada' AND duplicada_de_id IS NOT NULL)
                OR (status IN ('erro_download', 'erro_processamento')
                    AND mensagem_erro IS NULL)
                OR (status = 'erro_processamento' AND (
                    caminho_relativo_raw IS NULL OR sha256 IS NULL
                    OR baixado_em IS NULL
                ))
                OR audit_valid_datetime(iniciada_em) = 0
                OR (baixado_em IS NOT NULL AND audit_valid_datetime(baixado_em) = 0)
                OR (processado_em IS NOT NULL
                    AND audit_valid_datetime(processado_em) = 0)
            """,
            "O estado persistido não obedece ao contrato da tabela coletas.",
        )
        self._audit_query(
            connection,
            "sha256_invalidos",
            "Hashes de documento inválidos",
            "erro",
            """
            SELECT id, fonte_id, status, sha256, 1 AS ocorrencias
            FROM coletas
            WHERE sha256 IS NOT NULL AND audit_valid_sha256(sha256) = 0
            """,
            "SHA-256 deve ser hexadecimal minúsculo com 64 caracteres.",
        )
        self._audit_query(
            connection,
            "duplicatas_documentais_canonicas",
            "Mais de um documento canônico para fonte e SHA-256",
            "erro",
            """
            SELECT fonte_id, sha256, COUNT(*) AS ocorrencias,
                   GROUP_CONCAT(id) AS coletas
            FROM coletas
            WHERE sha256 IS NOT NULL
              AND status <> 'descartada_duplicada'
              AND caminho_relativo_raw IS NOT NULL
            GROUP BY fonte_id, sha256
            HAVING COUNT(*) > 1
            """,
            "A issue 9 deduplica documentos por fonte_id + SHA-256.",
        )
        self._audit_query(
            connection,
            "referencias_duplicadas_invalidas",
            "Coletas duplicadas apontam para documento incompatível",
            "erro",
            """
            SELECT d.id, d.fonte_id, d.sha256, d.duplicada_de_id,
                   o.fonte_id AS fonte_original, o.sha256 AS sha_original,
                   o.status AS status_original,
                   o.caminho_relativo_raw AS raw_original,
                   1 AS ocorrencias
            FROM coletas d
            LEFT JOIN coletas o ON o.id = d.duplicada_de_id
            WHERE d.status = 'descartada_duplicada'
              AND (
                  o.id IS NULL OR o.fonte_id <> d.fonte_id
                  OR o.sha256 IS NOT d.sha256
                  OR o.status = 'descartada_duplicada'
                  OR o.caminho_relativo_raw IS NULL
              )
            """,
            "A tentativa repetida deve apontar para o raw canônico da mesma fonte e hash.",
        )
        self._audit_query(
            connection,
            "caminhos_raw_repetidos",
            "Mesmo caminho raw associado a mais de uma coleta canônica",
            "erro",
            """
            SELECT caminho_relativo_raw, COUNT(*) AS ocorrencias,
                   GROUP_CONCAT(id) AS coletas
            FROM coletas
            WHERE caminho_relativo_raw IS NOT NULL
            GROUP BY caminho_relativo_raw
            HAVING COUNT(*) > 1
            """,
            "Um arquivo físico preservado deve pertencer a uma única coleta canônica.",
        )
        self._audit_query(
            connection,
            "coletas_pendentes",
            "Coletas ainda pendentes no pacote publicado",
            "alerta",
            """
            SELECT id, fonte_id, url_origem, iniciada_em, caminho_relativo_raw,
                   sha256, 1 AS ocorrencias
            FROM coletas
            WHERE status = 'pendente'
            """,
            "Uma coleta pendente pode indicar execução interrompida antes da consolidação.",
        )

    def _audit_quotes(self, connection: sqlite3.Connection) -> None:
        self._audit_query(
            connection,
            "cotacoes_fora_de_coleta_processada",
            "Cotações ligadas a coleta não processada",
            "erro",
            """
            SELECT c.id, c.coleta_id, col.status, 1 AS ocorrencias
            FROM cotacoes c
            JOIN coletas col ON col.id = c.coleta_id
            WHERE col.status <> 'processada'
            """,
            "Coletas duplicadas e com erro não podem originar cotações.",
        )
        self._audit_query(
            connection,
            "cotacoes_duplicadas_mesma_coleta",
            "Cotações idênticas repetidas dentro da mesma coleta",
            "erro",
            """
            SELECT coleta_id, entreposto_id, produto_alias_id, apresentacao_id,
                   data_cotacao, variedade, classificacao, procedencia,
                   preco_minimo, preco_comum, preco_maximo, situacao_mercado,
                   COUNT(*) AS ocorrencias, GROUP_CONCAT(id) AS cotacoes
            FROM cotacoes
            GROUP BY coleta_id, entreposto_id, produto_alias_id, apresentacao_id,
                     data_cotacao, variedade, classificacao, procedencia,
                     preco_minimo, preco_comum, preco_maximo, situacao_mercado
            HAVING COUNT(*) > 1
            """,
            "Só repetições exatas emitidas para a mesma coleta são inválidas; "
            "cotações iguais de documentos diferentes são preservadas.",
        )
        self._audit_query(
            connection,
            "cotacoes_valores_invalidos",
            "Cotações com data ou preços inválidos",
            "erro",
            """
            SELECT id, coleta_id, data_cotacao, preco_minimo, preco_comum,
                   preco_maximo, 1 AS ocorrencias
            FROM cotacoes
            WHERE audit_valid_date(data_cotacao) = 0
               OR (preco_minimo IS NULL AND preco_comum IS NULL AND preco_maximo IS NULL)
               OR COALESCE(preco_minimo, 0) < 0
               OR COALESCE(preco_comum, 0) < 0
               OR COALESCE(preco_maximo, 0) < 0
            """,
            "A data deve ser ISO, ao menos um preço deve existir e nenhum pode ser negativo.",
        )
        self._audit_query(
            connection,
            "cadastros_textuais_vazios",
            "Cadastros obrigatórios com texto vazio",
            "erro",
            """
            SELECT tipo, id, valor, 1 AS ocorrencias
            FROM (
                SELECT 'fonte_slug' AS tipo, id, slug AS valor FROM fontes
                UNION ALL SELECT 'fonte_nome', id, nome FROM fontes
                UNION ALL SELECT 'entreposto_slug', id, slug FROM entrepostos
                UNION ALL SELECT 'entreposto_nome', id, nome FROM entrepostos
                UNION ALL SELECT 'categoria_slug', id, slug FROM categorias
                UNION ALL SELECT 'categoria_nome', id, nome FROM categorias
                UNION ALL SELECT 'produto_nome', id, nome FROM produtos
                UNION ALL SELECT 'produto_alias', id, texto_original FROM produto_aliases
                UNION ALL SELECT 'apresentacao', id, texto_original FROM apresentacoes
            )
            WHERE trim(valor) = ''
            """,
            "Campos identificadores obrigatórios não podem conter apenas espaços.",
        )

    def _audit_complements(self, connection: sqlite3.Connection) -> None:
        self._audit_query(
            connection,
            "complementos_prohort_invalidos",
            "Histórico de complemento incompatível com a regra PROHORT",
            "erro",
            """
            SELECT cc.id, cc.cotacao_id, cc.coleta_id, cc.campo,
                   cc.valor_anterior, cc.valor_novo, cc.complementado_em,
                   f.slug AS fonte_complemento, col.status AS status_coleta,
                   c.preco_comum AS valor_atual, 1 AS ocorrencias
            FROM cotacao_complementos cc
            JOIN coletas col ON col.id = cc.coleta_id
            JOIN fontes f ON f.id = col.fonte_id
            JOIN cotacoes c ON c.id = cc.cotacao_id
            WHERE cc.campo <> 'preco_comum'
               OR cc.valor_anterior IS NOT NULL
               OR trim(cc.valor_novo) = ''
               OR audit_valid_nonnegative_decimal(cc.valor_novo) = 0
               OR c.preco_comum IS NULL
               OR CAST(c.preco_comum AS NUMERIC) <> CAST(cc.valor_novo AS NUMERIC)
               OR f.slug <> 'prohort'
               OR col.status <> 'processada'
               OR audit_valid_datetime(cc.complementado_em) = 0
            """,
            "O PROHORT só preenche preco_comum vazio e registra a coleta de origem.",
        )

    def _audit_backfill(self, connection: sqlite3.Connection) -> None:
        self._audit_query(
            connection,
            "controle_backfill_invalido",
            "Estados de backfill inválidos",
            "erro",
            """
            SELECT id, fonte_id, categoria_id, status, data_cursor,
                   tentativas_sem_progresso, proxima_verificacao_em,
                   atualizado_em, 1 AS ocorrencias
            FROM controle_backfill
            WHERE status NOT IN (
                    'complete', 'partial', 'paused_for_recheck', 'exhausted', 'failed'
                  )
               OR tentativas_sem_progresso < 0
               OR (data_cursor IS NOT NULL AND audit_valid_date(data_cursor) = 0)
               OR (proxima_verificacao_em IS NOT NULL
                   AND audit_valid_datetime(proxima_verificacao_em) = 0)
               OR audit_valid_datetime(atualizado_em) = 0
            """,
            "O estado operacional deve obedecer aos enums e formatos do modelo v4.",
        )
        self._audit_query(
            connection,
            "controle_backfill_duplicado",
            "Mais de um estado de backfill para o mesmo escopo",
            "erro",
            """
            SELECT fonte_id, categoria_id, COUNT(*) AS ocorrencias,
                   GROUP_CONCAT(id) AS registros
            FROM controle_backfill
            GROUP BY fonte_id, categoria_id
            HAVING COUNT(*) > 1
            """,
            "Deve existir no máximo um estado geral ou por categoria em cada fonte.",
        )

    def _audit_date_gaps(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            WITH datas AS (
                SELECT DISTINCT f.slug AS fonte, c.data_cotacao
                FROM cotacoes c
                JOIN coletas col ON col.id = c.coleta_id
                JOIN fontes f ON f.id = col.fonte_id
                WHERE audit_valid_date(c.data_cotacao) = 1
            ), ordenadas AS (
                SELECT fonte, data_cotacao,
                       LAG(data_cotacao) OVER (
                           PARTITION BY fonte ORDER BY data_cotacao
                       ) AS data_anterior
                FROM datas
            )
            SELECT fonte, data_anterior, data_cotacao,
                   CAST(julianday(data_cotacao) - julianday(data_anterior) AS INTEGER)
                       AS dias_sem_publicacao
            FROM ordenadas
            WHERE data_anterior IS NOT NULL
              AND julianday(data_cotacao) - julianday(data_anterior) >= ?
            ORDER BY fonte, data_anterior
            """,
            (self.gap_days,),
        ).fetchall()
        if rows:
            self._write_csv(
                "lacunas_datas.csv",
                tuple(rows[0].keys()),
                rows[: self.sample_limit],
            )
            self._add_finding(
                "date_gaps",
                "Lacunas temporais acima do limite configurado",
                "alerta",
                len(rows),
                len(rows),
                "lacunas_datas.csv",
                "Lacunas podem ser esperadas, mas devem ser comparadas com as fontes.",
            )

    def _audit_raw_files(self, connection: sqlite3.Connection) -> dict[str, object]:
        checked = 0
        direct_files = 0
        archived_files = 0
        unsafe: list[tuple[object, ...]] = []
        status_counts = {
            "exact_match": 0,
            "relocated_match": 0,
            "ambiguous_candidates": 0,
            "missing": 0,
            "hash_mismatch": 0,
            "unreadable": 0,
        }
        samples: dict[str, list[tuple[object, ...]]] = {
            status: [] for status in status_counts
        }
        sample_headers = (
            "coleta_id",
            "fonte",
            "caminho_relativo_raw",
            "local_encontrado",
            "classificacao",
            "sha256_registrado",
            "sha256_calculado",
            "quantidade_candidatos",
            "candidatos",
        )
        data_root = self.database_path.parent.resolve()
        rows = connection.execute(
            """
            SELECT c.id, f.slug, c.caminho_relativo_raw, c.sha256, c.status
            FROM coletas c
            JOIN fontes f ON f.id = c.fonte_id
            WHERE c.caminho_relativo_raw IS NOT NULL AND c.sha256 IS NOT NULL
            ORDER BY c.id
            """
        )
        for row in rows:
            checked += 1
            coleta_id, source_slug, relative_text, expected_hash, status = row
            relative_path = PurePosixPath(str(relative_text))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                unsafe.append((coleta_id, source_slug, relative_text, status))
                continue
            candidate = data_root.joinpath(*relative_path.parts)
            try:
                candidate.resolve().relative_to(data_root)
            except ValueError:
                unsafe.append((coleta_id, source_slug, relative_text, status))
                continue

            classification: str
            actual_hash: str | None = None
            location: str | None = None
            candidate_details: list[str] = []
            candidate_count = 0
            if candidate.is_file():
                location = candidate.as_posix()
                direct_files += 1
                candidate_count = 1
                try:
                    actual_hash = self._hash_file(candidate)
                except OSError as error:
                    classification = "unreadable"
                    candidate_details.append(
                        f"{location} [{type(error).__name__}: {error}]"
                    )
                else:
                    classification = (
                        "exact_match"
                        if actual_hash == expected_hash
                        else "hash_mismatch"
                    )
                    candidate_details.append(f"{location} [{actual_hash}]")
            else:
                old_file = (
                    self.database_path.parent
                    / "raw"
                    / str(source_slug)
                    / "old"
                    / candidate.name
                )
                alternatives: list[tuple[Path, str | None]] = []
                if old_file.is_file():
                    alternatives.append((old_file, None))
                alternatives.extend(
                    self._find_archived_raw(candidate, str(source_slug))
                )
                candidate_count = len(alternatives)
                if alternatives:
                    archived_files += 1
                    readable_hashes: list[str] = []
                    matching_location: str | None = None
                    unreadable_candidates = 0
                    for alternative_path, member in alternatives:
                        display_path = (
                            alternative_path.as_posix()
                            if member is None
                            else f"{alternative_path.as_posix()}::{member}"
                        )
                        try:
                            candidate_hash = (
                                self._hash_file(alternative_path)
                                if member is None
                                else self._hash_zip_member(
                                    alternative_path,
                                    member,
                                )
                            )
                        except (
                            OSError,
                            zipfile.BadZipFile,
                            KeyError,
                            RuntimeError,
                        ) as error:
                            unreadable_candidates += 1
                            candidate_details.append(
                                f"{display_path} "
                                f"[{type(error).__name__}: {error}]"
                            )
                            continue
                        readable_hashes.append(candidate_hash)
                        candidate_details.append(
                            f"{display_path} [{candidate_hash}]"
                        )
                        if (
                            matching_location is None
                            and candidate_hash == expected_hash
                        ):
                            matching_location = display_path

                    if matching_location is not None:
                        classification = "relocated_match"
                        location = matching_location
                        actual_hash = str(expected_hash)
                    elif unreadable_candidates:
                        classification = "unreadable"
                        location = self._raw_candidate_location(alternatives[0])
                        if readable_hashes:
                            actual_hash = " | ".join(
                                sorted(set(readable_hashes))
                            )
                    elif readable_hashes:
                        classification = "ambiguous_candidates"
                        location = self._raw_candidate_location(alternatives[0])
                        actual_hash = " | ".join(sorted(set(readable_hashes)))
                    else:
                        classification = "unreadable"
                        location = self._raw_candidate_location(alternatives[0])
                else:
                    classification = "missing"

            status_counts[classification] += 1
            if len(samples[classification]) < self.sample_limit:
                samples[classification].append(
                    (
                        coleta_id,
                        source_slug,
                        relative_text,
                        location,
                        classification,
                        expected_hash,
                        actual_hash,
                        candidate_count,
                        " | ".join(candidate_details),
                    )
                )
            if checked % 1000 == 0:
                print(f"[auditoria] Raws conferidos: {checked}", flush=True)

        self._raw_finding(
            "raw_paths_unsafe",
            "Caminhos raw inseguros",
            unsafe,
            ("coleta_id", "fonte", "caminho_relativo_raw", "status"),
            "raws_caminhos_invalidos.csv",
            "O caminho deve ser relativo à pasta data e não pode escapar dela.",
        )
        report_specs = {
            "exact_match": "raws_caminho_exato.csv",
            "relocated_match": "raws_realocados.csv",
            "ambiguous_candidates": "raws_candidatos_ambiguos.csv",
            "missing": "raws_ausentes.csv",
            "hash_mismatch": "raws_hash_divergentes.csv",
            "unreadable": "raws_ilegíveis.csv",
        }
        report_files: dict[str, str] = {}
        for classification, report_file in report_specs.items():
            if status_counts[classification] == 0:
                continue
            self._write_csv(
                report_file,
                sample_headers,
                samples[classification],
            )
            report_files[classification] = report_file

        finding_specs = {
            "ambiguous_candidates": (
                "raw_candidates_ambiguous",
                "Candidatos raw semelhantes sem confirmação por SHA-256",
                "Os caminhos semelhantes são apenas candidatos; nenhum possui "
                "o SHA-256 registrado.",
            ),
            "missing": (
                "raw_files_missing",
                "Arquivos raw canônicos ausentes",
                "O documento canônico não foi encontrado no caminho exato nem "
                "entre os candidatos arquivados.",
            ),
            "hash_mismatch": (
                "raw_hash_mismatch",
                "SHA-256 do raw no caminho exato difere do valor registrado",
                "A divergência só é confirmada quando o arquivo existe no "
                "caminho registrado.",
            ),
            "unreadable": (
                "raw_file_unreadable",
                "Arquivos raw encontrados, mas ilegíveis",
                "A rastreabilidade não pode ser concluída sem ler o conteúdo.",
            ),
        }
        for classification, (code, title, explanation) in finding_specs.items():
            occurrences = status_counts[classification]
            if occurrences == 0:
                continue
            self._add_finding(
                code,
                title,
                "erro",
                occurrences,
                occurrences,
                report_files[classification],
                explanation,
            )
        return {
            "status": "checked",
            "canonical_raws_checked": checked,
            "direct_files": direct_files,
            "archived_files": archived_files,
            "exact_matches": status_counts["exact_match"],
            "relocated_matches": status_counts["relocated_match"],
            "ambiguous_candidates": status_counts["ambiguous_candidates"],
            "missing_files": status_counts["missing"],
            "unsafe_paths": len(unsafe),
            "hash_mismatches": status_counts["hash_mismatch"],
            "unreadable_files": status_counts["unreadable"],
            "report_files": report_files,
        }

    @staticmethod
    def _raw_candidate_location(candidate: tuple[Path, str | None]) -> str:
        path, member = candidate
        return (
            path.as_posix()
            if member is None
            else f"{path.as_posix()}::{member}"
        )

    def _find_archived_raw(
        self, expected_path: Path, source_slug: str
    ) -> list[tuple[Path, str]]:
        old_directory = self.database_path.parent / "raw" / source_slug / "old"
        if old_directory not in self._zip_indexes:
            index: dict[str, list[tuple[Path, str]]] = {}
            if old_directory.is_dir():
                for archive_path in sorted(old_directory.glob("*.zip")):
                    try:
                        with zipfile.ZipFile(archive_path) as archive:
                            for member in archive.namelist():
                                if member.endswith("/"):
                                    continue
                                index.setdefault(Path(member).name, []).append(
                                    (archive_path, member)
                                )
                    except (OSError, zipfile.BadZipFile) as error:
                        self._add_finding(
                            "raw_archive_unreadable",
                            "ZIP de raws não pôde ser lido",
                            "erro",
                            1,
                            1,
                            None,
                            f"{archive_path}: {error}",
                        )
            self._zip_indexes[old_directory] = index
        return self._zip_indexes[old_directory].get(expected_path.name, [])

    def _audit_query(
        self,
        connection: sqlite3.Connection,
        code: str,
        title: str,
        severity: str,
        sql: str,
        explanation: str,
    ) -> None:
        cursor = connection.execute(sql)
        headers = tuple(item[0] for item in cursor.description or ())
        groups = 0
        occurrences = 0
        samples: list[sqlite3.Row] = []
        occurrence_index = headers.index("ocorrencias") if "ocorrencias" in headers else -1
        for row in cursor:
            groups += 1
            occurrences += int(row[occurrence_index]) if occurrence_index >= 0 else 1
            if len(samples) < self.sample_limit:
                samples.append(row)
        if groups == 0:
            return
        report_file = f"{code}.csv"
        self._write_csv(report_file, headers, samples)
        self._add_finding(
            code, title, severity, groups, occurrences, report_file, explanation
        )

    def _raw_finding(
        self,
        code: str,
        title: str,
        rows: list[tuple[object, ...]],
        headers: tuple[str, ...],
        report_file: str,
        explanation: str,
    ) -> None:
        if not rows:
            return
        self._write_csv(report_file, headers, rows[: self.sample_limit])
        self._add_finding(
            code, title, "erro", len(rows), len(rows), report_file, explanation
        )

    def _add_finding(
        self,
        code: str,
        title: str,
        severity: str,
        groups: int,
        occurrences: int,
        report_file: str | None,
        explanation: str,
    ) -> None:
        self.findings.append(
            V4Finding(
                code=code,
                title=title,
                severity=severity,
                groups=groups,
                occurrences=occurrences,
                report_file=report_file,
                explanation=explanation,
            )
        )

    def _build_result(
        self,
        started_at: datetime,
        finished_at: datetime,
        schema_summary: dict[str, object],
        structural: dict[str, object],
        table_counts: dict[str, int],
        source_summary: list[dict[str, object]],
        raw_summary: dict[str, object],
    ) -> dict[str, object]:
        errors = [item for item in self.findings if item.severity == "erro"]
        warnings = [item for item in self.findings if item.severity == "alerta"]
        if errors:
            status = "inconsistente"
            explanation = "Foram encontrados problemas que violam o contrato do SQLite v4."
        elif warnings:
            status = "consistente_com_alertas"
            explanation = "O modelo v4 está íntegro, com ocorrências que pedem revisão operacional."
        else:
            status = "consistente_nos_testes_executados"
            explanation = "Nenhuma violação foi encontrada nas verificações executadas."
        generated_files = sorted(
            {*self.generated_files, "execucao.json", "resultado.json", "resumo.md"}
        )
        return {
            "schema_version": 6,
            "database_model": "sqlite_v4_issue9",
            "execution_status": "completed",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": (finished_at - started_at).total_seconds(),
            "command": self.execution_context["request"]["command"],
            "execution_context": self.execution_context,
            "stages": self.stage_history,
            "database": {
                "path": self.database_path.as_posix(),
                "size_bytes": self.database_path.stat().st_size,
            },
            "parameters": {
                "sample_limit": self.sample_limit,
                "gap_days": self.gap_days,
                "verify_raw": self.verify_raw,
                "full_integrity_check": self.full_integrity_check,
                "semantic_analysis": False,
                "semantic_analysis_reason": (
                    "A issue 9 preserva cotações iguais entre documentos diferentes."
                ),
            },
            "verdict": {
                "status": status,
                "error_findings": len(errors),
                "warning_findings": len(warnings),
                "info_findings": 0,
                "error_occurrences": sum(item.occurrences for item in errors),
                "warning_occurrences": sum(item.occurrences for item in warnings),
                "info_occurrences": 0,
                "explanation": explanation,
            },
            "safety": {
                "audit_only": True,
                "database_opened_read_only": True,
                "automatic_deletion_allowed": False,
                "reason": "A auditoria documenta ocorrências e nunca autoriza exclusões.",
            },
            "schema": schema_summary,
            "structural": structural,
            "table_counts": table_counts,
            "source_summary": source_summary,
            "raw_traceability": raw_summary,
            "environment": self.environment,
            "artifacts": {"generated_files": generated_files},
            "findings": [asdict(item) for item in self.findings],
            "limitations": [
                "internal_consistency_does_not_prove_external_truth",
                "equal_quotes_across_distinct_documents_are_intentionally_preserved",
                "raw_hash_does_not_validate_parser_semantics",
                "calendar_gaps_may_be_expected",
                "no_report_authorizes_deletion",
            ],
        }

    def _write_markdown(self, result: dict[str, object]) -> None:
        verdict = result["verdict"]
        assert isinstance(verdict, dict)
        raw_summary = result["raw_traceability"]
        assert isinstance(raw_summary, dict)
        lines = [
            "# Auditoria do SQLite v4",
            "",
            f"- Status: **{verdict['status']}**",
            f"- Achados de erro: `{verdict['error_findings']}`",
            f"- Alertas: `{verdict['warning_findings']}`",
            f"- Duração: `{result['duration_seconds']:.2f} s`",
            f"- Banco: `{self.database_path}`",
            "- Exclusão automática permitida: **não**",
            "",
            "A auditoria respeita a issue 9: cotações iguais em documentos "
            "diferentes são preservadas; somente repetições na mesma coleta e "
            "documentos repetidos por fonte + SHA-256 são tratados como erro.",
            "",
            "## Rastreabilidade dos raws",
            "",
            f"- Verificados: `{raw_summary.get('canonical_raws_checked', 0)}`",
            f"- No caminho exato: `{raw_summary.get('exact_matches', 0)}`",
            f"- Realocados: `{raw_summary.get('relocated_matches', 0)}`",
            (
                "- Candidatos ambíguos: "
                f"`{raw_summary.get('ambiguous_candidates', 0)}`"
            ),
            f"- Ausentes: `{raw_summary.get('missing_files', 0)}`",
            (
                "- Divergências confirmadas no caminho exato: "
                f"`{raw_summary.get('hash_mismatches', 0)}`"
            ),
            f"- Ilegíveis: `{raw_summary.get('unreadable_files', 0)}`",
            f"- Caminhos inseguros: `{raw_summary.get('unsafe_paths', 0)}`",
            "",
            "## Achados",
            "",
        ]
        if self.findings:
            lines.extend(
                [
                    "| Severidade | Código | Grupos | Ocorrências | Relatório |",
                    "| --- | --- | ---: | ---: | --- |",
                ]
            )
            for item in self.findings:
                report = f"`{item.report_file}`" if item.report_file else "-"
                lines.append(
                    f"| {item.severity} | `{item.code}` | {item.groups} | "
                    f"{item.occurrences} | {report} |"
                )
        else:
            lines.append("Nenhum achado foi registrado.")
        lines.extend(["", "## Contagens", "", "| Tabela | Registros |", "| --- | ---: |"])
        table_counts = result["table_counts"]
        assert isinstance(table_counts, dict)
        for table, count in table_counts.items():
            lines.append(f"| `{table}` | {count} |")
        lines.extend(["", "## Arquivos gerados", ""])
        for file_name in result["artifacts"]["generated_files"]:  # type: ignore[index]
            lines.append(f"- `{file_name}`")
        self._write_text("resumo.md", "\n".join(lines).rstrip() + "\n")

    def _step(self, message: str) -> None:
        self.current_stage = message
        self.stage_history.append(
            {"occurred_at": self._now().isoformat(timespec="seconds"), "message": message}
        )
        print(f"[auditoria] {message}", flush=True)
        self._write_execution_state("running")

    def _write_execution_state(
        self,
        status: str,
        finished_at: datetime | None = None,
        error: BaseException | None = None,
    ) -> None:
        payload = {
            "schema_version": 3,
            "database_model": "sqlite_v4_issue9",
            "status": status,
            "started_at": (
                self.started_at.isoformat(timespec="seconds") if self.started_at else None
            ),
            "finished_at": finished_at.isoformat(timespec="seconds") if finished_at else None,
            "stage": self.current_stage,
            "command": self.execution_context["request"]["command"],
            "database": self.database_path.as_posix(),
            "output": self.output_directory.as_posix(),
            "safe_for_cleanup": False,
            "automatic_deletion_allowed": False,
            "generated_files": sorted(set(self.generated_files)),
            "execution_context": self.execution_context,
            "stages": self.stage_history,
            "environment": self.environment,
            "error": (
                {"type": type(error).__name__, "message": str(error)} if error else None
            ),
        }
        try:
            path = self.output_directory / "execucao.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            if "execucao.json" not in self.generated_files:
                self.generated_files.append("execucao.json")
        except OSError:
            if status == "completed":
                raise

    def _write_incomplete_summary(self, status: str, error: BaseException) -> None:
        content = (
            "# Auditoria v4 incompleta\n\n"
            f"- Status: **{status}**\n"
            f"- Etapa: {self.current_stage}\n"
            f"- Erro: `{type(error).__name__}: {error}`\n"
            "- Exclusão automática permitida: **não**\n"
        )
        try:
            self._write_text("resumo_incompleto.md", content)
        except OSError:
            pass

    def _write_json(self, file_name: str, payload: object) -> None:
        self._write_text(
            file_name, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def _write_csv(
        self,
        file_name: str,
        headers: Iterable[str],
        rows: Iterable[Iterable[object]],
    ) -> None:
        path = self.output_directory / file_name
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
            writer.writerows(rows)
        temporary.replace(path)
        if file_name not in self.generated_files:
            self.generated_files.append(file_name)

    def _write_text(self, file_name: str, content: str) -> None:
        path = self.output_directory / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(path, content)
        if file_name not in self.generated_files:
            self.generated_files.append(file_name)

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    def _build_environment(self) -> dict[str, object]:
        module_path = Path(__file__).resolve()
        return {
            "python_executable": os.path.realpath(os.sys.executable),
            "python_version": os.sys.version,
            "auditor_entrypoint_sha256": self._hash_file(self.auditor_path),
            "auditor_v4_sha256": self._hash_file(module_path),
            "database_initial_stat": self.initial_database_stat,
        }

    def _database_stat(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for label, path in (
            ("database", self.database_path),
            ("wal", Path(f"{self.database_path}-wal")),
            ("shm", Path(f"{self.database_path}-shm")),
        ):
            if not path.exists():
                result[label] = {"exists": False}
                continue
            stat = path.stat()
            result[label] = {
                "exists": True,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        return result

    def _audit_input_stability(self) -> None:
        final_stat = self._database_stat()
        self.environment["database_final_stat"] = final_stat
        if final_stat != self.initial_database_stat:
            self._add_finding(
                "input_changed_during_audit",
                "Banco alterado durante a auditoria",
                "erro",
                1,
                1,
                None,
                "O tamanho ou mtime do SQLite mudou durante a leitura.",
            )

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _hash_zip_member(archive_path: Path, member: str) -> str:
        digest = hashlib.sha256()
        with zipfile.ZipFile(archive_path) as archive, archive.open(member) as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _valid_date(value: object | None) -> int:
        if value is None:
            return 0
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError:
            return 0
        return int(parsed.isoformat() == str(value))

    @staticmethod
    def _valid_datetime(value: object | None) -> int:
        if value is None:
            return 0
        try:
            datetime.fromisoformat(str(value))
        except ValueError:
            return 0
        return 1

    @staticmethod
    def _valid_sha256(value: object | None) -> int:
        if not isinstance(value, str) or len(value) != 64:
            return 0
        return int(all(character in "0123456789abcdef" for character in value))

    @staticmethod
    def _valid_nonnegative_decimal(value: object | None) -> int:
        if value is None or str(value).strip() == "":
            return 0
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return 0
        return int(parsed.is_finite() and parsed >= 0)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(AUDIT_TIMEZONE)
