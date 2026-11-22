# Zotero Plugin (Antigravity)

Bundles Zotero 7 **search & full-text reading (MCP)** together with **reverse-SSH literature ingestion (`zotero-paper-bridge` Skill)** as one coherent Antigravity plugin.

## Components

- **MCP Server (`zotero_library`)**: Connects to your local Zotero 7 over the private read-only Unix socket (`~/.local/run/paper-bridge/zotero-read.sock`) via `zotero-mcp-paper-bridge`, providing `zotero_search_items`, `zotero_item_metadata`, and `zotero_item_fulltext`.
- **Skill (`zotero-paper-bridge`)**: Guides the agent to use `paper-bridge` to download IEEE Xplore / arXiv / DOI / BibTeX papers via your local campus/VPN client into Zotero 7, then read them back via MCP.

## Installation

Run `./scripts/install-server.sh --with-antigravity` from the `zotero-paper-bridge` repository root. It links the plugin into `~/.gemini/config/plugins/zotero/` and writes the resolved path to `zotero-mcp-paper-bridge` in `mcp_config.json`.
