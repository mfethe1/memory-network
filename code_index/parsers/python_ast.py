"""Native Python AST parser.

Native compiler-backed semantic source: highest confidence per the spec's
priority order when parsing Python in v1. Emits:

- symbols: module, class, function, method
- occurrences: definition + import (as import references)
- relations: contains (class -> method, module -> class/function), imports,
  calls (function/method -> callee), inherits (class -> base)
- chunks: module + class + function/method (no inner-block splits in v1)

Known limits (tracked, not bugs):
- Does not follow imports or resolve cross-file references beyond the name.
- Calls and inheritance use best-effort scope resolution; dynamic dispatch
  (e.g., getattr, method passed as variable) is not tracked.
- Decorators stored as source slices, not resolved to symbols.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from code_index.hashing import normalized_hash, raw_hash
from code_index.parsers.base import (
    ChunkDraft,
    DiagnosticDraft,
    OccurrenceDraft,
    ParseResult,
    PendingRelation,
    RelationDraft,
    SymbolDraft,
)
from code_index.symbols import SymbolIdentity, make_chunk_uid, normalize_signature

PYTHON_EXTS = (".py", ".pyi")


def _module_name_from_path(rel_path: str) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    if not parts:
        return rel_path
    last = parts[-1]
    if last.endswith(".pyi"):
        last = last[:-4]
    elif last.endswith(".py"):
        last = last[:-3]
    parts[-1] = last
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p)


def _slice_source(source_lines: list[str], node: ast.AST) -> tuple[str, int, int]:
    start = getattr(node, "lineno", 1) or 1
    end = getattr(node, "end_lineno", start) or start
    start = max(start, 1)
    end = max(end, start)
    start_idx = start - 1
    end_idx = min(end, len(source_lines))
    content = "\n".join(source_lines[start_idx:end_idx])
    if content and not content.endswith("\n"):
        content += "\n"
    return content, start, end


def _decorators(node: ast.AST, source_lines: list[str]) -> list[str]:
    raw = getattr(node, "decorator_list", []) or []
    out: list[str] = []
    for dec in raw:
        try:
            out.append(ast.unparse(dec))
        except Exception:
            deco_src, _, _ = _slice_source(source_lines, dec)
            out.append(deco_src.strip())
    return out


def _signature_of(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        try:
            args = ast.unparse(node.args)
        except Exception:
            args = ""
        returns = ""
        if node.returns is not None:
            try:
                returns = f" -> {ast.unparse(node.returns)}"
            except Exception:
                returns = ""
        prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
        return f"{prefix}{node.name}({args}){returns}"
    if isinstance(node, ast.ClassDef):
        bases: list[str] = []
        for base in node.bases:
            try:
                bases.append(ast.unparse(base))
            except Exception:
                bases.append("?")
        base_sfx = f"({', '.join(bases)})" if bases else ""
        return f"class {node.name}{base_sfx}"
    return ""


def _docstring_summary(node: ast.AST) -> str | None:
    if not isinstance(
        node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        return None
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return None
    first = doc.strip().splitlines()[0]
    return first[:240]


def _extract_parametrize(node: ast.AST) -> dict[str, Any] | None:
    """Return a compact summary of @pytest.mark.parametrize on a test function.

    Output shape (None when no parametrize decorator is present):
      {
        "argnames": ["a", "b"],
        "case_count": 3,
        "cases": ["(1, 2)", "(3, 4)", "(5, 6)"],   # truncated to 16 cases
      }

    We intentionally do NOT construct pytest's node-id style (`test_foo[1-2]`)
    because that requires running pytest collection. This grouped
    representation is the honest best-effort fallback.
    """
    decorators = getattr(node, "decorator_list", []) or []
    for dec in decorators:
        call = dec
        if not isinstance(call, ast.Call):
            continue
        # We recognise:  @pytest.mark.parametrize(...)
        #            or  @parametrize(...)  (aliased import)
        flat = _flatten_attribute(call.func)
        if flat is None and isinstance(call.func, ast.Name):
            flat = [call.func.id]
        if not flat:
            continue
        name = flat[-1]
        if name != "parametrize":
            continue
        if len(call.args) < 2:
            continue
        argnames_node, values_node = call.args[0], call.args[1]
        argnames: list[str] = []
        try:
            if isinstance(argnames_node, ast.Constant) and isinstance(
                argnames_node.value, str
            ):
                # "a,b,c" or "a, b, c"
                argnames = [
                    piece.strip()
                    for piece in argnames_node.value.split(",")
                    if piece.strip()
                ]
            elif isinstance(argnames_node, (ast.List, ast.Tuple)):
                argnames = [
                    elt.value
                    for elt in argnames_node.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
        except Exception:
            argnames = []
        cases: list[str] = []
        case_count = 0
        if isinstance(values_node, (ast.List, ast.Tuple)):
            for elt in values_node.elts:
                case_count += 1
                if len(cases) >= 16:
                    continue
                try:
                    cases.append(ast.unparse(elt))
                except Exception:
                    cases.append("?")
        else:
            # Not a literal list — give up on per-case identity.
            try:
                case_count = 0
                cases.append(ast.unparse(values_node))
            except Exception:
                cases.append("?")

        # Capture custom ids= kwarg. Pytest accepts list[str] or a callable.
        # We surface only the literal-list form; callables mean "generated at
        # collection time" and we don't run collection.
        explicit_ids: list[str] | None = None
        ids_callable = False
        for kw in getattr(call, "keywords", []) or []:
            if kw.arg != "ids":
                continue
            if isinstance(kw.value, (ast.List, ast.Tuple)):
                literal: list[str] = []
                all_literal = True
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(
                        elt.value, (str, int, float, bool)
                    ):
                        literal.append(str(elt.value))
                    else:
                        all_literal = False
                        break
                if all_literal:
                    explicit_ids = literal[:16]
            else:
                # Either `ids=name_func` (callable reference) or a non-literal
                # expression we can't safely render.
                ids_callable = True
            break

        return {
            "argnames": argnames,
            "case_count": case_count,
            "cases": cases,
            "truncated": case_count > len(cases),
            "ids": explicit_ids,
            "ids_callable": ids_callable,
        }
    return None


@dataclass
class _Ctx:
    module_name: str
    rel_path: str
    source_lines: list[str]
    imports: list[dict[str, Any]]
    symbols: list[SymbolDraft]
    occurrences: list[OccurrenceDraft]
    relations: list[RelationDraft]
    pending_relations: list[PendingRelation]
    chunks: list[ChunkDraft]
    diagnostics: list[DiagnosticDraft]
    scope: dict[str, list[str]]  # name → ordered canonical-name candidates
    chunk_occurrences: dict[tuple[str, str, str], int]


def _next_chunk_occurrence(
    ctx: _Ctx,
    *,
    chunk_type: str,
    symbol_uid: str,
    symbol_path: str,
) -> int:
    key = (chunk_type, symbol_uid, symbol_path)
    occurrence = ctx.chunk_occurrences.get(key, 0)
    ctx.chunk_occurrences[key] = occurrence + 1
    return occurrence


def _new_symbol(
    *,
    kind: str,
    name: str,
    canonical_name: str,
    signature: str,
    container_uid: str,
    node: ast.AST,
    source_lines: list[str],
) -> SymbolDraft:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    start_byte = getattr(node, "col_offset", None)
    end_byte = getattr(node, "end_col_offset", None)
    sig_norm = normalize_signature(signature)
    ident = SymbolIdentity(
        language="python",
        kind=kind,
        canonical_name=canonical_name,
        signature_norm=sig_norm,
        container_uid=container_uid,
    )
    return SymbolDraft(
        symbol_uid=ident.symbol_uid,
        kind=kind,
        canonical_name=canonical_name,
        display_name=name,
        signature=signature or None,
        signature_norm=sig_norm,
        container_uid=container_uid or None,
        start_line=start,
        end_line=end,
        start_byte=start_byte,
        end_byte=end_byte,
        confidence=0.95,
        semantic_source="python-ast",
        language="python",
    )


def _resolve_relative_module(
    level: int, module: str | None, self_module: str, *, is_package: bool = False
) -> str | None:
    """Resolve `from ..pkg import X` given the parsing file's module path.

    `level` is the leading-dot count (0 for absolute imports). `module` may
    be None for `from .. import X` (in which case the base is the resolved
    package).

    `is_package` is True when the parsing file is `__init__.py`: Python's
    `__package__` for `__init__.py` equals its own module name, so
    `from . import x` at level=1 means "within this package" and must NOT
    drop a segment. Regular modules drop `level` segments (level=1 goes to
    the containing package).

    Returns the fully qualified base module, or None if the level pops
    past the package root (malformed relative import).
    """
    if level <= 0:
        return module
    parts = self_module.split(".") if self_module else []
    # For __init__.py, self_module already names the package. level=1 means
    # the package itself (drop 0 segments); level=2 means the parent package
    # (drop 1 segment). Regular modules are one level deeper.
    drop = (level - 1) if is_package else level
    if drop > len(parts):
        return None
    package_parts = parts[: len(parts) - drop] if drop > 0 else list(parts)
    if module:
        package_parts = package_parts + module.split(".")
    return ".".join(package_parts) if package_parts else None


def _collect_imports(
    tree: ast.Module, self_module: str, *, is_package: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(
                    {
                        "kind": "import",
                        "module": alias.name,
                        "asname": alias.asname,
                        "line": node.lineno,
                        "level": 0,
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            resolved_base = _resolve_relative_module(
                level, node.module, self_module, is_package=is_package
            )
            for alias in node.names:
                out.append(
                    {
                        "kind": "import_from",
                        "module": resolved_base or "",
                        "raw_module": node.module,
                        "level": level,
                        "name": alias.name,
                        "asname": alias.asname,
                        "line": node.lineno,
                    }
                )
    return out


def _flatten_attribute(node: ast.AST) -> list[str] | None:
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return list(reversed(parts))
    return None


def _resolve_callee(
    func: ast.AST,
    scope: dict[str, list[str]],
    *,
    module_name: str,
    class_qual: str | None,
) -> list[str]:
    """Return ordered canonical-name candidates for a call's callee expression.

    Precision tiers, preferred first:
      1. self./cls. → current class method
      2. ClassName.method() inside that class body → class-qualified
      3. scope lookup (imports + top-level names)
      4. same-module best guess (module.<name>)
      5. raw dotted path as given
    """
    candidates: list[str] = []
    if isinstance(func, ast.Name):
        name = func.id
        if name in scope:
            candidates.extend(scope[name])
        candidates.append(f"{module_name}.{name}")
        return _dedup(candidates)
    if isinstance(func, ast.Attribute):
        parts = _flatten_attribute(func)
        if parts is None:
            return []
        head, *rest = parts
        tail = ".".join(rest)
        if head in {"self", "cls"} and class_qual:
            return [f"{class_qual}.{tail}"]
        # ClassName.method() inside ClassName's body — common pattern in
        # @classmethod / staticmethod usage. class_qual ends with the class
        # name; if the head matches, treat it as an internal reference.
        if class_qual and rest and head == class_qual.rsplit(".", 1)[-1]:
            candidates.append(f"{class_qual}.{tail}")
        if head in scope and rest:
            for base in scope[head]:
                candidates.append(f"{base}.{tail}")
        candidates.append(".".join(parts))
        return _dedup(candidates)
    return []


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _iter_calls_shallow(node: ast.AST):
    """Yield ast.Call nodes reachable from `node` without descending into
    nested function/class definitions (they get their own pass)."""
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _iter_calls_shallow(child)


def _scope_from_module(
    tree: ast.Module,
    imports: list[dict[str, Any]],
    module_name: str,
) -> dict[str, list[str]]:
    scope: dict[str, list[str]] = {}

    def _add(name: str, canonical: str) -> None:
        scope.setdefault(name, []).append(canonical)

    for imp in imports:
        if imp["kind"] == "import":
            mod = imp["module"]
            if not mod:
                continue
            asname = imp.get("asname") or mod.split(".")[0]
            _add(asname, mod)
        elif imp["kind"] == "import_from":
            base = imp.get("module") or ""
            name = imp["name"]
            if not base:
                # `from . import X` at package root that couldn't resolve,
                # or malformed relative. Best-effort: use the name as-is so
                # the unresolved-relations table can keep retrying on backfill.
                asname = imp.get("asname") or name
                _add(asname, name)
                continue
            asname = imp.get("asname") or name
            if name == "*":
                # Star import: record the module itself as the scope source.
                _add(base.rsplit(".", 1)[-1], base)
                continue
            _add(asname, f"{base}.{name}")
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _add(node.name, f"{module_name}.{node.name}")
    return scope


def _walk(
    nodes: list[ast.AST],
    *,
    ctx: _Ctx,
    parent_qual: str,
    parent_uid: str,
    parent_kind: str,
) -> None:
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            qual = f"{parent_qual}.{name}" if parent_qual else name
            signature = _signature_of(node)
            if isinstance(node, ast.ClassDef):
                kind = "class"
            else:
                kind = "method" if parent_kind == "class" else "function"
            sym = _new_symbol(
                kind=kind,
                name=name,
                canonical_name=qual,
                signature=signature,
                container_uid=parent_uid,
                node=node,
                source_lines=ctx.source_lines,
            )
            ctx.symbols.append(sym)
            ctx.occurrences.append(
                OccurrenceDraft(
                    symbol_uid=sym.symbol_uid,
                    role="definition",
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    syntax_kind=kind,
                )
            )
            if parent_uid:
                ctx.relations.append(
                    RelationDraft(
                        src_symbol_uid=parent_uid,
                        dst_symbol_uid=sym.symbol_uid,
                        relation_kind="contains",
                        provenance="python-ast",
                    )
                )
            content, start_line, end_line = _slice_source(ctx.source_lines, node)
            byte_content = content.encode("utf-8", "replace")
            context = {
                "parser": "python-ast",
                "module": ctx.module_name,
                "parent_symbol_path": parent_qual or None,
                "decorators": _decorators(node, ctx.source_lines),
                "docstring_summary": _docstring_summary(node),
                "imports": ctx.imports,
            }
            if isinstance(node, ast.ClassDef):
                context["bases"] = [_safe_unparse(b) for b in node.bases]
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                param_summary = _extract_parametrize(node)
                if param_summary is not None:
                    context["parametrize"] = param_summary
            chunk_uid = make_chunk_uid(
                file_path=ctx.rel_path,
                chunk_type=kind,
                symbol_uid=sym.symbol_uid,
                symbol_path=qual,
                occurrence_index=_next_chunk_occurrence(
                    ctx,
                    chunk_type=kind,
                    symbol_uid=sym.symbol_uid,
                    symbol_path=qual,
                ),
            )
            ctx.chunks.append(
                ChunkDraft(
                    chunk_uid=chunk_uid,
                    chunk_type=kind,
                    symbol_uid=sym.symbol_uid,
                    symbol_name=name,
                    symbol_path=qual,
                    parent_symbol_path=parent_qual or None,
                    signature=signature or None,
                    start_line=start_line,
                    end_line=end_line,
                    start_byte=None,
                    end_byte=len(byte_content),
                    content=content,
                    raw_hash=raw_hash(content),
                    normalized_hash=normalized_hash(content),
                    context=context,
                )
            )
            if isinstance(node, ast.ClassDef):
                # Inheritance edges: class → each declared base.
                for base in node.bases:
                    # Unwrap subscripted generics: Mapping[str, int] → Mapping.
                    base_expr = base.value if isinstance(base, ast.Subscript) else base
                    if isinstance(base_expr, ast.Name):
                        base_parts = [base_expr.id]
                    else:
                        base_parts = _flatten_attribute(base_expr)
                    if not base_parts:
                        continue
                    head = base_parts[0]
                    tail = ".".join(base_parts[1:])
                    candidates: list[str] = []
                    if head in ctx.scope:
                        for base_can in ctx.scope[head]:
                            candidates.append(
                                f"{base_can}.{tail}" if tail else base_can
                            )
                    candidates.append(".".join(base_parts))
                    ctx.pending_relations.append(
                        PendingRelation(
                            src_symbol_uid=sym.symbol_uid,
                            relation_kind="inherits",
                            dst_candidates=_dedup(candidates),
                            provenance="python-ast",
                            site_line=getattr(base, "lineno", None),
                        )
                    )
                _walk(
                    list(node.body),
                    ctx=ctx,
                    parent_qual=qual,
                    parent_uid=sym.symbol_uid,
                    parent_kind="class",
                )
            else:
                # Call edges: every ast.Call inside this function body, not
                # descending into nested defs (they get their own pass).
                class_qual = parent_qual if parent_kind == "class" else None
                for call_node in _iter_calls_shallow(node):
                    if not isinstance(call_node, ast.Call):
                        continue
                    candidates = _resolve_callee(
                        call_node.func,
                        ctx.scope,
                        module_name=ctx.module_name,
                        class_qual=class_qual,
                    )
                    if not candidates:
                        continue
                    ctx.pending_relations.append(
                        PendingRelation(
                            src_symbol_uid=sym.symbol_uid,
                            relation_kind="calls",
                            dst_candidates=candidates,
                            provenance="python-ast",
                            site_line=getattr(call_node, "lineno", None),
                        )
                    )
                _walk(
                    list(node.body),
                    ctx=ctx,
                    parent_qual=qual,
                    parent_uid=sym.symbol_uid,
                    parent_kind="function",
                )


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


class PythonAstParser:
    name = "python-ast"
    language = "python"
    confidence = 0.95

    def supports(self, rel_path: str) -> bool:
        low = rel_path.lower()
        return any(low.endswith(ext) for ext in PYTHON_EXTS)

    def parse(self, *, rel_path: str, source: str) -> ParseResult:
        if not source.strip():
            return ParseResult(
                language="python",
                semantic_source="python-ast",
                confidence=self.confidence,
                parse_status="empty",
            )
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            return ParseResult(
                language="python",
                semantic_source="python-ast",
                confidence=self.confidence,
                parse_status="failed",
                parse_error=f"SyntaxError: {exc.msg} (line {exc.lineno})",
                diagnostics=[
                    DiagnosticDraft(
                        tool="python-ast",
                        severity="error",
                        code="syntax-error",
                        message=str(exc.msg or "syntax error"),
                        start_line=exc.lineno,
                        end_line=exc.end_lineno or exc.lineno,
                    )
                ],
            )

        source_lines = source.splitlines()
        module_name = _module_name_from_path(rel_path) or rel_path
        module_sig = f"module {module_name}"
        module_sym = _new_symbol(
            kind="module",
            name=module_name.rsplit(".", 1)[-1] or module_name,
            canonical_name=module_name,
            signature=module_sig,
            container_uid="",
            node=tree,
            source_lines=source_lines,
        )
        is_package_init = rel_path.replace("\\", "/").endswith(
            "/__init__.py"
        ) or rel_path in ("__init__.py",)
        imports = _collect_imports(tree, module_name, is_package=is_package_init)
        scope = _scope_from_module(tree, imports, module_name)
        ctx = _Ctx(
            module_name=module_name,
            rel_path=rel_path,
            source_lines=source_lines,
            imports=imports,
            symbols=[module_sym],
            occurrences=[
                OccurrenceDraft(
                    symbol_uid=module_sym.symbol_uid,
                    role="definition",
                    start_line=1,
                    end_line=len(source_lines) or 1,
                    syntax_kind="module",
                )
            ],
            relations=[],
            pending_relations=[],
            chunks=[],
            diagnostics=[],
            scope=scope,
            chunk_occurrences={},
        )
        # Emit pending 'imports' relations from module → each imported target.
        for imp in imports:
            if imp["kind"] == "import":
                mod = imp["module"]
                ctx.pending_relations.append(
                    PendingRelation(
                        src_symbol_uid=module_sym.symbol_uid,
                        relation_kind="imports",
                        dst_candidates=[mod],
                        provenance="python-ast",
                        site_line=imp.get("line"),
                    )
                )
            elif imp["kind"] == "import_from":
                # `module` is already resolved against the file's package for
                # relative imports (see _resolve_relative_module).
                base = imp.get("module") or ""
                if not base:
                    continue
                name = imp.get("name") or ""
                if name == "*":
                    candidates = [base]
                else:
                    candidates = [f"{base}.{name}", base]
                prov = "python-ast"
                if imp.get("level"):
                    prov = f"python-ast;relative=level{imp['level']}"
                ctx.pending_relations.append(
                    PendingRelation(
                        src_symbol_uid=module_sym.symbol_uid,
                        relation_kind="imports",
                        dst_candidates=_dedup(candidates),
                        provenance=prov,
                        site_line=imp.get("line"),
                    )
                )
        module_chunk_uid = make_chunk_uid(
            file_path=rel_path,
            chunk_type="module",
            symbol_uid=module_sym.symbol_uid,
            symbol_path=module_name,
        )
        module_byte_len = len(source.encode("utf-8", "replace"))
        ctx.chunks.append(
            ChunkDraft(
                chunk_uid=module_chunk_uid,
                chunk_type="module",
                symbol_uid=module_sym.symbol_uid,
                symbol_name=module_sym.display_name,
                symbol_path=module_name,
                parent_symbol_path=None,
                signature=module_sig,
                start_line=1,
                end_line=len(source_lines) or 1,
                start_byte=0,
                end_byte=module_byte_len,
                content=source if source.endswith("\n") else source + "\n",
                raw_hash=raw_hash(source),
                normalized_hash=normalized_hash(source),
                context={
                    "parser": "python-ast",
                    "module": module_name,
                    "imports": imports,
                    "docstring_summary": _docstring_summary(tree),
                },
            )
        )
        _walk(
            list(tree.body),
            ctx=ctx,
            parent_qual=module_name,
            parent_uid=module_sym.symbol_uid,
            parent_kind="module",
        )

        # Module-level calls (`app = FastAPI()`, `@app.get("/")`, decorator
        # invocations, top-level initialisers) are not inside any function or
        # class body, so the per-function walker above skipped them. Emit
        # `calls` pending relations attributed to the module symbol itself.
        # `_iter_calls_shallow` already stops at nested FunctionDef/ClassDef
        # boundaries, so this does NOT double-count calls inside functions.
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Function/method/class bodies were handled by _walk.
                # Decorators on those defs are module-level calls though —
                # emit a call edge per decorator for richer impact analysis.
                for dec in getattr(stmt, "decorator_list", []) or []:
                    dec_call = dec if isinstance(dec, ast.Call) else None
                    if dec_call is None:
                        # Bare decorator like `@classmethod` — resolve as
                        # a name reference so impact can find decorator usage.
                        candidates = _resolve_callee(
                            dec,
                            ctx.scope,
                            module_name=ctx.module_name,
                            class_qual=None,
                        )
                    else:
                        candidates = _resolve_callee(
                            dec_call.func,
                            ctx.scope,
                            module_name=ctx.module_name,
                            class_qual=None,
                        )
                    if not candidates:
                        continue
                    ctx.pending_relations.append(
                        PendingRelation(
                            src_symbol_uid=module_sym.symbol_uid,
                            relation_kind="calls",
                            dst_candidates=candidates,
                            provenance="python-ast;module-decorator",
                            site_line=getattr(dec, "lineno", None),
                        )
                    )
                continue
            for call_node in _iter_calls_shallow(stmt):
                if not isinstance(call_node, ast.Call):
                    continue
                candidates = _resolve_callee(
                    call_node.func,
                    ctx.scope,
                    module_name=ctx.module_name,
                    class_qual=None,
                )
                if not candidates:
                    continue
                ctx.pending_relations.append(
                    PendingRelation(
                        src_symbol_uid=module_sym.symbol_uid,
                        relation_kind="calls",
                        dst_candidates=candidates,
                        provenance="python-ast;module-level",
                        site_line=getattr(call_node, "lineno", None),
                    )
                )

        return ParseResult(
            language="python",
            semantic_source="python-ast",
            confidence=self.confidence,
            parse_status="ok",
            symbols=ctx.symbols,
            occurrences=ctx.occurrences,
            relations=ctx.relations,
            pending_relations=ctx.pending_relations,
            chunks=ctx.chunks,
            diagnostics=ctx.diagnostics,
        )
