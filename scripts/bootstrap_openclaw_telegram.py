#!/usr/bin/env python3
"""Bootstrap Telegram control-plane state in the OpenClaw messaging store."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code_index.openclaw_controller.service_config import OPENCLAW_SIGNING_SECRET_ENV
from code_index.openclaw_messaging.adapter_registry import AdapterRegistry
from code_index.openclaw_messaging.store import MessagingStore


DEFAULT_FLEET_ROOM_ID = "room-openclaw-telegram-operators"
DEFAULT_FLEET_ROOM_NAME = "OpenClaw Telegram Operators"
DEFAULT_OPERATOR_SCOPES = ("message:write", "command:write")
DEFAULT_TRUSTED_HOST_ALIASES = ("lenny", "rosie")


def bootstrap_telegram(
    store: MessagingStore,
    *,
    telegram_chat_id: str,
    telegram_user_id: str,
    openclaw_identity_id: str,
    telegram_thread_id: str | None = None,
    room_id: str = DEFAULT_FLEET_ROOM_ID,
    room_display_name: str = DEFAULT_FLEET_ROOM_NAME,
    operator_display_name: str | None = None,
    trusted_host_aliases: tuple[str, ...] = DEFAULT_TRUSTED_HOST_ALIASES,
    room_metadata: dict[str, Any] | None = None,
    mapping_metadata: dict[str, Any] | None = None,
    identity_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or reuse the Telegram operator routing state for OpenClaw."""

    chat_id = _required_text(telegram_chat_id, field_name="telegram_chat_id")
    user_id = _required_text(telegram_user_id, field_name="telegram_user_id")
    identity_id = _required_text(
        openclaw_identity_id,
        field_name="openclaw_identity_id",
    )
    thread_id = _optional_text(telegram_thread_id)
    bootstrap_room_id = _required_text(room_id, field_name="room_id")
    room_name = _required_text(room_display_name, field_name="room_display_name")
    aliases = _normalize_aliases(trusted_host_aliases)
    if not aliases:
        raise ValueError("trusted_host_aliases must not be empty")

    AdapterRegistry(store).register_defaults()
    store.set_adapter_command_promotion("telegram", enabled=True)

    room, room_created = _resolve_fleet_room(
        store,
        telegram_chat_id=chat_id,
        telegram_thread_id=thread_id,
        room_id=bootstrap_room_id,
        room_display_name=room_name,
        room_metadata=room_metadata,
    )
    mapping = store.map_platform_room(
        adapter_id="telegram",
        platform_room_id=chat_id,
        platform_thread_id=thread_id,
        room_id=room["room_id"],
        route_policy=_route_policy(aliases),
        metadata=_mapping_metadata(mapping_metadata),
    )
    identity = store.link_external_identity(
        adapter_id="telegram",
        platform_user_id=user_id,
        openclaw_identity_id=identity_id,
        scopes=DEFAULT_OPERATOR_SCOPES,
        display_name=_optional_text(operator_display_name) or identity_id,
        metadata=_identity_metadata(identity_metadata),
    )
    adapter = store.get_adapter("telegram")
    if adapter is None:
        raise RuntimeError("telegram adapter registration was not persisted")
    return {
        "adapter": adapter,
        "room": room,
        "room_created": room_created,
        "mapping": mapping,
        "identity": identity,
        "trusted_host_aliases": list(aliases),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap Telegram room routing and trusted operator links.",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to the OpenClaw messaging SQLite database.",
    )
    parser.add_argument(
        "--signing-secret",
        default=os.environ.get(OPENCLAW_SIGNING_SECRET_ENV),
        help=(
            "Command signing secret for the messaging store. "
            f"May also be supplied as {OPENCLAW_SIGNING_SECRET_ENV}."
        ),
    )
    parser.add_argument(
        "--telegram-chat-id",
        required=True,
        help="Telegram chat ID to map to the fleet operator room.",
    )
    parser.add_argument(
        "--telegram-thread-id",
        help="Optional Telegram thread ID within the mapped chat.",
    )
    parser.add_argument(
        "--telegram-user-id",
        required=True,
        help="Trusted Telegram platform user ID to link.",
    )
    parser.add_argument(
        "--openclaw-identity-id",
        required=True,
        help="Verified OpenClaw identity ID for the trusted Telegram operator.",
    )
    parser.add_argument(
        "--operator-display-name",
        help="Optional display name for the linked trusted operator.",
    )
    parser.add_argument(
        "--room-id",
        default=DEFAULT_FLEET_ROOM_ID,
        help="Stable OpenClaw room ID for the Telegram operator fleet room.",
    )
    parser.add_argument(
        "--room-display-name",
        default=DEFAULT_FLEET_ROOM_NAME,
        help="Display name for the fleet room.",
    )
    parser.add_argument(
        "--trusted-host-alias",
        action="append",
        dest="trusted_host_aliases",
        help=(
            "Host alias allowed by the Telegram route policy. "
            "Repeat to allow multiple aliases."
        ),
    )
    parser.add_argument(
        "--room-metadata-json",
        help="Optional JSON object merged into the created room metadata.",
    )
    parser.add_argument(
        "--mapping-metadata-json",
        help="Optional JSON object merged into the platform-room mapping metadata.",
    )
    parser.add_argument(
        "--identity-metadata-json",
        help="Optional JSON object merged into the external identity metadata.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    signing_secret = str(args.signing_secret or "").strip()
    if not signing_secret:
        raise SystemExit(
            f"--signing-secret or {OPENCLAW_SIGNING_SECRET_ENV} is required"
        )

    store = MessagingStore(args.db, signing_secret=signing_secret)
    try:
        result = bootstrap_telegram(
            store,
            telegram_chat_id=args.telegram_chat_id,
            telegram_thread_id=args.telegram_thread_id,
            telegram_user_id=args.telegram_user_id,
            openclaw_identity_id=args.openclaw_identity_id,
            operator_display_name=args.operator_display_name,
            room_id=args.room_id,
            room_display_name=args.room_display_name,
            trusted_host_aliases=tuple(
                args.trusted_host_aliases or DEFAULT_TRUSTED_HOST_ALIASES
            ),
            room_metadata=_parse_json_object(
                args.room_metadata_json,
                field_name="room_metadata_json",
            ),
            mapping_metadata=_parse_json_object(
                args.mapping_metadata_json,
                field_name="mapping_metadata_json",
            ),
            identity_metadata=_parse_json_object(
                args.identity_metadata_json,
                field_name="identity_metadata_json",
            ),
        )
    finally:
        store.close()

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _resolve_fleet_room(
    store: MessagingStore,
    *,
    telegram_chat_id: str,
    telegram_thread_id: str | None,
    room_id: str,
    room_display_name: str,
    room_metadata: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    existing_mapping = store.find_platform_room_mapping(
        adapter_id="telegram",
        platform_room_id=telegram_chat_id,
        platform_thread_id=telegram_thread_id,
    )
    if existing_mapping is not None:
        room = store.get_room(existing_mapping["room_id"])
        _require_fleet_room(room)
        return room, False
    try:
        room = store.get_room(room_id)
    except KeyError:
        room = store.create_room(
            room_kind="fleet",
            display_name=room_display_name,
            room_id=room_id,
            metadata=_room_metadata(room_metadata),
        )
        return room, True
    _require_fleet_room(room)
    return room, False


def _route_policy(trusted_host_aliases: tuple[str, ...]) -> dict[str, Any]:
    return {
        "command_promotion": {
            "enabled": True,
            "allowed_command_types": ["assign_task"],
            "allowed_target_kinds": ["task"],
        },
        "routing": {
            "allowed_host_aliases": list(trusted_host_aliases),
        },
    }


def _room_metadata(extra: dict[str, Any] | None) -> dict[str, Any]:
    metadata = {
        "bootstrap": {
            "utility": "openclaw_telegram",
            "version": 1,
        },
        "operator_group": {
            "adapter_id": "telegram",
            "scope": "fleet",
        },
    }
    if extra:
        metadata.update(extra)
    return metadata


def _mapping_metadata(extra: dict[str, Any] | None) -> dict[str, Any]:
    metadata = {
        "bootstrap": {
            "utility": "openclaw_telegram",
            "version": 1,
        },
        "room_role": "telegram_operator_group",
    }
    if extra:
        metadata.update(extra)
    return metadata


def _identity_metadata(extra: dict[str, Any] | None) -> dict[str, Any]:
    metadata = {
        "bootstrap": {
            "utility": "openclaw_telegram",
            "version": 1,
        },
        "trust": {
            "verified_by": "bootstrap_openclaw_telegram.py",
        },
    }
    if extra:
        metadata.update(extra)
    return metadata


def _require_fleet_room(room: dict[str, Any]) -> None:
    if room["room_kind"] != "fleet":
        raise ValueError(
            f"bootstrap room must be a fleet room, got {room['room_kind']!r}"
        )


def _normalize_aliases(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = _optional_text(value)
        if not alias:
            continue
        canonical = alias.lower()
        if canonical in seen:
            continue
        seen.add(canonical)
        aliases.append(canonical)
    return tuple(aliases)


def _parse_json_object(value: str | None, *, field_name: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"{field_name} must decode to a JSON object")
    return dict(parsed)


def _required_text(value: Any, *, field_name: str) -> str:
    text = _optional_text(value)
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text else None


if __name__ == "__main__":
    raise SystemExit(main())
