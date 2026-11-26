"""Security utilities: Bearer token management, constant-time checks, and URL allowlist."""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import ssl
from pathlib import Path
from urllib.parse import urlparse

from .persistence import atomic_write_text

SYSTEM_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL / CentOS / Fedora
    "/etc/ssl/ca-bundle.pem",              # openSUSE
    "/etc/ssl/cert.pem",                   # Alpine / macOS / BSD
)
SYSTEM_CA_DIR_CANDIDATES = ("/etc/ssl/certs",)

ALLOWED_ACADEMIC_SUFFIXES = (
    "ieee.org",
    "arxiv.org",
    "doi.org",
    "crossref.org",
    "openalex.org",
    "unpaywall.org",
    "semanticscholar.org",
    "acm.org",
    "springer.com",
    "springeropen.com",
    "nature.com",
    "sciencedirect.com",
    "elsevier.com",
    "wiley.com",
    "tandfonline.com",
    "mdpi.com",
    "frontiersin.org",
    "oup.com",
    "cambridge.org",
    "plos.org",
    "biorxiv.org",
    "medrxiv.org",
    "openreview.net",
    "mlr.press",
    "neurips.cc",
    "thecvf.com",
    "aclanthology.org",
    "usenix.org",
    "optica.org",
    "aps.org",
    "aip.org",
    "iop.org",
    "nih.gov",
    "europepmc.org",
    "hal.science",
    "zenodo.org",
    "core.ac.uk",
    "pdf.sciencedirectassets.com",
    "cloudfront.net",
    "amazonaws.com",
)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def write_token(token_path: Path, token: str) -> None:
    atomic_write_text(token_path, token + "\n")
    if os.name == "posix":
        os.chmod(token_path, 0o600)


def ensure_token(token_path: Path) -> tuple[str, bool]:
    """Load existing token or create a new 256-bit token with strict permissions.

    Returns (token, created_now).
    """
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(token_path.parent, 0o700)
        except OSError:
            pass
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token, False
    token = generate_token()
    write_token(token_path, token)
    return token, True


def load_token(token_path: Path) -> str:
    if not token_path.exists():
        raise FileNotFoundError(f"Bearer token file not found: {token_path}")
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise ValueError(f"Bearer token in {token_path} is too short (<16 chars)")
    return token


def verify_bearer_header(auth_header: str | None, expected_token: str) -> bool:
    if not auth_header or not expected_token:
        return False
    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return hmac.compare_digest(parts[1].strip(), expected_token.strip())


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_safe_http_url(url: str, *, require_academic_host: bool = False) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127."):
        return False
    if not require_academic_host:
        return True
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_ACADEMIC_SUFFIXES)


def build_ssl_context() -> ssl.SSLContext:
    """Create a strictly verified SSLContext, falling back to OS CA bundles when Python lacks one.

    Relocated Conda/Anaconda Python builds on Linux can compile in a non-existent default CA path
    (leaving both cafile and capath as None). When that happens, locate standard OS trust stores
    without ever relaxing TLS certificate verification.
    """
    paths = ssl.get_default_verify_paths()
    if paths.cafile or paths.capath:
        return ssl.create_default_context()
    for candidate in SYSTEM_CA_BUNDLE_CANDIDATES:
        if os.path.isfile(candidate):
            return ssl.create_default_context(cafile=candidate)
    for candidate_dir in SYSTEM_CA_DIR_CANDIDATES:
        if os.path.isdir(candidate_dir):
            return ssl.create_default_context(capath=candidate_dir)
    return ssl.create_default_context()
