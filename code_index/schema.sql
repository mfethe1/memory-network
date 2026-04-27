-- code_index schema.
-- Layering:
--   semantic spine: files, symbols, occurrences, relations, diagnostics
--   retrieval projection: chunks, chunks_fts, chunk_edits, chunk_lineage
--   activity: agent_runs, agent_events
--   reserved: embeddings, test_edges, repo_map_snapshots, commits, file_versions
--
-- Apply idempotently: CREATE TABLE IF NOT EXISTS throughout.
-- Schema version tracked in schema_meta; bump SCHEMA_VERSION in db.py on change.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    file_pk         INTEGER PRIMARY KEY,
    file_path       TEXT NOT NULL UNIQUE,
    language        TEXT,
    worktree_hash   TEXT,
    head_blob_oid   TEXT,
    is_tracked      INTEGER DEFAULT 0,
    is_dirty        INTEGER DEFAULT 0,
    size_bytes      INTEGER,
    mtime_ns        INTEGER,
    parse_status    TEXT DEFAULT 'pending',  -- pending | ok | empty | skipped | binary | failed
    parse_error     TEXT,
    semantic_source TEXT,                     -- python-ast | tree-sitter:<lang> | ctags | heuristic
    parser_confidence REAL,
    indexed_at      TEXT,                     -- ISO-8601
    deleted_at      TEXT,
    git_blob_oid    TEXT,                     -- HEAD blob oid for this path (git ls-files --stage)
    git_committed_at INTEGER,                 -- unix ts of last commit touching this path
    git_author      TEXT                      -- author name of that last commit
);

CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(parse_status);
CREATE INDEX IF NOT EXISTS idx_files_git_committed_at ON files(git_committed_at);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_pk           INTEGER PRIMARY KEY,
    symbol_uid          TEXT NOT NULL UNIQUE,
    language            TEXT,
    kind                TEXT NOT NULL,            -- module | class | function | method | interface | enum | ...
    canonical_name      TEXT NOT NULL,            -- pkg.mod.Class.method
    display_name        TEXT,
    container_symbol_pk INTEGER REFERENCES symbols(symbol_pk) ON DELETE SET NULL,
    signature_norm      TEXT,
    semantic_source     TEXT,
    confidence          REAL,
    first_seen_commit   TEXT,
    last_seen_commit    TEXT,
    first_indexed_at    TEXT,
    last_indexed_at     TEXT,
    deleted_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(canonical_name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_container ON symbols(container_symbol_pk);

CREATE TABLE IF NOT EXISTS occurrences (
    occurrence_pk   INTEGER PRIMARY KEY,
    symbol_pk       INTEGER NOT NULL REFERENCES symbols(symbol_pk) ON DELETE CASCADE,
    file_pk         INTEGER NOT NULL REFERENCES files(file_pk) ON DELETE CASCADE,
    role            TEXT NOT NULL,               -- definition | reference | import | export | alias
    start_line      INTEGER,
    end_line        INTEGER,
    start_byte      INTEGER,
    end_byte        INTEGER,
    syntax_kind     TEXT
);

CREATE INDEX IF NOT EXISTS idx_occurrences_symbol ON occurrences(symbol_pk);
CREATE INDEX IF NOT EXISTS idx_occurrences_file ON occurrences(file_pk);
CREATE INDEX IF NOT EXISTS idx_occurrences_role ON occurrences(role);

CREATE TABLE IF NOT EXISTS relations (
    relation_pk     INTEGER PRIMARY KEY,
    src_symbol_pk   INTEGER NOT NULL REFERENCES symbols(symbol_pk) ON DELETE CASCADE,
    dst_symbol_pk   INTEGER NOT NULL REFERENCES symbols(symbol_pk) ON DELETE CASCADE,
    relation_kind   TEXT NOT NULL,               -- calls | contains | imports | inherits | implements | overrides
    provenance      TEXT,
    weight          REAL DEFAULT 1.0,
    UNIQUE(src_symbol_pk, dst_symbol_pk, relation_kind)
);

CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(src_symbol_pk);
CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(dst_symbol_pk);

CREATE TABLE IF NOT EXISTS diagnostics (
    diagnostic_pk   INTEGER PRIMARY KEY,
    file_pk         INTEGER NOT NULL REFERENCES files(file_pk) ON DELETE CASCADE,
    tool            TEXT NOT NULL,               -- python-ast | tree-sitter | ctags | heuristic | watchdog
    code            TEXT,
    severity        TEXT,                        -- error | warning | info
    start_line      INTEGER,
    end_line        INTEGER,
    message         TEXT,
    observed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_file ON diagnostics(file_pk);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_pk            INTEGER PRIMARY KEY,
    chunk_uid           TEXT NOT NULL UNIQUE,
    file_pk             INTEGER NOT NULL REFERENCES files(file_pk) ON DELETE CASCADE,
    file_path           TEXT NOT NULL,
    language            TEXT,
    chunk_type          TEXT NOT NULL,           -- module | class | function | method | file (heuristic)
    symbol_name         TEXT,
    symbol_path         TEXT,
    parent_symbol_path  TEXT,
    primary_symbol_pk   INTEGER REFERENCES symbols(symbol_pk) ON DELETE SET NULL,
    signature           TEXT,
    start_line          INTEGER,
    end_line            INTEGER,
    start_byte          INTEGER,
    end_byte            INTEGER,
    context_json        TEXT,                    -- imports, docstring, decorators, parent sig, etc.
    content             TEXT NOT NULL,
    raw_hash            TEXT NOT NULL,
    normalized_hash     TEXT NOT NULL,
    edit_count          INTEGER NOT NULL DEFAULT 0,
    last_seen_commit    TEXT,
    last_indexed_at     TEXT,
    deleted_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_pk);
CREATE INDEX IF NOT EXISTS idx_chunks_symbol_path ON chunks(symbol_path);
CREATE INDEX IF NOT EXISTS idx_chunks_symbol ON chunks(primary_symbol_pk);
CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(chunk_type);

-- External-content FTS5 over chunks.
-- Columns separated so BM25 weighting can rank symbol_name/signature above body.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    symbol_name,
    symbol_path,
    signature,
    file_path,
    content,
    content='chunks',
    content_rowid='chunk_pk',
    tokenize='unicode61 remove_diacritics 2'
);

-- Keep FTS in sync via triggers. Tombstoned rows (deleted_at IS NOT NULL) are
-- intentionally left in chunks but removed from chunks_fts by the update
-- pipeline using DELETE-then-INSERT rather than trigger logic, to keep
-- semantics explicit.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES (new.chunk_pk,
            COALESCE(new.symbol_name, ''),
            COALESCE(new.symbol_path, ''),
            COALESCE(new.signature, ''),
            COALESCE(new.file_path, ''),
            COALESCE(new.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES ('delete', old.chunk_pk,
            COALESCE(old.symbol_name, ''),
            COALESCE(old.symbol_path, ''),
            COALESCE(old.signature, ''),
            COALESCE(old.file_path, ''),
            COALESCE(old.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES ('delete', old.chunk_pk,
            COALESCE(old.symbol_name, ''),
            COALESCE(old.symbol_path, ''),
            COALESCE(old.signature, ''),
            COALESCE(old.file_path, ''),
            COALESCE(old.content, ''));
    INSERT INTO chunks_fts(rowid, symbol_name, symbol_path, signature, file_path, content)
    VALUES (new.chunk_pk,
            COALESCE(new.symbol_name, ''),
            COALESCE(new.symbol_path, ''),
            COALESCE(new.signature, ''),
            COALESCE(new.file_path, ''),
            COALESCE(new.content, ''));
END;

CREATE TABLE IF NOT EXISTS chunk_edits (
    edit_pk         INTEGER PRIMARY KEY,
    chunk_pk        INTEGER REFERENCES chunks(chunk_pk) ON DELETE SET NULL,
    chunk_uid       TEXT,                          -- retained even if chunk is hard-deleted later
    symbol_uid      TEXT,
    timestamp       TEXT NOT NULL,
    event_source    TEXT,                          -- init | update | watch | hook | manual
    commit_oid      TEXT,
    old_raw_hash    TEXT,
    new_raw_hash    TEXT,
    old_norm_hash   TEXT,
    new_norm_hash   TEXT,
    change_type     TEXT NOT NULL,                 -- create | update | delete | rename | move | split | merge
    changed_lines   INTEGER,
    diff_summary    TEXT
);

CREATE INDEX IF NOT EXISTS idx_chunk_edits_chunk ON chunk_edits(chunk_pk);
CREATE INDEX IF NOT EXISTS idx_chunk_edits_symbol ON chunk_edits(symbol_uid);
CREATE INDEX IF NOT EXISTS idx_chunk_edits_time ON chunk_edits(timestamp);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_pk              INTEGER PRIMARY KEY,
    run_id              TEXT NOT NULL UNIQUE,
    agent_name          TEXT,
    status              TEXT,
    prompt              TEXT,
    selected_nodes_json TEXT,
    started_at          TEXT,
    updated_at          TEXT,
    ended_at            TEXT,
    metadata_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_updated ON agent_runs(updated_at);

CREATE TABLE IF NOT EXISTS agent_events (
    event_pk     INTEGER PRIMARY KEY,
    run_pk       INTEGER NOT NULL REFERENCES agent_runs(run_pk) ON DELETE CASCADE,
    timestamp    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    file_path    TEXT,
    symbol_path  TEXT,
    message      TEXT,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_pk);
CREATE INDEX IF NOT EXISTS idx_agent_events_time ON agent_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_events_file ON agent_events(file_path);
CREATE INDEX IF NOT EXISTS idx_agent_events_type ON agent_events(event_type);

CREATE TABLE IF NOT EXISTS chunk_lineage (
    lineage_pk          INTEGER PRIMARY KEY,
    parent_chunk_pk     INTEGER REFERENCES chunks(chunk_pk) ON DELETE SET NULL,
    child_chunk_pk      INTEGER REFERENCES chunks(chunk_pk) ON DELETE SET NULL,
    relation_type       TEXT NOT NULL,             -- rename | move | split | merge
    created_at          TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    embedding_pk    INTEGER PRIMARY KEY,
    chunk_pk        INTEGER NOT NULL REFERENCES chunks(chunk_pk) ON DELETE CASCADE,
    provider        TEXT,
    model           TEXT,
    dimension       INTEGER,
    embedding_blob  BLOB,
    embedding_norm  REAL,
    content_hash    TEXT,                    -- chunks.raw_hash at embed time; drift-detect signal
    updated_at      TEXT
);

-- Prevent duplicate embeddings for the same (chunk, provider, model). Concurrent
-- `populate` passes used to be able to race past `INSERT OR REPLACE` and leave
-- two rows; the unique index is what actually enforces dedup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_chunk_provider_model
    ON embeddings(chunk_pk, provider, model);

CREATE TABLE IF NOT EXISTS test_edges (
    edge_pk             INTEGER PRIMARY KEY,
    test_chunk_pk       INTEGER NOT NULL REFERENCES chunks(chunk_pk) ON DELETE CASCADE,
    test_symbol_pk      INTEGER REFERENCES symbols(symbol_pk) ON DELETE CASCADE,
    target_symbol_pk    INTEGER NOT NULL REFERENCES symbols(symbol_pk) ON DELETE CASCADE,
    edge_type           TEXT,                      -- direct | transitive
    depth               INTEGER NOT NULL DEFAULT 1,
    confidence          REAL,
    path_json           TEXT,                      -- ordered symbol_uids along the shortest path, JSON array
    provenance          TEXT,
    UNIQUE(test_symbol_pk, target_symbol_pk)
);

CREATE INDEX IF NOT EXISTS idx_test_edges_target ON test_edges(target_symbol_pk);
CREATE INDEX IF NOT EXISTS idx_test_edges_test ON test_edges(test_symbol_pk);

CREATE TABLE IF NOT EXISTS unresolved_calls (
    unresolved_pk       INTEGER PRIMARY KEY,
    file_pk             INTEGER NOT NULL REFERENCES files(file_pk) ON DELETE CASCADE,
    src_symbol_uid      TEXT NOT NULL,
    relation_kind       TEXT NOT NULL,            -- calls | imports | inherits
    dst_candidates_json TEXT NOT NULL,            -- JSON array of canonical-name candidates
    site_line           INTEGER,
    provenance          TEXT,
    created_at          TEXT,
    resolved_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_unresolved_file ON unresolved_calls(file_pk);
CREATE INDEX IF NOT EXISTS idx_unresolved_src ON unresolved_calls(src_symbol_uid);
CREATE INDEX IF NOT EXISTS idx_unresolved_open ON unresolved_calls(resolved_at) WHERE resolved_at IS NULL;

CREATE TABLE IF NOT EXISTS repo_map_snapshots (
    snapshot_pk     INTEGER PRIMARY KEY,
    created_at      TEXT,
    commit_oid      TEXT,
    payload_json    TEXT
);

CREATE TABLE IF NOT EXISTS commits (
    commit_pk       INTEGER PRIMARY KEY,
    commit_oid      TEXT UNIQUE,
    author          TEXT,
    committed_at    TEXT,
    summary         TEXT
);

CREATE TABLE IF NOT EXISTS file_versions (
    version_pk      INTEGER PRIMARY KEY,
    file_pk         INTEGER NOT NULL REFERENCES files(file_pk) ON DELETE CASCADE,
    commit_pk       INTEGER REFERENCES commits(commit_pk) ON DELETE SET NULL,
    blob_oid        TEXT,
    size_bytes      INTEGER,
    observed_at     TEXT
);
