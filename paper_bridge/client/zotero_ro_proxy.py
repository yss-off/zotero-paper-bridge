"""Read-only HTTP proxy in front of Zotero Desktop (port 23119).

Allows server-side `zotero-mcp` (GET/HEAD `/api/*`) while blocking all mutating
methods and `/connector/*` write endpoints when port 23119 is forwarded on a
multi-user Ubuntu server.
"""

from __future__ import annotations

import http.client
import http.server
import threading
from urllib.parse import urlparse


def build_readonly_proxy_server(
    listen_host: str,
    listen_port: int,
    zotero_base_url: str = "http://127.0.0.1:23119",
) -> http.server.ThreadingHTTPServer:
    parsed = urlparse(zotero_base_url)
    upstream_host = parsed.hostname or "127.0.0.1"
    upstream_port = parsed.port or 23119

    class ZoteroReadOnlyProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _reject(self, code: int, msg: str) -> None:
            # Close connection on rejected requests so unconsumed POST/PUT bodies never desync HTTP/1.1 keep-alive
            self.close_connection = True
            body = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _forward_readonly(self, method: str) -> None:
            if not self.path.startswith("/api/"):
                self._reject(403, "Forbidden: read-only Zotero proxy allows GET/HEAD /api/* only.\n")
                return
            conn = http.client.HTTPConnection(upstream_host, upstream_port, timeout=30.0)
            try:
                fwd_headers = {}
                for k in ("Accept", "Zotero-API-Version", "If-Modified-Since-Version", "User-Agent"):
                    val = self.headers.get(k)
                    if val:
                        fwd_headers[k] = val
                conn.request(method, self.path, headers=fwd_headers)
                resp = conn.getresponse()
                data = resp.read()
                self.send_response(resp.status, resp.reason)
                hop_by_hop = {"connection", "keep-alive", "transfer-encoding", "proxy-authenticate", "upgrade"}
                for hk, hv in resp.getheaders():
                    if hk.lower() in hop_by_hop or hk.lower() == "content-length":
                        continue
                    self.send_header(hk, hv)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data)
            except (OSError, http.client.HTTPException) as exc:
                self._reject(502, f"Bad Gateway: cannot reach local Zotero ({exc})\n")
            finally:
                conn.close()

        def do_GET(self) -> None:  # noqa: N802
            self._forward_readonly("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._forward_readonly("HEAD")

        def do_POST(self) -> None:  # noqa: N802
            self._reject(403, "Forbidden: POST is blocked on the read-only Zotero proxy.\n")

        def do_PUT(self) -> None:  # noqa: N802
            self._reject(403, "Forbidden: PUT is blocked on the read-only Zotero proxy.\n")

        def do_DELETE(self) -> None:  # noqa: N802
            self._reject(403, "Forbidden: DELETE is blocked on the read-only Zotero proxy.\n")

        def do_PATCH(self) -> None:  # noqa: N802
            self._reject(403, "Forbidden: PATCH is blocked on the read-only Zotero proxy.\n")

    class ReusableThreadingServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    return ReusableThreadingServer((listen_host, listen_port), ZoteroReadOnlyProxyHandler)


def start_readonly_proxy_thread(
    listen_host: str,
    listen_port: int,
    zotero_base_url: str = "http://127.0.0.1:23119",
) -> tuple[http.server.ThreadingHTTPServer, threading.Thread]:
    server = build_readonly_proxy_server(listen_host, listen_port, zotero_base_url)
    thread = threading.Thread(target=server.serve_forever, name="zotero-ro-proxy", daemon=True)
    thread.start()
    return server, thread
