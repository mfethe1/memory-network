Please improve this setup and challenge this as an expert in setting up codebase indexing systems.

Here is the expert-level revision of your prompt, heavily optimized for AI context retrieval, AST precision, and SQLite performance.
Improved Prompt Setup
# Role and Objective
You are an expert software architect and retrieval-augmented generation (RAG) specialist operating within the Claude Code environment. Your objective is to build a highly efficient, automated local codebase indexing, semantic chunking, and edit-tracking system.
# System Architecture & Requirements
Create a complete system that parses, chunks, indexes, and tracks edits to this codebase. You will implement this using a SQLite database (leveraging FTS5), AST-based semantic chunking logic, and a robust file-system watcher or Git hook mechanism.
Follow these strict implementation steps:
## Step 1: Directory & Setup
Create a dedicated hidden folder in the root directory named .code_index. This folder will house the SQLite database (index.db), the chunking scripts, the search utility, and hook configurations.
## Step 2: Semantic Chunking Engine (Context-Aware)
Write a chunking script utilizing an AST parser (like tree-sitter for multi-language support, or Python's ast) to split the codebase into meaningful structural boundaries.
• Target Size & Boundaries: Target individual Classes, Functions, and Methods. If a single function exceeds ~150-200 lines, use AST to chunk at logical internal boundaries (e.g., loops, conditionals) rather than arbitrary line counts.
• Context Preservation: The chunker MUST extract file-level imports and class-level definitions and attach them to the metadata of every child chunk. An isolated function chunk is useless without knowing its imports.
• Hashing: Generate a secure hash (e.g., SHA-256) of the chunk's normalized text to detect exact edits.
## Step 3: Optimized SQLite Database Schema
Initialize a SQLite database (.code_index/index.db). Do not compress the code text; instead, utilize SQLite's FTS5 extension for rapid keyword searching. Include the following schema:
1.	chunks (Standard Table):
• id (Text/UUID, Primary Key)
• file_path (Text)
• chunk_type (Text - e.g., 'class', 'function', 'module')
• start_line (Integer), end_line (Integer)
• context_imports (Text - JSON array of imports needed for this chunk)
• content_hash (Text)
• embedding (Blob - nullable, reserved for future vector search)
2.	chunks_fts (Virtual Table using FTS5):
• id (Unindexed - joins to chunks.id)
• content (Text - the raw code for fast BM25 keyword matching)
3.	chunk_edits (Audit/Hotspot Table):
• id (Primary Key)
• chunk_id (Foreign Key referencing chunks)
• timestamp (DateTime)
• diff_summary (Text)
• edit_count (Integer)
## Step 4: The Automation Mechanism
Implement a lightweight filesystem watcher (e.g., using Python's watchdog or Node's chokidar) running in the background, or a robust Git post-commit / post-merge hook.
• The Trigger: Detect modified files on save or commit.
• The Action: Pass only the modified files to the Chunking Engine. Compare new chunks against the content_hash of existing chunks for that file. Insert new chunks, update changed ones, and delete removed ones.
• Hotspot Tracking: For every chunk that changes hash, log an entry in chunk_edits, increment the edit_count, and generate a brief diff summary.
## Step 5: Search and Analytics Interface
Create a CLI utility (search_index) inside .code_index/ to query the database. It must include:
• --search "keyword": Uses SQLite FTS5 to perform blazingly fast full-text searches, returning the code, file path, and line numbers.
• --hotspots: Queries the chunk_edits table to return a ranked list of the most frequently edited chunks (the most volatile code in the system).
• --get-context <chunk_id>: Retrieves a specific chunk alongside its context_imports so an AI agent has the fully resolvable code block.
# Execution Instructions
Please write the initialization scripts for the Database (with FTS5 setup), the AST Semantic Chunking logic, the update mechanism, and the Search CLI now. Before writing the AST logic, please ask me for the primary programming language of this codebase so you can select the correct parsing library.

Your draft is already stronger than most code-indexing prompts, but I would still challenge it on a few important fronts.

1. **Tree-sitter terminology and the “primary language” gate**
   If you use Tree-sitter, calling the system “AST-based” is slightly off. Tree-sitter is an incremental parser that produces **concrete syntax trees**, and its AST-like utility comes from traversing **named nodes** and query captures. I would rewrite this as “syntax-tree-based semantic chunking with Tree-sitter named nodes and queries.” I would also stop blocking the implementation on a “primary language” question unless repo language detection is actually ambiguous; mixed-language repos are normal, and Tree-sitter is explicitly designed for language-general parsing. ([Tree-sitter][1])

2. **Your FTS layout is not the best source-of-truth design**
   The current schema makes the search index awkwardly close to the canonical storage for code text. SQLite FTS5 supports indexing **content stored elsewhere**, but SQLite is explicit that you must keep that external-content index in sync yourself, commonly with triggers. That is the right pattern here: keep canonical code in `chunks.content`, then let `chunks_fts` index it. Also, do not collapse search into a single `content` field. FTS5 supports BM25 column weighting, and SQLite notes that sorting by `rank` can be faster than calling `bm25()` directly in `ORDER BY`, so you want separate searchable fields like `symbol_name`, `signature`, `content`, and `file_path`. ([SQLite][2])

3. **Your identity model will make hotspot history drift**
   A random UUID is fine as an external identifier, but it is the wrong hot-path identity for SQLite joins and FTS row mapping. I would use an `INTEGER PRIMARY KEY` internally, keep a stable text `chunk_uid` or `symbol_key` as a unique secondary key, and track `symbol_path`, `parent_symbol`, and lineage. Otherwise renames, moves, chunk splits, and function extractions will look like deletes plus inserts, which makes hotspot tracking noisy and historically misleading.

4. **The hook/watcher plan needs a cleaner separation of concerns**
   Git hooks do not run just because files exist in `.code_index`; by default Git looks in `$GIT_DIR/hooks`, unless you explicitly set `core.hooksPath`. Also, `post-commit` alone is not enough. Git has separate lifecycle hooks for commits, branch switches/checkouts, merges/pulls, and rewritten history (`--amend`, `rebase`), so a repo-state-consistent indexer should at minimum account for `post-commit`, `post-checkout`, `post-merge`, and `post-rewrite`. I would make `watch` mode a convenience feature and `update` a deterministic CLI entrypoint that both hooks and the watcher call. ([Git][3])

5. **`context_imports` is too narrow for AI retrieval**
   For AI context resolution, imports alone are not enough. You usually want module/namespace, enclosing class/interface signature, decorators/annotations, generic parameters, base types, docstring/header summary, and parser diagnostics. Storing that as JSON text is perfectly reasonable in SQLite: JSON functions are built in by default in modern SQLite, and JSON is stored as ordinary text. ([SQLite][4])

6. **Your operational SQLite guidance is underspecified**
   If you use SQLite seriously here, spell out the runtime posture: `WAL` mode, `busy_timeout`, `foreign_keys`, and `PRAGMA optimize` policy. SQLite now recommends `PRAGMA optimize` when short-lived connections close, or `PRAGMA optimize=0x10002` when long-lived connections open plus periodic `PRAGMA optimize` afterward. Also, WAL mode is persistent once enabled, automatic checkpoints are on by default, and there is a recent WAL-reset bug fix that matters if you rely on concurrent WAL usage; pinning a current SQLite build is worth stating explicitly. ([SQLite][5])

Here is the version I would actually hand to an agent.

```text
# Role and Objective
You are an expert software architect building a local, language-aware code index optimized for AI context retrieval, structural search, and incremental maintenance. Your goal is to build a robust indexing system for this repository using SQLite + FTS5, syntax-tree-based semantic chunking, and deterministic incremental updates.

Prioritize:
1. Stable symbol identity across edits, renames, and refactors
2. Fast local search and context retrieval
3. Low-latency incremental updates
4. Deterministic, inspectable behavior over “magic”
5. Strong failure handling for unsupported or partially broken files

# Non-Negotiable Design Rules
- Do not assume this is a single-language repository.
- First inspect the repository to detect dominant languages from file extensions, manifests, lockfiles, and build files.
- Ask the user for the primary language only if detection is ambiguous or if the parser/runtime choice materially changes the implementation.
- Use Tree-sitter for supported languages, but treat it as a syntax-tree engine. Build an AST-like semantic layer using named nodes, fields, and queries.
- Prefer deterministic CLI workflows over background daemons. Implement init/update/watch modes. Watch mode is optional convenience, not the core system.
- Keep runtime state in `.code_index/`.
- If hook scripts are stored under `.code_index/hooks`, install them by configuring Git to use that directory via `core.hooksPath`.
- Ignore `.git`, `.code_index`, common build output, vendored dependencies, binaries, generated files, and anything excluded by `.gitignore` unless explicitly told otherwise.
- Do not store embeddings in the hot `chunks` table. Use a separate table reserved for future vector search.

# Required System Layout
Create `.code_index/` in the repository root. It must contain:
- `index.db`
- one main CLI entrypoint (preferred: `code_index`) with subcommands
- parser/chunking logic
- update logic
- hook scripts or hook templates
- a small manifest/config file describing supported languages and ignore rules

Preferred CLI shape:
- `code_index init`
- `code_index update --files <...>`
- `code_index watch`
- `code_index search --query "..."`
- `code_index hotspots`
- `code_index context --chunk-id <id>`
- `code_index install-hooks`

# Database Requirements
Use SQLite with:
- FTS5 enabled
- WAL mode
- foreign keys enabled
- a busy timeout
- a documented optimize/analyze strategy

Use an internal INTEGER PRIMARY KEY for hot-path joins and FTS row mapping.
Also store a stable text identifier for external use.

Create these tables:

1. `files`
Purpose: one row per indexed file
Fields should include at least:
- file_pk (INTEGER PRIMARY KEY)
- file_path (TEXT UNIQUE)
- language (TEXT)
- file_hash (TEXT)
- git_oid (TEXT, nullable)
- size_bytes (INTEGER)
- mtime_ns (INTEGER, nullable)
- parse_status (TEXT)
- parse_error (TEXT, nullable)
- indexed_at (DATETIME)

2. `chunks`
Purpose: canonical chunk storage
Fields should include at least:
- chunk_pk (INTEGER PRIMARY KEY)
- chunk_uid (TEXT UNIQUE)
- file_pk (INTEGER FK -> files.file_pk)
- file_path (TEXT, denormalized for convenience)
- language (TEXT)
- chunk_type (TEXT; e.g. module, class, interface, enum, function, method, block)
- symbol_name (TEXT)
- symbol_path (TEXT)              # e.g. package.module.Class.method
- parent_symbol_path (TEXT, nullable)
- signature (TEXT, nullable)
- start_line, end_line (INTEGER)
- start_byte, end_byte (INTEGER, nullable)
- context_json (TEXT)             # JSON metadata bundle
- content (TEXT)                  # canonical raw code text
- raw_hash (TEXT)                 # exact-text hash
- normalized_hash (TEXT)          # semantic-ish normalized hash
- edit_count (INTEGER DEFAULT 0)
- last_seen_commit (TEXT, nullable)
- last_indexed_at (DATETIME)
- deleted_at (DATETIME, nullable) # tombstone support preferred

3. `chunks_fts`
Purpose: FTS5 index over canonical chunk content
Requirements:
- use an external-content FTS5 table over `chunks`
- index at least: `symbol_name`, `signature`, `content`, `file_path`
- support weighted ranking so symbol names/signatures rank above body text

4. `chunk_edits`
Purpose: append-only audit log
Fields should include at least:
- edit_pk (INTEGER PRIMARY KEY)
- chunk_pk (INTEGER FK -> chunks.chunk_pk)
- timestamp (DATETIME)
- event_source (TEXT; watch, hook, manual, init)
- commit_oid (TEXT, nullable)
- old_hash (TEXT, nullable)
- new_hash (TEXT, nullable)
- change_type (TEXT; create, update, delete, rename, move, split, merge)
- changed_lines (INTEGER, nullable)
- diff_summary (TEXT)

5. `chunk_lineage` (recommended)
Purpose: preserve identity across chunk splits/merges/renames
Fields should include at least:
- parent_chunk_pk
- child_chunk_pk
- relation_type

6. `embeddings` (optional, reserved for future)
Purpose: keep vectors out of hot tables
Fields should include at least:
- chunk_pk
- provider
- model
- dimension
- embedding_blob
- updated_at

# Semantic Chunking Requirements
Chunk semantically, not by arbitrary line count.

Primary chunk targets:
- modules / files
- classes / interfaces / enums / structs / traits
- functions / methods
- significant top-level declarations
- optionally meaningful inner blocks for oversized functions

Oversized function policy:
- Do not blindly split at 150–200 lines.
- Use syntax-tree boundaries first.
- Split only when a function exceeds the target budget and the resulting child chunks remain semantically resolvable.
- Candidate split points: major conditionals, loops, match/case branches, nested helper functions, or coherent statement groups.
- Every child chunk must retain enough parent context to be understandable.

For each chunk, capture:
- symbol name
- fully qualified symbol path
- signature / header
- exact source span
- enclosing type / module
- raw content
- raw hash + normalized hash

`context_json` must include as available:
- imports / usings / requires
- module / namespace / package
- enclosing class or interface signature
- decorators / annotations / attributes
- generic/type parameters
- base classes / implemented interfaces
- docstring or leading comment summary
- parser diagnostics / parse quality
- file-level exports or public declarations

Use normalized hashing to detect semantically equivalent chunks after formatting-only edits.
Also preserve a raw exact-text hash so true text changes remain detectable.

# Language Support Strategy
- Build a parser registry keyed by language.
- Auto-detect repository languages first.
- Prefer Tree-sitter where available.
- Fall back to language-native parsers when materially better.
- Fall back to a conservative text chunker for unsupported file types.
- Record parse quality in metadata so downstream tools know whether a chunk is syntax-derived or heuristic.

# Incremental Update Rules
Implement three deterministic paths:

1. `init`
- full repository scan
- build file table, chunk table, FTS index, and edit baseline

2. `update --files`
- accept an explicit list of changed files
- skip unchanged files via file hash / mtime / git oid checks
- reparse only touched files
- compare old and new chunk sets by stable symbol identity first
- fall back to normalized-hash and span heuristics when identity is unclear
- upsert changed chunks
- tombstone removed chunks
- append `chunk_edits` rows
- increment `chunks.edit_count` for changed chunks
- preserve lineage for renames, moves, splits, and merges when reasonably inferable

3. `watch`
- optional convenience mode
- debounce filesystem events
- batch changed paths
- call the same underlying `update` logic
- never contain separate indexing logic from `update`

# Git Automation Rules
Support hook-driven updates through `.code_index/hooks` and install them via `core.hooksPath`.

At minimum, support:
- `post-commit`
- `post-checkout`
- `post-merge`
- `post-rewrite`

Behavior:
- hooks gather changed paths or changed refs
- hooks call the same deterministic `code_index update` entrypoint
- do not duplicate indexing logic inside each hook script

# Search and Retrieval Interface
The CLI must support:

1. `search`
Example:
- `code_index search --query "keyword"`
Requirements:
- use FTS5
- return file path, symbol path, chunk type, line range, score, and snippet
- support optional filters: language, path glob, chunk type
- support JSON output for agents
- rank symbol/signature hits above body-only hits

2. `hotspots`
Example:
- `code_index hotspots`
Requirements:
- rank frequently edited chunks by stable identity
- support optional recency filtering
- prefer edit history that survives renames and moves where possible

3. `context`
Example:
- `code_index context --chunk-id <id>`
Requirements:
- return the canonical chunk plus its context bundle
- support expansion modes such as parent, siblings, and nearby tests
- output should be agent-friendly and optionally JSON

# Operational Requirements
- prevent concurrent writers with a lock file or transaction strategy
- log parse failures without aborting the whole run
- keep the index usable even when some files fail to parse
- document ignore rules and parser coverage
- keep the implementation small, inspectable, and runnable locally

# Delivery Instructions
Implement the full system now:
- SQLite initialization
- schema creation
- chunking engine
- incremental updater
- hook installation logic
- search / hotspots / context CLI
- ignore handling
- parser registry

Before choosing the parsing library, inspect the repository to detect languages and existing toolchains.
Ask the user one concise question only if language detection is ambiguous or if a parser/runtime choice cannot be made responsibly from the repository itself.
Otherwise proceed without blocking.
```

The single biggest architectural upgrade here is this: **make `chunks` the source of truth, make FTS an index, and make `update` the single deterministic pathway that both hooks and watch mode call.** That one change prevents a lot of long-term pain.

[1]: https://tree-sitter.github.io/tree-sitter/ "Introduction - Tree-sitter"
[2]: https://sqlite.org/fts5.html "SQLite FTS5 Extension"
[3]: https://git-scm.com/docs/githooks "Git - githooks Documentation"
[4]: https://sqlite.org/json1.html "JSON Functions And Operators"
[5]: https://sqlite.org/lang_analyze.html "ANALYZE"

I don’t see a repository mounted in this session, so I can’t do the repo-first language detection or implement the concrete code against real manifests/build files yet. As an architecture review, though: yes, this is a very recognizable pattern. Mature systems usually split the problem into fast lexical retrieval, language-aware symbol/cross-reference facts, and fallback indexing for broad language coverage. Zoekt is explicit about fast trigram-based code search with symbol-aware ranking, ripgrep is optimized for gitignore-aware recursive text search, SCIP is a language-agnostic code-intelligence protocol, clangd maintains whole-codebase indexes for LSP features, Kythe/Glean model code as facts and relations, and OpenGrok still combines indexing, cross-references, ctags, and history. Your design is basically a small local SQLite synthesis of those families. ([GitHub][1])

The part I would change is the center of gravity. I would not make `chunks` the primary semantic identity layer. clangd treats `Symbol`s and `SymbolID`s as primary indexed objects, SCIP is organized around documents/symbols/occurrences, and Kythe/Glean are explicitly fact/edge-oriented. My inference from those systems is that the durable core of your local DB should be symbol/occurrence/relation data, with chunks sitting on top as retrieval-oriented projections. That also lines up with SCIP’s own design note that it is meant as a transmission format rather than a storage format. ([Clangd][2])

What I’d pull in to improve your setup:

1. **Add `symbols`, `occurrences`, and `relations` tables, and make `chunk_uid` secondary.**
   Keep `chunks` for agent retrieval, but introduce `symbol_uid` as the durable identity. Use language-native IDs when available, and fall back to qualified name + container + signature + kind when not. This mirrors the way clangd centers its index on `SymbolID`, and the way SCIP centers on symbols/occurrences rather than free-form chunks. ([Clangd][2])

2. **Keep SQLite FTS5, but add a text-first fast path.**
   SQLite FTS5 is a good local choice because it supports BM25 weighting by column and `snippet()` extraction, so your idea of ranking `symbol_name`/`signature` above body text is solid. But for literal/regex/path-glob queries, the model to borrow from is Zoekt/ripgrep: very fast lexical filtering first, then richer ranking/context after. In practice that means either an optional ripgrep/Zoekt prefilter or at least a dedicated text-search mode separate from semantic retrieval. ([SQLite][3])

3. **Make structural search a first-class command, not just a chunking implementation detail.**
   Tree-sitter’s query language is specifically designed for pattern matching over syntax trees, and Tree-sitter distinguishes named nodes from anonymous tokens so you can build an AST-like layer on top of CSTs. ast-grep is the clearest example of the UX payoff here: syntax-aware search and rewrite exposed directly to users. I’d add a `code_index search-ast` or `code_index query` subcommand that runs Tree-sitter queries and returns symbol/chunk hits. ([Tree-sitter][4])

4. **Use Universal Ctags JSON as the fallback before heuristic text chunking.**
   Your “unsupported language => conservative text chunker” rule is good, but there should be a middle layer first. Universal Ctags can emit structured JSON with language, kind, scope, and other fields, and OpenGrok still uses ctags in its indexing pipeline alongside xrefs/history. That gives you a much better fallback for symbol extraction than pure text heuristics. ([Universal Ctags][5])

5. **Use Tree-sitter incrementality mainly for watch mode, not as a hard dependency for `update --files`.**
   Tree-sitter is an incremental parsing library and is excellent for long-lived edit sessions, but your deterministic CLI modes do not need to preserve parse trees across processes to get most of the benefit. For `init` and `update --files`, reparsing touched files and diffing old/new symbol+chunk sets is simpler and usually good enough. clangd’s split between per-file dynamic indexing and full-project background/static indexes is a useful mental model here. ([Tree-sitter][6])

6. **Be explicit about SQLite operational rules.**
   Your SQLite direction is good, but FTS5 external-content tables come with an important caveat: SQLite says it is your responsibility to keep them consistent with the content table, suggests triggers as one approach, and provides `rebuild` when they drift. WAL is also the right default for local tooling, but SQLite documents that it only allows one writer at a time and does not work over network filesystems. `PRAGMA optimize` is now the recommended maintenance path, and SQLite documents running it on connection close or periodically for long-lived connections; busy timeouts help with lock waits, but each connection still has only one busy handler. So I would keep your explicit writer lock, app-managed FTS updates in the same transaction, and a visible `rebuild-fts`/`vacuum`/`optimize` maintenance story. ([SQLite][3])

7. **Your hook installation choice is correct; your ignore baseline should be Git-aware by default.**
   Git officially supports relocating hooks via `core.hooksPath`, so `.code_index/hooks` plus `install-hooks` is the right shape. For ignores, ripgrep is a good model: respect `.gitignore` and skip hidden/binary files by default unless the user opts out. ([Git][7])

Two design tweaks I’d make immediately even before coding:

* Replace `git_oid` with something more precise like `head_blob_oid`, `worktree_hash`, `is_tracked`, and `is_dirty`. A single nullable `git_oid` blurs together committed and uncommitted states.
* Compute hotspots on `symbol_uid` or a lineage root, not on `chunk_pk`. Hotspots are about stable entities, not the current shape of chunk boundaries.

And one thing I would de-scope in v1: aggressive split/merge lineage for arbitrary inner blocks. Start with file/module, type, function, and method chunks. Only create inner-block chunks as secondary “expansion” chunks for oversized functions, and don’t let them become the main identity unit. That gets you most of the retrieval value without turning lineage inference into the hardest part of the system.

So the answer is: **yes, the setup is grounded in common practice, but the best improvement is to make it more explicitly hybrid** — symbol graph at the core, retrieval chunks as a projection, FTS/text search as a separate fast path, and AST query as a first-class capability. Once the repository is available, the next concrete step is a repo-specific pass over manifests, lockfiles, file extensions, and build files to decide parser coverage and runtime.

[1]: https://github.com/sourcegraph/zoekt "GitHub - sourcegraph/zoekt: Fast trigram based code search · GitHub"
[2]: https://clangd.llvm.org/design/indexing "The clangd index"
[3]: https://sqlite.org/fts5.html "SQLite FTS5 Extension"
[4]: https://tree-sitter.github.io/tree-sitter/using-parsers/queries/index.html?utm_source=chatgpt.com "Queries - Tree-sitter"
[5]: https://docs.ctags.io/en/stable/man/ctags-json-output.5.html "ctags-json-output — Universal Ctags 0.3.0 documentation"
[6]: https://tree-sitter.github.io/tree-sitter/ "Introduction - Tree-sitter"
[7]: https://git-scm.com/docs/githooks?utm_source=chatgpt.com "Git - githooks Documentation"

Further improve this suggestion and challenge the approach with the best performing and latest technology. Also determine if this helps coding agents or if there are better methods to improve their performance in large repos. Work in the latest ways to improve coding agents with hooks and setups that compliment the indexing solution and synergistically help improve codebase tracking, indexing, and version control.

# Role and Objective
You are an expert software architect building a local, language-aware code index optimized for AI context retrieval, structural search, and incremental maintenance. Your goal is to build a robust indexing system for this repository using SQLite + FTS5, syntax-tree-based semantic chunking, and deterministic incremental updates.

Prioritize:
1. Stable symbol identity across edits, renames, and refactors
2. Fast local search and context retrieval
3. Low-latency incremental updates
4. Deterministic, inspectable behavior over “magic”
5. Strong failure handling for unsupported or partially broken files

# Non-Negotiable Design Rules
- Do not assume this is a single-language repository.
- First inspect the repository to detect dominant languages from file extensions, manifests, lockfiles, and build files.
- Ask the user for the primary language only if detection is ambiguous or if the parser/runtime choice materially changes the implementation.
- Use Tree-sitter for supported languages, but treat it as a syntax-tree engine. Build an AST-like semantic layer using named nodes, fields, and queries.
- Prefer deterministic CLI workflows over background daemons. Implement init/update/watch modes. Watch mode is optional convenience, not the core system.
- Keep runtime state in `.code_index/`.
- If hook scripts are stored under `.code_index/hooks`, install them by configuring Git to use that directory via `core.hooksPath`.
- Ignore `.git`, `.code_index`, common build output, vendored dependencies, binaries, generated files, and anything excluded by `.gitignore` unless explicitly told otherwise.
- Do not store embeddings in the hot `chunks` table. Use a separate table reserved for future vector search.

# Required System Layout
Create `.code_index/` in the repository root. It must contain:
- `index.db`
- one main CLI entrypoint (preferred: `code_index`) with subcommands
- parser/chunking logic
- update logic
- hook scripts or hook templates
- a small manifest/config file describing supported languages and ignore rules

Preferred CLI shape:
- `code_index init`
- `code_index update --files <...>`
- `code_index watch`
- `code_index search --query "..."`
- `code_index hotspots`
- `code_index context --chunk-id <id>`
- `code_index install-hooks`

# Database Requirements
Use SQLite with:
- FTS5 enabled
- WAL mode
- foreign keys enabled
- a busy timeout
- a documented optimize/analyze strategy

Use an internal INTEGER PRIMARY KEY for hot-path joins and FTS row mapping.
Also store a stable text identifier for external use.

Create these tables:

1. `files`
Purpose: one row per indexed file
Fields should include at least:
- file_pk (INTEGER PRIMARY KEY)
- file_path (TEXT UNIQUE)
- language (TEXT)
- file_hash (TEXT)
- git_oid (TEXT, nullable)
- size_bytes (INTEGER)
- mtime_ns (INTEGER, nullable)
- parse_status (TEXT)
- parse_error (TEXT, nullable)
- indexed_at (DATETIME)

2. `chunks`
Purpose: canonical chunk storage
Fields should include at least:
- chunk_pk (INTEGER PRIMARY KEY)
- chunk_uid (TEXT UNIQUE)
- file_pk (INTEGER FK -> files.file_pk)
- file_path (TEXT, denormalized for convenience)
- language (TEXT)
- chunk_type (TEXT; e.g. module, class, interface, enum, function, method, block)
- symbol_name (TEXT)
- symbol_path (TEXT)              # e.g. package.module.Class.method
- parent_symbol_path (TEXT, nullable)
- signature (TEXT, nullable)
- start_line, end_line (INTEGER)
- start_byte, end_byte (INTEGER, nullable)
- context_json (TEXT)             # JSON metadata bundle
- content (TEXT)                  # canonical raw code text
- raw_hash (TEXT)                 # exact-text hash
- normalized_hash (TEXT)          # semantic-ish normalized hash
- edit_count (INTEGER DEFAULT 0)
- last_seen_commit (TEXT, nullable)
- last_indexed_at (DATETIME)
- deleted_at (DATETIME, nullable) # tombstone support preferred

3. `chunks_fts`
Purpose: FTS5 index over canonical chunk content
Requirements:
- use an external-content FTS5 table over `chunks`
- index at least: `symbol_name`, `signature`, `content`, `file_path`
- support weighted ranking so symbol names/signatures rank above body text

4. `chunk_edits`
Purpose: append-only audit log
Fields should include at least:
- edit_pk (INTEGER PRIMARY KEY)
- chunk_pk (INTEGER FK -> chunks.chunk_pk)
- timestamp (DATETIME)
- event_source (TEXT; watch, hook, manual, init)
- commit_oid (TEXT, nullable)
- old_hash (TEXT, nullable)
- new_hash (TEXT, nullable)
- change_type (TEXT; create, update, delete, rename, move, split, merge)
- changed_lines (INTEGER, nullable)
- diff_summary (TEXT)

5. `chunk_lineage` (recommended)
Purpose: preserve identity across chunk splits/merges/renames
Fields should include at least:
- parent_chunk_pk
- child_chunk_pk
- relation_type

6. `embeddings` (optional, reserved for future)
Purpose: keep vectors out of hot tables
Fields should include at least:
- chunk_pk
- provider
- model
- dimension
- embedding_blob
- updated_at

# Semantic Chunking Requirements
Chunk semantically, not by arbitrary line count.

Primary chunk targets:
- modules / files
- classes / interfaces / enums / structs / traits
- functions / methods
- significant top-level declarations
- optionally meaningful inner blocks for oversized functions

Oversized function policy:
- Do not blindly split at 150–200 lines.
- Use syntax-tree boundaries first.
- Split only when a function exceeds the target budget and the resulting child chunks remain semantically resolvable.
- Candidate split points: major conditionals, loops, match/case branches, nested helper functions, or coherent statement groups.
- Every child chunk must retain enough parent context to be understandable.

For each chunk, capture:
- symbol name
- fully qualified symbol path
- signature / header
- exact source span
- enclosing type / module
- raw content
- raw hash + normalized hash

`context_json` must include as available:
- imports / usings / requires
- module / namespace / package
- enclosing class or interface signature
- decorators / annotations / attributes
- generic/type parameters
- base classes / implemented interfaces
- docstring or leading comment summary
- parser diagnostics / parse quality
- file-level exports or public declarations

Use normalized hashing to detect semantically equivalent chunks after formatting-only edits.
Also preserve a raw exact-text hash so true text changes remain detectable.

# Language Support Strategy
- Build a parser registry keyed by language.
- Auto-detect repository languages first.
- Prefer Tree-sitter where available.
- Fall back to language-native parsers when materially better.
- Fall back to a conservative text chunker for unsupported file types.
- Record parse quality in metadata so downstream tools know whether a chunk is syntax-derived or heuristic.

# Incremental Update Rules
Implement three deterministic paths:

1. `init`
- full repository scan
- build file table, chunk table, FTS index, and edit baseline

2. `update --files`
- accept an explicit list of changed files
- skip unchanged files via file hash / mtime / git oid checks
- reparse only touched files
- compare old and new chunk sets by stable symbol identity first
- fall back to normalized-hash and span heuristics when identity is unclear
- upsert changed chunks
- tombstone removed chunks
- append `chunk_edits` rows
- increment `chunks.edit_count` for changed chunks
- preserve lineage for renames, moves, splits, and merges when reasonably inferable

3. `watch`
- optional convenience mode
- debounce filesystem events
- batch changed paths
- call the same underlying `update` logic
- never contain separate indexing logic from `update`

# Git Automation Rules
Support hook-driven updates through `.code_index/hooks` and install them via `core.hooksPath`.

At minimum, support:
- `post-commit`
- `post-checkout`
- `post-merge`
- `post-rewrite`

Behavior:
- hooks gather changed paths or changed refs
- hooks call the same deterministic `code_index update` entrypoint
- do not duplicate indexing logic inside each hook script

# Search and Retrieval Interface
The CLI must support:

1. `search`
Example:
- `code_index search --query "keyword"`
Requirements:
- use FTS5
- return file path, symbol path, chunk type, line range, score, and snippet
- support optional filters: language, path glob, chunk type
- support JSON output for agents
- rank symbol/signature hits above body-only hits

2. `hotspots`
Example:
- `code_index hotspots`
Requirements:
- rank frequently edited chunks by stable identity
- support optional recency filtering
- prefer edit history that survives renames and moves where possible

3. `context`
Example:
- `code_index context --chunk-id <id>`
Requirements:
- return the canonical chunk plus its context bundle
- support expansion modes such as parent, siblings, and nearby tests
- output should be agent-friendly and optionally JSON

# Operational Requirements
- prevent concurrent writers with a lock file or transaction strategy
- log parse failures without aborting the whole run
- keep the index usable even when some files fail to parse
- document ignore rules and parser coverage
- keep the implementation small, inspectable, and runnable locally

# Delivery Instructions
Implement the full system now:
- SQLite initialization
- schema creation
- chunking engine
- incremental updater
- hook installation logic
- search / hotspots / context CLI
- ignore handling
- parser registry

Before choosing the parsing library, inspect the repository to detect languages and existing toolchains.
Ask the user one concise question only if language detection is ambiguous or if a parser/runtime choice cannot be made responsibly from the repository itself.
Otherwise proceed without blocking.


As an architecture review, yes, this is a very recognizable pattern. Mature systems usually split the problem into fast lexical retrieval, language-aware symbol/cross-reference facts, and fallback indexing for broad language coverage. Zoekt is explicit about fast trigram-based code search with symbol-aware ranking, ripgrep is optimized for gitignore-aware recursive text search, SCIP is a language-agnostic code-intelligence protocol, clangd maintains whole-codebase indexes for LSP features, Kythe/Glean model code as facts and relations, and OpenGrok still combines indexing, cross-references, ctags, and history. Your design is basically a small local SQLite synthesis of those families.  

The part I would change is the center of gravity. I would not make chunks the primary semantic identity layer. clangd treats Symbols and SymbolIDs as primary indexed objects, SCIP is organized around documents/symbols/occurrences, and Kythe/Glean are explicitly fact/edge-oriented. My inference from those systems is that the durable core of your local DB should be symbol/occurrence/relation data, with chunks sitting on top as retrieval-oriented projections. That also lines up with SCIP’s own design note that it is meant as a transmission format rather than a storage format.  

What I’d pull in to improve your setup:

1. Add symbols, occurrences, and relations tables, and make chunk_uid secondary.
    Keep chunks for agent retrieval, but introduce symbol_uid as the durable identity. Use language-native IDs when available, and fall back to qualified name + container + signature + kind when not. This mirrors the way clangd centers its index on SymbolID, and the way SCIP centers on symbols/occurrences rather than free-form chunks.  
2. Keep SQLite FTS5, but add a text-first fast path.
    SQLite FTS5 is a good local choice because it supports BM25 weighting by column and snippet() extraction, so your idea of ranking symbol_name/signature above body text is solid. But for literal/regex/path-glob queries, the model to borrow from is Zoekt/ripgrep: very fast lexical filtering first, then richer ranking/context after. In practice that means either an optional ripgrep/Zoekt prefilter or at least a dedicated text-search mode separate from semantic retrieval.  
3. Make structural search a first-class command, not just a chunking implementation detail.
    Tree-sitter’s query language is specifically designed for pattern matching over syntax trees, and Tree-sitter distinguishes named nodes from anonymous tokens so you can build an AST-like layer on top of CSTs. ast-grep is the clearest example of the UX payoff here: syntax-aware search and rewrite exposed directly to users. I’d add a code_index search-ast or code_index query subcommand that runs Tree-sitter queries and returns symbol/chunk hits.  
4. Use Universal Ctags JSON as the fallback before heuristic text chunking.
    Your “unsupported language => conservative text chunker” rule is good, but there should be a middle layer first. Universal Ctags can emit structured JSON with language, kind, scope, and other fields, and OpenGrok still uses ctags in its indexing pipeline alongside xrefs/history. That gives you a much better fallback for symbol extraction than pure text heuristics.  
5. Use Tree-sitter incrementality mainly for watch mode, not as a hard dependency for update --files.
    Tree-sitter is an incremental parsing library and is excellent for long-lived edit sessions, but your deterministic CLI modes do not need to preserve parse trees across processes to get most of the benefit. For init and update --files, reparsing touched files and diffing old/new symbol+chunk sets is simpler and usually good enough. clangd’s split between per-file dynamic indexing and full-project background/static indexes is a useful mental model here.  
6. Be explicit about SQLite operational rules.
    Your SQLite direction is good, but FTS5 external-content tables come with an important caveat: SQLite says it is your responsibility to keep them consistent with the content table, suggests triggers as one approach, and provides rebuild when they drift. WAL is also the right default for local tooling, but SQLite documents that it only allows one writer at a time and does not work over network filesystems. PRAGMA optimize is now the recommended maintenance path, and SQLite documents running it on connection close or periodically for long-lived connections; busy timeouts help with lock waits, but each connection still has only one busy handler. So I would keep your explicit writer lock, app-managed FTS updates in the same transaction, and a visible rebuild-fts/vacuum/optimize maintenance story.  
7. Your hook installation choice is correct; your ignore baseline should be Git-aware by default.
    Git officially supports relocating hooks via core.hooksPath, so .code_index/hooks plus install-hooks is the right shape. For ignores, ripgrep is a good model: respect .gitignore and skip hidden/binary files by default unless the user opts out.  

Two design tweaks I’d make immediately even before coding:

* Replace git_oid with something more precise like head_blob_oid, worktree_hash, is_tracked, and is_dirty. A single nullable git_oid blurs together committed and uncommitted states.
* Compute hotspots on symbol_uid or a lineage root, not on chunk_pk. Hotspots are about stable entities, not the current shape of chunk boundaries.

And one thing I would de-scope in v1: aggressive split/merge lineage for arbitrary inner blocks. Start with file/module, type, function, and method chunks. Only create inner-block chunks as secondary “expansion” chunks for oversized functions, and don’t let them become the main identity unit. That gets you most of the retrieval value without turning lineage inference into the hardest part of the system.

So the answer is: yes, the setup is grounded in common practice, but the best improvement is to make it more explicitly hybrid — symbol graph at the core, retrieval chunks as a projection, FTS/text search as a separate fast path, and AST query as a first-class capability. Once the repository is available, the next concrete step is a repo-specific pass over manifests, lockfiles, file extensions, and build files to decide parser coverage and runtime.
Your design is directionally right, but I would not ship it as “a chunk index with some semantic metadata.” I would ship it as a hybrid code-memory system whose durable core is symbols, occurrences, and relations, with chunks as a retrieval projection on top. That shift matches how clangd structures its index around Symbol, Ref, and Relation, how SCIP organizes documents around symbols and occurrences, and how Kythe and Glean model code as facts and edges rather than free-form chunks.

That matters for coding agents because recent evidence says the real bottleneck is not “more scaffolding” by itself, but better context selection and use. ContextBench (Feb 2026) evaluated 1,136 issue-resolution tasks from 66 repositories across eight languages and found only marginal context-retrieval gains from sophisticated agent scaffolding, with substantial gaps between explored context and actually used context. Sourcegraph’s Cody work likewise frames the problem as a two-stage retrieval-and-ranking system, and current agent tools still add explicit codebase context layers such as repo maps and codebase indexing instead of trusting raw long context alone.

What I would change
Make symbol_uid the primary semantic identity; keep chunk_uid secondary.
Use chunks for retrieval and context packing, but persist durable identities in symbols, occurrences, and relations. For supported languages, prefer language-native semantic IDs from existing indexers and compiler/LSP frontends; Sourcegraph’s SCIP docs explicitly recommend using a compiler frontend or language server as the basis for occurrence data, and the current SCIP ecosystem already covers Java/Kotlin/Scala, TS/JS, Rust, C/C++, Ruby, Python, C#, Dart, and PHP.
Keep SQLite, but do not make SQLite the only search path.
SQLite FTS5 is still a very good local control plane because it gives you external-content indexing, BM25 ranking, and per-column weighting, which is perfect for ranking symbol_name and signature above body text. But for literal, regex, path, and broad prefilter queries, use a fast lexical front end first—ripgrep for single-user local workflows or an optional Zoekt layer for very large monorepos/multi-repo search. ripgrep defaults to respecting ignore files and skipping hidden/binary files, and Zoekt is explicitly built for fast trigram-based code search with symbol-aware ranking.
Make structural search first-class.
Tree-sitter is an incremental parsing library that gives you concrete syntax trees, and its query system supports matching node types plus named fields. That is strong enough to expose code_index query/search-ast directly instead of hiding structure only inside the chunker. I would also support ast-grep-style UX for pattern search because that is exactly the “grep, but syntax-aware” interaction agents and humans want.
Use Tree-sitter for syntax, not as your highest-quality semantic source.
Tree-sitter should own chunking, AST querying, and graceful fallback; it should not be your only source of semantic identity in languages where compiler-backed indexers already exist. The right priority order is: native semantic indexer/SCIP emitter first, Tree-sitter second, Universal Ctags JSON third, heuristic text chunker last. Universal Ctags’ JSON output is structured enough to be a materially better fallback than plain text heuristics.
Challenge the “chunk lineage everywhere” ambition.
I would preserve lineage aggressively for files, types, functions, and methods, but I would de-scope inner-block split/merge lineage in v1. Tree-sitter queries and fields are excellent for creating oversized-function expansion chunks, but making those inner blocks the main identity layer will cost a lot and buy less than symbol-first identity plus good context assembly. That tradeoff is also consistent with clangd/SCIP/Kythe-style systems, which center semantic entities and relations rather than arbitrary sub-function fragments.
Be stricter about SQLite operations.
External-content FTS5 tables are your responsibility to keep in sync; SQLite recommends triggers as one way to do that and provides a rebuild command when they drift. WAL is still a good default, but SQLite documents that there is only one writer at a time and that WAL is not suitable for network filesystems. SQLite now recommends PRAGMA optimize after schema changes and periodically on long-lived connections rather than hand-tuning ANALYZE.
What this becomes for coding agents

Yes, this will help coding agents. It will improve grounding, symbol lookup, relevant-file selection, API usage discovery, hotspot analysis, and change-impact assembly. That aligns with how Aider uses a concise repo map of important symbols and signatures, how Cursor and Windsurf describe codebase indexing as part of context retrieval, and how Cody describes context retrieval/ranking as the crucial search layer inside the assistant.

But no, this is not the only or even the highest-leverage large-repo improvement by itself. The stronger pattern is:

Index + repo map. A concise global map is often a better first context than raw chunks. Aider explicitly sends a repository map with key files, symbols, and signatures alongside user requests.
Index + planning/impact analysis. CodePlan treats repository-level work as a planning problem using dependency analysis and adaptive multi-step edits instead of one-shot retrieval only.
Index + execution feedback. CoCoGen uses compiler feedback plus repository context to iteratively fix project-context errors and reported over 80% improvement for project-context-dependent generation in Python versus vanilla baselines.
Index + graph retrieval. GraphCoder reported higher exact-match results than baseline retrieval methods by using a code context graph and coarse-to-fine graph retrieval.
Index + adaptive pruning. SWE-Pruner reported 23–54% token reduction on agent tasks with minimal performance impact by doing task-aware context pruning instead of fixed compression.
Index + persistent graph memory. A March 2026 preprint, Codebase-Memory, reported 10x fewer tokens and 2.1x fewer tool calls than a file-exploration agent, though with somewhat lower answer quality overall (83% vs 92%). I would treat that as promising but still early evidence.

So the upgraded answer is: the index helps, but agents in large repos improve more when the index is part of a wider context-engineering stack that includes repo maps, impact analysis, execution feedback, pruning, and agent-facing tooling.

The architecture I would actually build

I would turn your design into five layers:

1. Semantic spine
files, symbols, occurrences, relations, symbol_aliases, diagnostics, commits, file_versions.

2. Retrieval projection
chunks, chunks_fts, repo_map_snapshots, hotspots, test_edges, lineage_roots.

3. Search engines

lexical: ripgrep fast path, optional Zoekt for huge repos
structural: Tree-sitter query / ast-grep
ranked retrieval: SQLite FTS5 with BM25 weights

4. Agent interface
code_index mcp serve, JSON-first CLI output, stable chunk/symbol URIs, agent-specific instruction files.

5. Automation
Git hooks, agent hooks, CI/webhook feeds, nightly verify/rebuild.

The key schema addition is not another chunk table. It is:

symbols(symbol_pk, symbol_uid, kind, canonical_name, container_symbol_pk, signature_norm, semantic_source, confidence, first_seen_commit, last_seen_commit, deleted_at)
occurrences(occurrence_pk, symbol_pk, file_pk, role, start_line, end_line, start_byte, end_byte, syntax_kind)
relations(src_symbol_pk, dst_symbol_pk, relation_kind, provenance, weight)
chunks(..., primary_symbol_pk nullable, chunk_uid, chunk_kind, context_json, raw_hash, normalized_hash, deleted_at)
test_edges(test_chunk_pk, target_symbol_pk, edge_type, confidence)
diagnostics(file_pk, tool, code, severity, range, message, observed_at)

That gives you stable identity for hotspots, change history, callers/callees, implementations, and affected-test lookup without forcing chunks to do semantic work they are not good at.

Search and update behavior I’d prefer

For any query, classify intent first:

literal/path/regex → rg/Zoekt prefilter, then optional chunk ranking
symbol lookup → symbols/occurrences
structural pattern → Tree-sitter query / ast-grep
“explain/refactor this area” → repo map + chunks + graph neighbors
“what breaks if I change this?” → relations + recent diffs + tests + diagnostics

For updates:

init: full scan, emit semantic spine first, then retrieval projections
update --files: reparse touched files, reconcile by symbol_uid first, then normalized hash/span heuristics
watch: just debounce and call the same update path
rebuild-fts: explicit maintenance command
doctor: coverage, drift, parse failures, ignored-file stats, unsupported-language stats

I would also add code_index grep, code_index symbol, code_index query, code_index impact, code_index tests, code_index mcp-serve, and code_index doctor. The current CLI shape is good, but it is missing the commands agents most often need.

Hooks and setups that actually synergize

Git hooks
Your core.hooksPath idea is right; Git explicitly supports relocating hooks via core.hooksPath, and the hooks you named exist and are appropriate: post-commit, post-checkout, post-merge, and post-rewrite. Use those to gather changed paths or rewritten refs and call the same code_index update entrypoint.

Codex setup
For Codex, the high-value setup is not generic hooks; it is small AGENTS.md guidance + skills + MCP + selective subagents. Codex’s customization model is explicitly built around project guidance (AGENTS.md), memories, skills, MCP, and subagents as complementary layers, and AGENTS.md is discovered from the Git root downward. Codex also supports MCP in the CLI and IDE, and Codex CLI can itself run as an MCP server for deterministic, reviewable multi-agent workflows. Use subagents for parallel review lanes, but not for routine context lookup, because they consume more tokens than single-agent runs.

Claude Code setup
For Claude Code, the highest-value additions are CLAUDE.md, hooks, MCP, and channels. Claude documents CLAUDE.md as persistent project memory, keeps project instructions and hooks under .claude/, and says a PostToolUse hook can fire after edits. Its hooks are designed for deterministic control and can be shell commands, HTTP endpoints, or prompt hooks. Claude also supports MCP, and channels can push CI/webhook events into a live session. That means a strong setup is: keep a short CLAUDE.md; add a PostToolUse hook that reindexes changed files; add a stop/task-finish hook that runs affected tests and stores diagnostics; and push CI/lint failures back into the session through channels when you want live agent follow-up.

MCP surface for the index
MCP is the agent-neutral integration point I would bet on. MCP separates prompts, resources, and tools: prompts are user-controlled, resources are application-controlled context, and tools are model-controlled actions. It also gives you roots to bound filesystem scope. I would expose:

resources: codeindex://repo-map, codeindex://hotspots, codeindex://symbol/<uid>, codeindex://chunk/<uid>, codeindex://diff/<base>...HEAD
tools: search_text, search_symbol, query_ast, get_context, impact_analysis, affected_tests, get_recent_failures, update_index
prompts: review_diff, safe_rename, explain_module, investigate_failure
That matches the MCP primitive model and makes the index usable from Codex, Claude Code, and any other MCP-capable client without bespoke plugins. MCP’s current published roadmap for 2026 is focused on transport scalability and agent communication, so this is also the most future-proof integration bet right now.
What I would explicitly not do

I would not:

make chunks the primary identity layer
let embeddings become the default retrieval path in v1
split functions aggressively by line count
build separate logic in watch mode
bet only on Tree-sitter when native semantic emitters exist
assume a larger context window removes the need for retrieval, pruning, or execution feedback

The strongest current pattern is still hybrid context engineering, not one giant index and not one giant prompt.
Your design is directionally right, but I would not ship it as “a chunk index with some semantic metadata.” I would ship it as a hybrid code-memory system whose durable core is symbols, occurrences, and relations, with chunks as a retrieval projection on top. That shift matches how clangd structures its index around Symbol, Ref, and Relation, how SCIP organizes documents around symbols and occurrences, and how Kythe and Glean model code as facts and edges rather than free-form chunks. ￼ That matters for coding agents because recent evidence says the real bottleneck is not “more scaffolding” by itself, but better context selection and use. ContextBench (Feb 2026) evaluated 1,136 issue-resolution tasks from 66 repositories across eight languages and found only marginal context-retrieval gains from sophisticated agent scaffolding, with substantial gaps between explored context and actually used context. Sourcegraph’s Cody work likewise frames the problem as a two-stage retrieval-and-ranking system, and current agent tools still add explicit codebase context layers such as repo maps and codebase indexing instead of trusting raw long context alone. ￼ What I would change 1. Make symbol_uid the primary semantic identity; keep chunk_uid secondary. Use chunks for retrieval and context packing, but persist durable identities in symbols, occurrences, and relations. For supported languages, prefer language-native semantic IDs from existing indexers and compiler/LSP frontends; Sourcegraph’s SCIP docs explicitly recommend using a compiler frontend or language server as the basis for occurrence data, and the current SCIP ecosystem already covers Java/Kotlin/Scala, TS/JS, Rust, C/C++, Ruby, Python, C#, Dart, and PHP. ￼ 2. Keep SQLite, but do not make SQLite the only search path. SQLite FTS5 is still a very good local control plane because it gives you external-content indexing, BM25 ranking, and per-column weighting, which is perfect for ranking symbol_name and signature above body text. But for literal, regex, path, and broad prefilter queries, use a fast lexical front end first—ripgrep for single-user local workflows or an optional Zoekt layer for very large monorepos/multi-repo search. ripgrep defaults to respecting ignore files and skipping hidden/binary files, and Zoekt is explicitly built for fast trigram-based code search with symbol-aware ranking. ￼ 3. Make structural search first-class. Tree-sitter is an incremental parsing library that gives you concrete syntax trees, and its query system supports matching node types plus named fields. That is strong enough to expose code_index query/search-ast directly instead of hiding structure only inside the chunker. I would also support ast-grep-style UX for pattern search because that is exactly the “grep, but syntax-aware” interaction agents and humans want. ￼ 4. Use Tree-sitter for syntax, not as your highest-quality semantic source. Tree-sitter should own chunking, AST querying, and graceful fallback; it should not be your only source of semantic identity in languages where compiler-backed indexers already exist. The right priority order is: native semantic indexer/SCIP emitter first, Tree-sitter second, Universal Ctags JSON third, heuristic text chunker last. Universal Ctags’ JSON output is structured enough to be a materially better fallback than plain text heuristics. ￼ 5. Challenge the “chunk lineage everywhere” ambition. I would preserve lineage aggressively for files, types, functions, and methods, but I would de-scope inner-block split/merge lineage in v1. Tree-sitter queries and fields are excellent for creating oversized-function expansion chunks, but making those inner blocks the main identity layer will cost a lot and buy less than symbol-first identity plus good context assembly. That tradeoff is also consistent with clangd/SCIP/Kythe-style systems, which center semantic entities and relations rather than arbitrary sub-function fragments. ￼ 6. Be stricter about SQLite operations. External-content FTS5 tables are your responsibility to keep in sync; SQLite recommends triggers as one way to do that and provides a rebuild command when they drift. WAL is still a good default, but SQLite documents that there is only one writer at a time and that WAL is not suitable for network filesystems. SQLite now recommends PRAGMA optimize after schema changes and periodically on long-lived connections rather than hand-tuning ANALYZE. ￼ What this becomes for coding agents Yes, this will help coding agents. It will improve grounding, symbol lookup, relevant-file selection, API usage discovery, hotspot analysis, and change-impact assembly. That aligns with how Aider uses a concise repo map of important symbols and signatures, how Cursor and Windsurf describe codebase indexing as part of context retrieval, and how Cody describes context retrieval/ranking as the crucial search layer inside the assistant. ￼ But no, this is not the only or even the highest-leverage large-repo improvement by itself. The stronger pattern is: * Index + repo map. A concise global map is often a better first context than raw chunks. Aider explicitly sends a repository map with key files, symbols, and signatures alongside user requests. ￼ * Index + planning/impact analysis. CodePlan treats repository-level work as a planning problem using dependency analysis and adaptive multi-step edits instead of one-shot retrieval only. ￼ * Index + execution feedback. CoCoGen uses compiler feedback plus repository context to iteratively fix project-context errors and reported over 80% improvement for project-context-dependent generation in Python versus vanilla baselines. ￼ * Index + graph retrieval. GraphCoder reported higher exact-match results than baseline retrieval methods by using a code context graph and coarse-to-fine graph retrieval. ￼ * Index + adaptive pruning. SWE-Pruner reported 23–54% token reduction on agent tasks with minimal performance impact by doing task-aware context pruning instead of fixed compression. ￼ * Index + persistent graph memory. A March 2026 preprint, Codebase-Memory, reported 10x fewer tokens and 2.1x fewer tool calls than a file-exploration agent, though with somewhat lower answer quality overall (83% vs 92%). I would treat that as promising but still early evidence. ￼ So the upgraded answer is: the index helps, but agents in large repos improve more when the index is part of a wider context-engineering stack that includes repo maps, impact analysis, execution feedback, pruning, and agent-facing tooling. ￼ The architecture I would actually build I would turn your design into five layers: 1. Semantic spine files, symbols, occurrences, relations, symbol_aliases, diagnostics, commits, file_versions. 2. Retrieval projection chunks, chunks_fts, repo_map_snapshots, hotspots, test_edges, lineage_roots. 3. Search engines * lexical: ripgrep fast path, optional Zoekt for huge repos * structural: Tree-sitter query / ast-grep * ranked retrieval: SQLite FTS5 with BM25 weights 4. Agent interface code_index mcp serve, JSON-first CLI output, stable chunk/symbol URIs, agent-specific instruction files. 5. Automation Git hooks, agent hooks, CI/webhook feeds, nightly verify/rebuild. The key schema addition is not another chunk table. It is: * symbols(symbol_pk, symbol_uid, kind, canonical_name, container_symbol_pk, signature_norm, semantic_source, confidence, first_seen_commit, last_seen_commit, deleted_at) * occurrences(occurrence_pk, symbol_pk, file_pk, role, start_line, end_line, start_byte, end_byte, syntax_kind) * relations(src_symbol_pk, dst_symbol_pk, relation_kind, provenance, weight) * chunks(..., primary_symbol_pk nullable, chunk_uid, chunk_kind, context_json, raw_hash, normalized_hash, deleted_at) * test_edges(test_chunk_pk, target_symbol_pk, edge_type, confidence) * diagnostics(file_pk, tool, code, severity, range, message, observed_at) That gives you stable identity for hotspots, change history, callers/callees, implementations, and affected-test lookup without forcing chunks to do semantic work they are not good at. Search and update behavior I’d prefer For any query, classify intent first: * literal/path/regex → rg/Zoekt prefilter, then optional chunk ranking * symbol lookup → symbols/occurrences * structural pattern → Tree-sitter query / ast-grep * “explain/refactor this area” → repo map + chunks + graph neighbors * “what breaks if I change this?” → relations + recent diffs + tests + diagnostics For updates: * init: full scan, emit semantic spine first, then retrieval projections * update --files: reparse touched files, reconcile by symbol_uid first, then normalized hash/span heuristics * watch: just debounce and call the same update path * rebuild-fts: explicit maintenance command * doctor: coverage, drift, parse failures, ignored-file stats, unsupported-language stats I would also add code_index grep, code_index symbol, code_index query, code_index impact, code_index tests, code_index mcp-serve, and code_index doctor. The current CLI shape is good, but it is missing the commands agents most often need. Hooks and setups that actually synergize Git hooks Your core.hooksPath idea is right; Git explicitly supports relocating hooks via core.hooksPath, and the hooks you named exist and are appropriate: post-commit, post-checkout, post-merge, and post-rewrite. Use those to gather changed paths or rewritten refs and call the same code_index update entrypoint. ￼ Codex setup For Codex, the high-value setup is not generic hooks; it is small AGENTS.md guidance + skills + MCP + selective subagents. Codex’s customization model is explicitly built around project guidance (AGENTS.md), memories, skills, MCP, and subagents as complementary layers, and AGENTS.md is discovered from the Git root downward. Codex also supports MCP in the CLI and IDE, and Codex CLI can itself run as an MCP server for deterministic, reviewable multi-agent workflows. Use subagents for parallel review lanes, but not for routine context lookup, because they consume more tokens than single-agent runs. ￼ Claude Code setup For Claude Code, the highest-value additions are CLAUDE.md, hooks, MCP, and channels. Claude documents CLAUDE.md as persistent project memory, keeps project instructions and hooks under .claude/, and says a PostToolUse hook can fire after edits. Its hooks are designed for deterministic control and can be shell commands, HTTP endpoints, or prompt hooks. Claude also supports MCP, and channels can push CI/webhook events into a live session. That means a strong setup is: keep a short CLAUDE.md; add a PostToolUse hook that reindexes changed files; add a stop/task-finish hook that runs affected tests and stores diagnostics; and push CI/lint failures back into the session through channels when you want live agent follow-up. ￼ MCP surface for the index MCP is the agent-neutral integration point I would bet on. MCP separates prompts, resources, and tools: prompts are user-controlled, resources are application-controlled context, and tools are model-controlled actions. It also gives you roots to bound filesystem scope. I would expose: * resources: codeindex://repo-map, codeindex://hotspots, codeindex://symbol/<uid>, codeindex://chunk/<uid>, codeindex://diff/<base>...HEAD * tools: search_text, search_symbol, query_ast, get_context, impact_analysis, affected_tests, get_recent_failures, update_index * prompts: review_diff, safe_rename, explain_module, investigate_failure That matches the MCP primitive model and makes the index usable from Codex, Claude Code, and any other MCP-capable client without bespoke plugins. MCP’s current published roadmap for 2026 is focused on transport scalability and agent communication, so this is also the most future-proof integration bet right now. ￼ What I would explicitly not do I would not: * make chunks the primary identity layer * let embeddings become the default retrieval path in v1 * split functions aggressively by line count * build separate logic in watch mode * bet only on Tree-sitter when native semantic emitters exist * assume a larger context window removes the need for retrieval, pruning, or execution feedback The strongest current pattern is still hybrid context engineering, not one giant index and not one giant prompt. ￼