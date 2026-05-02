# Folder Switcher — Graph UI Design

## Summary

Add a modular folder/project switcher to the graph web UI header. Users can
browse the local filesystem and switch the active indexed project without
leaving the browser. Known (workspace-registered) projects appear at the top;
unknown directories can be initialized on the spot.

---

## Architecture

Three modular pieces:

| Layer | Change |
|-------|--------|
| Backend routes | Two new routes in `graph_server_routes.py` |
| Server state | `active_root` tracking in `graph_server_state.py` |
| Frontend | Self-contained overlay injected into `graph_mobile.py` / `graph_html.py` |

---

## Backend Routes

### `GET /api/dirs?path=<dir>`

Returns directory contents for the given path (subdirectories only).
Defaults to the parent of the current `active_root` when `path` is omitted.

```json
{
  "path": "/Users/mfeth/Projects",
  "parent": "/Users/mfeth",
  "entries": [
    {"name": "memory-claude", "path": "/Users/mfeth/Projects/memory-claude", "indexed": true},
    {"name": "buildbid",      "path": "/Users/mfeth/Projects/buildbid",      "indexed": true},
    {"name": "other-dir",     "path": "/Users/mfeth/Projects/other-dir",     "indexed": false}
  ],
  "known_projects": [
    {"name": "memory-claude", "path": "/Users/mfeth/Projects/memory-claude"},
    {"name": "buildbid",      "path": "/Users/mfeth/Projects/buildbid"}
  ]
}
```

- `indexed: true` → directory contains `.code_index/`
- `known_projects` is always the full workspace list regardless of browse path
- Unreadable directories → `{"error": "permission denied"}`

### `POST /api/switch-project`

Body: `{"path": "/some/dir"}`

Responses:

```json
{"ok": true, "path": "..."}         // switched; frontend reloads graph
{"needs_init": true, "path": "..."} // unindexed — show confirm dialog
{"error": "not a directory"}        // invalid path
```

On `ok`, server updates `active_root` in state.

### `GET /api/init-status?path=<dir>`

Polled every 2 s during the init flow.

```json
{"status": "running", "elapsed": 14}
{"status": "done",    "path": "..."}
{"status": "error",   "message": "..."}
```

---

## Server State

`graph_server_state.py` gains:

- `active_root: Path` — currently served project root (mutable, starts from CLI arg)
- `known_projects() -> list[dict]` — reads `~/.code_index/workspaces.json`

---

## Frontend Overlay

### Header

Current static project title becomes a clickable chip:

```
[📁 memory-claude ▾]
```

### Overlay Layout

```
┌─────────────────────────────────────┐
│  Switch Project                   ✕ │
├─────────────────────────────────────┤
│  Known Projects                      │
│  ● memory-claude  (current)          │
│    buildbid                          │
│    rareagent-work                    │
├─────────────────────────────────────┤
│  Browse                              │
│  [/Users/mfeth/Projects_________] ▶ │
│                                      │
│  📁 memory-claude  ✓ indexed         │
│  📁 buildbid       ✓ indexed         │
│  📁 some-other-dir                   │
│                                      │
│  Breadcrumbs: Projects > ...         │
└─────────────────────────────────────┘
```

### Interactions

| Action | Behavior |
|--------|----------|
| Click known project | `POST /api/switch-project` directly |
| Click directory entry | `GET /api/dirs?path=...` (navigate into it) |
| Edit path bar + Enter | Jump to typed path |
| Click unindexed folder | Inline confirm banner → Yes → init spinner + poll → switch |
| Successful switch | Overlay closes, graph reloads |

### Delivery

One `<template>` block + one `<script>` section added via a shared Python
helper — no new frontend files. Both `graph_mobile.py` and `graph_html.py`
call the helper.

---

## Error Handling & Edge Cases

- **Init duration** — spinner shows elapsed time; typical range 10–60 s
- **Init failure** — inline error message with retry link
- **Permission denied** — directory shown greyed-out, not clickable
- **Path normalization** — server normalizes OS paths; frontend always receives `/`-separated strings
- **In-flight graph load** — if `active_root` changes mid-load, discard response and re-fetch
- **No-op switch** — switching to the already-active project closes overlay, no API call
- **Workspace sync** — newly initialized folders are added to `workspaces.json` automatically

---

## Out of Scope (v1)

- Removing a project from the workspace via UI (use `code_index workspace remove`)
- Search/filter within the directory browser
- Remote/network paths
