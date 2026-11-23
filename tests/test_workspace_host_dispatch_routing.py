"""Regression coverage at the durable Workspace acceptance and recovery boundary."""
import os
from unittest.mock import patch

import pytest

from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.host_instances import HostInstanceConflict, LEGACY_INSTANCE_ID
from agentbridge.workspace.stores import WorkspaceConflictError
from tests.test_workspace import FakeGateway, _create_account, _service


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    key = tmp_path / "session.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(key))
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
    app = WorkspaceApplication(service=service, gateway=FakeGateway())
    account_id = account["account_id"]
    directory = app.hosts
    assert directory.choices(account_id)["default_instance_id"] == LEGACY_INSTANCE_ID
    directory.save_instance("office", host_type="nanobot", display_name="办公实例", route_ref="route-office",
                            adapter_version="0.2.0", state="enabled", subjects=["user-a"],
                            expected_revision=0, actor="admin", reason="fixture")
    endpoint, _ = service.tasks.ensure_endpoint(user_subject="user-a", agent_host="nanobot", account_id=account_id,
        token_id="fixture", client_type="web", external_subject=account_id, endpoint_key="office:web:alice",
        conversation_ref="isolated-office-conversation")
    directory.bind(account_id, "office", endpoint_id=endpoint["endpoint_id"], credential_ref="alice")
    with patch.object(app, "_ensure_dispatch_worker"):
        yield app, account
    app.close()


def test_retry_joins_original_after_default_change_and_history_is_scoped(workspace):
    app, account = workspace
    account_id = account["account_id"]
    old = app.hosts.choices(account_id)
    send = lambda key, text, host, version: app.send_chat_stream(account, message=text, idempotency_key=key,
                                                               instance_id=host, expected_revision=version)
    send("already-saved", "old message", LEGACY_INSTANCE_ID, old["revision"])
    new = app.hosts.set_default(account_id, "office", expected_revision=old["revision"])
    send("already-saved", "old message", LEGACY_INSTANCE_ID, old["revision"])
    with pytest.raises(WorkspaceConflictError, match="IDEMPOTENCY_PAYLOAD_MISMATCH"):
        send("already-saved", "different", "office", new["revision"])
    with pytest.raises(HostInstanceConflict):
        send("stale-new", "stale message", LEGACY_INSTANCE_ID, old["revision"])
    send("new", "office message", "office", new["revision"])
    dispatches = app.list_chat_dispatches(account)
    assert {item["idempotencyKey"]: item["instanceId"] for item in dispatches} == {
        "already-saved": LEGACY_INSTANCE_ID, "new": "office"}
    timeline = app.history_page(account)["timeline"]["items"]
    assert [entry["text"] for entry in timeline] == ["office message"]
    assert timeline[0]["source"]["is_origin"]
    app.hosts.set_default(account_id, LEGACY_INSTANCE_ID, expected_revision=new["revision"])
    assert [entry["text"] for entry in app.list_timeline(account, entry_type="chat_message")] == ["old message"]


def test_original_task_binding_and_private_card_are_independent_of_default(workspace):
    app, account = workspace
    new = app.hosts.set_default(account["account_id"], "office", expected_revision=1)
    host = app.store.current_host(account["account_id"])
    task, _ = app.service.tasks.ensure_task(user_subject=account["user_subject"], agent_host="nanobot",
        host_task_key="synthetic-original", origin_endpoint_id=host["endpoint_id"], title="合成任务",
        active_conversation_ref=host["session_key"])
    app.hosts.set_default(account["account_id"], LEGACY_INSTANCE_ID, expected_revision=new["revision"])
    assert app.store.task_host(account, task["task_id"]) == host
    followup = app.continue_task(account, task["task_id"])
    app.send_chat_stream(account, message=followup["message"], idempotency_key="original-task",
                         continuation_task_id=task["task_id"])
    dispatch = app.list_chat_dispatches(account)[0]
    assert dispatch["instanceId"] == "office"
    with pytest.raises(KeyError):
        app.private_card_url({**account, "user_subject":"another-user"}, dispatch["dispatchId"])


def test_model_context_excludes_the_same_accounts_other_host(workspace):
    app, account = workspace
    subject = account["user_subject"]
    other = app.hosts.set_default(account["account_id"], "office", expected_revision=1)
    assert other["default_instance_id"] == "office"
    office = app.store.current_host(account["account_id"])
    for endpoint_id, message in ((account["endpoint_id"], "legacy-only"), (office["endpoint_id"], "office-only")):
        app.service.tasks.append_timeline_message(user_subject=subject, source_endpoint_id=endpoint_id,
            message_key=message, role="assistant", text=message)
    for host, endpoint, instance in (("nanobot", office["endpoint_key"], "office"),
                                     ("openclaw", account["endpoint_key"], LEGACY_INSTANCE_ID)):
        result = app.service.get_host_cross_endpoint_context(user_subject=subject, agent_host=host,
            endpoint_key=endpoint, host_instance_id=instance)
        assert not result["entries"]
    with pytest.raises(PermissionError):
        app.service.get_host_cross_endpoint_context(user_subject=subject, agent_host="nanobot",
            endpoint_key=office["endpoint_key"], host_instance_id="other-nanobot")


def test_private_card_refresh_checks_the_version_returned_with_the_url(workspace):
    from unittest.mock import Mock

    app, account = workspace
    app.hosts.set_default(account["account_id"], "office", expected_revision=1)
    binding = app.store.current_host(account["account_id"])
    client = Mock()
    app.host_routes[binding["route_ref"]] = client
    client.private_card.return_value = {
        "available": True, "cardRevision": 2,
        "url": "https://cards.example.invalid/flow/input/synthetic-fixture",
    }
    app.service.trusted_card_base_url = "https://cards.example.invalid"
    dispatch = {"account_id": account["account_id"], "accepted_run_id": "current-run",
                "instance_id": "office", "state": "accepted"}
    with patch.object(app.store, "get_host_dispatch", return_value=dispatch), \
         patch.object(app.store, "dispatch_binding", return_value=binding):
        with pytest.raises(KeyError, match="可信卡已更新"):
            app.private_card_url(account, "example", expected_revision=1)
        assert app.private_card_url(account, "example", expected_revision=2) == client.private_card.return_value["url"]


def test_readiness_accepts_a_workspace_with_only_an_independent_host(workspace):
    from threading import Thread
    from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
    from tests.test_workspace import _free_port, _request

    original, _ = workspace
    app = WorkspaceApplication(service=original.service, host_routes={"office": object()})
    port = _free_port()
    config = validate_workspace_server_config(host="127.0.0.1", port=port, public_base_url=f"http://127.0.0.1:{port}",
                                             tls_cert=None, tls_key=None)
    with create_workspace_http_server(config=config, application=app) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            status, _, receipt = _request(port, "GET", "/readyz")
            assert status == 200 and receipt["status"] == "ready"
            assert receipt["gatewayConfigured"] is False and receipt["configuredHostCount"] == 1
        finally:
            server.shutdown()
            worker.join()
            app.close()
