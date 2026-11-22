"""Ubuntu server CLI (`paper-bridge`) for ingesting papers into Windows Zotero via reverse SSH."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..common.identifiers import load_identifiers_from_file
from ..common.security import ensure_token
from ..common.transport import HTTPStatusError, TransportError
from .client import PaperBridgeClient
from .config import ServerConfig
from .queue_store import OfflineQueueStore


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
    queue = OfflineQueueStore(cfg.queue_path)
    pending = len(queue.list_pending())
    client = PaperBridgeClient(cfg)
    ok, data, err = client.is_reachable()
    payload: dict[str, Any] = {
        "reachable": ok,
        "socket_path": str(cfg.socket_path),
        "socket_exists": cfg.socket_path.exists(),
        "tcp_base_url": cfg.tcp_base_url,
        "pending_offline_queue": pending,
    }
    if ok and data:
        payload["helper"] = data
    else:
        payload["error"] = err
    _print_output(payload, as_json=args.json)
    return 0 if ok and (data or {}).get("zotero_alive") else 1


def cmd_collections(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    client = PaperBridgeClient(cfg)
    try:
        health = client.health()
    except (TransportError, HTTPStatusError, FileNotFoundError) as exc:
        print(f"[ERROR] Cannot reach Windows helper: {exc}", file=sys.stderr)
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

    ok, _, err = client.is_reachable()
    if not ok:
        if args.no_queue:
            print(f"[ERROR] Windows helper unreachable ({err})", file=sys.stderr)
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
            "reason": f"client_helper_offline: {err}",
            "next_step": "Start the client tunnel (start-paper-bridge.ps1 on Windows or start-paper-bridge.sh on macOS/Linux) to auto-drain queued papers.",
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


def _wait_for_batch(client: PaperBridgeClient, batch_id: str, *, poll_sec: float = 2.0) -> dict[str, Any]:
    while True:
        data = client.get_batch(batch_id)
        if data.get("state") in {"completed", "completed_with_manual"}:
            return data
        time.sleep(poll_sec)


def cmd_batch(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    identifiers = _collect_batch_identifiers(args.inputs)
    if not identifiers:
        print("[ERROR] No paper identifiers found in inputs.", file=sys.stderr)
        return 1

    queue = OfflineQueueStore(cfg.queue_path)
    client = PaperBridgeClient(cfg)
    ok, _, err = client.is_reachable()
    if not ok:
        if args.no_queue:
            print(f"[ERROR] Windows helper unreachable ({err})", file=sys.stderr)
            return 1
        added, skipped = queue.enqueue(identifiers, collection=args.collection, tags=args.tag or [])
        res = {
            "status": "queued",
            "total_input": len(identifiers),
            "queued_count": len(added),
            "skipped_duplicate": skipped,
            "collection": args.collection or cfg.default_collection,
            "reason": f"client_helper_offline: {err}",
        }
        _print_output(res, as_json=args.json)
        return 0

    try:
        submitted = client.submit_batch(identifiers, collection=args.collection, tags=args.tag or [])
        if args.wait and submitted.get("batch_id"):
            final = _wait_for_batch(client, str(submitted["batch_id"]))
            _print_output(final, as_json=args.json)
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
        entries = queue.load_all() if args.all else queue.list_pending()
        _print_output([e.to_dict() for e in entries], as_json=True)
        return 0
    if args.queue_cmd == "clear":
        removed = queue.clear(only_completed=not args.all)
        _print_output({"removed": removed, "cleared_all": bool(args.all)}, as_json=True)
        return 0
    return 1


def cmd_drain(args: argparse.Namespace) -> int:
    cfg = ServerConfig.load()
    queue = OfflineQueueStore(cfg.queue_path)
    pending = queue.list_pending()
    if not pending:
        _print_output({"status": "empty", "drained": 0}, as_json=args.json)
        return 0

    client = PaperBridgeClient(cfg)
    ok, _, err = client.is_reachable()
    if not ok:
        print(f"[ERROR] Cannot drain queue; Windows helper unreachable: {err}", file=sys.stderr)
        return 1

    # Group pending tasks by (collection, tuple(sorted(tags)))
    groups: dict[tuple[str | None, tuple[str, ...]], list[Any]] = defaultdict(list)
    for entry in pending:
        groups[(entry.collection, tuple(sorted(entry.tags)))].append(entry)

    batches_out: list[dict[str, Any]] = []
    for (col, tags_tuple), entries in groups.items():
        ids = [e.identifier for e in entries]
        task_ids = {e.task_id for e in entries}
        if len(ids) == 1:
            res = client.ingest(
                ids[0],
                collection=col,
                tags=list(tags_tuple),
                mode="batch",  # non-blocking during auto-drain on tunnel startup
                wait_for_manual=False,
            )
            queue.update_status(task_ids, status="done", last_result=res)
            batches_out.append({"mode": "single", "result": res})
        else:
            submitted = client.submit_batch(ids, collection=col, tags=list(tags_tuple))
            if args.wait and submitted.get("batch_id"):
                final = _wait_for_batch(client, str(submitted["batch_id"]))
                queue.update_status(task_ids, status="done", last_result={"batch_id": submitted["batch_id"], "state": final.get("state")})
                batches_out.append({"mode": "batch", "batch": final})
            else:
                queue.update_status(task_ids, status="submitted", last_result=submitted)
                batches_out.append({"mode": "batch", "batch": submitted})

    _print_output({"status": "drained", "drained_count": len(pending), "dispatches": batches_out}, as_json=args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paper-bridge", description="Server CLI for zotero-paper-bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize ~/.config/paper-bridge, token, and socket directory")
    p_init.add_argument("--show-token", action="store_true", help="Print the Bearer token to stdout")
    p_init.set_defaults(func=cmd_init)

    p_health = sub.add_parser("health", help="Check reverse SSH tunnel, Windows helper, and Zotero status")
    p_health.add_argument("--json", action="store_true")
    p_health.set_defaults(func=cmd_health)

    p_cols = sub.add_parser("collections", help="List available collections in Windows Zotero")
    p_cols.add_argument("--json", action="store_true")
    p_cols.set_defaults(func=cmd_collections)

    p_add = sub.add_parser("add", help="Download and ingest a single paper (auto-queues if Windows is offline)")
    p_add.add_argument("identifier", help="DOI, IEEE/arXiv URL, arXiv ID, or paper title")
    p_add.add_argument("--collection", default=None, help="Target Zotero collection name")
    p_add.add_argument("--tag", action="append", default=[], help="Extra Zotero tag (repeatable)")
    p_add.add_argument("--no-wait", action="store_true", help="Do not wait for manual browser handoff if blocked")
    p_add.add_argument("--timeout", type=int, default=300, help="Seconds to wait for single browser handoff")
    p_add.add_argument("--no-queue", action="store_true", help="Fail instead of queuing when Windows is offline")
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

    p_status = sub.add_parser("status", help="Inspect a batch job status or open its manual checklist on Windows")
    p_status.add_argument("batch_id")
    p_status.add_argument("--open-manual", action="store_true")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_scan = sub.add_parser("scan-inbox", help="Scan Windows paper-bridge-inbox folder and attach downloaded PDFs")
    p_scan.add_argument("--batch-id", default=None)
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=cmd_scan_inbox)

    p_queue = sub.add_parser("queue", help="Inspect or clean the server-side offline queue")
    q_sub = p_queue.add_subparsers(dest="queue_cmd", required=True)
    p_ql = q_sub.add_parser("list", help="List queued items")
    p_ql.add_argument("--all", action="store_true", help="Include completed/submitted items")
    p_qc = q_sub.add_parser("clear", help="Clear completed items (or --all)")
    p_qc.add_argument("--all", action="store_true")
    p_queue.set_defaults(func=cmd_queue)

    p_drain = sub.add_parser("drain", help="Submit all pending offline queue items to Windows helper")
    p_drain.add_argument("--wait", action="store_true")
    p_drain.add_argument("--json", action="store_true")
    p_drain.set_defaults(func=cmd_drain)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
