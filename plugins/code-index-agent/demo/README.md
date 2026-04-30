# Code Index Agent Demo

This demo validates the plugin loop without requiring a real provider.

1. Install repo-local config:

```bash
python plugins/code-index-agent/scripts/install_plugin.py --root . --provider codex --json
```

2. Start the graph:

```bash
python .code_index/start-code-index-agent.ps1
```

On non-Windows shells:

```bash
sh .code_index/start-code-index-agent.sh
```

3. Open `http://127.0.0.1:8767/repo-graph.html`.

4. Select a file, open the Notes tab, submit a task, then click `View` in the
agent runs pane to inspect the transcript.

Provider output can emit structured events:

```text
READ code_index/commands/graph_script.py inspected graph navigation
EDIT code_index/commands/graph_script.py added transcript view
STATUS completed finished graph navigation work
```

JSON lines also work:

```json
{"event_type":"decision","message":"Use context packet from task JSON","payload":{"status":"accepted"}}
```
