"""Claude Code hook coverage for Edit/Write/MultiEdit payloads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "reindex-after-edit.sh"
VERIFY_HOOK = ROOT / ".claude" / "hooks" / "verify-claim-before-edit.sh"


def _bash_path() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # Prefer Git Bash on Windows. The WSL launcher at C:\Windows\System32\bash.exe
    # strips backslashes from a native Windows path when invoked this way, so it
    # cannot execute E:\...\reindex-after-edit.sh directly.
    candidates = [
        shutil.which("bash", path=os.environ.get("PATH", "")),
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        if "Windows\\System32" in candidate:
            continue
        return candidate
    return None


def _run_hook(
    payload: dict, *, hook: Path = HOOK, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    bash = _bash_path()
    assert bash is not None, "Git Bash is required for hook tests on Windows"
    env = os.environ.copy()
    env["CODE_INDEX_DRY_RUN"] = "1"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [bash, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )


def test_hook_extracts_edit_file_path():
    proc = _run_hook({"tool_input": {"file_path": "code_index/pipeline.py"}})

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "python -m code_index update --files code_index/pipeline.py --json\n"
    )


def test_hook_extracts_multiedit_top_level_file_path():
    proc = _run_hook(
        {
            "tool_input": {
                "file_path": "code_index/pipeline.py",
                "edits": [{"old_string": "a", "new_string": "b"}],
            }
        }
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "python -m code_index update --files code_index/pipeline.py --json\n"
    )


def test_hook_extracts_multiedit_per_edit_file_paths_once():
    proc = _run_hook(
        {
            "tool_input": {
                "edits": [
                    {"file_path": "a.py", "old_string": "a", "new_string": "b"},
                    {"file_path": "b.py", "old_string": "c", "new_string": "d"},
                    {"file_path": "a.py", "old_string": "e", "new_string": "f"},
                ]
            }
        }
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "python -m code_index update --files a.py b.py --json\n"


def test_hook_ignores_generated_multiedit_paths():
    proc = _run_hook(
        {
            "tool_input": {
                "edits": [
                    {
                        "file_path": ".claude/settings.json",
                        "old_string": "a",
                        "new_string": "b",
                    },
                    {
                        "file_path": "pkg/good.py",
                        "old_string": "c",
                        "new_string": "d",
                    },
                    {
                        "file_path": "pkg/__pycache__/bad.pyc",
                        "old_string": "e",
                        "new_string": "f",
                    },
                ]
            }
        }
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "python -m code_index update --files pkg/good.py --json\n"


def test_hook_exits_silently_when_no_paths_are_present():
    proc = _run_hook({"tool_input": {"edits": [{"old_string": "a"}]}})

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_verify_claim_hook_is_silent_without_supervised_run():
    proc = _run_hook(
        {"tool_input": {"file_path": "code_index/pipeline.py"}},
        hook=VERIFY_HOOK,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_verify_claim_hook_emits_claim_check_for_supervised_write():
    proc = _run_hook(
        {"tool_input": {"file_path": "code_index/pipeline.py"}},
        hook=VERIFY_HOOK,
        extra_env={
            "CODE_INDEX_AGENT_RUN_ID": "run-123",
            "CODE_INDEX_AGENT_FENCE": "7",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "python -m code_index agent verify-claim --run-id run-123 "
        "--file code_index/pipeline.py --fence 7\n"
    )


def test_verify_claim_hook_uses_per_file_fence_map():
    proc = _run_hook(
        {
            "tool_input": {
                "edits": [
                    {"file_path": "a.py", "old_string": "a", "new_string": "b"},
                    {"file_path": "b.py", "old_string": "c", "new_string": "d"},
                ]
            }
        },
        hook=VERIFY_HOOK,
        extra_env={
            "CODE_INDEX_AGENT_RUN_ID": "run-123",
            "CODE_INDEX_AGENT_FENCES": json.dumps({"a.py": 1, "b.py": 2}),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == (
        "python -m code_index agent verify-claim --run-id run-123 --file a.py --fence 1\n"
        "python -m code_index agent verify-claim --run-id run-123 --file b.py --fence 2\n"
    )


def test_verify_claim_hook_fails_when_supervised_fence_is_missing():
    proc = _run_hook(
        {"tool_input": {"file_path": "code_index/pipeline.py"}},
        hook=VERIFY_HOOK,
        extra_env={"CODE_INDEX_AGENT_RUN_ID": "run-123"},
    )

    assert proc.returncode == 2
    assert "missing fence token" in proc.stderr
