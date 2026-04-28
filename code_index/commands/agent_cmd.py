"""`code_index agent`: record agent work for the live code graph."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from code_index import agent_activity
from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.agent_collaboration import append_event_jsonl
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


def _print_transcript_text(transcript: dict[str, Any]) -> None:
    run = transcript["run"]
    summary = transcript["summary"]
    print(
        f"{run['run_id']} {run['agent_name']} {run['status']} "
        f"{summary['included_event_count']}/{summary['event_count']} events"
    )
    if transcript["active_files"]:
        print("active files: " + ", ".join(transcript["active_files"]))
    for event in transcript["events"]:
        label = event["event_type"]
        target = event.get("file_path") or event.get("symbol_path") or ""
        suffix = f" {target}" if target else ""
        print(f"{event['timestamp']} {label}{suffix}: {event['message']}")


def _file_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in value if str(item).strip()]


def _first_file_path(value: Any) -> str | None:
    paths = _file_paths(value)
    return paths[0] if paths else None


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


def _resolve_existing_run(conn, args: argparse.Namespace) -> dict[str, Any]:
    if args.run_id:
        run = agent_activity.get_run(conn, args.run_id)
        if run is None:
            raise ValueError(f"unknown agent run_id: {args.run_id}")
        return run
    run = agent_activity.latest_active_run(conn, agent_name=args.agent_name)
    if run is None:
        raise ValueError("no active run found; pass --run-id")
    return run


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

        if action == "claims":
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                _print_json(
                    {
                        "action": "claims",
                        "active_claims": agent_activity.active_file_claims(
                            conn,
                            limit=max(0, int(args.limit)),
                            file_path=_first_file_path(args.file_path),
                        ),
                    }
                )
            finally:
                db_mod.close(conn)
            return 0

        if action == "verify-claim":
            if not args.run_id:
                print(
                    "error: --run-id is required for `code_index agent verify-claim`",
                    file=sys.stderr,
                )
                return 2
            path = _first_file_path(args.file_path)
            if not path:
                print(
                    "error: --file is required for `code_index agent verify-claim`",
                    file=sys.stderr,
                )
                return 2
            if args.fence is None:
                print(
                    "error: --fence is required for `code_index agent verify-claim`",
                    file=sys.stderr,
                )
                return 2
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                result = agent_activity.verify_write_claim(
                    conn,
                    run_id=args.run_id,
                    file_path=path,
                    fence_token=args.fence,
                )
            finally:
                db_mod.close(conn)
            if args.json:
                _print_json({"action": "verify-claim", **result})
            elif result["ok"]:
                print(result["message"])
            else:
                print(f"error: {result['message']}", file=sys.stderr)
            return 0 if result["ok"] else 1

        if action == "board":
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                _print_json(agent_activity.kanban_board(conn, limit=max(0, int(args.limit))))
            finally:
                db_mod.close(conn)
            return 0

        if action == "transcript":
            if not args.run_id:
                print(
                    "error: --run-id is required for `code_index agent transcript`",
                    file=sys.stderr,
                )
                return 2
            conn = db_mod.connect(config.db_path)
            try:
                db_mod.ensure_schema(conn, config)
                transcript = agent_activity.run_transcript(
                    conn,
                    args.run_id,
                    limit=max(0, int(args.limit)),
                )
            finally:
                db_mod.close(conn)
            if transcript is None:
                print(f"error: unknown agent run_id: {args.run_id}", file=sys.stderr)
                return 2
            if args.json:
                _print_json(transcript)
            else:
                _print_transcript_text(transcript)
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
                        status=(
                            "blocked"
                            if args.blocked_by_run_id
                            else (args.status or "working")
                        ),
                    )
                    blockers = []
                    if args.blocked_by_run_id:
                        blockers = agent_activity.add_run_blockers(
                            conn,
                            run_id=run_payload["run_id"],
                            blocked_by_run_ids=args.blocked_by_run_id,
                            reason=args.message,
                            metadata={"source": "agent-cli"},
                        )
                        run_payload = agent_activity.get_run(
                            conn, run_payload["run_id"]
                        )
                    _print_json(
                        {
                            "action": "start",
                            "run": run_payload,
                            "blockers": blockers,
                        }
                    )
                    return 0

                if action == "block":
                    if not args.run_id:
                        print(
                            "error: --run-id is required for `code_index agent block`",
                            file=sys.stderr,
                        )
                        return 2
                    if not args.blocked_by_run_id:
                        print(
                            "error: --blocked-by is required for `code_index agent block`",
                            file=sys.stderr,
                        )
                        return 2
                    blockers = agent_activity.add_run_blockers(
                        conn,
                        run_id=args.run_id,
                        blocked_by_run_ids=args.blocked_by_run_id,
                        reason=args.message,
                        metadata=_parse_json_object(args.payload, label="--payload"),
                    )
                    _print_json(
                        {
                            "action": "block",
                            "run": agent_activity.get_run(conn, args.run_id),
                            "blockers": blockers,
                        }
                    )
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
                        file_path=_first_file_path(args.file_path),
                        symbol_path=args.symbol_path,
                        message=args.message,
                        payload=event_payload_json,
                    )
                    append_event_jsonl(config.root, event_payload)
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

                if action == "claim":
                    paths = _file_paths(args.file_path)
                    if not paths:
                        print(
                            "error: --file is required for `code_index agent claim`",
                            file=sys.stderr,
                        )
                        return 2
                    run_payload = _resolve_active_or_new_run(conn, args)
                    claims = agent_activity.claim_files(
                        conn,
                        run_id=run_payload["run_id"],
                        file_paths=paths,
                        mode=args.mode,
                        reason=args.message,
                        ttl_seconds=args.ttl_seconds,
                        metadata=_parse_json_object(args.payload, label="--payload"),
                    )
                    _print_json(
                        {
                            "action": "claim",
                            "run": agent_activity.get_run(
                                conn, run_payload["run_id"]
                            ),
                            "claims": claims,
                        }
                    )
                    return 0

                if action == "release":
                    run_payload = _resolve_existing_run(conn, args)
                    paths = _file_paths(args.file_path)
                    released: list[dict[str, Any]] = []
                    if paths:
                        for path in paths:
                            released.extend(
                                agent_activity.release_claims(
                                    conn,
                                    run_id=run_payload["run_id"],
                                    file_path=path,
                                    mode=args.mode,
                                )
                            )
                    else:
                        released = agent_activity.release_claims(
                            conn,
                            run_id=run_payload["run_id"],
                            mode=args.mode,
                        )
                    _print_json(
                        {
                            "action": "release",
                            "run": agent_activity.get_run(
                                conn, run_payload["run_id"]
                            ),
                            "claims": released,
                        }
                    )
                    return 0

                if action == "decision":
                    if not args.message:
                        print(
                            "error: --message is required for `code_index agent decision`",
                            file=sys.stderr,
                        )
                        return 2
                    run_payload = _resolve_active_or_new_run(conn, args)
                    decision_payload_json = _parse_json_object(
                        args.payload, label="--payload"
                    )
                    event_payload = agent_activity.record_decision(
                        conn,
                        run_id=run_payload["run_id"],
                        decision=args.message,
                        status=args.status,
                        payload=decision_payload_json,
                    )
                    append_event_jsonl(config.root, event_payload)
                    _print_json(
                        {
                            "action": "decision",
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
                    suggestions_event = agent_activity.record_run_suggestions(
                        conn, run_id=ended["run_id"]
                    )
                    append_event_jsonl(config.root, suggestions_event)
                    _print_json(
                        {
                            "action": "end",
                            "run": ended,
                            "suggestions_event": suggestions_event,
                            "suggestions": agent_activity.build_run_suggestions(
                                conn, ended["run_id"]
                            ),
                        }
                    )
                    return 0
            finally:
                db_mod.close(conn)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"error: unknown agent action: {action}", file=sys.stderr)
    return 2
