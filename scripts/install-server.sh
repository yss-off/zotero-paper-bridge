#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
INSTALL_ANTIGRAVITY="auto"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--with-antigravity | --no-antigravity] [--bin-dir DIR]

Installs zotero-paper-bridge commands into ~/.local/bin, initializes local CLI and
helper configs with a shared 256-bit Bearer token, and optionally installs the
Antigravity Zotero plugin.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-antigravity) INSTALL_ANTIGRAVITY="yes"; shift ;;
    --no-antigravity)   INSTALL_ANTIGRAVITY="no"; shift ;;
    --bin-dir)          BIN_DIR="$2"; shift 2 ;;
    -h|--help)          usage; exit 0 ;;
    *)                  echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

mkdir -p "${BIN_DIR}"
chmod +x "${ROOT_DIR}/scripts/paper-bridge" "${ROOT_DIR}/scripts/paper-bridge-helper" "${ROOT_DIR}/scripts/zotero-mcp-paper-bridge"
ln -sfn "${ROOT_DIR}/scripts/paper-bridge" "${BIN_DIR}/paper-bridge"
ln -sfn "${ROOT_DIR}/scripts/paper-bridge-helper" "${BIN_DIR}/paper-bridge-helper"
ln -sfn "${ROOT_DIR}/scripts/zotero-mcp-paper-bridge" "${BIN_DIR}/zotero-mcp-paper-bridge"

echo "[OK] Linked ${BIN_DIR}/paper-bridge -> ${ROOT_DIR}/scripts/paper-bridge"
echo "[OK] Linked ${BIN_DIR}/paper-bridge-helper -> ${ROOT_DIR}/scripts/paper-bridge-helper"
echo "[OK] Linked ${BIN_DIR}/zotero-mcp-paper-bridge -> ${ROOT_DIR}/scripts/zotero-mcp-paper-bridge"

"${BIN_DIR}/paper-bridge" init >/dev/null
TOKEN_VAL="$(cat "${HOME}/.config/paper-bridge/token")"
"${BIN_DIR}/paper-bridge-helper" init --token "${TOKEN_VAL}" >/dev/null
echo "[OK] Initialized ~/.config/paper-bridge, ~/.paper-bridge (shared token mode 600), and ~/.local/run/paper-bridge (mode 700)"

if [[ "${INSTALL_ANTIGRAVITY}" == "auto" && -d "${HOME}/.gemini/config" ]]; then
  INSTALL_ANTIGRAVITY="yes"
fi

if [[ "${INSTALL_ANTIGRAVITY}" == "yes" ]]; then
  SRC_PLUGIN="${ROOT_DIR}/integrations/antigravity/zotero"
  DST_PLUGIN="${HOME}/.gemini/config/plugins/zotero"
  mkdir -p "${DST_PLUGIN}/assets" "${DST_PLUGIN}/skills/zotero-paper-bridge"

  ln -sfn "${SRC_PLUGIN}/plugin.json" "${DST_PLUGIN}/plugin.json"
  ln -sfn "${SRC_PLUGIN}/README.md" "${DST_PLUGIN}/README.md"
  ln -sfn "${SRC_PLUGIN}/assets/logo.svg" "${DST_PLUGIN}/assets/logo.svg"
  ln -sfn "${SRC_PLUGIN}/skills/zotero-paper-bridge/SKILL.md" "${DST_PLUGIN}/skills/zotero-paper-bridge/SKILL.md"

  # Render resolved absolute MCP command path for GUI/systemd Language Server environments
  cat > "${DST_PLUGIN}/mcp_config.json" <<EOF
{
  "mcpServers": {
    "library": {
      "command": "${BIN_DIR}/zotero-mcp-paper-bridge",
      "env": {
        "ZOTERO_LOCAL": "true"
      }
    }
  }
}
EOF
  echo "[OK] Installed Antigravity plugin at ${DST_PLUGIN} (MCP server: zotero_library)"
fi

echo ""
echo "Next steps:"
echo "  1. Enable Local API in local Zotero 7 (Settings -> Advanced -> Allow other applications...)."
echo "  2. Set zotero_base_url in ~/.paper-bridge/config.toml if Zotero uses a custom port."
echo "  3. Verify Zotero access: paper-bridge-helper smoke-test"
echo "  4. Start the local helper daemon: paper-bridge-helper serve --no-browser"
echo "     (or install scripts/paper-bridge-helper.service.example into ~/.config/systemd/user/)"
