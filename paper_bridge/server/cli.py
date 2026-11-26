"""CLI (`paper-bridge`) for ingesting papers into local Zotero 7 via `paper-bridge-helper`."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..common.identifiers import load_identifiers_from_file
from ..common.models import ingest_outcome
from ..common.security import ensure_token
from ..common.transport import HTTPStatusError, TransportError
from .client import PaperBridgeClient
from .config import ServerConfig
from .diagnostics import check_mcp_fulltext, check_webdav_file
from .queue_store import OfflineQueueStore, QueueEntry

ACTIVE_BATCH_STATES = {"queued", "running"}
FINISHED_BATCH_STATES = {"completed", "completed_with_manual", "completed_with_errors"}
DRAIN_BUSY_ERROR = "Another paper-bridge drain is already running; try again shortly."
ZOTERO_OFFLINE_ERROR = "Zotero Desktop is offline"


def _print_output(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict):
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data)


def cmd_init(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    cfg.ensure_initialized()
    token, created = ensure_token(cfg.token_path)
    out = {
        "config_path": str(cfg.config_path),
        "token_path": str(cfg.token_path),
        "token_status": "created" if created else "existing",
        "socket_dir": str(cfg.socket_path.parent),
        "queue_path": str(cfg.queue_path),
    }
    if args.show_token:
        out["token"] = token
    _print_output(out, as_json=True)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    if not math.isfinite(args.probe_timeout) or args.probe_timeout <= 0:
        raise ValueError("The probe timeout must be a positive number of seconds")
    if args.webdav_file_url is None and (args.webdav_user is not None or args.webdav_password_file is not None):
        raise ValueError("Provide --webdav-file-url when supplying WebDAV credentials")
    queue = OfflineQueueStore(cfg.queue_path)
    client = PaperBridgeClient(cfg)
    ok, data, err = client.is_reachable()
    payload: dict[str, Any] = {
        "reachable": ok,
        "socket_path": str(cfg.socket_path),
        "socket_exists": cfg.socket_path.exists(),
        "tcp_base_url": cfg.tcp_base_url,
    }
    try:
        queue_counts = Counter(entry.status for entry in queue.load_all())
    except (ValueError, OSError) as exc:
        payload.update(pending_offline_queue=None, offline_queue=None, queue_error=str(exc))
    else:
        payload["pending_offline_queue"] = queue_counts["pending"] + queue_counts["submitted"]
        payload["offline_queue"] = dict(queue_counts)
    if ok and data:
        payload["helper"] = data
    else:
        payload["error"] = err
    probes = {}
    if args.fulltext_key is not None:
        try:
            probes["mcp_read_socket"] = check_mcp_fulltext(args.fulltext_key, timeout=args.probe_timeout)
        except (ValueError, OSError) as exc:
            probes["mcp_read_socket"] = {"readable": False, "error": str(exc)}
    if args.webdav_file_url is not None:
        try:
            probes["webdav_file"] = check_webdav_file(
                args.webdav_file_url,
                username=args.webdav_user,
                password_file=args.webdav_password_file,
                timeout=args.probe_timeout,
            )
        except (ValueError, OSError) as exc:
            probes["webdav_file"] = {"readable": False, "error": str(exc)}
    if probes:
        payload["probes"] = probes
    _print_output(payload, as_json=args.json)
    healthy = (
        ok and (data or {}).get("zotero_alive") and "queue_error" not in payload
        and all(probe["readable"] for probe in probes.values())
    )
    return 0 if healthy else 1


def _ingest_ready(client: PaperBridgeClient) -> tuple[dict[str, Any] | None, str]:
    ok, data, error = client.is_reachable()
    if not ok:
        return None, error
    if not (data or {}).get("zotero_alive"):
        return None, ZOTERO_OFFLINE_ERROR
    return data, ""


def _queued_next_step(error: str) -> str:
    if error == ZOTERO_OFFLINE_ERROR:
        return "Open Zotero Desktop, then run `paper-bridge drain --wait`."
    return "Start the local helper (`systemctl --user start paper-bridge-helper.service` or `paper-bridge-helper serve`), then run `paper-bridge drain --wait`."


def cmd_collections(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    client = PaperBridgeClient(cfg)
    try:
        health = client.health()
    except (TransportError, HTTPStatusError, FileNotFoundError) as exc:
        print(f"[ERROR] Cannot reach local helper: {exc}", file=sys.stderr)
        return 1
    cols = health.get("collections") or []
    if args.json:
        print(json.dumps(cols, ensure_ascii=False, indent=2))
    else:
        if not cols:
            print("(No collections returned; check Zotero 7 is open and Local API is enabled)")
        for c in cols:
            print(f"- {c.get('name')}  (connector_id={c.get('connector_target_id')}, api_key={c.get('api_key')})")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    queue = OfflineQueueStore(cfg.queue_path)
    client = PaperBridgeClient(cfg)

    ok, err = _ingest_ready(client)
    if not ok:
        if args.no_queue:
            print(f"[ERROR] Local helper unreachable ({err})", file=sys.stderr)
            return 1
        added, skipped = queue.enqueue(
            [args.identifier],
            collection=args.collection,
            tags=args.tag or [],
        )
        res = {
            "status": "queued",
            "identifier": args.identifier,
            "collection": args.collection or cfg.default_collection,
            "queued_count": len(added),
            "skipped_duplicate": skipped,
            "reason": f"local_helper_offline: {err}",
            "next_step": _queued_next_step(err),
        }
        _print_output(res, as_json=args.json)
        return 0

    try:
        res = client.ingest(
            args.identifier,
            collection=args.collection,
            tags=args.tag or [],
            mode="single",
            wait_for_manual=not args.no_wait,
            manual_timeout_sec=args.timeout,
        )
        if res.get("reason") == "zotero_not_running":
            raise TransportError("Zotero Desktop went offline before ingest")
    except HTTPStatusError as exc:
        print(f"[ERROR] Helper returned {exc}", file=sys.stderr)
        return 1
    except TransportError as exc:
        if args.no_queue:
            print(f"[ERROR] Transport failed: {exc}", file=sys.stderr)
            return 1
        added, skipped = queue.enqueue([args.identifier], collection=args.collection, tags=args.tag or [])
        res = {
            "status": "queued",
            "identifier": args.identifier,
            "queued_count": len(added),
            "skipped_duplicate": skipped,
            "reason": str(exc),
        }
    _print_output(res, as_json=args.json)
    return 0 if res.get("status") not in {"failed"} else 1


def _collect_batch_identifiers(inputs: list[str]) -> list[str]:
    items: list[str] = []
    for entry in inputs:
        p = Path(entry)
        if p.exists() and p.is_file():
            items.extend(load_identifiers_from_file(p))
        else:
            cleaned = entry.strip()
            if cleaned:
                items.append(cleaned)
    return items


def _wait_for_batch(
    client: PaperBridgeClient, batch_id: str, *, poll_sec: float = 2.0, idle_timeout: float = 320.0
) -> dict[str, Any]:
    processed = None
    deadline = time.monotonic() + idle_timeout
    while True:
        data = client.get_batch(batch_id)
        if data.get("state") in FINISHED_BATCH_STATES:
            return data
        if data.get("state") not in ACTIVE_BATCH_STATES:
            raise TransportError(f"Batch {batch_id} stopped in state {data.get('state')}")
        if data.get("processed") != processed:
            processed = data.get("processed")
            deadline = time.monotonic() + idle_timeout
        if time.monotonic() >= deadline:
            raise TransportError(f"Batch {batch_id} has made no progress within {idle_timeout:g} seconds")
        time.sleep(poll_sec)


def cmd_batch(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    identifiers = _collect_batch_identifiers(args.inputs)
    if not identifiers:
        print("[ERROR] No paper identifiers found in inputs.", file=sys.stderr)
        return 1

    queue = OfflineQueueStore(cfg.queue_path)
    client = PaperBridgeClient(cfg)
    ok, err = _ingest_ready(client)
    if not ok:
        if args.no_queue:
            print(f"[ERROR] Local helper unreachable ({err})", file=sys.stderr)
            return 1
        added, skipped = queue.enqueue(identifiers, collection=args.collection, tags=args.tag or [])
        res = {
            "status": "queued",
            "total_input": len(identifiers),
            "queued_count": len(added),
            "skipped_duplicate": skipped,
            "collection": args.collection or cfg.default_collection,
            "reason": f"local_helper_offline: {err}",
            "next_step": _queued_next_step(err),
        }
        _print_output(res, as_json=args.json)
        return 0

    try:
        submitted = client.submit_batch(identifiers, collection=args.collection, tags=args.tag or [])
        if args.wait and submitted.get("batch_id"):
            final = _wait_for_batch(client, str(submitted["batch_id"]))
            _print_output(final, as_json=args.json)
            return 1 if final.get("state") == "completed_with_errors" else 0
        else:
            _print_output(submitted, as_json=args.json)
        return 0
    except (HTTPStatusError, TransportError) as exc:
        print(f"[ERROR] Batch submission failed: {exc}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    client = PaperBridgeClient(cfg)
    try:
        if args.open_manual:
            res = client.open_manual(args.batch_id)
        else:
            res = client.get_batch(args.batch_id)
    except (HTTPStatusError, TransportError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    _print_output(res, as_json=args.json)
    return 0


def cmd_scan_inbox(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    client = PaperBridgeClient(cfg)
    try:
        res = client.scan_inbox(batch_id=args.batch_id)
    except (HTTPStatusError, TransportError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    _print_output(res, as_json=args.json)
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    queue = OfflineQueueStore(cfg.queue_path)
    if args.queue_cmd == "list":
        entries = [entry for entry in queue.load_all() if args.all or entry.status != "done"]
        _print_output([e.to_dict() for e in entries], as_json=True)
        return 0
    if args.queue_cmd == "clear":
        removed = queue.clear(only_completed=not args.all)
        _print_output({"removed": removed, "cleared_all": bool(args.all)}, as_json=True)
        return 0
    if args.queue_cmd == "retry":
        if not queue.retry(args.task_id):
            print(
                f"[ERROR] Queue task {args.task_id} is missing or cannot be retried. "
                "Eligible statuses are failed, needs_attention, and submitted.",
                file=sys.stderr,
            )
            return 1
        _print_output({"task_id": args.task_id, "status": "pending"}, as_json=True)
        return 0
    return 1


def cmd_drain(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    queue = OfflineQueueStore(cfg.queue_path)
    try:
        with queue.drain_lock():
            rows = queue.load_all()
            work = [
                entry for entry in rows
                if entry.status in {"pending", "submitted"}
                or (entry.status == "needs_attention" and (entry.last_result or {}).get("status") == "needs_manual"
                    and (entry.last_result or {}).get("manual_task_id"))
            ]
            if not work:
                outstanding = Counter(entry.status for entry in rows if entry.status != "done")
                _print_output(
                    {"status": "needs_attention" if outstanding else "empty", "drained": 0, "counts": dict(outstanding)},
                    as_json=args.json,
                )
                return 1 if outstanding else 0
            client = PaperBridgeClient(cfg)
            helper, error = _ingest_ready(client)
            if helper is None:
                raise TransportError(f"Cannot drain queue: {error}")
            return _drain_batches(queue, client, work, args, durable=bool(helper.get("durable_batches")))
    except (HTTPStatusError, TransportError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


def _drain_batches(
    queue: OfflineQueueStore, client: PaperBridgeClient, entries: list[QueueEntry], args: argparse.Namespace,
    *, durable: bool,
) -> int:
    submitted: dict[tuple[str, str, bool], list[QueueEntry]] = defaultdict(list)
    pending: dict[tuple[str | None, tuple[str, ...]], list[QueueEntry]] = defaultdict(list)
    manual: dict[str, list[QueueEntry]] = defaultdict(list)
    for entry in entries:
        previous = entry.last_result or {}
        batch_id = previous.get("batch_id")
        if entry.status == "needs_attention":
            if batch_id:
                manual[batch_id].append(entry)
        elif entry.status == "submitted" and batch_id:
            submitted[(previous.get("request_id") or batch_id, batch_id, bool(previous.get("durable")))].append(entry)
        else:
            pending[(entry.collection, tuple(sorted(entry.tags)))].append(entry)

    dispatches: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    batches = [(*key, group) for key, group in submitted.items()]
    batches.extend((f"batch-{uuid.uuid4().hex}", None, durable, group) for group in pending.values())
    for request_id, batch_id, recoverable, group in batches:
        dispatch = {"mode": "batch", "batch_id": batch_id or request_id}
        try:
            data = None
            if batch_id:
                try:
                    data = client.get_batch(batch_id)
                except HTTPStatusError as exc:
                    if exc.status != 404:
                        raise
                    if not (recoverable and durable):
                        reason = "batch_status_unavailable_on_legacy_helper"
                        failure = {"batch_id": batch_id, "reason": reason}
                        queue.update_results({entry.task_id: ("failed", failure) for entry in group})
                        counts["failed"] += len(group)
                        dispatches.append({**dispatch, "error": reason})
                        continue
            reconciling = data is not None
            if data is None:
                # A durable request ID can reconcile a lost acknowledgement on the next drain.
                plan = {"batch_id": request_id, "request_id": request_id, "durable": durable}
                if durable:
                    queue.update_results({entry.task_id: ("submitted", plan) for entry in group}, count_attempt=True)
                try:
                    data = client.submit_batch(
                        [entry.identifier for entry in group],
                        collection=group[0].collection,
                        tags=sorted(group[0].tags),
                        request_id=request_id,
                    )
                except HTTPStatusError as exc:
                    if not 400 <= exc.status < 500:
                        raise
                    failure = {**plan, "reason": str(exc), "http_status": exc.status}
                    queue.update_results(
                        {entry.task_id: ("failed", failure) for entry in group}, count_attempt=not durable,
                    )
                    counts["failed"] += len(group)
                    dispatches.append({**dispatch, "error": str(exc)})
                    continue
                batch_id = data.get("batch_id")
                if not batch_id:
                    raise TransportError(f"Missing batch ID after submitting {request_id}")
                dispatch["batch_id"] = batch_id
                receipt = {**plan, "batch_id": batch_id}
                queue.update_results(
                    {entry.task_id: ("submitted", receipt) for entry in group}, count_attempt=not durable,
                )
                if data.get("state") in FINISHED_BATCH_STATES and "results" not in data:
                    data = client.get_batch(batch_id)
            if args.wait and data.get("state") in ACTIVE_BATCH_STATES:
                data = _wait_for_batch(client, batch_id)

            if data.get("state") in FINISHED_BATCH_STATES:
                results = data.get("results") or []
                identifiers = [entry.identifier for entry in group]
                if reconciling:
                    # An individually retried task may have left the original batch.
                    remaining = set(identifiers)
                    results = [result for result in results if result.get("identifier") in remaining]
                if [result.get("identifier") for result in results] != identifiers:
                    raise TransportError(f"Batch {batch_id} results do not match the queued papers")
                updates = {
                    entry.task_id: (ingest_outcome(result), {**result, "batch_id": batch_id})
                    for entry, result in zip(group, results)
                }
                queue.update_results(updates)
                counts.update(status for status, _ in updates.values())
            elif data.get("state") in ACTIVE_BATCH_STATES:
                counts["submitted"] += len(group)
            else:
                raise TransportError(f"Batch {batch_id} stopped in state {data.get('state')}")
        except (HTTPStatusError, TransportError) as exc:
            counts["errors"] += len(group)
            dispatches.append({**dispatch, "error": str(exc)})
        else:
            dispatches.append({"mode": "batch", "batch": data})

    for batch_id, group in manual.items():
        try:
            data = client.get_batch(batch_id)
        except (HTTPStatusError, TransportError) as exc:
            status = "needs_attention" if isinstance(exc, HTTPStatusError) and exc.status == 404 else "errors"
            counts[status] += len(group)
            dispatches.append({"mode": "manual", "batch_id": batch_id, "error": str(exc)})
            continue
        results = {result.get("manual_task_id"): result for result in data.get("results", [])
                   if result.get("manual_task_id")}
        updates = {}
        for entry in group:
            previous = entry.last_result or {}
            result = results.get(previous.get("manual_task_id"), previous)
            status = ingest_outcome(result)
            counts[status] += 1
            if result != previous:
                updates[entry.task_id] = (status, {**result, "batch_id": batch_id})
        if updates:
            queue.update_results(updates)

    needs_attention = counts["failed"] + counts["needs_attention"] + counts["errors"]
    if needs_attention:
        status = "needs_attention"
    elif counts["submitted"]:
        status = "submitted"
    else:
        status = "drained"
    _print_output(
        {"status": status, "drained_count": counts["done"], "counts": dict(counts), "dispatches": dispatches},
        as_json=args.json,
    )
    return 1 if needs_attention else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paper-bridge", description="CLI for local Zotero 7 literature ingestion")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize ~/.config/paper-bridge, token, and socket directory")
    p_init.add_argument("--show-token", action="store_true", help="Print the Bearer token to stdout")
    p_init.set_defaults(func=cmd_init)

    p_health = sub.add_parser("health", help="Check local helper daemon, Zotero 7, and offline queue status")
    p_health.add_argument("--json", action="store_true")
    p_health.add_argument("--fulltext-key", help="Probe a known indexed attachment via the private Zotero read socket")
    p_health.add_argument("--webdav-file-url", help="Probe a known existing WebDAV file")
    p_health.add_argument("--webdav-user")
    p_health.add_argument("--webdav-password-file", type=Path)
    p_health.add_argument("--probe-timeout", type=float, default=20.0, help="Timeout in seconds for optional read probes")
    p_health.set_defaults(func=cmd_health)

    p_cols = sub.add_parser("collections", help="List available collections in local Zotero")
    p_cols.add_argument("--json", action="store_true")
    p_cols.set_defaults(func=cmd_collections)

    p_add = sub.add_parser("add", help="Download and ingest a single paper (auto-queues if helper/Zotero is offline)")
    p_add.add_argument("identifier", help="DOI, IEEE/arXiv URL, arXiv ID, or paper title")
    p_add.add_argument("--collection", default=None, help="Target Zotero collection name")
    p_add.add_argument("--tag", action="append", default=[], help="Extra Zotero tag (repeatable)")
    p_add.add_argument("--no-wait", action="store_true", help="Do not wait for manual browser handoff if blocked")
    p_add.add_argument("--timeout", type=int, default=300, help="Seconds to wait for single browser handoff")
    p_add.add_argument("--no-queue", action="store_true", help="Fail instead of queuing when helper/Zotero is offline")
    p_add.add_argument("--json", action="store_true")
    p_add.set_defaults(func=cmd_add)

    p_batch = sub.add_parser("batch", help="Ingest multiple papers from .bib file, text list, or CLI args")
    p_batch.add_argument("inputs", nargs="+", help="Paths to .bib/.txt files or raw identifiers")
    p_batch.add_argument("--collection", default=None)
    p_batch.add_argument("--tag", action="append", default=[])
    p_batch.add_argument("--wait", action="store_true", help="Poll until the serial batch finishes")
    p_batch.add_argument("--no-queue", action="store_true")
    p_batch.add_argument("--json", action="store_true")
    p_batch.set_defaults(func=cmd_batch)

    p_status = sub.add_parser("status", help="Inspect a batch job status or open its manual checklist")
    p_status.add_argument("batch_id")
    p_status.add_argument("--open-manual", action="store_true")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_scan = sub.add_parser("scan-inbox", help="Scan paper-bridge-inbox folder and attach manually downloaded PDFs")
    p_scan.add_argument("--batch-id", default=None)
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=cmd_scan_inbox)

    p_queue = sub.add_parser("queue", help="Inspect or clean the local offline queue")
    q_sub = p_queue.add_subparsers(dest="queue_cmd", required=True)
    p_ql = q_sub.add_parser("list", help="List queued items")
    p_ql.add_argument("--all", action="store_true", help="Include completed items")
    p_qc = q_sub.add_parser("clear", help="Clear completed items (or --all)")
    p_qc.add_argument("--all", action="store_true")
    p_retry = q_sub.add_parser(
        "retry", description="Requeue a failed, needs_attention, or submitted task.",
        help="Requeue a failed, needs_attention, or submitted task",
    )
    p_retry.add_argument("task_id")
    p_queue.set_defaults(func=cmd_queue)

    p_drain = sub.add_parser("drain", help="Submit all pending offline queue items to the local helper")
    p_drain.add_argument("--wait", action="store_true")
    p_drain.add_argument("--json", action="store_true")
    p_drain.set_defaults(func=cmd_drain)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        if isinstance(exc, BlockingIOError) and (
            args.cmd == "drain" or (args.cmd == "queue" and args.queue_cmd in {"clear", "retry"})
        ):
            if args.cmd == "drain" and args.json:
                _print_output({"status": "busy", "error": DRAIN_BUSY_ERROR}, as_json=True)
            else:
                print(f"[ERROR] {DRAIN_BUSY_ERROR}", file=sys.stderr)
        elif args.cmd == "health" and args.json:
            _print_output({"error": str(exc)}, as_json=True)
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
