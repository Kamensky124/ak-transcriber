#!/usr/bin/env bash
# setup.sh — build image, extract API keys, install quadlet, enable service.
# Compatible with bash and zsh (invoked via bash or as ./setup.sh with shebang).
# Flags: --env-only (just refresh transcriber.env) | --no-start (build+install, don't enable)
set -euo pipefail

SECRETS_FILE="$HOME/org/env-files/secrets"
ENV_DIR="$HOME/org/transcriber"
ENV_FILE="$ENV_DIR/transcriber.env"
DATA_DIR="$ENV_DIR/data"
QUADLET_DIR="$HOME/.config/containers/systemd"
QUADLET_SRC="$(cd "$(dirname "$0")" && pwd)/transcriber.container"
IMAGE="localhost/transcriber:latest"

ENV_ONLY=0
NO_START=0
for arg in "$@"; do
  case "$arg" in
    --env-only) ENV_ONLY=1 ;;
    --no-start) NO_START=1 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# extract_key NAME — pull NAME from the secrets file, stripping an optional
# `export ` prefix and surrounding quotes. Fails loudly if missing/empty.
extract_key() {
  local name="$1" line value
  line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$name=" "$SECRETS_FILE" | head -n1 || true)
  if [ -z "$line" ]; then
    echo "ERROR: $name not found in $SECRETS_FILE" >&2
    exit 1
  fi
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  if [ -z "$value" ]; then
    echo "ERROR: $name is empty in $SECRETS_FILE" >&2
    exit 1
  fi
  printf '%s\n' "$value"
}

if [ ! -f "$SECRETS_FILE" ]; then
  echo "ERROR: secrets file not found: $SECRETS_FILE" >&2
  exit 1
fi

KEY1=$(extract_key GEMINI_API_KEY)
KEY2=$(extract_key GEMINI_API_KEY_2)
KEY3=$(extract_key GEMINI_API_KEY_3)

mkdir -p "$ENV_DIR" "$DATA_DIR"
umask 177
{
  echo "GEMINI_API_KEY=$KEY1"
  echo "GEMINI_API_KEY_2=$KEY2"
  echo "GEMINI_API_KEY_3=$KEY3"
  echo "TRANSCRIBE_INBOX=$HOME/org/media/inbox"
  echo "TRANSCRIBE_DB=/var/lib/transcriber/queue.db"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "Wrote $ENV_FILE (chmod 600)"

if [ "$ENV_ONLY" -eq 1 ]; then
  echo "--env-only: done. Restart with: systemctl --user restart transcriber"
  exit 0
fi

mkdir -p "$HOME/org/media/inbox" "$HOME/.cache/transcriber-models"

echo "Building $IMAGE ..."
podman build -t "$IMAGE" "$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$QUADLET_DIR"
install -m 644 "$QUADLET_SRC" "$QUADLET_DIR/transcriber.container"

systemctl --user daemon-reload
if [ "$NO_START" -eq 0 ]; then
  systemctl --user enable --now transcriber.service
  echo "Service enabled."
fi

echo
echo "Ops hints:"
echo "  logs:   podman logs -f transcriber"
echo "  status: podman exec transcriber python /app/transcriberd.py status"
echo "  rotate keys: edit $SECRETS_FILE && $0 --env-only && systemctl --user restart transcriber"
