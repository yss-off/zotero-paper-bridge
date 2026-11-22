"""HTTP server for the Windows desktop helper (Bearer-authenticated loopback only)."""

from __future__ import annotations

import http.server
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..common.models import IngestRequest
from ..common.security import verify_bearer_header
from .engine import PaperBridgeEngine


def build_helper_http_server(
    engine: PaperBridgeEngine,
    *,
    token: str,
    host: str | None = None,
    port: int | None = None,
) -> http.server.ThreadingHTTPServer:
    listen_host = host or engine.config.listen_host
    listen_port = port if port is not None else engine.config.listen_port

    class HelperRequestHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, payload: dict[str, Any], *, close: bool = False) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if close:
                self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, code: int, html_body: str) -> None:
            raw = html_body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                return {}
            if length > 2 * 1024 * 1024:
                raise ValueError("Request body too large")
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON payload must be an object")
            return data

        def _require_auth(self) -> bool:
            if not verify_bearer_header(self.headers.get("Authorization"), token):
                self._send_json(
                    401,
                    {"error": "unauthorized", "message": "Missing or invalid Bearer token"},
                    close=True,
                )
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            # Local browser checklist page (authorized via per-batch nonce, never exposes master token)
            if path.startswith("/manual/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 2:
                    batch_id = parts[1]
                    nonce = (parse_qs(parsed.query).get("nonce") or [""])[0]
                    code, body = engine.render_manual_page(batch_id, nonce)
                    self._send_html(code, body)
                    return

            if not self._require_auth():
                return

            if path == "/health":
                self._send_json(200, engine.health())
                return

            if path.startswith("/v1/batches/"):
                batch_id = path.split("/v1/batches/", 1)[1].strip("/")
                data = engine.get_batch(batch_id)
                if data is None:
                    self._send_json(404, {"error": "not_found", "batch_id": batch_id})
                    return
                self._send_json(200, data)
                return

            self._send_json(404, {"error": "not_found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            # Local browser checklist scan button (authorized via per-batch nonce)
            if path.startswith("/manual/") and path.endswith("/scan"):
                parts = [p for p in path.split("/") if p]
                if len(parts) == 3:
                    batch_id = parts[1]
                    nonce = (parse_qs(parsed.query).get("nonce") or [""])[0]
                    if not engine.verify_batch_nonce(batch_id, nonce):
                        self._send_json(403, {"error": "invalid_nonce"})
                        return
                    self._send_json(200, engine.scan_inbox_and_attach(batch_id=batch_id))
                    return

            if not self._require_auth():
                return

            try:
                body = self._read_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": "bad_request", "message": str(exc)})
                return

            if path == "/v1/ingest":
                req = IngestRequest.from_dict(body)
                result, _ = engine.ingest_one(req)
                self._send_json(200, result.to_dict())
                return

            if path == "/v1/batches":
                items = body.get("items") or []
                if not isinstance(items, list) or not items:
                    self._send_json(400, {"error": "bad_request", "message": "items must be a non-empty list"})
                    return
                identifiers = [str(x).strip() for x in items if str(x).strip()]
                delay_raw = body.get("delay_range_sec")
                delay_range = None
                if isinstance(delay_raw, list) and len(delay_raw) == 2:
                    delay_range = (float(delay_raw[0]), float(delay_raw[1]))
                res = engine.submit_batch(
                    identifiers,
                    collection=body.get("collection") or None,
                    tags=[str(t).strip() for t in (body.get("tags") or []) if str(t).strip()],
                    delay_range_sec=delay_range,
                )
                self._send_json(202, res)
                return

            if path.startswith("/v1/batches/") and path.endswith("/open-manual"):
                batch_id = path[len("/v1/batches/") : -len("/open-manual")].strip("/")
                res = engine.open_manual_checklist(batch_id)
                self._send_json(200 if res.get("ok") else 404, res)
                return

            if path == "/v1/inbox/scan":
                batch_id = body.get("batch_id") or None
                self._send_json(200, engine.scan_inbox_and_attach(batch_id=batch_id))
                return

            self._send_json(404, {"error": "not_found", "path": path})

    class ReusableThreadingServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    return ReusableThreadingServer((listen_host, listen_port), HelperRequestHandler)
