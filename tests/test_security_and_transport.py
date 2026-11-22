"""Tests for Bearer token security, URL allowlist, and Unix socket + TCP transport."""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import PaperBridgeEngine
from paper_bridge.client.server import build_helper_http_server
from paper_bridge.common.security import ensure_token, is_safe_http_url, verify_bearer_header
from paper_bridge.common.transport import HTTPStatusError, request_json


def test_token_permissions_and_verification(tmp_path: Path) -> None:
    tok_path = tmp_path / "secure" / "token"
    token, created = ensure_token(tok_path)
    assert created is True
    assert len(token) >= 32
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(tok_path).st_mode)
        assert mode == 0o600

    token2, created2 = ensure_token(tok_path)
    assert created2 is False
    assert token2 == token

    assert verify_bearer_header(f"Bearer {token}", token) is True
    assert verify_bearer_header(f"bearer {token}", token) is True
    assert verify_bearer_header("Bearer wrong-token", token) is False
    assert verify_bearer_header(None, token) is False
    assert verify_bearer_header(token, token) is False


def test_url_allowlist() -> None:
    assert is_safe_http_url("https://ieeexplore.ieee.org/document/12345", require_academic_host=True)
    assert is_safe_http_url("https://arxiv.org/pdf/2401.12345.pdf", require_academic_host=True)
    assert not is_safe_http_url("https://evil.example.com/payload.pdf", require_academic_host=True)
    assert not is_safe_http_url("http://127.0.0.1:18765/health", require_academic_host=False)
    assert not is_safe_http_url("file:///etc/passwd", require_academic_host=False)
    assert not is_safe_http_url("javascript:alert(1)", require_academic_host=False)


def test_helper_http_server_auth_and_tcp_transport(tmp_path: Path) -> None:
    cfg = ClientHelperConfig.load(tmp_path / "client_cfg")
    cfg.zotero_base_url = "http://127.0.0.1:1"  # unreachable dummy
    cfg.ensure_initialized()
    token, _ = ensure_token(cfg.token_path)

    engine = PaperBridgeEngine(cfg)
    srv = build_helper_http_server(engine, token=token, host="127.0.0.1", port=0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base_url = f"http://127.0.0.1:{port}"
        # Wrong token -> 401
        with pytest.raises(HTTPStatusError) as exc_info:
            request_json("GET", "/health", token="bad-token-1234567890", tcp_base_url=base_url)
        assert exc_info.value.status == 401

        # Valid token -> 200
        data = request_json("GET", "/health", token=token, tcp_base_url=base_url)
        assert data["zotero_alive"] is False
        assert data["status"] == "degraded"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="Unix Domain Socket test requires POSIX",
)
def test_unix_domain_socket_transport() -> None:
    # Keep socket path short (<104 bytes) for macOS AF_UNIX sun_path limit
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="zpb-") as short_dir:
        sock_dir = Path(short_dir) / "r"
        sock_dir.mkdir(mode=0o700)
        sock_path = sock_dir / "h.sock"
        expected_token = "super-secret-token-for-unix-socket-test"

        class UnixHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if not verify_bearer_header(self.headers.get("Authorization"), expected_token):
                    body = b'{"error":"unauthorized"}'
                    self.send_response(401)
                else:
                    body = b'{"status":"ok","via":"unix_socket"}'
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        srv = UnixHTTPServer(str(sock_path), UnixHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            data = request_json("GET", "/health", token=expected_token, socket_path=sock_path)
            assert data == {"status": "ok", "via": "unix_socket"}

            with pytest.raises(HTTPStatusError) as exc_info:
                request_json("GET", "/health", token="wrong-token-value-1234", socket_path=sock_path)
            assert exc_info.value.status == 401
        finally:
            srv.shutdown()
            srv.server_close()
