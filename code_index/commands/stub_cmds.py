"""Reserved CLI commands with clean not-yet-implemented responses.

Currently empty — every previously stubbed command has shipped a real
implementation:

- `watch`          → `code_index.commands.watch_cmd`
- `impact`         → `code_index.commands.impact_cmd`
- `tests`          → `code_index.commands.tests_cmd`
- `install-hooks`  → `code_index.commands.install_hooks_cmd`
- `mcp-serve`      → `code_index.commands.mcp_serve_cmd`

This module is retained so future reserved commands have a place to live,
and so existing imports (e.g. in tests) do not break.
"""
