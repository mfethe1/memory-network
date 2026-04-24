# External code-intelligence systems review

Date: 2026-04-24

## Decision

Use **SCIP** as the first external system to incorporate. For this Python-first
repo, the concrete producer is **`scip-python`**, which is built on Pyright and
emits a language-neutral SCIP index. Do not replace the local SQLite store,
MCP surface, repo-map, embeddings, or test-edge machinery. Replace or augment
the weakest local semantic extraction path: cross-file definitions/references,
implementation relationships, and typed Python resolution.

The current system should stay as the local control plane:

1. SQLite remains the agent-facing cache and query surface.
2. `pipeline.reindex()` remains the only mutating local update path.
3. SCIP ingestion becomes an optional higher-confidence semantic source.
4. Python AST remains the zero-dependency fallback.

## Why This Matches Our Current Gaps

`code_index doctor --json` on this workspace reports:

- 849 open unresolved calls.
- Python AST as the dominant semantic source.
- `tree_sitter_python` structural search available, but generic
  `tree_sitter` package unavailable.
- `scip-python`, Zoekt, and ast-grep are not installed on PATH.
- FTS drift is above the rebuild threshold.

The high-value problem is semantic precision, not basic search. SCIP directly
models documents, occurrences, symbols, symbol roles, documentation, enclosing
ranges, diagnostics, and implementation/reference relationships. That overlaps
with our `files`, `symbols`, `occurrences`, `relations`, and `diagnostics`
tables.

## Systems Evaluated

### SCIP / scip-python

Fit: best.

SCIP is a language-agnostic source-code indexing protocol for Go to definition,
Find references, and Find implementations. The public SCIP schema centers on
documents, occurrences, symbol metadata, symbol roles, diagnostics, enclosing
ranges, and relationships. Sourcegraph lists maintained SCIP emitters for
Java/JVM, TypeScript/JavaScript, Rust, C/C++, Ruby, Python, .NET, Dart, and PHP.

`scip-python` is a Sourcegraph fork/addition to Pyright. It reuses Pyright's
type checking and semantic analysis, supports external package references, and
has first-class Python indexer usage via `scip-python index . --project-name`.
Glean also uses Sourcegraph's Python SCIP indexer for Python ingestion, which is
a strong signal that SCIP is a practical interchange layer rather than a
Sourcegraph-only artifact.

Adopt:

- Add `code_index import-scip --from index.scip|index.json`.
- Prefer consuming `scip print --json index.scip` first so we do not need to
  vendor protobuf bindings in the initial slice.
- Map SCIP document/symbol/occurrence data into existing tables.
- Mark imported rows with `semantic_source = "scip:<tool>"` and higher
  confidence than `python-ast`.
- Keep local `chunk` creation separate. SCIP is the semantic spine source;
  chunks remain our retrieval projection.

Do not adopt yet:

- Replacing the whole DB with SCIP's experimental SQLite converter. It stores
  occurrences opaquely and would remove too much of our agent-specific shape.
- Uploading to Sourcegraph as a requirement. Local ingestion is enough.

### ast-grep

Fit: good for structural search and codemods, not a full index replacement.

ast-grep is a fast, polyglot structural search/rewrite tool powered by
tree-sitter. It can improve `code_index query --ast` and future codemod flows,
but it does not replace symbol identity, occurrence storage, relation graphs, or
hotspot/test-edge data.

Adopt later:

- Add optional `code_index query --ast-engine ast-grep` when `sg` or
  `ast-grep` is installed.
- Use it for syntax-aware searches and rewrite previews, not semantic identity.

### Zoekt

Fit: good for large-repo lexical search, not needed for this repo today.

Zoekt is a trigram code-search engine with boolean/regexp queries and
symbol-aware ranking. It is valuable if SQLite FTS and ripgrep become slow on
large repos or multi-repo setups. Today, our corpus is small enough that ripgrep
+ SQLite FTS is simpler.

Adopt later:

- Keep ripgrep as the local fast path.
- Add a Zoekt backend only after benchmarks show local FTS or rg is the
  bottleneck.

### Glean

Fit: design reference, not a near-term replacement.

Glean is a distributed facts database for source-code facts and derived
predicates. It is attractive for large organizations and cross-language
centralized indexing. It is too heavy for this local-first hackathon repo, but
its model validates our direction: facts, relationships, symbol IDs, query
interfaces, and derived data.

Learn:

- Keep semantic data as facts/edges.
- Keep a query/API layer over raw storage.
- Keep generated documentation/signature data tied to symbol IDs.

### Kythe

Fit: design reference, not a near-term replacement.

Kythe has mature language-neutral graph concepts, cross-references, callgraphs,
schema docs, and indexer verification. It is compiler/build-system oriented and
heavier than SCIP for the next slice.

Learn:

- Use graph verification fixtures for future semantic import tests.
- Keep relation kinds explicit and typed.

### CodeQL

Fit: security/data-flow sidecar, not code-memory replacement.

CodeQL creates language-specific databases with AST, control-flow, data-flow,
and type/name-binding data, then runs QL queries. It is excellent for security
variant analysis and deeper data-flow questions, but it is not optimized as a
lightweight agent context cache.

Adopt later:

- Add a `diagnostics` ingestion path for CodeQL SARIF or query output.
- Do not make CodeQL the primary index.

## Implementation Plan

1. External tool visibility
   - Add `doctor.external_tools` for `scip`, `scip-python`, `ast-grep`,
     `zoekt`, and `codeql`.
   - Include role and install/use hint so agents know what to do next.

2. SCIP JSON fixture importer
   - Add a pure parser for `scip print --json` output.
   - Test against a minimal checked-in fixture with one module, one function,
     one reference, and one implementation relationship.
   - Do not shell out in tests.

3. `code_index import-scip`
   - Accept `--json-index PATH` first.
   - Later accept `--from index.scip` by shelling out to `scip print --json`
     when `scip` is installed.
   - Use writer lock and `apply_schema`.
   - Upsert `files`, `symbols`, `occurrences`, `relations`, and `diagnostics`.
   - Preserve existing chunks unless `--replace-semantic-source` is passed.

4. Optional scip-python runner
   - Add `code_index scip-python-index` only after the importer is solid.
   - Check Node/scip-python availability through `doctor`.
   - Emit `index.scip` under `.code_index/external/scip-python/`.

5. Precision gates
   - Imported SCIP edges can supersede ambiguous suffix-match pending calls.
   - Keep unresolved rows when SCIP has no answer.
   - Track stats: `symbols_imported_from_scip`, `occurrences_imported_from_scip`,
     `relations_imported_from_scip`, `calls_resolved_by_scip`.

## Risks

- SCIP symbol strings will not match our deterministic `symbol_uid` directly.
  Mitigation: store SCIP source symbol strings in context/provenance first;
  only unify with local symbols when canonical-name/signature mapping is
  proven by tests.

- `scip-python` requires Node and may need dependency environment data.
  Mitigation: make it optional, keep Python AST fallback, and surface
  availability in `doctor`.

- Overwriting local parser data could regress existing commands.
  Mitigation: import as an additive high-confidence semantic source first,
  then add explicit replacement flags after fixtures prove equivalence.

## Success Criteria

- `doctor` reports whether SCIP tooling is available.
- A SCIP JSON fixture imports into live `symbols`, `occurrences`, and
  `relations` without breaking existing commands.
- On this repo, enabling SCIP import reduces `unresolved_calls_open` materially
  from the current 849 baseline.
- Full test suite remains green.

## Sources

- SCIP README and schema: https://github.com/scip-code/scip
- SCIP schema raw: https://raw.githubusercontent.com/scip-code/scip/main/scip.proto
- Sourcegraph SCIP indexer guide: https://sourcegraph.com/docs/code-navigation/writing-an-indexer
- scip-python README: https://github.com/sourcegraph/scip-python
- scip-python announcement: https://sourcegraph.com/blog/scip-python
- Glean Python SCIP docs: https://glean.software/docs/indexer/scip-python/
- ast-grep docs: https://ast-grep.github.io/
- Zoekt README: https://github.com/sourcegraph/zoekt
- Glean engineering blog: https://engineering.fb.com/2024/12/19/developer-tools/glean-open-source-code-indexing/
- Kythe docs: https://kythe.io/docs/
- CodeQL overview: https://codeql.github.com/docs/codeql-overview/about-codeql/
