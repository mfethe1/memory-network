"""Thin synchronous NATS lifecycle wrapper for OpenClaw hostd."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Protocol, runtime_checkable


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
