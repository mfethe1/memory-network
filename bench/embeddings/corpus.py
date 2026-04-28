"""Corpus preparation for the embeddings benchmark.

The repo root itself has `bench/queries.json` sitting in it, which leaks
the query text and its ground-truth target into any BM25 index built over
the working tree. To get honest BM25 numbers we copy only the source
trees we want to measure into a deterministic cache directory and build
the index there.

Cache layout:
    bench/embeddings/.corpus/<name>/
        <mirrored source trees>
        .code_index/
            index.db
            config.json
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import pipeline as pipeline_mod


HERE = Path(__file__).resolve().parent
CACHE_ROOT = HERE / ".corpus"

# Source trees mirrored into the benchmark corpus. Intentionally omits
# `bench/` (ground-truth leak), `docs/` (prose about the code, would
# dominate BM25), `benchmarks/` (vendored fastapi), and root-level
# scaffolding like CLAUDE.md and README.md.
SELF_SOURCE_DIRS = ("code_index", "tests")


def _mirror(src: Path, dst: Path) -> None:
    """Copy `src` into `dst`, overwriting. Skips __pycache__ and .pyc."""
    if dst.exists():
        shutil.rmtree(dst)

    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [
            n
            for n in names
            if n == "__pycache__" or n.endswith(".pyc") or n == ".code_index"
        ]

    shutil.copytree(src, dst, ignore=_ignore)


def prepare_self_corpus(
    repo_root: Path,
    *,
    refresh: bool = False,
) -> Path:
    """Build (or reuse) the 'self' benchmark corpus. Returns the corpus
    root. The returned path has a populated `.code_index/` already.

    If `refresh` is False and the corpus + DB already exist, the mirror
    step is skipped but reindex still runs to pick up source changes.
    """
    corpus_root = CACHE_ROOT / "self"
    corpus_root.mkdir(parents=True, exist_ok=True)

    # Mirror source trees on first build or when refresh is requested.
    need_mirror = refresh or not (corpus_root / "code_index").exists()
    if need_mirror:
        for d in SELF_SOURCE_DIRS:
            src = repo_root / d
            if not src.exists():
                continue
            _mirror(src, corpus_root / d)
        # Drop a sentinel .gitignore so `include_hidden=False` behavior
        # and ignore matching work identically to the real repo.
        (corpus_root / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n", encoding="utf-8"
        )

    # Always (re)load config and ensure the index is up to date.
    config = cfg_mod.load(corpus_root)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        cfg_mod.save(config)

    # Force a full reindex on first build or when mirror was refreshed.
    conn = db_mod.connect(config.db_path)
    db_mod.apply_schema(conn)
    chunk_count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL"
    ).fetchone()[0]
    db_mod.close(conn)

    if chunk_count == 0 or need_mirror:
        pipeline_mod.reindex(
            db_mod.connect(config.db_path),
            config,
            event_source="benchmark",
            force=True,
        )

    return corpus_root


def corpus_db_path(corpus_root: Path) -> Path:
    return corpus_root / ".code_index" / "index.db"
