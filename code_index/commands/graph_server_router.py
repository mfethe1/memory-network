"""Lightweight router for the graph HTTP server.

Provides a declarative way to register routes so `graph_server_http.py`
can move away from long if/else chains. Routes are matched in
registration order; the first match wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Route:
    method: str  # GET, POST, etc. or "*" for any
    pattern: str
    handler: Callable[..., Any]
    # Extract path parameters from the pattern:
    # "/api/agent-runs/{run_id}" -> {"run_id": "abc123"}
    _param_re: re.Pattern | None = None
    _param_names: tuple[str, ...] = ()

    def __init__(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        object.__setattr__(self, "method", method.upper())
        object.__setattr__(self, "handler", handler)
        # Convert {name} segments to capture groups
        names: list[str] = []
        parts = []
        for piece in pattern.split("/"):
            if piece.startswith("{") and piece.endswith("}"):
                names.append(piece[1:-1])
                parts.append("([^/]+)")
            else:
                parts.append(re.escape(piece))
        regex = "^" + "\\/".join(parts) + "$"
        object.__setattr__(self, "_param_re", re.compile(regex))
        object.__setattr__(self, "_param_names", tuple(names))

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if self.method != "*" and self.method != method.upper():
            return None
        m = self._param_re.match(path)
        if not m:
            return None
        return {name: m.group(i + 1) for i, name in enumerate(self._param_names)}


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Callable[..., Any]) -> None:
        self._routes.append(Route(method, pattern, handler))

    def get(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add("GET", pattern, handler)

    def post(self, pattern: str, handler: Callable[..., Any]) -> None:
        self.add("POST", pattern, handler)

    def resolve(self, method: str, path: str) -> tuple[Callable[..., Any], dict[str, str]] | None:
        for route in self._routes:
            params = route.match(method, path)
            if params is not None:
                return route.handler, params
        return None
