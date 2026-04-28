#!/usr/bin/env bash
# PreToolUse hook: verify code_index write claims before Edit|Write|MultiEdit.
#
# Opt-in for supervised agent writes. Set CODE_INDEX_AGENT_RUN_ID plus either
# CODE_INDEX_AGENT_FENCE for a single touched file or CODE_INDEX_AGENT_FENCES
# as a JSON object mapping repo-relative file paths to fence tokens.
set -euo pipefail

resolve_root() {
  local d
  d="$(pwd)"
  while [ "$d" != "/" ] && [ "$d" != "" ]; do
    if [ -d "$d/.claude" ]; then
      printf '%s' "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

ROOT="$(resolve_root || true)"
if [ -z "${ROOT:-}" ]; then
  exit 0
fi

RUN_ID="${CODE_INDEX_AGENT_RUN_ID:-}"
if [ -z "$RUN_ID" ]; then
  exit 0
fi

PAYLOAD="$(cat || true)"
RAW_PATHS=""
if [ -n "$PAYLOAD" ]; then
  RAW_PATHS="$(printf '%s' "$PAYLOAD" | python -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool_input = data.get("tool_input") or {}
seen = set()

def add(value):
    if isinstance(value, str) and value and value not in seen:
        print(value)
        seen.add(value)

add(tool_input.get("file_path"))
edits = tool_input.get("edits")
if isinstance(edits, list):
    for edit in edits:
        if isinstance(edit, dict):
            add(edit.get("file_path"))
' 2>/dev/null || true)"
fi

if [ -z "$RAW_PATHS" ]; then
  exit 0
fi

should_ignore() {
  local rel="$1"
  case "$rel" in
    .claude/*|.git/*|.code_index/*|__pycache__/*|*/__pycache__/*) return 0 ;;
    .venv/*|venv/*|env/*|node_modules/*|dist/*|build/*|target/*) return 0 ;;
    .tox/*|.pytest_cache/*|.mypy_cache/*|.ruff_cache/*|.idea/*|.vscode/*) return 0 ;;
    *.pyc|*.pyo|*.so|*.dll|*.exe|*.png|*.jpg|*.jpeg|*.gif|*.pdf|*.zip|*.sqlite|*.db) return 0 ;;
  esac
  return 1
}

REL_PATHS=()
SEEN_RELS="|"
while IFS= read -r FILE_PATH; do
  FILE_PATH="${FILE_PATH%$'\r'}"
  [ -n "$FILE_PATH" ] || continue

  case "$FILE_PATH" in
    "$ROOT"*)
      REL="${FILE_PATH#"$ROOT"}"
      REL="${REL#/}"
      ;;
    /*)
      continue
      ;;
    *)
      REL="$FILE_PATH"
      ;;
  esac

  REL="${REL#./}"
  [ -n "$REL" ] || continue
  if should_ignore "$REL"; then
    continue
  fi
  case "$SEEN_RELS" in
    *"|$REL|"*) continue ;;
  esac
  REL_PATHS+=("$REL")
  SEEN_RELS="${SEEN_RELS}${REL}|"
done <<< "$RAW_PATHS"

if [ "${#REL_PATHS[@]}" -eq 0 ]; then
  exit 0
fi

fence_for() {
  local rel="$1"
  if [ -n "${CODE_INDEX_AGENT_FENCES:-}" ]; then
    CODE_INDEX_AGENT_FENCES="$CODE_INDEX_AGENT_FENCES" python - "$rel" <<'PY'
import json
import os
import sys

rel = sys.argv[1]
try:
    fences = json.loads(os.environ.get("CODE_INDEX_AGENT_FENCES", "{}"))
except Exception:
    fences = {}
value = fences.get(rel) or fences.get(rel.replace("\\", "/"))
if value is not None:
    print(value)
PY
    return 0
  fi
  if [ -n "${CODE_INDEX_AGENT_FENCE:-}" ]; then
    printf '%s\n' "$CODE_INDEX_AGENT_FENCE"
  fi
}

cd "$ROOT"
for rel in "${REL_PATHS[@]}"; do
  FENCE="$(fence_for "$rel" | head -n 1)"
  if [ -z "$FENCE" ]; then
    printf 'code_index claim verification failed for %s: missing fence token\n' "$rel" >&2
    exit 2
  fi
  if [ "${CODE_INDEX_DRY_RUN:-}" = "1" ]; then
    printf 'python -m code_index agent verify-claim --run-id %s --file %s --fence %s\n' "$RUN_ID" "$rel" "$FENCE"
    continue
  fi
  if ! OUT="$(python -m code_index agent verify-claim --run-id "$RUN_ID" --file "$rel" --fence "$FENCE" 2>&1)"; then
    printf '%s\n' "$OUT" >&2
    exit 2
  fi
done
