"""Zero-dependency HTTP client supporting both Unix Domain Sockets and TCP."""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def prepare_unix_socket_path(socket_path: Path) -> Path:
    """Create a private 0o700 parent directory and remove any stale socket at socket_path."""
    if os.name != "posix" or not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("Unix Domain Sockets are only supported on POSIX systems")
    path = socket_path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    try:
        st = path.lstat()
    except FileNotFoundError:
        return path
    if not stat.S_ISSOCK(st.st_mode):
        raise ValueError(f"Refusing to overwrite non-socket path: {path}")
    path.unlink()
    return path


def cleanup_unix_socket_path(socket_path: Path, *, expected_inode: int | None = None) -> None:
    """Remove a bound Unix socket file only if it still matches this server's inode."""
    try:
        st = socket_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        return
    if expected_inode is not None and st.st_ino != expected_inode:
        return
    try:
        socket_path.unlink()
    except FileNotFoundError:
        return


class TransportError(RuntimeError):
    """Raised when communication with the helper fails."""


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
        try:
            sock.settimeout(self.timeout)
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
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
                # Fall back only while we know no HTTP request has been sent.
                try:
                    conn.connect()
                except (OSError, http.client.HTTPException) as exc:
                    errors.append(f"{kind}:{target} -> {exc}")
                    continue
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
        except (OSError, http.client.HTTPException) as exc:
            errors.append(f"{kind}:{target} -> {exc}")
            raise TransportError("; ".join(errors)) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    raise TransportError("; ".join(errors))
