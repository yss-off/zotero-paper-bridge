#!/usr/bin/env bash
# Start local Zotero Paper Bridge helper on macOS / Linux desktop, open private
# reverse SSH Unix-socket tunnels, and auto-drain queued papers on the remote server.
set -euo pipefail

SSH_HOST=""
HELPER_PORT=18765
ZOTERO_RO_PROXY_PORT=23120
USE_TCP_FALLBACK=0
USE_TCP_23119=0
NO_ZOTERO_READBACK=0
PYTHON_EXE="${PYTHON_EXE:-python3}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <ssh-host> [options]

Options:
  --use-tcp-fallback      Use 127.0.0.1:18765 instead of remote Unix socket
  --use-tcp-23119         Forward remote 127.0.0.1:23119 instead of zotero-read.sock
  --no-zotero-readback    Only forward helper API (skip Zotero read-back socket)
  --helper-port PORT      Local helper port (default: 18765)
  --ro-proxy-port PORT    Local read-only Zotero proxy port (default: 23120)
  --python BIN            Python executable (default: python3)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --use-tcp-fallback)   USE_TCP_FALLBACK=1; shift ;;
    --use-tcp-23119)      USE_TCP_23119=1; shift ;;
    --no-zotero-readback) NO_ZOTERO_READBACK=1; shift ;;
    --helper-port)        HELPER_PORT="$2"; shift 2 ;;
    --ro-proxy-port)      ZOTERO_RO_PROXY_PORT="$2"; shift 2 ;;
    --python)             PYTHON_EXE="$2"; shift 2 ;;
    -h|--help)            usage; exit 0 ;;
    -*)                   echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *)                    SSH_HOST="$1"; shift ;;
  esac
done

if [[ -z "${SSH_HOST}" ]]; then
  echo "[ERROR] SSH host is required." >&2
  usage
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

TOKEN_PATH="${HOME}/.paper-bridge/token"
if [[ ! -f "${TOKEN_PATH}" ]]; then
  echo "[ERROR] Token file not found: ${TOKEN_PATH}" >&2
  echo "Initialize with: ${PYTHON_EXE} -m paper_bridge.client.cli init --token <YOUR_TOKEN>" >&2
  exit 1
fi
chmod 600 "${TOKEN_PATH}"

HELPER_PID=""
cleanup() {
  if [[ -n "${HELPER_PID}" ]] && kill -0 "${HELPER_PID}" 2>/dev/null; then
    echo "[INFO] Stopping local helper (PID ${HELPER_PID}) ..."
    kill "${HELPER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if "${PYTHON_EXE}" -c "import socket,sys; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', ${HELPER_PORT})); s.close()" 2>/dev/null; then
  echo "[OK] paper-bridge-helper is already running on 127.0.0.1:${HELPER_PORT}"
else
  echo "[INFO] Starting paper-bridge-helper on 127.0.0.1:${HELPER_PORT} ..."
  "${PYTHON_EXE}" -m paper_bridge.client.cli serve &
  HELPER_PID=$!
  sleep 2
fi

REMOTE_HOME="$(ssh "${SSH_HOST}" 'printf "%s" "$HOME"')"
if [[ -z "${REMOTE_HOME}" ]]; then
  echo "[ERROR] Failed to detect remote \$HOME on ${SSH_HOST}." >&2
  exit 1
fi

SSH_ARGS=(
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o ExitOnForwardFailure=no
)
SOCKETS_TO_CLEAN=()

if [[ "${USE_TCP_FALLBACK}" -eq 1 ]]; then
  SSH_ARGS+=(-R "127.0.0.1:${HELPER_PORT}:127.0.0.1:${HELPER_PORT}")
else
  REMOTE_HELPER_SOCK="${REMOTE_HOME}/.local/run/paper-bridge/helper.sock"
  SOCKETS_TO_CLEAN+=("${REMOTE_HELPER_SOCK}")
  SSH_ARGS+=(-R "${REMOTE_HELPER_SOCK}:127.0.0.1:${HELPER_PORT}")
fi

if [[ "${NO_ZOTERO_READBACK}" -eq 0 ]]; then
  if [[ "${USE_TCP_23119}" -eq 1 ]]; then
    SSH_ARGS+=(-R "127.0.0.1:23119:127.0.0.1:${ZOTERO_RO_PROXY_PORT}")
  else
    REMOTE_ZOTERO_SOCK="${REMOTE_HOME}/.local/run/paper-bridge/zotero-read.sock"
    SOCKETS_TO_CLEAN+=("${REMOTE_ZOTERO_SOCK}")
    SSH_ARGS+=(-R "${REMOTE_ZOTERO_SOCK}:127.0.0.1:${ZOTERO_RO_PROXY_PORT}")
  fi
fi

if [[ "${#SOCKETS_TO_CLEAN[@]}" -gt 0 ]]; then
  echo "[INFO] Cleaning stale remote sockets on ${SSH_HOST} ..."
  ssh "${SSH_HOST}" "mkdir -p ~/.local/run/paper-bridge && chmod 700 ~/.local/run/paper-bridge && rm -f ${SOCKETS_TO_CLEAN[*]}"
fi

REMOTE_CMD="echo '[paper-bridge] Tunnel established. Draining offline queue...' && ~/.local/bin/paper-bridge drain || true; echo '[paper-bridge] Tunnel active. Press Ctrl+C to close.'; sleep infinity"
echo "[INFO] Opening reverse SSH tunnel to ${SSH_HOST} ..."
ssh "${SSH_ARGS[@]}" "${SSH_HOST}" "${REMOTE_CMD}"
