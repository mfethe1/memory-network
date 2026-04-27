"""`code_index agent`: record agent work for the live code graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db as db_mod
from code_index.locking import writer_lock


def _parse_json_object(raw: str | None, *, label: str) -> dict[str, Any]:
    if not raw:
        return {}
    text = raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def _resolve_active_or_new_run(conn, args: argparse.Namespace) -> dict[str, Any]:
    if args.run_id:
        run = agent_activity.get_run(conn, args.run_id)
        if run is None:
            raise ValueError(f"unknown agent run_id: {args.run_id}")
        return run
    run = agent_activity.latest_active_run(conn, agent_name=args.agent_name)
    if run is not None:
        return run
    return agent_activity.start_run(
        conn,
        agent_name=args.agent_name,
        prompt=args.prompt or "",
        selected_nodes=args.selected_node or [],
        metadata={"implicit": True},
    )


def run(args: argparse.Namespace) -> int:
    root_hint = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    root = cfg_mod.find_root(root_hint) or root_hint
    config = cfg_mod.load(root)
    if not config.db_path.exists():
        print(f"error: no index at {config.index_dir}. run `code_index init` first.")
        return 2

    action = args.agent_action
    try:
        if action == "recent":
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                _print_json(
                    agent_activity.activity_snapshot(
                        conn,
                        event_limit=max(0, int(args.limit)),
                        file_limit=max(1, min(25, int(args.file_limit))),
                    )
                )
            finally:
                db_mod.close(conn)
            return 0

        with writer_lock(config):
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.apply_schema(conn)
                if action == "start":
                    run_payload = agent_activity.start_run(
                        conn,
                        run_id=args.run_id,
                        agent_name=args.agent_name,
                        prompt=args.prompt or "",
                        selected_nodes=args.selected_node or [],
                        metadata=_parse_json_object(args.metadata, label="--metadata"),
                        status=args.status or "working",
                    )
                    _print_json({"action": "start", "run": run_payload})
                    return 0

                if action == "event":
                    if not args.event_type:
                        print(
                            "error: --type is required for `code_index agent event`",
                            file=sys.stderr,
                        )
                        return 2
                    run_payload = _resolve_active_or_new_run(conn, args)
                    event_payload_json = _parse_json_object(
                        args.payload, label="--payload"
                    )
                    if args.status:
                        event_payload_json["status"] = args.status
                    event_payload = agent_activity.record_event(
                        conn,
                        run_id=run_payload["run_id"],
                        event_type=args.event_type,
                        file_path=args.file_path,
                        symbol_path=args.symbol_path,
                        message=args.message,
                        payload=event_payload_json,
                    )
                    _print_json(
                        {
                            "action": "event",
                            "run": agent_activity.get_run(
                                conn, event_payload["run_id"]
                            ),
                            "event": event_payload,
                        }
                    )
                    return 0

                if action == "end":
                    run_payload = _resolve_active_or_new_run(conn, args)
                    ended = agent_activity.end_run(
                        conn,
                        run_id=run_payload["run_id"],
                        status=args.status or "completed",
                    )
                    _print_json({"action": "end", "run": ended})
                    return 0
            finally:
                db_mod.close(conn)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"error: unknown agent action: {action}", file=sys.stderr)
    return 2
