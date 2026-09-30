import argparse
from pathlib import Path

from cotacoes_ceasa.workflows.provenance import (
    ProvenanceCandidateResult,
    create_provenance_candidate,
    write_duplicate_provenance_manifest,
    write_provenance_report,
)


ISSUE_6_DUPLICATE_GROUPS = 11_781
ISSUE_6_DUPLICATE_OCCURRENCES = 157_425
ISSUE_6_DUPLICATE_EXCESS = 145_644


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = run(
        database_path=args.database_path,
        candidate_path=args.candidate_path,
        validate_issue_6_counts=not args.skip_issue_6_counts,
    )
    print_summary(result)
    if args.report_path is not None:
        write_provenance_report(result, args.report_path)
        print(f"Manifesto JSON: {args.report_path}")
    if args.manifest_path is not None:
        rows = write_duplicate_provenance_manifest(
            args.candidate_path,
            args.manifest_path,
        )
        print(f"Manifesto CSV: {args.manifest_path} ({rows} ocorrencias)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Cria uma copia SQLite com proveniencia historica, sem remover "
            "cotacoes nem alterar o banco de origem."
        )
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path("data/cotacoes.sqlite"),
        help="SQLite de origem. Padrao: data/cotacoes.sqlite.",
    )
    parser.add_argument(
        "--candidate-path",
        type=Path,
        required=True,
        help="Novo SQLite que recebera a migracao aditiva.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Grava o manifesto auditavel em JSON.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        help="Grava uma linha por ocorrencia dos grupos duplicados em CSV.",
    )
    parser.add_argument(
        "--skip-issue-6-counts",
        action="store_true",
        help="Nao exige os totais historicos registrados na issue 6.",
    )
    return parser


def run(
    database_path: Path,
    candidate_path: Path,
    validate_issue_6_counts: bool,
) -> ProvenanceCandidateResult:
    expected_groups = (
        ISSUE_6_DUPLICATE_GROUPS if validate_issue_6_counts else None
    )
    expected_occurrences = (
        ISSUE_6_DUPLICATE_OCCURRENCES if validate_issue_6_counts else None
    )
    expected_excess = ISSUE_6_DUPLICATE_EXCESS if validate_issue_6_counts else None
    return create_provenance_candidate(
        source_database_path=database_path,
        candidate_database_path=candidate_path,
        expected_duplicate_groups=expected_groups,
        expected_duplicate_occurrences=expected_occurrences,
        expected_duplicate_excess=expected_excess,
    )


def print_summary(result: ProvenanceCandidateResult) -> None:
    print(f"SQLite de origem preservado: {result.source_database_path}")
    print(f"SQLite candidato: {result.candidate_database_path}")
    print(f"Cotacoes historicas: {result.after.quotes}")
    print(
        "Migracao de proveniencia registrada: "
        f"{'sim' if result.after.provenance_migration_applied else 'nao'}"
    )
    print(
        f"Checkpoint de cotacoes: {result.after.provenance_migration_quote_max_id}"
    )
    print(f"Linhas de proveniencia: {result.after.provenance_rows}")
    print(f"Grupos duplicados: {result.after.duplicate_groups}")
    print(f"Ocorrencias nos grupos: {result.after.duplicate_occurrences}")
    print(f"Registros excedentes: {result.after.duplicate_excess}")
    print(
        "Grupos ambiguos: "
        f"{result.after.ambiguous_duplicate_groups}"
    )
    print(f"Status: {'valida' if result.valid else 'invalida'}")


if __name__ == "__main__":
    main()
