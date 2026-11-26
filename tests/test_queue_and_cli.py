"""Tests for the helper CLI, offline queue, and end-to-end server drain."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_fetcher_and_engine import FakeFetcher, FakeResolver
from test_zotero_and_proxy import MockZoteroState, start_mock_zotero

from paper_bridge.client import cli as client_cli
from paper_bridge.client.config import ClientHelperConfig
from paper_bridge.client.engine import BatchRecord, PaperBridgeEngine
from paper_bridge.client.server import build_helper_http_server
from paper_bridge.client.zotero_local import ZoteroLocalClient
from paper_bridge.common.models import IngestResult
from paper_bridge.common.transport import HTTPStatusError, TransportError
from paper_bridge.server import cli as server_cli
from paper_bridge.server import diagnostics
from paper_bridge.server.queue_store import OfflineQueueStore


@pytest.mark.skipif(os.name != "posix", reason="POSIX token mode")
@pytest.mark.parametrize("replace_existing", [False, True])
def test_helper_init_explicit_token_is_private(tmp_path, replace_existing) -> None:
    token_path = tmp_path / "token"
    if replace_existing:
        token_path.write_text("old-token\n", encoding="utf-8")
        token_path.chmod(0o644)

    assert client_cli.main(["--config-dir", str(tmp_path), "init", "--token", " shared-token-for-testing "]) == 0

    assert token_path.read_text(encoding="utf-8") == "shared-token-for-testing\n"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("configured_host", ["127.0.0.1", "0.0.0.0"])
def test_helper_serve_rejects_non_loopback_before_starting_proxy(tmp_path, monkeypatch, configured_host) -> None:
    (tmp_path / "config.toml").write_text(f'listen_host = "{configured_host}"\n', encoding="utf-8")
    start_proxy = Mock()
    monkeypatch.setattr(client_cli, "start_readonly_proxy_thread", start_proxy)
    override = "0.0.0.0" if configured_host == "127.0.0.1" else "127.0.0.1"

    with pytest.raises(ValueError, match="listen_host must be a loopback host"):
        client_cli.main(["--config-dir", str(tmp_path), "serve", "--host", override])

    start_proxy.assert_not_called()


def test_helper_serve_overrides_api_and_proxy_ports(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_CLIENT_CONFIG_DIR", str(tmp_path))
    token = "test-token-for-helper"
    (tmp_path / "token").write_text(token, encoding="utf-8")
    (tmp_path / "config.toml").write_text("listen_port = 18765\nzotero_ro_proxy_port = 23120\n", encoding="utf-8")
    helper = Mock(server_address=("127.0.0.1", 19765))
    build_helper = Mock(return_value=helper)
    start_proxy = Mock(return_value=(Mock(), None))
    monkeypatch.setattr(client_cli, "build_helper_http_server", build_helper)
    monkeypatch.setattr(client_cli, "start_readonly_proxy_thread", start_proxy)
    monkeypatch.setattr(PaperBridgeEngine, "health", lambda _: {
        "zotero_alive": False, "zotero_local_api_ready": False, "collections": [],
    })

    assert client_cli.main(["serve", "--port", "19765", "--ro-proxy-port", "24120"]) == 0

    (engine,) = build_helper.call_args.args
    assert engine.config.listen_port == 19765  # Used by the manual checklist URLs.
    assert engine.config.zotero_ro_proxy_port == 24120
    build_helper.assert_called_once_with(engine, token=token, host="127.0.0.1", port=19765)
    start_proxy.assert_called_once_with("127.0.0.1", 24120, "http://127.0.0.1:23119")


def test_helper_serve_unix_socket_mode_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_CLIENT_CONFIG_DIR", str(tmp_path))
    token = "test-token-for-helper"
    (tmp_path / "token").write_text(token, encoding="utf-8")
    helper_sock = tmp_path / "run" / "helper.sock"
    ro_sock = tmp_path / "run" / "zotero-read.sock"
    helper = Mock()
    build_helper = Mock(return_value=helper)
    start_proxy = Mock(return_value=(Mock(), None))
    monkeypatch.setattr(client_cli, "build_helper_http_server", build_helper)
    monkeypatch.setattr(client_cli, "start_readonly_proxy_thread", start_proxy)
    monkeypatch.setattr(PaperBridgeEngine, "health", lambda _: {
        "zotero_alive": True, "zotero_local_api_ready": True, "collections": [],
    })

    rc = client_cli.main([
        "serve",
        "--socket", str(helper_sock),
        "--ro-proxy-socket", str(ro_sock),
        "--zotero-url", "http://127.0.0.1:23137/",
        "--no-browser",
    ])
    assert rc == 0

    (engine,) = build_helper.call_args.args
    assert engine.config.zotero_base_url == "http://127.0.0.1:23137"
    assert engine.config.open_browser_on_single_manual is False
    assert engine.config.listen_socket == helper_sock
    assert engine.config.zotero_ro_proxy_socket == ro_sock
    build_helper.assert_called_once_with(engine, token=token, socket_path=helper_sock)
    start_proxy.assert_called_once_with("127.0.0.1", 23120, "http://127.0.0.1:23137", socket_path=ro_sock)


@pytest.mark.parametrize("cmd", ["add", "batch"])
def test_cli_queues_with_drain_instructions_when_zotero_is_offline(tmp_path, monkeypatch, capsys, cmd) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_DATA_DIR", str(tmp_path))
    client = Mock()
    client.is_reachable.return_value = (True, {"zotero_alive": False}, "")
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)

    assert server_cli.main([cmd, "2401.11111", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "queued"
    assert "Zotero Desktop is offline" in result["reason"]
    assert "Open Zotero Desktop" in result["next_step"]
    assert "paper-bridge drain --wait" in result["next_step"]
    assert [entry.identifier for entry in OfflineQueueStore(tmp_path / "queue.jsonl").load_all()] == ["2401.11111"]
    client.ingest.assert_not_called()
    client.submit_batch.assert_not_called()


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


def test_concurrent_enqueues_keep_both_tasks(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    barrier = threading.Barrier(2)

    def enqueue(identifier: str) -> None:
        barrier.wait(timeout=3)
        OfflineQueueStore(path).enqueue([identifier])

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(enqueue, ["2401.11111", "2401.22222"]))

    assert {entry.identifier for entry in OfflineQueueStore(path).load_all()} == {"2401.11111", "2401.22222"}


@pytest.mark.parametrize("status", ["failed", "needs_attention", "submitted"])
def test_queue_retry_requeues_task(tmp_path, monkeypatch, capsys, status) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_DATA_DIR", str(tmp_path))
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    tasks, _ = queue.enqueue(["2401.11111", "2401.22222"])
    task, other = tasks
    last_result = {"batch_id": "previous-batch"}
    queue.update_results({task.task_id: (status, last_result)}, count_attempt=True)

    assert server_cli.main(["queue", "retry", task.task_id]) == 0

    assert json.loads(capsys.readouterr().out) == {"task_id": task.task_id, "status": "pending"}
    retried, untouched = queue.load_all()
    assert (retried.status, retried.attempts, retried.last_result) == ("pending", 1, last_result)
    assert untouched == other


@pytest.mark.parametrize(
    "args", [("drain", "--json"), ("drain",), ("queue", "clear"), ("queue", "retry", "task-1")]
)
def test_cli_reports_drain_lock_contention(tmp_path, monkeypatch, capsys, args) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_DATA_DIR", str(tmp_path))
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")

    with queue.drain_lock():
        assert server_cli.main(list(args)) == 1

    output = capsys.readouterr()
    message = "Another paper-bridge drain is already running; try again shortly."
    if "--json" in args:
        assert json.loads(output.out) == {"status": "busy", "error": message}
        assert not output.err
    else:
        assert message in output.err
        assert not output.out


@pytest.fixture
def healthy_helper(monkeypatch) -> None:
    monkeypatch.setattr(
        server_cli.PaperBridgeClient, "is_reachable", lambda _: (True, {"zotero_alive": True}, "")
    )


def test_health_json_reports_corrupt_queue(tmp_path, monkeypatch, capsys, healthy_helper) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_DATA_DIR", str(tmp_path))
    (tmp_path / "queue.jsonl").write_text('{"task_id":\n', encoding="utf-8")

    assert server_cli.main(["health", "--json"]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["helper"]["zotero_alive"] is True
    assert data["pending_offline_queue"] is None
    assert "Invalid queue record" in data["queue_error"]


@pytest.mark.parametrize("item_key", ["not-a-key", ""])
def test_health_json_reports_invalid_fulltext_key(capsys, healthy_helper, item_key) -> None:
    assert server_cli.main(["health", "--json", "--fulltext-key", item_key]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["helper"]["zotero_alive"] is True
    probe = data["probes"]["mcp_read_socket"]
    assert probe["readable"] is False
    assert "eight-character Zotero item key" in probe["error"]


def test_health_json_reports_missing_webdav_password_file(tmp_path, capsys, healthy_helper) -> None:
    password_file = tmp_path / "missing-password"
    assert server_cli.main([
        "health", "--json", "--webdav-file-url", "http://127.0.0.1/webdav/sample.zip",
        "--webdav-user", "test-user", "--webdav-password-file", str(password_file),
    ]) == 1

    data = json.loads(capsys.readouterr().out)
    assert data["helper"]["zotero_alive"] is True
    probe = data["probes"]["webdav_file"]
    assert probe["readable"] is False
    assert str(password_file) in probe["error"]


@pytest.mark.parametrize("read_url", ["", " http://127.0.0.1:23119/zotero/ "])
def test_health_fulltext_uses_mcp_read_endpoint(tmp_path, monkeypatch, capsys, healthy_helper, read_url) -> None:
    socket_path = tmp_path / "custom-read.sock"
    monkeypatch.setenv("PAPER_BRIDGE_ZOTERO_READ_SOCKET", str(socket_path))
    monkeypatch.setenv("PAPER_BRIDGE_ZOTERO_READ_URL", read_url)
    connection = Mock()
    connection.getresponse.return_value = Mock(status=200, read=Mock(return_value=b'{"content":"indexed"}'))
    connect_unix = Mock(return_value=connection)
    connect_tcp = Mock(return_value=connection)
    monkeypatch.setattr(diagnostics, "UnixHTTPConnection", connect_unix)
    monkeypatch.setattr(diagnostics.http.client, "HTTPConnection", connect_tcp)

    assert server_cli.main(["health", "--json", "--fulltext-key", "ABCDEFGH"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["probes"]["mcp_read_socket"] == {"readable": True, "http_status": 200}
    path = "/api/users/0/items/ABCDEFGH/fulltext"
    if read_url:
        connect_tcp.assert_called_once_with("127.0.0.1", 23119, timeout=20.0)
        connect_unix.assert_not_called()
        path = "/zotero" + path
    else:
        connect_unix.assert_called_once_with(str(socket_path), timeout=20.0)
        connect_tcp.assert_not_called()
    connection.request.assert_called_once()
    assert connection.request.call_args.args == ("GET", path)
    assert connection.request.call_args.kwargs["headers"]["Zotero-API-Version"] == "3"
    connection.close.assert_called_once()


def test_retrying_one_submitted_task_preserves_batch_reconciliation(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PAPER_BRIDGE_DATA_DIR", str(tmp_path))
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    tasks, _ = queue.enqueue(["2401.11111", "2401.22222"])
    receipt = {"batch_id": "previous-batch", "durable": True}
    queue.update_results({task.task_id: ("submitted", receipt) for task in tasks}, count_attempt=True)
    client = Mock()
    client.is_reachable.return_value = (True, {"zotero_alive": True, "durable_batches": True}, "")
    client.get_batch.return_value = {
        "state": "completed",
        "results": [{"identifier": task.identifier, "status": "saved", "pdf_attached": True} for task in tasks],
    }
    client.submit_batch.return_value = {"batch_id": "new-batch", "state": "running"}
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)

    assert server_cli.main(["queue", "retry", tasks[0].task_id]) == 0
    capsys.readouterr()
    assert server_cli.main(["drain", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["counts"] == {"done": 1, "submitted": 1}
    client.get_batch.assert_called_once_with("previous-batch")
    assert client.submit_batch.call_args.args == ([tasks[0].identifier],)
    retried, sibling = queue.load_all()
    assert (retried.status, retried.last_result["batch_id"]) == ("submitted", "new-batch")
    assert (sibling.status, sibling.last_result["batch_id"]) == ("done", "previous-batch")


def test_drain_recovers_missing_ack_and_records_individual_outcomes(tmp_path, monkeypatch, capsys) -> None:
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    queue.enqueue(["2401.11111", "2401.22222"], collection="Inbox")

    class DisconnectingClient:
        accepted: dict | None = None
        requests = 0

        def is_reachable(self):
            return True, {"zotero_alive": True, "durable_batches": True}, ""

        def submit_batch(self, identifiers, *, request_id, **kwargs):
            self.requests += 1
            self.accepted = {
                "batch_id": request_id,
                "state": "completed_with_errors",
                "results": [
                    {"identifier": identifiers[0], "status": "saved", "pdf_attached": True},
                    {"identifier": identifiers[1], "status": "failed"},
                ],
            }
            raise TransportError("Connection closed before the acknowledgement arrived")

        def get_batch(self, batch_id):
            assert self.accepted and self.accepted["batch_id"] == batch_id
            return self.accepted

    client = DisconnectingClient()
    monkeypatch.setattr(server_cli.ServerConfig, "load", lambda: SimpleNamespace(queue_path=queue.path))
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)
    assert server_cli.main(["drain"]) == 1
    assert [row.status for row in queue.load_all()] == ["submitted", "submitted"]
    capsys.readouterr()
    assert server_cli.main(["drain", "--wait", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"] == {"done": 1, "failed": 1}
    first, second = queue.load_all()
    assert first.status == "done"
    assert second.status == "failed"
    assert client.requests == 1
    assert queue.retry_failed(second.task_id)
    assert [row.status for row in queue.load_all()] == ["done", "pending"]


@pytest.mark.parametrize("error", [TransportError("no progress"), HTTPStatusError(503, "unavailable")])
def test_drain_continues_to_later_batch_and_manual_group(tmp_path, monkeypatch, capsys, error) -> None:
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    queue.enqueue(["2401.11111"], collection="First")
    queue.enqueue(["2401.22222"], collection="Second")
    manual, _ = queue.enqueue(["2401.33333"])
    queue.update_results({
        manual[0].task_id: ("needs_attention", {
            "batch_id": "manual-batch", "manual_task_id": "manual-1", "status": "needs_manual",
        }),
    })
    client = Mock()
    client.is_reachable.return_value = (True, {"zotero_alive": True, "durable_batches": True}, "")
    client.submit_batch.side_effect = [
        {"batch_id": "first-batch", "state": "running"},
        {"batch_id": "second-batch", "state": "completed", "results": [
            {"identifier": "2401.22222", "status": "saved", "pdf_attached": True},
        ]},
    ]
    client.get_batch.side_effect = [
        error,
        {"state": "completed", "results": [
            {"manual_task_id": "manual-1", "status": "attached", "pdf_attached": True},
        ]},
    ]
    monkeypatch.setattr(server_cli.ServerConfig, "load", lambda: SimpleNamespace(queue_path=queue.path))
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)

    assert server_cli.main(["drain", "--wait", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["drained_count"] == 2
    assert data["counts"] == {"done": 2, "errors": 1}
    assert data["dispatches"][0]["batch_id"] == "first-batch"
    assert data["dispatches"][0]["error"] == str(error)
    assert [entry.status for entry in queue.load_all()] == ["submitted", "done", "done"]


@pytest.mark.parametrize("durable", [True, False])
def test_drain_marks_rejected_submission_failed(tmp_path, monkeypatch, capsys, durable) -> None:
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    queue.enqueue(["2401.11111", "2401.22222"])
    client = Mock()
    client.is_reachable.return_value = (True, {"zotero_alive": True, "durable_batches": durable}, "")
    client.submit_batch.side_effect = HTTPStatusError(400, "Batch request ID already belongs to different input")
    monkeypatch.setattr(server_cli.ServerConfig, "load", lambda: SimpleNamespace(queue_path=queue.path))
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)

    assert server_cli.main(["drain", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["counts"] == {"failed": 2}
    assert "different input" in data["dispatches"][0]["error"]
    for entry in queue.load_all():
        assert entry.status == "failed"
        assert "different input" in entry.last_result["reason"]
        assert queue.retry_failed(entry.task_id)


def test_drain_retries_legacy_submission_after_connection_failure(tmp_path, monkeypatch, capsys) -> None:
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    queue.enqueue(["2401.11111"])
    client = Mock()
    client.is_reachable.return_value = (True, {"zotero_alive": True}, "")

    def disconnect(*args, **kwargs):
        assert queue.load_all()[0].status == "pending"
        raise TransportError("connection refused")

    client.submit_batch.side_effect = disconnect
    monkeypatch.setattr(server_cli.ServerConfig, "load", lambda: SimpleNamespace(queue_path=queue.path))
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)

    assert server_cli.main(["drain", "--json"]) == 1
    assert "connection refused" in json.loads(capsys.readouterr().out)["dispatches"][0]["error"]
    assert queue.load_all()[0].status == "pending"
    client.submit_batch.side_effect = None
    client.submit_batch.return_value = {"batch_id": "legacy-batch", "state": "running"}
    assert server_cli.main(["drain", "--json"]) == 0
    assert queue.load_all()[0].last_result["batch_id"] == "legacy-batch"
    client.get_batch.assert_not_called()


@pytest.mark.parametrize("outcome", ["needs_manual", "ambiguous", "saved_merge_needed", "saved"])
def test_drain_wait_returns_nonzero_for_papers_needing_attention(tmp_path, monkeypatch, capsys, outcome) -> None:
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    queue.enqueue(["2401.11111"])
    client = Mock()
    client.is_reachable.return_value = (True, {"zotero_alive": True}, "")
    client.submit_batch.return_value = {"batch_id": "batch-0123456789", "state": "running"}
    client.get_batch.return_value = {
        "state": "completed_with_errors",
        "results": [{"identifier": "2401.11111", "status": outcome, "pdf_attached": False}],
    }
    monkeypatch.setattr(server_cli.ServerConfig, "load", lambda: SimpleNamespace(queue_path=queue.path))
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: client)

    for _ in range(2):  # Newly completed batch, then the persisted unresolved queue entry.
        assert server_cli.main(["drain", "--wait", "--json"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "needs_attention"
        assert data["counts"] == {"needs_attention": 1}
    client.submit_batch.assert_called_once()
    client.get_batch.assert_called_once()


def test_drain_keeps_unverifiable_legacy_batch_for_review(tmp_path, monkeypatch, capsys) -> None:
    queue = OfflineQueueStore(tmp_path / "queue.jsonl")
    tasks, _ = queue.enqueue(["2401.11111"])
    queue.update_results({tasks[0].task_id: ("submitted", {"batch_id": "batch-0123456789"})})

    class LegacyClient:
        def is_reachable(self):
            return True, {"zotero_alive": True}, ""

        def get_batch(self, batch_id):
            raise HTTPStatusError(404, "missing legacy state")

        def submit_batch(self, *args, **kwargs):
            raise AssertionError("An unverifiable legacy submission must remain available for manual reconciliation")

    monkeypatch.setattr(server_cli.ServerConfig, "load", lambda: SimpleNamespace(queue_path=queue.path))
    monkeypatch.setattr(server_cli, "PaperBridgeClient", lambda _: LegacyClient())
    assert server_cli.main(["drain", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"failed": 1}
    assert queue.load_all()[0].last_result["reason"] == "batch_status_unavailable_on_legacy_helper"


@pytest.mark.parametrize("restart", [False, True])
def test_batch_resumes_after_interruption(tmp_path, monkeypatch, restart) -> None:
    configuration = ClientHelperConfig.load(tmp_path)
    configuration.batch_delay_min_sec = 0.01
    configuration.batch_delay_max_sec = 0.01
    identifiers = ["2401.11111", "2401.22222"]
    outcomes: list[str] = []

    def ingest(request, **kwargs):
        outcomes.append(request.identifier)
        return IngestResult(request.identifier, "saved", pdf_attached=True), False

    sleep = Mock(side_effect=RuntimeError("simulated worker interruption"))

    def wait_for(engine, batch_id, wanted):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            data = engine.get_batch(batch_id)
            if data and data["state"] == wanted:
                return data
            time.sleep(0.01)
        raise AssertionError(f"Batch {batch_id} did not reach {wanted}")

    first = PaperBridgeEngine(configuration, sleep_fn=sleep, notifier=lambda *_: True)
    monkeypatch.setattr(first, "ingest_one", ingest)
    request_id = "batch-0123456789"
    first.submit_batch(identifiers, request_id=request_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with first._lock:
            if request_id not in first._running_batches:
                break
        time.sleep(0.01)
    else:
        pytest.fail("Initial batch worker did not stop")

    persisted = BatchRecord.from_file(configuration.config_dir / "batches" / f"{request_id}.json")
    assert (persisted.state, persisted.processed) == ("interrupted", 1)
    if restart:
        engine = PaperBridgeEngine(configuration, sleep_fn=lambda _: None, notifier=lambda *_: True)
        monkeypatch.setattr(engine, "ingest_one", ingest)
        assert engine.submit_batch(identifiers, request_id=request_id)["batch_id"] == request_id
    else:
        engine = first  # Polling get_batch must start a new worker on this same engine.
    assert wait_for(engine, request_id, "completed")["processed"] == 2
    assert engine.submit_batch(identifiers, request_id=request_id)["state"] == "completed"
    assert outcomes == identifiers


def test_finishing_save_failure_keeps_terminal_batch_state(tmp_path, monkeypatch) -> None:
    configuration = ClientHelperConfig.load(tmp_path)
    engine = PaperBridgeEngine(configuration, notifier=lambda *_: True)
    record = BatchRecord(batch_id="batch-0123456789")
    monkeypatch.setattr(engine, "_save_batch", Mock(side_effect=OSError("disk full")))

    engine._run_batch_worker(record)

    assert record.state == "completed"


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
    sock_literal = str(s_run_dir / "helper.sock")
    (s_cfg_dir / "config.toml").write_text(
        f"socket_path = {json.dumps(sock_literal)}\ntcp_base_url = \"http://127.0.0.1:1\"\n",
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

    # 3. Start Mock Zotero 7 + Local Helper HTTP server on ephemeral port
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
        monkeypatch.setenv("PAPER_BRIDGE_TCP_URL", f"http://127.0.0.1:{h_port}")

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
