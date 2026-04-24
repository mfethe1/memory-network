"""Universal Ctags JSON adapter.

Scaffold only in v1: we probe for `ctags --output-format=json --version`
availability so `doctor` can report it, but extraction is deferred.
"""

from __future__ import annotations

import shutil
import subprocess


def available() -> bool:
    exe = shutil.which("ctags")
    if not exe:
        return False
    try:
        out = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = (out.stdout or "") + (out.stderr or "")
    return "Universal Ctags" in blob
