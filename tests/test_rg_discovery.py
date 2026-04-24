"""Robust ripgrep discovery tests.

Focus: resolver fallbacks and the resolution trail. These tests do not
require a real rg binary — they mock subprocess.run via monkeypatch.
"""

from __future__ import annotations

from unittest.mock import patch

from code_index.search import rg_discovery


def test_resolver_returns_missing_when_nothing_works(monkeypatch):
    # No config, no env var. Force `shutil.which` to return None and fail
    # every subprocess invocation.
    monkeypatch.delenv("CODE_INDEX_RG", raising=False)
    with (
        patch("code_index.search.rg_discovery.shutil.which", return_value=None),
        patch(
            "code_index.search.rg_discovery.subprocess.run",
            side_effect=OSError("no rg"),
        ),
    ):
        result = rg_discovery.resolve()
    assert result.path is None
    assert result.source == "missing"
    # At minimum: rg + rg.exe probes via which + the candidate list.
    sources = {entry["source"] for entry in result.tried}
    # rg.exe/rg via which aren't recorded when which returns None, only candidates.
    assert "candidate" in sources


def test_resolver_prefers_explicit_config(monkeypatch):
    monkeypatch.delenv("CODE_INDEX_RG", raising=False)
    with patch(
        "code_index.search.rg_discovery._probe",
        side_effect=lambda p: "ripgrep 14.9.0" if p == "/custom/rg" else None,
    ):
        result = rg_discovery.resolve(config_rg_path="/custom/rg")
    assert result.path == "/custom/rg"
    assert result.source == "config"
    assert result.version.startswith("ripgrep")


def test_resolver_env_var(monkeypatch):
    monkeypatch.setenv("CODE_INDEX_RG", "/env/rg")
    with patch(
        "code_index.search.rg_discovery._probe",
        side_effect=lambda p: "ripgrep 14.8.0" if p == "/env/rg" else None,
    ):
        result = rg_discovery.resolve()
    assert result.path == "/env/rg"
    assert result.source == "env"


def test_resolver_records_full_trail(monkeypatch):
    monkeypatch.delenv("CODE_INDEX_RG", raising=False)
    with (
        patch(
            "code_index.search.rg_discovery.shutil.which",
            side_effect=lambda n: None,
        ),
        patch(
            "code_index.search.rg_discovery._probe",
            return_value=None,
        ),
    ):
        result = rg_discovery.resolve()
    assert result.path is None
    # Each `tried` entry must include source, candidate, and ok.
    for entry in result.tried:
        assert set(entry).issuperset({"source", "candidate", "ok"})
