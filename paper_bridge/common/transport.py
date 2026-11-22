"""Zero-dependency HTTP client supporting both Unix Domain Sockets and TCP."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class TransportError(RuntimeError):
    """Raised when the helper endpoint cannot be reached."""


class HTTPStatusError(RuntimeError):
    """Raised when the helper responds with an HTTP error status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over a POSIX Unix Domain Socket."""

    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def request_json(
    method: str,
    path: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    socket_path: Path | None = None,
    tcp_base_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Send an authenticated JSON request over Unix socket (preferred) or TCP fallback."""
    errors: list[str] = []

    targets: list[tuple[str, str]] = []
    if socket_path is not None and socket_path.exists():
        targets.append(("unix", str(socket_path)))
    if tcp_base_url:
        targets.append(("tcp", tcp_base_url))

    if not targets:
        raise TransportError(
            f"No reachable transport (socket missing: {socket_path}, tcp_base_url={tcp_base_url})"
        )

    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"

    for kind, target in targets:
        conn: http.client.HTTPConnection | None = None
        try:
            if kind == "unix":
                conn = UnixHTTPConnection(target, timeout=timeout)
                req_path = path
            else:
                parsed = urlparse(target)
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
                prefix = parsed.path.rstrip("/")
                req_path = f"{prefix}{path}"

            conn.request(method.upper(), req_path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status >= 400:
                raise HTTPStatusError(resp.status, raw)
            if not raw.strip():
                return {}
            return json.loads(raw)
        except HTTPStatusError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            errors.append(f"{kind}:{target} -> {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    raise TransportError("; ".join(errors))
