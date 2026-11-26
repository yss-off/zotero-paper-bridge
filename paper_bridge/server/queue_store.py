"""Atomic, idempotent JSONL offline queue stored on the Ubuntu server."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..common.identifiers import parse_identifier
from ..common.persistence import atomic_write_text, file_lock


@dataclass
class QueueEntry:
    task_id: str
    identifier: str
    dedup_key: str
    collection: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | submitted | done | failed | needs_attention
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
        self.lock_path = path.with_suffix(".lock")

    def load_all(self) -> list[QueueEntry]:
        with file_lock(self.lock_path):
            return self._load_all()

    def _load_all(self) -> list[QueueEntry]:
        if not self.path.exists():
            return []
        entries: list[QueueEntry] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            cleaned = line.strip()
            if not cleaned:
                continue
            try:
                entries.append(QueueEntry.from_dict(json.loads(cleaned)))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f"Invalid queue record at {self.path}:{number}") from exc
        return entries

    def _save_all(self, entries: list[QueueEntry]) -> None:
        lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in entries]
        atomic_write_text(self.path, "\n".join(lines) + ("\n" if lines else ""))

    def drain_lock(self) -> AbstractContextManager[None]:
        return file_lock(self.path.with_suffix(".drain.lock"), blocking=False)

    def enqueue(
        self,
        identifiers: list[str],
        *,
        collection: str | None = None,
        tags: list[str] | None = None,
    ) -> tuple[list[QueueEntry], int]:
        """Enqueue identifiers idempotently. Returns (newly_added_entries, skipped_duplicate_count)."""
        with file_lock(self.lock_path):
            return self._enqueue(identifiers, collection=collection, tags=tags)

    def _enqueue(
        self, identifiers: list[str], *, collection: str | None, tags: list[str] | None
    ) -> tuple[list[QueueEntry], int]:
        entries = self._load_all()
        existing_pending = {
            (e.dedup_key, (e.collection or "").lower())
            for e in entries
            if e.status in {"pending", "submitted"}
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
            self._save_all(entries)
        return added, skipped

    def list_pending(self) -> list[QueueEntry]:
        return [e for e in self.load_all() if e.status == "pending"]

    def update_results(
        self, updates: dict[str, tuple[str, dict[str, Any]]], *, count_attempt: bool = False
    ) -> None:
        with file_lock(self.lock_path):
            entries = self._load_all()
            for entry in entries:
                if entry.task_id in updates:
                    entry.status, entry.last_result = updates[entry.task_id]
                    entry.updated_at = time.time()
                    entry.attempts += int(count_attempt)
            self._save_all(entries)

    def retry(self, task_id: str) -> bool:
        """Put a failed, needs_attention, or submitted task back into the pending queue."""
        with self.drain_lock(), file_lock(self.lock_path):
            entries = self._load_all()
            for entry in entries:
                if entry.task_id == task_id and entry.status in {"failed", "needs_attention", "submitted"}:
                    entry.status = "pending"
                    entry.updated_at = time.time()
                    self._save_all(entries)
                    return True
        return False

    retry_failed = retry  # Compatibility with callers of the original method.

    def clear(self, *, only_completed: bool = True) -> int:
        with self.drain_lock(), file_lock(self.lock_path):
            entries = self._load_all()
            kept = [e for e in entries if e.status != "done"] if only_completed else []
            self._save_all(kept)
            return len(entries) - len(kept)
