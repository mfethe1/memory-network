"""Corpus preparation for the retrieval benchmark.

The benchmark mirrors only source and test trees into an isolated corpus. That
keeps checked-in benchmark cases out of both the broker FTS index and the rg
baseline, and it avoids touching the worktree's live `.code_index` database.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from code_index import config as cfg_mod
from code_index import db_router as db_mod
from code_index import pipeline as pipeline_mod


HERE = Path(__file__).resolve().parent
PERSISTENT_CORPUS_ROOT = HERE / ".corpus" / "self"
SELF_SOURCE_DIRS = ("code_index", "tests")


@dataclass
class PreparedCorpus:
    root: Path
    db_path: Path
    _tempdir: tempfile.TemporaryDirectory[str] | None = None

    def cleanup(self) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None

    def __enter__(self) -> "PreparedCorpus":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()


def prepare_self_corpus(repo_root: Path, *, keep: bool = False) -> PreparedCorpus:
    tempdir: tempfile.TemporaryDirectory[str] | None = None
    if keep:
        corpus_root = PERSISTENT_CORPUS_ROOT
        if corpus_root.exists():
            shutil.rmtree(corpus_root)
        corpus_root.mkdir(parents=True)
    else:
        tempdir = tempfile.TemporaryDirectory(prefix="retrieval-bench-")
        corpus_root = Path(tempdir.name)

    for dirname in SELF_SOURCE_DIRS:
        src = repo_root / dirname
        if src.exists():
            _mirror(src, corpus_root / dirname)

    (corpus_root / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.code_index/\n", encoding="utf-8"
    )

    config = cfg_mod.load(corpus_root)
    config.index_dir.mkdir(parents=True, exist_ok=True)
    cfg_mod.save(config)
    conn = db_mod.connect(config.db_path)
    try:
        db_mod.apply_schema(conn)
        pipeline_mod.reindex(
            conn,
            config,
            event_source="retrieval-benchmark",
            force=True,
        )
    finally:
        db_mod.close(conn)

    return PreparedCorpus(root=corpus_root, db_path=config.db_path, _tempdir=tempdir)


def _mirror(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)

    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [
            name
            for name in names
            if name == "__pycache__"
            or name == ".code_index"
            or name.endswith(".pyc")
        ]

    shutil.copytree(src, dst, ignore=ignore)
