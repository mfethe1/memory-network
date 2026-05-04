"""Minimal OpenClaw controller embedding surface."""

from __future__ import annotations

__all__ = ["OpenClawControllerApp", "create_app"]


def __getattr__(name: str):
    if name in __all__:
        from code_index.openclaw_controller import app

        return getattr(app, name)
    raise AttributeError(name)
