"""Tests for offline queue idempotency and end-to-end server CLI -> Windows helper drain."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

from test_fetcher_and_engine import FakeFetcher, FakeResolver
from test_zotero_and_proxy import MockZoteroState, start_mock_zotero

from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import PaperBridgeEngine
from paper_bridge.client.server import build_helper_http_server
from paper_bridge.client.zotero_local import ZoteroLocalClient
from paper_bridge.server import cli as server_cli
from paper_bridge.server.queue_store import OfflineQueueStore


def test_offline_queue_idempotency_and_permissions(tmp_path: Path) -> None:
    q_path = tmp_path / "queue.jsonl"
    store = OfflineQueueStore(q_path)

    added, skipped = store.enqueue(
        [
            "10.1109/TWC.2024.111",
            "https://doi.org/10.1109/twc.2024.111",  # duplicate after normalization
            "2401.12345",
        ],
        collection="TWC-Related-Work",
        tags=["baseline"],
    )
    assert len(added) == 2
    assert skipped == 1
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(q_path).st_mode) == 0o600

    # Re-enqueuing same paper into same collection skips it
    added2, skipped2 = store.enqueue(["doi:10.1109/TWC.2024.111"], collection="TWC-Related-Work")
    assert len(added2) == 0
    assert skipped2 == 1


def test_cli_offline_enqueue_then_online_drain(tmp_path: Path, monkeypatch, capsys) -> None:
    # Isolated server config dirs
    s_cfg_dir = tmp_path / "srv_cfg"
    s_data_dir = tmp_path / "srv_data"
    s_run_dir = tmp_path / "srv_run"
    monkeypatch.setenv("PAPER_BRIDGE_CONFIG_DIR", str(s_cfg_dir))
    monkeypatch.setenv("PAPER_BRIDGE_DATA_DIR", str(s_data_dir))
    monkeypatch.setenv("PAPER_BRIDGE_RUN_DIR", str(s_run_dir))

    # 1. Initialize server config + token
    assert server_cli.main(["init"]) == 0
    init_out = json.loads(capsys.readouterr().out)
    token = Path(init_out["token_path"]).read_text(encoding="utf-8").strip()

    # Point server TCP fallback to an unreachable port first -> offline mode
    (s_cfg_dir / "config.toml").write_text(
        f'socket_path = "{s_run_dir / "helper.sock"}"\ntcp_base_url = "http://127.0.0.1:1"\n',
        encoding="utf-8",
    )

    # 2. Offline add -> queued cleanly
    assert server_cli.main(["add", "2401.11111", "--collection", "TWC-Related-Work", "--json"]) == 0
    add_out = json.loads(capsys.readouterr().out)
    assert add_out["status"] == "queued"
    assert add_out["queued_count"] == 1

    # Add a second item offline
    assert server_cli.main(["add", "2401.22222", "--collection", "TWC-Related-Work", "--json"]) == 0
    _ = capsys.readouterr()

    # 3. Start Mock Zotero 7 + Windows Helper HTTP server on ephemeral port
    state = MockZoteroState()
    z_srv, z_url = start_mock_zotero(state)
    w_cfg = ClientHelperConfig.load(tmp_path / "client_cfg")
    w_cfg.zotero_base_url = z_url
    w_cfg.batch_delay_min_sec = 0.0
    w_cfg.batch_delay_max_sec = 0.0
    w_cfg.ensure_initialized()
    w_cfg.token_path.write_text(token + "\n", encoding="utf-8")

    engine = PaperBridgeEngine(
        w_cfg,
        zotero=ZoteroLocalClient(z_url),
        resolver=FakeResolver(),  # type: ignore[arg-type]
        fetcher=FakeFetcher(),  # type: ignore[arg-type]
        browser_opener=lambda _: True,
        notifier=lambda _t, _m: True,
        sleep_fn=lambda _: None,
    )
    h_srv = build_helper_http_server(engine, token=token, host="127.0.0.1", port=0)
    h_port = h_srv.server_address[1]
    t = threading.Thread(target=h_srv.serve_forever, daemon=True)
    t.start()
    try:
        (s_cfg_dir / "config.toml").write_text(
            f'socket_path = "{s_run_dir / "helper.sock"}"\ntcp_base_url = "http://127.0.0.1:{h_port}"\n',
            encoding="utf-8",
        )

        # 4. Health check now sees live helper + Zotero + 2 pending items
        assert server_cli.main(["health", "--json"]) == 0
        health_out = json.loads(capsys.readouterr().out)
        assert health_out["reachable"] is True
        assert health_out["helper"]["zotero_alive"] is True
        assert health_out["pending_offline_queue"] == 2

        # 5. Drain queue -> ingests both arXiv items into Mock Zotero!
        assert server_cli.main(["drain", "--wait", "--json"]) == 0
        drain_out = json.loads(capsys.readouterr().out)
        assert drain_out["status"] == "drained"
        assert drain_out["drained_count"] == 2
        assert len(state.items) == 2

        # 6. Queue is now empty
        assert server_cli.main(["queue", "list"]) == 0
        assert json.loads(capsys.readouterr().out) == []

        # 7. Collections command works
        assert server_cli.main(["collections"]) == 0
        cols_text = capsys.readouterr().out
        assert "Agent-Inbox" in cols_text
        assert "TWC-Related-Work" in cols_text
    finally:
        h_srv.shutdown()
        h_srv.server_close()
        z_srv.shutdown()
        z_srv.server_close()
