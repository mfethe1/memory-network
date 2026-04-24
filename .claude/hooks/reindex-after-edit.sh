#!/usr/bin/env bash
# PostToolUse hook: reindex files touched by Edit|Write|MultiEdit through code_index.
#
# Silent on success. Emits a single actionable line on failure so Claude can
# surface it. Ignores generated/irrelevant paths so the hook does not churn.
#
# Reads JSON on stdin from Claude Code; extracts file paths from Edit/Write and
# MultiEdit tool_input shapes.
set -euo pipefail

# Locate repo root. The hook is called from the working directory of the
# Claude session; the repo root is the nearest ancestor containing .claude/.
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

# Parse the tool JSON. We care about:
# - tool_input.file_path (Edit/Write and current MultiEdit shape)
# - tool_input.edits[].file_path (multi-file MultiEdit shape)
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
  # Ignore list — keep aligned with code_index/ignore.py ALWAYS_SKIP plus
  # common generated artifacts and the index DB itself.
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
  # Python's text-mode print() on Windows emits \r\n; bash `read -r` leaves
  # the \r in the variable. Strip it so downstream matching and printing
  # don't insert stray carriage returns.
  FILE_PATH="${FILE_PATH%$'\r'}"
  [ -n "$FILE_PATH" ] || continue

  # Normalize to a repo-relative path if possible.
  case "$FILE_PATH" in
    "$ROOT"*)
      REL="${FILE_PATH#"$ROOT"}"
      REL="${REL#/}"
      ;;
    /*)
      # Absolute path outside repo — skip.
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

# The index must exist. If not, skip silently — `init` is a user action.
if [ "${CODE_INDEX_DRY_RUN:-}" = "1" ]; then
  printf 'python -m code_index update --files'
  for rel in "${REL_PATHS[@]}"; do
    printf ' %s' "$rel"
  done
  printf ' --json\n'
  exit 0
fi

if [ ! -f "$ROOT/.code_index/index.db" ]; then
  exit 0
fi

# Run update --files. Keep STDOUT silent on success; relay only errors.
cd "$ROOT"
if ! OUT="$(python -m code_index update --files "${REL_PATHS[@]}" --json 2>&1)"; then
  REL_JOINED="$(IFS=,; printf '%s' "${REL_PATHS[*]}")"
  OUT_TAIL="$(printf '%s' "$OUT" | tail -n 20)"
  printf 'code_index reindex failed for %s\n%s\n' "$REL_JOINED" "$OUT_TAIL" >&2
  exit 1
fi
exit 0
