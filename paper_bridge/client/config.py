"""Configuration loader for the local desktop helper (Windows, macOS, and Linux)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


DEFAULT_CONFIG_TOML = """\
# zotero-paper-bridge local client helper configuration
listen_host = "127.0.0.1"
listen_port = 18765

# Local Zotero 7 desktop server
zotero_base_url = "http://127.0.0.1:23119"

# Read-only proxy for server zotero-mcp (blocks POST/PUT/DELETE and /connector/*)
enable_zotero_ro_proxy = true
zotero_ro_proxy_port = 23120

# Default Zotero collection when no target collection is requested or found
default_collection = "Agent-Inbox"

# Folder watched for manually downloaded PDFs during browser handoff
# Leave empty to default to ~/Downloads/paper-bridge-inbox
inbox_dir = ""

# Polite serial delay (seconds) between remote fetches during batch runs
batch_delay_min_sec = 3.0
batch_delay_max_sec = 6.0

# Stop silent IEEE fetching for remaining batch items after N consecutive rate-limit/challenge hits
ieee_circuit_breaker_threshold = 2

# Open default local browser immediately when a single-item request needs login/verification
open_browser_on_single_manual = true
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


@dataclass
class ClientHelperConfig:
    config_dir: Path
    token_path: Path
    config_path: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 18765
    zotero_base_url: str = "http://127.0.0.1:23119"
    enable_zotero_ro_proxy: bool = True
    zotero_ro_proxy_port: int = 23120
    default_collection: str = "Agent-Inbox"
    inbox_dir: Path = Path.home() / "Downloads" / "paper-bridge-inbox"
    batch_delay_min_sec: float = 3.0
    batch_delay_max_sec: float = 6.0
    ieee_circuit_breaker_threshold: int = 2
    open_browser_on_single_manual: bool = True
    single_manual_timeout_sec: int = 300
    polite_email: str = ""

    @classmethod
    def default_config_dir(cls) -> Path:
        env_dir = os.environ.get("PAPER_BRIDGE_CLIENT_CONFIG_DIR") or os.environ.get(
            "PAPER_BRIDGE_WIN_CONFIG_DIR"
        )
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
        inbox_dir = (
            Path(inbox_raw).expanduser()
            if inbox_raw
            else Path.home() / "Downloads" / "paper-bridge-inbox"
        )
        return cls(
            config_dir=base,
            token_path=tok_path,
            config_path=cfg_path,
            listen_host=str(raw.get("listen_host", "127.0.0.1")),
            listen_port=int(raw.get("listen_port", 18765)),
            zotero_base_url=str(raw.get("zotero_base_url", "http://127.0.0.1:23119")).rstrip("/"),
            enable_zotero_ro_proxy=bool(raw.get("enable_zotero_ro_proxy", True)),
            zotero_ro_proxy_port=int(raw.get("zotero_ro_proxy_port", 23120)),
            default_collection=str(raw.get("default_collection", "Agent-Inbox")),
            inbox_dir=inbox_dir,
            batch_delay_min_sec=float(raw.get("batch_delay_min_sec", 3.0)),
            batch_delay_max_sec=float(raw.get("batch_delay_max_sec", 6.0)),
            ieee_circuit_breaker_threshold=int(raw.get("ieee_circuit_breaker_threshold", 2)),
            open_browser_on_single_manual=bool(raw.get("open_browser_on_single_manual", True)),
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


# Backward-compatible alias for older Windows-only imports
WindowsHelperConfig = ClientHelperConfig
