# Slice 9 — Codex review follow-ups

> Implements the top findings from the codex review captured at
> `C:\Users\mfeth\AppData\Local\Temp\codex-review.jsonl`. Five tasks. Task 1
> and Task 5 done in the main session; Tasks 2-4 dispatched as parallel
> subagents.

## Ground rules (all tasks)

1. Full suite green after each task: `python -m pytest tests/ -q --timeout=60`.
2. `symbol_uid` stays primary. No breaking schema changes (add, don't
   rename).
3. Keep `pipeline.reindex()` as the shared entrypoint. Don't fork.
4. New stats fields append, existing keep their names.
5. Subagents MAY use `codex exec -s read-only --skip-git-repo-check` as a
   second-opinion tool during diagnosis — but write the code themselves
   via Edit/Write. Codex-rescue has a sandbox policy that rejects writes
   to the parent repo; don't rely on it for edits.

---

## Task 1 — Repo-wide writer lock (main session, foreground)

### Why
> "WAL + busy_timeout=5000 only serializes individual SQLite write
> transactions, not a whole logical reindex. Two writers can interleave
> file clears, symbol tombstones, unresolved backfill, and test-edge
> rebuilds." — codex review §1

### Scope
- New module `code_index/locking.py` with a file-based advisory lock
  (`fcntl` on POSIX, `msvcrt` on Windows) exposed as
  `writer_lock(config, *, timeout_s=30) -> ContextManager`.
- Every mutating command path wraps its write section in the lock:
  `init`, `update`, `embed`, `rebuild-fts`, `rebuild-tests`,
  `install-hooks`, `watch` (per-flush).
- MCP `update` and `rebuild_fts` tools wrap their bodies.
- Lock path: `config.lock_path` (already defined in `config.py`).
- If the lock can't be acquired in `timeout_s`, the command exits with
  a clear JSON error `{"error": "another writer holds the lock", "lock_path": ...}`.

### Tests
- `tests/test_writer_lock.py` — 2 writers competing for the lock; one
  wins, the other blocks then times out; lock is released on exception.

---

## Task 2 — Jedi resolver tier integration (subagent A)

### Why
> "Jedi as a standalone optional pass will rot. AST resolution creates
> wrong-but-resolved edges before Jedi sees them; Jedi only sees rows
> that survived into unresolved_calls; scans by line, not span; caps at
> arbitrary 2000 rows; never rebuilds test_edges." — codex review §3

### Scope
- Rewrite `code_index/parsers/jedi_enhanced.py`:
  - Accept a list of pending call-site records (src_symbol_uid, file_pk,
    line, column) instead of querying `unresolved_calls` directly.
  - Use `column` (from the parser) for precise goto, not line-scan.
  - Return a mapping `{pending_key: candidate_list}` the main resolver
    consumes as another candidate source.
  - Drop the 2000 row cap — it's now per-reindex-call.
- Modify `code_index/pipeline.py::_resolve_pending`:
  - When `config.enable_jedi` is True and Jedi is available, call Jedi
    resolver BEFORE the suffix-match fallback. Attach provenance
    `jedi:goto` and confidence 0.9.
  - Still persist unresolved rows for future backfill.
- Modify `_backfill_unresolved` similarly.
- After Jedi adds relations, trigger the scoped `_rebuild_test_edges`
  for affected test symbols.
- Add a new stats field `relations_resolved_by_jedi: int`.

### Files to touch (and only these)
- `code_index/parsers/jedi_enhanced.py`
- `code_index/pipeline.py` (ONLY the `_resolve_pending`,
  `_backfill_unresolved`, and ReindexStats blocks)
- `tests/test_jedi_enhanced.py` (update to new API)
- `tests/test_jedi_pipeline_integration.py` (new — end-to-end from
  `reindex()` with `enable_jedi=True`)

### Do NOT touch
- `code_index/parsers/python_ast.py`
- `code_index/commands/mcp_serve_cmd.py`
- `code_index/embeddings/`
- `code_index/locking.py` (Task 1 will land first; assume its API)

### Acceptance
- `config.enable_jedi=True` + a typed-instance call like
  `foo = Bar(); foo.method()` produces a relation during the first
  `reindex()` pass, not as a separate command.
- `stats.relations_resolved_by_jedi` > 0 on the fixture.
- `python -m pytest tests/ -q` green.

---

## Task 3 — Embeddings hardening (subagent B)

### Why
> "search() fetches every matching embedding blob into memory, unpacks
> every vector into Python floats, scores every row, stores every
> scored dict, then full-sorts. At 500k chunks, this becomes
> seconds-to-tens-of-seconds... no UNIQUE(chunk_pk, provider, model)
> constraint, so concurrent populate can duplicate embeddings despite
> INSERT OR REPLACE." — codex review §2

### Scope
- Schema bump v3 → v4. Add:
  - `UNIQUE(chunk_pk, provider, model)` on `embeddings` table.
  - `embedding_norm REAL` column (precomputed vector L2 norm).
  - Migration in `code_index/db.py::_migrate_if_needed` that adds the
    UNIQUE index and populates `embedding_norm` for existing rows via
    a single UPDATE pass.
- Rewrite `code_index/embeddings/store.py::search`:
  - Use a fixed-size heap (`heapq.nlargest`) instead of full sort.
  - Read `embedding_norm` from the DB instead of recomputing per-query.
  - Stream row-at-a-time instead of `.fetchall()`.
- Update `populate` to compute + store `embedding_norm` on insert.

### Files to touch (and only these)
- `code_index/schema.sql`
- `code_index/db.py` (migration only)
- `code_index/embeddings/store.py`
- `tests/test_embeddings.py` (update to verify norm stored + heap path
  correctness; add a migration test)

### Do NOT touch
- `code_index/pipeline.py`
- `code_index/parsers/`
- `code_index/commands/` (except nothing — you shouldn't edit any)

### Acceptance
- Schema version reports `4`.
- Running `populate` twice produces the same row count, no dupes.
- `search()` return order is identical to the pre-rewrite implementation
  on the fixture (lock-in regression test using existing mock backend).
- `python -m pytest tests/ -q` green.

---

## Task 4 — MCP HTTP transport auth (subagent C)

### Why
> "CLI exposes --transport http|streamable-http with no token/host/auth
> options. The surface includes read tools, update, and rebuild_fts.
> Minimum: bind only 127.0.0.1, require a per-repo random bearer token
> for HTTP, fail closed if missing." — codex review §5

### Scope
- Add `--bearer-token`, `--bearer-token-file`, `--bind` flags to
  `code_index mcp-serve`.
- For `--transport http|streamable-http`:
  - Default `--bind=127.0.0.1` (reject other binds without an explicit
    `--allow-remote` flag that also requires a token).
  - If neither `--bearer-token` nor `--bearer-token-file` is given and
    no `CODE_INDEX_MCP_TOKEN` env var is set: generate a random token,
    print it once to stderr, write it to `.code_index/mcp-token`
    (0600), and require it on subsequent requests.
  - Configure FastMCP's streamable-http middleware to 401 on missing or
    wrong bearer.
- For `--transport stdio`: no auth required (unchanged).

### Files to touch (and only these)
- `code_index/commands/mcp_serve_cmd.py`
- `code_index/cli.py` (add the three flags to the `mcp-serve` subparser)
- `tests/test_mcp_auth.py` (new — use `mcp.server.fastmcp` directly to
  simulate a request with/without token; skip if mcp not available)

### Do NOT touch
- `code_index/pipeline.py`
- `code_index/embeddings/`
- Any parser

### Acceptance
- `mcp-serve --transport stdio` works without a token.
- `mcp-serve --transport http` with no env/file/flag generates a token,
  writes it to `.code_index/mcp-token` with 0600 perms, and prints it
  to stderr.
- `mcp-serve --transport http --bind 0.0.0.0` without
  `--allow-remote` fails with a JSON error.
- `python -m pytest tests/ -q` green.

---

## Task 5 — Migration discipline (main session, same session as Task 1)

### Why
> "_migrate_if_needed returns early on exact-version match even if a
> column is missing from partial corruption. Read commands skip
> apply_schema entirely." — codex review §6

### Scope
- `_migrate_if_needed` re-probes v3 columns even when `prior == SCHEMA_VERSION`
  and repairs any missing via `_add_column_if_missing`.
- Every read-only command (`symbol`, `query`, `grep`, `impact`, `tests`,
  `similar`, `repo-map`, `ask`, `doctor`, `mcp-serve`) calls
  `db_mod.apply_schema(conn)` at startup. Idempotent; cheap.
- `doctor --json` gains a `schema_health` block:
  `{ "version": str, "expected": str, "columns_ok": bool, "missing": [str] }`.

### Files to touch
- `code_index/db.py`
- Every read command in `code_index/commands/*_cmd.py` that currently
  connects without `apply_schema`.
- `code_index/commands/doctor_cmd.py`
- `tests/test_migration_repair.py` (new)

---

## Final — independent codex review pass

After all five tasks land:

```bash
codex exec "Re-review the five fixes in plans/slice-9-codex-followups.md. Confirm each is present, tested, and doesn't re-introduce the original concern. Flag anything that regressed from slice 8. Keep it to one paragraph per task." \
  -C "$(pwd)" -s read-only --skip-git-repo-check \
  -c 'model_reasoning_effort="high"' --json > /tmp/codex-slice9-review.jsonl
```
