---
description: Prefer code_index for symbol lookup, structural search, impact, and retrieval when editing Python in this repo.
paths:
  - "**/*.py"
  - "**/*.pyi"
---

# Python-editing rules

When working on `.py` / `.pyi` files:

- For "where is X defined?" → `code_index symbol X --json` (returns canonical
  name, def file/line, kind, symbol_uid). Cite the returned def_file.
- For "show call sites of X" → `code_index symbol X --references --json`
  (returns up to 50 resolved call references with file/start/end line).
- For "who calls X?" or "what breaks if I change X?" → `code_index impact X`.
- For "find all calls matching a shape" (e.g. subscripted generics, decorated
  methods) → `code_index query --ast call` or a raw S-expression with
  `code_index query --ast "(pattern)"`.
- For literal / regex searches (TODO markers, version strings, path globs),
  use `code_index grep` — it will resolve ripgrep robustly or fall back to a
  python-re engine; both return the same JSON shape.
- For ranked retrieval over code (BM25 over symbol+signature+content),
  use `code_index query "phrase"`.
- After material edits to `.py` files, the PostToolUse hook reindexes
  touched files automatically for Edit, Write, and MultiEdit. You do not
  need to call `update --files` manually for normal Claude Code edits.

Things the Python indexer already knows — do not reimplement:

- imports (absolute AND relative: `from . import X`, `from ..pkg import Y`),
  class/function/method containment, `self`/`cls` method calls,
  subscripted base classes (`class X(Mapping[str, int])` → inherits Mapping),
  `ClassName.method()` references inside that class's body.
- `unresolved_calls` persists cross-file edges we could not resolve at first
  parse. They backfill automatically on later reindexes — no manual linkage
  step needed.
- Dead edges (live src → tombstoned dst) are also queued into
  `unresolved_calls` on each reindex, so they heal if a same-canonical-name
  symbol reappears.
- Resolved call relations write `occurrences(role="reference",
  syntax_kind="call")`, so `symbol --references --json` can list call-site
  file/line locations.
- `@pytest.mark.parametrize` argnames + literal case lists are captured and
  surfaced via `code_index tests --json`; `code_index tests X --runner pytest`
  expands captured literal cases into pytest node ids.

Things the indexer does NOT model:

- Dynamic attribute access (`getattr`, `__getattr__`).
- Calls through typed instances (`foo = Bar(); foo.method()` — unless
  `foo` is `self`/`cls`, or the call is `ClassName.method()` inside that
  class's own body).
- Global rename inference. If `helper` is renamed to `run` in one file,
  other files calling `helper` keep an open unresolved row until some
  file reintroduces `helper` or the caller file is reparsed.
