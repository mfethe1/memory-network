"""Minimal OpenClaw controller app wrapper for embedded messaging routes."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from code_index.openclaw_messaging.adapter_registry import AdapterRegistry
from code_index.openclaw_messaging.routes import ApiResponse
from code_index.openclaw_messaging.routes import MessagingRouter
from code_index.openclaw_messaging.store import MessagingStore


@dataclass
class OpenClawControllerApp:
    store: MessagingStore
    router: MessagingRouter

    def handle_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> ApiResponse:
        return self.router.handle(method, path, body, headers=headers)

    def close(self) -> None:
        self.store.close()


def create_app(
    db_path: str | Path,
    *,
    signing_secret: str,
    telegram_secret_token: str | None = None,
    register_default_adapters: bool = True,
) -> OpenClawControllerApp:
    store = MessagingStore(db_path, signing_secret=signing_secret)
    if register_default_adapters:
        AdapterRegistry(store).register_defaults()
    return OpenClawControllerApp(
        store=store,
        router=MessagingRouter(store, telegram_secret_token=telegram_secret_token),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OpenClaw controller embedded messaging route dispatcher."
    )
    parser.add_argument("--db", default=":memory:", help="SQLite database path")
    parser.add_argument("--method", default="GET", help="Request method")
    parser.add_argument("--path", default="/rooms", help="Request path")
    parser.add_argument(
        "--body-json",
        default="{}",
        help="Request body JSON object for dispatcher smoke checks",
    )
    parser.add_argument(
        "--signing-secret",
        default=os.environ.get("OPENCLAW_CONTROLLER_SIGNING_SECRET"),
        help="Command reference signing secret. May also use OPENCLAW_CONTROLLER_SIGNING_SECRET.",
    )
    parser.add_argument(
        "--telegram-secret-token",
        default=os.environ.get("OPENCLAW_TELEGRAM_SECRET_TOKEN"),
        help="Telegram webhook secret token. May also use OPENCLAW_TELEGRAM_SECRET_TOKEN.",
    )
    args = parser.parse_args(argv)
    try:
        body = json.loads(args.body_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--body-json must be valid JSON: {exc}")
    if not isinstance(body, dict):
        parser.error("--body-json must be a JSON object")
    if not args.signing_secret:
        parser.error("--signing-secret or OPENCLAW_CONTROLLER_SIGNING_SECRET is required")

    app = create_app(
        args.db,
        signing_secret=args.signing_secret,
        telegram_secret_token=args.telegram_secret_token,
    )
    try:
        response = app.handle_request(args.method, args.path, body)
        print(
            json.dumps(
                {"status_code": response.status_code, "body": response.body},
                sort_keys=True,
            )
        )
        return 0 if response.status_code < 500 else 1
    finally:
        app.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
