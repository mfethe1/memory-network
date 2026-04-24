# Slice 10 — Adversarial review fixes

> Implements the top findings from the codex adversarial review captured in
> session 05b1d9de on 2026-04-24. Ten tasks grouped into three priority tiers.
> Main-session tasks serialize on `pipeline.py`; subagent tasks run in parallel
> on non-overlapping files and use `codex exec -s read-only
> --skip-git-repo-check` for second-opinion diagnosis.

## Ground rules (all tasks)

1. Full suite green after each task: `python -m pytest tests/ -q --timeout=120`.
2. `symbol_uid` stays primary. No breaking schema changes (add, don't rename).
3. Keep `pipeline.reindex()` as the shared entrypoint.
4. New stats fields append, existing keep their names.
5. Subagents MUST use `codex exec -s read-only --skip-git-repo-check
   -c 'model_reasoning_effort="high"'` at least twice per task:
   - Once during diagnosis to locate the right seams in the code.
   - Once before finishing to validate the fix doesn't re-introduce the issue.
6. Edits are done via Edit/Write by the subagent. Do not rely on codex for
   writes — its sandbox rejects them.
7. Every task lands a regression test that FAILS on the pre-fix code and
   PASSES after the fix. Write the failing test first.

---

# P0 — Correctness bugs shipping today (MUST fix before any new surface)

## Task A — Stale embeddings after chunk update (subagent α)

### Why
> "`populate()` skips any chunk that already has an embedding keyed by
> `(chunk_pk, provider, model)` (`store.py:94`). But chunk updates keep the
> same `chunk_pk` and do not delete or invalidate embeddings
> (`pipeline.py:461`). Edited chunks keep stale vectors forever unless the
> user runs a full refresh." — codex adversarial §4

### Scope
- In `code_index/pipeline.py`: when a chunk's `content_hash` changes during
  an update pass, invalidate the chunk's embedding row. Do this by either
  deleting the `embeddings` row (simpler) or stamping an `invalidated_at`
  column (preserves history — only choose this if trivial).
- In `code_index/embeddings/store.py::populate`: the invalidation must be
  visible — `populate` should now re-embed any chunk whose embedding row
  was deleted or whose `content_hash` differs from what's stored.
- Add a `content_hash` column to the `embeddings` table if needed so
  `populate` can cheaply detect drift on re-run without deletions. Migrate
  v4→v5 through the existing `_migrate_if_needed` path.

### Files to touch
- `code_index/schema.sql`
- `code_index/db.py` (migration + `_EXPECTED_COLUMNS`)
- `code_index/embeddings/store.py`
- `code_index/pipeline.py` (chunk-update invalidation hook only)
- `tests/test_embeddings_staleness.py` (new)

### Do NOT touch
- `code_index/parsers/`
- `code_index/commands/` (except mcp_serve_cmd if stats surface changes)
- Any writer-lock code

### Acceptance
- New test: index a repo, populate embeddings, edit a chunk's content,
  reindex, repopulate. Verify the embedding vector now corresponds to the
  NEW content, not the pre-edit content. This test FAILS on `main`.
- `doctor --json` embeddings block surfaces a `stale_count` field.
- Full suite green.

---

## Task B — Reader transactional consistency (main session)

### Why
> "Readers do not take the lock, so they can observe an index midway
> through a multi-file update: some files new, relations not backfilled,
> test edges stale." — codex adversarial §5

### Scope
Two-part fix.

1. **Mark reindex boundaries.** Bump a `reindex_epoch` counter in
   `schema_meta` at the START of `_reindex_body` (set `in_progress=True`)
   and at the END (set `in_progress=False`, `epoch=N+1`). Commit only the
   transitions inside the existing writer lock.

2. **Readers respect the boundary.** Read commands that care about graph
   consistency (`impact`, `tests`, `similar`, `repo-map`, `ask`,
   `find_symbol --references`) check `in_progress` at start and either:
   - Return partial-state warning in JSON (`{..., "index_state":
     "mid_reindex", "consistency": "partial"}`) so agents know to retry.
   - Or gate on a short wait loop (up to 2s) if reindex is mid-flight.

### Files to touch
- `code_index/pipeline.py` (epoch bump around `_reindex_body`)
- `code_index/commands/impact_cmd.py`, `tests_cmd.py`, `similar_cmd.py`,
  `repo_map_cmd.py`, `ask_cmd.py`, `symbol_cmd.py` (consistency check +
  `index_state` field in JSON output)
- `code_index/commands/mcp_serve_cmd.py` (same consistency propagation
  for MCP tools)
- `tests/test_reader_consistency.py` (new)

### Acceptance
- Regression test: start a long `reindex()` in a thread, in a second
  thread issue `find_symbol` — the JSON must include
  `index_state: "mid_reindex"`. This test FAILS on `main`.
- `doctor --json` reports `reindex.in_progress` + `epoch` + `last_completed_at`.
- Full suite green.

---

## Task C — Suffix-match wrong-edge risk (main session)

### Why
> "Resolver may suffix-match `%.candidate` (`pipeline.py:772`). That
> creates plausible-looking wrong edges. Wrong context is worse than
> missing context for coding agents because it steers edits and tests
> toward false confidence." — codex adversarial §8

### Scope
- Demote `LIKE '%.cand'` suffix matches from a fallback that lands an edge
  to a candidate that requires either (a) a unique suffix match across
  the whole index OR (b) agreement with another signal (Jedi, import
  graph, same-package heuristic).
- On non-unique suffix match with no corroborating signal: leave the edge
  in `unresolved_calls` instead of guessing. This is an intentional
  precision-over-recall trade.
- Track demoted edges in stats as `calls_unresolved_ambiguous: int` so
  doctor can report them.

### Files to touch
- `code_index/pipeline.py` (resolver only — `_try_resolve_candidates`,
  `_match_suffix`)
- `tests/test_resolver_precision.py` (new)

### Acceptance
- Regression test: a repo with two classes both having `method_x`, where
  one caller's `self.method_x()` could suffix-match either. Pre-fix: one
  is chosen (wrong half the time); post-fix: both stay unresolved and
  are surfaced via `unresolved_calls`. Test FAILS on `main`.
- `stats.calls_unresolved_ambiguous` appears in `ReindexStats.to_dict()`.
- Full suite green.

---

# P1 — Design scope narrowing

## Task D — Read-only MCP default mode (subagent β)

### Why
> "Exposes mutating `update` and `rebuild_fts` as normal model-controlled
> tools. A coding agent can accidentally trigger an expensive full
> reindex, repair FTS while another workflow expects stale state, or
> mutate local state during a read-only investigation. There should be a
> read-only default mode, with write tools enabled only by explicit CLI
> flag." — codex adversarial §6

### Scope
- Default mode for `code_index mcp-serve` becomes READ-ONLY:
  `search_text`, `search_query`, `search_ast`, `find_symbol`, `impact`,
  `affected_tests`, `doctor` are exposed. `update`, `rebuild_fts` are
  omitted from the tool list unless `--allow-writes` is passed.
- `--allow-writes` also implies a second per-request confirmation: the
  tool description prominently notes "MUTATING — will reindex the repo".
- `describe_surface()` must reflect the actual exposed tool set.
- Update docs/`claude-code.md` to explain the new default.

### Files to touch
- `code_index/commands/mcp_serve_cmd.py`
- `code_index/cli.py` (add `--allow-writes` to mcp-serve subparser)
- `docs/claude-code.md`
- `tests/test_mcp_readonly_default.py` (new)

### Acceptance
- `mcp-serve` without `--allow-writes` does NOT register `update` or
  `rebuild_fts` as tools (verified via `describe_surface`).
- `mcp-serve --allow-writes` registers the full surface.
- Full suite green.

---

## Task E — Identity model honesty pass (subagent γ)

### Why
> "The 'durable identity' claim is oversold if read as refactor-stable.
> It is really a deterministic key for 'same qualified declaration
> shape.' Signature-only API-compatible changes change identity." —
> codex adversarial §2

### Scope
This is a DOCS + stats task, not a code rewrite. The identity model is
fine; the CLAIMS are not.

- Update `CLAUDE.md`, `.claude/CLAUDE.md`, `docs/code-index-spec.md`,
  `README` section on identity: replace "durable identity" with
  "deterministic declaration key". List exactly what changes the UID:
  canonical_name, container, signature, kind, language.
- Add a `--rename-map` input to `code_index update` that accepts a
  JSON list of `{old_canonical, new_canonical}` pairs and migrates the
  symbol row (preserving `symbol_pk` → same FK references, changing
  `canonical_name` and `symbol_uid`). Opt-in only — no inference.
- `doctor --json` gains an `identity_model` block naming the fields the
  UID depends on. No code logic change besides that surface.

### Files to touch
- `CLAUDE.md`, `.claude/CLAUDE.md`
- `docs/code-index-spec.md`
- `code_index/commands/update_cmd.py` (rename-map plumbing)
- `code_index/commands/doctor_cmd.py` (identity_model block)
- `code_index/symbols.py` (only if a `rename_symbol` helper is needed)
- `tests/test_rename_map.py` (new)

### Acceptance
- Docs no longer claim refactor-durable identity.
- `update --rename-map` takes `{"old": "pkg.a.foo", "new": "pkg.b.bar"}`
  and the symbol keeps its `symbol_pk` but gets the new canonical name.
- Full suite green.

---

## Task F — Embedding relevance benchmark (subagent δ)

### Why
> "Without relevance benchmarks, embeddings here are complexity with
> unproven upside." — codex adversarial §4

### Scope
- New harness under `bench/embeddings/` that:
  1. Indexes a fixed Python corpus (vendored `fastapi/` if present, else
     a pinned synthetic corpus of ~200 files).
  2. Runs N tagged queries (e.g. "where is jwt token validated?",
     "find the middleware that handles CORS", "decorator for
     rate-limiting") each with a known-good target symbol.
  3. Scores BM25-only, embeddings-only, and BM25+embeddings-rerank on
     recall@1, recall@5, MRR.
- Output a JSON summary + markdown report.
- This is the evidence base we'll use to either keep, fix, or kill the
  embeddings pillar in a later slice.

### Files to touch
- `bench/embeddings/__init__.py`, `bench/embeddings/run.py`,
  `bench/embeddings/corpus.py`, `bench/embeddings/queries.json`
- `pyproject.toml` (add `[project.scripts] code_index_bench = ...`
  only if trivial, else omit)
- `docs/embeddings-evaluation.md` (methodology + initial numbers)

### Do NOT touch
- Any code under `code_index/` — this is purely a harness.

### Acceptance
- `python -m bench.embeddings.run --corpus fastapi --limit 20` writes
  `bench/embeddings/results.json` with the three scoring modes.
- Initial numbers reported in `docs/embeddings-evaluation.md`.

---

# P2 — Scaling (deferred to slice 11)

Do NOT implement in slice 10. Noted here so the plan has a full picture:
- **Task G**: Ordered migration files (`migrations/0005_embedding_hash.sql`).
- **Task H**: ANN embedding search (usearch or hnswlib backend).
- **Task I**: Real-repo benchmark suite against Django/transformers/FastAPI.
- **Task J**: Non-Python parser: at minimum TS/Go via tree-sitter.

---

# Dispatch plan

## Parallel (run together)
- Subagent α → Task A (stale embeddings)
- Subagent β → Task D (read-only MCP)
- Subagent γ → Task E (identity honesty)
- Subagent δ → Task F (embedding benchmark)

## Serial (main session, after parallel wave completes)
- Task B (reader consistency)
- Task C (suffix-match precision)

Rationale: A/D/E/F touch disjoint file sets. B+C both touch `pipeline.py`
in the resolver area and must serialize.

## Final

After all six tasks land:
```bash
codex exec "Independent review of slice-10-adversarial-review-fixes.md.
Verify each P0/P1 fix actually closes the concern from the slice-9
adversarial review. Flag anything that introduced a new problem." \
  -C "$(pwd)" -s read-only --skip-git-repo-check \
  -c 'model_reasoning_effort="high"' --json > /tmp/codex-slice10-review.jsonl
```
