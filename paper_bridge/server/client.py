"""Authenticated HTTP client for talking to the local `paper-bridge-helper` daemon."""

from __future__ import annotations

from typing import Any

from ..common.security import load_token
from ..common.transport import HTTPStatusError, TransportError, request_json
from .config import ServerConfig


class PaperBridgeClient:
    def __init__(self, config: ServerConfig, *, token: str | None = None) -> None:
        self.config = config
        self._token = token

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = load_token(self.config.token_path)
        return self._token

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return request_json(
            method,
            path,
            token=self.token,
            payload=payload,
            socket_path=self.config.socket_path,
            tcp_base_url=self.config.tcp_base_url,
            timeout=timeout or self.config.request_timeout_sec,
        )

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health", timeout=self.config.health_timeout_sec)

    def is_reachable(self) -> tuple[bool, dict[str, Any] | None, str]:
        try:
            data = self.health()
            return True, data, ""
        except HTTPStatusError as exc:
            return False, None, f"Authentication or HTTP error: {exc}"
        except (TransportError, FileNotFoundError, ValueError) as exc:
            return False, None, str(exc)

    def ingest(
        self,
        identifier: str,
        *,
        collection: str | None = None,
        tags: list[str] | None = None,
        mode: str = "single",
        wait_for_manual: bool = True,
        manual_timeout_sec: int = 300,
    ) -> dict[str, Any]:
        timeout = self.config.request_timeout_sec
        if wait_for_manual:
            timeout = max(timeout, float(manual_timeout_sec) + 60.0)
        return self._call(
            "POST",
            "/v1/ingest",
            payload={
                "identifier": identifier,
                "collection": collection,
                "tags": list(tags or []),
                "mode": mode,
                "wait_for_manual": wait_for_manual,
                "manual_timeout_sec": manual_timeout_sec,
            },
            timeout=timeout,
        )

    def submit_batch(
        self,
        items: list[str],
        *,
        collection: str | None = None,
        tags: list[str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"items": items, "collection": collection, "tags": list(tags or [])}
        if request_id is not None:
            payload["request_id"] = request_id
        return self._call(
            "POST",
            "/v1/batches",
            payload=payload,
            timeout=30.0,
        )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/batches/{batch_id}", timeout=15.0)

    def open_manual(self, batch_id: str) -> dict[str, Any]:
        return self._call("POST", f"/v1/batches/{batch_id}/open-manual", payload={}, timeout=15.0)

    def scan_inbox(self, batch_id: str | None = None) -> dict[str, Any]:
        return self._call(
            "POST",
            "/v1/inbox/scan",
            payload={"batch_id": batch_id},
            timeout=60.0,
        )
