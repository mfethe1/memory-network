"""Composable JavaScript fragments for the standalone graph client."""

from __future__ import annotations

from code_index.commands.graph_client.state import GRAPH_SCRIPT_STATE
from code_index.commands.graph_client.layout import GRAPH_SCRIPT_LAYOUT
from code_index.commands.graph_client.render_graph import GRAPH_SCRIPT_RENDER_GRAPH
from code_index.commands.graph_client.navigator import GRAPH_SCRIPT_NAVIGATOR
from code_index.commands.graph_client.inspector import GRAPH_SCRIPT_INSPECTOR
from code_index.commands.graph_client.activity import GRAPH_SCRIPT_ACTIVITY
from code_index.commands.graph_client.controls import GRAPH_SCRIPT_CONTROLS

GRAPH_SCRIPT_PARTS = (
    GRAPH_SCRIPT_STATE,
    GRAPH_SCRIPT_LAYOUT,
    GRAPH_SCRIPT_RENDER_GRAPH,
    GRAPH_SCRIPT_NAVIGATOR,
    GRAPH_SCRIPT_INSPECTOR,
    GRAPH_SCRIPT_ACTIVITY,
    GRAPH_SCRIPT_CONTROLS,
)

__all__ = ["GRAPH_SCRIPT_PARTS"]
