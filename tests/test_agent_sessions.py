from __future__ import annotations

from pathlib import Path

import pytest


def test_target_session_resolves_index_root_and_scope(tmp_path: Path):
    from code_index.agent_sessions import create_target_session

    repo = tmp_path / "repo"
    scope = repo / "packages" / "api"
    (repo / ".code_index").mkdir(parents=True)
    scope.mkdir(parents=True)

    session = create_target_session(scope)

    assert session.root == repo.resolve()
    assert session.scope == Path("packages/api")


def test_target_session_separates_explicit_root_and_scope(tmp_path: Path):
    from code_index.agent_sessions import create_target_session, graph_server_command

    repo = tmp_path / "repo"
    scope = repo / "packages" / "api"
    (repo / ".code_index").mkdir(parents=True)
    scope.mkdir(parents=True)

    session = create_target_session(repo, scope="packages/api")

    assert session.root == repo.resolve()
    assert session.scope == Path("packages/api")
    command = graph_server_command(session, python_executable="python-test")
    assert command[:5] == [
        "python-test",
        "-m",
        "code_index",
        "graph-server",
        "--root",
    ]
    assert "--scope" in command
    assert command[command.index("--scope") + 1] == "packages/api"


def test_target_session_uses_unindexed_directory_as_root(tmp_path: Path):
    from code_index.agent_sessions import create_target_session

    target = tmp_path / "external-repo"
    target.mkdir()

    session = create_target_session(target)

    assert session.root == target.resolve()
    assert session.scope == Path(".")


def test_ensure_index_policy_initializes_missing_index(tmp_path: Path):
    from code_index.agent_sessions import (
        IndexPolicy,
        create_target_session,
        prepare_session_index,
    )

    session = create_target_session(tmp_path)
    calls: list[tuple[list[str], str, dict[str, str]]] = []

    def fake_check_call(command, *, cwd, env):
        calls.append((command, cwd, env))

    prepare_session_index(
        session,
        {"PYTHONPATH": "src"},
        policy=IndexPolicy.ENSURE,
        check_call=fake_check_call,
        python_executable="python-test",
    )

    assert calls == [
        (
            [
                "python-test",
                "-m",
                "code_index",
                "init",
                "--root",
                str(tmp_path.resolve()),
                "--json",
            ],
            str(tmp_path.resolve()),
            {"PYTHONPATH": "src"},
        )
    ]


def test_refresh_index_policy_updates_existing_index(tmp_path: Path):
    from code_index.agent_sessions import (
        IndexPolicy,
        create_target_session,
        prepare_session_index,
    )

    (tmp_path / ".code_index").mkdir()
    (tmp_path / ".code_index" / "index.db").write_text("", encoding="utf-8")
    session = create_target_session(tmp_path)
    calls: list[list[str]] = []

    def fake_check_call(command, *, cwd, env):
        calls.append(command)

    prepare_session_index(
        session,
        {},
        policy=IndexPolicy.REFRESH,
        check_call=fake_check_call,
        python_executable="python-test",
    )

    assert calls == [
        [
            "python-test",
            "-m",
            "code_index",
            "update",
            "--root",
            str(tmp_path.resolve()),
            "--all",
            "--json",
        ]
    ]


def test_no_index_policy_rejects_missing_index_without_running_commands(tmp_path: Path):
    from code_index.agent_sessions import (
        IndexPolicy,
        create_target_session,
        prepare_session_index,
    )

    session = create_target_session(tmp_path)
    calls: list[list[str]] = []

    def fake_check_call(command, *, cwd, env):
        calls.append(command)

    with pytest.raises(ValueError, match="no index"):
        prepare_session_index(
            session,
            {},
            policy=IndexPolicy.NO_INDEX,
            check_call=fake_check_call,
        )

    assert calls == []
