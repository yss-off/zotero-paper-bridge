"""Tests for MCP bridge reconnect resilience, headers, backward-compat shims, and cross-platform notifications."""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import threading
from pathlib import Path

import pytest

from paper_bridge.client import config as client_config
from paper_bridge.client.handoff import show_desktop_notification
from paper_bridge.server.mcp_bridge import DEFAULT_ZOTERO_HEADERS, create_zotero_http_client
from paper_bridge.windows import cli as legacy_windows_cli


def test_backward_compat_windows_shim() -> None:
    parser = legacy_windows_cli.build_parser()
    args = parser.parse_args(["smoke-test"])
    assert args.cmd == "smoke-test"
    assert client_config.WindowsHelperConfig is client_config.ClientHelperConfig


def test_desktop_notification_dispatches_safely(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(cmd))
        return None

    monkeypatch.setattr("paper_bridge.client.handoff.subprocess.run", fake_run)
    monkeypatch.setattr("paper_bridge.client.handoff.os.name", "posix")
    monkeypatch.setattr("paper_bridge.client.handoff.sys.platform", "darwin")
    assert show_desktop_notification("Title", "Message") is True
    assert calls and calls[-1][0] == "osascript"


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="Unix Domain Socket test requires POSIX",
)
def test_mcp_bridge_client_headers_and_socket_reconnect(tmp_path: Path) -> None:
    pytest.importorskip("httpx")
    sock_path = tmp_path / "zotero-read.sock"
    seen_headers: list[str | None] = []

    def _start_unix_server(tag: str):
        if sock_path.exists():
            sock_path.unlink()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                seen_headers.append(self.headers.get("Zotero-API-Version"))
                body = f'{{"generation":"{tag}"}}'.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        srv = UnixServer(str(sock_path), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv

    srv1 = _start_unix_server("gen1")
    with create_zotero_http_client(socket_path=sock_path, timeout=5.0) as client:
        assert client.headers.get("Zotero-API-Version") == DEFAULT_ZOTERO_HEADERS["Zotero-API-Version"]
        r1 = client.get("http://localhost:23119/api/users/0/collections")
        assert r1.status_code == 200
        assert r1.json() == {"generation": "gen1"}

        # Simulate Windows SSH tunnel disconnect + stale socket removal + reconnect on new inode!
        srv1.shutdown()
        srv1.server_close()
        srv2 = _start_unix_server("gen2")
        try:
            r2 = client.get("http://localhost:23119/api/users/0/collections")
            assert r2.status_code == 200
            assert r2.json() == {"generation": "gen2"}
        finally:
            srv2.shutdown()
            srv2.server_close()

    assert seen_headers == ["3", "3"]
