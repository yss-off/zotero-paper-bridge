"""Tests for Bearer token security, URL allowlist, and Unix socket + TCP transport."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import socketserver
import ssl
import stat
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import PaperBridgeEngine
from paper_bridge.client.server import build_helper_http_server
from paper_bridge.client.zotero_local import ZoteroError
from paper_bridge.client.zotero_ro_proxy import build_readonly_proxy_server
from paper_bridge.common import security, transport
from paper_bridge.common.security import build_ssl_context, ensure_token, is_safe_http_url, verify_bearer_header
from paper_bridge.common.transport import HTTPStatusError, TransportError, request_json
from paper_bridge.server import client as server_client
from paper_bridge.server.config import ServerConfig


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


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5"])
def test_http_servers_reject_non_loopback_bindings(tmp_path: Path, host: str) -> None:
    cfg = ClientHelperConfig.load(tmp_path)
    cfg.listen_host = host
    engine = PaperBridgeEngine(cfg)

    with pytest.raises(ValueError, match="listen_host must be a loopback host"):
        with build_helper_http_server(engine, token="test-token-for-helper", port=0):
            pass
    with pytest.raises(ValueError, match="listen_host must be a loopback host"):
        with build_readonly_proxy_server(host, 0):
            pass


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


@pytest.mark.parametrize(
    ("method", "path", "payload", "engine_method", "failure", "status", "error"),
    [
        ("POST", "/v1/ingest", {"manual_timeout_sec": "invalid"}, None, None, 400, "bad_request"),
        ("GET", "/health", None, "health", ZoteroError("Zotero unavailable"), 500, "internal_error"),
        ("POST", "/v1/ingest", {"identifier": "2401.12345"}, "ingest_one", OSError("write failed"), 500,
         "internal_error"),
    ],
)
def test_helper_http_handler_errors(
    tmp_path, monkeypatch, method, path, payload, engine_method, failure, status, error
) -> None:
    engine = PaperBridgeEngine(ClientHelperConfig.load(tmp_path))
    if engine_method:
        def fail(*args, **kwargs):
            raise failure

        monkeypatch.setattr(engine, engine_method, fail)

    token = "test-token-for-helper"
    srv = build_helper_http_server(engine, token=token, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(HTTPStatusError) as exc_info:
            request_json(
                method, path, token=token, tcp_base_url=f"http://127.0.0.1:{srv.server_port}", payload=payload,
            )
        assert exc_info.value.status == status
        body = json.loads(exc_info.value.body)
        assert body["error"] == error
        assert body["message"]
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


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="Unix Domain Socket test requires POSIX",
)
def test_stale_unix_socket_falls_back_to_tcp(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock(status=200)
    response.read.return_value = b'{"status":"ok"}'
    tcp = Mock()
    tcp.return_value.getresponse.return_value = response
    monkeypatch.setattr(transport.http.client, "HTTPConnection", tcp)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="zpb-") as short_dir:
        sock_path = Path(short_dir) / "stale.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(str(sock_path))
        assert sock_path.is_socket()

        data = request_json(
            "POST", "/v1/inbox/scan", token="test-token", payload={},
            socket_path=sock_path, tcp_base_url="http://127.0.0.1:18765",
        )

    assert data == {"status": "ok"}
    tcp.return_value.request.assert_called_once()


@pytest.mark.parametrize("error", [TimeoutError("response timed out"), http.client.RemoteDisconnected()])
def test_post_response_failure_does_not_fall_back_to_tcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError,
) -> None:
    sock_path = tmp_path / "helper.sock"
    sock_path.touch()
    unix = Mock(spec=transport.UnixHTTPConnection)
    unix.getresponse.side_effect = error
    response = Mock(status=200)
    response.read.return_value = b'{"status":"ok"}'
    tcp = Mock()
    tcp.return_value.getresponse.return_value = response
    monkeypatch.setattr(transport, "UnixHTTPConnection", Mock(return_value=unix))
    monkeypatch.setattr(transport.http.client, "HTTPConnection", tcp)

    with pytest.raises(TransportError) as exc_info:
        request_json(
            "POST", "/v1/ingest", token="test-token", payload={"identifier": "2401.12345"},
            socket_path=sock_path, tcp_base_url="http://127.0.0.1:18765",
        )

    assert exc_info.value.__cause__ is error
    unix.connect.assert_called_once_with()
    unix.request.assert_called_once()
    tcp.assert_not_called()


@pytest.mark.parametrize(
    ("wait_for_manual", "manual_timeout", "configured_timeout", "expected_timeout"),
    [(True, 600, 320.0, 660.0), (True, 300, 320.0, 360.0), (True, 600, 900.0, 900.0), (False, 600, 320.0, 320.0)],
)
def test_ingest_http_timeout(
    monkeypatch: pytest.MonkeyPatch,
    wait_for_manual: bool,
    manual_timeout: int,
    configured_timeout: float,
    expected_timeout: float,
) -> None:
    config = ServerConfig.load()
    config.request_timeout_sec = configured_timeout
    request = Mock(return_value={"status": "saved"})
    monkeypatch.setattr(server_client, "request_json", request)
    client = server_client.PaperBridgeClient(config, token="test-token")

    client.ingest("2401.12345", wait_for_manual=wait_for_manual, manual_timeout_sec=manual_timeout)

    assert request.call_args.kwargs["timeout"] == expected_timeout


def test_build_ssl_context_falls_back_to_system_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    empty_paths = ssl.DefaultVerifyPaths(None, None, "SSL_CERT_FILE", "/missing/cert.pem", "SSL_CERT_DIR", "/missing")
    created_calls: list[dict[str, object]] = []
    sentinel = object()

    def fake_create_default_context(**kwargs: object) -> object:
        created_calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(security.ssl, "get_default_verify_paths", lambda: empty_paths)
    monkeypatch.setattr(security.ssl, "create_default_context", fake_create_default_context)
    monkeypatch.setattr(security.os.path, "isfile", lambda p: p == "/etc/ssl/certs/ca-certificates.crt")

    assert build_ssl_context() is sentinel
    assert created_calls == [{"cafile": "/etc/ssl/certs/ca-certificates.crt"}]


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(socket, "AF_UNIX"),
    reason="Unix Domain Socket test requires POSIX",
)
def test_helper_http_server_unix_socket_auth_permissions_and_cleanup(tmp_path: Path) -> None:
    cfg = ClientHelperConfig.load(tmp_path / "client_cfg")
    cfg.zotero_base_url = "http://127.0.0.1:1"
    cfg.ensure_initialized()
    token, _ = ensure_token(cfg.token_path)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="zpb-") as short_dir:
        sock_dir = Path(short_dir) / "run"
        sock_path = sock_dir / "helper.sock"

        # Refuse to overwrite an existing non-socket file
        sock_dir.mkdir(mode=0o700)
        sock_path.write_text("not a socket", encoding="utf-8")
        with pytest.raises(ValueError, match="Refusing to overwrite non-socket path"):
            build_helper_http_server(PaperBridgeEngine(cfg), token=token, socket_path=sock_path)
        sock_path.unlink()

        engine = PaperBridgeEngine(cfg)
        srv = build_helper_http_server(engine, token=token, socket_path=sock_path)
        assert stat.S_IMODE(sock_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(sock_path.lstat().st_mode) == 0o600
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with pytest.raises(HTTPStatusError) as exc_info:
                request_json("GET", "/health", token="wrong-token-1234567890", socket_path=sock_path)
            assert exc_info.value.status == 401

            data = request_json("GET", "/health", token=token, socket_path=sock_path)
            assert data["status"] == "degraded"
            assert data["zotero_alive"] is False
        finally:
            srv.shutdown()
            srv.server_close()

        assert not sock_path.exists()
