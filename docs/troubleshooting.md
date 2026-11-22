# Troubleshooting Guide

## 1. `zotero_local_api_ready: false` (or `zotero-mcp` returns 403 / empty)

Zotero 7 disables the Local Read API (`/api/users/0/...`) by default.

1. Open **Zotero 7 Desktop**.
2. Go to **Settings → Advanced**.
3. Check **"Allow other applications on this computer to communicate with Zotero"** (中文界面：**设置 → 高级 → 允许此计算机上的其他应用程序与 Zotero 通信**).
4. Re-run `smoke-test.ps1` (Windows) or `smoke-test.sh` (macOS/Linux).

## 2. `paper-bridge health --json` exits with code `1`

This is intentional for shell scripting: exit code `1` indicates either the SSH tunnel is offline (`reachable: false`) or Zotero Desktop is closed (`zotero_alive: false`). The JSON output on stdout is still complete and valid—check `error` or `helper.zotero_alive`.

## 3. SSH fails to forward Unix Domain Sockets (`remote port forwarding failed`)

Some Linux `sshd` configurations disable `AllowStreamLocalForwarding` or disallow overwriting stale socket files (`StreamLocalBindUnlink no`).

- Our launcher scripts automatically run `rm -f ~/.local/run/paper-bridge/*.sock` before connecting, which resolves `StreamLocalBindUnlink no`.
- If `AllowStreamLocalForwarding no` is enforced by your server admin, pass `-UseTcpFallback` (Windows) or `--use-tcp-fallback` (macOS/Linux). Bearer Token authentication remains mandatory over TCP loopback.

## 4. `SESSION_NOT_FOUND` when attaching a PDF to an existing Zotero item

Zotero 7's local Connector endpoint (`/connector/saveAttachment`) only allows attaching files to items created in the **same Connector session**:

- **In-session manual completion** (e.g. a batch created a `needs-pdf` metadata item and you drop the PDF into `~/Downloads/paper-bridge-inbox` during the same helper run): attaches directly to that exact item with zero duplicates.
- **Older historical metadata-only item** (from a previous session): `paper-bridge-helper` automatically saves a complete new item with the PDF tagged `merge-duplicate` (`status: "saved_merge_needed"`). Open Zotero's **Duplicate Items** view to merge them in one click.

## 5. `export.arxiv.org` returns HTTP 429 on shared campus IPs

Handled automatically since v0.1.0: `MetadataResolver` falls back to parsing `<meta name="citation_*">` tags from `https://arxiv.org/abs/<id>` and then OpenAlex's DataCite arXiv DOI record (`10.48550/arxiv.<id>`).

## 6. Bare IEEE URLs (`https://ieeexplore.ieee.org/document/<arnumber>`) return HTTP 202

IEEE's HTML landing page often serves an empty JS challenge to non-browser clients even when campus IP allows `stampPDF/getPDF.jsp` to download the full PDF. Since v0.1.0, `paper-bridge-helper` automatically extracts the DOI from the downloaded PDF's XMP metadata / `/Info` / page-1 stream and resolves complete official metadata via Crossref before saving to Zotero.

Whenever possible, passing the paper's canonical **DOI** (`10.1109/...`) is even faster and lets Zotero deduplicate *before* downloading the PDF.

## 7. Windows PowerShell execution policy blocks `.ps1`

Run with `-ExecutionPolicy Bypass`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\client\start-paper-bridge.ps1 -SshHost my-gpu-server
```
