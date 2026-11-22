# Cursor Integration

## 1. Install `zotero-paper-bridge` on your remote Linux server

```bash
./scripts/install-server.sh --no-antigravity
```

## 2. Add the Zotero MCP server in `.cursor/mcp.json` (or global `~/.cursor/mcp.json`)

Replace `<YOUR_HOME>` with your remote home directory:

```json
{
  "mcpServers": {
    "zotero": {
      "command": "<YOUR_HOME>/.local/bin/zotero-mcp-paper-bridge",
      "env": {
        "ZOTERO_LOCAL": "true"
      }
    }
  }
}
```

You can also tell Cursor's agent rules to invoke `paper-bridge add` / `paper-bridge batch` (see `integrations/antigravity/zotero/skills/zotero-paper-bridge/SKILL.md` for the full command workflow).
