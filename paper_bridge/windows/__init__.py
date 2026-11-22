"""Backward-compatible shim re-exporting `paper_bridge.client`."""

from ..client import (  # noqa: F401
    cli,
    config,
    engine,
    fetcher,
    handoff,
    metadata,
    pdf_meta,
    server,
    zotero_local,
    zotero_ro_proxy,
)
