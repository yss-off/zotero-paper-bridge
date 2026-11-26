"""Configuration loader for the local Zotero 7 helper daemon."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG_TOML = """\
# zotero-paper-bridge local helper configuration (server-local Unix socket mode)
# Private POSIX Unix Domain Sockets inside a chmod 700 directory (recommended)
listen_socket = "~/.local/run/paper-bridge/helper.sock"
zotero_ro_proxy_socket = "~/.local/run/paper-bridge/zotero-read.sock"

# Optional loopback TCP settings (used only when listen_socket / zotero_ro_proxy_socket is set to "")
listen_host = "127.0.0.1"
listen_port = 18765
zotero_ro_proxy_port = 23120

# Local Zotero 7 desktop server (adjust if Zotero uses a custom extensions.zotero.httpServer.port)
zotero_base_url = "http://127.0.0.1:23119"

# Read-only proxy for zotero-mcp (allows GET/HEAD /api/* only; blocks /connector/* and writes)
enable_zotero_ro_proxy = true

# Default Zotero collection when no target collection is requested or found
default_collection = "Agent-Inbox"

# Folder watched for manually downloaded PDFs when a publisher requires manual login/verification
# Leave empty to default to ~/Downloads/paper-bridge-inbox
inbox_dir = ""

# Polite serial delay (seconds) between remote fetches during batch runs
batch_delay_min_sec = 3.0
batch_delay_max_sec = 6.0

# Stop silent IEEE fetching for remaining batch items after N consecutive rate-limit/challenge hits
ieee_circuit_breaker_threshold = 2

# Headless/24-7 server default: save metadata with `needs-pdf` instead of blocking on a browser window
open_browser_on_single_manual = false
single_manual_timeout_sec = 300

# Optional email for Crossref/Unpaywall polite pool (recommended, e.g. your academic email)
polite_email = ""
"""


def _parse_simple_toml(text: str) -> dict[str, Any]:
    if tomllib is not None:
        return tomllib.loads(text)
    data: dict[str, Any] = {}
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, raw_val = [p.strip() for p in cleaned.split("=", 1)]
        if raw_val.startswith('"') and raw_val.endswith('"'):
            data[key] = raw_val[1:-1]
        elif raw_val.lower() in {"true", "false"}:
            data[key] = raw_val.lower() == "true"
        else:
            try:
                data[key] = float(raw_val) if "." in raw_val else int(raw_val)
            except ValueError:
                data[key] = raw_val
    return data


def _default_inbox_dir() -> Path:
    return Path.home() / "Downloads" / "paper-bridge-inbox"


def _default_run_dir() -> Path:
    return Path(os.environ.get("PAPER_BRIDGE_RUN_DIR") or "~/.local/run/paper-bridge").expanduser()


def _default_helper_socket() -> Path:
    return _default_run_dir() / "helper.sock"


def _default_ro_proxy_socket() -> Path:
    return _default_run_dir() / "zotero-read.sock"


@dataclass
class ClientHelperConfig:
    config_dir: Path
    token_path: Path
    config_path: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 18765
    listen_socket: Path | None = field(default_factory=_default_helper_socket)
    zotero_base_url: str = "http://127.0.0.1:23119"
    enable_zotero_ro_proxy: bool = True
    zotero_ro_proxy_port: int = 23120
    zotero_ro_proxy_socket: Path | None = field(default_factory=_default_ro_proxy_socket)
    default_collection: str = "Agent-Inbox"
    inbox_dir: Path = field(default_factory=_default_inbox_dir)
    batch_delay_min_sec: float = 3.0
    batch_delay_max_sec: float = 6.0
    ieee_circuit_breaker_threshold: int = 2
    open_browser_on_single_manual: bool = False
    single_manual_timeout_sec: int = 300
    polite_email: str = ""

    @classmethod
    def default_config_dir(cls) -> Path:
        env_dir = os.environ.get("PAPER_BRIDGE_CLIENT_CONFIG_DIR")
        if env_dir:
            return Path(env_dir).expanduser()
        return Path.home() / ".paper-bridge"

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "ClientHelperConfig":
        base = (config_dir or cls.default_config_dir()).expanduser()
        cfg_path = base / "config.toml"
        tok_path = base / "token"
        raw: dict[str, Any] = {}
        if cfg_path.exists():
            raw = _parse_simple_toml(cfg_path.read_text(encoding="utf-8"))

        inbox_raw = str(raw.get("inbox_dir") or "").strip()
        inbox_dir = Path(inbox_raw).expanduser() if inbox_raw else _default_inbox_dir()

        if "listen_socket" in raw:
            listen_sock_raw = str(raw.get("listen_socket") or "").strip()
            listen_socket = Path(listen_sock_raw).expanduser() if listen_sock_raw else None
        else:
            listen_socket = _default_helper_socket()

        if "zotero_ro_proxy_socket" in raw:
            ro_sock_raw = str(raw.get("zotero_ro_proxy_socket") or "").strip()
            ro_proxy_socket = Path(ro_sock_raw).expanduser() if ro_sock_raw else None
        else:
            ro_proxy_socket = _default_ro_proxy_socket()

        return cls(
            config_dir=base,
            token_path=tok_path,
            config_path=cfg_path,
            listen_host=str(raw.get("listen_host", "127.0.0.1")),
            listen_port=int(raw.get("listen_port", 18765)),
            listen_socket=listen_socket,
            zotero_base_url=str(raw.get("zotero_base_url", "http://127.0.0.1:23119")).rstrip("/"),
            enable_zotero_ro_proxy=bool(raw.get("enable_zotero_ro_proxy", True)),
            zotero_ro_proxy_port=int(raw.get("zotero_ro_proxy_port", 23120)),
            zotero_ro_proxy_socket=ro_proxy_socket,
            default_collection=str(raw.get("default_collection", "Agent-Inbox")),
            inbox_dir=inbox_dir,
            batch_delay_min_sec=float(raw.get("batch_delay_min_sec", 3.0)),
            batch_delay_max_sec=float(raw.get("batch_delay_max_sec", 6.0)),
            ieee_circuit_breaker_threshold=int(raw.get("ieee_circuit_breaker_threshold", 2)),
            open_browser_on_single_manual=bool(raw.get("open_browser_on_single_manual", False)),
            single_manual_timeout_sec=int(raw.get("single_manual_timeout_sec", 300)),
            polite_email=str(raw.get("polite_email", "")).strip(),
        )

    def ensure_initialized(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self.config_dir, 0o700)
            except OSError:
                pass
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
            if os.name == "posix":
                try:
                    os.chmod(self.config_path, 0o600)
                except OSError:
                    pass
