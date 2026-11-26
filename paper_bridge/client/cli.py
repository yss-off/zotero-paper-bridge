"""CLI entrypoint for the local Zotero desktop helper (`paper-bridge-helper`)."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from ..common.models import Author, PaperMetadata
from ..common.security import ensure_token, is_loopback_host, load_token, write_token
from .config import ClientHelperConfig
from .engine import PaperBridgeEngine
from .server import build_helper_http_server
from .zotero_local import ZoteroLocalClient
from .zotero_ro_proxy import start_readonly_proxy_thread

MINIMAL_TEST_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R >> endobj\n"
    b"4 0 obj << /Length 44 >> stream\nBT /F1 12 Tf 20 72 Td (Paper Bridge Smoke Test) Tj ET\nendstream endobj\n"
    b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
    b"0000000115 00000 n \n0000000201 00000 n \n"
    b"trailer << /Size 5 /Root 1 0 R >>\nstartxref\n295\n%%EOF\n"
) + (b" " * 1024)


def cmd_init(args: argparse.Namespace) -> int:
    cfg = ClientHelperConfig.load(Path(args.config_dir) if args.config_dir else None)
    cfg.ensure_initialized()
    if args.token:
        token = args.token.strip()
        write_token(cfg.token_path, token)
        created = True
    else:
        token, created = ensure_token(cfg.token_path)

    print(f"[OK] Config file : {cfg.config_path}")
    print(f"[OK] Inbox folder: {cfg.inbox_dir}")
    print(f"[OK] Token file  : {cfg.token_path} ({'written' if created else 'existing'})")
    if args.print_token:
        print(f"TOKEN={token}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = ClientHelperConfig.load(Path(args.config_dir) if args.config_dir else None)
    if args.socket:
        listen_socket: Path | None = Path(args.socket).expanduser()
    elif args.port is not None or args.host is not None:
        listen_socket = None
    else:
        listen_socket = cfg.listen_socket

    if args.ro_proxy_socket:
        ro_proxy_socket: Path | None = Path(args.ro_proxy_socket).expanduser()
    elif args.ro_proxy_port is not None:
        ro_proxy_socket = None
    else:
        ro_proxy_socket = cfg.zotero_ro_proxy_socket

    listen_host = cfg.listen_host if args.host is None else args.host
    if listen_socket is None or args.host is not None or (
        cfg.enable_zotero_ro_proxy and not args.no_ro_proxy and ro_proxy_socket is None
    ):
        for host in (cfg.listen_host, listen_host):
            if not is_loopback_host(host):
                raise ValueError(f"listen_host must be a loopback host, got {host!r}")
    cfg.ensure_initialized()
    if args.port is not None:
        cfg.listen_port = args.port
    if args.ro_proxy_port is not None:
        cfg.zotero_ro_proxy_port = args.ro_proxy_port
    if args.zotero_url:
        cfg.zotero_base_url = args.zotero_url.rstrip("/")
    if args.no_browser:
        cfg.open_browser_on_single_manual = False
    cfg.listen_socket = listen_socket
    cfg.zotero_ro_proxy_socket = ro_proxy_socket

    token = load_token(cfg.token_path)
    engine = PaperBridgeEngine(cfg)

    ro_server = None
    if cfg.enable_zotero_ro_proxy and not args.no_ro_proxy:
        if ro_proxy_socket is not None:
            ro_server, _ = start_readonly_proxy_thread(
                cfg.listen_host,
                cfg.zotero_ro_proxy_port,
                cfg.zotero_base_url,
                socket_path=ro_proxy_socket,
            )
            ro_endpoint = f"unix://{ro_proxy_socket}"
        else:
            ro_server, _ = start_readonly_proxy_thread(
                cfg.listen_host, cfg.zotero_ro_proxy_port, cfg.zotero_base_url
            )
            ro_endpoint = f"http://{cfg.listen_host}:{cfg.zotero_ro_proxy_port}"
        print(
            f"[paper-bridge-helper] Read-only Zotero proxy listening on "
            f"{ro_endpoint} -> {cfg.zotero_base_url} (GET /api/* only)",
            flush=True,
        )

    if listen_socket is not None:
        server = build_helper_http_server(
            engine,
            token=token,
            socket_path=listen_socket,
        )
        helper_endpoint = f"unix://{listen_socket}"
    else:
        server = build_helper_http_server(
            engine,
            token=token,
            host=listen_host,
            port=cfg.listen_port,
        )
        if cfg.listen_port == 0:
            cfg.listen_port = server.server_address[1]
        helper_endpoint = f"http://{server.server_address[0]}:{server.server_address[1]}"

    h = engine.health()
    print(
        f"[paper-bridge-helper] Helper API listening on {helper_endpoint} "
        f"(zotero_alive={h['zotero_alive']}, local_api={h['zotero_local_api_ready']}, "
        f"collections={len(h['collections'])})",
        flush=True,
    )

    prev_sigterm = None
    if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
        def _handle_sigterm(_signum: int, _frame: object) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()

        prev_sigterm = signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[paper-bridge-helper] Shutting down...", flush=True)
    finally:
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        server.shutdown()
        server.server_close()
        if ro_server is not None:
            ro_server.shutdown()
            ro_server.server_close()
    return 0


def cmd_smoke_test(args: argparse.Namespace) -> int:
    """Phase 1 live verification against your local Zotero 7 Desktop."""
    cfg = ClientHelperConfig.load(Path(args.config_dir) if args.config_dir else None)
    if args.zotero_url:
        cfg.zotero_base_url = args.zotero_url.rstrip("/")
    zotero = ZoteroLocalClient(cfg.zotero_base_url)
    report: dict[str, object] = {"zotero_base_url": cfg.zotero_base_url}

    alive = zotero.ping()
    report["connector_ping"] = alive
    if not alive:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("[FAIL] Zotero Desktop is not reachable on 23119. Open Zotero 7 first.", file=sys.stderr)
        return 1

    local_ready = zotero.local_api_ready()
    report["local_api_ready"] = local_ready
    collections = zotero.list_collections()
    report["collections"] = [
        {"name": c.name, "connector_target_id": c.connector_target_id, "api_key": c.api_key}
        for c in collections
    ]
    report["has_default_collection"] = any(
        c.name.lower() == cfg.default_collection.lower() for c in collections
    )

    if args.write_test:
        target_col = args.collection or cfg.default_collection
        meta = PaperMetadata(
            title="Zotero Paper Bridge Phase-1 Smoke Test Item (Safe to Delete)",
            item_type="journalArticle",
            creators=[Author(first_name="Paper", last_name="Bridge")],
            doi="10.0000/paper-bridge.smoke-test",
            url="https://example.org/paper-bridge-smoke-test",
            publication_title="Paper Bridge Diagnostics",
            date="2026",
            source="smoke_test",
        )
        saved = zotero.save_item(
            meta,
            pdf_bytes=MINIMAL_TEST_PDF,
            pdf_filename="paper-bridge-smoke-test.pdf",
            pdf_url="https://example.org/paper-bridge-smoke-test.pdf",
            collection_name=target_col,
            default_collection=cfg.default_collection,
            tags=["source:agent", "smoke-test"],
        )
        report["write_test"] = saved

        # Also test attaching to a newly saved metadata-only item in same session vs cross-session
        if args.test_cross_session_attach and saved.get("item_key"):
            fresh_client = ZoteroLocalClient(cfg.zotero_base_url)
            ok, err = fresh_client._upload_attachment(  # noqa: SLF001
                session_id="pb-cross-probe",
                parent_item_id=str(saved["item_key"]),
                pdf_bytes=MINIMAL_TEST_PDF,
                filename="cross-session-probe.pdf",
                pdf_url="https://example.org/cross-session-probe.pdf",
            )
            report["cross_session_attach_supported"] = ok
            report["cross_session_attach_detail"] = err

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not local_ready:
        print(
            "[WARN] Zotero Local Read API is OFF. Enable: Zotero Settings -> Advanced -> "
            "'Allow other applications on this computer to communicate with Zotero'.",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="paper-bridge-helper",
        description="Local Zotero 7 Paper Bridge Helper Daemon",
    )
    p.add_argument("--config-dir", default=None, help="Override config directory (default: ~/.paper-bridge)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize local config, inbox directory, and Bearer token")
    p_init.add_argument("--token", default=None, help="Set explicit shared Bearer token from ~/.config/paper-bridge/token")
    p_init.add_argument("--print-token", action="store_true", help="Print the token to stdout")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="Run the local helper daemon and read-only Zotero proxy")
    p_serve.add_argument("--host", default=None, help="Bind Helper API on a loopback TCP host instead of Unix socket")
    p_serve.add_argument("--port", type=int, help="Bind Helper API on a loopback TCP port instead of Unix socket")
    p_serve.add_argument("--socket", default=None, help="Bind Helper API on a private POSIX Unix Domain Socket")
    p_serve.add_argument("--ro-proxy-port", type=int, help="Bind read-only Zotero proxy on a loopback TCP port")
    p_serve.add_argument("--ro-proxy-socket", default=None, help="Bind read-only Zotero proxy on a private POSIX Unix Domain Socket")
    p_serve.add_argument("--zotero-url", default=None, help="Override local Zotero base URL (e.g. http://127.0.0.1:23137)")
    p_serve.add_argument("--no-browser", action="store_true", help="Disable interactive browser handoff wait")
    p_serve.add_argument("--no-ro-proxy", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_smoke = sub.add_parser("smoke-test", help="Verify Zotero 7 Local API and Connector write behavior")
    p_smoke.add_argument("--zotero-url", default=None, help="Override local Zotero base URL")
    p_smoke.add_argument("--write-test", action="store_true", help="Create a tagged smoke-test item with PDF in Zotero")
    p_smoke.add_argument("--collection", default=None, help="Target collection for --write-test")
    p_smoke.add_argument(
        "--test-cross-session-attach",
        action="store_true",
        help="Probe whether this Zotero 7 build supports attaching a PDF across Connector sessions",
    )
    p_smoke.set_defaults(func=cmd_smoke_test)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
