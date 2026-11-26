# Troubleshooting Guide

## 1. `zotero_local_api_ready: false` (or `zotero-mcp` returns 403 / empty)

Zotero 7 disables the Local Read API (`/api/users/0/...`) by default.

1. Open **Zotero 7 Desktop** (or edit `prefs.js` while Zotero is stopped).
2. Go to **Settings → Advanced**.
3. Check **"Allow other applications on this computer to communicate with Zotero"** (中文界面：**设置 → 高级 → 允许此计算机上的其他应用程序与 Zotero 通信**; preference key `extensions.zotero.httpServer.localAPI.enabled = true`).
4. Verify with `paper-bridge-helper smoke-test` and `paper-bridge health --json`.

## 2. `paper-bridge health --json` exits with code `1`

The command prints a complete, valid JSON response to stdout and exits `1` in any of these cases:

- The local helper daemon is stopped or unreachable. Check `reachable`, `error`, and `systemctl --user status paper-bridge-helper.service`.
- Local Zotero 7 is closed or listening on a different port. Check `helper.zotero_alive` and `zotero_base_url` in `~/.paper-bridge/config.toml`.
- The offline queue file cannot be read or parsed. Check `queue_error`.
- Either requested probe (`--fulltext-key` or `--webdav-file-url`) fails. Check `probes`.

## 3. Custom local Zotero port or port conflicts

If local Zotero runs on a custom `extensions.zotero.httpServer.port` (for example `23137` instead of `23119`), set it in `~/.paper-bridge/config.toml`:

```toml
zotero_base_url = "http://127.0.0.1:23137"
```

Because `paper-bridge-helper` binds private Unix sockets (`helper.sock` and `zotero-read.sock`) by default, it does not occupy TCP port `18765` or `23120`. Leave `tcp_base_url = ""` in `~/.config/paper-bridge/config.toml` so the CLI never falls back to another local service using `18765`.

## 4. `SESSION_NOT_FOUND` when attaching a PDF to an existing Zotero item

Zotero 7's local Connector endpoint (`/connector/saveAttachment`) only allows attaching files to items created in the **same Connector session**:

- **In-session manual completion** (e.g. a batch created a `needs-pdf` metadata item and you drop the PDF into `~/Downloads/paper-bridge-inbox` during the same helper run): attaches directly to that exact item with zero duplicates.
- **Older metadata-only item** (from a previous session) may require a new copy. If attaching directly fails, `paper-bridge-helper` saves a new item tagged `merge-duplicate` (`status: "saved_merge_needed"`). Check `pdf_attached` and merge the items in Zotero's **Duplicate Items** view.

Automatic inbox matching uses identifiers from the filename and a DOI declared in the PDF's metadata. If several files match equally well, open the Zotero item and attach the correct file manually.

## 5. `export.arxiv.org` returns HTTP 429 on shared campus IPs

Handled automatically: `MetadataResolver` falls back to parsing `<meta name="citation_*">` tags from `https://arxiv.org/abs/<id>` and then OpenAlex's DataCite arXiv DOI record (`10.48550/arxiv.<id>`).

## 6. Bare IEEE URLs (`https://ieeexplore.ieee.org/document/<arnumber>`) return HTTP 202

IEEE's HTML landing page often serves an empty JS challenge to non-browser clients even when campus IP allows `stampPDF/getPDF.jsp` to download the full PDF. `paper-bridge-helper` automatically extracts the DOI from the downloaded PDF's XMP metadata / `/Info` / page-1 stream and resolves complete official metadata via Crossref before saving to Zotero.

Whenever possible, passing the paper's canonical **DOI** (`10.1109/...`) is even faster and lets Zotero deduplicate *before* downloading the PDF.

## 7. `SSL: CERTIFICATE_VERIFY_FAILED` under relocated Conda Python

Relocated Conda/Anaconda Python builds on Linux may have a broken built-in OpenSSL CA path. `paper-bridge-helper` automatically falls back to standard OS CA bundles (e.g. `/etc/ssl/certs/ca-certificates.crt`) while keeping strict TLS verification enabled. You can also set `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` or run with `/usr/bin/python3`.

## 8. Check and drain the offline queue

Once `paper-bridge-helper.service` and local Zotero 7 are running, use `paper-bridge drain --wait` to submit waiting tasks and check the outcome of each paper in an existing batch. `paper-bridge queue list` shows waiting tasks, submissions in progress, failures, and papers that need manual action.

Run `paper-bridge queue retry <task-id>` to move a `failed`, `needs_attention`, or `submitted` task back to `pending`. The next `paper-bridge drain --wait` submits it again. If a drain is already running, let it finish before retrying or clearing the queue.

## 9. Check the MCP and WebDAV read paths

Use a known indexed attachment key for the Zotero read probe and a known existing file URL for the WebDAV probe. The Zotero probe uses the same `PAPER_BRIDGE_ZOTERO_READ_SOCKET` and `PAPER_BRIDGE_ZOTERO_READ_URL` settings as the MCP bridge. The probes report availability and HTTP status without printing the attachment or its contents.

```bash
paper-bridge health --json --fulltext-key "$attachment_key" \
  --webdav-file-url "$webdav_file_url" --webdav-user "$webdav_user" \
  --webdav-password-file "$webdav_password_file"
```
