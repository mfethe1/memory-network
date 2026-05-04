"""Repo-local config for code_index."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIRNAME = ".code_index"
CONFIG_FILENAME = "config.json"
DB_FILENAME = "index.db"
LOCK_FILENAME = "index.lock"
HOOKS_DIRNAME = "hooks"


@dataclass
class Config:
    root: Path
    extra_ignore: list[str] = field(default_factory=list)
    include_hidden: bool = False
    max_file_bytes: int = 2 * 1024 * 1024
    languages: list[str] = field(default_factory=list)  # empty = auto
    rg_path: str | None = None  # explicit ripgrep binary override
    enable_jedi: bool = False  # opt-in Jedi-augmented call resolution

    @property
    def index_dir(self) -> Path:
        return self.root / CONFIG_DIRNAME

    @property
    def db_path(self) -> Path:
        return self.index_dir / DB_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.index_dir / LOCK_FILENAME

    @property
    def hooks_dir(self) -> Path:
        return self.index_dir / HOOKS_DIRNAME

    @property
    def config_path(self) -> Path:
        return self.index_dir / CONFIG_FILENAME

    def to_dict(self) -> dict[str, Any]:
        return {
            "extra_ignore": list(self.extra_ignore),
            "include_hidden": self.include_hidden,
            "max_file_bytes": self.max_file_bytes,
            "languages": list(self.languages),
            "rg_path": self.rg_path,
            "enable_jedi": self.enable_jedi,
        }


def find_root(start: Path) -> Path | None:
    """Walk upward looking for an existing .code_index/."""
    start = start.resolve()
    home = Path.home().resolve()
    for candidate in (start, *start.parents):
        if candidate == home and start != home:
            continue
        if (candidate / CONFIG_DIRNAME).is_dir():
            return candidate
    return None


def load(root: Path) -> Config:
    root = root.resolve()
    cfg_path = root / CONFIG_DIRNAME / CONFIG_FILENAME
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    return Config(
        root=root,
        extra_ignore=list(data.get("extra_ignore", [])),
        include_hidden=bool(data.get("include_hidden", False)),
        max_file_bytes=int(data.get("max_file_bytes", 2 * 1024 * 1024)),
        languages=list(data.get("languages", [])),
        rg_path=data.get("rg_path"),
        enable_jedi=bool(data.get("enable_jedi", False)),
    )


def save(config: Config) -> None:
    config.index_dir.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text(
        json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
