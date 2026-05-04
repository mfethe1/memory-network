"""OpenClaw controller embedding and fleet scheduling surface."""

from __future__ import annotations

__all__ = ["FleetController", "OpenClawControllerApp", "create_app"]


def __getattr__(name: str):
    if name in {"OpenClawControllerApp", "create_app"}:
        from code_index.openclaw_controller import app

        return getattr(app, name)
    if name == "FleetController":
        from code_index.openclaw_controller.scheduler import FleetController

        return FleetController
    raise AttributeError(name)
