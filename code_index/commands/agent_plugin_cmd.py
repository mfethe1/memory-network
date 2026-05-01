"""`code_index agent-plugin`: start the repo-local graph agent plugin session."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from code_index import agent_sessions


def _index_plan(session: agent_sessions.TargetSession, policy: agent_sessions.IndexPolicy) -> str | None:
    db_path = session.root / ".code_index" / "index.db"
    index_dir = session.root / ".code_index"
    if policy == agent_sessions.IndexPolicy.REFRESH and db_path.exists():
        return f"index will be refreshed at {index_dir}"
    if policy != agent_sessions.IndexPolicy.NO_INDEX and not db_path.exists():
        return f"index will be initialized at {index_dir}"
    return None


def _check_only_payload(
    *,
    session: agent_sessions.TargetSession,
    provider: agent_sessions.AgentProviderSelection,
    policy: agent_sessions.IndexPolicy,
    provider_message: str,
) -> dict[str, object]:
    return {
        "ok": True,
        "kind": "code_index_agent_plugin_check",
        "root": str(session.root),
        "scope": str(session.scope).replace("\\", "/"),
        "provider": provider.provider,
        "agent_command_configured": bool(provider.agent_command),
        "provider_check": provider_message,
        "index_plan": _index_plan(session, policy),
    }


def _run_start(args: argparse.Namespace) -> int:
    try:
        session = agent_sessions.create_target_session(args.root, scope=args.scope)
        provider = agent_sessions.create_provider_selection(
            args.provider,
            agent_command=args.agent_command,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    policy = agent_sessions.index_policy_from_flags(
        ensure_index=bool(args.ensure_index),
        refresh_index=bool(args.refresh_index),
    )
    provider_message = "provider check skipped"
    if not args.skip_provider_check:
        ok, provider_message = agent_sessions.validate_agent_command(provider)
        if not ok:
            print(f"error: {provider_message}", file=sys.stderr)
            return 2

    if args.check_only:
        if args.json:
            print(
                json.dumps(
                    _check_only_payload(
                        session=session,
                        provider=provider,
                        policy=policy,
                        provider_message=provider_message,
                    ),
                    indent=2,
                )
            )
        else:
            print(provider_message)
            plan = _index_plan(session, policy)
            if plan:
                print(plan)
        return 0

    env = agent_sessions.build_graph_env(
        os.environ.copy(),
        provider,
        graph_token=args.graph_token,
        command_timeout=args.command_timeout,
        max_output_events=args.max_output_events,
    )
    try:
        agent_sessions.prepare_session_index(
            session,
            env,
            policy=policy,
            check_call=subprocess.check_call,
            python_executable=sys.executable,
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"error: index setup failed with exit code {exc.returncode}",
            file=sys.stderr,
        )
        return int(exc.returncode) or 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return agent_sessions.launch_graph_server(
        session,
        env,
        graph=agent_sessions.GraphDefaults(host=args.host, port=str(args.port)),
        quiet=bool(args.quiet),
        call=subprocess.call,
        python_executable=sys.executable,
    )


def run(args: argparse.Namespace) -> int:
    if args.agent_plugin_action == "start":
        return _run_start(args)
    print(f"error: unknown agent-plugin action: {args.agent_plugin_action}", file=sys.stderr)
    return 2
