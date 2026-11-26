---
name: zotero-paper-bridge
description: >-
  Download academic papers (IEEE Xplore, arXiv, DOI, BibTeX, or titles) into the
  local Zotero 7 library via `paper-bridge` and read them back via Zotero MCP.
  Use whenever the user asks to download, collect, aggregate, or save papers into
  Zotero—including IEEE Xplore papers over the server's authorized campus network.
---

# Zotero Paper Bridge Workflow

Use `~/.local/bin/paper-bridge` (or `paper-bridge` on `PATH`) to send literature
ingestion requests to the local `paper-bridge-helper` daemon over the private Unix
socket (`~/.local/run/paper-bridge/helper.sock`). The helper deduplicates against
local Zotero 7, downloads PDFs using the server's authorized campus/OA network,
enriches sparse metadata from the downloaded PDF/Crossref, and ingests them into
Zotero.

> **Read-only queries**: If the user only asks to search or read papers *already*
> in Zotero, call the bundled Zotero MCP tools (`zotero_search_items`,
> `zotero_item_metadata`, `zotero_item_fulltext`) directly—no need to invoke
> `paper-bridge`.

## Standard Procedure

1. **Check helper & Zotero readiness first**:
   ```bash
   paper-bridge health --json || true
   ```
   - Parse stdout even when `health --json` exits `1`. It prints valid JSON when
     the helper daemon is stopped, local Zotero is closed, the offline queue file
     has an error, or a requested probe fails. Check `reachable`,
     `helper.zotero_alive`, `helper.zotero_local_api_ready`,
     `pending_offline_queue`, `queue_error`, and `probes` as applicable.
   - To inspect exact Zotero collection names before passing `--collection`:
     ```bash
     paper-bridge collections
     ```
   - If `reachable` is `false`, do **not** bypass publisher gates or scrape IEEE
     manually. You can still use `add` or `batch` to queue papers until
     `paper-bridge-helper.service` and local Zotero 7 are running.

2. **Ingest a single paper** (DOI, IEEE URL, arXiv ID, or title):
   ```bash
   paper-bridge add "<IDENTIFIER>" --collection "<COLLECTION>" --tag "<TAG>" --json
   ```
   - Prefer canonical DOIs (`10.1109/...`), IEEE document URLs, or arXiv IDs over
     bare titles to avoid ambiguity.
   - Handle the returned `status`:
     - `saved` / `already_exists` / `attached` mean the item is in Zotero. Check
       `pdf_attached` before reporting PDF availability or proceeding to Step 4.
       A `saved` item can have `pdf_attached: false` with the attachment error in
       `reason`.
     - `saved_merge_needed` means the helper saved a new copy tagged
       `merge-duplicate` when Zotero rejected a cross-session attachment
       (`SESSION_NOT_FOUND`). With `pdf_attached: true`, the copy includes the PDF.
       Merge the items in Zotero's **Duplicate Items** view. The queue records
       `needs_attention` and `drain --wait` exits `1` when it reports this outcome.
       With no manual downloads outstanding, the batch ends in
       `completed_with_errors` and `batch --wait` exits `1`.
     - `needs_manual`: The publisher required institutional sign-in or human
       verification. Report `manual_url` and `reason` clearly to the user—**never**
       attempt to solve CAPTCHAs or bypass logins/paywalls.
     - `ambiguous`: Title search returned multiple plausible matches (`candidates`).
       Present the candidates and ask the user which DOI to ingest.
     - `failed` returns exit code `1` on `add`. Report `reason` to the user.
     - `queued` means the request is in the offline queue. Once
       `paper-bridge-helper.service` and local Zotero 7 are running, run
       `paper-bridge drain --wait`.

3. **Ingest a batch of papers** (`.bib` file, line-separated `.txt`, or CLI args):
   ```bash
   paper-bridge batch <FILE_OR_IDENTIFIERS...> --collection "<COLLECTION>" --wait --json
   ```
   - Batches run strictly serially with polite 3–6 s jitter and an IEEE circuit
     breaker.
   - A `queued` result follows the same recovery steps as `add`.
   - If the batch ends in `completed_with_manual`, tell the user how many papers
     need manual PDF download (metadata is already saved with `needs-pdf`). Once
     the user saves PDFs into `~/Downloads/paper-bridge-inbox`, run:
     ```bash
     paper-bridge scan-inbox --batch-id "<BATCH_ID>" --json
     ```
   - `completed_with_errors` means at least one paper failed or still needs
     attention after manual downloads are accounted for. `batch --wait` prints
     the batch result and exits `1`. Check each paper's `status`, `pdf_attached`,
     and `reason`.

4. **Read back newly ingested papers via Zotero MCP**:
   - By default, `zotero-mcp-paper-bridge` reads through the private Unix socket
     `~/.local/run/paper-bridge/zotero-read.sock`.
   - Call `zotero_search_items`, `zotero_item_metadata`, and `zotero_item_fulltext`
     using the returned `item_key` / `title` / `DOI` to read and cite full text.

## Offline Queue Commands

| Queue status | Meaning |
| --- | --- |
| `pending` | Waiting to be sent to the helper. |
| `submitted` | Assigned to a batch whose result is still being tracked. |
| `done` | Zotero has the item and its PDF. |
| `needs_attention` | Review the result, for example to finish a manual download, resolve an ambiguous match, merge a duplicate, or attach a missing PDF. |
| `failed` | Ingestion failed or the batch could not be submitted or recovered. |

- `paper-bridge queue list` shows outstanding tasks. Add `--all` to include `done`.
- `paper-bridge drain --wait --json` submits pending tasks and checks submitted
  batches once the local helper and Zotero 7 are available.
- `paper-bridge queue retry <TASK_ID>` moves a `failed`, `needs_attention`, or
  `submitted` task back to `pending`. The next drain submits it again.
- `paper-bridge queue clear` removes `done` entries.
