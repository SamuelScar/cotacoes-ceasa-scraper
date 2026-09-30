import argparse
from pathlib import Path

from cotacoes_ceasa.workflows.ceasa_df_migration import (
    RecoveryCandidateResult,
    create_ceasa_df_recovery_candidate,
    write_recovery_candidate_report,
)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    result = create_ceasa_df_recovery_candidate(
        source_database_path=args.database_path,
        candidate_database_path=args.candidate_path,
        plan_path=args.plan_path,
    )
    write_recovery_candidate_report(result, args.report_path)
    print_summary(result, args.report_path)
    if not result.valid:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Cria e valida uma copia SQLite com a recuperacao idempotente "
            "das variedades FUJI e GALA da CEASA-DF."
        )
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path("auditoria_cotacoes/cotacoes-proveniencia.sqlite"),
        help="SQLite da etapa 2, preservado como origem somente leitura.",
    )
    parser.add_argument(
        "--candidate-path",
        type=Path,
        default=Path(
            "auditoria_cotacoes/cotacoes-ceasa-df-corrigida.sqlite"
        ),
        help="SQLite candidato que recebera a correcao.",
    )
    parser.add_argument(
        "--plan-path",
        type=Path,
        default=Path(
            "auditoria_cotacoes/plano-recuperacao-ceasa-df.json"
        ),
        help="Manifesto validado produzido pelo planejador.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path(
            "auditoria_cotacoes/correcao-ceasa-df-issue6.json"
        ),
        help="Relatorio JSON da aplicacao e validacao.",
    )
    return parser


def print_summary(
    result: RecoveryCandidateResult,
    report_path: Path,
) -> None:
    print(f"SQLite de origem preservado: {result.source_database_path}")
    print(f"SQLite candidato corrigido: {result.candidate_database_path}")
    print(
        "Candidata criada nesta execucao: "
        f"{'sim' if result.candidate_created else 'nao'}"
    )
    print(
        "Migracao aplicada nesta execucao: "
        f"{'sim' if result.migration_applied_now else 'nao (ja estava aplicada)'}"
    )
    print(f"Cotacoes antes: {result.before.quotes}")
    print(f"Cotacoes depois: {result.after.quotes}")
    print(f"Proveniencias antes: {result.before.provenance_rows}")
    print(f"Proveniencias depois: {result.after.provenance_rows}")
    print(
        "Classificacoes genericas restantes: "
        f"{result.after.generic_target_rows}"
    )
    print(
        "Cotacoes corrigidas da CEASA-DF: "
        f"{result.after.corrected_target_rows}"
    )
    print(
        "Proveniencias corrigidas da CEASA-DF: "
        f"{result.after.corrected_target_provenances}"
    )
    print(
        "Proveniencias recuperadas: "
        f"{result.after.recovered_provenance_rows}"
    )
    print(
        "Novas cotacoes com proveniencia de origem: "
        f"{result.after.recovered_origin_rows}"
    )
    print(
        "Cotacoes sem proveniencia de origem: "
        f"{result.after.quotes_without_origin_provenance}"
    )
    print(f"Quick check: {', '.join(result.quick_check)}")
    print(f"Violacoes de chave estrangeira: {result.foreign_key_violations}")
    print(f"Status: {'valida' if result.valid else 'invalida'}")
    print(f"Relatorio JSON: {report_path}")


if __name__ == "__main__":
    main()
