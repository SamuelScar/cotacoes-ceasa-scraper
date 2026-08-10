import argparse
from pathlib import Path

from cotacoes_ceasa.workflows.deduplication import (
    CandidateBaselineResult,
    DuplicateAnalysis,
    analyze_duplicate_content,
    create_candidate_baseline,
    write_duplicate_report,
)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.vacuum and args.candidate_path is None:
        parser.error("--vacuum exige --candidate-path.")
    result = run(args.database_path, args.candidate_path, args.vacuum)

    print_summary(result)
    if args.report_path is not None:
        write_duplicate_report(result, args.report_path)
        print(f"Relatorio JSON: {args.report_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mede repeticoes logicas e, opcionalmente, cria uma baseline "
            "deduplicada em outro SQLite."
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
        help="Cria uma copia deduplicada neste caminho sem alterar a origem.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Grava o diagnostico estruturado em JSON.",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Recompacta o arquivo candidato depois de remover repeticoes.",
    )
    return parser


def run(
    database_path: Path,
    candidate_path: Path | None,
    vacuum: bool,
) -> DuplicateAnalysis | CandidateBaselineResult:
    if candidate_path is None:
        return analyze_duplicate_content(database_path)

    return create_candidate_baseline(
        source_database_path=database_path,
        candidate_database_path=candidate_path,
        vacuum=vacuum,
    )


def print_summary(result: DuplicateAnalysis | CandidateBaselineResult) -> None:
    analysis = result.before if isinstance(result, CandidateBaselineResult) else result
    print("| Fonte | Observacoes | Conteudos logicos | Repeticoes |")
    print("| --- | ---: | ---: | ---: |")

    for source in analysis.sources:
        print(
            f"| {source.source_slug} | {source.observations} | "
            f"{source.logical_contents} | {source.repeated_observations} |"
        )

    print()
    print(f"Observacoes: {analysis.observations}")
    print(f"Conteudos logicos: {analysis.logical_contents}")
    print(f"Repeticoes: {analysis.repeated_observations}")
    print(f"Cobertura fonte/data: {analysis.source_date_coverage_hash}")

    if isinstance(result, CandidateBaselineResult):
        print(f"Baseline candidata: {result.candidate_database_path}")
        print(f"Observacoes removidas: {result.removed_observations}")
        print(f"Status: {'valida' if result.valid else 'invalida'}")


if __name__ == "__main__":
    main()
