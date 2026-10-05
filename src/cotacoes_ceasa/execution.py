"""Contexto compartilhado dos logs de uma execucao operacional."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


EXECUTION_LOG_ROOT = Path("data/logs/execucao")
EXECUTION_TIMEZONE = ZoneInfo("America/Sao_Paulo")
INCOMPLETE_MARKER = "_INCOMPLETA"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def execution_now() -> datetime:
    return datetime.now(EXECUTION_TIMEZONE)


def sanitize_log_text(value: object) -> str:
    """Mascara credenciais comuns antes de persistir mensagens operacionais."""
    text = str(value)
    text = re.sub(
        r"(?i)(postgres(?:ql)?://[^\s:/]+:)[^@\s/]+(@)",
        r"\1***\2",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:token|password|secret|api[-_]?key)=)[^&\s]+",
        r"\1***",
        text,
    )

    for name, secret in os.environ.items():
        if not secret or len(secret) < 4:
            continue
        if any(
            key in name.upper()
            for key in ("PASSWORD", "TOKEN", "SECRET", "API_KEY")
        ):
            text = text.replace(secret, "***")

    return text


def write_json_atomic(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        temporary.write_text(f"{content}\n", encoding="utf-8")
        temporary.replace(destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def write_text_atomic(destination: Path, content: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def create_execution_id() -> str:
    timestamp = execution_now().strftime("%Y%m%d_%H%M%S_%f")
    run_id = _safe_identifier(os.getenv("GITHUB_RUN_ID", ""))
    run_attempt = _safe_identifier(os.getenv("GITHUB_RUN_ATTEMPT", ""))
    if run_id:
        return f"{timestamp}_{run_id}_{run_attempt or '1'}"
    return f"{timestamp}_local_{uuid.uuid4().hex[:8]}"


@dataclass
class ExecutionContext:
    execution_id: str
    directory: Path
    externally_managed: bool = False
    step_name: str = "cli"

    @classmethod
    def from_environment(cls) -> "ExecutionContext":
        configured_directory = os.getenv("COTACOES_EXECUTION_DIR", "").strip()
        if configured_directory:
            directory = Path(configured_directory)
            execution_id = _safe_identifier(
                os.getenv("COTACOES_EXECUTION_ID", directory.name)
            )
            return cls(
                execution_id=execution_id or directory.name,
                directory=directory,
                externally_managed=_environment_bool(
                    "COTACOES_EXECUTION_MANAGED", default=True
                ),
                step_name=_safe_identifier(
                    os.getenv("COTACOES_EXECUTION_STEP", "cli")
                )
                or "cli",
            )

        execution_id = create_execution_id()
        return cls(
            execution_id=execution_id,
            directory=EXECUTION_LOG_ROOT / execution_id,
            externally_managed=False,
        )

    @property
    def state_path(self) -> Path:
        return self.directory / "execucao.json"

    @property
    def incomplete_marker(self) -> Path:
        return self.directory / INCOMPLETE_MARKER

    @property
    def steps_directory(self) -> Path:
        return self.directory / "etapas"

    def initialize(self) -> None:
        self.steps_directory.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            return

        console_path = self.directory / "console.log"
        if not console_path.exists():
            write_text_atomic(console_path, "")
        if not self.incomplete_marker.exists():
            write_text_atomic(
                self.incomplete_marker,
                "Execucao em andamento ou interrompida. Nao remover automaticamente.\n",
            )

        started_at = execution_now().isoformat(timespec="seconds")
        state = {
            "schema_version": 1,
            "execution_id": self.execution_id,
            "status": "running",
            "current_stage": "initializing",
            "started_at": started_at,
            "finished_at": None,
            "history": [
                {
                    "occurred_at": started_at,
                    "stage": "initializing",
                    "status": "running",
                    "detail": "Contexto da execucao criado.",
                }
            ],
            "exit_code": None,
            "files": [],
            "workflow": _workflow_context(),
            "final_error": None,
            "complete": False,
            "retention_safe": False,
        }
        write_json_atomic(self.state_path, state)

    def set_step_name(self, step_name: str) -> None:
        self.step_name = _safe_identifier(step_name) or "cli"

    def record_stage(
        self,
        stage: str,
        status: str = "running",
        detail: str | None = None,
    ) -> None:
        state = self._load_state()
        safe_stage = sanitize_log_text(stage)
        state["current_stage"] = safe_stage
        state.setdefault("history", []).append(
            {
                "occurred_at": execution_now().isoformat(timespec="seconds"),
                "stage": safe_stage,
                "status": status,
                "detail": sanitize_log_text(detail) if detail else None,
            }
        )
        write_json_atomic(self.state_path, state)

    def register_file(self, file_path: Path) -> None:
        state = self._load_state()
        try:
            stored_path = (
                file_path.resolve()
                .relative_to(self.directory.resolve())
                .as_posix()
            )
        except ValueError:
            stored_path = file_path.as_posix()
        files = state.setdefault("files", [])
        if stored_path not in files:
            files.append(stored_path)
            files.sort()
        write_json_atomic(self.state_path, state)

    def finish_step(
        self,
        status: str,
        exit_code: int,
        error: str | None = None,
    ) -> None:
        self.record_stage(
            self.step_name,
            status=status,
            detail=(
                f"Codigo de saida: {exit_code}. Erro: {error}"
                if error
                else f"Codigo de saida: {exit_code}."
            ),
        )
        state = self._load_state()
        if status in {"failed", "cancelled"}:
            state["status"] = status
        state["files"] = self._discover_files(state.get("files", []))
        write_json_atomic(self.state_path, state)

    def finalize(
        self,
        status: str,
        exit_code: int,
        error: str | None = None,
        required_files: Iterable[Path] = (),
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Status final invalido: {status}")
        missing_files = [
            path.relative_to(self.directory).as_posix()
            if path.is_relative_to(self.directory)
            else path.as_posix()
            for path in required_files
            if not path.is_file()
        ]
        if missing_files:
            missing = ", ".join(sorted(missing_files))
            raise RuntimeError(
                "Execucao nao pode ser marcada como completa; "
                f"arquivos obrigatorios ausentes: {missing}."
            )
        state = self._load_state()
        finished_at = execution_now().isoformat(timespec="seconds")
        state["files"] = self._discover_files(state.get("files", []))
        state.update(
            {
                "status": status,
                "current_stage": "finished",
                "finished_at": finished_at,
                "exit_code": exit_code,
                "final_error": sanitize_log_text(error) if error else None,
                "complete": True,
                "retention_safe": True,
            }
        )
        state.setdefault("history", []).append(
            {
                "occurred_at": finished_at,
                "stage": "finished",
                "status": status,
                "detail": sanitize_log_text(error) if error else None,
            }
        )
        write_json_atomic(self.state_path, state)
        self.incomplete_marker.unlink(missing_ok=True)

    def _discover_files(self, existing_files: list[str]) -> list[str]:
        generated_files = {
            path.relative_to(self.directory).as_posix()
            for path in self.directory.rglob("*")
            if path.is_file()
            and path.name != INCOMPLETE_MARKER
            and not path.name.endswith(".tmp")
        }
        return sorted({*existing_files, *generated_files})

    def _load_state(self) -> dict[str, Any]:
        self.initialize()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Estado da execucao invalido em {self.state_path}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(f"Estado da execucao invalido em {self.state_path}.")
        return payload


def cleanup_completed_executions(
    root: Path,
    current_directory: Path | None,
    max_age_days: int | None,
    max_count: int | None,
) -> list[Path]:
    """Remove somente execucoes finalizadas e marcadas como seguras."""
    for name, value in (
        ("max_age_days", max_age_days),
        ("max_count", max_count),
    ):
        if value is not None and value < 1:
            raise ValueError(f"{name} deve ser maior ou igual a 1.")

    if not root.is_dir():
        return []

    current_resolved = current_directory.resolve() if current_directory else None
    candidates: list[tuple[datetime, Path]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name == "auditoria":
            continue
        if current_resolved is not None and directory.resolve() == current_resolved:
            continue
        state_path = directory / "execucao.json"
        if not state_path.is_file() or (directory / INCOMPLETE_MARKER).exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            finished_at = datetime.fromisoformat(str(state["finished_at"]))
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            continue
        if (
            state.get("status") not in TERMINAL_STATUSES
            or state.get("complete") is not True
            or state.get("retention_safe") is not True
        ):
            continue
        candidates.append((finished_at, directory))

    candidates.sort(key=lambda item: item[0], reverse=True)
    cutoff = (
        execution_now() - timedelta(days=max_age_days)
        if max_age_days is not None
        else None
    )
    removable: set[Path] = set()
    retained_candidate_count = (
        max(max_count - 1, 0)
        if max_count is not None and current_directory is not None
        else max_count
    )
    for index, (finished_at, directory) in enumerate(candidates):
        expired_by_age = cutoff is not None and finished_at < cutoff
        expired_by_count = (
            retained_candidate_count is not None
            and index >= retained_candidate_count
        )
        if expired_by_age or expired_by_count:
            removable.add(directory)

    removed: list[Path] = []
    root_resolved = root.resolve()
    for directory in sorted(removable):
        if directory.resolve().parent != root_resolved:
            continue
        shutil.rmtree(directory)
        removed.append(directory)
    return removed


def _workflow_context() -> dict[str, str | None]:
    names = (
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_EVENT_NAME",
        "GITHUB_REF",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW",
        "GITHUB_JOB",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "RUNNER_NAME",
    )
    return {name.lower(): os.getenv(name) for name in names}


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "-", value.strip())


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("o valor deve ser um numero inteiro") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("o valor deve ser maior ou igual a 1")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Gerencia logs de uma execucao.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--directory", type=Path, required=True)
    init_parser.add_argument("--execution-id", required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--directory", type=Path, required=True)
    record_parser.add_argument("--execution-id", required=True)
    record_parser.add_argument("--stage", required=True)
    record_parser.add_argument("--status", default="running")
    record_parser.add_argument("--detail")

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--root", type=Path, default=EXECUTION_LOG_ROOT)
    cleanup_parser.add_argument("--current-directory", type=Path)
    cleanup_parser.add_argument("--max-age-days", type=_positive_int)
    cleanup_parser.add_argument("--max-count", type=_positive_int)

    args = parser.parse_args()
    if args.command == "cleanup":
        removed = cleanup_completed_executions(
            args.root,
            args.current_directory,
            args.max_age_days,
            args.max_count,
        )
        for directory in removed:
            print(f"Log antigo removido: {directory}")
        return 0

    context = ExecutionContext(
        execution_id=_safe_identifier(args.execution_id),
        directory=args.directory,
        externally_managed=True,
    )
    context.initialize()
    if args.command == "record":
        context.record_stage(args.stage, args.status, args.detail)
    print(context.directory.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
