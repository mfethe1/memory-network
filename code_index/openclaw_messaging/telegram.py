"""Telegram adapter stub for OpenClaw Messaging Service."""

from __future__ import annotations

import hmac
from typing import Any, Mapping

from code_index.openclaw_messaging.adapters import AdapterCapabilities
from code_index.openclaw_messaging.adapters import MessagingAdapter
from code_index.openclaw_messaging.models import MessageDraft
from code_index.openclaw_messaging.store import MessagingStore
from code_index.openclaw_messaging.store import adapter_idempotency_key


HOST_ALIAS_MENTIONS = frozenset({"rosie", "lenny"})


class TelegramAdapter(MessagingAdapter):
    adapter_id = "telegram"
    adapter_type = "telegram"
    display_name = "Telegram"
    command_promotion_enabled = False

    def normalize_inbound(self, payload: Mapping[str, Any], **kwargs: Any) -> MessageDraft:
        room_id = str(kwargs.get("room_id") or "").strip()
        if not room_id:
            raise ValueError("room_id is required for Telegram normalization")
        message = _telegram_message(payload)
        chat = _object(message.get("chat"))
        sender = _object(message.get("from"))
        platform_room_id = str(chat.get("id") or "")
        platform_user_id = str(sender.get("id") or "")
        platform_message_id = str(message.get("message_id") or "")
        platform_thread_id = _thread_id(message)
        platform_event_id = str(payload.get("update_id") or platform_message_id)
        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text:
            text = "[unsupported Telegram message]"
        message_type = "command" if text.startswith("/") else "chat"
        target_scope = _command_target_scope(text) if message_type == "command" else {}
        metadata = (
            _command_metadata(text)
            if message_type == "command"
            else _message_metadata(text)
        )
        platform_ref = {
            "platform_room_id": platform_room_id,
            "platform_thread_id": platform_thread_id,
            "platform_event_id": platform_event_id,
            "platform_message_id": platform_message_id,
            "platform_user_id": platform_user_id,
            "chat_type": chat.get("type"),
            "chat_title": chat.get("title"),
            "username": sender.get("username"),
        }
        platform_ref = {key: value for key, value in platform_ref.items() if value}
        return MessageDraft(
            room_id=room_id,
            sender_kind="human",
            sender_id=str(kwargs.get("sender_id") or f"telegram:{platform_user_id}"),
            body=text,
            message_type=message_type,
            target_scope=target_scope,
            adapter_id=self.adapter_id,
            platform_ref=platform_ref,
            idempotency_key=adapter_idempotency_key(self.adapter_id, platform_ref),
            command_type=_command_type(text) if message_type == "command" else None,
            platform_user_id=platform_user_id,
            metadata=metadata,
        )

    def render_outbound(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        message = _object(delivery.get("message"))
        delivery_record = _object(delivery.get("delivery"))
        metadata = _object(delivery_record.get("metadata"))
        chat_id = str(
            metadata.get("platform_room_id")
            or message.get("platform_ref", {}).get("platform_room_id")
            or ""
        )
        severity = str(_object(message.get("metadata")).get("severity") or "").strip()
        body = str(message.get("body") or "").strip()
        text = f"[{severity}] {body}" if severity else body
        return {
            "method": "sendMessage",
            "payload": {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        }

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            threads=True,
            edits=False,
            reactions=False,
            attachments=True,
            rich_text=False,
            outbound_notifications=True,
            command_promotion=False,
        )


def handle_telegram_webhook(
    store: MessagingStore,
    update: Mapping[str, Any],
    *,
    adapter_id: str = "telegram",
    secret_token: str | None,
    provided_secret_token: str | None,
) -> dict[str, Any]:
    _require_secret_token(
        expected=secret_token,
        provided=provided_secret_token,
    )
    adapter = TelegramAdapter()
    if adapter_id != adapter.adapter_id:
        raise ValueError("only the built-in telegram adapter is supported")
    message = _telegram_message(update)
    chat = _object(message.get("chat"))
    platform_room_id = str(chat.get("id") or "")
    platform_thread_id = _thread_id(message)
    mapping = store.find_platform_room_mapping(
        adapter_id=adapter_id,
        platform_room_id=platform_room_id,
        platform_thread_id=platform_thread_id,
    )
    if mapping is None:
        raise KeyError(f"no OpenClaw room mapping for Telegram chat {platform_room_id}")
    draft = adapter.normalize_inbound(update, room_id=mapping["room_id"])
    return store.ingest_adapter_message(
        adapter_id=draft.adapter_id or adapter_id,
        platform_user_id=draft.platform_user_id or "",
        room_id=draft.room_id,
        body=draft.body,
        message_type=draft.message_type,
        command_type=draft.command_type,
        platform_ref=draft.platform_ref,
        target_scope=draft.target_scope or None,
        idempotency_key=draft.idempotency_key,
        metadata=draft.metadata,
        parent_message_id=draft.parent_message_id,
        trace_id=draft.trace_id,
        correlation_id=draft.correlation_id,
    )


def _require_secret_token(*, expected: str | None, provided: str | None) -> None:
    expected_text = str(expected or "").strip()
    provided_text = str(provided or "").strip()
    if not expected_text:
        raise PermissionError("Telegram webhook secret token is not configured")
    if not provided_text:
        raise PermissionError("Telegram webhook secret token is required")
    if not hmac.compare_digest(provided_text, expected_text):
        raise PermissionError("Telegram webhook secret token is invalid")


def _telegram_message(update: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        value = update.get(key)
        if isinstance(value, Mapping):
            return value
    raise ValueError("Telegram update does not contain a message")


def _thread_id(message: Mapping[str, Any]) -> str | None:
    thread_id = message.get("message_thread_id")
    if thread_id:
        return str(thread_id)
    reply = message.get("reply_to_message")
    if isinstance(reply, Mapping) and reply.get("message_id"):
        return str(reply["message_id"])
    return None


def _command_type(text: str) -> str:
    command = _command_name(text)
    if command in {"/assign", "/task"}:
        return "assign_task"
    if command == "/cancel":
        return "cancel"
    if command == "/retry":
        return "retry"
    if command == "/checkpoint":
        return "checkpoint"
    return "run_message"


def _command_target_scope(text: str) -> dict[str, str]:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2:
        return {}
    if _command_name(text) not in {"/assign", "/task"}:
        return {}
    task_id = str(parts[1] or "").strip()
    return {"kind": "task", "task_id": task_id} if task_id else {}


def _command_metadata(text: str) -> dict[str, dict[str, str]]:
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 3:
        return {}
    if _command_name(text) not in {"/assign", "/task"}:
        return {}
    task_message = str(parts[2] or "").strip()
    host_alias, task_message = _consume_host_alias(task_message)
    metadata: dict[str, dict[str, str]] = {}
    if task_message:
        metadata["assignment"] = {"message": task_message}
    if host_alias:
        metadata["routing"] = {"host_alias": host_alias}
    return metadata


def _message_metadata(text: str) -> dict[str, dict[str, str]]:
    host_alias, remaining = _consume_host_alias(text)
    metadata: dict[str, dict[str, str]] = {}
    if host_alias:
        metadata["routing"] = {"host_alias": host_alias}
    if _is_claimable_text(text, remaining=remaining, has_host_alias=bool(host_alias)):
        metadata["claimable_work"] = {"status": "pending", "source": "telegram"}
    return metadata


def _command_name(text: str) -> str:
    command = text.strip().split(maxsplit=1)[0].lower()
    return command.split("@", 1)[0]


def _consume_host_alias(text: str) -> tuple[str | None, str]:
    body = str(text or "").strip()
    if not body.startswith("@"):
        return None, body
    parts = body.split(maxsplit=1)
    mention = parts[0].removeprefix("@").rstrip(":").lower()
    if mention not in HOST_ALIAS_MENTIONS:
        return None, body
    remaining = parts[1].strip() if len(parts) > 1 else ""
    return mention, remaining


def _is_claimable_text(
    text: str,
    *,
    remaining: str,
    has_host_alias: bool,
) -> bool:
    body = str(text or "").strip()
    if not body or body == "[unsupported Telegram message]" or body.startswith("/"):
        return False
    if body.startswith("@") and not has_host_alias:
        return False
    return bool(remaining if has_host_alias else body)


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
