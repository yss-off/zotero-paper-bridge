"""Run `zotero-mcp` over the local private read-only Unix Domain Socket.

Handles three subtle issues when running `pyzotero` over a local UDS proxy:
1. Explicitly supplies `Zotero-API-Version: 3` (which `pyzotero` skips when given a custom `httpx.Client`).
2. Disables persistent HTTP keep-alive pooling (`max_keepalive_connections=0`) so restarting
   `paper-bridge-helper` and recreating `zotero-read.sock` never hits stale pooled sockets.
3. Creates an independent `httpx.Client` per `get_zotero_client()` call because `pyzotero`
   closes `self.client` inside `Zotero.__del__`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_ZOTERO_HEADERS = {
    "Zotero-API-Version": "3",
    "User-Agent": "zotero-paper-bridge/0.1",
}


def resolve_read_socket_path() -> Path:
    raw = os.environ.get("PAPER_BRIDGE_ZOTERO_READ_SOCKET")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".local" / "run" / "paper-bridge" / "zotero-read.sock"


def resolve_read_url() -> str:
    return os.environ.get("PAPER_BRIDGE_ZOTERO_READ_URL", "").strip()


def create_zotero_http_client(
    *,
    socket_path: Path | None = None,
    timeout: float = 30.0,
) -> Any:
    """Create an `httpx.Client` bound to the private Zotero read-only Unix socket."""
    import httpx

    target_url = resolve_read_url()
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=16)

    if target_url:
        return httpx.Client(
            base_url=target_url,
            headers=dict(DEFAULT_ZOTERO_HEADERS),
            limits=limits,
            follow_redirects=True,
            timeout=timeout,
        )

    sock = socket_path or resolve_read_socket_path()
    transport = httpx.HTTPTransport(uds=str(sock))
    return httpx.Client(
        transport=transport,
        headers=dict(DEFAULT_ZOTERO_HEADERS),
        limits=limits,
        follow_redirects=True,
        timeout=timeout,
    )


def create_bridged_zotero_client(
    *,
    socket_path: Path | None = None,
    timeout: float = 30.0,
) -> Any:
    """Create a fresh PyZotero client backed by its own per-call httpx.Client.

    PyZotero's `Zotero.__del__` closes `self.client` when each instance goes out of
    scope, so each `get_zotero_client()` call must receive a dedicated `httpx.Client`
    rather than sharing one client across calls.
    """
    from pyzotero import zotero

    http_client = create_zotero_http_client(socket_path=socket_path, timeout=timeout)
    return zotero.Zotero("0", "user", local=True, client=http_client)


def main() -> int:
    try:
        import zotero_mcp
        import zotero_mcp.client
        from pyzotero import zotero  # noqa: F401
        from zotero_mcp.cli import main as zotero_mcp_main
    except ImportError as exc:
        print(
            "zotero-mcp-paper-bridge requires optional MCP dependencies (`zotero-mcp`, `pyzotero`, `httpx`).\n"
            "Install with: pip install 'zotero-paper-bridge[mcp]'  (or `uv tool install zotero-mcp`).\n"
            f"Import error: {exc}",
            file=sys.stderr,
        )
        return 1

    zotero_mcp.get_zotero_client = create_bridged_zotero_client
    zotero_mcp.client.get_zotero_client = create_bridged_zotero_client
    zotero_mcp_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
