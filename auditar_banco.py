#!/usr/bin/env python3

import argparse
import csv
from collections import Counter
import hashlib
import importlib
import importlib.metadata
import json
import platform
import re
import sqlite3
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


REQUIRED_SCHEMA = {
    "ceasas": {"id", "slug", "nome"},
    "coletas": {
        "id",
        "chave_unica",
        "ceasa_id",
        "arquivo_raw",
        "hash_raw",
        "url_origem",
        "baixado_em",
        "processado_em",
    },
    "entrepostos": {"id", "ceasa_id", "slug", "nome"},
    "categorias": {"id", "slug"},
    "produtos": {"id", "nome_normalizado"},
    "produto_aliases": {"id", "produto_id", "nome_original"},
    "unidades": {"id", "sigla"},
    "apresentacoes_unidade": {
        "id",
        "chave_unica",
        "unidade_id",
        "unidade_original",
        "unidade_normalizada",
        "embalagem",
        "quantidade_minima",
        "quantidade_maxima",
        "detalhe_unidade",
    },
    "cotacoes": {
        "id",
        "chave_unica",
        "chave_identidade",
        "coleta_id",
        "entreposto_id",
        "categoria_id",
        "produto_alias_id",
        "apresentacao_unidade_id",
        "data_cotacao",
        "preco_minimo",
        "preco_comum",
        "preco_maximo",
        "procedencia",
        "classificacao",
        "situacao_mercado",
        "fonte_complemento",
        "url_complemento",
        "data_complemento",
    },
}

TABLES = (
    "estados",
    "ceasas",
    "entrepostos",
    "categorias",
    "produtos",
    "produto_aliases",
    "unidades",
    "apresentacoes_unidade",
    "coletas",
    "cotacoes",
    "backfill_states",
)

SEMANTIC_ROWS_SQL = """
    SELECT
        co.id,
        ce.slug AS fonte,
        audit_canon(ent.slug) AS entreposto,
        audit_canon(cat.slug) AS categoria,
        audit_canon(p.nome_normalizado) AS produto,
        audit_canon(COALESCE(u.sigla, au.unidade_normalizada, au.unidade_original))
            AS unidade,
        audit_canon(au.embalagem) AS embalagem,
        audit_decimal(au.quantidade_minima) AS quantidade_minima,
        audit_decimal(au.quantidade_maxima) AS quantidade_maxima,
        audit_canon(au.detalhe_unidade) AS detalhe_unidade,
        audit_canon(co.procedencia) AS procedencia,
        audit_canon(co.classificacao) AS classificacao,
        co.data_cotacao,
        audit_decimal(co.preco_minimo) AS preco_minimo,
        audit_decimal(co.preco_comum) AS preco_comum,
        audit_decimal(co.preco_maximo) AS preco_maximo,
        audit_canon(co.situacao_mercado) AS situacao_mercado,
        audit_content_signature(
            co.preco_minimo,
            co.preco_comum,
            co.preco_maximo,
            co.situacao_mercado
        ) AS assinatura_conteudo,
        co.chave_identidade,
        co.chave_unica
    FROM cotacoes co
    JOIN coletas col ON col.id = co.coleta_id
    JOIN ceasas ce ON ce.id = col.ceasa_id
    LEFT JOIN entrepostos ent ON ent.id = co.entreposto_id
    JOIN categorias cat ON cat.id = co.categoria_id
    JOIN produto_aliases pa ON pa.id = co.produto_alias_id
    JOIN produtos p ON p.id = pa.produto_id
    LEFT JOIN apresentacoes_unidade au
        ON au.id = co.apresentacao_unidade_id
    LEFT JOIN unidades u ON u.id = au.unidade_id
"""

SEMANTIC_IDENTITY_COLUMNS = (
    "fonte",
    "entreposto",
    "categoria",
    "produto",
    "unidade",
    "embalagem",
    "quantidade_minima",
    "quantidade_maxima",
    "detalhe_unidade",
    "procedencia",
    "classificacao",
    "data_cotacao",
)


@dataclass(frozen=True)
class Finding:
    code: str
    title: str
    severity: str
    groups: int
    occurrences: int
    report_file: str | None
    explanation: str


class DatabaseAuditor:
    def __init__(
        self,
        database_path: Path,
        output_directory: Path,
        sample_limit: int,
        gap_days: int,
        verify_raw: bool,
        full_integrity_check: bool,
        skip_semantic: bool,
    ) -> None:
        self.database_path = database_path.resolve()
        self.project_root = Path(__file__).resolve().parent
        self.output_directory = output_directory.resolve()
        self.sample_limit = sample_limit
        self.gap_days = gap_days
        self.verify_raw = verify_raw
        self.full_integrity_check = full_integrity_check
        self.skip_semantic = skip_semantic
        self.findings: list[Finding] = []
        self.generated_files: list[str] = []
        self.duplicate_summary: list[dict[str, object]] = []
        self.environment: dict[str, object] = {}
        self.source_config: dict[str, dict[str, object]] = {}
        self.raw_reconciliation_summary: dict[str, object] = {
            "status": "not_checked"
        }
        self.current_stage = "Inicializando"
        self.started_at: datetime | None = None
        self.initial_database_stat: dict[str, object] = {}
        self.raw_archive_errors: dict[str, str] = {}
        self.raw_file_index_cache: dict[
            Path, dict[str, list[Path]]
        ] = {}
        self.raw_zip_index_cache: dict[
            Path, dict[str, list[tuple[Path, str]]]
        ] = {}

    def run(self) -> dict[str, object]:
        self._validate_paths()
        self.output_directory.mkdir(parents=True)
        self.started_at = datetime.now().astimezone()
        self.initial_database_stat = self._database_stat()
        incomplete_marker = self.output_directory / "_INCOMPLETA"
        write_text_atomic(
            incomplete_marker,
            "Auditoria em andamento ou interrompida. Nao use estes arquivos "
            "para remover dados.\n",
        )
        self.generated_files.append("execucao.json")
        self._write_execution_state("running")

        try:
            self._step("Validando ambiente e dependencias")
            self.environment = self._preflight_environment()
            self._write_json("ambiente_auditoria.json", self.environment)
            self._step(f"Banco: {self.database_path}")
            self._step(f"Saida: {self.output_directory}")

            with self._connect() as connection:
                initial_data_version = int(
                    connection.execute("PRAGMA data_version").fetchone()[0]
                )
                self.environment["sqlite_data_version_initial"] = (
                    initial_data_version
                )
                self._step("Validando esquema e integridade estrutural")
                self._validate_schema(connection)
                structural = self._audit_structure(connection)
                table_counts = self._load_table_counts(connection)
                self._step("Resumindo cobertura por fonte")
                source_summary = self._audit_source_coverage(connection)
                self._step("Conferindo chaves derivadas")
                self._audit_keys(connection)
                self._step("Procurando repeticoes pela regra oficial")
                self._audit_stored_duplicates(connection)
                self._step("Resumindo repeticoes dos arquivos brutos")
                self._audit_repeated_raws(connection)

                if not self.skip_semantic:
                    self._step("Procurando candidatos semanticos entre identidades")
                    self._audit_semantic_duplicates(connection)
                    self._step("Procurando variacoes para a mesma identidade")
                    self._audit_semantic_variations(connection)

                self._step("Conferindo precos e datas")
                self._audit_domain_values(connection)
                self._step("Calculando lacunas temporais")
                self._audit_date_gaps(connection)
                if self.verify_raw:
                    self._step("Conferindo arquivos brutos e hashes SHA-256")
                raw_summary = (
                    self._audit_raw_files(connection)
                    if self.verify_raw
                    else {"status": "not_checked"}
                )
                if self.verify_raw:
                    self._step("Reconciliando duplicatas com o pipeline atual")
                    self.raw_reconciliation_summary = (
                        self._audit_duplicate_raw_content(connection)
                    )

                final_data_version = int(
                    connection.execute("PRAGMA data_version").fetchone()[0]
                )
                self.environment["sqlite_data_version_final"] = (
                    final_data_version
                )
                if final_data_version != initial_data_version:
                    self.findings.append(
                        Finding(
                            code="sqlite_changed_during_audit",
                            title="SQLite recebeu escrita durante a auditoria",
                            severity="erro",
                            groups=1,
                            occurrences=1,
                            report_file=None,
                            explanation=(
                                "PRAGMA data_version mudou durante a mesma "
                                "conexao de leitura. O snapshot consultado "
                                "permaneceu consistente, mas os artefatos nao "
                                "representam necessariamente o arquivo atual."
                            ),
                        )
                    )

            self._audit_input_stability()
            self._write_json("ambiente_auditoria.json", self.environment)
            finished_at = datetime.now().astimezone()
            result = self._build_result(
                started_at=self.started_at,
                finished_at=finished_at,
                structural=structural,
                table_counts=table_counts,
                source_summary=source_summary,
                raw_summary=raw_summary,
            )
            self._write_json("resultado.json", result)
            self._write_markdown("resumo.md", result)
            self._write_execution_state(
                "completed",
                finished_at=finished_at,
                strict=True,
            )
            incomplete_marker.unlink(missing_ok=True)
            return result
        except BaseException as error:
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            self._write_incomplete_summary(status, error)
            self._write_execution_state(
                status,
                finished_at=datetime.now().astimezone(),
                error=error,
            )
            raise

    def _step(self, message: str) -> None:
        self.current_stage = message
        print(f"[auditoria] {message}", flush=True)
        self._write_execution_state("running")

    def _preflight_environment(self) -> dict[str, object]:
        config_path = self.project_root / "config/fontes.json"
        self.source_config = json.loads(config_path.read_text(encoding="utf-8"))

        if self.verify_raw:
            source_path = self.project_root / "src"
            source_path_text = source_path.as_posix()
            if source_path_text not in sys.path:
                sys.path.insert(0, source_path_text)

            try:
                for module_name in ("bs4", "lxml", "pypdf"):
                    importlib.import_module(module_name)
                importlib.import_module("cotacoes_ceasa.sources.registry")
                importlib.import_module("cotacoes_ceasa.workflows.raw_processing")
            except ImportError as error:
                raise RuntimeError(
                    "Dependencia do pipeline ausente no Python "
                    f"{sys.executable}: {error}. Execute o auditor no ambiente "
                    "do projeto, por exemplo pelo servico Docker app."
                ) from error

        dependency_versions: dict[str, str | None] = {}
        for package_name in ("beautifulsoup4", "lxml", "pypdf"):
            try:
                dependency_versions[package_name] = importlib.metadata.version(
                    package_name
                )
            except importlib.metadata.PackageNotFoundError:
                dependency_versions[package_name] = None

        return {
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": platform.platform(),
            "dependencies": dependency_versions,
            "auditor_sha256": hash_file_bytes(Path(__file__).resolve()),
            "config_sha256": hash_file_bytes(config_path),
            "pipeline_sha256": self._pipeline_hash(),
            "pdf_cache_mode": "fresh_temporary",
            "database_initial_stat": self.initial_database_stat,
        }

    def _pipeline_hash(self) -> str:
        package_root = self.project_root / "src/cotacoes_ceasa"
        digest = hashlib.sha256()

        for path in sorted(package_root.rglob("*.py")):
            relative_path = path.relative_to(self.project_root).as_posix()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")

        return digest.hexdigest()

    def _database_stat(self) -> dict[str, object]:
        paths = {
            "database": self.database_path,
            "wal": Path(f"{self.database_path}-wal"),
            "shm": Path(f"{self.database_path}-shm"),
        }
        result: dict[str, object] = {}
        for label, path in paths.items():
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

        if final_stat == self.initial_database_stat:
            return

        self.findings.append(
            Finding(
                code="input_changed_during_audit",
                title="Banco alterado durante a auditoria",
                severity="erro",
                groups=1,
                occurrences=1,
                report_file=None,
                explanation=(
                    "O tamanho ou mtime do SQLite mudou durante a leitura. "
                    "O snapshot SQL permaneceu estavel, mas o resultado nao "
                    "deve ser usado para uma decisao destrutiva."
                ),
            )
        )

    def _write_execution_state(
        self,
        status: str,
        finished_at: datetime | None = None,
        error: BaseException | None = None,
        strict: bool = False,
    ) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "status": status,
            "started_at": (
                self.started_at.isoformat(timespec="seconds")
                if self.started_at is not None
                else None
            ),
            "finished_at": (
                finished_at.isoformat(timespec="seconds")
                if finished_at is not None
                else None
            ),
            "stage": self.current_stage,
            "command": sys.argv,
            "database": self.database_path.as_posix(),
            "output": self.output_directory.as_posix(),
            "safe_for_cleanup": False,
            "automatic_deletion_allowed": False,
            "generated_files": sorted(set(self.generated_files)),
            "environment": self.environment,
            "error": (
                {"type": type(error).__name__, "message": str(error)}
                if error is not None
                else None
            ),
        }

        try:
            write_text_atomic(
                self.output_directory / "execucao.json",
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
        except OSError:
            if strict:
                raise

    def _write_incomplete_summary(
        self,
        status: str,
        error: BaseException,
    ) -> None:
        content = "\n".join(
            (
                "# Auditoria incompleta",
                "",
                f"- Status: **{status}**",
                f"- Etapa: {self.current_stage}",
                f"- Erro: `{type(error).__name__}: {error}`",
                "- Exclusao automatica permitida: **nao**",
                "",
                (
                    "Os arquivos desta pasta podem ser parciais. Nao os use "
                    "para alterar ou remover dados."
                ),
                "",
            )
        )
        try:
            write_text_atomic(
                self.output_directory / "resumo_incompleto.md",
                content,
            )
            self.generated_files.append("resumo_incompleto.md")
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        database_uri = f"{self.database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.create_function(
            "audit_hash_values",
            -1,
            hash_values,
            deterministic=True,
        )
        connection.create_function(
            "audit_decimal",
            1,
            decimal_key,
            deterministic=True,
        )
        connection.create_function(
            "audit_canon",
            1,
            canonical_text,
            deterministic=True,
        )
        connection.create_function(
            "audit_basic_name",
            1,
            basic_name,
            deterministic=True,
        )
        connection.create_function(
            "audit_content_signature",
            4,
            content_signature,
            deterministic=True,
        )
        connection.create_function(
            "audit_valid_iso_date",
            1,
            is_valid_iso_date,
            deterministic=True,
        )
        connection.create_function(
            "audit_valid_sha256",
            1,
            is_valid_sha256,
            deterministic=True,
        )
        connection.create_function(
            "audit_structured_hash",
            -1,
            structured_hash,
            deterministic=True,
        )
        connection.execute("BEGIN")
        return connection

    def _validate_paths(self) -> None:
        if not self.database_path.is_file():
            raise FileNotFoundError(
                f"Banco SQLite nao encontrado: {self.database_path}"
            )

        if self.output_directory.exists():
            raise FileExistsError(
                "O diretorio de saida ja existe. Escolha outro caminho: "
                f"{self.output_directory}"
            )

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        missing_tables = sorted(set(REQUIRED_SCHEMA) - existing_tables)

        if missing_tables:
            raise RuntimeError(
                "Tabelas obrigatorias ausentes: " + ", ".join(missing_tables)
            )

        missing_columns: list[str] = []

        for table_name, required_columns in REQUIRED_SCHEMA.items():
            existing_columns = {
                str(row["name"])
                for row in connection.execute(
                    f'PRAGMA table_info("{table_name}")'
                )
            }
            for column_name in sorted(required_columns - existing_columns):
                missing_columns.append(f"{table_name}.{column_name}")

        if missing_columns:
            raise RuntimeError(
                "Colunas obrigatorias ausentes: " + ", ".join(missing_columns)
            )

    def _audit_structure(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, object]:
        pragma = "integrity_check" if self.full_integrity_check else "quick_check"
        integrity_rows = [
            str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")
        ]
        integrity_ok = integrity_rows == ["ok"]

        if not integrity_ok:
            report = self._write_rows(
                "integridade_sqlite.csv",
                ("mensagem",),
                ((message,) for message in integrity_rows),
            )
            self.findings.append(
                Finding(
                    code="sqlite_integrity",
                    title="Falhas na integridade interna do SQLite",
                    severity="erro",
                    groups=len(integrity_rows),
                    occurrences=len(integrity_rows),
                    report_file=report,
                    explanation=(
                        "O SQLite encontrou problemas em paginas, indices ou "
                        "estruturas internas do arquivo."
                    ),
                )
            )

        foreign_key_rows = list(connection.execute("PRAGMA foreign_key_check"))
        foreign_key_report = None

        if foreign_key_rows:
            foreign_key_report = self._write_rows(
                "violacoes_chaves_estrangeiras.csv",
                ("tabela", "rowid", "tabela_referenciada", "indice_fk"),
                (
                    tuple(row)
                    for row in foreign_key_rows[: self.sample_limit]
                ),
            )
            self.findings.append(
                Finding(
                    code="foreign_key_violations",
                    title="Referencias quebradas entre tabelas",
                    severity="erro",
                    groups=len(foreign_key_rows),
                    occurrences=len(foreign_key_rows),
                    report_file=foreign_key_report,
                    explanation=(
                        "Existem registros apontando para entidades que nao "
                        "estao presentes nas tabelas relacionadas."
                    ),
                )
            )

        return {
            "check": pragma,
            "status": "ok" if integrity_ok else "failed",
            "messages": integrity_rows,
            "foreign_key_violations": len(foreign_key_rows),
        }

    def _load_table_counts(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, int]:
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }

        return {
            table_name: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table_name}"'
                ).fetchone()[0]
            )
            for table_name in TABLES
            if table_name in existing_tables
        }

    def _audit_source_coverage(
        self,
        connection: sqlite3.Connection,
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """
            SELECT
                ce.slug AS fonte,
                COUNT(DISTINCT col.id) AS coletas,
                COUNT(co.id) AS cotacoes,
                COUNT(DISTINCT co.chave_identidade) AS identidades,
                COUNT(DISTINCT co.data_cotacao) AS datas_com_cotacao,
                MIN(co.data_cotacao) AS primeira_data,
                MAX(co.data_cotacao) AS ultima_data,
                COALESCE(SUM(
                    CASE
                        WHEN co.id IS NOT NULL
                         AND co.preco_minimo IS NULL
                         AND co.preco_comum IS NULL
                         AND co.preco_maximo IS NULL
                        THEN 1 ELSE 0
                    END
                ), 0) AS cotacoes_sem_preco,
                COUNT(DISTINCT CASE WHEN co.id IS NULL THEN col.id END)
                    AS coletas_sem_cotacao,
                COUNT(DISTINCT CASE WHEN col.arquivo_raw IS NULL THEN col.id END)
                    AS coletas_sem_bruto_local,
                COALESCE(SUM(
                    CASE
                        WHEN co.id IS NOT NULL AND col.arquivo_raw IS NULL
                        THEN 1 ELSE 0
                    END
                ), 0) AS cotacoes_sem_bruto_local
            FROM ceasas ce
            LEFT JOIN coletas col ON col.ceasa_id = ce.id
            LEFT JOIN cotacoes co ON co.coleta_id = col.id
            GROUP BY ce.id, ce.slug
            ORDER BY ce.slug
            """
        ).fetchall()
        payload = [dict(row) for row in rows]
        self._write_rows(
            "resumo_por_fonte.csv",
            tuple(payload[0]) if payload else (
                "fonte",
                "coletas",
                "cotacoes",
                "identidades",
                "datas_com_cotacao",
                "primeira_data",
                "ultima_data",
                "cotacoes_sem_preco",
                "coletas_sem_cotacao",
                "coletas_sem_bruto_local",
                "cotacoes_sem_bruto_local",
            ),
            (tuple(row.values()) for row in payload),
        )

        present_sources = {str(row["fonte"]) for row in rows}
        missing_sources = [
            (
                source,
                str(config.get("publication_policy", "unknown")),
                str(config.get("name", "")),
            )
            for source, config in sorted(self.source_config.items())
            if source not in present_sources
        ]
        if missing_sources:
            report = self._write_rows(
                "fontes_configuradas_ausentes.csv",
                ("fonte", "politica_publicacao", "nome"),
                missing_sources,
            )
            required_missing = sum(
                policy == "required" for _, policy, _ in missing_sources
            )
            self.findings.append(
                Finding(
                    code="configured_sources_missing",
                    title="Fontes configuradas sem dados no banco",
                    severity="alerta" if required_missing else "info",
                    groups=len(missing_sources),
                    occurrences=len(missing_sources),
                    report_file=report,
                    explanation=(
                        "A cobertura foi comparada com config/fontes.json. "
                        "A ausencia pode ser esperada para fonte opcional, mas "
                        "fica explicita no resultado."
                    ),
                )
            )

        return payload

    def _audit_stored_duplicates(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        groups_sql = """
            WITH grupos AS (
                SELECT
                    ce.slug AS fonte,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo) AS preco_minimo,
                    audit_decimal(co.preco_comum) AS preco_comum,
                    audit_decimal(co.preco_maximo) AS preco_maximo,
                    co.situacao_mercado,
                    COUNT(*) AS quantidade
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                GROUP BY
                    ce.slug,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo),
                    audit_decimal(co.preco_comum),
                    audit_decimal(co.preco_maximo),
                    co.situacao_mercado
                HAVING COUNT(*) > 1
            )
            SELECT
                COUNT(*) AS grupos,
                COALESCE(SUM(quantidade - 1), 0) AS repeticoes
            FROM grupos
        """
        detail_sql = """
            SELECT
                ce.slug AS fonte,
                co.data_cotacao,
                p.nome_normalizado AS produto,
                co.chave_identidade,
                audit_decimal(co.preco_minimo) AS preco_minimo,
                audit_decimal(co.preco_comum) AS preco_comum,
                audit_decimal(co.preco_maximo) AS preco_maximo,
                co.situacao_mercado,
                COUNT(*) AS quantidade,
                COUNT(DISTINCT co.coleta_id) AS coletas,
                COUNT(DISTINCT col.hash_raw) AS hashes_raw,
                CASE
                    WHEN COUNT(DISTINCT co.coleta_id) = 1
                    THEN 'mesma_coleta'
                    WHEN COUNT(DISTINCT col.hash_raw) = 1
                     AND SUM(col.hash_raw IS NULL) = 0
                    THEN 'coletas_diferentes_mesmo_bruto'
                    ELSE 'coletas_diferentes_brutos_diferentes'
                END AS origem_repeticao,
                MIN(col.processado_em) AS primeira_coleta,
                MAX(col.processado_em) AS ultima_coleta,
                GROUP_CONCAT(co.id) AS ids,
                GROUP_CONCAT(DISTINCT co.coleta_id) AS coleta_ids,
                GROUP_CONCAT(DISTINCT col.arquivo_raw) AS arquivos_raw
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            GROUP BY
                ce.slug,
                co.chave_identidade,
                audit_decimal(co.preco_minimo),
                audit_decimal(co.preco_comum),
                audit_decimal(co.preco_maximo),
                co.situacao_mercado
            HAVING COUNT(*) > 1
            ORDER BY quantidade DESC, fonte, co.data_cotacao
        """
        self._add_group_finding(
            connection=connection,
            code="stored_content_duplicates",
            title="Conteudos repetidos pela chave oficial",
            severity="erro",
            count_sql=groups_sql,
            detail_sql=detail_sql,
            report_name="amostra_grupos_duplicatas_exatas.csv",
            explanation=(
                "A mesma identidade, precos e situacao de mercado aparecem "
                "mais de uma vez. Sao repeticoes segundo a regra atual do sistema. "
                "O manifesto integral por linha e a referencia segura para auditoria."
            ),
        )
        summary_sql = """
            WITH grupos AS (
                SELECT
                    ce.slug AS fonte,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo) AS preco_minimo,
                    audit_decimal(co.preco_comum) AS preco_comum,
                    audit_decimal(co.preco_maximo) AS preco_maximo,
                    co.situacao_mercado,
                    COUNT(*) AS quantidade,
                    COUNT(DISTINCT co.coleta_id) AS coletas,
                    COUNT(DISTINCT col.hash_raw) AS hashes_raw,
                    SUM(col.hash_raw IS NULL) AS hashes_ausentes,
                    MAX(col.processado_em) AS ultima_repeticao
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                GROUP BY
                    ce.slug,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo),
                    audit_decimal(co.preco_comum),
                    audit_decimal(co.preco_maximo),
                    co.situacao_mercado
                HAVING COUNT(*) > 1
            )
            SELECT
                fonte,
                COUNT(*) AS grupos,
                SUM(quantidade) AS linhas_envolvidas,
                SUM(quantidade - 1) AS registros_excedentes,
                SUM(CASE WHEN coletas = 1 THEN 1 ELSE 0 END)
                    AS grupos_mesma_coleta,
                SUM(CASE WHEN coletas > 1 THEN 1 ELSE 0 END)
                    AS grupos_varias_coletas,
                SUM(
                    CASE
                        WHEN hashes_raw = 1 AND hashes_ausentes = 0
                        THEN 1 ELSE 0
                    END
                ) AS grupos_mesmo_bruto,
                SUM(
                    CASE
                        WHEN hashes_raw != 1 OR hashes_ausentes > 0
                        THEN 1 ELSE 0
                    END
                ) AS grupos_brutos_diferentes,
                MAX(quantidade) AS maior_grupo,
                MAX(ultima_repeticao) AS ultima_repeticao
            FROM grupos
            GROUP BY fonte
            ORDER BY registros_excedentes DESC, fonte
        """
        summary_rows = connection.execute(summary_sql).fetchall()
        self.duplicate_summary = [dict(row) for row in summary_rows]
        self._write_rows(
            "resumo_duplicatas_por_fonte.csv",
            tuple(self.duplicate_summary[0])
            if self.duplicate_summary
            else (
                "fonte",
                "grupos",
                "linhas_envolvidas",
                "registros_excedentes",
                "grupos_mesma_coleta",
                "grupos_varias_coletas",
                "grupos_mesmo_bruto",
                "grupos_brutos_diferentes",
                "maior_grupo",
                "ultima_repeticao",
            ),
            (tuple(row.values()) for row in self.duplicate_summary),
        )

        manifest_sql = """
            WITH base AS (
                SELECT
                    co.id AS cotacao_id,
                    ce.id AS ceasa_id,
                    ce.slug AS fonte,
                    co.chave_unica AS chave_conteudo_armazenada,
                    co.chave_identidade,
                    co.coleta_id,
                    ent.slug AS entreposto,
                    ent.nome AS entreposto_nome,
                    cat.slug AS categoria,
                    p.nome_normalizado AS produto,
                    pa.id AS produto_alias_id,
                    pa.nome_original AS produto_original,
                    u.sigla AS unidade,
                    au.unidade_original,
                    au.unidade_normalizada,
                    au.embalagem,
                    audit_decimal(au.quantidade_minima) AS quantidade_minima,
                    audit_decimal(au.quantidade_maxima) AS quantidade_maxima,
                    au.detalhe_unidade,
                    co.data_cotacao,
                    co.procedencia,
                    co.classificacao,
                    audit_decimal(co.preco_minimo) AS preco_minimo,
                    audit_decimal(co.preco_comum) AS preco_comum,
                    audit_decimal(co.preco_maximo) AS preco_maximo,
                    co.situacao_mercado,
                    co.fonte_complemento,
                    co.url_complemento,
                    co.data_complemento,
                    col.chave_unica AS chave_coleta,
                    col.arquivo_raw,
                    col.hash_raw,
                    col.url_origem,
                    col.baixado_em,
                    col.processado_em,
                    audit_hash_values(
                        co.chave_identidade,
                        audit_decimal(co.preco_minimo),
                        audit_decimal(co.preco_comum),
                        audit_decimal(co.preco_maximo),
                        co.situacao_mercado
                    ) AS chave_conteudo_atual,
                    co.chave_identidade = audit_hash_values(
                        ce.slug,
                        ent.slug,
                        cat.slug,
                        p.nome_normalizado,
                        au.chave_unica,
                        co.procedencia,
                        co.classificacao,
                        co.data_cotacao
                    ) AS chave_identidade_valida,
                    (
                        co.entreposto_id IS NULL OR (
                            ent.id IS NOT NULL
                            AND ent.ceasa_id IS col.ceasa_id
                        )
                    ) AS ownership_entreposto_valido,
                    audit_structured_hash(
                        ce.slug,
                        ent.slug,
                        cat.slug,
                        p.nome_normalizado,
                        au.chave_unica,
                        co.procedencia,
                        co.classificacao,
                        co.data_cotacao
                    ) AS assinatura_identidade_estruturada,
                    (
                        instr(COALESCE(ce.slug, ''), '|') > 0 OR
                        instr(COALESCE(ent.slug, ''), '|') > 0 OR
                        instr(COALESCE(cat.slug, ''), '|') > 0 OR
                        instr(COALESCE(p.nome_normalizado, ''), '|') > 0 OR
                        instr(COALESCE(au.chave_unica, ''), '|') > 0 OR
                        instr(COALESCE(co.procedencia, ''), '|') > 0 OR
                        instr(COALESCE(co.classificacao, ''), '|') > 0 OR
                        instr(COALESCE(co.situacao_mercado, ''), '|') > 0
                    ) AS componente_chave_ambiguo,
                    audit_structured_hash(
                        co.fonte_complemento,
                        co.url_complemento,
                        co.data_complemento
                    ) AS assinatura_complemento
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                LEFT JOIN entrepostos ent ON ent.id = co.entreposto_id
                JOIN categorias cat ON cat.id = co.categoria_id
                JOIN produto_aliases pa ON pa.id = co.produto_alias_id
                JOIN produtos p ON p.id = pa.produto_id
                LEFT JOIN apresentacoes_unidade au
                    ON au.id = co.apresentacao_unidade_id
                LEFT JOIN unidades u ON u.id = au.unidade_id
            ),
            grupos AS (
                SELECT
                    ceasa_id,
                    fonte,
                    chave_identidade,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    COUNT(*) AS quantidade,
                    MIN(cotacao_id) AS cotacao_canonica_id,
                    COUNT(DISTINCT coleta_id) AS coletas,
                    COUNT(DISTINCT hash_raw) AS hashes_raw,
                    SUM(hash_raw IS NULL) AS hashes_ausentes,
                    COUNT(DISTINCT assinatura_complemento)
                        AS variantes_complemento,
                    COUNT(DISTINCT assinatura_identidade_estruturada)
                        AS identidades_materiais,
                    COUNT(DISTINCT produto_alias_id) AS aliases_produto,
                    SUM(chave_identidade_valida = 0)
                        AS chaves_identidade_invalidas,
                    SUM(ownership_entreposto_valido = 0)
                        AS ownerships_invalidos,
                    SUM(componente_chave_ambiguo = 1)
                        AS componentes_ambiguos
                FROM base
                GROUP BY
                    ceasa_id,
                    fonte,
                    chave_identidade,
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado
                HAVING COUNT(*) > 1
            )
            SELECT
                audit_structured_hash(
                    g.fonte,
                    g.chave_identidade,
                    g.preco_minimo,
                    g.preco_comum,
                    g.preco_maximo,
                    g.situacao_mercado
                ) AS grupo_id,
                g.quantidade,
                g.cotacao_canonica_id AS menor_cotacao_id_referencia,
                b.cotacao_id,
                CASE
                    WHEN b.cotacao_id = g.cotacao_canonica_id
                    THEN 'menor_id_referencia_auditoria'
                    ELSE 'outra_ocorrencia_logica'
                END AS papel_auditoria,
                0 AS exclusao_automatica_permitida,
                CASE
                    WHEN g.identidades_materiais > 1
                    THEN 'componentes_identidade_divergentes'
                    WHEN g.chaves_identidade_invalidas > 0
                    THEN 'chave_identidade_invalida'
                    WHEN g.ownerships_invalidos > 0
                    THEN 'ownership_entreposto_invalido'
                    WHEN g.componentes_ambiguos > 0
                    THEN 'serializacao_de_chave_ambigua'
                    WHEN g.variantes_complemento > 1
                    THEN 'metadados_complementares_divergentes'
                    WHEN g.coletas > 1
                    THEN 'preservar_proveniencia_de_todas_as_coletas'
                    ELSE 'revisao_obrigatoria'
                END AS bloqueio_limpeza,
                g.coletas,
                g.hashes_raw,
                g.hashes_ausentes,
                g.variantes_complemento,
                g.identidades_materiais,
                g.aliases_produto,
                b.chave_identidade_valida,
                b.ownership_entreposto_valido,
                b.componente_chave_ambiguo,
                CASE
                    WHEN b.chave_conteudo_armazenada = b.chave_conteudo_atual
                    THEN 'atual'
                    ELSE 'nao_atual'
                END AS formato_chave_conteudo,
                b.fonte,
                b.entreposto,
                b.entreposto_nome,
                b.categoria,
                b.produto,
                b.produto_alias_id,
                b.produto_original,
                b.unidade,
                b.unidade_original,
                b.unidade_normalizada,
                b.embalagem,
                b.quantidade_minima,
                b.quantidade_maxima,
                b.detalhe_unidade,
                b.data_cotacao,
                b.procedencia,
                b.classificacao,
                b.preco_minimo,
                b.preco_comum,
                b.preco_maximo,
                b.situacao_mercado,
                b.fonte_complemento,
                b.url_complemento,
                b.data_complemento,
                b.chave_conteudo_armazenada,
                b.chave_conteudo_atual,
                b.coleta_id,
                b.chave_coleta,
                b.arquivo_raw,
                b.hash_raw,
                b.url_origem,
                b.baixado_em,
                b.processado_em
            FROM base b
            JOIN grupos g
              ON g.ceasa_id = b.ceasa_id
             AND g.chave_identidade = b.chave_identidade
             AND g.preco_minimo IS b.preco_minimo
             AND g.preco_comum IS b.preco_comum
             AND g.preco_maximo IS b.preco_maximo
             AND g.situacao_mercado IS b.situacao_mercado
            ORDER BY g.fonte, g.cotacao_canonica_id, b.cotacao_id
        """
        manifest_report = self._export_query_all(
            connection,
            "manifesto_integral_duplicatas_exatas.csv",
            manifest_sql,
        )

        for index, finding in enumerate(self.findings):
            if finding.code != "stored_content_duplicates":
                continue
            self.findings[index] = Finding(
                code=finding.code,
                title=finding.title,
                severity=finding.severity,
                groups=finding.groups,
                occurrences=finding.occurrences,
                report_file=manifest_report,
                explanation=(
                    finding.explanation
                    + " O manifesto integral contem todas as linhas; o arquivo "
                    "amostra_grupos_duplicatas_exatas.csv e apenas amostral."
                ),
            )
            break

        complement_conflict_row = connection.execute(
            """
            WITH grupos AS (
                SELECT
                    col.ceasa_id,
                    ce.slug AS fonte,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo) AS preco_minimo,
                    audit_decimal(co.preco_comum) AS preco_comum,
                    audit_decimal(co.preco_maximo) AS preco_maximo,
                    co.situacao_mercado,
                    COUNT(*) AS quantidade,
                    COUNT(DISTINCT audit_structured_hash(
                        co.fonte_complemento,
                        co.url_complemento,
                        co.data_complemento
                    )) AS variantes
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                GROUP BY
                    col.ceasa_id,
                    ce.slug,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo),
                    audit_decimal(co.preco_comum),
                    audit_decimal(co.preco_maximo),
                    co.situacao_mercado
                HAVING COUNT(*) > 1 AND variantes > 1
            )
            SELECT COUNT(*), COALESCE(SUM(quantidade), 0)
            FROM grupos
            """
        ).fetchone()
        complement_conflict_groups = int(complement_conflict_row[0])
        if complement_conflict_groups:
            self.findings.append(
                Finding(
                    code="duplicate_complement_conflicts",
                    title="Duplicatas exatas com complementos divergentes",
                    severity="alerta",
                    groups=complement_conflict_groups,
                    occurrences=int(complement_conflict_row[1]),
                    report_file=manifest_report,
                    explanation=(
                        "As linhas possuem o mesmo conteudo comercial, mas diferem "
                        "em fonte, URL ou data de complemento. Nenhuma delas pode "
                        "ser descartada sem uma regra explicita de mesclagem."
                    ),
                )
            )

    def _audit_repeated_raws(self, connection: sqlite3.Connection) -> None:
        groups_sql = """
            WITH grupos AS (
                SELECT
                    ce.slug AS fonte,
                    col.hash_raw,
                    COUNT(*) AS quantidade
                FROM coletas col
                JOIN ceasas ce ON ce.id = col.ceasa_id
                WHERE col.hash_raw IS NOT NULL
                GROUP BY ce.slug, col.hash_raw
                HAVING COUNT(*) > 1
            )
            SELECT
                COUNT(*) AS grupos,
                COALESCE(SUM(quantidade - 1), 0) AS coletas_repetidas
            FROM grupos
        """
        detail_sql = """
            SELECT
                ce.slug AS fonte,
                col.hash_raw,
                COUNT(*) AS quantidade,
                COUNT(DISTINCT col.arquivo_raw) AS caminhos,
                MIN(col.baixado_em) AS primeiro_download,
                MAX(col.baixado_em) AS ultimo_download,
                GROUP_CONCAT(col.id) AS coleta_ids,
                GROUP_CONCAT(DISTINCT col.arquivo_raw) AS arquivos_raw
            FROM coletas col
            JOIN ceasas ce ON ce.id = col.ceasa_id
            WHERE col.hash_raw IS NOT NULL
            GROUP BY ce.slug, col.hash_raw
            HAVING COUNT(*) > 1
            ORDER BY quantidade DESC, fonte, primeiro_download
        """
        self._add_group_finding(
            connection=connection,
            code="repeated_raw_content",
            title="Coletas com conteudo bruto identico",
            severity="info",
            count_sql=groups_sql,
            detail_sql=detail_sql,
            report_name="coletas_bruto_repetido.csv",
            explanation=(
                "O mesmo conteudo bruto foi registrado em coletas diferentes. "
                "Isso demonstra repeticao da coleta, mas so representa duplicacao "
                "no banco quando as cotacoes logicas tambem se repetem."
            ),
        )

    def _audit_semantic_duplicates(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        identity_columns = ", ".join(SEMANTIC_IDENTITY_COLUMNS)
        logical_rows_sql = f"""
            WITH linhas AS ({SEMANTIC_ROWS_SQL}),
            identidades AS (
                SELECT
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    assinatura_conteudo,
                    chave_identidade,
                    MIN(id) AS id_exemplo,
                    COUNT(*) AS repeticoes_exatas
                FROM linhas
                GROUP BY
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    assinatura_conteudo,
                    chave_identidade
            )
        """
        groups_sql = logical_rows_sql + f"""
            , grupos AS (
                SELECT
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    assinatura_conteudo,
                    COUNT(*) AS identidades_distintas
                FROM identidades
                GROUP BY
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    assinatura_conteudo
                HAVING COUNT(*) > 1
            )
            SELECT
                COUNT(*) AS grupos,
                COALESCE(SUM(identidades_distintas - 1), 0) AS candidatos
            FROM grupos
        """
        detail_sql = logical_rows_sql + f"""
            SELECT
                audit_structured_hash(
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado
                ) AS grupo_semantico_id,
                {identity_columns},
                preco_minimo,
                preco_comum,
                preco_maximo,
                situacao_mercado,
                COUNT(*) AS identidades_distintas,
                SUM(repeticoes_exatas) AS linhas_originais,
                GROUP_CONCAT(chave_identidade) AS chaves_identidade,
                GROUP_CONCAT(id_exemplo) AS ids_exemplo
            FROM identidades
            GROUP BY
                {identity_columns},
                preco_minimo,
                preco_comum,
                preco_maximo,
                situacao_mercado,
                assinatura_conteudo
            HAVING COUNT(*) > 1
            ORDER BY identidades_distintas DESC, fonte, data_cotacao, produto
        """
        self._add_group_finding(
            connection=connection,
            code="semantic_duplicate_candidates",
            title="Candidatos semanticos entre identidades distintas",
            severity="alerta",
            count_sql=groups_sql,
            detail_sql=detail_sql,
            report_name="candidatos_duplicatas_semanticas.csv",
            explanation=(
                "A normalizacao conservadora aproximou conteudos pertencentes a "
                "chaves de identidade oficiais diferentes. Sao apenas candidatos: "
                "acentos, caixa, vazio e representacao de unidade podem distinguir "
                "dados validos. Nenhuma exclusao automatica e permitida."
            ),
            limit_report=False,
        )

    def _audit_semantic_variations(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        identity_columns = ", ".join(SEMANTIC_IDENTITY_COLUMNS)
        logical_rows_sql = f"""
            WITH linhas AS ({SEMANTIC_ROWS_SQL}),
            conteudos AS (
                SELECT
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    assinatura_conteudo,
                    MIN(id) AS id_exemplo,
                    COUNT(*) AS linhas_originais
                FROM linhas
                GROUP BY
                    {identity_columns},
                    preco_minimo,
                    preco_comum,
                    preco_maximo,
                    situacao_mercado,
                    assinatura_conteudo
            )
        """
        groups_sql = logical_rows_sql + f"""
            , grupos AS (
                SELECT
                    {identity_columns},
                    COUNT(*) AS versoes,
                    SUM(linhas_originais) AS observacoes
                FROM conteudos
                GROUP BY {identity_columns}
                HAVING COUNT(*) > 1
            )
            SELECT
                COUNT(*) AS grupos,
                COALESCE(SUM(versoes), 0) AS versoes
            FROM grupos
        """
        detail_sql = logical_rows_sql + f"""
            SELECT
                {identity_columns},
                COUNT(*) AS versoes,
                SUM(linhas_originais) AS observacoes_originais,
                GROUP_CONCAT(
                    COALESCE(preco_minimo, '-') || '/' ||
                    COALESCE(preco_comum, '-') || '/' ||
                    COALESCE(preco_maximo, '-') || '/' ||
                    COALESCE(situacao_mercado, '-')
                ) AS valores,
                GROUP_CONCAT(id_exemplo) AS ids_exemplo
            FROM conteudos
            GROUP BY {identity_columns}
            HAVING COUNT(*) > 1
            ORDER BY versoes DESC, observacoes_originais DESC, fonte, data_cotacao
        """
        self._add_group_finding(
            connection=connection,
            code="semantic_variations",
            title="Mesma identidade semantica com conteudos diferentes",
            severity="alerta",
            count_sql=groups_sql,
            detail_sql=detail_sql,
            report_name="variacoes_mesma_identidade.csv",
            explanation=(
                "As repeticoes exatas foram colapsadas antes desta contagem. "
                "As versoes restantes podem ser correcoes reais, variedades que "
                "o parser nao distinguiu ou enriquecimentos. Exigem revisao do "
                "bruto e nunca sao removiveis automaticamente."
            ),
            limit_report=False,
        )

    def _audit_content_key_formats(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            WITH formatos AS MATERIALIZED (
                SELECT
                    ce.slug AS fonte,
                    co.id,
                    co.chave_unica = audit_hash_values(
                        co.chave_identidade,
                        audit_decimal(co.preco_minimo),
                        audit_decimal(co.preco_comum),
                        audit_decimal(co.preco_maximo),
                        co.situacao_mercado
                    ) AS formato_atual
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
            )
            SELECT
                fonte,
                COUNT(*) AS total,
                SUM(formato_atual) AS formato_atual,
                SUM(NOT formato_atual) AS formato_nao_atual,
                MIN(CASE WHEN formato_atual THEN id END) AS primeiro_id_atual,
                MAX(CASE WHEN NOT formato_atual THEN id END)
                    AS ultimo_id_nao_atual
            FROM formatos
            GROUP BY fonte
            ORDER BY fonte
            """
        ).fetchall()
        payload = [dict(row) for row in rows]
        report = self._write_rows(
            "chaves_conteudo_por_formato.csv",
            tuple(payload[0]) if payload else (
                "fonte",
                "total",
                "formato_atual",
                "formato_nao_atual",
                "primeiro_id_atual",
                "ultimo_id_nao_atual",
            ),
            (tuple(row.values()) for row in payload),
        )
        noncurrent_count = sum(
            int(row["formato_nao_atual"] or 0) for row in rows
        )

        if noncurrent_count:
            self.findings.append(
                Finding(
                    code="noncurrent_content_keys",
                    title="Cotacoes com chave de conteudo nao atual",
                    severity="info",
                    groups=sum(
                        bool(row["formato_nao_atual"]) for row in rows
                    ),
                    occurrences=noncurrent_count,
                    report_file=report,
                    explanation=(
                        "A chave armazenada difere da formula atual. Isso pode "
                        "refletir formulas historicas, mas esta auditoria nao "
                        "prova quando nem por qual versao ela foi criada. Nao e "
                        "tratado como corrupcao: as duplicatas sao verificadas "
                        "diretamente pelos campos logicos persistidos."
                    ),
                )
            )

    def _audit_key_serialization(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        cursor = connection.execute(
            """
            WITH componentes(tabela, registro_id, campo, valor) AS (
                SELECT 'ceasas', id, 'slug', slug FROM ceasas
                UNION ALL
                SELECT 'entrepostos', id, 'slug', slug FROM entrepostos
                UNION ALL
                SELECT 'categorias', id, 'slug', slug FROM categorias
                UNION ALL
                SELECT 'produtos', id, 'nome_normalizado', nome_normalizado
                FROM produtos
                UNION ALL
                SELECT 'unidades', id, 'sigla', sigla FROM unidades
                UNION ALL
                SELECT 'apresentacoes_unidade', id, 'unidade_original',
                       unidade_original FROM apresentacoes_unidade
                UNION ALL
                SELECT 'apresentacoes_unidade', id, 'unidade_normalizada',
                       unidade_normalizada FROM apresentacoes_unidade
                UNION ALL
                SELECT 'apresentacoes_unidade', id, 'embalagem', embalagem
                FROM apresentacoes_unidade
                UNION ALL
                SELECT 'apresentacoes_unidade', id, 'detalhe_unidade',
                       detalhe_unidade FROM apresentacoes_unidade
                UNION ALL
                SELECT 'coletas', id, 'arquivo_raw', arquivo_raw FROM coletas
                UNION ALL
                SELECT 'coletas', id, 'hash_raw', hash_raw FROM coletas
                UNION ALL
                SELECT 'coletas', id, 'url_origem', url_origem FROM coletas
                UNION ALL
                SELECT 'coletas', id, 'baixado_em', baixado_em FROM coletas
                UNION ALL
                SELECT 'coletas', id, 'processado_em', processado_em
                FROM coletas
                UNION ALL
                SELECT 'cotacoes', id, 'data_cotacao', data_cotacao
                FROM cotacoes
                UNION ALL
                SELECT 'cotacoes', id, 'procedencia', procedencia FROM cotacoes
                UNION ALL
                SELECT 'cotacoes', id, 'classificacao', classificacao
                FROM cotacoes
                UNION ALL
                SELECT 'cotacoes', id, 'situacao_mercado', situacao_mercado
                FROM cotacoes
            )
            SELECT
                CASE
                    WHEN instr(valor, '|') > 0 THEN 'delimitador_vertical'
                    ELSE 'vazio_equivalente_a_null'
                END AS problema,
                tabela,
                registro_id,
                campo,
                valor
            FROM componentes
            WHERE instr(valor, '|') > 0 OR valor = ''
            ORDER BY tabela, registro_id, campo
            """
        )
        counts = {
            "delimitador_vertical": 0,
            "vazio_equivalente_a_null": 0,
        }
        sample_rows: list[tuple[object, ...]] = []
        for row in cursor:
            problem = str(row["problema"])
            counts[problem] += 1
            if len(sample_rows) < self.sample_limit:
                sample_rows.append(tuple(row))

        if not any(counts.values()):
            return

        report = self._write_rows(
            "amostra_componentes_chave_ambiguos.csv",
            ("problema", "tabela", "registro_id", "campo", "valor"),
            sample_rows,
        )
        if counts["delimitador_vertical"]:
            self.findings.append(
                Finding(
                    code="key_delimiter_components",
                    title="Componentes de chave contendo o delimitador vertical",
                    severity="alerta",
                    groups=counts["delimitador_vertical"],
                    occurrences=counts["delimitador_vertical"],
                    report_file=report,
                    explanation=(
                        "A implementacao concatena componentes com '|'. Um "
                        "componente que ja contenha esse caractere torna a "
                        "serializacao ambigua. O alerta nao autoriza alterar "
                        "nem excluir o registro."
                    ),
                )
            )
        if counts["vazio_equivalente_a_null"]:
            self.findings.append(
                Finding(
                    code="empty_key_components",
                    title="Componentes vazios indistinguiveis de valores nulos",
                    severity="alerta",
                    groups=counts["vazio_equivalente_a_null"],
                    occurrences=counts["vazio_equivalente_a_null"],
                    report_file=report,
                    explanation=(
                        "Na formula atual, texto vazio e NULL produzem o mesmo "
                        "trecho da chave. Isso indica risco de colisao sem "
                        "provar que duas entidades distintas colidiram."
                    ),
                )
            )

    def _audit_keys(self, connection: sqlite3.Connection) -> None:
        self._audit_content_key_formats(connection)
        self._audit_key_serialization(connection)
        self._add_row_finding(
            connection=connection,
            code="market_collection_ownership",
            title="Entrepostos ligados a uma CEASA diferente da coleta",
            severity="erro",
            count_sql="""
                SELECT COUNT(*)
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN entrepostos ent ON ent.id = co.entreposto_id
                WHERE ent.ceasa_id IS NOT col.ceasa_id
            """,
            detail_sql="""
                SELECT
                    co.id AS cotacao_id,
                    co.coleta_id,
                    col.ceasa_id AS ceasa_coleta_id,
                    co.entreposto_id,
                    ent.ceasa_id AS ceasa_entreposto_id
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN entrepostos ent ON ent.id = co.entreposto_id
                WHERE ent.ceasa_id IS NOT col.ceasa_id
                ORDER BY co.id
            """,
            report_name="ownership_entreposto_coleta_invalido.csv",
            explanation=(
                "A cotacao referencia um entreposto pertencente a outra CEASA, "
                "o que contradiz a origem registrada na coleta."
            ),
        )
        self._add_row_finding(
            connection=connection,
            code="presentation_keys_not_reproducible",
            title="Chaves de apresentacao nao reproduzidas pelos valores atuais",
            severity="alerta",
            count_sql="""
                SELECT COUNT(*)
                FROM apresentacoes_unidade au
                LEFT JOIN unidades u ON u.id = au.unidade_id
                WHERE au.unidade_original IS NULL
                   OR au.chave_unica IS NOT audit_hash_values(
                        au.unidade_original,
                        au.unidade_normalizada,
                        u.sigla,
                        au.embalagem,
                        audit_decimal(au.quantidade_minima),
                        audit_decimal(au.quantidade_maxima),
                        au.detalhe_unidade
                   )
            """,
            detail_sql="""
                SELECT
                    au.id,
                    au.chave_unica AS chave_armazenada,
                    audit_hash_values(
                        au.unidade_original,
                        au.unidade_normalizada,
                        u.sigla,
                        au.embalagem,
                        audit_decimal(au.quantidade_minima),
                        audit_decimal(au.quantidade_maxima),
                        au.detalhe_unidade
                    ) AS chave_recalculada,
                    au.unidade_original,
                    au.unidade_normalizada,
                    u.sigla,
                    au.embalagem,
                    au.quantidade_minima,
                    au.quantidade_maxima,
                    au.detalhe_unidade
                FROM apresentacoes_unidade au
                LEFT JOIN unidades u ON u.id = au.unidade_id
                WHERE au.unidade_original IS NULL
                   OR au.chave_unica IS NOT audit_hash_values(
                        au.unidade_original,
                        au.unidade_normalizada,
                        u.sigla,
                        au.embalagem,
                        audit_decimal(au.quantidade_minima),
                        audit_decimal(au.quantidade_maxima),
                        au.detalhe_unidade
                   )
                ORDER BY au.id
            """,
            report_name="chaves_apresentacao_nao_reproduzidas.csv",
            explanation=(
                "O SQLite pode perder zeros decimais finais pela afinidade "
                "NUMERIC, portanto uma divergencia aqui nao prova corrupcao. "
                "Ela apenas impede validar historicamente a chave sem o bruto."
            ),
        )
        key_checks = (
            (
                "invalid_quote_key_format",
                "Chaves de cotacao fora do formato SHA-256",
                """
                SELECT COUNT(*)
                FROM cotacoes co
                WHERE audit_valid_sha256(co.chave_unica) = 0
                   OR audit_valid_sha256(co.chave_identidade) = 0
                """,
                """
                SELECT
                    co.id,
                    ce.slug AS fonte,
                    co.data_cotacao,
                    co.chave_unica,
                    co.chave_identidade
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                WHERE audit_valid_sha256(co.chave_unica) = 0
                   OR audit_valid_sha256(co.chave_identidade) = 0
                ORDER BY co.id
                """,
                "chaves_cotacao_formato_invalido.csv",
                "A chave nao possui 64 caracteres hexadecimais minusculos.",
            ),
            (
                "invalid_presentation_key_format",
                "Chaves de apresentacao fora do formato SHA-256",
                """
                SELECT COUNT(*)
                FROM apresentacoes_unidade au
                WHERE audit_valid_sha256(au.chave_unica) = 0
                """,
                """
                SELECT au.id, au.chave_unica, au.unidade_original
                FROM apresentacoes_unidade au
                WHERE audit_valid_sha256(au.chave_unica) = 0
                ORDER BY au.id
                """,
                "chaves_apresentacao_formato_invalido.csv",
                "A chave nao possui 64 caracteres hexadecimais minusculos.",
            ),
            (
                "invalid_collection_or_raw_hash_format",
                "Chaves de coleta ou hashes brutos fora do formato SHA-256",
                """
                SELECT COUNT(*)
                FROM coletas col
                WHERE audit_valid_sha256(col.chave_unica) = 0
                   OR (
                        col.hash_raw IS NOT NULL
                        AND audit_valid_sha256(col.hash_raw) = 0
                   )
                """,
                """
                SELECT col.id, col.chave_unica, col.hash_raw, col.arquivo_raw
                FROM coletas col
                WHERE audit_valid_sha256(col.chave_unica) = 0
                   OR (
                        col.hash_raw IS NOT NULL
                        AND audit_valid_sha256(col.hash_raw) = 0
                   )
                ORDER BY col.id
                """,
                "chaves_coleta_hash_raw_formato_invalido.csv",
                (
                    "A chave de coleta ou o hash bruto registrado nao possui "
                    "64 caracteres hexadecimais minusculos."
                ),
            ),
            (
                "invalid_identity_keys",
                "Chaves de identidade divergentes",
                """
                SELECT COUNT(*)
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                LEFT JOIN entrepostos ent ON ent.id = co.entreposto_id
                JOIN categorias cat ON cat.id = co.categoria_id
                JOIN produto_aliases pa ON pa.id = co.produto_alias_id
                JOIN produtos p ON p.id = pa.produto_id
                LEFT JOIN apresentacoes_unidade au
                    ON au.id = co.apresentacao_unidade_id
                WHERE co.chave_identidade IS NOT audit_hash_values(
                    ce.slug,
                    ent.slug,
                    cat.slug,
                    p.nome_normalizado,
                    au.chave_unica,
                    co.procedencia,
                    co.classificacao,
                    co.data_cotacao
                )
                """,
                """
                SELECT
                    co.id,
                    ce.slug AS fonte,
                    co.data_cotacao,
                    p.nome_normalizado AS produto,
                    co.chave_identidade AS chave_armazenada,
                    audit_hash_values(
                        ce.slug,
                        ent.slug,
                        cat.slug,
                        p.nome_normalizado,
                        au.chave_unica,
                        co.procedencia,
                        co.classificacao,
                        co.data_cotacao
                    ) AS chave_esperada
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                LEFT JOIN entrepostos ent ON ent.id = co.entreposto_id
                JOIN categorias cat ON cat.id = co.categoria_id
                JOIN produto_aliases pa ON pa.id = co.produto_alias_id
                JOIN produtos p ON p.id = pa.produto_id
                LEFT JOIN apresentacoes_unidade au
                    ON au.id = co.apresentacao_unidade_id
                WHERE co.chave_identidade IS NOT audit_hash_values(
                    ce.slug,
                    ent.slug,
                    cat.slug,
                    p.nome_normalizado,
                    au.chave_unica,
                    co.procedencia,
                    co.classificacao,
                    co.data_cotacao
                )
                ORDER BY co.id
                """,
                "chaves_identidade_invalidas.csv",
                (
                    "A chave_identidade nao corresponde aos campos que definem "
                    "fonte, produto, unidade, classificacao e data."
                ),
            ),
            (
                "invalid_collection_keys",
                "Chaves de coleta divergentes",
                """
                SELECT COUNT(*)
                FROM coletas col
                JOIN ceasas ce ON ce.id = col.ceasa_id
                WHERE col.chave_unica IS NOT audit_hash_values(
                    ce.slug,
                    col.arquivo_raw,
                    col.hash_raw,
                    col.url_origem,
                    COALESCE(col.baixado_em, col.processado_em)
                )
                """,
                """
                SELECT
                    col.id,
                    ce.slug AS fonte,
                    col.arquivo_raw,
                    col.chave_unica AS chave_armazenada,
                    audit_hash_values(
                        ce.slug,
                        col.arquivo_raw,
                        col.hash_raw,
                        col.url_origem,
                        COALESCE(col.baixado_em, col.processado_em)
                    ) AS chave_esperada
                FROM coletas col
                JOIN ceasas ce ON ce.id = col.ceasa_id
                WHERE col.chave_unica IS NOT audit_hash_values(
                    ce.slug,
                    col.arquivo_raw,
                    col.hash_raw,
                    col.url_origem,
                    COALESCE(col.baixado_em, col.processado_em)
                )
                ORDER BY col.id
                """,
                "chaves_coleta_invalidas.csv",
                "A assinatura da coleta nao corresponde aos metadados armazenados.",
            ),
            (
                "invalid_product_normalization",
                "Produtos ligados a normalizacoes divergentes",
                """
                SELECT COUNT(*)
                FROM produto_aliases pa
                JOIN produtos p ON p.id = pa.produto_id
                WHERE p.nome_normalizado IS NOT audit_basic_name(pa.nome_original)
                """,
                """
                SELECT
                    pa.id AS alias_id,
                    pa.nome_original,
                    p.id AS produto_id,
                    p.nome_normalizado,
                    audit_basic_name(pa.nome_original) AS normalizacao_esperada
                FROM produto_aliases pa
                JOIN produtos p ON p.id = pa.produto_id
                WHERE p.nome_normalizado IS NOT audit_basic_name(pa.nome_original)
                ORDER BY p.nome_normalizado, pa.nome_original
                """,
                "normalizacao_produtos_divergente.csv",
                (
                    "O alias aponta para um produto diferente do resultado do "
                    "normalizador usado atualmente pelo armazenamento."
                ),
            ),
        )

        for (
            code,
            title,
            count_sql,
            detail_sql,
            report_name,
            explanation,
        ) in key_checks:
            formula_alerts = {
                "invalid_identity_keys",
                "invalid_collection_keys",
                "invalid_product_normalization",
            }
            self._add_row_finding(
                connection=connection,
                code=code,
                title=title,
                severity="alerta" if code in formula_alerts else "erro",
                count_sql=count_sql,
                detail_sql=detail_sql,
                report_name=report_name,
                explanation=explanation,
            )

    def _audit_domain_values(self, connection: sqlite3.Connection) -> None:
        objective_issues_sql = """
            WITH problemas AS (
                SELECT co.id, 'todos_precos_ausentes' AS problema
                FROM cotacoes co
                WHERE co.preco_minimo IS NULL
                  AND co.preco_comum IS NULL
                  AND co.preco_maximo IS NULL

                UNION ALL

                SELECT co.id, 'preco_negativo'
                FROM cotacoes co
                WHERE co.preco_minimo < 0
                   OR co.preco_comum < 0
                   OR co.preco_maximo < 0

                UNION ALL

                SELECT co.id, 'tipo_de_preco_invalido'
                FROM cotacoes co
                WHERE typeof(co.preco_minimo) NOT IN ('null', 'integer', 'real')
                   OR typeof(co.preco_comum) NOT IN ('null', 'integer', 'real')
                   OR typeof(co.preco_maximo) NOT IN ('null', 'integer', 'real')

                UNION ALL

                SELECT co.id, 'data_invalida'
                FROM cotacoes co
                WHERE audit_valid_iso_date(co.data_cotacao) = 0
            )
        """
        objective_count_sql = (
            objective_issues_sql + " SELECT COUNT(*) FROM problemas"
        )
        objective_detail_sql = objective_issues_sql + """
            SELECT
                problemas.problema,
                co.id,
                ce.slug AS fonte,
                co.data_cotacao,
                p.nome_normalizado AS produto,
                co.preco_minimo,
                co.preco_comum,
                co.preco_maximo,
                co.situacao_mercado
            FROM problemas
            JOIN cotacoes co ON co.id = problemas.id
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            ORDER BY problemas.problema, ce.slug, co.data_cotacao, co.id
        """
        self._add_row_finding(
            connection=connection,
            code="invalid_domain_values",
            title="Valores objetivamente invalidos",
            severity="erro",
            count_sql=objective_count_sql,
            detail_sql=objective_detail_sql,
            report_name="valores_invalidos.csv",
            explanation=(
                "Inclui data invalida, ausencia total de preco, preco negativo "
                "ou tipo de armazenamento nao numerico."
            ),
        )

        ordering_issues_sql = """
            WITH anomalias AS (
                SELECT co.id, 'preco_minimo_maior_que_maximo' AS anomalia
                FROM cotacoes co
                WHERE co.preco_minimo IS NOT NULL
                  AND co.preco_maximo IS NOT NULL
                  AND co.preco_minimo > co.preco_maximo

                UNION ALL

                SELECT co.id, 'preco_comum_abaixo_do_minimo'
                FROM cotacoes co
                WHERE co.preco_minimo IS NOT NULL
                  AND co.preco_comum IS NOT NULL
                  AND co.preco_comum < co.preco_minimo

                UNION ALL

                SELECT co.id, 'preco_comum_acima_do_maximo'
                FROM cotacoes co
                WHERE co.preco_comum IS NOT NULL
                  AND co.preco_maximo IS NOT NULL
                  AND co.preco_comum > co.preco_maximo
            )
        """
        ordering_count_sql = (
            ordering_issues_sql + " SELECT COUNT(*) FROM anomalias"
        )
        ordering_detail_sql = ordering_issues_sql + """
            SELECT
                anomalias.anomalia,
                co.id,
                ce.slug AS fonte,
                co.data_cotacao,
                p.nome_normalizado AS produto,
                co.preco_minimo,
                co.preco_comum,
                co.preco_maximo,
                co.situacao_mercado
            FROM anomalias
            JOIN cotacoes co ON co.id = anomalias.id
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            ORDER BY anomalias.anomalia, ce.slug, co.data_cotacao, co.id
        """
        self._add_row_finding(
            connection=connection,
            code="price_ordering_anomalies",
            title="Relacoes de minimo, comum e maximo fora da ordem",
            severity="alerta",
            count_sql=ordering_count_sql,
            detail_sql=ordering_detail_sql,
            report_name="anomalias_ordem_precos.csv",
            explanation=(
                "Essas ocorrencias existem no banco, mas podem ter sido "
                "publicadas pela fonte ou produzidas pelo parser. A mesma "
                "cotacao pode violar mais de uma regra e precisa ser conferida "
                "no arquivo bruto antes de ser considerada incorreta."
            ),
        )

        expected_partial_rows = connection.execute(
            """
            SELECT
                ce.slug AS fonte,
                CASE
                    WHEN co.fonte_complemento = 'prohort' THEN 'prohort'
                    WHEN ce.slug = 'ceasa-mg' THEN 'fonte_publica_apenas_comum'
                END AS motivo,
                COUNT(*) AS quantidade
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            WHERE (
                (co.preco_minimo IS NULL) +
                (co.preco_comum IS NULL) +
                (co.preco_maximo IS NULL)
            ) BETWEEN 1 AND 2
              AND (
                  co.fonte_complemento = 'prohort'
                  OR ce.slug = 'ceasa-mg'
              )
            GROUP BY ce.slug, motivo
            ORDER BY ce.slug, motivo
            """
        ).fetchall()
        expected_partial = [dict(row) for row in expected_partial_rows]

        if expected_partial:
            report = self._write_rows(
                "precos_parciais_esperados.csv",
                tuple(expected_partial[0]),
                (tuple(row.values()) for row in expected_partial),
            )
            self.findings.append(
                Finding(
                    code="expected_partial_prices",
                    title="Precos parciais esperados pelo contrato da fonte",
                    severity="info",
                    groups=len(expected_partial),
                    occurrences=sum(
                        int(row["quantidade"]) for row in expected_partial
                    ),
                    report_file=report,
                    explanation=(
                        "Registros do PROHORT e da CEASA-MG podem conter apenas "
                        "o preco comum. Eles foram separados dos alertas de "
                        "preenchimento incompleto."
                    ),
                )
            )

        today = date.today().isoformat()
        warning_issues_sql = """
            WITH alertas AS (
                SELECT co.id, 'preco_zero' AS alerta
                FROM cotacoes co
                WHERE co.preco_minimo = 0
                   OR co.preco_comum = 0
                   OR co.preco_maximo = 0

                UNION ALL

                SELECT co.id, 'conjunto_de_precos_incompleto'
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                WHERE (
                    (co.preco_minimo IS NULL) +
                    (co.preco_comum IS NULL) +
                    (co.preco_maximo IS NULL)
                ) BETWEEN 1 AND 2
                  AND ce.slug != 'ceasa-mg'
                  AND COALESCE(co.fonte_complemento, '') != 'prohort'

                UNION ALL

                SELECT co.id, 'amplitude_de_preco_acima_de_100x'
                FROM cotacoes co
                WHERE co.preco_minimo > 0
                  AND co.preco_maximo / co.preco_minimo > 100

                UNION ALL

                SELECT co.id, 'data_futura'
                FROM cotacoes co
                WHERE audit_valid_iso_date(co.data_cotacao) = 1
                  AND co.data_cotacao > ?
            )
        """
        warning_count_sql = warning_issues_sql + " SELECT COUNT(*) FROM alertas"
        warning_detail_sql = warning_issues_sql + """
            SELECT
                alertas.alerta,
                co.id,
                ce.slug AS fonte,
                co.data_cotacao,
                p.nome_normalizado AS produto,
                co.preco_minimo,
                co.preco_comum,
                co.preco_maximo,
                co.situacao_mercado
            FROM alertas
            JOIN cotacoes co ON co.id = alertas.id
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            ORDER BY alertas.alerta, ce.slug, co.data_cotacao, co.id
        """
        self._add_row_finding(
            connection=connection,
            code="suspicious_domain_values",
            title="Valores plausiveis, mas que merecem revisao",
            severity="alerta",
            count_sql=warning_count_sql,
            detail_sql=warning_detail_sql,
            report_name="valores_suspeitos.csv",
            explanation=(
                "Inclui preco zero, preenchimento parcial nao previsto pelo "
                "contrato da fonte, amplitude acima de 100 vezes e data futura."
            ),
            params=(today,),
        )

    def _audit_date_gaps(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT DISTINCT
                ce.slug AS fonte,
                COALESCE(ent.slug, '') AS entreposto,
                cat.slug AS categoria,
                co.data_cotacao
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            LEFT JOIN entrepostos ent ON ent.id = co.entreposto_id
            JOIN categorias cat ON cat.id = co.categoria_id
            WHERE audit_valid_iso_date(co.data_cotacao) = 1
            ORDER BY ce.slug, entreposto, cat.slug, co.data_cotacao
            """
        )
        gaps: list[tuple[str, str, str, str, str, int]] = []
        previous_scope: tuple[str, str, str] | None = None
        previous_date: date | None = None

        for row in rows:
            scope = (
                str(row["fonte"]),
                str(row["entreposto"]),
                str(row["categoria"]),
            )
            current_date = date.fromisoformat(str(row["data_cotacao"]))

            if scope == previous_scope and previous_date is not None:
                missing_days = (current_date - previous_date).days - 1
                if missing_days >= self.gap_days:
                    gaps.append(
                        (
                            *scope,
                            previous_date.isoformat(),
                            current_date.isoformat(),
                            missing_days,
                        )
                    )

            previous_scope = scope
            previous_date = current_date

        if not gaps:
            return

        report = self._write_rows(
            "lacunas_temporais.csv",
            (
                "fonte",
                "entreposto",
                "categoria",
                "data_anterior",
                "data_seguinte",
                "dias_sem_cotacao",
            ),
            gaps[: self.sample_limit],
        )
        self.findings.append(
            Finding(
                code="date_gaps",
                title=f"Lacunas de pelo menos {self.gap_days} dias",
                severity="info",
                groups=len(gaps),
                occurrences=len(gaps),
                report_file=report,
                explanation=(
                    "As lacunas foram calculadas por fonte, entreposto e "
                    "categoria. Elas descrevem a cobertura, mas nao sao erro "
                    "sem considerar calendario e disponibilidade historica."
                ),
            )
        )

    def _audit_duplicate_raw_content(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, object]:
        rows = connection.execute(
            """
            WITH grupos AS (
                SELECT
                    col.ceasa_id,
                    ce.slug AS fonte,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo) AS preco_minimo,
                    audit_decimal(co.preco_comum) AS preco_comum,
                    audit_decimal(co.preco_maximo) AS preco_maximo,
                    co.situacao_mercado
                FROM cotacoes co
                JOIN coletas col ON col.id = co.coleta_id
                JOIN ceasas ce ON ce.id = col.ceasa_id
                GROUP BY
                    col.ceasa_id,
                    ce.slug,
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo),
                    audit_decimal(co.preco_comum),
                    audit_decimal(co.preco_maximo),
                    co.situacao_mercado
                HAVING COUNT(*) > 1
            )
            SELECT
                audit_structured_hash(
                    g.fonte,
                    g.chave_identidade,
                    g.preco_minimo,
                    g.preco_comum,
                    g.preco_maximo,
                    g.situacao_mercado
                ) AS grupo_id,
                ce.slug AS fonte,
                co.id AS cotacao_id,
                col.id AS coleta_id,
                col.arquivo_raw,
                col.hash_raw,
                col.url_origem,
                co.fonte_complemento,
                audit_hash_values(
                    co.chave_identidade,
                    audit_decimal(co.preco_minimo),
                    audit_decimal(co.preco_comum),
                    audit_decimal(co.preco_maximo),
                    co.situacao_mercado
                ) AS chave_logica
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN grupos g
              ON g.ceasa_id = col.ceasa_id
             AND g.fonte = ce.slug
             AND g.chave_identidade = co.chave_identidade
             AND g.preco_minimo IS audit_decimal(co.preco_minimo)
             AND g.preco_comum IS audit_decimal(co.preco_comum)
             AND g.preco_maximo IS audit_decimal(co.preco_maximo)
             AND g.situacao_mercado IS co.situacao_mercado
            ORDER BY ce.slug, grupo_id, col.id, co.id
            """
        ).fetchall()

        if not rows:
            return {
                "status": "not_applicable",
                "safe_for_cleanup": False,
                "duplicate_groups": 0,
                "duplicate_rows": 0,
                "scopes_total": 0,
            }

        scopes: dict[
            tuple[str, int, str, str, str],
            dict[str, object],
        ] = {}
        group_info: dict[str, dict[str, object]] = {}
        source_groups: dict[str, set[str]] = {}

        for row in rows:
            group_id = str(row["grupo_id"])
            source = str(row["fonte"])
            stored_path = str(row["arquivo_raw"] or "")
            expected_hash = str(row["hash_raw"] or "")
            source_url = str(row["url_origem"] or "")
            scope_key = (
                source,
                int(row["coleta_id"]),
                stored_path,
                expected_hash,
                source_url,
            )
            scope = scopes.setdefault(
                scope_key,
                {
                    "expected": {},
                    "has_external_complement": False,
                },
            )
            expected = scope["expected"]
            assert isinstance(expected, dict)
            expected.setdefault(str(row["chave_logica"]), set()).add(group_id)
            scope["has_external_complement"] = bool(
                scope["has_external_complement"]
                or row["fonte_complemento"]
            )

            group = group_info.setdefault(
                group_id,
                {
                    "fonte": source,
                    "cotacao_ids": set(),
                    "escopos": set(),
                },
            )
            cotacao_ids = group["cotacao_ids"]
            group_scopes = group["escopos"]
            assert isinstance(cotacao_ids, set)
            assert isinstance(group_scopes, set)
            cotacao_ids.add(int(row["cotacao_id"]))
            group_scopes.add(scope_key)
            source_groups.setdefault(source, set()).add(group_id)

        source_path = self.project_root / "src"
        source_path_text = source_path.as_posix()
        if source_path_text not in sys.path:
            sys.path.insert(0, source_path_text)

        from cotacoes_ceasa.normalizers.text import slugify
        from cotacoes_ceasa.normalizers.unit import normalize_unit
        from cotacoes_ceasa.parsers.pdf import configure_pdf_text_cache
        from cotacoes_ceasa.sources.registry import build_source_parser
        from cotacoes_ceasa.workflows.raw_processing import (
            parse_raw_document_metadata,
        )

        parser_cache: dict[str, object] = {}
        parse_cache: dict[
            tuple[str, str, str, str, str],
            tuple[set[str], int, int, tuple[tuple[str, int], ...]],
        ] = {}
        zip_cache = self.raw_zip_index_cache
        file_cache = self.raw_file_index_cache
        issue_rows: list[tuple[object, ...]] = []
        parser_duplicate_documents: dict[
            tuple[str, str, str, str, str],
            tuple[object, ...],
        ] = {}
        group_states: dict[str, list[str]] = {
            group_id: [] for group_id in group_info
        }
        source_totals: dict[str, dict[str, int]] = {}

        def source_total(source: str) -> dict[str, int]:
            return source_totals.setdefault(
                source,
                {
                    "grupos_duplicados": len(source_groups.get(source, set())),
                    "escopos_total": 0,
                    "escopos_sem_bruto": 0,
                    "escopos_sem_hash": 0,
                    "escopos_elegiveis": 0,
                    "escopos_processados": 0,
                    "escopos_totalmente_reproduzidos": 0,
                    "escopos_parcialmente_reproduzidos": 0,
                    "escopos_nao_reproduzidos": 0,
                    "erros_pipeline": 0,
                    "chaves_esperadas": 0,
                    "chaves_reproduzidas": 0,
                    "chaves_nao_reproduzidas": 0,
                    "chaves_extras_parser": 0,
                    "repeticoes_emitidas_parser": 0,
                },
            )

        def register_state(
            expected: dict[str, set[str]],
            state: str,
            matched_keys: set[str] | None = None,
        ) -> None:
            matched_keys = matched_keys or set()
            for logical_key, group_ids in expected.items():
                key_state = "reproduzida" if logical_key in matched_keys else state
                for group_id in group_ids:
                    group_states[group_id].append(key_state)

        total_scopes = len(scopes)
        with tempfile.TemporaryDirectory(
            prefix=".pdf-text-audit-",
            dir=self.output_directory,
        ) as temporary_cache:
            configure_pdf_text_cache(Path(temporary_cache))
            try:
                for index, (scope_key, scope) in enumerate(
                    scopes.items(),
                    start=1,
                ):
                    (
                        source,
                        collection_id,
                        stored_path,
                        expected_hash,
                        source_url,
                    ) = scope_key
                    expected = scope["expected"]
                    assert isinstance(expected, dict)
                    expected_keys = set(expected)
                    totals = source_total(source)
                    totals["escopos_total"] += 1
                    totals["chaves_esperadas"] += len(expected_keys)

                    if not stored_path:
                        totals["escopos_sem_bruto"] += 1
                        register_state(expected, "sem_bruto")
                        issue_rows.append(
                            (
                                source,
                                collection_id,
                                stored_path,
                                "sem_bruto_registrado",
                                len(expected_keys),
                                0,
                                len(expected_keys),
                                0,
                                bool(scope["has_external_complement"]),
                                "",
                            )
                        )
                    elif not expected_hash:
                        totals["escopos_sem_hash"] += 1
                        register_state(expected, "sem_hash")
                        issue_rows.append(
                            (
                                source,
                                collection_id,
                                stored_path,
                                "sem_hash_registrado",
                                len(expected_keys),
                                0,
                                len(expected_keys),
                                0,
                                bool(scope["has_external_complement"]),
                                "",
                            )
                        )
                    else:
                        totals["escopos_elegiveis"] += 1
                        try:
                            metadata = parse_raw_document_metadata(
                                Path(stored_path)
                            )
                            candidates = self._find_raw_candidates(
                                stored_path,
                                zip_cache,
                                file_cache,
                            )
                            if not candidates:
                                raise FileNotFoundError(
                                    "Bruto registrado nao foi localizado."
                                )

                            raw_content: bytes | str | None = None
                            selected_display = ""
                            first_hash = ""
                            for candidate in candidates:
                                candidate_content, candidate_hash = (
                                    self._read_raw_candidate(candidate)
                                )
                                first_hash = first_hash or candidate_hash
                                if candidate_hash != expected_hash:
                                    continue
                                raw_content = candidate_content
                                _, candidate_path, member = candidate
                                selected_display = (
                                    candidate_path.as_posix()
                                    if member is None
                                    else (
                                        f"{candidate_path.as_posix()}::{member}"
                                    )
                                )
                                break

                            if raw_content is None:
                                raise RuntimeError(
                                    "Nenhum candidato corresponde ao hash "
                                    f"registrado; primeiro hash={first_hash}."
                                )

                            target_date = (
                                metadata.target_date.isoformat()
                                if metadata.target_date is not None
                                else ""
                            )
                            parse_key = (
                                source,
                                expected_hash,
                                metadata.category_slug,
                                target_date,
                                source_url,
                            )
                            parsed_result = parse_cache.get(parse_key)

                            if parsed_result is None:
                                parser = parser_cache.get(source)
                                if parser is None:
                                    parser = build_source_parser(source)
                                    parser_cache[source] = parser

                                if source == "ceasa-pr":
                                    parsed_quotes = parser.parse_category(
                                        raw_content,
                                        metadata.category_slug,
                                        source_url,
                                        target_date=metadata.target_date,
                                    )
                                else:
                                    parsed_quotes = parser.parse_category(
                                        raw_content,
                                        metadata.category_slug,
                                        source_url,
                                    )

                                city = str(self.source_config[source]["city"])
                                parsed_keys: list[str] = []
                                for quote in parsed_quotes:
                                    quote_date = quote.data_cotacao
                                    if (
                                        quote_date is None
                                        and source == "ceasa-pr"
                                        and metadata.target_date is not None
                                    ):
                                        quote_date = metadata.target_date

                                    market_name = quote.entreposto or (
                                        None
                                        if city.lower() == "varias cidades"
                                        else city
                                    )
                                    market_slug = (
                                        slugify(market_name)
                                        if market_name is not None
                                        else None
                                    )
                                    unit = normalize_unit(quote.unidade)
                                    presentation_key = (
                                        None
                                        if unit.original is None
                                        else hash_values(
                                            unit.original,
                                            unit.normalized,
                                            unit.symbol,
                                            unit.packaging,
                                            (
                                                str(unit.quantity_min)
                                                if unit.quantity_min is not None
                                                else None
                                            ),
                                            (
                                                str(unit.quantity_max)
                                                if unit.quantity_max is not None
                                                else None
                                            ),
                                            unit.detail,
                                        )
                                    )
                                    identity_key = hash_values(
                                        source,
                                        market_slug,
                                        quote.categoria,
                                        basic_name(quote.produto),
                                        presentation_key,
                                        quote.procedencia,
                                        quote.classificacao,
                                        (
                                            quote_date.isoformat()
                                            if quote_date is not None
                                            else None
                                        ),
                                    )
                                    parsed_keys.append(
                                        hash_values(
                                            identity_key,
                                            decimal_key(quote.preco_minimo),
                                            decimal_key(quote.preco_comum),
                                            decimal_key(quote.preco_maximo),
                                            quote.situacao_mercado,
                                        )
                                    )

                                parsed_key_counts = Counter(parsed_keys)
                                parsed_key_set = set(parsed_key_counts)
                                duplicated_keys = tuple(
                                    sorted(
                                        (key, count)
                                        for key, count in parsed_key_counts.items()
                                        if count > 1
                                    )
                                )
                                parsed_result = (
                                    parsed_key_set,
                                    sum(
                                        count - 1
                                        for _, count in duplicated_keys
                                    ),
                                    len(parsed_keys),
                                    duplicated_keys,
                                )
                                parse_cache[parse_key] = parsed_result

                            (
                                parsed_keys_set,
                                repeated_by_parser,
                                parsed_count,
                                duplicated_keys,
                            ) = parsed_result
                            if repeated_by_parser and parse_key not in (
                                parser_duplicate_documents
                            ):
                                parser_duplicate_documents[parse_key] = (
                                    source,
                                    collection_id,
                                    stored_path,
                                    selected_display,
                                    expected_hash,
                                    metadata.category_slug,
                                    target_date,
                                    source_url,
                                    parsed_count,
                                    len(parsed_keys_set),
                                    len(duplicated_keys),
                                    repeated_by_parser,
                                    max(count for _, count in duplicated_keys),
                                    json.dumps(
                                        dict(duplicated_keys),
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                )
                            matched = expected_keys & parsed_keys_set
                            missing = expected_keys - parsed_keys_set
                            extras = parsed_keys_set - expected_keys
                            totals["escopos_processados"] += 1
                            totals["chaves_reproduzidas"] += len(matched)
                            totals["chaves_nao_reproduzidas"] += len(missing)
                            totals["chaves_extras_parser"] += len(extras)
                            totals["repeticoes_emitidas_parser"] += (
                                repeated_by_parser
                            )
                            register_state(
                                expected,
                                "nao_reproduzida",
                                matched,
                            )

                            if not missing:
                                totals[
                                    "escopos_totalmente_reproduzidos"
                                ] += 1
                            elif matched:
                                totals[
                                    "escopos_parcialmente_reproduzidos"
                                ] += 1
                            else:
                                totals["escopos_nao_reproduzidos"] += 1

                            if missing:
                                issue_rows.append(
                                    (
                                        source,
                                        collection_id,
                                        stored_path,
                                        "chaves_nao_reproduzidas",
                                        len(expected_keys),
                                        len(matched),
                                        len(missing),
                                        len(extras),
                                        bool(
                                            scope[
                                                "has_external_complement"
                                            ]
                                        ),
                                        (
                                            f"parser_rows={parsed_count}; "
                                            f"bruto={selected_display}"
                                        ),
                                    )
                                )
                        except Exception as error:
                            totals["erros_pipeline"] += 1
                            register_state(expected, "erro_pipeline")
                            issue_rows.append(
                                (
                                    source,
                                    collection_id,
                                    stored_path,
                                    "erro_pipeline_atual_ou_bruto",
                                    len(expected_keys),
                                    0,
                                    len(expected_keys),
                                    0,
                                    bool(scope["has_external_complement"]),
                                    f"{type(error).__name__}: {error}",
                                )
                            )

                    if index % 250 == 0 or index == total_scopes:
                        self._step(
                            "Reconciliacao de duplicatas: "
                            f"{index:,}/{total_scopes:,} escopos"
                        )
            finally:
                configure_pdf_text_cache(None)

        group_rows: list[tuple[object, ...]] = []
        group_class_counts: dict[str, int] = {}
        for group_id, info in sorted(group_info.items()):
            states = group_states[group_id]
            reproduced = states.count("reproduzida")
            not_reproduced = states.count("nao_reproduzida")
            without_raw = states.count("sem_bruto")
            without_hash = states.count("sem_hash")
            errors = states.count("erro_pipeline")
            if states and reproduced == len(states):
                classification = "reproduzido_integralmente_pipeline_atual"
            elif reproduced:
                classification = "cobertura_parcial"
            elif not_reproduced:
                classification = "nao_reproduzido_pipeline_atual"
            else:
                classification = "nao_verificavel"
            group_class_counts[classification] = (
                group_class_counts.get(classification, 0) + 1
            )
            cotacao_ids = info["cotacao_ids"]
            group_scopes = info["escopos"]
            assert isinstance(cotacao_ids, set)
            assert isinstance(group_scopes, set)
            group_rows.append(
                (
                    group_id,
                    info["fonte"],
                    len(cotacao_ids),
                    len(group_scopes),
                    reproduced,
                    not_reproduced,
                    without_raw,
                    without_hash,
                    errors,
                    classification,
                    0,
                )
            )

        group_report = self._write_rows(
            "cobertura_integral_grupos_duplicados.csv",
            (
                "grupo_id",
                "fonte",
                "linhas_banco",
                "escopos",
                "escopos_reproduzidos",
                "escopos_nao_reproduzidos",
                "escopos_sem_bruto",
                "escopos_sem_hash",
                "escopos_com_erro",
                "classificacao",
                "exclusao_automatica_permitida",
            ),
            group_rows,
        )
        summary_rows = [
            {"fonte": source, **totals}
            for source, totals in sorted(source_totals.items())
        ]
        summary_report = self._write_rows(
            "resumo_reconciliacao_duplicatas_brutos.csv",
            tuple(summary_rows[0]),
            (tuple(row.values()) for row in summary_rows),
        )
        issue_report = None
        if issue_rows:
            issue_report = self._write_rows(
                "problemas_reconciliacao_duplicatas_brutos.csv",
                (
                    "fonte",
                    "coleta_id",
                    "arquivo_raw",
                    "problema",
                    "chaves_esperadas",
                    "chaves_reproduzidas",
                    "chaves_nao_reproduzidas",
                    "chaves_extras_parser",
                    "possui_complemento_externo",
                    "detalhe",
                ),
                issue_rows,
            )

        parser_duplicate_report = None
        if parser_duplicate_documents:
            parser_duplicate_report = self._write_rows(
                "documentos_com_repeticoes_emitidas_parser.csv",
                (
                    "fonte",
                    "coleta_id_exemplo",
                    "arquivo_registrado",
                    "arquivo_resolvido",
                    "hash_raw",
                    "categoria",
                    "data_alvo",
                    "url_origem",
                    "linhas_parser",
                    "chaves_unicas_parser",
                    "chaves_repetidas_distintas",
                    "ocorrencias_excedentes",
                    "maior_multiplicidade",
                    "contagens_por_chave",
                ),
                parser_duplicate_documents.values(),
            )
            repeated_unique = sum(
                int(row[11])
                for row in parser_duplicate_documents.values()
            )
            self.findings.append(
                Finding(
                    code="current_parser_emits_repeated_keys",
                    title=(
                        "Documentos em que o parser atual emite chaves repetidas"
                    ),
                    severity="alerta",
                    groups=len(parser_duplicate_documents),
                    occurrences=repeated_unique,
                    report_file=parser_duplicate_report,
                    explanation=(
                        "A mesma chave logica saiu mais de uma vez ao processar "
                        "um unico bruto. Isso pode refletir linhas repetidas na "
                        "fonte ou sobreposicao do parser. O armazenamento atual "
                        "impediu repeticoes dentro da mesma coleta; o achado nao "
                        "autoriza remover registros de coletas diferentes."
                    ),
                )
            )

        affected_sources = {
            str(row[0]) for row in parser_duplicate_documents.values()
        }
        source_wide_collision_summary = (
            self._audit_source_wide_parser_collisions(
                connection,
                affected_sources,
            )
            if affected_sources
            else {"status": "not_needed", "sources": []}
        )

        totals_all = {
            key: sum(source.get(key, 0) for source in source_totals.values())
            for key in (
                "escopos_total",
                "escopos_sem_bruto",
                "escopos_sem_hash",
                "escopos_elegiveis",
                "escopos_processados",
                "escopos_totalmente_reproduzidos",
                "escopos_parcialmente_reproduzidos",
                "escopos_nao_reproduzidos",
                "erros_pipeline",
                "chaves_esperadas",
                "chaves_reproduzidas",
                "chaves_nao_reproduzidas",
                "chaves_extras_parser",
                "repeticoes_emitidas_parser",
            )
        }
        incomplete_groups = len(group_info) - group_class_counts.get(
            "reproduzido_integralmente_pipeline_atual",
            0,
        )
        status = (
            "all_groups_reencountered_by_current_pipeline"
            if incomplete_groups == 0
            else "partial_or_with_current_pipeline_differences"
        )

        coverage_issues = (
            totals_all["escopos_sem_bruto"]
            + totals_all["escopos_sem_hash"]
            + totals_all["erros_pipeline"]
            + totals_all["chaves_nao_reproduzidas"]
        )
        if coverage_issues:
            self.findings.append(
                Finding(
                    code="duplicate_raw_reconciliation_issues",
                    title=(
                        "Grupos repetidos sem cobertura integral no pipeline atual"
                    ),
                    severity="alerta",
                    groups=incomplete_groups,
                    occurrences=coverage_issues,
                    report_file=issue_report or group_report,
                    explanation=(
                        "Inclui bruto ou hash ausente, falha de leitura/parser e "
                        "chave que o pipeline atual nao reencontrou. Mudancas "
                        "historicas do parser/configuracao podem explicar casos. "
                        "Isso nao prova erro no dado e nao autoriza exclusao."
                    ),
                )
            )

        self.findings.append(
            Finding(
                code="duplicate_raw_reconciliation",
                title=(
                    "Chaves de grupos repetidos reencontradas nos brutos pelo "
                    "pipeline atual"
                ),
                severity="info",
                groups=group_class_counts.get(
                    "reproduzido_integralmente_pipeline_atual",
                    0,
                ),
                occurrences=totals_all["chaves_reproduzidas"],
                report_file=summary_report,
                explanation=(
                    "A comparacao usa o codigo e a configuracao atuais, com "
                    "cache PDF temporario e isolado. Reencontrar uma chave prova "
                    "apenas que ela e reproduzivel hoje naquele bruto; nao prova "
                    "equivalencia documental completa nem permite apagar linhas."
                ),
            )
        )

        return {
            "status": status,
            "safe_for_cleanup": False,
            "duplicate_groups": len(group_info),
            "duplicate_rows": len(rows),
            **totals_all,
            "group_classification": group_class_counts,
            "summary_report": summary_report,
            "group_coverage_report": group_report,
            "issue_report": issue_report,
            "issue_rows": len(issue_rows),
            "parser_duplicate_documents": len(parser_duplicate_documents),
            "parser_duplicate_occurrences_unique_documents": sum(
                int(row[11])
                for row in parser_duplicate_documents.values()
            ),
            "parser_duplicate_report": parser_duplicate_report,
            "source_wide_parser_collisions": (
                source_wide_collision_summary
            ),
            "cache_mode": "fresh_temporary_deleted_after_use",
            "pipeline_scope": "current_code_and_current_configuration",
        }

    def _audit_source_wide_parser_collisions(
        self,
        connection: sqlite3.Connection,
        sources: set[str],
    ) -> dict[str, object]:
        placeholders = ",".join("?" for _ in sources)
        rows = connection.execute(
            f"""
            SELECT
                ce.slug AS fonte,
                col.id AS coleta_id,
                col.arquivo_raw,
                col.hash_raw,
                col.url_origem
            FROM coletas col
            JOIN ceasas ce ON ce.id = col.ceasa_id
            WHERE ce.slug IN ({placeholders})
              AND col.arquivo_raw IS NOT NULL
              AND col.hash_raw IS NOT NULL
            ORDER BY ce.slug, col.id
            """,
            tuple(sorted(sources)),
        ).fetchall()

        from cotacoes_ceasa.normalizers.text import slugify
        from cotacoes_ceasa.normalizers.unit import normalize_unit
        from cotacoes_ceasa.parsers.pdf import configure_pdf_text_cache
        from cotacoes_ceasa.sources.registry import build_source_parser
        from cotacoes_ceasa.workflows.raw_processing import (
            parse_raw_document_metadata,
        )

        scopes: dict[
            tuple[str, str, str, str, str],
            dict[str, object],
        ] = {}
        for row in rows:
            stored_path = str(row["arquivo_raw"])
            metadata = parse_raw_document_metadata(Path(stored_path))
            target_date = (
                metadata.target_date.isoformat()
                if metadata.target_date is not None
                else ""
            )
            scope_key = (
                str(row["fonte"]),
                str(row["hash_raw"]),
                metadata.category_slug,
                target_date,
                str(row["url_origem"] or ""),
            )
            scope = scopes.setdefault(
                scope_key,
                {"paths": [], "collection_ids": []},
            )
            paths = scope["paths"]
            collection_ids = scope["collection_ids"]
            assert isinstance(paths, list)
            assert isinstance(collection_ids, list)
            if stored_path not in paths:
                paths.append(stored_path)
            collection_ids.append(int(row["coleta_id"]))

        parser_cache: dict[str, object] = {}
        collision_rows: list[tuple[object, ...]] = []
        error_rows: list[tuple[object, ...]] = []
        source_summary: dict[str, dict[str, int]] = {}

        def totals(source: str) -> dict[str, int]:
            return source_summary.setdefault(
                source,
                {
                    "colecoes": 0,
                    "brutos_unicos": 0,
                    "brutos_processados": 0,
                    "brutos_com_colisao": 0,
                    "grupos_colisao": 0,
                    "ocorrencias_excedentes": 0,
                    "erros": 0,
                },
            )

        for row in rows:
            totals(str(row["fonte"]))["colecoes"] += 1
        for source, *_ in scopes:
            totals(source)["brutos_unicos"] += 1

        with tempfile.TemporaryDirectory(
            prefix=".pdf-text-source-audit-",
            dir=self.output_directory,
        ) as temporary_cache:
            configure_pdf_text_cache(Path(temporary_cache))
            try:
                for index, (scope_key, scope) in enumerate(
                    sorted(scopes.items()),
                    start=1,
                ):
                    source, expected_hash, category, target_date, source_url = (
                        scope_key
                    )
                    source_totals = totals(source)
                    paths = scope["paths"]
                    collection_ids = scope["collection_ids"]
                    assert isinstance(paths, list)
                    assert isinstance(collection_ids, list)
                    try:
                        selected_content: bytes | str | None = None
                        selected_path = ""
                        selected_stored_path = ""
                        for stored_path in paths:
                            candidates = self._find_raw_candidates(
                                str(stored_path),
                                self.raw_zip_index_cache,
                                self.raw_file_index_cache,
                            )
                            for candidate in candidates:
                                content, actual_hash = self._read_raw_candidate(
                                    candidate
                                )
                                if actual_hash != expected_hash:
                                    continue
                                selected_content = content
                                selected_stored_path = str(stored_path)
                                _, candidate_path, member = candidate
                                selected_path = (
                                    candidate_path.as_posix()
                                    if member is None
                                    else (
                                        f"{candidate_path.as_posix()}::{member}"
                                    )
                                )
                                break
                            if selected_content is not None:
                                break

                        if selected_content is None:
                            raise FileNotFoundError(
                                "Nenhum bruto corresponde ao hash registrado."
                            )

                        parser = parser_cache.get(source)
                        if parser is None:
                            parser = build_source_parser(source)
                            parser_cache[source] = parser
                        metadata = parse_raw_document_metadata(
                            Path(selected_stored_path)
                        )
                        if source == "ceasa-pr":
                            quotes = parser.parse_category(
                                selected_content,
                                category,
                                source_url,
                                target_date=metadata.target_date,
                            )
                        else:
                            quotes = parser.parse_category(
                                selected_content,
                                category,
                                source_url,
                            )

                        city = str(self.source_config[source]["city"])
                        quotes_by_key: dict[str, list[object]] = {}
                        for quote in quotes:
                            quote_date = quote.data_cotacao
                            if (
                                quote_date is None
                                and source == "ceasa-pr"
                                and metadata.target_date is not None
                            ):
                                quote_date = metadata.target_date
                            market_name = quote.entreposto or (
                                None
                                if city.lower() == "varias cidades"
                                else city
                            )
                            market_slug = (
                                slugify(market_name)
                                if market_name is not None
                                else None
                            )
                            unit = normalize_unit(quote.unidade)
                            presentation_key = (
                                None
                                if unit.original is None
                                else hash_values(
                                    unit.original,
                                    unit.normalized,
                                    unit.symbol,
                                    unit.packaging,
                                    (
                                        str(unit.quantity_min)
                                        if unit.quantity_min is not None
                                        else None
                                    ),
                                    (
                                        str(unit.quantity_max)
                                        if unit.quantity_max is not None
                                        else None
                                    ),
                                    unit.detail,
                                )
                            )
                            identity_key = hash_values(
                                source,
                                market_slug,
                                quote.categoria,
                                basic_name(quote.produto),
                                presentation_key,
                                quote.procedencia,
                                quote.classificacao,
                                (
                                    quote_date.isoformat()
                                    if quote_date is not None
                                    else None
                                ),
                            )
                            logical_key = hash_values(
                                identity_key,
                                decimal_key(quote.preco_minimo),
                                decimal_key(quote.preco_comum),
                                decimal_key(quote.preco_maximo),
                                quote.situacao_mercado,
                            )
                            quotes_by_key.setdefault(logical_key, []).append(
                                (quote, quote_date)
                            )

                        document_has_collision = False
                        for logical_key, duplicate_quotes in quotes_by_key.items():
                            if len(duplicate_quotes) < 2:
                                continue
                            document_has_collision = True
                            quote, quote_date = duplicate_quotes[0]
                            excess = len(duplicate_quotes) - 1
                            source_totals["grupos_colisao"] += 1
                            source_totals["ocorrencias_excedentes"] += excess
                            collision_rows.append(
                                (
                                    source,
                                    expected_hash,
                                    ",".join(map(str, collection_ids)),
                                    selected_stored_path,
                                    selected_path,
                                    category,
                                    target_date,
                                    (
                                        quote_date.isoformat()
                                        if quote_date is not None
                                        else None
                                    ),
                                    quote.produto,
                                    quote.unidade,
                                    quote.procedencia,
                                    quote.classificacao,
                                    decimal_key(quote.preco_minimo),
                                    decimal_key(quote.preco_comum),
                                    decimal_key(quote.preco_maximo),
                                    quote.situacao_mercado,
                                    logical_key,
                                    len(duplicate_quotes),
                                    excess,
                                )
                            )
                        source_totals["brutos_processados"] += 1
                        if document_has_collision:
                            source_totals["brutos_com_colisao"] += 1
                    except Exception as error:
                        source_totals["erros"] += 1
                        error_rows.append(
                            (
                                source,
                                expected_hash,
                                ",".join(map(str, collection_ids)),
                                ";".join(map(str, paths)),
                                f"{type(error).__name__}: {error}",
                            )
                        )

                    if index % 25 == 0 or index == len(scopes):
                        self._step(
                            "Colisoes do parser nas fontes afetadas: "
                            f"{index:,}/{len(scopes):,} brutos unicos"
                        )
            finally:
                configure_pdf_text_cache(None)

        collision_report = None
        if collision_rows:
            collision_report = self._write_rows(
                "colisoes_parser_fontes_afetadas.csv",
                (
                    "fonte",
                    "hash_raw",
                    "coleta_ids",
                    "arquivo_registrado",
                    "arquivo_resolvido",
                    "categoria",
                    "data_alvo",
                    "data_cotacao",
                    "produto",
                    "unidade",
                    "procedencia",
                    "classificacao",
                    "preco_minimo",
                    "preco_comum",
                    "preco_maximo",
                    "situacao_mercado",
                    "chave_logica",
                    "multiplicidade",
                    "ocorrencias_excedentes",
                ),
                collision_rows,
            )
            self.findings.append(
                Finding(
                    code="source_wide_parser_key_collisions",
                    title=(
                        "Colisoes de chave emitidas pelo parser nas fontes "
                        "afetadas"
                    ),
                    severity="alerta",
                    groups=len(collision_rows),
                    occurrences=sum(int(row[-1]) for row in collision_rows),
                    report_file=collision_report,
                    explanation=(
                        "A expansao reprocessou todos os brutos unicos das "
                        "fontes onde o replay inicial encontrou repeticoes. "
                        "Uma colisao pode ser repeticao real da fonte ou perda "
                        "de uma distincao pelo parser. Ela exige conferir o "
                        "documento e impede qualquer limpeza automatica."
                    ),
                )
            )

        error_report = None
        if error_rows:
            error_report = self._write_rows(
                "erros_colisoes_parser_fontes_afetadas.csv",
                (
                    "fonte",
                    "hash_raw",
                    "coleta_ids",
                    "arquivos_registrados",
                    "erro",
                ),
                error_rows,
            )

        return {
            "status": "complete" if not error_rows else "partial_with_errors",
            "sources": sorted(sources),
            "collections": len(rows),
            "unique_raw_scopes": len(scopes),
            "collision_documents": sum(
                row["brutos_com_colisao"] for row in source_summary.values()
            ),
            "collision_groups": len(collision_rows),
            "collision_excess_occurrences": sum(
                int(row[-1]) for row in collision_rows
            ),
            "errors": len(error_rows),
            "source_summary": [
                {"fonte": source, **values}
                for source, values in sorted(source_summary.items())
            ],
            "collision_report": collision_report,
            "error_report": error_report,
            "safe_for_cleanup": False,
        }

    def _audit_raw_files(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, object]:
        without_local_rows = connection.execute(
            """
            SELECT
                ce.slug AS fonte,
                COUNT(DISTINCT col.id) AS coletas,
                COUNT(co.id) AS cotacoes,
                MIN(col.processado_em) AS primeiro_processamento,
                MAX(col.processado_em) AS ultimo_processamento
            FROM coletas col
            JOIN ceasas ce ON ce.id = col.ceasa_id
            LEFT JOIN cotacoes co ON co.coleta_id = col.id
            WHERE col.arquivo_raw IS NULL
            GROUP BY ce.slug
            ORDER BY coletas DESC, ce.slug
            """
        ).fetchall()
        without_local = [dict(row) for row in without_local_rows]
        without_local_collections = sum(
            int(row["coletas"]) for row in without_local
        )
        without_local_quotes = sum(
            int(row["cotacoes"]) for row in without_local
        )
        without_local_report = None

        if without_local:
            without_local_report = self._write_rows(
                "coletas_sem_bruto_local.csv",
                tuple(without_local[0]),
                (tuple(row.values()) for row in without_local),
            )
            self.findings.append(
                Finding(
                    code="collections_without_local_raw",
                    title="Coletas sem arquivo bruto local",
                    severity="info",
                    groups=without_local_collections,
                    occurrences=without_local_quotes,
                    report_file=without_local_report,
                    explanation=(
                        "Essas coletas nao foram verificadas por hash local. "
                        "Incluem importacoes externas, como PROHORT, e devem ser "
                        "avaliadas conforme o contrato da fonte."
                    ),
                )
            )

        raw_total = int(
            connection.execute(
                "SELECT COUNT(*) FROM coletas WHERE arquivo_raw IS NOT NULL"
            ).fetchone()[0]
        )
        raw_rows = connection.execute(
            """
            SELECT
                col.id,
                ce.slug AS fonte,
                col.arquivo_raw,
                col.hash_raw
            FROM coletas col
            JOIN ceasas ce ON ce.id = col.ceasa_id
            WHERE col.arquivo_raw IS NOT NULL
            ORDER BY col.id
            """
        )
        zip_cache = self.raw_zip_index_cache
        file_cache = self.raw_file_index_cache
        hash_cache: dict[tuple[str, Path, str | None], str] = {}
        issue_rows: list[tuple[object, ...]] = []
        status_counts = {
            "checked": 0,
            "ok": 0,
            "missing": 0,
            "hash_mismatch": 0,
            "missing_hash": 0,
            "unreadable": 0,
        }

        for row in raw_rows:
            status_counts["checked"] += 1
            expected_hash = str(row["hash_raw"]) if row["hash_raw"] else None

            if expected_hash is None:
                status = "missing_hash"
                resolved_path = None
                actual_hash = None
            else:
                try:
                    candidates = self._find_raw_candidates(
                        str(row["arquivo_raw"]),
                        zip_cache,
                        file_cache,
                    )
                    if not candidates:
                        status = "missing"
                        resolved_path = None
                        actual_hash = None
                    else:
                        status, resolved_path, actual_hash = self._match_raw_hash(
                            candidates,
                            expected_hash,
                            hash_cache,
                        )
                except (
                    OSError,
                    UnicodeDecodeError,
                    zipfile.BadZipFile,
                    RuntimeError,
                ):
                    status = "unreadable"
                    resolved_path = None
                    actual_hash = None

            status_counts[status] += 1

            if status != "ok" and len(issue_rows) < self.sample_limit:
                issue_rows.append(
                    (
                        row["id"],
                        row["fonte"],
                        row["arquivo_raw"],
                        resolved_path,
                        status,
                        expected_hash,
                        actual_hash,
                    )
                )

            checked = status_counts["checked"]
            if checked % 1000 == 0 or checked == raw_total:
                self._step(
                    "Arquivos brutos: "
                    f"{checked:,}/{raw_total:,} coletas verificadas"
                )

        if issue_rows:
            report = self._write_rows(
                "problemas_arquivos_brutos.csv",
                (
                    "coleta_id",
                    "fonte",
                    "arquivo_registrado",
                    "arquivo_encontrado",
                    "problema",
                    "hash_esperado",
                    "hash_encontrado",
                ),
                issue_rows,
            )
        else:
            report = None

        raw_findings = (
            (
                "raw_hash_mismatch",
                "Arquivos brutos com hash divergente",
                "erro",
                "hash_mismatch",
            ),
            (
                "raw_missing",
                "Arquivos brutos registrados e nao encontrados",
                "alerta",
                "missing",
            ),
            (
                "raw_missing_hash",
                "Arquivos brutos registrados sem hash",
                "alerta",
                "missing_hash",
            ),
            (
                "raw_unreadable",
                "Arquivos brutos que nao puderam ser lidos",
                "alerta",
                "unreadable",
            ),
        )

        for code, title, severity, status_key in raw_findings:
            occurrences = status_counts[status_key]
            if occurrences == 0:
                continue
            self.findings.append(
                Finding(
                    code=code,
                    title=title,
                    severity=severity,
                    groups=occurrences,
                    occurrences=occurrences,
                    report_file=report,
                    explanation=(
                        "A verificacao usa a mesma representacao do crawler: "
                        "bytes para PDF e texto UTF-8 com quebras de linha "
                        "normalizadas para HTML."
                    ),
                )
            )

        archive_error_report = None
        if self.raw_archive_errors:
            archive_error_report = self._write_rows(
                "arquivos_zip_ilegíveis.csv",
                ("arquivo_zip", "erro"),
                sorted(self.raw_archive_errors.items()),
            )
            self.findings.append(
                Finding(
                    code="raw_archive_unreadable",
                    title="Arquivos ZIP de brutos que nao puderam ser indexados",
                    severity="alerta",
                    groups=len(self.raw_archive_errors),
                    occurrences=len(self.raw_archive_errors),
                    report_file=archive_error_report,
                    explanation=(
                        "Esses ZIPs foram ignorados na busca. Um bruto ausente "
                        "pode estar dentro deles; nenhuma conclusao destrutiva "
                        "pode usar essa cobertura como completa."
                    ),
                )
            )

        registered_complete = status_counts["checked"] == status_counts["ok"]
        status = (
            "all_registered_local_hashes_match"
            if registered_complete and without_local_collections == 0
            else (
                "all_registered_hashes_match_with_nonlocal_collections"
                if registered_complete
                else "partial_or_with_issues"
            )
        )
        return {
            "status": status,
            **status_counts,
            "without_local_file": without_local_collections,
            "quotes_without_local_file": without_local_quotes,
            "without_local_report": without_local_report,
            "report_file": report,
            "unreadable_archives": len(self.raw_archive_errors),
            "archive_error_report": archive_error_report,
        }

    def _find_raw_candidates(
        self,
        stored_path: str,
        zip_cache: dict[Path, dict[str, list[tuple[Path, str]]]],
        file_cache: dict[Path, dict[str, list[Path]]],
    ) -> list[tuple[str, Path, str | None]]:
        raw_path = Path(stored_path)
        direct_path = (
            raw_path
            if raw_path.is_absolute()
            else self.project_root / raw_path
        )
        base_paths = [direct_path]

        if raw_path.is_absolute() and "data" in raw_path.parts:
            data_index = raw_path.parts.index("data")
            relocated = self.project_root.joinpath(*raw_path.parts[data_index:])
            if relocated != direct_path:
                base_paths.append(relocated)

        candidates: list[tuple[str, Path, str | None]] = []
        seen: set[tuple[str, Path, str | None]] = set()

        def add_candidate(
            candidate: tuple[str, Path, str | None],
        ) -> None:
            if candidate in seen:
                return
            seen.add(candidate)
            candidates.append(candidate)

        search_directories: set[Path] = set()
        expected_names: set[str] = set()
        for base_path in base_paths:
            expected_names.add(base_path.name)
            search_directories.add(base_path.parent)
            search_directories.add(
                base_path.parent.parent
                if base_path.parent.name == "old"
                else base_path.parent / "old"
            )

        for directory in sorted(search_directories):
            file_index = file_cache.get(directory)
            if file_index is None:
                file_index = {}
                if directory.is_dir():
                    for candidate_path in sorted(directory.iterdir()):
                        if not candidate_path.is_file():
                            continue
                        for lookup_name in raw_filename_lookup_keys(
                            candidate_path.name
                        ):
                            file_index.setdefault(lookup_name, []).append(
                                candidate_path
                            )
                file_cache[directory] = file_index

            for expected_name in expected_names:
                for candidate_path in file_index.get(expected_name, []):
                    add_candidate(("file", candidate_path, None))

            if directory.name != "old":
                continue

            archive_index = zip_cache.get(directory)
            if archive_index is None:
                archive_index = {}
                if directory.is_dir():
                    for archive_path in sorted(directory.glob("*.zip")):
                        try:
                            with zipfile.ZipFile(archive_path) as archive:
                                for member in archive.namelist():
                                    for lookup_name in raw_filename_lookup_keys(
                                        Path(member).name
                                    ):
                                        archive_index.setdefault(
                                            lookup_name,
                                            [],
                                        ).append((archive_path, member))
                        except (OSError, zipfile.BadZipFile) as error:
                            self.raw_archive_errors[archive_path.as_posix()] = (
                                f"{type(error).__name__}: {error}"
                            )
                zip_cache[directory] = archive_index

            for expected_name in expected_names:
                for archive_path, member in archive_index.get(
                    expected_name,
                    [],
                ):
                    add_candidate(("zip", archive_path, member))

        return candidates

    def _read_raw_candidate(
        self,
        candidate: tuple[str, Path, str | None],
    ) -> tuple[bytes | str, str]:
        candidate_type, path, member = candidate
        if candidate_type == "file":
            raw_bytes = path.read_bytes()
            suffix = path.suffix.lower()
        else:
            with zipfile.ZipFile(path) as archive:
                raw_bytes = archive.read(member or "")
            suffix = Path(member or "").suffix.lower()

        if suffix == ".pdf":
            return raw_bytes, hashlib.sha256(raw_bytes).hexdigest()

        content = raw_bytes.decode("utf-8")
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return normalized, digest

    def _match_raw_hash(
        self,
        candidates: Sequence[tuple[str, Path, str | None]],
        expected_hash: str,
        hash_cache: dict[tuple[str, Path, str | None], str],
    ) -> tuple[str, str | None, str | None]:
        first_path: str | None = None
        first_hash: str | None = None

        for candidate in candidates:
            candidate_type, path, member = candidate
            actual_hash = hash_cache.get(candidate)

            if actual_hash is None:
                _, actual_hash = self._read_raw_candidate(candidate)
                hash_cache[candidate] = actual_hash

            display_path = (
                path.as_posix()
                if member is None
                else f"{path.as_posix()}::{member}"
            )
            first_path = first_path or display_path
            first_hash = first_hash or actual_hash

            if actual_hash == expected_hash:
                return "ok", display_path, actual_hash

        return "hash_mismatch", first_path, first_hash

    def _add_group_finding(
        self,
        connection: sqlite3.Connection,
        code: str,
        title: str,
        severity: str,
        count_sql: str,
        detail_sql: str,
        report_name: str,
        explanation: str,
        params: Sequence[object] = (),
        limit_report: bool = True,
    ) -> None:
        row = connection.execute(count_sql, params).fetchone()
        groups = int(row[0]) if row else 0
        occurrences = int(row[1]) if row else 0

        if groups == 0:
            return

        report = (
            self._export_query(
                connection,
                report_name,
                detail_sql,
                params,
            )
            if limit_report
            else self._export_query_all(
                connection,
                report_name,
                detail_sql,
                params,
            )
        )
        self.findings.append(
            Finding(
                code=code,
                title=title,
                severity=severity,
                groups=groups,
                occurrences=occurrences,
                report_file=report,
                explanation=explanation,
            )
        )

    def _add_row_finding(
        self,
        connection: sqlite3.Connection,
        code: str,
        title: str,
        severity: str,
        count_sql: str,
        detail_sql: str,
        report_name: str,
        explanation: str,
        params: Sequence[object] = (),
    ) -> None:
        row = connection.execute(count_sql, params).fetchone()
        occurrences = int(row[0]) if row else 0

        if occurrences == 0:
            return

        report = self._export_query(
            connection,
            report_name,
            detail_sql,
            params,
        )
        self.findings.append(
            Finding(
                code=code,
                title=title,
                severity=severity,
                groups=occurrences,
                occurrences=occurrences,
                report_file=report,
                explanation=explanation,
            )
        )

    def _export_query(
        self,
        connection: sqlite3.Connection,
        file_name: str,
        query: str,
        params: Sequence[object] = (),
    ) -> str:
        cursor = connection.execute(
            f"{query.rstrip()} LIMIT ?",
            (*params, self.sample_limit),
        )
        headers = tuple(column[0] for column in cursor.description)
        return self._write_rows(file_name, headers, cursor)

    def _export_query_all(
        self,
        connection: sqlite3.Connection,
        file_name: str,
        query: str,
        params: Sequence[object] = (),
    ) -> str:
        cursor = connection.execute(query.rstrip(), params)
        headers = tuple(column[0] for column in cursor.description)
        return self._write_rows(file_name, headers, cursor)

    def _write_rows(
        self,
        file_name: str,
        headers: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> str:
        destination = self.output_directory / file_name
        temporary = destination.with_suffix(f"{destination.suffix}.tmp")

        try:
            with temporary.open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(headers)
                writer.writerows(rows)
            temporary.replace(destination)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

        self.generated_files.append(file_name)
        return file_name

    def _write_json(self, file_name: str, payload: dict[str, object]) -> None:
        destination = self.output_directory / file_name
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        write_text_atomic(destination, f"{content}\n")
        self.generated_files.append(file_name)

    def _write_markdown(
        self,
        file_name: str,
        result: dict[str, object],
    ) -> None:
        verdict = result["verdict"]
        structural = result["structural"]
        findings = result["findings"]
        table_counts = result["table_counts"]
        duplicate_summary = result["duplicate_summary"]
        raw_summary = result["raw_traceability"]
        reconciliation = result["raw_duplicate_reconciliation"]
        safety = result["safety"]
        lines = [
            "# Auditoria do banco de cotações",
            "",
            "## Resultado",
            "",
            f"- Status: **{verdict['status']}**",
            f"- Banco: `{result['database']['path']}`",
            f"- Tamanho: {result['database']['size_bytes']:,} bytes",
            f"- Duração: {result['duration_seconds']:.2f} segundos",
            (
                f"- Verificacao SQLite `{structural['check']}`: "
                f"**{structural['status']}**"
            ),
            (
                "- Exclusao automatica permitida: "
                f"**{'sim' if safety['automatic_deletion_allowed'] else 'nao'}**"
            ),
            (
                "- Rastreabilidade dos arquivos brutos: "
                f"**{raw_summary['status']}**"
            ),
            (
                "- Arquivos registrados verificados: "
                f"{raw_summary.get('ok', 0):,}/{raw_summary.get('checked', 0):,}"
            ),
            (
                "- Coletas sem bruto local: "
                f"{raw_summary.get('without_local_file', 0):,} "
                f"({raw_summary.get('quotes_without_local_file', 0):,} cotacoes)"
            ),
            (
                "- Reconciliacao das repeticoes com o pipeline atual: "
                f"**{reconciliation.get('status', 'not_checked')}**"
            ),
            (
                "- Grupos repetidos cobertos pela reconciliacao: "
                f"{reconciliation.get('duplicate_groups', 0):,}"
            ),
            "",
            str(verdict["explanation"]),
            "",
            "## Quantidades por tabela",
            "",
            "| Tabela | Registros |",
            "| --- | ---: |",
        ]

        for table_name, count in table_counts.items():
            lines.append(f"| `{table_name}` | {count:,} |")
        if duplicate_summary:
            lines.extend(
                [
                    "",
                    "## Repeticoes exatas pela regra logica atual, por fonte",
                    "",
                    (
                        "| Fonte | Grupos | Linhas envolvidas | Excedentes | "
                        "Mesmo bruto | Brutos diferentes | Ultima repeticao |"
                    ),
                    "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
                ]
            )
            for source in duplicate_summary:
                lines.append(
                    f"| {source['fonte']} | {source['grupos']:,} | "
                    f"{source['linhas_envolvidas']:,} | "
                    f"{source['registros_excedentes']:,} | "
                    f"{source['grupos_mesmo_bruto']:,} | "
                    f"{source['grupos_brutos_diferentes']:,} | "
                    f"{source['ultima_repeticao']} |"
                )


        lines.extend(
            [
                "",
                "## Achados",
                "",
                "| Severidade | Verificação | Grupos | Ocorrências | Relatório |",
                "| --- | --- | ---: | ---: | --- |",
            ]
        )

        if findings:
            for finding in findings:
                report = (
                    f"`{finding['report_file']}`"
                    if finding["report_file"]
                    else "-"
                )
                lines.append(
                    f"| {finding['severity']} | {finding['title']} | "
                    f"{finding['groups']:,} | {finding['occurrences']:,} | "
                    f"{report} |"
                )
        else:
            lines.append("| - | Nenhum problema encontrado | 0 | 0 | - |")

        lines.extend(["", "## Interpretação", ""])

        for finding in findings:
            lines.extend(
                [
                    f"### {finding['title']}",
                    "",
                    str(finding["explanation"]),
                    "",
                ]
            )

        lines.extend(
            [
                "## Limitações",
                "",
                "- A consistência interna não prova que a fonte publicou o valor correto.",
                (
                    "- A validação de hashes confirma apenas que o conteúdo encontrado "
                    "corresponde ao hash registrado no banco; ela não fornece uma "
                    "referência externa independente nem garante a interpretação do documento. "
                    "A reexecução dos parsers é limitada aos brutos envolvidos nas "
                    "duplicatas exatas e reflete a versão atual dos parsers."
                ),
                (
                    "- Variações de preço para a mesma identidade podem representar "
                    "correções legítimas e precisam ser comparadas com o arquivo bruto."
                ),
                (
                    "- Lacunas são medidas em dias corridos e podem incluir finais de "
                    "semana, feriados e calendários específicos de cada fonte."
                ),
                (
                    f"- Relatórios amostrais contêm no máximo {self.sample_limit:,} "
                    "exemplos. O manifesto de duplicatas, a cobertura por grupo e os "
                    "relatórios semânticos são integrais; as contagens sempre usam todos "
                    "os registros."
                ),
                "",
                "## Arquivos gerados",
                "",
            ]
        )

        for generated_file in sorted({*self.generated_files, file_name}):
            lines.append(f"- `{generated_file}`")

        write_text_atomic(
            self.output_directory / file_name,
            "\n".join(lines).rstrip() + "\n",
        )
        self.generated_files.append(file_name)

    def _build_result(
        self,
        started_at: datetime,
        finished_at: datetime,
        structural: dict[str, object],
        table_counts: dict[str, int],
        source_summary: list[dict[str, object]],
        raw_summary: dict[str, object],
    ) -> dict[str, object]:
        errors = [
            finding
            for finding in self.findings
            if finding.severity == "erro" and finding.occurrences > 0
        ]
        warnings = [
            finding
            for finding in self.findings
            if finding.severity == "alerta" and finding.occurrences > 0
        ]
        infos = [
            finding
            for finding in self.findings
            if finding.severity == "info" and finding.occurrences > 0
        ]

        if errors:
            status = "achados_de_erro_e_repeticoes_logicas"
            explanation = (
                "Existem achados classificados como erro, inclusive repeticoes "
                "pela regra logica atual. Isso nao significa que uma linha seja "
                "falsa nem autoriza remover registros."
            )
        elif warnings:
            status = "consistente_com_alertas"
            explanation = (
                "Nenhuma inconsistência estrutural foi encontrada, mas existem "
                "casos que precisam de comparação com as fontes originais."
            )
        else:
            status = "consistente_nos_testes_executados"
            explanation = (
                "Nenhum problema foi encontrado pelas verificações executadas. "
                "Isso não substitui a conferência externa com as fontes."
            )

        manifest_path = (
            self.output_directory / "manifesto_integral_duplicatas_exatas.csv"
        )
        manifest_artifact = (
            {
                "file": manifest_path.name,
                "size_bytes": manifest_path.stat().st_size,
                "sha256": hash_file_bytes(manifest_path),
            }
            if manifest_path.is_file()
            else None
        )
        generated_files = sorted(
            {
                *self.generated_files,
                "execucao.json",
                "resultado.json",
                "resumo.md",
            }
        )

        return {
            "schema_version": 3,
            "execution_status": "completed",
            "generated_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": (
                finished_at - started_at
            ).total_seconds(),
            "command": sys.argv,
            "database": {
                "path": self.database_path.as_posix(),
                "size_bytes": self.database_path.stat().st_size,
            },
            "parameters": {
                "sample_limit": self.sample_limit,
                "gap_days": self.gap_days,
                "verify_raw": self.verify_raw,
                "full_integrity_check": self.full_integrity_check,
                "semantic_analysis": not self.skip_semantic,
            },
            "verdict": {
                "status": status,
                "error_findings": len(errors),
                "warning_findings": len(warnings),
                "info_findings": len(infos),
                "error_occurrences": sum(item.occurrences for item in errors),
                "warning_occurrences": sum(
                    item.occurrences for item in warnings
                ),
                "info_occurrences": sum(item.occurrences for item in infos),
                "explanation": explanation,
            },
            "safety": {
                "audit_only": True,
                "database_opened_read_only": True,
                "automatic_deletion_allowed": False,
                "cleanup_readiness": "blocked",
                "reason": (
                    "O auditor apenas identifica e documenta ocorrencias; "
                    "nenhum artefato constitui plano de exclusao."
                ),
            },
            "structural": structural,
            "table_counts": table_counts,
            "source_summary": source_summary,
            "duplicate_summary": self.duplicate_summary,
            "raw_traceability": raw_summary,
            "raw_duplicate_reconciliation": self.raw_reconciliation_summary,
            "environment": self.environment,
            "artifacts": {
                "duplicate_manifest": manifest_artifact,
                "generated_files": generated_files,
            },
            "findings": [asdict(finding) for finding in self.findings],
            "limitations": [
                "internal_consistency_does_not_prove_external_truth",
                "raw_hash_does_not_validate_parser_semantics",
                "price_variations_require_source_review",
                "calendar_gaps_may_be_expected",
                "current_parser_replay_does_not_prove_historical_parser_output",
                "no_report_authorizes_deletion",
            ],
        }


def raw_filename_lookup_keys(file_name: str) -> tuple[str, ...]:
    path = Path(file_name)
    canonical_stem = re.sub(r"_[0-9]+$", "", path.stem)
    canonical_name = f"{canonical_stem}{path.suffix}"
    return (file_name,) if canonical_name == file_name else (file_name, canonical_name)


def structured_hash(*values: object | None) -> str:
    payload = [
        {
            "type": "null" if value is None else type(value).__name__,
            "value": None if value is None else str(value),
        }
        for value in values
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def hash_file_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_values(*values: object | None) -> str:
    raw_key = "|".join("" if value is None else str(value) for value in values)
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def decimal_key(value: object | None) -> str | None:
    if value is None:
        return None

    try:
        normalized = Decimal(str(value)).normalize()
    except (InvalidOperation, ValueError):
        return f"INVALID:{value}"

    return "0" if normalized == 0 else format(normalized, "f")


def canonical_text(value: object | None) -> str:
    if value is None:
        return ""

    decomposed = unicodedata.normalize("NFKD", str(value))
    without_accents = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(without_accents.casefold().split())


def basic_name(value: object | None) -> str:
    return " ".join(str(value or "").lower().split())


def content_signature(
    minimum: object | None,
    common: object | None,
    maximum: object | None,
    market_status: object | None,
) -> str:
    return "\x1f".join(
        (
            decimal_key(minimum) or "",
            decimal_key(common) or "",
            decimal_key(maximum) or "",
            canonical_text(market_status),
        )
    )


def is_valid_iso_date(value: object | None) -> int:
    if value is None:
        return 0

    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return 0

    return int(parsed.isoformat() == str(value))


def is_valid_sha256(value: object | None) -> int:
    return int(
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def hash_file(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        content = path.read_text(encoding="utf-8")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def hash_zip_member(archive_path: Path, member: str) -> str:
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member) as file:
            if Path(member).suffix.lower() != ".pdf":
                content = file.read().decode("utf-8")
                normalized = content.replace("\r\n", "\n").replace("\r", "\n")
                return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

            digest = hashlib.sha256()
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)

    return digest.hexdigest()


def write_text_atomic(destination: Path, content: str) -> None:
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")

    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audita o SQLite de cotacoes sem alterar o banco. Gera relatorios "
            "sobre integridade, duplicatas, chaves, valores e cobertura."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/cotacoes.sqlite"),
        help="Caminho do SQLite. Padrao: data/cotacoes.sqlite.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Diretorio novo para os relatorios. Por padrao, cria uma pasta "
            "datada dentro de auditoria_cotacoes/."
        ),
    )
    parser.add_argument(
        "--sample-limit",
        type=positive_integer,
        default=5000,
        help="Maximo de exemplos gravados em cada CSV. Padrao: 5000.",
    )
    parser.add_argument(
        "--gap-days",
        type=positive_integer,
        default=14,
        help="Quantidade minima de dias para registrar uma lacuna. Padrao: 14.",
    )
    parser.add_argument(
        "--verify-raw",
        action="store_true",
        help=(
            "Localiza arquivos brutos, inclusive dentro dos ZIPs de old, e "
            "confere o SHA-256 registrado nas coletas. Tambem reprocessa os "
            "brutos envolvidos em duplicatas exatas com os parsers atuais."
        ),
    )
    parser.add_argument(
        "--full-integrity-check",
        action="store_true",
        help="Executa PRAGMA integrity_check no lugar de quick_check.",
    )
    parser.add_argument(
        "--skip-semantic",
        action="store_true",
        help=(
            "Pula os agrupamentos normalizados mais pesados. As verificacoes "
            "de chaves e duplicatas oficiais continuam sendo executadas."
        ),
    )
    return parser


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("O valor deve ser maior que zero.")
    return parsed


def default_output_directory() -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return Path("auditoria_cotacoes") / timestamp


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output_directory = args.output or default_output_directory()
    auditor = DatabaseAuditor(
        database_path=args.database,
        output_directory=output_directory,
        sample_limit=args.sample_limit,
        gap_days=args.gap_days,
        verify_raw=args.verify_raw,
        full_integrity_check=args.full_integrity_check,
        skip_semantic=args.skip_semantic,
    )

    try:
        result = auditor.run()
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    print(f"Status: {result['verdict']['status']}")
    print(f"Relatorio: {output_directory.resolve() / 'resumo.md'}")
    print(f"Resultado JSON: {output_directory.resolve() / 'resultado.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
