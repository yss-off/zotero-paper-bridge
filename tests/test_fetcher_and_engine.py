"""Tests for PDF classification, tiered routing, single browser handoff, serial batching, and circuit breaker."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from test_zotero_and_proxy import SAMPLE_PDF, MockZoteroState, start_mock_zotero

from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import PaperBridgeEngine
from paper_bridge.client.fetcher import FetchOutcome, classify_non_pdf_response, is_valid_pdf
from paper_bridge.client.zotero_local import ZoteroLocalClient
from paper_bridge.common.models import IngestRequest, PaperMetadata, ParsedIdentifier


def test_pdf_validation_and_gate_classification() -> None:
    assert is_valid_pdf(SAMPLE_PDF) is True
    assert is_valid_pdf(b"<html><body>Sign in</body></html>") is False
    assert is_valid_pdf(b"%PDF-1.4 short") is False

    assert classify_non_pdf_response(429, "https://ieeexplore.ieee.org/...", b"Too Many Requests") == (
        "rate_limited",
        True,
    )
    assert classify_non_pdf_response(
        403, "https://ieeexplore.ieee.org/...", b"<html>Please verify you are human - Cloudflare</html>"
    ) == ("captcha_or_challenge", True)
    assert classify_non_pdf_response(
        200, "https://idp.university.edu/shibboleth/login", b"<html>University Login</html>"
    ) == ("auth_required", False)
    assert classify_non_pdf_response(
        403, "https://ieeexplore.ieee.org/...", b"<html>Purchase PDF</html>"
    ) == ("paywall_or_forbidden", False)


class FakeResolver:
    def resolve(self, parsed: ParsedIdentifier) -> PaperMetadata:
        if parsed.arxiv_id:
            return PaperMetadata(
                title=f"ArXiv Paper {parsed.arxiv_id}",
                item_type="preprint",
                arxiv_id=parsed.arxiv_id,
                doi=f"10.48550/arxiv.{parsed.arxiv_id}",
                url=f"https://arxiv.org/abs/{parsed.arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{parsed.arxiv_id}.pdf",
                source="arxiv",
            )
        if parsed.ieee_arnumber:
            return PaperMetadata(
                title=f"IEEE Paper {parsed.ieee_arnumber}",
                item_type="journalArticle",
                doi=f"10.1109/twc.2026.{parsed.ieee_arnumber}",
                ieee_arnumber=parsed.ieee_arnumber,
                url=f"https://ieeexplore.ieee.org/document/{parsed.ieee_arnumber}",
                source="ieee",
            )
        return PaperMetadata(title=parsed.raw, doi=parsed.doi, url=parsed.url, source="crossref")


class FakeFetcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def fetch(self, meta: PaperMetadata, *, skip_silent_ieee: bool = False) -> FetchOutcome:
        self.calls.append((meta.ieee_arnumber or meta.arxiv_id or meta.title, skip_silent_ieee))
        if meta.arxiv_id:
            return FetchOutcome(
                ok=True,
                pdf_bytes=SAMPLE_PDF,
                pdf_url=meta.pdf_url or "",
                filename=f"{meta.arxiv_id}.pdf",
                source="arxiv",
            )
        if meta.ieee_arnumber:
            if skip_silent_ieee:
                return FetchOutcome(
                    ok=False,
                    needs_manual=True,
                    reason="ieee_circuit_breaker_active",
                    manual_url=meta.canonical_landing_url(),
                    source="ieee",
                )
            return FetchOutcome(
                ok=False,
                needs_manual=True,
                reason="captcha_or_challenge",
                manual_url=meta.canonical_landing_url(),
                source="ieee",
                is_rate_or_challenge=True,
            )
        return FetchOutcome(ok=False, needs_manual=True, reason="publisher_browser_required", manual_url=meta.canonical_landing_url())


def test_single_browser_handoff_detects_inbox_pdf_drop(tmp_path: Path) -> None:
    state = MockZoteroState()
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path / "cfg")
        cfg.zotero_base_url = base_url
        cfg.inbox_dir = tmp_path / "inbox"
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.single_manual_timeout_sec = 5

        opened_urls: list[str] = []
        toasts: list[tuple[str, str]] = []

        def fake_open(url: str) -> bool:
            opened_urls.append(url)
            # Simulate user saving the PDF from IEEE in browser 0.1s later
            def drop_later() -> None:
                time.sleep(0.1)
                (cfg.inbox_dir / "10123456.pdf").write_bytes(SAMPLE_PDF)

            threading.Thread(target=drop_later, daemon=True).start()
            return True

        engine = PaperBridgeEngine(
            cfg,
            zotero=ZoteroLocalClient(base_url),
            resolver=FakeResolver(),  # type: ignore[arg-type]
            fetcher=FakeFetcher(),  # type: ignore[arg-type]
            browser_opener=fake_open,
            notifier=lambda t, m: toasts.append((t, m)) or True,
            sleep_fn=lambda _: time.sleep(0.05),
        )
        res, _ = engine.ingest_one(
            IngestRequest(
                identifier="https://ieeexplore.ieee.org/document/10123456",
                collection="TWC-Related-Work",
                mode="single",
                wait_for_manual=True,
                manual_timeout_sec=5,
            )
        )
        assert res.status == "saved"
        assert res.pdf_attached is True
        assert res.source == "browser_download"
        assert opened_urls == ["https://ieeexplore.ieee.org/document/10123456"]
        assert len(toasts) == 1
        # Original file was archived into inbox/imported/
        assert not (cfg.inbox_dir / "10123456.pdf").exists()
        assert (cfg.inbox_dir / "imported" / "10123456.pdf").exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_batch_circuit_breaker_and_inbox_scan_attach(tmp_path: Path) -> None:
    state = MockZoteroState()
    state.add_existing(key="DUP0001", title="Already Here", doi="10.48550/arxiv.2401.00001", has_pdf=True)
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path / "cfg")
        cfg.zotero_base_url = base_url
        cfg.inbox_dir = tmp_path / "inbox"
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.batch_delay_min_sec = 0.0
        cfg.batch_delay_max_sec = 0.0
        cfg.ieee_circuit_breaker_threshold = 2

        opened: list[str] = []
        fetcher = FakeFetcher()
        engine = PaperBridgeEngine(
            cfg,
            zotero=ZoteroLocalClient(base_url),
            resolver=FakeResolver(),  # type: ignore[arg-type]
            fetcher=fetcher,  # type: ignore[arg-type]
            browser_opener=lambda u: opened.append(u) or True,
            notifier=lambda t, m: True,
            sleep_fn=lambda _: None,
        )

        submitted = engine.submit_batch(
            [
                "2401.00001",  # already exists with PDF
                "2401.00002",  # arXiv silent download
                "https://ieeexplore.ieee.org/document/10000001",  # IEEE challenge #1
                "https://ieeexplore.ieee.org/document/10000002",  # IEEE challenge #2 -> trips circuit breaker
                "https://ieeexplore.ieee.org/document/10000003",  # skipped silent IEEE fetch!
            ],
            collection="TWC-Related-Work",
            delay_range_sec=(0.0, 0.0),
        )
        batch_id = submitted["batch_id"]
        for _ in range(50):
            st = engine.get_batch(batch_id)
            if st and st["state"] in {"completed", "completed_with_manual"}:
                break
            time.sleep(0.05)

        final = engine.get_batch(batch_id)
        assert final is not None
        assert final["state"] == "completed_with_manual"
        assert final["counts"] == {"already_exists": 1, "saved": 1, "needs_manual": 3}
        # Circuit breaker engaged for the 3rd IEEE paper
        assert fetcher.calls[-1] == ("10000003", True)
        # Checklist opened ONCE at the end of batch (not mid-batch!)
        assert len(opened) == 1
        assert f"/manual/{batch_id}?nonce=" in opened[0]

        # Now user manually downloads 10000001.pdf into inbox_dir and triggers scan
        (cfg.inbox_dir / "10000001.pdf").write_bytes(SAMPLE_PDF)
        scan = engine.scan_inbox_and_attach(batch_id=batch_id)
        assert scan["attached_count"] == 1
        assert scan["remaining_count"] == 2
        assert scan["attached"][0]["status"] == "attached"  # in-session attach, zero duplicates!
    finally:
        srv.shutdown()
        srv.server_close()
