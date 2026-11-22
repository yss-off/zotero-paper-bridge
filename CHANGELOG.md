# Changelog

All notable changes to `zotero-paper-bridge` are documented in this file.

## [0.1.0] - 2026-11-22

### Added
- **Zero-dependency reverse-SSH architecture**:
  - Remote Linux CLI (`paper-bridge`) with `init`, `health`, `collections`, `add`, `batch`, `status`, `scan-inbox`, `queue`, and `drain`.
  - Atomic, idempotent JSONL offline queue on the remote server with auto-drain when the client tunnel connects.
  - Cross-platform local client helper (`paper-bridge-helper`, `paper_bridge/client/`) verified end-to-end on Windows 11 + Zotero 7, with macOS/Linux desktop launchers (`start-paper-bridge.sh`) and native notifications (`WinRT Toast` / `osascript` / `notify-send`).
  - Read-only local Zotero proxy (`127.0.0.1:23120`) blocking `POST`/`PUT`/`DELETE`/`PATCH` and `/connector/*` writes with `403`.
  - Private Unix Domain Socket MCP bridge (`zotero-mcp-paper-bridge`) with explicit `Zotero-API-Version: 3` header and `max_keepalive_connections=0` to survive SSH tunnel reconnects cleanly.
- **Compliant multi-source ingestion & metadata enrichment**:
  - Tier 1: arXiv (Atom API with automatic fallback to `/abs/<id>` citation meta tags + OpenAlex on HTTP 429) and legal Open Access PDFs (OpenAlex / Unpaywall).
  - Tier 2: Polite serial IEEE Xplore campus/VPN PDF fetching with `%PDF-` validation, 3–6 s jitter, and circuit breaker.
  - Pure-stdlib PDF DOI & XMP/`/Info` extractor (`pdf_meta.py`) to enrich sparse pre-fetch metadata (e.g. bare IEEE URLs) via Crossref before saving to Zotero.
  - Tier 3: Browser handoff with desktop notifications, per-batch local manual checklist HTML, and watched PDF inbox folder auto-attach.
- **AI coding agent integrations**:
  - Antigravity `zotero` plugin (`integrations/antigravity/zotero/`) bundling the `zotero_library` MCP server and `zotero-paper-bridge` Skill + vector SVG logo.
  - Drop-in integration guides for Claude Code, OpenAI Codex CLI, and Cursor.
- **Packaging & CI**:
  - Portable zero-dependency client bundler (`scripts/package-client-bundle.sh`), Linux server installer (`scripts/install-server.sh`), GitHub Actions cross-OS test CI, and tagged GitHub Release workflow.
