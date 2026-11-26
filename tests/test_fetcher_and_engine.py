"""Tests for PDF classification, tiered routing, single browser handoff, serial batching, and circuit breaker."""

from __future__ import annotations

import http.cookiejar
import os
import time
import urllib.request
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock
from urllib.response import addinfourl

import pytest
from test_zotero_and_proxy import SAMPLE_PDF, MockZoteroState, start_mock_zotero

from paper_bridge.client import engine as engine_module
from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import PaperBridgeEngine
from paper_bridge.client.fetcher import (
    FetchOutcome,
    TieredPaperFetcher,
    classify_non_pdf_response,
    is_valid_pdf,
)
from paper_bridge.client.handoff import PendingManualPaper, find_matching_pdf_in_inbox
from paper_bridge.client.zotero_local import ZoteroError, ZoteroLocalClient
from paper_bridge.common.models import IngestRequest, PaperMetadata, ParsedIdentifier


@pytest.mark.parametrize(
    ("target", "allowed"),
    [
        ("file:///tmp/paper.pdf", False),
        ("ftp://arxiv.org/paper.pdf", False),
        ("http://127.0.0.1/paper.pdf", False),
        ("http://169.254.169.254/paper.pdf", False),
        ("http://192.168.1.1/paper.pdf", False),
        ("https://openreview.net/pdf?id=paper", True),
    ],
)
@pytest.mark.parametrize("use_cookies", [False, True])
def test_download_validates_redirect_chain(monkeypatch, target: str, allowed: bool, use_cookies: bool) -> None:
    start = "https://arxiv.org/start"
    intermediate = "https://arxiv.org/intermediate"
    opened = []

    def open_url(self, req):
        url = req.full_url
        opened.append(url)
        headers = Message()
        if url in {start, intermediate}:
            headers["Location"] = "/intermediate" if url == start else target
            response = addinfourl(BytesIO(b""), headers, url, 302)
            response.msg = "Found"
        else:
            headers["Content-Type"] = "application/pdf"
            response = addinfourl(BytesIO(SAMPLE_PDF), headers, url, 200)
            response.msg = "OK"
        return response

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", open_url)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", open_url)
    monkeypatch.setattr(urllib.request.FTPHandler, "ftp_open", open_url)
    monkeypatch.setattr(urllib.request.FileHandler, "file_open", open_url)

    jar = http.cookiejar.CookieJar() if use_cookies else None
    status, body, final_url, _ = TieredPaperFetcher()._download(start, cookie_jar=jar)

    if allowed:
        assert (status, body, final_url) == (200, SAMPLE_PDF, target)
        assert opened == [start, intermediate, target]
    else:
        assert status != 200
        assert opened == [start, intermediate]


def test_manual_pdf_matching_checks_declared_doi_and_ambiguity(tmp_path: Path) -> None:
    pending = PendingManualPaper(
        "manual-1", "10000001", PaperMetadata(title="An example", doi="10.1109/example.2026", ieee_arnumber="10000001"),
        None, [], None, "", "",
    )

    def pdf(doi):
        return SAMPLE_PDF + f"<x:xmpmeta><prism:doi>{doi}</prism:doi></x:xmpmeta>".encode()

    (tmp_path / "10000001.pdf").write_bytes(pdf("10.1109/different.2026"))
    assert find_matching_pdf_in_inbox(tmp_path, pending) is None
    original = tmp_path / "download.PDF"
    original.write_bytes(pdf("10.1109/example.2026"))
    assert find_matching_pdf_in_inbox(tmp_path, pending) == original
    (tmp_path / "download-again.pdf").write_bytes(pdf("10.1109/example.2026") + b"\n")
    assert find_matching_pdf_in_inbox(tmp_path, pending) is None


@pytest.mark.parametrize(
    ("filename", "matches"),
    [("09123456.pdf", True), ("19123456.pdf", False), ("91234560.pdf", False)],
)
def test_inbox_matches_zero_padded_ieee_arnumber(tmp_path: Path, filename: str, matches: bool) -> None:
    pending = PendingManualPaper(
        "manual-1", "9123456", PaperMetadata(title="An example", ieee_arnumber="9123456"),
        None, [], None, "", "",
    )
    downloaded = tmp_path / filename
    downloaded.write_bytes(SAMPLE_PDF)

    assert find_matching_pdf_in_inbox(tmp_path, pending) == (downloaded if matches else None)


def test_inbox_resolves_ieee_ties_only_for_identical_downloads(tmp_path: Path) -> None:
    pending = PendingManualPaper(
        "manual-1", "9123456", PaperMetadata(title="An example", ieee_arnumber="9123456"),
        None, [], None, "", "",
    )
    original = tmp_path / "09123456.pdf"
    duplicate = tmp_path / "09123456 (1).pdf"
    original.write_bytes(SAMPLE_PDF)
    duplicate.write_bytes(SAMPLE_PDF)
    os.utime(original, (1, 1))
    os.utime(duplicate, (2, 2))

    assert find_matching_pdf_in_inbox(tmp_path, pending) == duplicate

    duplicate.write_bytes(SAMPLE_PDF.replace(b"0123456789", b"9876543210", 1))
    assert find_matching_pdf_in_inbox(tmp_path, pending) is None


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


def test_single_browser_handoff_detects_inbox_pdf_drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = MockZoteroState()
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path / "cfg")
        cfg.zotero_base_url = base_url
        cfg.listen_socket = None
        cfg.open_browser_on_single_manual = True
        cfg.inbox_dir = tmp_path / "inbox"
        cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        cfg.single_manual_timeout_sec = 5

        opened_urls: list[str] = []
        toasts: list[tuple[str, str]] = []

        def fake_open(url: str) -> bool:
            opened_urls.append(url)
            return True

        def drop_pdf_during_sleep(_seconds: float) -> None:
            assert not engine._inbox_lock.locked()
            (cfg.inbox_dir / "10123456.pdf").write_bytes(SAMPLE_PDF)

        engine = PaperBridgeEngine(
            cfg,
            zotero=ZoteroLocalClient(base_url),
            resolver=FakeResolver(),  # type: ignore[arg-type]
            fetcher=FakeFetcher(),  # type: ignore[arg-type]
            browser_opener=fake_open,
            notifier=lambda t, m: toasts.append((t, m)) or True,
            sleep_fn=drop_pdf_during_sleep,
        )

        def match_pdf(*args, **kwargs):
            assert engine._inbox_lock.locked()
            return find_matching_pdf_in_inbox(*args, **kwargs)

        archive_pdf = engine._archive_inbox_file

        def archive_with_lock(path: Path) -> None:
            assert engine._inbox_lock.locked()
            archive_pdf(path)

        monkeypatch.setattr(engine_module, "find_matching_pdf_in_inbox", match_pdf)
        monkeypatch.setattr(engine, "_archive_inbox_file", archive_with_lock)
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


def test_browser_handoff_finds_connector_pdf_behind_metadata_duplicate(tmp_path: Path) -> None:
    state = MockZoteroState()
    title = "IEEE Paper 10123456"
    doi = "10.1109/twc.2026.10123456"
    state.add_existing(key="METADATA", title=title, doi=doi, has_pdf=False)
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path)
        cfg.listen_socket = None
        cfg.open_browser_on_single_manual = True
        cfg.inbox_dir = tmp_path / "inbox"

        def open_browser(_url: str) -> bool:
            state.add_existing(key="NEWPDF", title=title, doi=doi, has_pdf=True)
            return True

        engine = PaperBridgeEngine(
            cfg,
            zotero=ZoteroLocalClient(base_url),
            resolver=FakeResolver(),  # type: ignore[arg-type]
            fetcher=FakeFetcher(),  # type: ignore[arg-type]
            browser_opener=open_browser,
            notifier=lambda *_: True,
        )
        res, _ = engine.ingest_one(IngestRequest(identifier=doi, manual_timeout_sec=1))

        assert res.status == "saved"
        assert res.source == "browser_connector"
        assert res.item_key == "NEWPDF"
        assert res.pdf_attached is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_inbox_scan_continues_past_file_and_zotero_errors(tmp_path, monkeypatch) -> None:
    cfg = ClientHelperConfig.load(tmp_path)
    cfg.inbox_dir = tmp_path / "inbox"
    cfg.inbox_dir.mkdir()
    zotero = Mock()
    zotero.attach_pdf_to_existing.side_effect = ZoteroError("cannot attach PDF")
    zotero.save_item.side_effect = [ZoteroError("cannot save item"), {"item_key": "SAVED", "pdf_attached": True}]
    engine = PaperBridgeEngine(cfg, zotero=zotero)
    for index in range(1, 5):
        identifier = f"2401.{index:05d}"
        task = PendingManualPaper(
            task_id=f"task-{index}", identifier=identifier,
            metadata=PaperMetadata(title=identifier, arxiv_id=identifier),
            collection=None, tags=[], item_key="EXISTING" if index == 2 else None, manual_url="", reason="",
        )
        engine._pending_manual[task.task_id] = task
        (cfg.inbox_dir / f"{identifier}.pdf").write_bytes(SAMPLE_PDF)

    read_bytes = Path.read_bytes

    def read_pdf(path: Path) -> bytes:
        if path.name == "2401.00001.pdf":
            raise OSError("cannot read PDF")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_pdf)
    data = engine.scan_inbox_and_attach()

    assert data["attached_count"] == 1
    assert data["attached"][0]["task_id"] == "task-4"
    assert data["remaining_count"] == 3
    assert [(error["task_id"], error["error"]) for error in data["errors"]] == [
        ("task-1", "cannot read PDF"), ("task-2", "cannot attach PDF"), ("task-3", "cannot save item"),
    ]
    assert len(list(cfg.inbox_dir.glob("*.pdf"))) == 3


def test_batch_circuit_breaker_and_inbox_scan_attach(tmp_path: Path) -> None:
    state = MockZoteroState()
    state.add_existing(key="DUP0001", title="Already Here", doi="10.48550/arxiv.2401.00001", has_pdf=True)
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path / "cfg")
        cfg.zotero_base_url = base_url
        cfg.listen_socket = None
        cfg.open_browser_on_single_manual = True
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
        restored = PaperBridgeEngine(cfg)
        assert restored.get_batch(batch_id)["counts"] == {
            "already_exists": 1, "saved": 1, "attached": 1, "needs_manual": 2,
        }
    finally:
        srv.shutdown()
        srv.server_close()


def test_unix_socket_mode_disables_browser_checklist_url(tmp_path: Path) -> None:
    state = MockZoteroState()
    srv, base_url = start_mock_zotero(state)
    try:
        cfg = ClientHelperConfig.load(tmp_path / "cfg")
        cfg.zotero_base_url = base_url
        cfg.listen_socket = tmp_path / "helper.sock"
        cfg.open_browser_on_single_manual = False
        cfg.batch_delay_min_sec = 0.0
        cfg.batch_delay_max_sec = 0.0

        opened: list[str] = []
        engine = PaperBridgeEngine(
            cfg,
            zotero=ZoteroLocalClient(base_url),
            resolver=FakeResolver(),  # type: ignore[arg-type]
            fetcher=FakeFetcher(),  # type: ignore[arg-type]
            browser_opener=lambda u: opened.append(u) or True,
            notifier=lambda t, m: True,
            sleep_fn=lambda _: None,
        )
        submitted = engine.submit_batch(
            ["https://ieeexplore.ieee.org/document/10000099"],
            collection="TWC-Related-Work",
            delay_range_sec=(0.0, 0.0),
        )
        batch_id = submitted["batch_id"]
        for _ in range(50):
            st = engine.get_batch(batch_id)
            if st and st["state"] == "completed_with_manual":
                break
            time.sleep(0.05)

        final = engine.get_batch(batch_id)
        assert final is not None
        assert final["state"] == "completed_with_manual"
        assert final["manual_checklist_url"] is None
        assert opened == []
        res = engine.open_manual_checklist(batch_id)
        assert res["ok"] is False
        assert res["reason"] == "unix_socket_mode_no_browser_checklist"
    finally:
        srv.shutdown()
        srv.server_close()
