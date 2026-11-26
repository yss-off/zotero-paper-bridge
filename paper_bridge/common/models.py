"""Data structures shared between the CLI and local Zotero helper daemon."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Author:
    first_name: str = ""
    last_name: str = ""
    name: str = ""

    def to_zotero_creator(self) -> dict[str, str]:
        if self.last_name or self.first_name:
            return {
                "creatorType": "author",
                "firstName": self.first_name.strip(),
                "lastName": self.last_name.strip(),
            }
        return {"creatorType": "author", "name": self.name.strip() or "Unknown"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Author":
        return cls(
            first_name=str(data.get("first_name") or data.get("firstName") or ""),
            last_name=str(data.get("last_name") or data.get("lastName") or ""),
            name=str(data.get("name") or ""),
        )


@dataclass
class ParsedIdentifier:
    raw: str
    kind: str  # doi | ieee_url | arxiv | url | title
    normalized: str
    doi: str | None = None
    arxiv_id: str | None = None
    ieee_arnumber: str | None = None
    url: str | None = None
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperMetadata:
    title: str
    item_type: str = "journalArticle"
    creators: list[Author] = field(default_factory=list)
    doi: str | None = None
    arxiv_id: str | None = None
    ieee_arnumber: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    publication_title: str = ""
    proceedings_title: str = ""
    date: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    abstract_note: str = ""
    publisher: str = ""
    source: str = "unknown"
    extra: str = ""

    def canonical_landing_url(self) -> str:
        if self.ieee_arnumber:
            return f"https://ieeexplore.ieee.org/document/{self.ieee_arnumber}"
        if self.url:
            return self.url
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.arxiv_id:
            return f"https://arxiv.org/abs/{self.arxiv_id}"
        return ""

    def to_zotero_item(self, connector_item_id: str, tags: list[str] | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": connector_item_id,
            "itemType": self.item_type,
            "title": self.title,
            "creators": [c.to_zotero_creator() for c in self.creators],
            "url": self.canonical_landing_url(),
            "date": self.date,
            "abstractNote": self.abstract_note,
            "tags": [{"tag": t} for t in (tags or []) if t],
        }
        if self.doi:
            item["DOI"] = self.doi
        if self.item_type == "conferencePaper":
            if self.proceedings_title or self.publication_title:
                item["proceedingsTitle"] = self.proceedings_title or self.publication_title
        elif self.item_type == "preprint":
            if self.publication_title:
                item["repository"] = self.publication_title
            if self.arxiv_id:
                item["archiveID"] = f"arXiv:{self.arxiv_id}"
        else:
            if self.publication_title:
                item["publicationTitle"] = self.publication_title
        if self.volume:
            item["volume"] = self.volume
        if self.issue:
            item["issue"] = self.issue
        if self.pages:
            item["pages"] = self.pages
        if self.publisher and self.item_type in {"conferencePaper", "book", "bookSection"}:
            item["publisher"] = self.publisher
        extra_lines: list[str] = []
        if self.extra:
            extra_lines.append(self.extra)
        if self.arxiv_id and f"arXiv:{self.arxiv_id}" not in self.extra:
            extra_lines.append(f"arXiv:{self.arxiv_id}")
        if self.ieee_arnumber and f"IEEE-arnumber:{self.ieee_arnumber}" not in self.extra:
            extra_lines.append(f"IEEE-arnumber:{self.ieee_arnumber}")
        if extra_lines:
            item["extra"] = "\n".join(extra_lines)
        return item

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class IngestRequest:
    identifier: str
    collection: str | None = None
    tags: list[str] = field(default_factory=list)
    mode: str = "single"  # single | batch
    wait_for_manual: bool = True
    manual_timeout_sec: int = 300

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IngestRequest":
        return cls(
            identifier=str(data.get("identifier", "")).strip(),
            collection=data.get("collection") or None,
            tags=[str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
            mode=str(data.get("mode", "single")),
            wait_for_manual=bool(data.get("wait_for_manual", True)),
            manual_timeout_sec=int(data.get("manual_timeout_sec", 300)),
        )


@dataclass
class IngestResult:
    identifier: str
    status: str  # saved | already_exists | attached | saved_merge_needed | needs_manual | ambiguous | failed | queued
    title: str = ""
    doi: str | None = None
    arxiv_id: str | None = None
    ieee_arnumber: str | None = None
    item_key: str | None = None
    existing_item_key: str | None = None
    zotero_collection: str | None = None
    pdf_attached: bool = False
    source: str = ""
    manual_url: str | None = None
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    manual_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ingest_outcome(result: dict[str, Any]) -> str:
    """Translate a paper result into the state of an offline queue entry."""
    status = result.get("status")
    if status == "failed":
        return "failed"
    if status in {"saved", "attached", "already_exists"} and result.get("pdf_attached"):
        return "done"
    return "needs_attention"
