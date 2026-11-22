# Claude Code Integration

## 1. Install `zotero-paper-bridge` on your remote Linux server

```bash
./scripts/install-server.sh --no-antigravity
```

## 2. Register the private read-only Zotero MCP server

```bash
claude mcp add zotero -- "$HOME/.local/bin/zotero-mcp-paper-bridge"
```

## 3. (Optional) Add the Skill to Claude Code

Copy or symlink the reusable Skill into `~/.claude/skills/zotero-paper-bridge/`:

```bash
mkdir -p ~/.claude/skills/zotero-paper-bridge
ln -sfn "$(pwd)/integrations/antigravity/zotero/skills/zotero-paper-bridge/SKILL.md" \
  ~/.claude/skills/zotero-paper-bridge/SKILL.md
```

Claude Code will then automatically use `paper-bridge` for downloading IEEE/arXiv/DOI papers into Zotero and read full text back through the `zotero` MCP server.
