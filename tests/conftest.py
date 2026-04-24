from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """A tiny sample repo used by pipeline tests."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        textwrap.dedent(
            '''
            """Sample module."""

            import os

            GREETING = "hi"


            def greet(name: str) -> str:
                """Return a greeting."""
                return f"{GREETING} {name}"


            class Counter:
                """A trivial counter."""

                def __init__(self) -> None:
                    self.value = 0

                def bump(self) -> int:
                    self.value += 1
                    return self.value
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# sample\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored_dir/\n*.log\n", encoding="utf-8")
    (tmp_path / "ignored_dir").mkdir()
    (tmp_path / "ignored_dir" / "nope.py").write_text(
        "SHOULD_NOT_BE_INDEXED = 1\n", encoding="utf-8"
    )
    (tmp_path / "trace.log").write_text("ignored log\n", encoding="utf-8")
    return tmp_path
