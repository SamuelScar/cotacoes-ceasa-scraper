import csv
import io
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import Request, urlopen

from cotacoes_ceasa.core.models import Cotacao
from cotacoes_ceasa.storage.sqlite import SQLiteStorage


PROHORT_URL = (
    "https://portaldeinformacoes.conab.gov.br/downloads/arquivos/"
    "ProhortDiario.txt"
)

SOURCE_CEASA_NAMES = {
    "ceasa-pe": "CEASA/PE - RECIFE",
    "ceasa-campinas": "CEASA/SP - CAMPINAS",
}

CEASA_PR_CATEGORIES = {
    "curitiba": "CEASA/PR - CURITIBA",
    "cascavel": "CEASA/PR - CASCAVEL",
    "foz-do-iguacu": "CEASA/PR - FOZ DO IGUACU",
    "londrina": "CEASA/PR - LONDRINA",
    "maringa": "CEASA/PR - MARINGA",
}

CEASA_MG_PROCEDENCIAS = {
    "grande bh": "CEASAMINAS - BELO HORIZONTE",
    "barbacena": "CEASAMINAS - BARBACENA",
}


@dataclass(frozen=True)
class ProhortComplementResult:
    database_found: bool
    candidate_count: int = 0
    fallback_scope_count: int = 0
    unmapped_count: int = 0
    ambiguous_count: int = 0
    scanned_rows: int = 0
    matched_rows: int = 0
    updated_count: int = 0
    inserted_count: int = 0


@dataclass(frozen=True)
class _SavedCotacao:
    id: int
    ceasa_id: int
    source_slug: str
    market_slug: str | None
    category_slug: str
    product_name: str
    unit: str | None
    quote_date: date
    procedencia: str | None
    preco_comum: str | None


@dataclass(frozen=True)
class _FallbackDestination:
    ceasa_id: int
    source_slug: str
    market_slug: str | None
    category_slug: str
    procedencia: str | None


class ProhortComplementer:
    """Complementa cotacoes ja salvas usando o PROHORT como fonte secundaria."""

    def __init__(
        self,
        database_path: Path,
        prohort_url: str = PROHORT_URL,
        timeout_seconds: int = 120,
    ) -> None:
        self.database_path = database_path
        self.prohort_url = prohort_url
        self.timeout_seconds = timeout_seconds

    def complement(self) -> ProhortComplementResult:
        if not self.database_path.exists():
            return ProhortComplementResult(database_found=False)

        SQLiteStorage(self.database_path).ensure_schema()

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            saved_cotacoes = self._fetch_saved_cotacoes(connection)
            targets = [
                cotacao
                for cotacao in saved_cotacoes
                if cotacao.preco_comum is None
            ]
            existing_keys, fallback_destinations = self._build_fallback_indexes(
                saved_cotacoes
            )

            if not targets and not fallback_destinations:
                return ProhortComplementResult(
                    database_found=True,
                    candidate_count=0,
                )

            targets_by_key, unmapped_count, ambiguous_count = self._index_targets(
                targets
            )

            if not targets_by_key and not fallback_destinations:
                return ProhortComplementResult(
                    database_found=True,
                    candidate_count=len(targets),
                    fallback_scope_count=len(fallback_destinations),
                    unmapped_count=unmapped_count,
                    ambiguous_count=ambiguous_count,
                )

            (
                scanned_rows,
                matched_rows,
                updated_count,
                inserted_count,
            ) = self._apply_prohort_rows(
                connection,
                targets_by_key,
                existing_keys,
                fallback_destinations,
            )

            return ProhortComplementResult(
                database_found=True,
                candidate_count=len(targets),
                fallback_scope_count=len(fallback_destinations),
                unmapped_count=unmapped_count,
                ambiguous_count=ambiguous_count,
                scanned_rows=scanned_rows,
                matched_rows=matched_rows,
                updated_count=updated_count,
                inserted_count=inserted_count,
            )

    def _fetch_saved_cotacoes(
        self,
        connection: sqlite3.Connection,
    ) -> list[_SavedCotacao]:
        rows = connection.execute(
            """
            SELECT
                co.id,
                col.ceasa_id,
                ce.slug,
                en.slug,
                ca.slug,
                pa.nome_original,
                COALESCE(au.unidade_original, u.sigla),
                co.data_cotacao,
                co.procedencia,
                co.preco_comum
            FROM cotacoes co
            JOIN coletas col ON col.id = co.coleta_id
            JOIN ceasas ce ON ce.id = col.ceasa_id
            LEFT JOIN entrepostos en ON en.id = co.entreposto_id
            JOIN categorias ca ON ca.id = co.categoria_id
            JOIN produto_aliases pa ON pa.id = co.produto_alias_id
            LEFT JOIN apresentacoes_unidade au
                ON au.id = co.apresentacao_unidade_id
            LEFT JOIN unidades u ON u.id = au.unidade_id
            WHERE co.data_cotacao IS NOT NULL
            """
        ).fetchall()

        return [
            _SavedCotacao(
                id=int(row[0]),
                ceasa_id=int(row[1]),
                source_slug=row[2],
                market_slug=row[3],
                category_slug=row[4],
                product_name=row[5],
                unit=row[6],
                quote_date=date.fromisoformat(row[7]),
                procedencia=row[8],
                preco_comum=row[9],
            )
            for row in rows
        ]

    def _index_targets(
        self,
        targets: list[_SavedCotacao],
    ) -> tuple[dict[tuple[str, str, str, str], int], int, int]:
        grouped_targets: dict[tuple[str, str, str, str], list[int]] = {}
        unmapped_count = 0

        for target in targets:
            key = self._build_target_key(target)

            if key is None:
                unmapped_count += 1
                continue

            grouped_targets.setdefault(key, []).append(target.id)

        indexed_targets: dict[tuple[str, str, str, str], int] = {}
        ambiguous_count = 0

        for key, target_ids in grouped_targets.items():
            if len(target_ids) > 1:
                ambiguous_count += len(target_ids)
                continue

            indexed_targets[key] = target_ids[0]

        return indexed_targets, unmapped_count, ambiguous_count

    def _build_target_key(
        self,
        target: _SavedCotacao,
    ) -> tuple[str, str, str, str] | None:
        ceasa_name = self._resolve_prohort_ceasa_name(target)
        unit = normalize_prohort_unit(target.unit)

        if ceasa_name is None or unit is None:
            return None

        return build_match_key(
            ceasa_name=ceasa_name,
            quote_date=target.quote_date,
            product_name=target.product_name,
            unit=unit,
        )

    def _build_fallback_indexes(
        self,
        saved_cotacoes: list[_SavedCotacao],
    ) -> tuple[
        set[tuple[str, str, str, str]],
        dict[tuple[str, str], _FallbackDestination],
    ]:
        existing_keys: set[tuple[str, str, str, str]] = set()
        fallback_destinations: dict[tuple[str, str], _FallbackDestination] = {}

        for cotacao in saved_cotacoes:
            ceasa_name = self._resolve_prohort_ceasa_name(cotacao)

            if ceasa_name is None:
                continue

            scope_key = (normalize_text(ceasa_name), cotacao.quote_date.isoformat())
            fallback_destinations.setdefault(
                scope_key,
                self._build_fallback_destination(cotacao),
            )

            unit = normalize_prohort_unit(cotacao.unit)

            if unit is None:
                continue

            existing_keys.add(
                build_match_key(
                    ceasa_name=ceasa_name,
                    quote_date=cotacao.quote_date,
                    product_name=cotacao.product_name,
                    unit=unit,
                )
            )

        return existing_keys, fallback_destinations

    def _build_fallback_destination(
        self,
        cotacao: _SavedCotacao,
    ) -> _FallbackDestination:
        return _FallbackDestination(
            ceasa_id=cotacao.ceasa_id,
            source_slug=cotacao.source_slug,
            market_slug=cotacao.market_slug,
            category_slug="prohort-complemento",
            procedencia=None,
        )

    def _resolve_prohort_ceasa_name(self, target: _SavedCotacao) -> str | None:
        if target.source_slug in SOURCE_CEASA_NAMES:
            return SOURCE_CEASA_NAMES[target.source_slug]

        if target.source_slug == "ceasa-pr":
            return CEASA_PR_CATEGORIES.get(target.market_slug or "")

        if target.source_slug == "ceasa-mg" and target.market_slug:
            return CEASA_MG_PROCEDENCIAS.get(normalize_text(target.market_slug))

        return None

    def _apply_prohort_rows(
        self,
        connection: sqlite3.Connection,
        targets_by_key: dict[tuple[str, str, str, str], int],
        existing_keys: set[tuple[str, str, str, str]],
        fallback_destinations: dict[tuple[str, str], _FallbackDestination],
    ) -> tuple[int, int, int, int]:
        scanned_rows = 0
        matched_rows = 0
        updated_count = 0
        inserted_count = 0
        updated_ids: set[int] = set()

        request = Request(
            self.prohort_url,
            headers={"User-Agent": "cotacoes-ceasa-scraper/0.1"},
        )

        with urlopen(request, timeout=self.timeout_seconds) as response:
            stream = io.TextIOWrapper(
                response,
                encoding="latin-1",
                errors="replace",
                newline="",
            )
            reader = csv.DictReader(stream, delimiter=";")

            for row in reader:
                scanned_rows += 1
                quote_date = parse_prohort_date(row.get("data_preco"))
                price = parse_prohort_price(row.get("preco_diario"))
                unit = normalize_prohort_unit(row.get("sig_unidade_medida"))

                if quote_date is None or price is None or unit is None:
                    continue

                ceasa_name = row.get("dsc_ceasa") or ""
                product_name = clean_prohort_value(row.get("dsc_produto"))

                if not product_name:
                    continue

                key = build_match_key(
                    ceasa_name=ceasa_name,
                    quote_date=quote_date,
                    product_name=product_name,
                    unit=unit,
                )
                target_id = targets_by_key.get(key)

                if target_id is not None and target_id not in updated_ids:
                    matched_rows += 1
                    updated_count += self._update_target(connection, target_id, price)
                    updated_ids.add(target_id)

                if key in existing_keys:
                    continue

                destination = fallback_destinations.get(
                    (normalize_text(ceasa_name), quote_date.isoformat())
                )

                if destination is None:
                    continue

                inserted_count += self._insert_fallback(
                    connection=connection,
                    destination=destination,
                    quote_date=quote_date,
                    product_name=product_name,
                    unit=unit,
                    price=price,
                )
                existing_keys.add(key)

        return scanned_rows, matched_rows, updated_count, inserted_count

    def _update_target(
        self,
        connection: sqlite3.Connection,
        target_id: int,
        price: Decimal,
    ) -> int:
        cursor = connection.execute(
            """
            UPDATE cotacoes
            SET
                preco_comum = ?,
                fonte_complemento = ?,
                url_complemento = ?,
                data_complemento = ?
            WHERE id = ?
              AND preco_comum IS NULL
            """,
            (
                str(price),
                "prohort",
                self.prohort_url,
                datetime.now().isoformat(timespec="seconds"),
                target_id,
            ),
        )

        return cursor.rowcount

    def _insert_fallback(
        self,
        connection: sqlite3.Connection,
        destination: _FallbackDestination,
        quote_date: date,
        product_name: str,
        unit: str,
        price: Decimal,
    ) -> int:
        now = datetime.now()
        cotacao = Cotacao(
            fonte="PROHORT",
            categoria=destination.category_slug,
            produto=product_name,
            unidade=unit,
            procedencia=destination.procedencia,
            classificacao=None,
            data_cotacao=quote_date,
            preco_minimo=None,
            preco_comum=price,
            preco_maximo=None,
            situacao_mercado=None,
            url_origem=self.prohort_url,
            entreposto=destination.market_slug,
            fonte_complemento="prohort",
            url_complemento=self.prohort_url,
            data_complemento=now,
        )
        return SQLiteStorage(self.database_path).insert_cotacoes(
            connection=connection,
            cotacoes=[cotacao],
            ceasa_id=destination.ceasa_id,
            source_slug=destination.source_slug,
            default_market=destination.market_slug or "Varias cidades",
        )


def build_match_key(
    ceasa_name: str,
    quote_date: date,
    product_name: str,
    unit: str,
) -> tuple[str, str, str, str]:
    return (
        normalize_text(ceasa_name),
        quote_date.isoformat(),
        normalize_text(product_name),
        unit,
    )


def normalize_prohort_unit(value: str | None) -> str | None:
    normalized_value = normalize_text(value)

    if normalized_value in {"kg", "quilo", "quilograma"}:
        return "KG"

    if normalized_value in {"un", "und", "unid", "unidade"}:
        return "UN"

    if normalized_value in {"dz", "duzia"}:
        return "DZ"

    return None


def parse_prohort_date(value: str | None) -> date | None:
    if not value:
        return None

    value = value.strip()[:10]

    for date_format in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    return None


def parse_prohort_price(value: str | None) -> Decimal | None:
    if not value:
        return None

    normalized_value = value.strip().replace(",", ".")

    try:
        return Decimal(normalized_value)
    except InvalidOperation:
        return None


def clean_prohort_value(value: str | None) -> str:
    return " ".join((value or "").split())


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""

    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")

    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()
