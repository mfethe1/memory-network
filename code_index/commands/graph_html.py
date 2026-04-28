"""Standalone HTML renderer for `code_index graph`."""

from __future__ import annotations

import json
from typing import Any

from code_index.commands.graph_template import HTML_TEMPLATE


def _json_for_html(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def render_html(payload: dict[str, Any]) -> str:
    graph_json = _json_for_html(payload)
    return HTML_TEMPLATE.replace("__GRAPH_JSON__", graph_json)
