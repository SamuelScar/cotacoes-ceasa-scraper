import argparse
from pathlib import Path

from cotacoes_ceasa.workflows.ceasa_df_recovery import (
    create_ceasa_df_recovery_plan,
    write_ceasa_df_recovery_plan,
)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    plan = create_ceasa_df_recovery_plan(
        database_path=args.database_path,
        project_root=args.project_root,
        raw_directory=args.raw_directory,
        pdf_cache_directory=args.pdf_cache_directory,
    )
    write_ceasa_df_recovery_plan(
        plan,
        json_path=args.json_path,
        csv_path=args.csv_path,
    )
    print_summary(plan, args.json_path, args.csv_path)
    if plan["status"] != "valid":
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Planeja a recuperacao das variedades FUJI e GALA da CEASA-DF "
            "sem alterar o banco, os raws ou o cache."
        )
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path("auditoria_cotacoes/cotacoes-proveniencia.sqlite"),
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path("."),
    )
    parser.add_argument(
        "--raw-directory",
        type=Path,
        default=Path("data/raw/ceasa-df"),
    )
    parser.add_argument(
        "--pdf-cache-directory",
        type=Path,
        default=Path("data/cache/pdf-text"),
    )
    parser.add_argument(
        "--json-path",
        type=Path,
        default=Path("auditoria_cotacoes/plano-recuperacao-ceasa-df.json"),
    )
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=Path("auditoria_cotacoes/plano-recuperacao-ceasa-df.csv"),
    )
    return parser


def print_summary(
    plan: dict[str, object],
    json_path: Path,
    csv_path: Path,
) -> None:
    summary = plan["summary"]
    print(f"Status: {plan['status']}")
    print(f"Coletas CEASA-DF: {summary['collections']}")
    print(f"PDFs unicos: {summary['unique_pdfs']}")
    print(f"Linhas armazenadas a corrigir: {summary['rows_to_update']}")
    print(
        "Proveniencias historicas cobertas: "
        f"{summary['existing_provenance_rows']}"
    )
    print(
        "Proveniencias historicas a remapear: "
        f"{summary['existing_provenance_reassignments']}"
    )
    print(f"Ocorrencias corrigidas esperadas: {summary['parsed_target_occurrences']}")
    print(f"Ocorrencias recuperadas: {summary['recovered_occurrences']}")
    print(f"Documentos unicos com colisao: {summary['unique_collision_documents']}")
    print(f"Distincoes unicas recuperadas: {summary['unique_distinctions']}")
    print(f"Novas cotacoes logicas: {summary['logical_inserts']}")
    print(f"Novas proveniencias planejadas: {summary['recovery_provenance_rows']}")
    print(f"Banco alterado: {plan['safety']['database_rows_changed']}")
    print(f"Manifesto JSON: {json_path}")
    print(f"Manifesto CSV: {csv_path}")


if __name__ == "__main__":
    main()
