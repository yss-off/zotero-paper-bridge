"""Cross-platform desktop handoff utilities: browser open, native notifications, manual checklist, inbox PDF matcher."""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from ..common.identifiers import normalize_doi, normalize_title
from ..common.models import PaperMetadata
from ..common.security import is_safe_http_url
from .fetcher import is_valid_pdf

INCOMPLETE_SUFFIXES = (".crdownload", ".part", ".tmp", ".download")


@dataclass
class PendingManualPaper:
    task_id: str
    identifier: str
    metadata: PaperMetadata
    collection: str | None
    tags: list[str]
    item_key: str | None
    manual_url: str
    reason: str
    batch_id: str | None = None
    completed: bool = False


def open_in_browser(url: str) -> bool:
    if not is_safe_http_url(url) and not url.startswith("http://127.0.0.1:"):
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def show_desktop_notification(title: str, message: str) -> bool:
    """Show a native desktop notification on Windows, macOS, or Linux (zero third-party deps)."""
    clean_title = " ".join(title.splitlines()).strip()[:120]
    clean_msg = " ".join(message.splitlines()).strip()[:300]

    if os.name == "nt":
        safe_title = clean_title.replace("'", "''")
        safe_msg = clean_msg.replace("'", "''")
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            f"$xml.LoadXml('<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{html.escape(safe_title)}</text><text id=\"2\">{html.escape(safe_msg)}</text></binding></visual></toast>'); "
            "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml; "
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('zotero-paper-bridge'); "
            "$notifier.Show($toast)"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                check=False,
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    if sys.platform == "darwin":
        osa_title = clean_title.replace("\\", "\\\\").replace('"', '\\"')
        osa_msg = clean_msg.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{osa_msg}" with title "{osa_title}"',
                ],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    if sys.platform.startswith("linux"):
        if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("notify-send"):
            try:
                subprocess.run(
                    ["notify-send", clean_title, clean_msg],
                    check=False,
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                return False

    return False


# Backward-compatible alias
show_windows_toast = show_desktop_notification


def score_pdf_match(pdf_path: Path, pending: PendingManualPaper) -> float:
    """Score how likely a downloaded PDF file matches a pending paper."""
    stem = pdf_path.stem.lower()
    meta = pending.metadata

    if meta.ieee_arnumber and meta.ieee_arnumber in stem:
        return 1.0
    if meta.arxiv_id:
        base_arxiv = meta.arxiv_id.lower().split("v")[0]
        if base_arxiv in stem:
            return 1.0
    if meta.doi:
        norm_doi = normalize_doi(meta.doi)
        doi_tail = norm_doi.split("/", 1)[-1]
        if doi_tail and len(doi_tail) >= 6 and doi_tail in stem:
            return 0.95

    norm_stem = normalize_title(pdf_path.stem)
    norm_title = normalize_title(meta.title)
    if not norm_stem or not norm_title:
        return 0.0
    stem_tokens = [t for t in norm_stem.split() if len(t) >= 3]
    title_tokens = [t for t in norm_title.split() if len(t) >= 3]
    if not stem_tokens or not title_tokens:
        return 0.0
    overlap = len(set(stem_tokens) & set(title_tokens))
    ratio = overlap / max(1, min(len(set(stem_tokens)), len(set(title_tokens))))
    if overlap >= 3 and ratio >= 0.6:
        return 0.8 + 0.15 * ratio
    return 0.0


def find_matching_pdf_in_inbox(
    inbox_dir: Path,
    pending: PendingManualPaper,
    *,
    seen_files: set[str] | None = None,
    min_mtime: float | None = None,
) -> Path | None:
    if not inbox_dir.exists():
        return None
    best_path: Path | None = None
    best_score = 0.0
    for candidate in sorted(inbox_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        if candidate.name.lower().endswith(INCOMPLETE_SUFFIXES):
            continue
        key = str(candidate.resolve())
        if seen_files and key in seen_files:
            continue
        try:
            st = candidate.stat()
        except OSError:
            continue
        if min_mtime is not None and st.st_mtime < min_mtime:
            continue
        if st.st_size < 1024:
            continue
        score = score_pdf_match(candidate, pending)
        if score > best_score:
            best_score = score
            best_path = candidate

    if best_path is not None and best_score >= 0.8:
        try:
            data = best_path.read_bytes()
        except OSError:
            return None
        if is_valid_pdf(data):
            return best_path
    return None


def render_manual_checklist_html(
    *,
    batch_id: str,
    pending_items: list[PendingManualPaper],
    inbox_dir: Path,
    scan_endpoint: str,
    local_nonce: str,
) -> str:
    rows_html: list[str] = []
    for idx, item in enumerate(pending_items, start=1):
        status_label = "Done / 已完成" if item.completed else "Pending / 待补 PDF"
        badge_cls = "done" if item.completed else "pending"
        title_esc = html.escape(item.metadata.title or item.identifier)
        doi_esc = html.escape(item.metadata.doi or "-")
        reason_esc = html.escape(item.reason or "needs_manual")
        url_esc = html.escape(item.manual_url or item.metadata.canonical_landing_url())
        rows_html.append(
            f"""
            <tr class="{badge_cls}">
              <td>{idx}</td>
              <td><strong>{title_esc}</strong><br><small>DOI: {doi_esc}</small></td>
              <td><code>{reason_esc}</code></td>
              <td><span class="badge {badge_cls}">{status_label}</span></td>
              <td><a class="btn" href="{url_esc}" target="_blank" rel="noopener">Open Page / 打开页面</a></td>
            </tr>
            """
        )

    inbox_esc = html.escape(str(inbox_dir))
    batch_esc = html.escape(batch_id)
    nonce_esc = html.escape(local_nonce)
    scan_esc = html.escape(scan_endpoint)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Zotero Paper Bridge - Manual PDF Checklist ({batch_esc})</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1020px; margin: 32px auto; padding: 0 16px; color: #1f2937; }}
    h1 {{ font-size: 22px; margin-bottom: 8px; }}
    .tip {{ background: #eff6ff; border-left: 4px solid #2563eb; padding: 12px 16px; margin: 16px 0; border-radius: 4px; line-height: 1.6; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f9fafb; }}
    .btn {{ display: inline-block; padding: 6px 12px; background: #2563eb; color: #fff; text-decoration: none; border-radius: 6px; border: 0; cursor: pointer; font-size: 13px; }}
    .btn.secondary {{ background: #059669; }}
    .badge {{ padding: 2px 8px; border-radius: 999px; font-size: 12px; }}
    .badge.pending {{ background: #fef3c7; color: #92400e; }}
    .badge.done {{ background: #d1fae5; color: #065f46; }}
    #scan-result {{ margin-top: 12px; font-size: 14px; color: #374151; }}
  </style>
</head>
<body>
  <h1>Manual PDF Checklist / 待人工补全 PDF 清单 (<code>{batch_esc}</code>)</h1>
  <div class="tip">
    <strong>Recommended steps (avoids duplicate Zotero items):</strong><br>
    1. Metadata for these papers is already saved in Zotero (tagged <code>needs-pdf</code>).<br>
    2. Click <em>Open Page</em>, complete institutional sign-in or verification in your browser, and download the PDF into: <code>{inbox_esc}</code>.<br>
    3. Click the button below to automatically attach downloaded PDFs to their existing Zotero items.
  </div>
  <p>
    <button class="btn secondary" onclick="scanInbox()">Scan Inbox &amp; Attach PDFs / 扫描下载目录并自动补挂</button>
  </p>
  <div id="scan-result"></div>
  <table>
    <thead>
      <tr><th>#</th><th>Title / DOI</th><th>Reason</th><th>Status</th><th>Action</th></tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
  <script>
    async function scanInbox() {{
      const el = document.getElementById('scan-result');
      el.textContent = 'Scanning inbox...';
      try {{
        const resp = await fetch('{scan_esc}?nonce={nonce_esc}', {{ method: 'POST' }});
        const data = await resp.json();
        el.textContent = 'Done: attached ' + (data.attached_count || 0) + ' PDF(s), remaining ' + (data.remaining_count || 0) + '. Reloading...';
        if ((data.attached_count || 0) > 0) setTimeout(() => location.reload(), 1200);
      }} catch (err) {{
        el.textContent = 'Scan failed: ' + err;
      }}
    }}
  </script>
</body>
</html>
"""
