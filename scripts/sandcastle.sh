#!/usr/bin/env bash
# Cross-platform Sandcastle launcher for macOS / Linux / WSL.
#
# Usage:
#   ./scripts/sandcastle.sh [mode] [provider] [agent] [model]
#
# Examples:
#   ./scripts/sandcastle.sh plan
#   ./scripts/sandcastle.sh implement docker claude claude-opus-4-6
#   ./scripts/sandcastle.sh review auto kimi

set -euo pipefail

MODE="${1:-default}"
PROVIDER="${2:-auto}"
AGENT="${3:-auto}"
MODEL="${4:-}"

# ─── Detect container runtime ────────────────────────────────────────────────
if [[ "$PROVIDER" == "auto" ]]; then
  if command -v docker &>/dev/null; then
    PROVIDER="docker"
  elif command -v podman &>/dev/null; then
    PROVIDER="podman"
  else
    echo "❌ No container runtime found. Install Docker or Podman."
    exit 1
  fi
fi

# ─── Detect agent ────────────────────────────────────────────────────────────
if [[ "$AGENT" == "auto" ]]; then
  if command -v claude &>/dev/null; then
    AGENT="claude"
  elif command -v kimi &>/dev/null; then
    AGENT="kimi"
  elif command -v codex &>/dev/null; then
    AGENT="codex"
  else
    echo "❌ No agent CLI found. Install Claude Code, Kimi, or Codex."
    exit 1
  fi
fi

# ─── WSL guidance ────────────────────────────────────────────────────────────
if grep -qiE "microsoft|wsl" /proc/sys/kernel/osrelease 2>/dev/null; then
  echo "ℹ WSL detected. For best performance, keep this repo inside the WSL filesystem (~/...) rather than /mnt/"
fi

# ─── Build Docker image if needed ────────────────────────────────────────────
if [[ "$PROVIDER" == "docker" ]]; then
  IMAGE_NAME="sandcastle:code-index"
  if ! docker images -q "$IMAGE_NAME" &>/dev/null; then
    echo "🔨 Building Sandcastle image ($IMAGE_NAME)..."
    docker build -t "$IMAGE_NAME" -f .sandcastle/Dockerfile .
  fi
fi

# ─── Run ─────────────────────────────────────────────────────────────────────
export SANDCASTLE_PROVIDER="$PROVIDER"
export SANDCASTLE_AGENT="$AGENT"
[[ -n "$MODEL" ]] && export MODEL="$MODEL"

cd "$(dirname "$0")/.."

case "$MODE" in
  interactive)
    npx tsx .sandcastle/interactive.ts --agent "$AGENT"
    ;;
  default)
    npx tsx .sandcastle/main.ts --agent "$AGENT"
    ;;
  *)
    npx tsx .sandcastle/main.ts --"$MODE" --agent "$AGENT"
    ;;
esac
