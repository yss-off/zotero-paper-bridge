"""Atomic, idempotent JSONL offline queue stored on the Ubuntu server."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..common.identifiers import parse_identifier


@dataclass
class QueueEntry:
    task_id: str
    identifier: str
    dedup_key: str
    collection: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | submitted | done | failed
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0
    last_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueEntry":
        return cls(
            task_id=str(data["task_id"]),
            identifier=str(data["identifier"]),
            dedup_key=str(data.get("dedup_key") or data["identifier"]),
            collection=data.get("collection") or None,
            tags=list(data.get("tags") or []),
            status=str(data.get("status") or "pending"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            attempts=int(data.get("attempts") or 0),
            last_result=data.get("last_result"),
        )


class OfflineQueueStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load_all(self) -> list[QueueEntry]:
        if not self.path.exists():
            return []
        entries: list[QueueEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            try:
                entries.append(QueueEntry.from_dict(json.loads(cleaned)))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return entries

    def save_all(self, entries: list[QueueEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in entries]
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def enqueue(
        self,
        identifiers: list[str],
        *,
        collection: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[list[QueueEntry], int]:
        """Enqueue identifiers idempotently. Returns (newly_added_entries, skipped_duplicate_count)."""
        entries = self.load_all()
        existing_pending = {
            (e.dedup_key, (e.collection or "").lower())
            for e in entries
            if e.status == "pending"
        }
        added: list[QueueEntry] = []
        skipped = 0
        for raw in identifiers:
            cleaned = raw.strip()
            if not cleaned:
                continue
            parsed = parse_identifier(cleaned)
            sig = (parsed.normalized, (collection or "").lower())
            if sig in existing_pending:
                skipped += 1
                continue
            entry = QueueEntry(
                task_id=f"q-{uuid.uuid4().hex[:10]}",
                identifier=cleaned,
                dedup_key=parsed.normalized,
                collection=collection,
                tags=list(tags or []),
            )
            entries.append(entry)
            existing_pending.add(sig)
            added.append(entry)

        if added:
            self.save_all(entries)
        return added, skipped

    def list_pending(self) -> list[QueueEntry]:
        return [e for e in self.load_all() if e.status == "pending"]

    def update_status(
        self,
        task_ids: set[str],
        *,
        status: str,
        last_result: dict[str, Any] | None = None,
    ) -> None:
        entries = self.load_all()
        now = time.time()
        for e in entries:
            if e.task_id in task_ids:
                e.status = status
                e.updated_at = now
                e.attempts += 1
                if last_result is not None:
                    e.last_result = last_result
        self.save_all(entries)

    def clear(self, *, only_completed: bool = True) -> int:
        entries = self.load_all()
        if only_completed:
            kept = [e for e in entries if e.status == "pending"]
        else:
            kept = []
        removed = len(entries) - len(kept)
        self.save_all(kept)
        return removed
