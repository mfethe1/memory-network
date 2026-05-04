"""Trace propagation and observability helpers for OpenClaw hostd."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
import uuid

from code_index.openclaw_hostd.logging import REDACTED
from code_index.openclaw_hostd.logging import get_logger
from code_index.openclaw_hostd.logging import is_secret_field
from code_index.openclaw_hostd.logging import redact_url


TRACE_ID_FIELD = "trace_id"
SPAN_KIND_ASSIGNMENT = "assignment"
SPAN_KIND_LOCAL_DISPATCH = "local_dispatch"
SPAN_KIND_PROVIDER_RUN = "provider_run"
SPAN_KIND_VERIFICATION = "verification"
SPAN_KIND_MEMORY_SYNC = "memory_sync"
SPAN_KINDS = frozenset(
    {
        SPAN_KIND_ASSIGNMENT,
        SPAN_KIND_LOCAL_DISPATCH,
        SPAN_KIND_PROVIDER_RUN,
        SPAN_KIND_VERIFICATION,
        SPAN_KIND_MEMORY_SYNC,
    }
)
SPAN_ATTRIBUTE_FIELDS = (
    "trace_id",
    "correlation_id",
    "host_id",
    "repo_id",
    "task_id",
    "run_id",
    "event_type",
    "provider",
    "scope",
    "resource_id",
)


def generate_trace_id() -> str:
    """Return an OpenTelemetry-compatible 128-bit trace id."""

    return uuid.uuid4().hex


def extract_trace_id(*payloads: Mapping[str, Any] | None) -> str | None:
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        trace_id = _optional_text(payload.get(TRACE_ID_FIELD))
        if trace_id is not None:
            return trace_id
    return None


def ensure_trace_id(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("telemetry payload must be an object")
    traced = dict(payload)
    traced[TRACE_ID_FIELD] = (
        _optional_text(trace_id)
        or extract_trace_id(payload, source)
        or generate_trace_id()
    )
    return traced


def trace_task_payload(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return ensure_trace_id(payload, source=source, trace_id=trace_id)


def trace_lease_payload(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return ensure_trace_id(payload, source=source, trace_id=trace_id)


def trace_run_payload(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return ensure_trace_id(payload, source=source, trace_id=trace_id)


def trace_event_payload(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return ensure_trace_id(payload, source=source, trace_id=trace_id)


def trace_memory_sync_payload(
    payload: Mapping[str, Any],
    *,
    source: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    return ensure_trace_id(payload, source=source, trace_id=trace_id)


def assignment_span(
    payload: Mapping[str, Any] | None = None,
    *,
    logger: Any | None = None,
    attributes: Mapping[str, Any] | None = None,
    prefer_otel: bool = True,
) -> "_TelemetrySpan":
    return telemetry_span(
        SPAN_KIND_ASSIGNMENT,
        payload,
        logger=logger,
        attributes=attributes,
        prefer_otel=prefer_otel,
    )


def local_dispatch_span(
    payload: Mapping[str, Any] | None = None,
    *,
    logger: Any | None = None,
    attributes: Mapping[str, Any] | None = None,
    prefer_otel: bool = True,
) -> "_TelemetrySpan":
    return telemetry_span(
        SPAN_KIND_LOCAL_DISPATCH,
        payload,
        logger=logger,
        attributes=attributes,
        prefer_otel=prefer_otel,
    )


def provider_run_span(
    payload: Mapping[str, Any] | None = None,
    *,
    logger: Any | None = None,
    attributes: Mapping[str, Any] | None = None,
    prefer_otel: bool = True,
) -> "_TelemetrySpan":
    return telemetry_span(
        SPAN_KIND_PROVIDER_RUN,
        payload,
        logger=logger,
        attributes=attributes,
        prefer_otel=prefer_otel,
    )


def verification_span(
    payload: Mapping[str, Any] | None = None,
    *,
    logger: Any | None = None,
    attributes: Mapping[str, Any] | None = None,
    prefer_otel: bool = True,
) -> "_TelemetrySpan":
    return telemetry_span(
        SPAN_KIND_VERIFICATION,
        payload,
        logger=logger,
        attributes=attributes,
        prefer_otel=prefer_otel,
    )


def memory_sync_span(
    payload: Mapping[str, Any] | None = None,
    *,
    logger: Any | None = None,
    attributes: Mapping[str, Any] | None = None,
    prefer_otel: bool = True,
) -> "_TelemetrySpan":
    return telemetry_span(
        SPAN_KIND_MEMORY_SYNC,
        payload,
        logger=logger,
        attributes=attributes,
        prefer_otel=prefer_otel,
    )


def telemetry_span(
    span_kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    logger: Any | None = None,
    attributes: Mapping[str, Any] | None = None,
    prefer_otel: bool = True,
) -> "_TelemetrySpan":
    kind = str(span_kind or "").strip().lower().replace("-", "_")
    if kind not in SPAN_KINDS:
        raise ValueError(
            "span_kind must be assignment, local_dispatch, provider_run, "
            "verification, or memory_sync"
        )
    return _TelemetrySpan(
        kind,
        payload=payload,
        logger=logger,
        attributes=attributes,
        prefer_otel=prefer_otel,
    )


def redact_payload(value: Any, *, field_name: str | None = None) -> Any:
    if field_name is not None and _is_secret_name(field_name):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): redact_payload(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value, field_name=field_name)
    return value


def configure_local_telemetry_logging(
    log_dir: str | Path,
    *,
    logger_name: str = "code_index.openclaw.telemetry",
    filename: str = "openclaw-telemetry.jsonl",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    log_path = path / filename
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    resolved = str(log_path.resolve())
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == resolved:
            return logger
    handler = RotatingFileHandler(
        resolved,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    handler.setFormatter(_OpenClawJsonFormatter())
    logger.addHandler(handler)
    return logger


class _TelemetrySpan:
    def __init__(
        self,
        span_kind: str,
        *,
        payload: Mapping[str, Any] | None,
        logger: Any | None,
        attributes: Mapping[str, Any] | None,
        prefer_otel: bool,
    ) -> None:
        self.span_kind = span_kind
        self.span_name = f"openclaw.{span_kind}"
        self.payload = dict(payload or {})
        self.extra_attributes = dict(attributes or {})
        self.logger = logger or get_logger("code_index.openclaw.telemetry")
        self.prefer_otel = prefer_otel
        self.trace_id = (
            extract_trace_id(self.payload, self.extra_attributes) or generate_trace_id()
        )
        self.attributes = _span_attributes(
            self.payload,
            self.extra_attributes,
            trace_id=self.trace_id,
        )
        self.safe_payload = redact_payload(self.payload)
        self._otel_context: Any | None = None
        self._otel_span: Any | None = None

    def __enter__(self) -> "_TelemetrySpan":
        if self.prefer_otel:
            self._otel_context = _start_otel_span(self.span_name, self.attributes)
            if self._otel_context is not None:
                self._otel_span = self._otel_context.__enter__()
        self._log("start")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> bool:
        if exc_type is not None and self._otel_span is not None:
            _record_otel_exception(self._otel_span, exc)
        self._log("end", exc_type=exc_type)
        if self._otel_context is not None:
            self._otel_context.__exit__(exc_type, exc, traceback)
        return False

    def _log(
        self,
        phase: str,
        *,
        exc_type: type[BaseException] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "kind": "openclaw.telemetry.span",
            "span_name": self.span_name,
            "span_kind": self.span_kind,
            "phase": phase,
            "trace_id": self.trace_id,
            "attributes": dict(self.attributes),
            "payload": self.safe_payload,
        }
        if exc_type is not None:
            record["exception_type"] = exc_type.__name__
        info = getattr(self.logger, "info", None)
        if info is not None:
            info(f"openclaw.telemetry.span.{phase}", extra={"openclaw": record})


class _OpenClawJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        openclaw = getattr(record, "openclaw", None)
        payload = openclaw if isinstance(openclaw, Mapping) else {"message": record.getMessage()}
        return _json_dumps(redact_payload(payload))


def _span_attributes(
    payload: Mapping[str, Any],
    extra_attributes: Mapping[str, Any],
    *,
    trace_id: str,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {"trace_id": trace_id}
    for field_name in SPAN_ATTRIBUTE_FIELDS:
        value = payload.get(field_name)
        if value is None:
            continue
        safe_value = redact_payload(value, field_name=field_name)
        if _is_attribute_value(safe_value):
            attributes[field_name] = safe_value
    for key, value in extra_attributes.items():
        safe_value = redact_payload(value, field_name=str(key))
        if _is_attribute_value(safe_value):
            attributes[str(key)] = safe_value
    return attributes


def _is_secret_name(name: str) -> bool:
    normalized = str(name or "").lower().replace("-", "_").replace(".", "_")
    compact = normalized.replace("_", "")
    return is_secret_field(normalized) or "apikey" in compact


def _is_attribute_value(value: Any) -> bool:
    return value is None or isinstance(value, bool | int | float | str)


def _start_otel_span(span_name: str, attributes: Mapping[str, Any]) -> Any | None:
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        tracer = trace.get_tracer("code_index.openclaw")
        return tracer.start_as_current_span(span_name, attributes=dict(attributes))
    except Exception:
        return None


def _record_otel_exception(otel_span: Any, exc: BaseException | None) -> None:
    if exc is None:
        return
    record_exception = getattr(otel_span, "record_exception", None)
    if record_exception is not None:
        try:
            record_exception(exc)
        except Exception:
            return


def _redact_text(value: str, *, field_name: str | None) -> str:
    if field_name is not None and "url" in field_name.lower():
        return redact_url(value) or REDACTED
    if value.startswith(("http://", "https://")):
        redacted = redact_url(value)
        if redacted is not None and redacted != value:
            return redacted
    return value


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
