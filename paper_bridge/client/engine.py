"""Core Windows ingestion engine: dedup, metadata, tiered fetch, serial batching, manual handoff."""

from __future__ import annotations

import random
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..common.identifiers import parse_identifier
from ..common.models import IngestRequest, IngestResult, PaperMetadata
from .config import ClientHelperConfig
from .fetcher import FetchOutcome, TieredPaperFetcher, sanitize_filename
from .handoff import (
    PendingManualPaper,
    find_matching_pdf_in_inbox,
    open_in_browser,
    render_manual_checklist_html,
    show_desktop_notification,
)
from .metadata import AmbiguousTitleError, MetadataResolver
from .pdf_meta import extract_pdf_metadata
from .zotero_local import ZoteroError, ZoteroLocalClient


@dataclass
class BatchRecord:
    batch_id: str
    state: str = "queued"  # queued | running | completed | completed_with_manual
    collection: str | None = None
    tags: list[str] = field(default_factory=list)
    total: int = 0
    processed: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    manual_items: list[PendingManualPaper] = field(default_factory=list)
    local_nonce: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def summary_dict(self, *, helper_port: int) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for r in self.results:
            st = str(r.get("status") or "unknown")
            counts[st] = counts.get(st, 0) + 1
        pending_manual = [m for m in self.manual_items if not m.completed]
        checklist_url = (
            f"http://127.0.0.1:{helper_port}/manual/{self.batch_id}?nonce={self.local_nonce}"
            if self.manual_items
            else None
        )
        return {
            "batch_id": self.batch_id,
            "state": self.state,
            "collection": self.collection,
            "total": self.total,
            "processed": self.processed,
            "counts": counts,
            "pending_manual_count": len(pending_manual),
            "manual_checklist_url": checklist_url,
            "results": self.results,
        }


class PaperBridgeEngine:
    def __init__(
        self,
        config: ClientHelperConfig,
        *,
        zotero: ZoteroLocalClient | None = None,
        resolver: MetadataResolver | None = None,
        fetcher: TieredPaperFetcher | None = None,
        browser_opener: Callable[[str], bool] = open_in_browser,
        notifier: Callable[[str, str], bool] = show_desktop_notification,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.zotero = zotero or ZoteroLocalClient(config.zotero_base_url)
        self.resolver = resolver or MetadataResolver(polite_email=config.polite_email)
        self.fetcher = fetcher or TieredPaperFetcher(polite_email=config.polite_email)
        self.browser_opener = browser_opener
        self.notifier = notifier
        self.sleep_fn = sleep_fn

        self._lock = threading.Lock()
        self._batches: dict[str, BatchRecord] = {}
        self._pending_manual: dict[str, PendingManualPaper] = {}
        self._processed_inbox_files: set[str] = set()

    def health(self) -> dict[str, Any]:
        z_alive = self.zotero.ping()
        local_api = self.zotero.local_api_ready() if z_alive else False
        collections = self.zotero.list_collections() if z_alive else []
        return {
            "status": "ok" if z_alive else "degraded",
            "zotero_alive": z_alive,
            "zotero_local_api_ready": local_api,
            "default_collection": self.config.default_collection,
            "inbox_dir": str(self.config.inbox_dir),
            "collections": [
                {
                    "name": c.name,
                    "connector_target_id": c.connector_target_id,
                    "api_key": c.api_key,
                }
                for c in collections
            ],
            "pending_manual_count": sum(1 for p in self._pending_manual.values() if not p.completed),
        }

    def ingest_one(
        self,
        req: IngestRequest,
        *,
        skip_silent_ieee: bool = False,
        batch_id: str | None = None,
    ) -> tuple[IngestResult, bool]:
        """Ingest one paper. Returns (IngestResult, hit_rate_or_challenge)."""
        if not req.identifier:
            return IngestResult(identifier="", status="failed", reason="empty_identifier"), False

        if not self.zotero.ping():
            return (
                IngestResult(
                    identifier=req.identifier,
                    status="failed",
                    reason="zotero_not_running",
                ),
                False,
            )

        parsed = parse_identifier(req.identifier)

        # Step 0: Fast pre-dedup by DOI / arXiv / IEEE arnumber before hitting metadata APIs
        existing = self.zotero.find_existing_item(
            doi=parsed.doi,
            arxiv_id=parsed.arxiv_id,
            ieee_arnumber=parsed.ieee_arnumber,
            title=parsed.title,
        )
        if existing and existing.has_pdf:
            return (
                IngestResult(
                    identifier=req.identifier,
                    status="already_exists",
                    title=existing.title,
                    doi=existing.doi or parsed.doi,
                    arxiv_id=parsed.arxiv_id,
                    ieee_arnumber=parsed.ieee_arnumber,
                    item_key=existing.item_key,
                    existing_item_key=existing.item_key,
                    pdf_attached=True,
                    source="zotero_existing",
                    reason=f"matched_by_{existing.match_reason}",
                ),
                False,
            )

        # Step 1: Resolve canonical metadata
        try:
            meta = self.resolver.resolve(parsed)
        except AmbiguousTitleError as exc:
            return (
                IngestResult(
                    identifier=req.identifier,
                    status="ambiguous",
                    title=req.identifier,
                    reason="title_match_ambiguous",
                    candidates=exc.candidates,
                ),
                False,
            )

        # Step 0b: Second dedup check with enriched DOI / IEEE arnumber / canonical title
        if existing is None:
            existing = self.zotero.find_existing_item(
                doi=meta.doi,
                arxiv_id=meta.arxiv_id,
                ieee_arnumber=meta.ieee_arnumber,
                title=meta.title,
            )
            if existing and existing.has_pdf:
                return (
                    IngestResult(
                        identifier=req.identifier,
                        status="already_exists",
                        title=existing.title or meta.title,
                        doi=existing.doi or meta.doi,
                        arxiv_id=meta.arxiv_id,
                        ieee_arnumber=meta.ieee_arnumber,
                        item_key=existing.item_key,
                        existing_item_key=existing.item_key,
                        pdf_attached=True,
                        source="zotero_existing",
                        reason=f"matched_by_{existing.match_reason}",
                    ),
                    False,
                )

        # Step 2: Tiered PDF acquisition
        outcome = self.fetcher.fetch(meta, skip_silent_ieee=skip_silent_ieee)
        tags = self._build_tags(req.tags, source=outcome.source or meta.source)

        # Step 3a: Silent PDF fetch succeeded!
        if outcome.ok and outcome.pdf_bytes:
            meta, outcome, existing = self._enrich_metadata_from_pdf(meta, outcome, existing)
            if existing is not None and existing.has_pdf:
                return (
                    IngestResult(
                        identifier=req.identifier,
                        status="already_exists",
                        title=existing.title or meta.title,
                        doi=existing.doi or meta.doi,
                        arxiv_id=meta.arxiv_id,
                        ieee_arnumber=meta.ieee_arnumber,
                        item_key=existing.item_key,
                        existing_item_key=existing.item_key,
                        pdf_attached=True,
                        source="zotero_existing",
                        reason=f"matched_after_pdf_doi_{existing.match_reason}",
                    ),
                    False,
                )
            return self._commit_pdf_to_zotero(
                req=req,
                meta=meta,
                outcome=outcome,
                existing=existing,
                tags=tags,
            ), False

        # Step 3b: Needs browser handoff (login / CAPTCHA / paywall / circuit breaker)
        manual_url = outcome.manual_url or meta.canonical_landing_url()
        if req.mode == "single" and req.wait_for_manual and self.config.open_browser_on_single_manual:
            result = self._run_single_browser_handoff(
                req=req,
                meta=meta,
                existing=existing,
                manual_url=manual_url,
                reason=outcome.reason,
                tags=tags,
            )
            return result, outcome.is_rate_or_challenge

        # Batch mode (or non-waiting single mode):
        # Save metadata-only item with `needs-pdf` tag (if not already present) and register pending handoff
        item_key = existing.item_key if existing else None
        saved_collection = req.collection or self.config.default_collection
        if existing is None:
            meta_tags = list(tags)
            if "needs-pdf" not in meta_tags:
                meta_tags.append("needs-pdf")
            try:
                saved = self.zotero.save_item(
                    meta,
                    pdf_bytes=None,
                    collection_name=req.collection,
                    default_collection=self.config.default_collection,
                    tags=meta_tags,
                )
                item_key = saved.get("item_key")
                saved_collection = saved.get("collection") or saved_collection
            except ZoteroError as exc:
                return (
                    IngestResult(
                        identifier=req.identifier,
                        status="failed",
                        title=meta.title,
                        doi=meta.doi,
                        manual_url=manual_url,
                        reason=f"zotero_save_failed: {exc}",
                    ),
                    outcome.is_rate_or_challenge,
                )

        pending = PendingManualPaper(
            task_id=f"man-{uuid.uuid4().hex[:10]}",
            identifier=req.identifier,
            metadata=meta,
            collection=req.collection,
            tags=tags,
            item_key=item_key,
            manual_url=manual_url,
            reason=outcome.reason,
            batch_id=batch_id,
        )
        with self._lock:
            self._pending_manual[pending.task_id] = pending
            if batch_id and batch_id in self._batches:
                self._batches[batch_id].manual_items.append(pending)

        return (
            IngestResult(
                identifier=req.identifier,
                status="needs_manual",
                title=meta.title,
                doi=meta.doi,
                arxiv_id=meta.arxiv_id,
                ieee_arnumber=meta.ieee_arnumber,
                item_key=item_key,
                existing_item_key=existing.item_key if existing else None,
                zotero_collection=saved_collection,
                pdf_attached=False,
                source=outcome.source or meta.source,
                manual_url=manual_url,
                reason=outcome.reason,
            ),
            outcome.is_rate_or_challenge,
        )

    def _is_metadata_incomplete(self, meta: PaperMetadata) -> bool:
        if not meta.doi or not meta.creators:
            return True
        if meta.title.startswith(("IEEE Document ", "arXiv:", "http://", "https://")):
            return True
        return False

    def _enrich_metadata_from_pdf(
        self,
        meta: PaperMetadata,
        outcome: FetchOutcome,
        existing: Any,
    ) -> tuple[PaperMetadata, FetchOutcome, Any]:
        """When pre-fetch metadata was sparse (e.g. bare IEEE URL), extract DOI/info from PDF and enrich via Crossref."""
        if not outcome.pdf_bytes or not self._is_metadata_incomplete(meta):
            return meta, outcome, existing

        extracted = extract_pdf_metadata(outcome.pdf_bytes, preferred_arnumber=meta.ieee_arnumber)
        enriched = meta

        # 1. If we extracted a DOI from the PDF, resolve full official metadata via Crossref/OpenAlex!
        if extracted.doi and (not meta.doi or not meta.creators or meta.title.startswith(("IEEE Document ", "arXiv:"))):
            resolve_doi_fn = getattr(self.resolver, "resolve_by_doi", None)
            resolved: PaperMetadata | None = None
            if callable(resolve_doi_fn):
                resolved = resolve_doi_fn(extracted.doi)
            if resolved is not None and (resolved.creators or resolved.title != extracted.doi):
                if meta.ieee_arnumber:
                    resolved.ieee_arnumber = meta.ieee_arnumber
                    resolved.url = f"https://ieeexplore.ieee.org/document/{meta.ieee_arnumber}"
                if meta.arxiv_id:
                    resolved.arxiv_id = meta.arxiv_id
                if meta.pdf_url and not resolved.pdf_url:
                    resolved.pdf_url = meta.pdf_url
                enriched = resolved
            else:
                enriched.doi = enriched.doi or extracted.doi

        # 2. Fallback to PDF XMP /Info title & authors if still sparse
        if extracted.title and (
            not enriched.title or enriched.title.startswith(("IEEE Document ", "arXiv:", "http://", "https://"))
        ):
            enriched.title = extracted.title
        if extracted.creators and not enriched.creators:
            enriched.creators = extracted.creators

        # Refresh PDF attachment filename using the real title
        outcome.filename = sanitize_filename(
            enriched.title,
            fallback=enriched.ieee_arnumber or enriched.arxiv_id or "paper",
        )

        # Re-check Zotero dedup using the newly discovered DOI/title so we never duplicate an existing paper
        if existing is None and (enriched.doi or enriched.title):
            existing = self.zotero.find_existing_item(
                doi=enriched.doi,
                arxiv_id=enriched.arxiv_id,
                ieee_arnumber=enriched.ieee_arnumber,
                title=enriched.title,
            )

        return enriched, outcome, existing

    def _build_tags(self, user_tags: list[str], *, source: str) -> list[str]:
        tags = ["source:agent"]
        if source:
            tags.append(f"provider:{source}")
        for t in user_tags:
            if t and t not in tags:
                tags.append(t)
        return tags

    def _commit_pdf_to_zotero(
        self,
        *,
        req: IngestRequest,
        meta: PaperMetadata,
        outcome: FetchOutcome,
        existing: Any,
        tags: list[str],
    ) -> IngestResult:
        assert outcome.pdf_bytes is not None
        try:
            if existing is not None:
                res = self.zotero.attach_pdf_to_existing(
                    existing.item_key,
                    outcome.pdf_bytes,
                    pdf_filename=outcome.filename or sanitize_filename(meta.title),
                    pdf_url=outcome.pdf_url or meta.canonical_landing_url(),
                    metadata=meta,
                    collection_name=req.collection,
                    default_collection=self.config.default_collection,
                    tags=tags,
                )
                return IngestResult(
                    identifier=req.identifier,
                    status=res["status"],
                    title=meta.title,
                    doi=meta.doi,
                    arxiv_id=meta.arxiv_id,
                    ieee_arnumber=meta.ieee_arnumber,
                    item_key=res.get("item_key"),
                    existing_item_key=existing.item_key,
                    zotero_collection=res.get("collection"),
                    pdf_attached=bool(res.get("pdf_attached")),
                    source=outcome.source,
                    reason=res.get("reason", ""),
                )

            saved = self.zotero.save_item(
                meta,
                pdf_bytes=outcome.pdf_bytes,
                pdf_filename=outcome.filename or sanitize_filename(meta.title),
                pdf_url=outcome.pdf_url or meta.canonical_landing_url(),
                collection_name=req.collection,
                default_collection=self.config.default_collection,
                tags=tags,
            )
            return IngestResult(
                identifier=req.identifier,
                status="saved",
                title=meta.title,
                doi=meta.doi,
                arxiv_id=meta.arxiv_id,
                ieee_arnumber=meta.ieee_arnumber,
                item_key=saved.get("item_key"),
                zotero_collection=saved.get("collection"),
                pdf_attached=bool(saved.get("pdf_attached")),
                source=outcome.source,
                reason=saved.get("attachment_error", ""),
            )
        except ZoteroError as exc:
            return IngestResult(
                identifier=req.identifier,
                status="failed",
                title=meta.title,
                doi=meta.doi,
                reason=f"zotero_write_failed: {exc}",
            )

    def _run_single_browser_handoff(
        self,
        *,
        req: IngestRequest,
        meta: PaperMetadata,
        existing: Any,
        manual_url: str,
        reason: str,
        tags: list[str],
    ) -> IngestResult:
        """Open paper in real Windows browser, notify user, wait for Zotero Connector or inbox PDF."""
        self.config.inbox_dir.mkdir(parents=True, exist_ok=True)
        started = time.time() - 2.0

        self.notifier(
            "需要浏览器验证/登录以保存文献",
            f"请完成验证后点击 Zotero Connector 保存，或把 PDF 下载到 {self.config.inbox_dir.name}：{meta.title[:60]}",
        )
        if manual_url:
            self.browser_opener(manual_url)

        pending = PendingManualPaper(
            task_id=f"man-{uuid.uuid4().hex[:10]}",
            identifier=req.identifier,
            metadata=meta,
            collection=req.collection,
            tags=tags + ["channel:browser-handoff"],
            item_key=existing.item_key if existing else None,
            manual_url=manual_url,
            reason=reason,
        )

        timeout_sec = max(1, min(req.manual_timeout_sec, self.config.single_manual_timeout_sec))
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            # Signal 1: User clicked Zotero Connector in browser -> item + PDF now exists in Zotero!
            match = self.zotero.find_existing_item(
                doi=meta.doi,
                arxiv_id=meta.arxiv_id,
                ieee_arnumber=meta.ieee_arnumber,
                title=meta.title,
            )
            if match and match.has_pdf:
                return IngestResult(
                    identifier=req.identifier,
                    status="saved",
                    title=match.title or meta.title,
                    doi=match.doi or meta.doi,
                    arxiv_id=meta.arxiv_id,
                    ieee_arnumber=meta.ieee_arnumber,
                    item_key=match.item_key,
                    pdf_attached=True,
                    source="browser_connector",
                    reason="completed_via_browser_handoff",
                )

            # Signal 2: User clicked Download PDF into inbox_dir
            pdf_file = find_matching_pdf_in_inbox(
                self.config.inbox_dir,
                pending,
                seen_files=self._processed_inbox_files,
                min_mtime=started,
            )
            if pdf_file is not None:
                pdf_bytes = pdf_file.read_bytes()
                manual_outcome = FetchOutcome(
                    ok=True,
                    pdf_bytes=pdf_bytes,
                    pdf_url=manual_url,
                    filename=sanitize_filename(meta.title),
                    source="browser_download",
                )
                meta, manual_outcome, existing = self._enrich_metadata_from_pdf(meta, manual_outcome, existing)
                res = self._commit_pdf_to_zotero(
                    req=req,
                    meta=meta,
                    outcome=manual_outcome,
                    existing=existing,
                    tags=pending.tags,
                )
                self._archive_inbox_file(pdf_file)
                return res

            self.sleep_fn(min(2.0, max(0.05, deadline - time.time())))

        # Timed out waiting: register pending so later inbox scan can still complete it
        with self._lock:
            self._pending_manual[pending.task_id] = pending
        return IngestResult(
            identifier=req.identifier,
            status="needs_manual",
            title=meta.title,
            doi=meta.doi,
            arxiv_id=meta.arxiv_id,
            ieee_arnumber=meta.ieee_arnumber,
            item_key=existing.item_key if existing else None,
            pdf_attached=False,
            source=meta.source,
            manual_url=manual_url,
            reason=f"{reason}; manual_wait_timeout",
        )

    def _archive_inbox_file(self, pdf_file: Path) -> None:
        self._processed_inbox_files.add(str(pdf_file.resolve()))
        done_dir = self.config.inbox_dir / "imported"
        try:
            done_dir.mkdir(parents=True, exist_ok=True)
            target = done_dir / pdf_file.name
            if target.exists():
                target = done_dir / f"{pdf_file.stem}_{int(time.time())}{pdf_file.suffix}"
            shutil.move(str(pdf_file), str(target))
        except OSError:
            pass

    def submit_batch(
        self,
        identifiers: list[str],
        *,
        collection: str | None = None,
        tags: list[str] | None = None,
        delay_range_sec: tuple[float, float] | None = None,
    ) -> dict[str, Any]:
        batch_id = f"batch-{uuid.uuid4().hex[:10]}"
        record = BatchRecord(
            batch_id=batch_id,
            state="queued",
            collection=collection,
            tags=list(tags or []),
            total=len(identifiers),
        )
        with self._lock:
            self._batches[batch_id] = record

        d_min, d_max = delay_range_sec or (
            self.config.batch_delay_min_sec,
            self.config.batch_delay_max_sec,
        )
        worker = threading.Thread(
            target=self._run_batch_worker,
            args=(record, identifiers, d_min, d_max),
            name=f"paper-bridge-{batch_id}",
            daemon=True,
        )
        worker.start()
        return {
            "batch_id": batch_id,
            "accepted_count": len(identifiers),
            "state": "queued",
        }

    def _run_batch_worker(
        self,
        record: BatchRecord,
        identifiers: list[str],
        delay_min: float,
        delay_max: float,
    ) -> None:
        record.state = "running"
        consecutive_ieee_blocks = 0

        for idx, ident in enumerate(identifiers):
            skip_ieee = consecutive_ieee_blocks >= self.config.ieee_circuit_breaker_threshold
            req = IngestRequest(
                identifier=ident,
                collection=record.collection,
                tags=record.tags,
                mode="batch",
                wait_for_manual=False,
            )
            res, hit_rl = self.ingest_one(req, skip_silent_ieee=skip_ieee, batch_id=record.batch_id)
            if hit_rl:
                consecutive_ieee_blocks += 1
            elif res.source == "ieee_campus" and res.pdf_attached:
                consecutive_ieee_blocks = 0

            with self._lock:
                record.results.append(res.to_dict())
                record.processed += 1

            # Polite jittered delay between network fetches (skip delay if already in Zotero)
            if idx + 1 < len(identifiers) and res.source != "zotero_existing" and delay_max > 0:
                delay = random.uniform(max(0.0, delay_min), max(delay_min, delay_max))
                self.sleep_fn(delay)

        record.finished_at = time.time()
        if record.manual_items:
            record.state = "completed_with_manual"
            self.open_manual_checklist(record.batch_id)
        else:
            record.state = "completed"
            self.notifier(
                "文献批量汇总完成",
                f"批次 {record.batch_id} 共 {record.total} 篇已全部静默入库 Zotero。",
            )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._batches.get(batch_id)
            if rec is None:
                return None
            return rec.summary_dict(helper_port=self.config.listen_port)

    def open_manual_checklist(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._batches.get(batch_id)
        if rec is None:
            return {"ok": False, "reason": "batch_not_found"}
        url = f"http://127.0.0.1:{self.config.listen_port}/manual/{batch_id}?nonce={rec.local_nonce}"
        pending_count = sum(1 for m in rec.manual_items if not m.completed)
        self.notifier(
            "批量下载完成，有文献待补 PDF",
            f"批次 {batch_id} 中有 {pending_count} 篇需要浏览器验证/登录，点击打开待补清单。",
        )
        opened = self.browser_opener(url)
        return {"ok": True, "url": url, "opened": opened, "pending_count": pending_count}

    def render_manual_page(self, batch_id: str, nonce: str) -> tuple[int, str]:
        with self._lock:
            rec = self._batches.get(batch_id)
        if rec is None:
            return 404, "Batch not found"
        if not secrets.compare_digest(nonce or "", rec.local_nonce):
            return 403, "Invalid nonce"
        html_doc = render_manual_checklist_html(
            batch_id=batch_id,
            pending_items=list(rec.manual_items),
            inbox_dir=self.config.inbox_dir,
            scan_endpoint=f"/manual/{batch_id}/scan",
            local_nonce=rec.local_nonce,
        )
        return 200, html_doc

    def verify_batch_nonce(self, batch_id: str, nonce: str) -> bool:
        with self._lock:
            rec = self._batches.get(batch_id)
        return rec is not None and secrets.compare_digest(nonce or "", rec.local_nonce)

    def scan_inbox_and_attach(self, *, batch_id: str | None = None) -> dict[str, Any]:
        """Match manually downloaded PDFs in inbox_dir against pending `needs-pdf` items."""
        self.config.inbox_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            candidates = [
                p
                for p in self._pending_manual.values()
                if not p.completed and (batch_id is None or p.batch_id == batch_id)
            ]

        attached: list[dict[str, Any]] = []
        for pending in candidates:
            pdf_file = find_matching_pdf_in_inbox(
                self.config.inbox_dir,
                pending,
                seen_files=self._processed_inbox_files,
            )
            if pdf_file is None:
                continue
            pdf_bytes = pdf_file.read_bytes()
            if pending.item_key:
                res = self.zotero.attach_pdf_to_existing(
                    pending.item_key,
                    pdf_bytes,
                    pdf_filename=sanitize_filename(pending.metadata.title),
                    pdf_url=pending.manual_url,
                    metadata=pending.metadata,
                    collection_name=pending.collection,
                    default_collection=self.config.default_collection,
                    tags=pending.tags + ["pdf-attached"],
                )
            else:
                saved = self.zotero.save_item(
                    pending.metadata,
                    pdf_bytes=pdf_bytes,
                    pdf_filename=sanitize_filename(pending.metadata.title),
                    pdf_url=pending.manual_url,
                    collection_name=pending.collection,
                    default_collection=self.config.default_collection,
                    tags=pending.tags + ["pdf-attached"],
                )
                res = {
                    "status": "saved",
                    "item_key": saved.get("item_key"),
                    "pdf_attached": bool(saved.get("pdf_attached")),
                    "reason": saved.get("attachment_error", ""),
                }

            if res.get("pdf_attached"):
                pending.completed = True
                self._archive_inbox_file(pdf_file)
                attached.append(
                    {
                        "task_id": pending.task_id,
                        "title": pending.metadata.title,
                        "status": res["status"],
                        "item_key": res.get("item_key"),
                        "file": pdf_file.name,
                    }
                )

        remaining = sum(
            1
            for p in self._pending_manual.values()
            if not p.completed and (batch_id is None or p.batch_id == batch_id)
        )
        return {
            "attached_count": len(attached),
            "remaining_count": remaining,
            "attached": attached,
        }
