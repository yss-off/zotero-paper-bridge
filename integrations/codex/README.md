# OpenAI Codex CLI Integration

## 1. Install `zotero-paper-bridge` on your Linux host (with local Zotero 7)

```bash
./scripts/install-server.sh --no-antigravity
```

## 2. Register the private read-only Zotero MCP server in `~/.codex/config.toml`

Replace `<YOUR_HOME>` with your home directory (e.g. `/home/alice`):

```toml
[mcp_servers.zotero]
command = "<YOUR_HOME>/.local/bin/zotero-mcp-paper-bridge"

[mcp_servers.zotero.env]
ZOTERO_LOCAL = "true"
```

## 3. (Optional) Add the Skill to Codex

```bash
mkdir -p ~/.codex/skills/zotero-paper-bridge
ln -sfn "$(pwd)/integrations/antigravity/zotero/skills/zotero-paper-bridge/SKILL.md" \
  ~/.codex/skills/zotero-paper-bridge/SKILL.md
```
