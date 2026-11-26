"""Tests for Mock Zotero 7 Local/Connector client and the read-only Zotero proxy."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from paper_bridge.client.zotero_local import ZoteroLocalClient
from paper_bridge.client.zotero_ro_proxy import start_readonly_proxy_thread
from paper_bridge.common.models import Author, PaperMetadata
from paper_bridge.common.transport import UnixHTTPConnection

SAMPLE_PDF = b"%PDF-1.4\n" + (b"0123456789" * 200) + b"\n%%EOF\n"


class MockZoteroState:
    def __init__(self, *, allow_cross_session_attach: bool = False) -> None:
        self.allow_cross_session_attach = allow_cross_session_attach
        self.collections = [
            {"key": "COL00001", "target_id": "C10", "name": "Agent-Inbox"},
            {"key": "COL00002", "target_id": "C22", "name": "TWC-Related-Work"},
        ]
        self.items: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[dict[str, Any]]] = {}
        self.sessions: dict[str, dict[str, str]] = {}  # sessionID -> {connector_id: item_key}
        self.session_targets: dict[str, str] = {}
        self.attachments_received: list[dict[str, Any]] = []

    def add_existing(
        self,
        *,
        key: str,
        title: str,
        doi: str | None = None,
        has_pdf: bool = False,
        extra: str = "",
    ) -> None:
        self.items[key] = {
            "key": key,
            "data": {
                "key": key,
                "itemType": "journalArticle",
                "title": title,
                "DOI": doi or "",
                "url": f"https://doi.org/{doi}" if doi else "",
                "extra": extra,
                "collections": ["COL00001"],
                "tags": [],
            },
        }
        if has_pdf:
            self.children[key] = [
                {
                    "key": f"{key}PDF",
                    "data": {
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "filename": "existing.pdf",
                    },
                }
            ]


def start_mock_zotero(state: MockZoteroState) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, data: Any) -> None:
            raw = json.dumps(data).encode("utf-8") if data is not None else b""
            self.send_response(code)
            if raw:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/users/0/collections":
                rows = [{"key": c["key"], "data": {"key": c["key"], "name": c["name"]}} for c in state.collections]
                self._send_json(200, rows)
                return
            if path in {"/api/users/0/items/top", "/api/users/0/items"}:
                q = (parse_qs(parsed.query).get("q") or [""])[0].lower()
                hits = []
                for item in state.items.values():
                    blob = json.dumps(item["data"]).lower()
                    if not q or q in blob:
                        hits.append(item)
                self._send_json(200, hits)
                return
            if path.startswith("/api/users/0/items/") and path.endswith("/children"):
                key = path.split("/api/users/0/items/", 1)[1].split("/children", 1)[0]
                self._send_json(200, state.children.get(key, []))
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/connector/") and self.headers.get("X-Zotero-Connector-API-Version") != "3":
                self._send_json(400, {"error": "missing connector version header"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length > 0 else b""

            if self.path == "/connector/ping":
                self._send_json(200, {})
                return

            if self.path == "/connector/getSelectedCollection":
                self._send_json(
                    200,
                    {
                        "libraryID": 1,
                        "libraryName": "My Library",
                        "targets": [
                            {"id": "L1", "name": "My Library", "level": 0},
                            *[
                                {"id": c["target_id"], "name": c["name"], "level": 1}
                                for c in state.collections
                            ],
                        ],
                    },
                )
                return

            if self.path == "/connector/saveItems":
                body = json.loads(raw.decode("utf-8"))
                sid = body["sessionID"]
                state.sessions.setdefault(sid, {})
                for item in body.get("items") or []:
                    key = f"Z{uuid.uuid4().hex[:7].upper()}"
                    cid = str(item.get("id") or key)
                    state.sessions[sid][cid] = key
                    state.items[key] = {
                        "key": key,
                        "data": {
                            "key": key,
                            "itemType": item.get("itemType", "journalArticle"),
                            "title": item.get("title", ""),
                            "DOI": item.get("DOI", ""),
                            "url": item.get("url", ""),
                            "extra": item.get("extra", ""),
                            "tags": item.get("tags", []),
                            "collections": [],
                        },
                    }
                self._send_json(201, None)
                return

            if self.path == "/connector/updateSession":
                body = json.loads(raw.decode("utf-8"))
                sid = body["sessionID"]
                target = body.get("target")
                if target:
                    state.session_targets[sid] = target
                    col_key = next((c["key"] for c in state.collections if c["target_id"] == target), None)
                    if col_key:
                        for item_key in state.sessions.get(sid, {}).values():
                            state.items[item_key]["data"]["collections"] = [col_key]
                self._send_json(200, None)
                return

            if self.path == "/connector/saveAttachment":
                meta = json.loads(self.headers.get("X-Metadata") or "{}")
                sid = str(meta.get("sessionID") or "")
                parent_id = str(meta.get("parentItemID") or "")
                item_key = state.sessions.get(sid, {}).get(parent_id)
                if item_key is None and state.allow_cross_session_attach and parent_id in state.items:
                    item_key = parent_id
                if item_key is None:
                    self._send_json(500, {"error": "unknown connector session/parentItemID"})
                    return
                state.children.setdefault(item_key, []).append(
                    {
                        "key": f"{item_key}A{len(state.children.get(item_key, []))}",
                        "data": {
                            "itemType": "attachment",
                            "contentType": "application/pdf",
                            "filename": meta.get("title", "doc.pdf"),
                        },
                    }
                )
                state.attachments_received.append({"item_key": item_key, "size": len(raw), "meta": meta})
                self._send_json(201, None)
                return

            self._send_json(404, {"error": "unknown_post"})

    class Server(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{port}"


def test_zotero_save_unicode_collection_and_attach_paths() -> None:
    state = MockZoteroState(allow_cross_session_attach=False)
    state.add_existing(key="OLDNO01", title="Existing No PDF Paper", doi="10.1109/OLD.1", has_pdf=False)
    state.add_existing(key="OLDPDF1", title="Existing With PDF Paper", doi="10.1109/OLD.2", has_pdf=True)

    srv, base_url = start_mock_zotero(state)
    try:
        client = ZoteroLocalClient(base_url)
        assert client.ping() is True
        assert client.local_api_ready() is True

        # 1. Existing dedup checks
        m_pdf = client.find_existing_item(doi="10.1109/old.2")
        assert m_pdf is not None and m_pdf.item_key == "OLDPDF1" and m_pdf.has_pdf is True
        m_nopdf = client.find_existing_item(doi="10.1109/old.1")
        assert m_nopdf is not None and m_nopdf.item_key == "OLDNO01" and m_nopdf.has_pdf is False

        # 2. Save new item with Unicode title into explicit existing collection
        meta = PaperMetadata(
            title="无线语义频谱感知 — Spectrum Segmentation",
            creators=[Author(first_name="Alice", last_name="Wang")],
            doi="10.1109/twc.2026.9999",
            ieee_arnumber="10999999",
            source="ieee",
        )
        saved = client.save_item(
            meta,
            pdf_bytes=SAMPLE_PDF,
            pdf_filename="无线语义频谱感知.pdf",
            collection_name="TWC-Related-Work",
            tags=["source:agent"],
        )
        assert saved["pdf_attached"] is True
        assert saved["collection"] == "TWC-Related-Work"
        assert saved["item_key"] is not None
        assert state.items[saved["item_key"]]["data"]["collections"] == ["COL00002"]

        # 3. Unknown collection falls back to Agent-Inbox + target:<name> tag
        meta2 = PaperMetadata(title="Metadata Only Batch Paper", doi="10.1109/twc.2026.8888", ieee_arnumber="10888888")
        saved2 = client.save_item(
            meta2,
            pdf_bytes=None,
            collection_name="NonExistentFolder",
            default_collection="Agent-Inbox",
            tags=["source:agent", "needs-pdf"],
        )
        assert saved2["collection"] == "Agent-Inbox"
        assert saved2["fallback_tag"] == "target:NonExistentFolder"
        assert saved2["item_key"] is not None

        # 4. In-session manual PDF attach reuses live Connector session with zero duplicates!
        attach_same = client.attach_pdf_to_existing(
            saved2["item_key"],
            SAMPLE_PDF,
            pdf_filename="10888888.pdf",
            pdf_url="https://ieeexplore.ieee.org/document/10888888",
            metadata=meta2,
            collection_name="Agent-Inbox",
        )
        assert attach_same["status"] == "attached"
        assert attach_same["item_key"] == saved2["item_key"]
        assert attach_same["reason"] == "attached_via_live_session"

        # 5. Cross-session attach on historical item -> falls back cleanly to saved_merge_needed
        meta_old = PaperMetadata(title="Existing No PDF Paper", doi="10.1109/old.1")
        attach_old = client.attach_pdf_to_existing(
            "OLDNO01",
            SAMPLE_PDF,
            pdf_filename="old.pdf",
            pdf_url="https://doi.org/10.1109/old.1",
            metadata=meta_old,
            collection_name="Agent-Inbox",
            tags=["source:agent"],
        )
        assert attach_old["status"] == "saved_merge_needed"
        assert attach_old["pdf_attached"] is True
        assert attach_old["existing_item_key"] == "OLDNO01"
        new_key = attach_old["item_key"]
        assert new_key is not None and new_key != "OLDNO01"
        tag_vals = [t["tag"] for t in state.items[new_key]["data"]["tags"]]
        assert "merge-duplicate" in tag_vals
    finally:
        srv.shutdown()
        srv.server_close()


def test_zotero_readonly_proxy_blocks_writes_and_connector() -> None:
    state = MockZoteroState()
    srv, base_url = start_mock_zotero(state)
    proxy_srv, _ = start_readonly_proxy_thread("127.0.0.1", 0, base_url)
    proxy_port = proxy_srv.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5.0)
        # Allowed: GET /api/*
        conn.request("GET", "/api/users/0/collections")
        r = conn.getresponse()
        body = json.loads(r.read().decode("utf-8"))
        assert r.status == 200
        assert len(body) == 2

        # Blocked: POST /connector/saveItems
        conn.request("POST", "/connector/saveItems", body=b"{}", headers={"Content-Type": "application/json"})
        r2 = conn.getresponse()
        r2.read()
        assert r2.status == 403

        # Blocked: GET /connector/ping
        conn.request("GET", "/connector/ping")
        r3 = conn.getresponse()
        r3.read()
        assert r3.status == 403

        # Blocked: POST /api/users/0/items
        conn.request("POST", "/api/users/0/items", body=b"[]")
        r4 = conn.getresponse()
        r4.read()
        assert r4.status == 403

        # Blocked: DELETE /api/users/0/items/ABC
        conn.request("DELETE", "/api/users/0/items/ABC")
        r5 = conn.getresponse()
        r5.read()
        assert r5.status == 403
        conn.close()
    finally:
        proxy_srv.shutdown()
        proxy_srv.server_close()
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="Unix Domain Socket test requires POSIX",
)
def test_zotero_readonly_proxy_unix_socket() -> None:
    state = MockZoteroState()
    srv, base_url = start_mock_zotero(state)
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="zpb-") as short_dir:
        sock_path = Path(short_dir) / "run" / "zotero-read.sock"
        proxy_srv, _ = start_readonly_proxy_thread("127.0.0.1", 0, base_url, socket_path=sock_path)
        try:
            assert stat.S_IMODE(sock_path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(sock_path.lstat().st_mode) == 0o600

            conn = UnixHTTPConnection(str(sock_path), timeout=5.0)
            conn.request("GET", "/api/users/0/collections")
            r = conn.getresponse()
            body = json.loads(r.read().decode("utf-8"))
            assert r.status == 200
            assert len(body) == 2

            conn.request("POST", "/connector/saveItems", body=b"{}", headers={"Content-Type": "application/json"})
            r2 = conn.getresponse()
            r2.read()
            assert r2.status == 403
            conn.close()
        finally:
            proxy_srv.shutdown()
            proxy_srv.server_close()
            srv.shutdown()
            srv.server_close()
        assert not sock_path.exists()
