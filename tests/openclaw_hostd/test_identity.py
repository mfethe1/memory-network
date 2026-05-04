from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
import time
from pathlib import Path

import pytest

from code_index.openclaw_hostd.identity import HostIdentity
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
    for round_index in range(20):
        identity_path = tmp_path / f"round-{round_index}" / "host-id.json"

        with ThreadPoolExecutor(max_workers=16) as executor:
            identities = list(
                executor.map(
                    lambda _: load_or_create_host_identity(identity_path),
                    range(64),
                )
            )

        host_ids = {identity.host_id for identity in identities}
        assert len(host_ids) == 1
        assert (
            load_or_create_host_identity(identity_path).host_id
            == identities[0].host_id
        )


def test_host_identity_retries_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_path = tmp_path / "host-id.json"
    expected = load_or_create_host_identity(identity_path)
    original_read_text = Path.read_text
    failures_remaining = 2

    def flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal failures_remaining
        if self == identity_path and failures_remaining > 0:
            failures_remaining -= 1
            raise PermissionError("simulated transient Windows file access race")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    assert load_or_create_host_identity(identity_path) == expected
    assert failures_remaining == 0


def test_host_identity_retries_partial_json_while_lock_exists(tmp_path: Path) -> None:
    identity_path = tmp_path / "host-id.json"
    lock_path = identity_path.with_suffix(identity_path.suffix + ".lock")
    identity_path.write_text('{"host_id":', encoding="utf-8")
    lock_path.write_text("creating\n", encoding="utf-8")
    expected = HostIdentity(host_id="host_0123456789abcdef0123456789abcdef")

    def finish_creation() -> None:
        time.sleep(0.05)
        identity_path.write_text(
            json.dumps(expected.to_dict()) + "\n",
            encoding="utf-8",
        )
        lock_path.unlink()

    thread = threading.Thread(target=finish_creation)
    thread.start()
    try:
        assert load_or_create_host_identity(identity_path) == expected
    finally:
        thread.join(timeout=1)
