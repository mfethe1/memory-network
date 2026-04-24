"""`code_index scip-python-index`: run the optional scip-python indexer."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.commands.import_scip_cmd import _load_from_scip_binary
from code_index.locking import LockTimeoutError, writer_lock
from code_index.scip_import import import_scip_json

DEFAULT_SCIP_PYTHON_SUBDIR = Path("external") / "scip-python"


def _text_tail(value: str, *, limit: int = 4000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _emit(args: argparse.Namespace, payload: dict, *, human: str | None = None) -> None:
    if args.json:
        print(json.dumps(payload, indent=2))
    elif human:
        print(human)
    elif "error" in payload:
        print(f"error: {payload['error']}")
        if payload.get("hint"):
            print(f"hint:  {payload['hint']}")
    else:
        print(json.dumps(payload, indent=2))


def _resolve_output_dir(config: cfg_mod.Config, value: str | None) -> Path:
    if value:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else config.root / candidate
    return config.index_dir / DEFAULT_SCIP_PYTHON_SUBDIR


def _build_command(
    scip_python: str,
    config: cfg_mod.Config,
    args: argparse.Namespace,
) -> list[str]:
    project_name = args.project_name or config.root.name or "project"
    command = [
        scip_python,
        "index",
        str(config.root),
        "--project-name",
        project_name,
    ]
    if args.project_version:
        command.extend(["--project-version", args.project_version])
    if args.project_namespace:
        command.extend(["--project-namespace", args.project_namespace])
    if args.environment:
        command.extend(["--environment", args.environment])
    if args.target_only:
        command.extend(["--target-only", args.target_only])
    for extra in args.extra_arg or []:
        command.append(extra)
    return command


def _import_generated_index(
    config: cfg_mod.Config,
    index_path: Path,
) -> tuple[dict, int]:
    if not config.db_path.exists():
        return (
            {
                "error": (
                    f"no index at {config.index_dir}; run `code_index init` before "
                    "using --import-index"
                )
            },
            2,
        )
    try:
        payload = _load_from_scip_binary(index_path)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return ({"error": f"invalid SCIP input: {exc}"}, 2)

    conn = db_mod.connect(config.db_path)
    try:
        try:
            with writer_lock(config):
                db_mod.apply_schema(conn)
                with db_mod.transaction(conn):
                    stats = import_scip_json(
                        conn,
                        config,
                        payload,
                        event_source="scip-python-index",
                    )
        except LockTimeoutError as exc:
            return (
                {
                    "error": "another writer holds the lock",
                    "lock_path": str(exc.lock_path),
                    "timeout_s": exc.timeout_s,
                },
                3,
            )
    finally:
        db_mod.close(conn)
    return ({"stats": stats.to_dict()}, 0)


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)

    scip_python = shutil.which("scip-python")
    if not scip_python:
        payload = {
            "error": "scip-python is not on PATH",
            "command": "scip-python",
            "hint": "Install with `npm install -g @sourcegraph/scip-python`.",
        }
        _emit(args, payload)
        return 2

    config.index_dir.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        cfg_mod.save(config)

    output_dir = _resolve_output_dir(config, args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "index.scip"
    command = _build_command(scip_python, config, args)

    proc = subprocess.run(
        command,
        cwd=output_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    report = {
        "command": command,
        "cwd": str(output_dir),
        "output_path": str(output_path),
        "returncode": proc.returncode,
        "stdout": _text_tail(proc.stdout),
        "stderr": _text_tail(proc.stderr),
    }
    if proc.returncode != 0:
        report["error"] = "scip-python index failed"
        _emit(args, report)
        return proc.returncode or 1
    if not output_path.exists():
        report["error"] = "scip-python completed but did not create index.scip"
        report["hint"] = "Run with --json to inspect stdout/stderr and command details."
        _emit(args, report)
        return 2

    if args.import_index:
        import_report, import_rc = _import_generated_index(config, output_path)
        report["import"] = import_report
        if import_rc != 0:
            report["error"] = "generated SCIP index but import failed"
            _emit(args, report)
            return import_rc

    human = f"wrote SCIP index: {output_path}"
    if args.import_index:
        stats = report["import"]["stats"]
        human += (
            f"\nimported SCIP: documents={stats['documents_seen']} "
            f"symbols={stats['symbols_upserted']} "
            f"occurrences={stats['occurrences_inserted']} "
            f"relations={stats['relations_inserted']}"
        )
    _emit(args, report, human=human)
    return 0
