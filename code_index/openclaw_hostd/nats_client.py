"""Thin synchronous NATS lifecycle wrapper for OpenClaw hostd."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import inspect
import json
import re
import threading
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit


AGENT_STATES_BUCKET = "openclaw_agent_states"


class NatsUnavailableError(RuntimeError):
    """Raised when NATS is requested without an available transport."""


@runtime_checkable
class NatsTransport(Protocol):
    def connect(self) -> Any:
        ...

    def publish(self, subject: str, payload: bytes) -> Any:
        ...

    def subscribe(self, subject: str, callback: Callable[[Any], Any]) -> Any:
        ...

    def close(self) -> Any:
        ...


class NatsClient:
    """Explicit connect/publish/subscribe/close wrapper around a transport.

    The project deliberately does not depend on a live NATS package yet. Tests
    and host integrations can inject a transport with compatible methods; when
    none is supplied, lifecycle methods fail closed with a clear error.
    """

    def __init__(self, *, transport: Any | None = None) -> None:
        self._transport = transport
        self.connected = False

    def connect(self) -> None:
        transport = self._require_transport()
        connect = getattr(transport, "connect", None)
        if connect is None:
            raise NatsUnavailableError("configured NATS transport has no connect()")
        connect()
        self.connected = True

    def publish(self, subject: str, payload: Any) -> Any:
        self._ensure_connected()
        subject = _subject(subject)
        publish = getattr(self._transport, "publish", None)
        if publish is None:
            raise NatsUnavailableError("configured NATS transport has no publish()")
        return publish(subject, _payload_bytes(payload))

    def subscribe(self, subject: str, callback: Callable[[Any], Any]) -> Any:
        self._ensure_connected()
        subject = _subject(subject)
        subscribe = getattr(self._transport, "subscribe", None)
        if subscribe is None:
            raise NatsUnavailableError("configured NATS transport has no subscribe()")
        return subscribe(subject, callback)

    def kv_put(
        self,
        bucket: str,
        key: str,
        value: Any,
        *,
        ttl_seconds: int | float | None = None,
    ) -> Any:
        self._ensure_connected()
        bucket = _text(bucket, field_name="bucket")
        key = _text(key, field_name="key")
        if ttl_seconds is not None and not getattr(
            self._transport,
            "supports_kv_ttl",
            False,
        ):
            raise NatsUnavailableError(
                "configured NATS transport does not explicitly support KV TTL"
            )
        kv_put = getattr(self._transport, "kv_put", None)
        if kv_put is not None:
            return kv_put(
                bucket,
                key,
                _payload_bytes(value),
                ttl_seconds=ttl_seconds,
            )
        kv_bucket = getattr(self._transport, "kv_bucket", None)
        if kv_bucket is None:
            raise NatsUnavailableError("configured NATS transport has no KV support")
        bucket_client = kv_bucket(bucket)
        return bucket_client.put(
            key,
            _payload_bytes(value),
            ttl_seconds=ttl_seconds,
        )

    def close(self) -> None:
        if self._transport is not None:
            close = getattr(self._transport, "close", None)
            if close is not None:
                close()
        self.connected = False

    def _require_transport(self) -> Any:
        if self._transport is None:
            raise NatsUnavailableError(
                "NATS transport is unavailable; inject a transport before connecting"
            )
        return self._transport

    def _ensure_connected(self) -> None:
        self._require_transport()
        if not self.connected:
            raise NatsUnavailableError("NATS client is not connected")


def _subject(value: str) -> str:
    return _text(value, field_name="subject")


def _text(value: str, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"NATS {field_name} is required")
    return text


def _payload_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class AgentRunState:
    agent_id: str
    task_id: str
    run_id: str
    current_subtask: str = ""
    active_files: tuple[str, ...] = ()
    active_symbols: tuple[str, ...] = ()
    loaded_context_handles: tuple[Mapping[str, Any], ...] = ()
    estimated_tokens: int = 0
    approach_history: tuple[str, ...] = ()
    last_action_at: str | datetime | None = None


def publish_agent_state_entries(
    nats_client: Any,
    *,
    host_id: str,
    active_agent_runs: Iterable[AgentRunState | Mapping[str, Any]],
    heartbeat_interval_seconds: int,
    now: datetime | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    generated_at = now or datetime.now(timezone.utc)
    ttl_seconds = max(1, int(heartbeat_interval_seconds)) * 3
    host_id = _text(host_id, field_name="host_id")
    published: list[tuple[str, dict[str, Any]]] = []
    for run in active_agent_runs:
        payload = _agent_state_payload(run, host_id=host_id, now=generated_at)
        key = f"{_kv_key_part(host_id)}.{_kv_key_part(payload['run_id'])}"
        nats_client.kv_put(
            AGENT_STATES_BUCKET,
            key,
            payload,
            ttl_seconds=ttl_seconds,
        )
        published.append((key, payload))
    return published


def _agent_state_payload(
    run: AgentRunState | Mapping[str, Any],
    *,
    host_id: str,
    now: datetime,
) -> dict[str, Any]:
    agent_id = _text(_state_get(run, "agent_id"), field_name="agent_id")
    task_id = _text(_state_get(run, "task_id"), field_name="task_id")
    run_id = _text(_state_get(run, "run_id"), field_name="run_id")
    last_action_at = _state_get(run, "last_action_at")
    return {
        "agent_id": agent_id,
        "host_id": host_id,
        "task_id": task_id,
        "run_id": run_id,
        "current_subtask": str(_state_get(run, "current_subtask") or ""),
        "active_files_json": _json_array_field(
            run,
            value_name="active_files",
            json_name="active_files_json",
        ),
        "active_symbols_json": _json_array_field(
            run,
            value_name="active_symbols",
            json_name="active_symbols_json",
        ),
        "loaded_context_handles_json": _json_array_field(
            run,
            value_name="loaded_context_handles",
            json_name="loaded_context_handles_json",
        ),
        "estimated_tokens": _non_negative_int(_state_get(run, "estimated_tokens")),
        "approach_history_json": _json_array_field(
            run,
            value_name="approach_history",
            json_name="approach_history_json",
        ),
        "last_action_at": _datetime_text(last_action_at, default=now),
    }


def _state_get(run: AgentRunState | Mapping[str, Any], key: str) -> Any:
    if isinstance(run, Mapping):
        return run.get(key)
    return getattr(run, key, None)


def _json_array_field(
    run: AgentRunState | Mapping[str, Any],
    *,
    value_name: str,
    json_name: str,
) -> str:
    raw_json = _state_get(run, json_name)
    if raw_json is not None:
        text = str(raw_json)
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError(f"{json_name} must encode a JSON array")
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    value = _state_get(run, value_name)
    if value is None:
        value = ()
    if isinstance(value, str):
        items: list[Any] = [value]
    else:
        items = list(value)
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def _datetime_text(value: Any, *, default: datetime) -> str:
    if value is None or value == "":
        value = default
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _kv_key_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unknown"


class NatsPyTransport:
    """Synchronous adapter around the optional nats-py async client."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout_seconds: float = 2.0,
        operation_timeout_seconds: float = 5.0,
    ) -> None:
        self.url = _nats_url(url)
        try:
            import nats  # type: ignore[import-not-found]
        except ImportError as exc:
            raise NatsUnavailableError(
                "OPENCLAW_HOSTD_NATS_URL is configured, but optional nats-py "
                "package is not installed"
            ) from exc
        self._nats = nats
        self._connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))
        self._operation_timeout_seconds = max(0.1, float(operation_timeout_seconds))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Any | None = None
        self._subscriptions: list[Any] = []
        self.supports_kv_ttl = True

    def connect(self) -> None:
        if self._client is not None:
            return
        self._start_loop()

        async def _connect() -> None:
            self._client = await self._nats.connect(
                servers=[self.url],
                connect_timeout=self._connect_timeout_seconds,
            )

        try:
            self._run(_connect())
        except Exception:
            self.close()
            raise

    def publish(self, subject: str, payload: bytes) -> None:
        client = self._require_client()

        async def _publish() -> None:
            await client.publish(subject, payload)
            await client.flush()

        self._run(_publish())

    def subscribe(self, subject: str, callback: Callable[[Any], Any]) -> Any:
        client = self._require_client()

        async def _wrapped(message: Any) -> None:
            result = await asyncio.to_thread(callback, message)
            if inspect.isawaitable(result):
                await result

        async def _subscribe() -> Any:
            subscription = await client.subscribe(subject, cb=_wrapped)
            self._subscriptions.append(subscription)
            return subscription

        return self._run(_subscribe())

    def kv_put(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        ttl_seconds: int | float | None = None,
    ) -> Any:
        client = self._require_client()

        async def _kv_put() -> Any:
            jetstream = client.jetstream()
            try:
                kv = await jetstream.key_value(bucket)
            except Exception as exc:
                if not _looks_like_not_found(exc):
                    raise
                try:
                    from nats.js.api import KeyValueConfig  # type: ignore[import-not-found]
                except ImportError as import_exc:
                    raise NatsUnavailableError(
                        "configured nats-py transport has no JetStream KV support"
                    ) from import_exc
                config_kwargs: dict[str, Any] = {"bucket": bucket}
                if ttl_seconds is not None:
                    config_kwargs["ttl"] = float(ttl_seconds)
                kv = await jetstream.create_key_value(
                    config=KeyValueConfig(**config_kwargs)
                )
            return await kv.put(key, payload)

        return self._run(_kv_put())

    def close(self) -> None:
        client = self._client
        if client is not None:

            async def _close() -> None:
                await client.drain()

            try:
                self._run(_close())
            finally:
                self._client = None
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        self._loop = None
        self._thread = None

    def _start_loop(self) -> None:
        if self._loop is not None:
            return
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run_loop,
            name="openclaw-nats-client",
            daemon=True,
        )
        thread.start()
        ready.wait(timeout=2)
        self._loop = loop
        self._thread = thread

    def _run(self, awaitable: Any) -> Any:
        if self._loop is None:
            raise NatsUnavailableError("NATS event loop is not running")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result(timeout=self._operation_timeout_seconds)

    def _require_client(self) -> Any:
        if self._client is None:
            raise NatsUnavailableError("NATS transport is not connected")
        return self._client


def create_nats_transport(url: str) -> NatsPyTransport:
    return NatsPyTransport(url)


def _nats_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"nats", "tls", "ws", "wss"} or not parsed.netloc:
        raise ValueError("OPENCLAW_HOSTD_NATS_URL must be an absolute NATS URL")
    return url


def _looks_like_not_found(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "notfound" in text or "not found" in text or "not_found" in text
