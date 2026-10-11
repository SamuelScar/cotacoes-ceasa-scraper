import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cotacoes_ceasa.storage.sqlite import (
    HISTORICAL_PROVENANCE_MIGRATION,
    SQLITE_SCHEMA_VERSION,
)
from cotacoes_ceasa.storage.sqlite_v4 import require_legacy_sqlite_schema
from cotacoes_ceasa.workflows.ceasa_df_recovery import (
    CEASA_DF_RECOVERY_MIGRATION,
    CEASA_DF_RECOVERY_PLAN_SCHEMA_VERSION,
    ISSUE_6_HISTORICAL_FLOOR,
    build_recovery_summary_checks,
)


CEASA_DF_RECOVERY_REPORT_SCHEMA_VERSION = 1
RECOVERY_PROVENANCE_ORIGIN = "recuperacao_ceasa_df_issue6"
GENERIC_CLASSIFICATIONS = (
    "CAT-1 TP.150 A 175",
    "COMERCIAL - SOLTA",
)
CORRECTED_CLASSIFICATIONS = (
    "FUJI CAT-1 TP.150 A 175",
    "GALA CAT-1 TP.150 A 175",
    "FUJI COMERCIAL - SOLTA",
    "GALA COMERCIAL - SOLTA",
)


@dataclass(frozen=True)
class RecoverySnapshot:
    quotes: int
    max_quote_id: int
    provenance_rows: int
    generic_target_rows: int
    corrected_target_rows: int
    corrected_target_provenances: int
    recovered_provenance_rows: int
    recovered_origin_rows: int
    origin_quotes_covered: int
    quotes_without_origin_provenance: int
    migration_applied: bool
    migration_checkpoint: int | None
    migration_plan_sha256: str | None

    def to_dict(self) -> dict[str, int | str | bool | None]:
        return {
            "quotes": self.quotes,
            "max_quote_id": self.max_quote_id,
            "provenance_rows": self.provenance_rows,
            "generic_target_rows": self.generic_target_rows,
            "corrected_target_rows": self.corrected_target_rows,
            "corrected_target_provenances": self.corrected_target_provenances,
            "recovered_provenance_rows": self.recovered_provenance_rows,
            "recovered_origin_rows": self.recovered_origin_rows,
            "origin_quotes_covered": self.origin_quotes_covered,
            "quotes_without_origin_provenance": self.quotes_without_origin_provenance,
            "migration_applied": self.migration_applied,
            "migration_checkpoint": self.migration_checkpoint,
            "migration_plan_sha256": self.migration_plan_sha256,
        }


@dataclass(frozen=True)
class RecoveryCandidateResult:
    source_database_path: str
    candidate_database_path: str
    plan_path: str
    plan_sha256: str
    source_unchanged: bool
    candidate_created: bool
    migration_applied_now: bool
    quick_check: tuple[str, ...]
    foreign_key_violations: int
    before: RecoverySnapshot
    after: RecoverySnapshot
    expected_updates: int
    expected_logical_inserts: int
    expected_recovery_provenances: int

    @property
    def valid(self) -> bool:
        return (
            self.source_unchanged
            and self.quick_check == ("ok",)
            and self.foreign_key_violations == 0
            and self.after.quotes
            == self.before.quotes + self.expected_logical_inserts
            and self.after.provenance_rows
            == self.before.provenance_rows + self.expected_recovery_provenances
            and self.after.generic_target_rows == 0
            and self.after.corrected_target_rows
            == self.expected_updates + self.expected_logical_inserts
            and self.after.corrected_target_provenances
            == self.expected_updates + self.expected_recovery_provenances
            and self.after.recovered_provenance_rows
            == self.expected_recovery_provenances
            and self.after.recovered_origin_rows
            == self.expected_logical_inserts
            and self.after.quotes_without_origin_provenance == 0
            and self.after.origin_quotes_covered == self.after.quotes
            and self.after.migration_applied
            and self.after.migration_plan_sha256 == self.plan_sha256
            and self.after.migration_checkpoint == self.after.max_quote_id
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": CEASA_DF_RECOVERY_REPORT_SCHEMA_VERSION,
            "status": "valid" if self.valid else "invalid",
            "source_database_path": self.source_database_path,
            "candidate_database_path": self.candidate_database_path,
            "plan_path": self.plan_path,
            "plan_sha256": self.plan_sha256,
            "source_unchanged": self.source_unchanged,
            "candidate_created": self.candidate_created,
            "migration_applied_now": self.migration_applied_now,
            "quick_check": list(self.quick_check),
            "foreign_key_violations": self.foreign_key_violations,
            "expected": {
                "updates": self.expected_updates,
                "logical_inserts": self.expected_logical_inserts,
                "recovery_provenances": self.expected_recovery_provenances,
            },
            "safety": {
                "source_opened_read_only": True,
                "historical_quotes_deleted": 0,
                "historical_provenances_deleted": 0,
                "migration_key": CEASA_DF_RECOVERY_MIGRATION,
                "rollback": "descartar o SQLite candidato corrigido",
            },
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


def create_ceasa_df_recovery_candidate(
    source_database_path: Path,
    candidate_database_path: Path,
    plan_path: Path,
) -> RecoveryCandidateResult:
    source_database_path = source_database_path.resolve()
    candidate_database_path = candidate_database_path.resolve()
    plan_path = plan_path.resolve()

    if not source_database_path.is_file():
        raise FileNotFoundError(
            f"SQLite de proveniencia nao encontrado: {source_database_path}"
        )
    require_legacy_sqlite_schema(
        source_database_path,
        "A correcao historica da CEASA-DF",
    )
    if not plan_path.is_file():
        raise FileNotFoundError(f"Plano de recuperacao nao encontrado: {plan_path}")
    if source_database_path == candidate_database_path:
        raise ValueError("A candidata corrigida deve usar outro arquivo SQLite.")

    source_signature_before = _file_signature(source_database_path)
    plan, plan_sha256 = _load_and_validate_plan(
        plan_path,
        source_database_path,
    )
    before = analyze_recovery_state(source_database_path)
    _validate_source_state(before, plan)

    candidate_created = not candidate_database_path.exists()
    migration_applied_now = False

    if candidate_created:
        candidate_database_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = candidate_database_path.with_name(
            f"{candidate_database_path.name}.tmp"
        )
        if temporary_path.exists():
            raise FileExistsError(
                f"Arquivo temporario ja existe: {temporary_path}"
            )

        try:
            _backup_database(source_database_path, temporary_path)
            migration_applied_now = apply_ceasa_df_recovery_migration(
                temporary_path,
                plan,
                plan_sha256,
            )
            after = analyze_recovery_state(temporary_path)
            _validate_applied_state(temporary_path, plan, plan_sha256, before, after)
            quick_check, foreign_key_violations = _inspect_candidate(temporary_path)
            result = _build_result(
                source_database_path=source_database_path,
                candidate_database_path=candidate_database_path,
                plan_path=plan_path,
                plan_sha256=plan_sha256,
                source_signature_before=source_signature_before,
                candidate_created=True,
                migration_applied_now=migration_applied_now,
                quick_check=quick_check,
                foreign_key_violations=foreign_key_violations,
                before=before,
                after=after,
                plan=plan,
            )
            if not result.valid:
                raise RuntimeError(
                    "A candidata corrigida falhou na validacao estrutural."
                )
            temporary_path.replace(candidate_database_path)
            return result
        except Exception:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise

    existing_state = analyze_recovery_state(candidate_database_path)
    if not existing_state.migration_applied:
        raise FileExistsError(
            "A candidata ja existe sem o marcador da migracao; "
            "ela nao sera alterada automaticamente."
        )

    migration_applied_now = apply_ceasa_df_recovery_migration(
        candidate_database_path,
        plan,
        plan_sha256,
    )
    after = analyze_recovery_state(candidate_database_path)
    _validate_applied_state(
        candidate_database_path,
        plan,
        plan_sha256,
        before,
        after,
    )
    quick_check, foreign_key_violations = _inspect_candidate(
        candidate_database_path
    )
    result = _build_result(
        source_database_path=source_database_path,
        candidate_database_path=candidate_database_path,
        plan_path=plan_path,
        plan_sha256=plan_sha256,
        source_signature_before=source_signature_before,
        candidate_created=False,
        migration_applied_now=migration_applied_now,
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        before=before,
        after=after,
        plan=plan,
    )
    if not result.valid:
        raise RuntimeError("A candidata existente falhou na validacao estrutural.")
    return result


def apply_ceasa_df_recovery_migration(
    database_path: Path,
    plan: dict[str, object],
    plan_sha256: str,
) -> bool:
    registered_at = datetime.now().astimezone().isoformat(timespec="seconds")
    database_uri = f"{database_path.resolve().as_uri()}?mode=rw"

    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _configure_sqlite_memory(connection)

        updates = list(plan["updates"])
        logical_inserts = list(plan["logical_inserts"])
        recoveries = list(plan["recovery_occurrences"])
        _validate_plan_structure(updates, logical_inserts, recoveries)

        migration_row = connection.execute(
            """
            SELECT cotacao_id_maximo, detalhes
            FROM schema_migrations
            WHERE chave = ?
            """,
            (CEASA_DF_RECOVERY_MIGRATION,),
        ).fetchone()
        if migration_row is not None:
            details = _load_migration_details(str(migration_row["detalhes"]))
            if details.get("plan_sha256") != plan_sha256:
                raise RuntimeError(
                    "A candidata existente usa outro plano de recuperacao."
                )
            _validate_plan_rows(connection, plan, require_recoveries=True)
            return False

        source_quotes = _scalar(connection, "SELECT COUNT(*) FROM cotacoes")
        source_provenances = _scalar(
            connection,
            "SELECT COUNT(*) FROM cotacao_proveniencias",
        )
        source_max_quote_id = _scalar(
            connection,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        )
        _validate_migration_preconditions(
            connection,
            plan,
            source_max_quote_id,
        )

        for action in updates:
            cursor = connection.execute(
                """
                UPDATE cotacoes
                SET chave_unica = ?,
                    chave_identidade = ?,
                    classificacao = ?
                WHERE id = ?
                """,
                (
                    action["new_unique_key"],
                    action["new_identity_key"],
                    action["new_classification"],
                    int(action["quote_id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Cotacao nao atualizada: {action['quote_id']}."
                )

        recovery_by_content: dict[str, list[dict[str, object]]] = {}
        for recovery in recoveries:
            recovery_by_content.setdefault(
                str(recovery["new_content_key"]),
                [],
            ).append(recovery)

        for insertion in logical_inserts:
            content_key = str(insertion["new_content_key"])
            occurrences = recovery_by_content.get(content_key, [])
            if not occurrences:
                raise RuntimeError(
                    f"Insercao sem ocorrencia de recuperacao: {content_key}"
                )
            representative = next(
                item
                for item in occurrences
                if item["creates_logical_quote"] is True
            )
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
                SELECT
                    ?,
                    ?,
                    ?,
                    template.entreposto_id,
                    template.categoria_id,
                    template.produto_alias_id,
                    template.apresentacao_unidade_id,
                    ?,
                    ?,
                    ?,
                    ?,
                    template.procedencia,
                    ?,
                    ?,
                    template.fonte_complemento,
                    template.url_complemento,
                    template.data_complemento
                FROM cotacoes template
                WHERE template.id = ?
                """,
                (
                    content_key,
                    insertion["new_identity_key"],
                    int(representative["collection_id"]),
                    insertion["quote_date"],
                    insertion["price_min"],
                    insertion["price_common"],
                    insertion["price_max"],
                    insertion["new_classification"],
                    insertion["market_status"],
                    int(insertion["template_quote_id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Nao foi possivel inserir a cotacao recuperada {content_key}."
                )

        canonical_ids = _load_canonical_ids(connection, plan)

        for action in updates:
            target_id = int(action["new_canonical_quote_id"])
            cursor = connection.execute(
                """
                UPDATE cotacao_proveniencias
                SET cotacao_id = ?
                WHERE id = ?
                  AND chave_unica = ?
                  AND cotacao_id = ?
                  AND cotacao_origem_id = ?
                  AND chave_cotacao_origem = ?
                  AND origem_registro = ?
                """,
                (
                    target_id,
                    int(action["provenance_id"]),
                    action["provenance_key"],
                    int(action["old_canonical_quote_id"]),
                    int(action["provenance_origin_quote_id"]),
                    action["provenance_origin_quote_key"],
                    action["provenance_registration_origin"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "A proveniencia historica divergiu durante o remapeamento: "
                    f"{action['provenance_id']}."
                )

        for recovery in recoveries:
            content_key = str(recovery["new_content_key"])
            target_id = canonical_ids[content_key]
            template = connection.execute(
                """
                SELECT fonte_complemento, url_complemento, data_complemento
                FROM cotacoes
                WHERE id = ?
                """,
                (int(recovery["template_quote_id"]),),
            ).fetchone()
            if template is None:
                raise RuntimeError(
                    f"Cotacao modelo ausente: {recovery['template_quote_id']}."
                )
            cursor = connection.execute(
                """
                INSERT INTO cotacao_proveniencias (
                    chave_unica,
                    cotacao_id,
                    coleta_id,
                    cotacao_origem_id,
                    chave_cotacao_origem,
                    ordem_ocorrencia,
                    fonte_complemento,
                    url_complemento,
                    data_complemento,
                    registrada_em,
                    origem_registro
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recovery["provenance_key"],
                    target_id,
                    int(recovery["collection_id"]),
                    (
                        target_id
                        if recovery["creates_logical_quote"]
                        else None
                    ),
                    recovery["provenance_origin_quote_key"],
                    int(recovery["provenance_order"]),
                    template["fonte_complemento"],
                    template["url_complemento"],
                    template["data_complemento"],
                    registered_at,
                    recovery["provenance_registration_origin"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Proveniencia recuperada nao inserida: "
                    f"{recovery['provenance_key']}."
                )

        after_max_quote_id = _scalar(
            connection,
            "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
        )
        details = json.dumps(
            {
                "plan_sha256": plan_sha256,
                "source_quotes": source_quotes,
                "source_provenance_rows": source_provenances,
                "source_max_quote_id": source_max_quote_id,
                "updates": len(updates),
                "logical_inserts": len(logical_inserts),
                "recovery_provenances": len(recoveries),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO schema_migrations (
                chave,
                aplicada_em,
                cotacao_id_maximo,
                detalhes
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                CEASA_DF_RECOVERY_MIGRATION,
                registered_at,
                after_max_quote_id,
                details,
            ),
        )
        _validate_plan_rows(connection, plan, require_recoveries=True)

    return True


def analyze_recovery_state(database_path: Path) -> RecoverySnapshot:
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite nao encontrado: {database_path}")

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        marker = connection.execute(
            """
            SELECT cotacao_id_maximo, detalhes
            FROM schema_migrations
            WHERE chave = ?
            """,
            (CEASA_DF_RECOVERY_MIGRATION,),
        ).fetchone()
        details = (
            _load_migration_details(str(marker["detalhes"]))
            if marker is not None
            else {}
        )
        quotes = _scalar(connection, "SELECT COUNT(*) FROM cotacoes")
        origin_quotes_covered = _scalar(
            connection,
            """
            SELECT COUNT(DISTINCT cp.cotacao_origem_id)
            FROM cotacao_proveniencias cp
            JOIN cotacoes co ON co.id = cp.cotacao_origem_id
            """,
        )
        return RecoverySnapshot(
            quotes=quotes,
            max_quote_id=_scalar(
                connection,
                "SELECT COALESCE(MAX(id), 0) FROM cotacoes",
            ),
            provenance_rows=_scalar(
                connection,
                "SELECT COUNT(*) FROM cotacao_proveniencias",
            ),
            generic_target_rows=_target_quote_count(
                connection,
                GENERIC_CLASSIFICATIONS,
            ),
            corrected_target_rows=_target_quote_count(
                connection,
                CORRECTED_CLASSIFICATIONS,
            ),
            corrected_target_provenances=_target_provenance_count(connection),
            recovered_provenance_rows=int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM cotacao_proveniencias
                    WHERE origem_registro = ?
                    """,
                    (RECOVERY_PROVENANCE_ORIGIN,),
                ).fetchone()[0]
            ),
            recovered_origin_rows=int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM cotacao_proveniencias
                    WHERE origem_registro = ?
                      AND cotacao_origem_id IS NOT NULL
                    """,
                    (RECOVERY_PROVENANCE_ORIGIN,),
                ).fetchone()[0]
            ),
            origin_quotes_covered=origin_quotes_covered,
            quotes_without_origin_provenance=(
                quotes - origin_quotes_covered
            ),
            migration_applied=marker is not None,
            migration_checkpoint=(
                int(marker["cotacao_id_maximo"])
                if marker is not None
                else None
            ),
            migration_plan_sha256=(
                str(details["plan_sha256"])
                if details.get("plan_sha256") is not None
                else None
            ),
        )


def write_recovery_candidate_report(
    result: RecoveryCandidateResult,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination.with_name(f"{destination.name}.tmp")
    content = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    try:
        temporary_path.write_text(f"{content}\n", encoding="utf-8")
        temporary_path.replace(destination)
    except OSError:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _load_and_validate_plan(
    plan_path: Path,
    source_database_path: Path,
) -> tuple[dict[str, object], str]:
    plan_bytes = plan_path.read_bytes()
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    plan = json.loads(plan_bytes.decode("utf-8"))

    if plan.get("schema_version") != CEASA_DF_RECOVERY_PLAN_SCHEMA_VERSION:
        raise RuntimeError("Versao inesperada do plano de recuperacao.")
    if plan.get("status") != "valid":
        raise RuntimeError("O plano de recuperacao nao possui status valido.")
    if plan.get("migration_key") != CEASA_DF_RECOVERY_MIGRATION:
        raise RuntimeError("O plano usa outra chave de migracao.")
    checks = plan.get("checks")
    if not isinstance(checks, dict) or not checks or not all(checks.values()):
        raise RuntimeError("O plano possui verificacoes incompletas.")
    summary = plan.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError("O plano nao possui resumo valido.")
    if plan.get("historical_floor") != ISSUE_6_HISTORICAL_FLOOR:
        raise RuntimeError("O plano nao registra o piso historico da issue 6.")
    summary_checks = build_recovery_summary_checks(summary)
    failed_summary_checks = sorted(
        key for key, passed in summary_checks.items() if not passed
    )
    if failed_summary_checks:
        raise RuntimeError(
            "Resumo inconsistente no plano de recuperacao: "
            + ", ".join(failed_summary_checks)
        )
    if any(
        checks.get(key) != passed
        for key, passed in summary_checks.items()
    ):
        raise RuntimeError(
            "As verificacoes registradas divergem do resumo do plano."
        )

    updates = plan.get("updates")
    logical_inserts = plan.get("logical_inserts")
    recoveries = plan.get("recovery_occurrences")
    if not isinstance(updates, list) or len(updates) != summary["rows_to_update"]:
        raise RuntimeError("Lista de atualizacoes divergente no plano.")
    if (
        not isinstance(logical_inserts, list)
        or len(logical_inserts) != summary["logical_inserts"]
    ):
        raise RuntimeError("Lista de insercoes divergente no plano.")
    if (
        not isinstance(recoveries, list)
        or len(recoveries) != summary["recovered_occurrences"]
    ):
        raise RuntimeError("Lista de proveniencias recuperadas divergente.")
    _validate_plan_structure(updates, logical_inserts, recoveries)
    derived_summary = {
        "existing_provenance_reassignments": sum(
            int(item["old_canonical_quote_id"])
            != int(item["new_canonical_quote_id"])
            for item in updates
        ),
        "recovery_provenance_rows": len(recoveries),
        "canonical_key_updates": sum(
            item["new_unique_key"] != item["old_unique_key"]
            for item in updates
        ),
        "legacy_duplicate_keys_preserved_until_baseline": sum(
            item["new_unique_key"] == item["old_unique_key"]
            for item in updates
        ),
    }
    for key, expected in derived_summary.items():
        if summary.get(key) != expected:
            raise RuntimeError(f"Total derivado inesperado no plano: {key}.")

    source = plan.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("Assinatura do SQLite ausente no plano.")
    stat = source_database_path.stat()
    if (
        source.get("size_bytes") != stat.st_size
        or source.get("mtime_ns") != stat.st_mtime_ns
    ):
        raise RuntimeError(
            "O SQLite de proveniencia divergiu desde a geracao do plano."
        )

    return plan, plan_sha256


def _validate_plan_structure(
    updates: list[dict[str, object]],
    logical_inserts: list[dict[str, object]],
    recoveries: list[dict[str, object]],
) -> None:
    if not all(isinstance(item, dict) for item in updates):
        raise RuntimeError("O plano possui atualizacoes invalidas.")
    if not all(isinstance(item, dict) for item in logical_inserts):
        raise RuntimeError("O plano possui insercoes logicas invalidas.")
    if not all(isinstance(item, dict) for item in recoveries):
        raise RuntimeError("O plano possui proveniencias recuperadas invalidas.")

    update_quote_ids = [int(item["quote_id"]) for item in updates]
    update_provenance_ids = [int(item["provenance_id"]) for item in updates]
    if len(set(update_quote_ids)) != len(update_quote_ids):
        raise RuntimeError("O plano repete IDs de cotacoes a atualizar.")
    if len(set(update_provenance_ids)) != len(update_provenance_ids):
        raise RuntimeError("O plano repete IDs de proveniencias historicas.")
    if any(item.get("action") != "update_existing" for item in updates):
        raise RuntimeError("O plano possui acao de atualizacao inesperada.")
    for item in updates:
        if (
            item.get("old_classification") not in GENERIC_CLASSIFICATIONS
            or item.get("new_classification") not in CORRECTED_CLASSIFICATIONS
            or item.get("creates_logical_quote") is not False
            or int(item["template_quote_id"]) != int(item["quote_id"])
            or item.get("provenance_registration_origin")
            != "migracao_historica"
        ):
            raise RuntimeError(
                f"Atualizacao fora do escopo CEASA-DF: {item['quote_id']}."
            )

    updates_by_content: dict[str, list[dict[str, object]]] = {}
    for item in updates:
        content_key = str(item["new_content_key"])
        updates_by_content.setdefault(content_key, []).append(item)

    for content_key, group in updates_by_content.items():
        canonical_ids = {
            int(item["new_canonical_quote_id"]) for item in group
        }
        canonical_rows = [item for item in group if item["is_canonical"] is True]
        if len(canonical_ids) != 1 or len(canonical_rows) != 1:
            raise RuntimeError(
                f"Grupo corrigido sem canonica unica: {content_key}."
            )
        canonical_id = next(iter(canonical_ids))
        if int(canonical_rows[0]["quote_id"]) != canonical_id:
            raise RuntimeError(
                f"Canonica corrigida inconsistente: {content_key}."
            )
        for item in group:
            expected_unique_key = (
                content_key
                if int(item["quote_id"]) == canonical_id
                else str(item["old_unique_key"])
            )
            if str(item["new_unique_key"]) != expected_unique_key:
                raise RuntimeError(
                    f"Chave corrigida inconsistente: {item['quote_id']}."
                )

    insertion_keys = [str(item["new_content_key"]) for item in logical_inserts]
    if len(set(insertion_keys)) != len(insertion_keys):
        raise RuntimeError("O plano repete chaves de insercoes logicas.")
    if set(insertion_keys) & set(updates_by_content):
        raise RuntimeError(
            "Uma insercao logica coincide com uma cotacao ja armazenada."
        )

    provenance_keys = [str(item["provenance_key"]) for item in recoveries]
    if len(set(provenance_keys)) != len(provenance_keys):
        raise RuntimeError("O plano repete chaves de proveniencias recuperadas.")

    update_quote_id_set = set(update_quote_ids)
    recoveries_by_content: dict[str, list[dict[str, object]]] = {}
    for item in recoveries:
        content_key = str(item["new_content_key"])
        if item.get("action") != "recover_missing":
            raise RuntimeError("O plano possui acao de recuperacao inesperada.")
        if int(item["template_quote_id"]) not in update_quote_id_set:
            raise RuntimeError(
                f"Cotacao modelo fora das atualizacoes: {item['template_quote_id']}."
            )
        if (
            item.get("provenance_registration_origin")
            != RECOVERY_PROVENANCE_ORIGIN
            or item.get("old_classification") not in GENERIC_CLASSIFICATIONS
            or item.get("new_classification") not in CORRECTED_CLASSIFICATIONS
            or item.get("quote_id") is not None
            or item.get("provenance_id") is not None
            or item.get("old_unique_key") is not None
            or item.get("old_canonical_quote_id") is not None
            or item.get("new_canonical_quote_id") is not None
            or item.get("is_canonical") is not True
            or not str(item["provenance_key"]).startswith("issue6-ceasa-df:")
            or item.get("provenance_origin_quote_id") is not None
            or item.get("provenance_origin_quote_key") != content_key
            or item.get("new_unique_key") != content_key
        ):
            raise RuntimeError(
                f"Proveniencia recuperada inconsistente: {item['provenance_key']}."
            )
        recoveries_by_content.setdefault(content_key, []).append(item)

    if set(recoveries_by_content) != set(insertion_keys):
        raise RuntimeError(
            "As recuperacoes nao correspondem as insercoes logicas do plano."
        )

    for insertion in logical_inserts:
        content_key = str(insertion["new_content_key"])
        group = recoveries_by_content[content_key]
        collection_ids = sorted(int(item["collection_id"]) for item in group)
        expected_collection_ids = sorted(
            int(value) for value in insertion["collection_ids"]
        )
        creators = [
            item for item in group if item["creates_logical_quote"] is True
        ]
        if (
            insertion.get("new_classification")
            not in CORRECTED_CLASSIFICATIONS
            or insertion.get("new_content_key") != content_key
            or any(
                int(item["provenance_order"]) < 1 for item in group
            )
            or int(insertion["template_quote_id"]) not in update_quote_id_set
            or len(creators) != 1
            or int(creators[0]["template_quote_id"])
            != int(insertion["template_quote_id"])
            or int(insertion["occurrence_count"]) != len(group)
            or collection_ids != expected_collection_ids
        ):
            raise RuntimeError(
                f"Insercao logica inconsistente: {content_key}."
            )
        for item in group:
            if (
                item.get("new_identity_key") != insertion["new_identity_key"]
                or item.get("new_classification")
                != insertion["new_classification"]
                or item.get("quote_date") != insertion["quote_date"]
                or not _decimal_equal(item.get("price_min"), insertion["price_min"])
                or not _decimal_equal(
                    item.get("price_common"), insertion["price_common"]
                )
                or not _decimal_equal(item.get("price_max"), insertion["price_max"])
                or item.get("market_status") != insertion["market_status"]
            ):
                raise RuntimeError(
                    f"Ocorrencia recuperada diverge da insercao: {content_key}."
                )



def _validate_source_state(
    snapshot: RecoverySnapshot,
    plan: dict[str, object],
) -> None:
    source = plan["source"]
    if snapshot.migration_applied:
        raise RuntimeError(
            "O banco de origem ja possui a migracao da CEASA-DF."
        )
    if snapshot.quotes_without_origin_provenance != 0:
        raise RuntimeError(
            "O banco de origem possui cotacoes sem proveniencia."
        )
    if snapshot.max_quote_id != int(source["max_quote_id"]):
        raise RuntimeError("O maior ID do banco de origem divergiu do plano.")
    if snapshot.generic_target_rows != int(
        plan["summary"]["stored_target_rows"]
    ):
        raise RuntimeError("As linhas genericas de origem divergiram do plano.")
    if snapshot.corrected_target_rows != 0:
        raise RuntimeError(
            "O banco de origem ja possui classificacoes corrigidas inesperadas."
        )


def _validate_migration_preconditions(
    connection: sqlite3.Connection,
    plan: dict[str, object],
    source_max_quote_id: int,
) -> None:
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if schema_version != SQLITE_SCHEMA_VERSION:
        raise RuntimeError(
            f"Schema SQLite inesperado: {schema_version}; "
            f"esperado {SQLITE_SCHEMA_VERSION}."
        )
    historical_marker = connection.execute(
        """
        SELECT cotacao_id_maximo
        FROM schema_migrations
        WHERE chave = ?
        """,
        (HISTORICAL_PROVENANCE_MIGRATION,),
    ).fetchone()
    if (
        historical_marker is None
        or int(historical_marker[0]) != source_max_quote_id
    ):
        raise RuntimeError(
            "A migracao de proveniencia historica nao esta completa."
        )

    _validate_plan_rows(connection, plan, require_recoveries=False)

    desired_keys = {
        str(action["new_unique_key"])
        for action in plan["updates"]
        if action["new_unique_key"] != action["old_unique_key"]
    }
    desired_keys.update(
        str(insertion["new_content_key"])
        for insertion in plan["logical_inserts"]
    )
    for chunk in _chunks(sorted(desired_keys), 900):
        placeholders = ", ".join("?" for _ in chunk)
        row = connection.execute(
            f"""
            SELECT id, chave_unica
            FROM cotacoes
            WHERE chave_unica IN ({placeholders})
            LIMIT 1
            """,
            chunk,
        ).fetchone()
        if row is not None:
            raise RuntimeError(
                "Uma chave corrigida ja existe antes da migracao: "
                f"{row['chave_unica']}."
            )

    recovery_keys = sorted(
        str(item["provenance_key"])
        for item in plan["recovery_occurrences"]
    )
    for chunk in _chunks(recovery_keys, 900):
        placeholders = ", ".join("?" for _ in chunk)
        if connection.execute(
            f"""
            SELECT 1
            FROM cotacao_proveniencias
            WHERE chave_unica IN ({placeholders})
            LIMIT 1
            """,
            chunk,
        ).fetchone():
            raise RuntimeError(
                "Uma proveniencia recuperada ja existe antes da migracao."
            )


def _validate_plan_rows(
    connection: sqlite3.Connection,
    plan: dict[str, object],
    require_recoveries: bool,
) -> None:
    for action in plan["updates"]:
        row = connection.execute(
            """
            SELECT
                co.chave_unica,
                co.chave_identidade,
                co.coleta_id,
                co.data_cotacao,
                co.preco_minimo,
                co.preco_comum,
                co.preco_maximo,
                co.classificacao,
                co.situacao_mercado,
                ce.slug AS source_slug,
                p.nome_normalizado AS product_name
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            WHERE co.id = ?
            """,
            (int(action["quote_id"]),),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Cotacao ausente: {action['quote_id']}.")

        expected_unique_key = (
            action["new_unique_key"]
            if require_recoveries
            else action["old_unique_key"]
        )
        expected_identity_key = (
            action["new_identity_key"]
            if require_recoveries
            else action["old_identity_key"]
        )
        expected_classification = (
            action["new_classification"]
            if require_recoveries
            else action["old_classification"]
        )
        if (
            str(row["chave_unica"]) != expected_unique_key
            or str(row["chave_identidade"]) != expected_identity_key
            or row["source_slug"] != "ceasa-df"
            or row["product_name"] != "maçã"
            or int(row["coleta_id"]) != int(action["collection_id"])
            or str(row["data_cotacao"]) != action["quote_date"]
            or not _decimal_equal(row["preco_minimo"], action["price_min"])
            or not _decimal_equal(row["preco_comum"], action["price_common"])
            or not _decimal_equal(row["preco_maximo"], action["price_max"])
            or row["classificacao"] != expected_classification
            or row["situacao_mercado"] != action["market_status"]
        ):
            raise RuntimeError(
                f"A cotacao {action['quote_id']} divergiu do plano."
            )

        provenance = connection.execute(
            """
            SELECT
                chave_unica,
                cotacao_id,
                cotacao_origem_id,
                chave_cotacao_origem,
                origem_registro
            FROM cotacao_proveniencias
            WHERE id = ?
            """,
            (int(action["provenance_id"]),),
        ).fetchone()
        expected_canonical_id = (
            int(action["new_canonical_quote_id"])
            if require_recoveries
            else int(action["old_canonical_quote_id"])
        )
        if (
            provenance is None
            or provenance["chave_unica"] != action["provenance_key"]
            or int(provenance["cotacao_id"]) != expected_canonical_id
            or int(provenance["cotacao_origem_id"])
            != int(action["provenance_origin_quote_id"])
            or provenance["chave_cotacao_origem"]
            != action["provenance_origin_quote_key"]
            or provenance["origem_registro"]
            != action["provenance_registration_origin"]
        ):
            raise RuntimeError(
                f"A proveniencia {action['provenance_id']} divergiu do plano."
            )

    if not require_recoveries:
        return

    canonical_ids = _load_canonical_ids(connection, plan)
    for insertion in plan["logical_inserts"]:
        content_key = str(insertion["new_content_key"])
        row = connection.execute(
            """
            SELECT
                id,
                chave_identidade,
                coleta_id,
                data_cotacao,
                preco_minimo,
                preco_comum,
                preco_maximo,
                classificacao,
                situacao_mercado
            FROM cotacoes
            WHERE chave_unica = ?
            """,
            (content_key,),
        ).fetchone()
        if (
            row is None
            or int(row["id"]) != canonical_ids[content_key]
            or row["chave_identidade"] != insertion["new_identity_key"]
            or int(row["coleta_id"]) not in {
                int(value) for value in insertion["collection_ids"]
            }
            or str(row["data_cotacao"]) != insertion["quote_date"]
            or not _decimal_equal(row["preco_minimo"], insertion["price_min"])
            or not _decimal_equal(row["preco_comum"], insertion["price_common"])
            or not _decimal_equal(row["preco_maximo"], insertion["price_max"])
            or row["classificacao"] != insertion["new_classification"]
            or row["situacao_mercado"] != insertion["market_status"]
        ):
            raise RuntimeError(
                f"A cotacao recuperada {content_key} divergiu do plano."
            )

    for recovery in plan["recovery_occurrences"]:
        content_key = str(recovery["new_content_key"])
        expected_origin_id = (
            canonical_ids[content_key]
            if recovery["creates_logical_quote"]
            else None
        )
        row = connection.execute(
            """
            SELECT
                cotacao_id,
                coleta_id,
                cotacao_origem_id,
                chave_cotacao_origem,
                ordem_ocorrencia,
                origem_registro
            FROM cotacao_proveniencias
            WHERE chave_unica = ?
            """,
            (recovery["provenance_key"],),
        ).fetchone()
        if (
            row is None
            or int(row["cotacao_id"]) != canonical_ids[content_key]
            or int(row["coleta_id"]) != int(recovery["collection_id"])
            or row["cotacao_origem_id"] != expected_origin_id
            or row["chave_cotacao_origem"]
            != recovery["provenance_origin_quote_key"]
            or int(row["ordem_ocorrencia"])
            != int(recovery["provenance_order"])
            or row["origem_registro"]
            != recovery["provenance_registration_origin"]
        ):
            raise RuntimeError(
                "A proveniencia recuperada divergiu do plano: "
                f"{recovery['provenance_key']}."
            )


def _validate_applied_state(
    database_path: Path,
    plan: dict[str, object],
    plan_sha256: str,
    before: RecoverySnapshot,
    after: RecoverySnapshot,
) -> None:
    expected_updates = int(plan["summary"]["rows_to_update"])
    expected_inserts = int(plan["summary"]["logical_inserts"])
    expected_recoveries = int(plan["summary"]["recovery_provenance_rows"])
    if (
        after.quotes != before.quotes + expected_inserts
        or after.provenance_rows
        != before.provenance_rows + expected_recoveries
        or after.generic_target_rows != 0
        or after.corrected_target_rows != expected_updates + expected_inserts
        or after.corrected_target_provenances
        != expected_updates + expected_recoveries
        or after.recovered_provenance_rows != expected_recoveries
        or after.recovered_origin_rows != expected_inserts
        or after.quotes_without_origin_provenance != 0
        or after.origin_quotes_covered != after.quotes
        or not after.migration_applied
        or after.migration_plan_sha256 != plan_sha256
        or after.migration_checkpoint != after.max_quote_id
    ):
        raise RuntimeError(
            "Os totais da candidata corrigida divergiram do plano."
        )

    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        _validate_recovery_marker(
            connection,
            plan,
            plan_sha256,
            before,
            after,
        )
        _validate_plan_rows(connection, plan, require_recoveries=True)


def _validate_recovery_marker(
    connection: sqlite3.Connection,
    plan: dict[str, object],
    plan_sha256: str,
    before: RecoverySnapshot,
    after: RecoverySnapshot,
) -> None:
    marker = connection.execute(
        """
        SELECT cotacao_id_maximo, detalhes
        FROM schema_migrations
        WHERE chave = ?
        """,
        (CEASA_DF_RECOVERY_MIGRATION,),
    ).fetchone()
    if marker is None or int(marker["cotacao_id_maximo"]) != after.max_quote_id:
        raise RuntimeError("O marcador da migracao CEASA-DF esta incompleto.")

    details = _load_migration_details(str(marker["detalhes"]))
    expected_details = {
        "plan_sha256": plan_sha256,
        "source_quotes": before.quotes,
        "source_provenance_rows": before.provenance_rows,
        "source_max_quote_id": before.max_quote_id,
        "updates": int(plan["summary"]["rows_to_update"]),
        "logical_inserts": int(plan["summary"]["logical_inserts"]),
        "recovery_provenances": int(
            plan["summary"]["recovery_provenance_rows"]
        ),
    }
    if details != expected_details:
        raise RuntimeError("Os detalhes do marcador CEASA-DF divergem do plano.")



def _load_canonical_ids(
    connection: sqlite3.Connection,
    plan: dict[str, object],
) -> dict[str, int]:
    content_keys = {
        str(action["new_content_key"]) for action in plan["updates"]
    }
    content_keys.update(
        str(item["new_content_key"])
        for item in plan["recovery_occurrences"]
    )
    canonical_ids: dict[str, int] = {}
    for chunk in _chunks(sorted(content_keys), 900):
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT chave_unica, id
            FROM cotacoes
            WHERE chave_unica IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        canonical_ids.update(
            {str(row["chave_unica"]): int(row["id"]) for row in rows}
        )
    missing = content_keys - set(canonical_ids)
    if missing:
        raise RuntimeError(
            f"Faltam {len(missing)} cotacoes canonicas corrigidas."
        )
    return canonical_ids


def _target_quote_count(
    connection: sqlite3.Connection,
    classifications: tuple[str, ...],
) -> int:
    placeholders = ", ".join("?" for _ in classifications)
    return int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            WHERE ce.slug = 'ceasa-df'
              AND p.nome_normalizado = 'maçã'
              AND co.classificacao IN ({placeholders})
            """,
            classifications,
        ).fetchone()[0]
    )


def _target_provenance_count(connection: sqlite3.Connection) -> int:
    placeholders = ", ".join("?" for _ in CORRECTED_CLASSIFICATIONS)
    return int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM cotacao_proveniencias cp
            JOIN cotacoes co ON co.id = cp.cotacao_id
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            JOIN produtos p ON p.id = pa.produto_id
            WHERE ce.slug = 'ceasa-df'
              AND p.nome_normalizado = 'maçã'
              AND co.classificacao IN ({placeholders})
            """,
            CORRECTED_CLASSIFICATIONS,
        ).fetchone()[0]
    )


def _build_result(
    source_database_path: Path,
    candidate_database_path: Path,
    plan_path: Path,
    plan_sha256: str,
    source_signature_before: tuple[int, int],
    candidate_created: bool,
    migration_applied_now: bool,
    quick_check: tuple[str, ...],
    foreign_key_violations: int,
    before: RecoverySnapshot,
    after: RecoverySnapshot,
    plan: dict[str, object],
) -> RecoveryCandidateResult:
    return RecoveryCandidateResult(
        source_database_path=source_database_path.as_posix(),
        candidate_database_path=candidate_database_path.as_posix(),
        plan_path=plan_path.as_posix(),
        plan_sha256=plan_sha256,
        source_unchanged=(
            source_signature_before == _file_signature(source_database_path)
        ),
        candidate_created=candidate_created,
        migration_applied_now=migration_applied_now,
        quick_check=quick_check,
        foreign_key_violations=foreign_key_violations,
        before=before,
        after=after,
        expected_updates=int(plan["summary"]["rows_to_update"]),
        expected_logical_inserts=int(plan["summary"]["logical_inserts"]),
        expected_recovery_provenances=int(
            plan["summary"]["recovery_provenance_rows"]
        ),
    )


def _load_migration_details(value: str) -> dict[str, object]:
    try:
        details = json.loads(value)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Detalhes invalidos no marcador da migracao CEASA-DF."
        ) from error
    if not isinstance(details, dict):
        raise RuntimeError(
            "Detalhes invalidos no marcador da migracao CEASA-DF."
        )
    return details


def _decimal_equal(left: object | None, right: object | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return Decimal(str(left)) == Decimal(str(right))


def _configure_sqlite_memory(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA temp_store = FILE")
    connection.execute("PRAGMA cache_size = -65536")


def _file_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _backup_database(source_path: Path, destination_path: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True) as source,
        sqlite3.connect(destination_path) as destination,
    ):
        source.backup(destination)


def _inspect_candidate(database_path: Path) -> tuple[tuple[str, ...], int]:
    database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        quick_check = tuple(
            str(row[0]) for row in connection.execute("PRAGMA quick_check")
        )
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
    return quick_check, foreign_key_violations


def _scalar(connection: sqlite3.Connection, query: str) -> int:
    return int(connection.execute(query).fetchone()[0])


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [
        values[index : index + size]
        for index in range(0, len(values), size)
    ]
