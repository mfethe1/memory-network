#!/bin/sh
set -eu

fail() {
  message="$1"
  code="${2:-64}"
  echo "$message" >&2
  exit "$code"
}

require_numeric_port() {
  name="$1"
  value="$2"
  case "$value" in
    "" | *[!0-9]*)
      fail "$name must be a numeric TCP port"
      ;;
  esac
}

case "${NATS_TOKEN:-}" in
  "")
    fail "NATS_TOKEN is required; refusing to start unauthenticated NATS"
    ;;
  *[!A-Za-z0-9_-]*)
    fail "NATS_TOKEN must contain only URL-safe token characters: A-Z a-z 0-9 _ -"
    ;;
esac

if [ -z "${RAILWAY_VOLUME_MOUNT_PATH:-}" ]; then
  fail "RAILWAY_VOLUME_MOUNT_PATH is required so JetStream does not use ephemeral storage" 73
fi

if [ ! -d "$RAILWAY_VOLUME_MOUNT_PATH" ]; then
  fail "RAILWAY_VOLUME_MOUNT_PATH does not exist: $RAILWAY_VOLUME_MOUNT_PATH" 73
fi

NATS_CLIENT_PORT="${RAILWAY_TCP_APPLICATION_PORT:-4222}"
NATS_MONITOR_PORT="${PORT:-8222}"
NATS_STORE_DIR="${NATS_STORE_DIR:-$RAILWAY_VOLUME_MOUNT_PATH/jetstream}"

require_numeric_port "RAILWAY_TCP_APPLICATION_PORT" "$NATS_CLIENT_PORT"
require_numeric_port "PORT" "$NATS_MONITOR_PORT"

case "$NATS_STORE_DIR" in
  "$RAILWAY_VOLUME_MOUNT_PATH" | "$RAILWAY_VOLUME_MOUNT_PATH"/*)
    ;;
  *)
    fail "NATS_STORE_DIR must be inside RAILWAY_VOLUME_MOUNT_PATH"
    ;;
esac

mkdir -p "$NATS_STORE_DIR"

config_file="/tmp/openclaw-nats-server.conf"
umask 077
cat > "$config_file" <<EOF
server_name: "openclaw-railway-nats"
port: ${NATS_CLIENT_PORT}
http_port: ${NATS_MONITOR_PORT}

jetstream {
  store_dir: "${NATS_STORE_DIR}"
}

authorization {
  token: "${NATS_TOKEN}"
}
EOF

echo "Starting OpenClaw Railway NATS: client_port=${NATS_CLIENT_PORT}, monitor_port=${NATS_MONITOR_PORT}, jetstream=${NATS_STORE_DIR}, NATS token auth enabled"
exec nats-server -c "$config_file"
