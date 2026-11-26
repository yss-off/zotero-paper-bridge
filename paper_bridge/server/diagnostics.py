"""Read-only probes used by the server health command."""

from __future__ import annotations

import base64
import http.client
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..common.transport import UnixHTTPConnection
from .mcp_bridge import DEFAULT_ZOTERO_HEADERS, resolve_read_socket_path, resolve_read_url


def check_mcp_fulltext(item_key: str, *, timeout: float) -> dict[str, bool | int | str]:
    if not re.fullmatch(r"[A-Z0-9]{8}", item_key):
        raise ValueError("The full-text probe needs an eight-character Zotero item key")
    path = f"/api/users/0/items/{item_key}/fulltext"
    read_url = resolve_read_url()
    if read_url:
        endpoint = urlsplit(read_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("PAPER_BRIDGE_ZOTERO_READ_URL must be an HTTP(S) URL")
        connection_type = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(endpoint.hostname, endpoint.port, timeout=timeout)
        path = endpoint.path.rstrip("/") + path
    else:
        connection = UnixHTTPConnection(str(resolve_read_socket_path()), timeout=timeout)
    try:
        connection.request("GET", path, headers=DEFAULT_ZOTERO_HEADERS)
        response = connection.getresponse()
        readable = False
        if response.status == 200:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) <= 1024 * 1024:
                text = json.loads(raw).get("content")
                readable = isinstance(text, str) and bool(text.strip())
        return {"readable": readable, "http_status": response.status}
    except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
        return {"readable": False, "error": type(exc).__name__}
    finally:
        connection.close()


def check_webdav_file(
    url: str, *, username: str | None, password_file: Path | None, timeout: float
) -> dict[str, bool | int | str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.fragment:
        raise ValueError("Use an HTTP(S) WebDAV file URL without embedded credentials or a fragment")
    if (username is None) != (password_file is None):
        raise ValueError("Provide both the WebDAV username and password file")
    headers = {}
    if username is not None and password_file is not None:
        credential = f"{username}:{password_file.read_text(encoding='utf-8').strip()}"
        headers["Authorization"] = "Basic " + base64.b64encode(credential.encode()).decode("ascii")
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        readable = response.status == 200 and bool(response.read(1))
        return {"readable": readable, "http_status": response.status}
    except (OSError, http.client.HTTPException) as exc:
        return {"readable": False, "error": type(exc).__name__}
    finally:
        connection.close()
