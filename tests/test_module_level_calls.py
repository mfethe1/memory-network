"""Module-level `ast.Call` nodes must produce `calls` pending relations
attributed to the module symbol. Pre-Slice-7 they were silently dropped,
which hid roughly 99% of the real call graph on FastAPI-scale repos."""

from __future__ import annotations

import textwrap

from code_index.parsers.python_ast import PythonAstParser


def test_module_level_calls_emit_relations():
    src = textwrap.dedent(
        """
        from fastapi import FastAPI, APIRouter

        app = FastAPI()
        router = APIRouter()
        """
    ).lstrip()
    r = PythonAstParser().parse(rel_path="tutorial.py", source=src)
    call_rels = [pr for pr in r.pending_relations if pr.relation_kind == "calls"]
    # Two module-level instantiations.
    assert len(call_rels) >= 2
    # Both attributed to the module symbol.
    module_sym = next(s for s in r.symbols if s.kind == "module")
    for pr in call_rels:
        assert pr.src_symbol_uid == module_sym.symbol_uid
    flat_candidates = {c for pr in call_rels for c in pr.dst_candidates}
    assert any("FastAPI" in c for c in flat_candidates)
    assert any("APIRouter" in c for c in flat_candidates)
    assert any("module-level" in (pr.provenance or "") for pr in call_rels)


def test_module_level_decorator_invocations_captured():
    src = textwrap.dedent(
        """
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/")
        def root():
            return {}
        """
    ).lstrip()
    r = PythonAstParser().parse(rel_path="pkg/routes.py", source=src)
    call_rels = [pr for pr in r.pending_relations if pr.relation_kind == "calls"]
    # Expect:
    #   1 call for `FastAPI()` at module scope,
    #   1 call for `@app.get("/")` decorator invocation at module scope.
    flat_candidates = {c for pr in call_rels for c in pr.dst_candidates}
    # Decorator resolves via scope + flat fallback.
    assert any(c.endswith("app.get") or c == "app.get" for c in flat_candidates)
    assert any("module-decorator" in (pr.provenance or "") for pr in call_rels)


def test_function_body_calls_not_double_counted():
    """Calls inside a function must NOT also register against the module."""
    src = textwrap.dedent(
        """
        def helper():
            return 1

        def caller():
            return helper()
        """
    ).lstrip()
    r = PythonAstParser().parse(rel_path="mod.py", source=src)
    call_rels = [pr for pr in r.pending_relations if pr.relation_kind == "calls"]
    module_sym = next(s for s in r.symbols if s.kind == "module")
    caller_sym = next(s for s in r.symbols if s.canonical_name == "mod.caller")
    # `helper()` call must attribute to caller, NOT module.
    helper_calls = [
        pr for pr in call_rels if any("helper" in c for c in pr.dst_candidates)
    ]
    assert helper_calls
    for pr in helper_calls:
        assert pr.src_symbol_uid == caller_sym.symbol_uid
        assert pr.src_symbol_uid != module_sym.symbol_uid
