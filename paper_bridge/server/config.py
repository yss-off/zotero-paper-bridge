"""Configuration loader for the `paper-bridge` CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


DEFAULT_SERVER_CONFIG_TOML = """\
# zotero-paper-bridge CLI configuration (local Unix socket mode)
# Private Unix socket served by `paper-bridge-helper` inside a chmod 700 directory
socket_path = "~/.local/run/paper-bridge/helper.sock"

# Optional loopback TCP fallback URL (leave empty to use the private Unix socket only)
# Bearer Token authentication is ALWAYS required on both Unix socket and TCP!
tcp_base_url = ""

# Default target collection in Zotero when --collection is omitted
default_collection = "Agent-Inbox"

# Default HTTP timeout for single requests
request_timeout_sec = 320.0
health_timeout_sec = 5.0
"""


def _parse_toml(text: str) -> dict[str, Any]:
    if tomllib is not None:
        return tomllib.loads(text)
    data: dict[str, Any] = {}
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        k, v = [p.strip() for p in cleaned.split("=", 1)]
        if v.startswith('"') and v.endswith('"'):
            data[k] = v[1:-1]
        elif v.lower() in {"true", "false"}:
            data[k] = v.lower() == "true"
        else:
            try:
                data[k] = float(v) if "." in v else int(v)
            except ValueError:
                data[k] = v
    return data


@dataclass
class ServerConfig:
    config_dir: Path
    data_dir: Path
    run_dir: Path
    config_path: Path
    token_path: Path
    queue_path: Path
    socket_path: Path
    tcp_base_url: str = ""
    default_collection: str = "Agent-Inbox"
    request_timeout_sec: float = 320.0
    health_timeout_sec: float = 5.0

    @classmethod
    def load(
        cls,
        *,
        config_dir: Path | None = None,
        data_dir: Path | None = None,
        run_dir: Path | None = None,
    ) -> "ServerConfig":
        c_dir = (
            config_dir
            or Path(os.environ.get("PAPER_BRIDGE_CONFIG_DIR") or "~/.config/paper-bridge")
        ).expanduser()
        d_dir = (
            data_dir
            or Path(os.environ.get("PAPER_BRIDGE_DATA_DIR") or "~/.local/share/zotero-paper-bridge")
        ).expanduser()
        r_dir = (
            run_dir
            or Path(os.environ.get("PAPER_BRIDGE_RUN_DIR") or "~/.local/run/paper-bridge")
        ).expanduser()

        cfg_path = c_dir / "config.toml"
        tok_path = c_dir / "token"
        q_path = d_dir / "queue.jsonl"
        raw: dict[str, Any] = {}
        if cfg_path.exists():
            raw = _parse_toml(cfg_path.read_text(encoding="utf-8"))

        sock_raw = str(raw.get("socket_path") or (r_dir / "helper.sock"))
        sock_path = Path(sock_raw).expanduser()
        return cls(
            config_dir=c_dir,
            data_dir=d_dir,
            run_dir=r_dir,
            config_path=cfg_path,
            token_path=tok_path,
            queue_path=q_path,
            socket_path=sock_path,
            tcp_base_url=(
                os.environ.get("PAPER_BRIDGE_TCP_URL") or str(raw.get("tcp_base_url", ""))
            ).rstrip("/"),
            default_collection=str(raw.get("default_collection", "Agent-Inbox")),
            request_timeout_sec=float(raw.get("request_timeout_sec", 320.0)),
            health_timeout_sec=float(raw.get("health_timeout_sec", 5.0)),
        )

    def ensure_initialized(self) -> None:
        for d in (self.config_dir, self.data_dir, self.run_dir, self.socket_path.parent):
            d.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(d, 0o700)
                except OSError:
                    pass
        if not self.config_path.exists():
            self.config_path.write_text(DEFAULT_SERVER_CONFIG_TOML, encoding="utf-8")
            if os.name == "posix":
                os.chmod(self.config_path, 0o600)
