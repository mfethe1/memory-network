"""Natural-language query synthesis: classifier + dispatcher + narrative."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index.nl import answer
from code_index.nl.classify import Intent, IntentKind, classify
from code_index.pipeline import reindex


# -- Classifier --------------------------------------------------------------


@pytest.mark.parametrize(
    "q, expected_kind, expected_target",
    [
        ("where is reindex", IntentKind.WHERE, "reindex"),
        ("where is `pkg.mod.Foo` defined", IntentKind.WHERE, "pkg.mod.Foo"),
        ("find the definition of FastAPI", IntentKind.WHERE, "FastAPI"),
        ("who calls reindex", IntentKind.CALLERS, "reindex"),
        ("what uses Widget", IntentKind.CALLERS, "Widget"),
        ("callers of run", IntentKind.CALLERS, "run"),
        ("what breaks if I change Config", IntentKind.IMPACT, "Config"),
        ("blast radius of apply_schema", IntentKind.IMPACT, "apply_schema"),
        ("what depends on pipeline.reindex", IntentKind.IMPACT, "pipeline.reindex"),
        ("which tests cover reindex", IntentKind.TESTS, "reindex"),
        ("tests for Foo", IntentKind.TESTS, "Foo"),
        ("affected tests for update", IntentKind.TESTS, "update"),
        ("call sites of reindex", IntentKind.REFERENCES, "reindex"),
        (
            "references for _apply_parsed_file",
            IntentKind.REFERENCES,
            "_apply_parsed_file",
        ),
        ("find all classes", IntentKind.STRUCTURAL, "classes"),
        ("find all functions", IntentKind.STRUCTURAL, "functions"),
        ("grep for TODO", IntentKind.LITERAL, "TODO"),
        ("is the index healthy", IntentKind.HEALTH, None),
        ("doctor", IntentKind.HEALTH, None),
        ("give me a tour", IntentKind.OVERVIEW, None),
        ("repo map", IntentKind.OVERVIEW, None),
    ],
)
def test_classifier_happy_paths(q, expected_kind, expected_target):
    intent = classify(q)
    assert intent.kind == expected_kind, (q, intent)
    assert intent.target == expected_target, (q, intent)
    assert intent.confidence > 0.0


def test_classifier_falls_back_to_unknown_with_reason():
    intent = classify("hello there friend")
    assert intent.kind == IntentKind.UNKNOWN
    assert intent.unknown_reason


def test_classifier_extracts_language_hint():
    intent = classify("find all classes in python")
    assert intent.language == "python"


def test_classifier_extracts_top_n_hint():
    intent = classify("find all functions top 5")
    assert intent.limit == 5


def test_similar_intent_captures_free_form_phrase():
    intent = classify("find code like jwt expiry validation")
    assert intent.kind == IntentKind.SIMILAR
    assert intent.target
    assert "jwt" in intent.target.lower()


# -- End-to-end dispatcher ---------------------------------------------------


def _build_small_repo(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "util.py").write_text(
        textwrap.dedent(
            """
            def helper(x):
                return x + 1
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "service.py").write_text(
        textwrap.dedent(
            """
            from pkg.util import helper

            def run():
                return helper(42)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_service.py").write_text(
        textwrap.dedent(
            """
            from pkg.service import run
            def test_run():
                assert run() == 43
            """
        ).lstrip(),
        encoding="utf-8",
    )


def _init(tmp_path: Path):
    config = cfg_mod.load(tmp_path)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    return config, conn


def test_answer_where_returns_def_file(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "where is helper")
        assert bundle["primary_tool"] == "symbol"
        assert bundle["intent"]["kind"] == IntentKind.WHERE.value
        results = bundle["results"]["results"]
        assert any(r["canonical_name"].endswith(".helper") for r in results)
        assert (
            "helper" in bundle["narrative"].lower() or "pkg.util" in bundle["narrative"]
        )
    finally:
        db_mod.close(conn)


def test_answer_callers_uses_impact_depth_1(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "who calls helper")
        assert bundle["primary_tool"] == "impact"
        r = bundle["results"]
        assert r["parameters"]["max_depth"] == 1
        names = [s["canonical_name"] for s in r.get("impacted_symbols", [])]
        assert any("pkg.service.run" in n for n in names), names
    finally:
        db_mod.close(conn)


def test_answer_tests_returns_runner_invocation(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "tests for helper")
        assert bundle["primary_tool"] == "tests"
        assert bundle["results"].get("runner", {}).get("node_ids") is not None
    finally:
        db_mod.close(conn)


def test_answer_health_returns_doctor_data(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "is the index healthy")
        assert bundle["primary_tool"] == "doctor"
        assert "relations" in bundle["results"]
        assert "fts_consistency" in bundle["results"]
    finally:
        db_mod.close(conn)


def test_answer_overview_returns_repo_map(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "give me a tour of this repo")
        assert bundle["primary_tool"] == "repo-map"
        assert isinstance(bundle["results"].get("symbols"), list)
    finally:
        db_mod.close(conn)


def test_answer_unknown_question_falls_back_with_suggestions(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "what is the best taco filling")
        assert bundle["intent"]["kind"] == IntentKind.UNKNOWN.value
        assert bundle["suggestions"], "unknown fallback must suggest concrete patterns"
    finally:
        db_mod.close(conn)


def test_answer_literal_grep_uses_lexical(tmp_path: Path):
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "grep for helper")
        assert bundle["primary_tool"] == "grep"
        # Engine may be ripgrep or python-re; both work.
        assert "engine" in bundle["results"]
    finally:
        db_mod.close(conn)


def test_bundle_shape_is_stable(tmp_path: Path):
    """Stable JSON contract for MCP / agent consumers."""
    _build_small_repo(tmp_path)
    config, conn = _init(tmp_path)
    try:
        reindex(conn, config, paths=None, event_source="init")
        bundle = answer(config, conn, "where is helper")
        for key in (
            "question",
            "intent",
            "primary_tool",
            "supporting_tools",
            "results",
            "narrative",
            "suggestions",
            "limitations",
        ):
            assert key in bundle, f"missing key {key}"
        for key in ("kind", "confidence", "target", "rationale"):
            assert key in bundle["intent"], f"missing intent.{key}"
    finally:
        db_mod.close(conn)
