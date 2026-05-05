from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_openclaw_nats_preflight.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_openclaw_nats_preflight", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_nats_url_reads_protected_file_without_printing_secret(
    tmp_path: Path,
) -> None:
    script = _load_script()
    nats_url_file = tmp_path / "nats-url"
    nats_url_file.write_text(
        "nats://token-secret@example.invalid:4222\n",
        encoding="utf-8",
    )

    url = script.resolve_nats_url(
        argparse.Namespace(
            nats_url=None,
            nats_url_file=str(nats_url_file),
            allow_unauthenticated_url=False,
        )
    )

    assert url == "nats://token-secret@example.invalid:4222"
    assert script.redact_nats_url(url) == "nats://example.invalid:4222"


def test_resolve_nats_url_rejects_unauthenticated_cutover_url() -> None:
    script = _load_script()

    with pytest.raises(ValueError, match="authentication"):
        script.resolve_nats_url(
            argparse.Namespace(
                nats_url="nats://example.invalid:4222",
                nats_url_file=None,
                allow_unauthenticated_url=False,
            )
        )


def test_run_preflight_uses_secret_url_but_returns_redacted_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    seen: dict[str, object] = {}

    def fake_create_connection(address, timeout):
        seen["tcp_address"] = address
        seen["tcp_timeout"] = timeout

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        return Conn()

    class Client:
        async def flush(self, timeout):
            seen["flush_timeout"] = timeout

        async def close(self):
            seen["closed"] = True

    class NatsModule:
        async def connect(self, **kwargs):
            seen["connect_kwargs"] = kwargs
            return Client()

    monkeypatch.setattr(script.socket, "create_connection", fake_create_connection)

    result = asyncio.run(
        script.run_preflight(
            argparse.Namespace(
                nats_url="nats://token-secret@example.invalid:4222",
                nats_url_file=None,
                connect_timeout=3,
                allow_unauthenticated_url=False,
            ),
            nats_module=NatsModule(),
        ),
    )

    assert result == {
        "host": "example.invalid",
        "nats_authenticated": True,
        "ok": True,
        "port": 4222,
        "tcp_reachable": True,
        "url": "nats://example.invalid:4222",
    }
    assert seen["tcp_address"] == ("example.invalid", 4222)
    assert seen["connect_kwargs"] == {
        "servers": ["nats://token-secret@example.invalid:4222"],
        "connect_timeout": 3,
        "allow_reconnect": False,
        "max_reconnect_attempts": 0,
    }
