from __future__ import annotations

import re
from pathlib import Path

from code_index.openclaw_hostd.identity import load_or_create_host_identity


def test_host_identity_is_created_and_reused(tmp_path: Path) -> None:
    identity_path = tmp_path / "host-id.json"

    first = load_or_create_host_identity(identity_path)
    second = load_or_create_host_identity(identity_path)

    assert first == second
    assert re.fullmatch(r"host_[0-9a-f]{32}", first.host_id)
    assert identity_path.is_file()
