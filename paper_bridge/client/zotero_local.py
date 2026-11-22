"""Zotero 7 Local API (read) + Connector HTTP API (write) client."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from ..common.identifiers import normalize_doi, normalize_title
from ..common.models import PaperMetadata


class ZoteroError(RuntimeError):
    """Raised when Zotero desktop communication fails."""


@dataclass
class ZoteroCollectionInfo:
    name: str
    connector_target_id: str | None = None  # e.g. "C12"
    api_key: str | None = None  # e.g. "ABCD1234"
    level: int = 1


@dataclass
class ZoteroExistingMatch:
    item_key: str
    title: str
    has_pdf: bool
    doi: str | None = None
    collections: list[str] | None = None
    match_reason: str = ""


@dataclass
class SessionRecord:
    session_id: str
    connector_item_id: str
    item_key: str | None
    title: str
    doi: str | None
    arxiv_id: str | None
    ieee_arnumber: str | None
    collection_name: str | None


class ZoteroLocalClient:
    """Talks to Zotero 7 desktop on http://127.0.0.1:23119."""

    CONNECTOR_HEADERS = {
        "X-Zotero-Connector-API-Version": "3",
        "X-Zotero-Version": "5.0.0",
        "User-Agent": "zotero-paper-bridge/0.1",
    }

    def __init__(self, base_url: str = "http://127.0.0.1:23119", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Remembers items created during this helper's lifetime so manual PDF drops
        # can attach directly to the exact Zotero Connector session without duplication.
        self._sessions_by_key: dict[str, SessionRecord] = {}
        self._sessions_by_doi: dict[str, SessionRecord] = {}
        self._sessions_by_arnumber: dict[str, SessionRecord] = {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        url = f"{self.base_url}{path}"
        req_headers = {"User-Agent": "zotero-paper-bridge/0.1"}
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            return exc.code, err_body, dict(exc.headers.items())
        except (urllib.error.URLError, OSError) as exc:
            raise ZoteroError(f"Cannot reach Zotero at {url}: {exc}") from exc

    def _connector_post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        headers = dict(self.CONNECTOR_HEADERS)
        headers["Content-Type"] = "application/json"
        status, raw, _ = self._request(
            "POST",
            path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        if not raw.strip():
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", errors="replace")

    def _local_api_get_json(self, path: str) -> tuple[int, Any]:
        status, raw, _ = self._request(
            "GET",
            path,
            headers={"Accept": "application/json", "Zotero-API-Version": "3"},
        )
        if not raw.strip():
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return status, raw.decode("utf-8", errors="replace")

    def ping(self) -> bool:
        try:
            status, _ = self._connector_post_json("/connector/ping", {})
            return 200 <= status < 300
        except ZoteroError:
            return False

    def local_api_ready(self) -> bool:
        try:
            status, _ = self._local_api_get_json("/api/users/0/collections?limit=1")
            return status == 200
        except ZoteroError:
            return False

    def get_selected_collection(self) -> dict[str, Any]:
        status, data = self._connector_post_json("/connector/getSelectedCollection", {})
        if status >= 400 or not isinstance(data, dict):
            return {}
        return data

    def list_collections(self) -> list[ZoteroCollectionInfo]:
        by_name: dict[str, ZoteroCollectionInfo] = {}

        # 1. Connector targets give us the exact "C<id>" IDs accepted by /connector/updateSession
        selected = self.get_selected_collection()
        for target in selected.get("targets") or []:
            if not isinstance(target, dict):
                continue
            tid = str(target.get("id") or "")
            name = str(target.get("name") or "").strip()
            if not name or not tid.startswith("C"):
                continue
            by_name[name.lower()] = ZoteroCollectionInfo(
                name=name,
                connector_target_id=tid,
                level=int(target.get("level", 1)),
            )

        # 2. Enrich with Zotero Local API collections (gives canonical keys even if connector omitted some)
        try:
            status, rows = self._local_api_get_json("/api/users/0/collections?limit=200")
            if status == 200 and isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    data = row.get("data") if isinstance(row.get("data"), dict) else row
                    name = str(data.get("name") or "").strip()
                    key = str(row.get("key") or data.get("key") or "").strip() or None
                    if not name:
                        continue
                    existing = by_name.get(name.lower())
                    if existing:
                        existing.api_key = key
                    else:
                        by_name[name.lower()] = ZoteroCollectionInfo(name=name, api_key=key)
        except ZoteroError:
            pass

        return sorted(by_name.values(), key=lambda c: c.name.lower())

    def resolve_collection_target(
        self, requested_name: str | None, default_collection: str
    ) -> tuple[ZoteroCollectionInfo | None, str | None]:
        """Resolve requested collection -> (CollectionInfo or None, fallback_tag or None)."""
        collections = self.list_collections()
        lookup = {c.name.lower(): c for c in collections}

        if requested_name:
            hit = lookup.get(requested_name.strip().lower())
            if hit:
                return hit, None
            # Fallback to default_collection and preserve requested collection name as a tag
            fallback = lookup.get(default_collection.strip().lower())
            return fallback, f"target:{requested_name.strip()}"

        return lookup.get(default_collection.strip().lower()), None

    def _item_has_pdf(self, item_key: str) -> bool:
        status, children = self._local_api_get_json(f"/api/users/0/items/{item_key}/children")
        if status != 200 or not isinstance(children, list):
            return False
        for child in children:
            data = child.get("data") if isinstance(child, dict) and isinstance(child.get("data"), dict) else child
            if not isinstance(data, dict):
                continue
            if data.get("itemType") == "attachment":
                ctype = str(data.get("contentType") or "").lower()
                fname = str(data.get("filename") or data.get("title") or "").lower()
                if "pdf" in ctype or fname.endswith(".pdf"):
                    return True
        return False

    def _search_local_items(self, query: str, *, limit: int = 25) -> list[dict[str, Any]]:
        encoded = urllib.parse.urlencode({"q": query, "qmode": "everything", "limit": str(limit)})
        status, rows = self._local_api_get_json(f"/api/users/0/items/top?{encoded}")
        if status == 200 and isinstance(rows, list):
            return rows
        # Fallback if /items/top isn't supported by a given Zotero build
        status, rows = self._local_api_get_json(f"/api/users/0/items?{encoded}")
        if status == 200 and isinstance(rows, list):
            return rows
        return []

    def find_existing_item(
        self,
        *,
        doi: str | None = None,
        arxiv_id: str | None = None,
        ieee_arnumber: str | None = None,
        title: str | None = None,
        exclude_keys: set[str] | None = None,
        prefer_with_pdf: bool = False,
    ) -> ZoteroExistingMatch | None:
        """Check local Zotero library for an existing matching top-level item."""
        if not self.local_api_ready():
            return None

        norm_doi = normalize_doi(doi) if doi else None
        norm_arxiv = arxiv_id.lower().split("v")[0] if arxiv_id else None
        norm_title = normalize_title(title) if title else None
        excluded = exclude_keys or set()

        queries: list[tuple[str, str]] = []
        if norm_doi:
            queries.append(("doi", norm_doi))
        if ieee_arnumber:
            queries.append(("ieee", ieee_arnumber))
        if norm_arxiv:
            queries.append(("arxiv", norm_arxiv))
        if title and len(title.strip()) >= 8:
            queries.append(("title", title.strip()[:120]))

        seen_keys: set[str] = set()
        matches: list[ZoteroExistingMatch] = []
        for _, q in queries:
            for row in self._search_local_items(q):
                if not isinstance(row, dict):
                    continue
                data = row.get("data") if isinstance(row.get("data"), dict) else row
                item_type = str(data.get("itemType") or "")
                if item_type in {"attachment", "note", "annotation"}:
                    continue
                key = str(row.get("key") or data.get("key") or "").strip()
                if not key or key in seen_keys or key in excluded:
                    continue
                seen_keys.add(key)

                item_doi = normalize_doi(str(data.get("DOI") or "")) if data.get("DOI") else None
                item_url = str(data.get("url") or "").lower()
                item_extra = str(data.get("extra") or "").lower()
                item_title = str(data.get("title") or "")
                item_norm_title = normalize_title(item_title)

                reason = ""
                if norm_doi and item_doi and item_doi == norm_doi:
                    reason = "doi"
                elif ieee_arnumber and (
                    f"/document/{ieee_arnumber}" in item_url
                    or f"arnumber={ieee_arnumber}" in item_url
                    or f"ieee-arnumber:{ieee_arnumber}" in item_extra
                ):
                    reason = "ieee_arnumber"
                elif norm_arxiv and (
                    norm_arxiv in item_url
                    or norm_arxiv in item_extra
                    or (item_doi and item_doi == f"10.48550/arxiv.{norm_arxiv}")
                ):
                    reason = "arxiv_id"
                elif norm_title and item_norm_title and norm_title == item_norm_title:
                    reason = "exact_title"

                if reason:
                    match = ZoteroExistingMatch(
                        item_key=key,
                        title=item_title,
                        has_pdf=self._item_has_pdf(key),
                        doi=item_doi,
                        collections=list(data.get("collections") or []),
                        match_reason=reason,
                    )
                    if not prefer_with_pdf or match.has_pdf:
                        return match
                    matches.append(match)
        return matches[0] if matches else None

    def save_item(
        self,
        metadata: PaperMetadata,
        *,
        pdf_bytes: bytes | None = None,
        pdf_filename: str | None = None,
        pdf_url: str | None = None,
        collection_name: str | None = None,
        default_collection: str = "Agent-Inbox",
        tags: list[str] | None = None,
        exclude_existing_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        """Create a Zotero item via Connector API, move to collection, and attach PDF if provided."""
        if not self.ping():
            raise ZoteroError("Zotero Desktop is not running or not listening on 23119.")

        target_col, fallback_tag = self.resolve_collection_target(collection_name, default_collection)
        merged_tags: list[str] = []
        for t in (tags or []):
            if t and t not in merged_tags:
                merged_tags.append(t)
        if fallback_tag and fallback_tag not in merged_tags:
            merged_tags.append(fallback_tag)

        session_id = f"pb-{uuid.uuid4().hex[:16]}"
        connector_item_id = f"item-{uuid.uuid4().hex[:12]}"
        zotero_item = metadata.to_zotero_item(connector_item_id, tags=merged_tags)

        status, resp = self._connector_post_json(
            "/connector/saveItems",
            {
                "sessionID": session_id,
                "uri": metadata.canonical_landing_url() or "https://example.org/paper-bridge",
                "items": [zotero_item],
            },
        )
        if status >= 400:
            raise ZoteroError(f"/connector/saveItems failed (HTTP {status}): {resp}")

        resolved_collection_name: str | None = None
        if target_col and target_col.connector_target_id:
            u_status, u_resp = self._connector_post_json(
                "/connector/updateSession",
                {
                    "sessionID": session_id,
                    "target": target_col.connector_target_id,
                },
            )
            if u_status < 400:
                resolved_collection_name = target_col.name
            else:
                # Don't abort the save if updateSession failed on a specific Zotero build; report clearly
                resolved_collection_name = f"(default-selected; updateSession HTTP {u_status}: {u_resp})"

        pdf_attached = False
        attachment_error = ""
        if pdf_bytes:
            pdf_attached, attachment_error = self._upload_attachment(
                session_id=session_id,
                parent_item_id=connector_item_id,
                pdf_bytes=pdf_bytes,
                filename=pdf_filename or "Full Text PDF.pdf",
                pdf_url=pdf_url or metadata.pdf_url or metadata.canonical_landing_url(),
            )

        # Resolve the newly saved item_key via Local API so server zotero-mcp can read it immediately
        item_key: str | None = None
        if self.local_api_ready():
            for _ in range(3):
                match = self.find_existing_item(
                    doi=metadata.doi,
                    arxiv_id=metadata.arxiv_id,
                    ieee_arnumber=metadata.ieee_arnumber,
                    title=metadata.title,
                    exclude_keys=exclude_existing_keys,
                    prefer_with_pdf=bool(pdf_attached),
                )
                if match:
                    item_key = match.item_key
                    break
                time.sleep(0.3)

        record = SessionRecord(
            session_id=session_id,
            connector_item_id=connector_item_id,
            item_key=item_key,
            title=metadata.title,
            doi=normalize_doi(metadata.doi) if metadata.doi else None,
            arxiv_id=metadata.arxiv_id,
            ieee_arnumber=metadata.ieee_arnumber,
            collection_name=resolved_collection_name or (target_col.name if target_col else None),
        )
        self._remember_session(record)

        return {
            "session_id": session_id,
            "connector_item_id": connector_item_id,
            "item_key": item_key,
            "collection": record.collection_name,
            "pdf_attached": pdf_attached,
            "attachment_error": attachment_error,
            "fallback_tag": fallback_tag,
        }

    def _remember_session(self, record: SessionRecord) -> None:
        if record.item_key:
            self._sessions_by_key[record.item_key] = record
        if record.doi:
            self._sessions_by_doi[record.doi] = record
        if record.ieee_arnumber:
            self._sessions_by_arnumber[record.ieee_arnumber] = record

    def get_session_record(
        self,
        *,
        item_key: str | None = None,
        doi: str | None = None,
        ieee_arnumber: str | None = None,
    ) -> SessionRecord | None:
        if item_key and item_key in self._sessions_by_key:
            return self._sessions_by_key[item_key]
        if doi and normalize_doi(doi) in self._sessions_by_doi:
            return self._sessions_by_doi[normalize_doi(doi)]
        if ieee_arnumber and ieee_arnumber in self._sessions_by_arnumber:
            return self._sessions_by_arnumber[ieee_arnumber]
        return None

    def _upload_attachment(
        self,
        *,
        session_id: str,
        parent_item_id: str,
        pdf_bytes: bytes,
        filename: str,
        pdf_url: str,
    ) -> tuple[bool, str]:
        meta_header = json.dumps(
            {
                "sessionID": session_id,
                "parentItemID": parent_item_id,
                "title": filename,
                "url": pdf_url or "",
            },
            ensure_ascii=True,  # HTTP headers must stay ASCII-safe
        )
        headers = dict(self.CONNECTOR_HEADERS)
        headers["Content-Type"] = "application/pdf"
        headers["X-Metadata"] = meta_header
        status, raw, _ = self._request(
            "POST",
            "/connector/saveAttachment",
            body=pdf_bytes,
            headers=headers,
            timeout=60.0,
        )
        if 200 <= status < 300:
            return True, ""
        return False, f"HTTP {status}: {raw.decode('utf-8', errors='replace')[:200]}"

    def attach_pdf_to_existing(
        self,
        existing_item_key: str,
        pdf_bytes: bytes,
        *,
        pdf_filename: str,
        pdf_url: str,
        metadata: PaperMetadata,
        collection_name: str | None = None,
        default_collection: str = "Agent-Inbox",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Attach PDF to an existing metadata-only Zotero item.

        1. If the item was created in this helper session, attach via its live Connector session.
        2. Otherwise try Connector attachment directly with parentItemID=existing_item_key.
        3. If Zotero rejects cross-session attachment, save a complete new item with
           tag `merge-duplicate` so the PDF is never lost and can be merged in one click.
        """
        rec = self.get_session_record(
            item_key=existing_item_key,
            doi=metadata.doi,
            ieee_arnumber=metadata.ieee_arnumber,
        )
        if rec is not None:
            ok, err = self._upload_attachment(
                session_id=rec.session_id,
                parent_item_id=rec.connector_item_id,
                pdf_bytes=pdf_bytes,
                filename=pdf_filename,
                pdf_url=pdf_url,
            )
            if ok:
                return {
                    "status": "attached",
                    "item_key": existing_item_key,
                    "existing_item_key": existing_item_key,
                    "collection": rec.collection_name,
                    "pdf_attached": True,
                    "reason": "attached_via_live_session",
                }
            direct_err = err
        else:
            # Probe whether this Zotero build accepts attaching via existing item key
            probe_session = f"pb-attach-{uuid.uuid4().hex[:12]}"
            ok, direct_err = self._upload_attachment(
                session_id=probe_session,
                parent_item_id=existing_item_key,
                pdf_bytes=pdf_bytes,
                filename=pdf_filename,
                pdf_url=pdf_url,
            )
            if ok:
                return {
                    "status": "attached",
                    "item_key": existing_item_key,
                    "existing_item_key": existing_item_key,
                    "collection": collection_name or default_collection,
                    "pdf_attached": True,
                    "reason": "attached_via_item_key",
                }

        # Clean fallback: save complete new item tagged merge-duplicate so PDF is preserved in Zotero
        fallback_tags = list(tags or [])
        if "merge-duplicate" not in fallback_tags:
            fallback_tags.append("merge-duplicate")
        saved = self.save_item(
            metadata,
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename,
            pdf_url=pdf_url,
            collection_name=collection_name,
            default_collection=default_collection,
            tags=fallback_tags,
            exclude_existing_keys={existing_item_key},
        )
        return {
            "status": "saved_merge_needed",
            "item_key": saved.get("item_key"),
            "existing_item_key": existing_item_key,
            "collection": saved.get("collection"),
            "pdf_attached": bool(saved.get("pdf_attached")),
            "reason": f"cross_session_attach_unsupported ({direct_err}); created complete item tagged merge-duplicate",
        }
