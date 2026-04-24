"""Robust ripgrep discovery.

Strategy, in order:
1. Explicit override: config.rg_path (string).
2. Environment: $CODE_INDEX_RG.
3. shutil.which("rg") / shutil.which("rg.exe").
4. Candidate install paths for this platform.

Returns a ResolvedRg describing the outcome. `doctor` surfaces the full
resolution trail so users can see exactly how (or whether) rg was found.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ResolvedRg:
    path: str | None
    source: str  # 'config' | 'env' | 'which' | 'candidate' | 'missing'
    tried: list[dict] = field(default_factory=list)
    version: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "version": self.version,
            "tried": list(self.tried),
        }


def _candidates() -> list[Path]:
    home = Path.home()
    out: list[Path] = []
    # Cross-platform: cargo install.
    out.append(home / ".cargo" / "bin" / "rg")
    out.append(home / ".cargo" / "bin" / "rg.exe")
    if os.name == "nt":
        out.extend(
            [
                home / "scoop" / "shims" / "rg.exe",
                home / "scoop" / "apps" / "ripgrep" / "current" / "rg.exe",
                Path("C:/ProgramData/chocolatey/bin/rg.exe"),
                Path("C:/msys64/mingw64/bin/rg.exe"),
                Path("C:/msys64/usr/bin/rg.exe"),
                home
                / "AppData"
                / "Local"
                / "Microsoft"
                / "WinGet"
                / "Links"
                / "rg.exe",
            ]
        )
        # WinGet packages live under AppData\Local\Microsoft\WinGet\Packages\BurntSushi.ripgrep.MSVC_*
        winget_root = home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
        if winget_root.is_dir():
            try:
                for child in winget_root.iterdir():
                    if "ripgrep" in child.name.lower():
                        for exe in child.rglob("rg.exe"):
                            out.append(exe)
                            break
            except OSError:
                pass
    else:
        out.extend(
            [
                Path("/usr/local/bin/rg"),
                Path("/usr/bin/rg"),
                Path("/opt/homebrew/bin/rg"),
                Path("/home/linuxbrew/.linuxbrew/bin/rg"),
                Path("/snap/bin/rg"),
            ]
        )
    return out


def _probe(path: str) -> str | None:
    """Return version string if path is an invokable ripgrep, else None."""
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    first = (proc.stdout or proc.stderr or "").splitlines()[:1]
    if not first:
        return None
    line = first[0].strip()
    if "ripgrep" not in line.lower():
        return None
    return line


def resolve(*, config_rg_path: str | None = None) -> ResolvedRg:
    resolved = ResolvedRg(path=None, source="missing")

    def _try(label: str, candidate: str | None) -> bool:
        if not candidate:
            return False
        entry = {"source": label, "candidate": candidate, "ok": False}
        resolved.tried.append(entry)
        version = _probe(candidate)
        if version is None:
            return False
        entry["ok"] = True
        resolved.path = candidate
        resolved.source = label
        resolved.version = version
        return True

    if config_rg_path and _try("config", str(config_rg_path)):
        return resolved

    env_rg = os.environ.get("CODE_INDEX_RG")
    if env_rg and _try("env", env_rg):
        return resolved

    for name in ("rg", "rg.exe"):
        found = shutil.which(name)
        if found and _try("which", found):
            return resolved

    for cand in _candidates():
        try:
            exists = cand.is_file()
        except OSError:
            exists = False
        if not exists:
            resolved.tried.append(
                {
                    "source": "candidate",
                    "candidate": str(cand),
                    "ok": False,
                    "reason": "not found",
                }
            )
            continue
        if _try("candidate", str(cand)):
            return resolved

    return resolved
