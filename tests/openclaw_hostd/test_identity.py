from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def test_concurrent_host_identity_creation_returns_one_stable_id(
    tmp_path: Path,
) -> None:
    identity_path = tmp_path / "host-id.json"

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = list(
            executor.map(
                lambda _: load_or_create_host_identity(identity_path),
                range(24),
            )
        )

    host_ids = {identity.host_id for identity in identities}
    assert len(host_ids) == 1
    assert load_or_create_host_identity(identity_path).host_id == identities[0].host_id
