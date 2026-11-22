---
name: zotero-paper-bridge
description: >-
  Download academic papers (IEEE Xplore, arXiv, DOI, BibTeX, or titles) into the
  user's local Zotero 7 library over a reverse SSH bridge (`paper-bridge`) and
  read them back via Zotero MCP. Use whenever the user asks to download, collect,
  aggregate, or save papers into Zotero—especially IEEE Xplore or paywalled papers
  that require the user's local campus/VPN session.
---

# Zotero Paper Bridge Workflow

Use `~/.local/bin/paper-bridge` (or `paper-bridge` on `PATH`) to send literature
ingestion requests from the remote Linux server to the user's local desktop helper
(Windows, macOS, or Linux), which deduplicates against Zotero 7, downloads PDFs
using the user's authorized campus/VPN network, enriches sparse metadata from the
downloaded PDF/Crossref, and ingests them into Zotero.

> **Read-only queries**: If the user only asks to search or read papers *already*
> in Zotero, call the bundled Zotero MCP tools (`zotero_search_items`,
> `zotero_item_metadata`, `zotero_item_fulltext`) directly—no need to invoke
> `paper-bridge`.

## Standard Procedure

1. **Check bridge & Zotero readiness first**:
   ```bash
   paper-bridge health --json || true
   ```
   - **Important**: `paper-bridge health --json` returns exit code `1` when the
     client tunnel is offline or Zotero Desktop is closed, while still printing
     valid JSON to stdout. Always parse the JSON (`reachable`,
     `helper.zotero_alive`, `helper.zotero_local_api_ready`,
     `pending_offline_queue`) instead of treating exit code `1` as a script crash.
   - To inspect exact Zotero collection names before passing `--collection`:
     ```bash
     paper-bridge collections
     ```
   - If `reachable` is `false`, do **not** probe ports or try to bypass IEEE on
     the server. You may still run `paper-bridge add` / `paper-bridge batch` to
     enqueue papers into `~/.local/share/zotero-paper-bridge/queue.jsonl`
     (`status: "queued"`), then ask the user to start `start-paper-bridge.ps1`
     (Windows) or `start-paper-bridge.sh` (macOS/Linux) to auto-drain.

2. **Ingest a single paper** (DOI, IEEE URL, arXiv ID, or title):
   ```bash
   paper-bridge add "<IDENTIFIER>" --collection "<COLLECTION>" --tag "<TAG>" --json
   ```
   - Prefer canonical DOIs (`10.1109/...`), IEEE document URLs, or arXiv IDs over
     bare titles to avoid ambiguity.
   - Handle the returned `status`:
     - `saved` / `already_exists` / `attached`: Paper and PDF are in Zotero
       (`item_key` returned). Proceed to Step 4.
     - `saved_merge_needed`: Zotero 7's Connector API rejects cross-session
       attachment (`SESSION_NOT_FOUND`), so a complete copy with PDF was saved and
       tagged `merge-duplicate` for one-click merge in Zotero's Duplicate Items view.
     - `needs_manual`: The publisher required institutional sign-in or human
       verification. Report `manual_url` and `reason` clearly to the user—**never**
       attempt to solve CAPTCHAs or bypass logins/paywalls.
     - `ambiguous`: Title search returned multiple plausible matches (`candidates`).
       Present the candidates and ask the user which DOI to ingest.
     - `queued`: Client helper is offline; queued on server for auto-drain.

3. **Ingest a batch of papers** (`.bib` file, line-separated `.txt`, or CLI args):
   ```bash
   paper-bridge batch <FILE_OR_IDENTIFIERS...> --collection "<COLLECTION>" --wait --json
   ```
   - Batches run strictly serially on the client with polite 3–6 s jitter and an
     IEEE circuit breaker.
   - If the batch ends in `completed_with_manual`, tell the user how many papers
     need manual PDF download (metadata is already saved with `needs-pdf` and the
     local helper opens a checklist page). Once the user saves PDFs into
     `~/Downloads/paper-bridge-inbox`, run:
     ```bash
     paper-bridge scan-inbox --batch-id "<BATCH_ID>" --json
     ```

4. **Read back newly ingested papers via Zotero MCP**:
   - Full-text read-back runs over the private Unix socket
     `~/.local/run/paper-bridge/zotero-read.sock` via `zotero-mcp-paper-bridge`
     (zero port `23119` collision and read-only proxy protection).
   - Call `zotero_search_items`, `zotero_item_metadata`, and `zotero_item_fulltext`
     using the returned `item_key` / `title` / `DOI` to read and cite full text.

## Offline Queue Commands

- View pending queued papers: `paper-bridge queue list`
- Drain manually once tunnel is active: `paper-bridge drain --wait --json`
