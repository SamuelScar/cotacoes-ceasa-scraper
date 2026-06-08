from cotacoes_ceasa.cli.commands.batch import run_all_sources
from cotacoes_ceasa.cli.commands.maintenance import (
    run_archive_command,
    run_prohort_command,
)
from cotacoes_ceasa.cli.commands.source import run_source
from cotacoes_ceasa.cli.output import TerminalOutput
from cotacoes_ceasa.cli.parser import build_parser
from cotacoes_ceasa.config import load_config


def main() -> None:
    """Executa comandos de coleta disponiveis no projeto."""
    output = TerminalOutput()

    try:
        run(output)
    except KeyboardInterrupt:
        output.error("Execucao interrompida pelo usuario.")
        output.summary()
        raise SystemExit(130)
    except Exception as error:
        output.error(f"{type(error).__name__}: {error}")
        output.summary()
        raise SystemExit(1)


def run(output: TerminalOutput) -> None:
    """Seleciona e executa o fluxo solicitado pela CLI."""
    config = load_config()
    args = build_parser(config).parse_args()

    if args.archive_raw_old:
        run_archive_command(args, output)
        return

    if args.complement_prohort:
        run_prohort_command(args, output)
        return

    if args.all_sources or args.download_and_process:
        run_all_sources(args, config, output)
        return

    run_source(args, config, output)


if __name__ == "__main__":
    main()
