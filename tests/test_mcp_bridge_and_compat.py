"""Tests for MCP bridge reconnect resilience, headers, local defaults, and desktop notifications."""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from paper_bridge.client import config as client_config
from paper_bridge.client.handoff import show_desktop_notification
from paper_bridge.server.mcp_bridge import (
    DEFAULT_ZOTERO_HEADERS,
    create_bridged_zotero_client,
    create_zotero_http_client,
    resolve_read_socket_path,
)


def test_client_config_defaults_to_private_unix_sockets_and_headless(tmp_path: Path) -> None:
    cfg = client_config.ClientHelperConfig.load(tmp_path / "cfg")
    run_dir = tmp_path / "home" / ".local" / "run" / "paper-bridge"
    assert cfg.listen_socket == run_dir / "helper.sock"
    assert cfg.zotero_ro_proxy_socket == run_dir / "zotero-read.sock"
    assert cfg.open_browser_on_single_manual is False


def test_client_inbox_defaults_to_isolated_home(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config = client_config.ClientHelperConfig(config_dir, config_dir / "token", config_dir / "config.toml")
    expected_inbox = tmp_path / "home" / "Downloads" / "paper-bridge-inbox"
    assert config.inbox_dir == expected_inbox
    assert client_config.ClientHelperConfig.load(config_dir).inbox_dir == expected_inbox


def test_mcp_read_socket_defaults_to_isolated_home(tmp_path: Path) -> None:
    assert resolve_read_socket_path() == tmp_path / "home" / ".local/run/paper-bridge/zotero-read.sock"


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
def test_mcp_bridge_client_headers_and_socket_reconnect() -> None:
    pytest.importorskip("httpx")
    # Keep socket path short (<104 bytes) for macOS AF_UNIX sun_path limit
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="zpb-") as short_dir:
        sock_path = Path(short_dir) / "z.sock"
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

            # Simulate helper restart + stale socket removal + reconnect on new inode!
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


def test_create_bridged_zotero_client_uses_independent_http_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []

    class FakeHttpClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeZotero:
        def __init__(self, library_id: str, library_type: str, *, local: bool, client: FakeHttpClient) -> None:
            self.library_id = library_id
            self.library_type = library_type
            self.local = local
            self.client = client

        def __del__(self) -> None:
            self.client.close()

    def fake_create_http(**_kwargs: object) -> FakeHttpClient:
        c = FakeHttpClient()
        created.append(c)
        return c

    import sys
    import types

    fake_zotero_mod = types.SimpleNamespace(Zotero=FakeZotero)
    fake_pkg = types.SimpleNamespace(zotero=fake_zotero_mod)
    monkeypatch.setitem(sys.modules, "pyzotero", fake_pkg)
    monkeypatch.setitem(sys.modules, "pyzotero.zotero", fake_zotero_mod)
    monkeypatch.setattr("paper_bridge.server.mcp_bridge.create_zotero_http_client", fake_create_http)

    z1 = create_bridged_zotero_client()
    z2 = create_bridged_zotero_client()
    assert z1.client is not z2.client
    z1.client.close()
    assert z1.client.closed is True
    assert z2.client.closed is False
    assert len(created) == 2
