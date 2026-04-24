from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from code_index.cli import main


def _write_fake_scip_python(tool_dir: Path) -> Path:
    tool_dir.mkdir()
    if os.name == "nt":
        script = tool_dir / "scip-python.cmd"
        script.write_text(
            "@echo off\r\n"
            "echo %*>\"%SCIP_PYTHON_FAKE_LOG%\"\r\n"
            "echo fake-index>index.scip\r\n",
            encoding="utf-8",
        )
    else:
        script = tool_dir / "scip-python"
        script.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" > \"$SCIP_PYTHON_FAKE_LOG\"\n"
            "printf '%s\\n' fake-index > index.scip\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_scip_python_index_missing_tool_is_clean_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PATH", "")

    rc = main(["scip-python-index", "--root", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 2
    assert payload["error"] == "scip-python is not on PATH"
    assert "npm install" in payload["hint"]


def test_scip_python_index_runs_tool_in_sidecar_dir(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    tool_dir = tmp_path / "tools"
    _write_fake_scip_python(tool_dir)
    log_path = tmp_path / "scip-python-args.txt"
    monkeypatch.setenv("SCIP_PYTHON_FAKE_LOG", str(log_path))
    monkeypatch.setenv(
        "PATH", f"{tool_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    rc = main(
        [
            "scip-python-index",
            "--root",
            str(tmp_path),
            "--project-name",
            "sample",
            "--target-only",
            "pkg",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)

    output_path = (
        tmp_path / ".code_index" / "external" / "scip-python" / "index.scip"
    )
    assert rc == 0
    assert output_path.read_text(encoding="utf-8").strip() == "fake-index"
    assert payload["output_path"] == str(output_path)
    assert payload["cwd"] == str(output_path.parent)

    logged_args = log_path.read_text(encoding="utf-8")
    assert "index" in logged_args
    assert str(tmp_path) in logged_args
    assert "--project-name" in logged_args
    assert "sample" in logged_args
    assert "--target-only" in logged_args
    assert "pkg" in logged_args


def test_scip_python_index_import_index_requires_initialized_db(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    tool_dir = tmp_path / "tools"
    _write_fake_scip_python(tool_dir)
    monkeypatch.setenv("SCIP_PYTHON_FAKE_LOG", str(tmp_path / "args.txt"))
    monkeypatch.setenv(
        "PATH", f"{tool_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    rc = main(
        [
            "scip-python-index",
            "--root",
            str(tmp_path),
            "--import-index",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert rc == 2
    assert payload["error"] == "generated SCIP index but import failed"
    assert "run `code_index init`" in payload["import"]["error"]
