"""Client-side JavaScript assembly for the standalone code graph HTML view."""

from __future__ import annotations

from code_index.commands.graph_client import GRAPH_SCRIPT_PARTS


GRAPH_SCRIPT = "".join(GRAPH_SCRIPT_PARTS)

__all__ = ["GRAPH_SCRIPT"]
