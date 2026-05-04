"""Small stdlib route dispatcher for OpenClaw Messaging Service APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from code_index.openclaw_messaging.models import MessagingError
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.telegram import handle_telegram_webhook


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class Principal:
    principal_id: str
    scopes: frozenset[str]


class MessagingRouter:
    def __init__(
        self,
        store: MessagingStore,
        *,
        telegram_secret_token: str | None = None,
    ) -> None:
        self.store = store
        self.telegram_secret_token = _string_or_none(telegram_secret_token)

    def handle(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> ApiResponse:
        method = method.upper()
        parts = [unquote(part) for part in urlsplit(path).path.strip("/").split("/") if part]
        payload = dict(body or {})
        request_headers = {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}
        try:
            if method == "GET" and parts == ["rooms"]:
                return ApiResponse(200, {"rooms": self.store.list_rooms()})
            if method == "GET" and len(parts) == 3 and parts[0] == "rooms" and parts[2] == "messages":
                return ApiResponse(200, {"messages": self.store.list_messages(parts[1])})
            if method == "POST" and parts == ["messages"]:
                message_type = str(payload.get("message_type") or "chat").strip().lower()
                if message_type == "command" and not _principal_can_sign_command(
                    principal,
                    payload,
                ):
                    return ApiResponse(403, {"error": "command signing requires command:write principal"})
                result = self.store.create_message(
                    room_id=str(payload.get("room_id") or ""),
                    sender_kind=str(payload.get("sender_kind") or "human"),
                    sender_id=str(payload.get("sender_id") or ""),
                    body=str(payload.get("body") or ""),
                    target_scope=_object_or_none(payload.get("target_scope")),
                    message_type=message_type,
                    context_handles=_list_or_none(payload.get("context_handles")),
                    adapter_id=_string_or_none(payload.get("adapter_id")),
                    platform_ref=_object_or_none(payload.get("platform_ref")),
                    trace_id=_string_or_none(payload.get("trace_id")),
                    correlation_id=_string_or_none(payload.get("correlation_id")),
                    parent_message_id=_string_or_none(payload.get("parent_message_id")),
                    idempotency_key=_string_or_none(payload.get("idempotency_key")),
                    metadata=_object_or_none(payload.get("metadata")),
                    recipients=_list_or_none(payload.get("recipients")),
                    command_type=_string_or_none(payload.get("command_type")),
                )
                return ApiResponse(201 if result["created"] else 200, result)
            if method == "POST" and parts == ["messages", "preview"]:
                preview = self.store.preview_target(_object(payload.get("target_scope")))
                return ApiResponse(200, {"preview": preview})
            if method == "POST" and len(parts) == 3 and parts[0] == "messages" and parts[2] == "ack":
                delivery = self.store.ack_delivery(
                    message_id=parts[1],
                    delivery_id=_string_or_none(payload.get("delivery_id")),
                    delivery_key=_string_or_none(payload.get("delivery_key")),
                    recipient_kind=_string_or_none(payload.get("recipient_kind")),
                    recipient_id=_string_or_none(payload.get("recipient_id")),
                    status=str(payload.get("status") or "acked"),
                    error=_string_or_none(payload.get("error")),
                )
                return ApiResponse(200, {"delivery": delivery})
            if method == "GET" and parts == ["messages", "stream"]:
                return ApiResponse(200, {"events": self.store.list_message_events()})
            if method == "POST" and parts == ["adapters", "telegram", "webhook"]:
                result = handle_telegram_webhook(
                    self.store,
                    payload,
                    secret_token=self.telegram_secret_token,
                    provided_secret_token=request_headers.get(
                        "x-telegram-bot-api-secret-token"
                    ),
                )
                return ApiResponse(201 if result["created"] else 200, result)
        except KeyError as exc:
            return ApiResponse(404, {"error": str(exc)})
        except PermissionError as exc:
            return ApiResponse(403, {"error": str(exc)})
        except (MessagingError, ValueError) as exc:
            return ApiResponse(400, {"error": str(exc)})
        return ApiResponse(404, {"error": "route not found"})


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    raise MessagingError("expected object")


def _object_or_none(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _object(value)


def _list_or_none(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return value
    raise MessagingError("expected list")


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _principal_can_sign_command(
    principal: Principal | None,
    payload: Mapping[str, Any],
) -> bool:
    if principal is None:
        return False
    if "command:write" not in principal.scopes:
        return False
    sender_id = _string_or_none(payload.get("sender_id"))
    return not principal.principal_id or not sender_id or principal.principal_id == sender_id
