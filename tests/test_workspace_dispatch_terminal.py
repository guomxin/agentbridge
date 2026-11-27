"""Synthetic terminal decisions remain atomic, private and tied to their admission."""
from datetime import timedelta
import json
from unittest.mock import patch

import pytest

from agentbridge.core import workspace_text_delivery as delivery
from agentbridge.core.commit_notifications import current_version
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.host_instances import LEGACY_INSTANCE_ID
from agentbridge.workspace.stores import WorkspaceConflictError
from agentbridge.workspace.workspace_dispatch_terminal import TEXT
from tests.test_workspace import FakeGateway
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_host_dispatch_routing import workspace  # noqa: F401


def admit(app, account, key="original-question"):
    with patch.object(app, "_ensure_dispatch_worker"):
        app.send_chat_stream(account, message="这次只查询我的待办", idempotency_key=key)
    return next(row for row in app.store.list_host_dispatches(user_subject=account["user_subject"])
                if row["idempotency_key"] == key)


@pytest.mark.parametrize("state", sorted(TEXT))
def test_each_terminal_projects_one_original_public_status_without_creating_a_task(openclaw, state):
    fixture, token, endpoint = openclaw
    service, account = fixture.service, fixture.account
    app = WorkspaceApplication(service=service, gateway=FakeGateway())
    try:
        dispatch = admit(app, account)
        if state == "canceled":
            app.cancel_chat_dispatch(account, dispatch["dispatch_id"])
            app.cancel_chat_dispatch(account, dispatch["dispatch_id"])
        else:
            dispatch = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="synthetic")
            if state in {"failed", "stopped", "result_unknown"}:
                dispatch = app.store.mark_host_dispatch_accepted(dispatch["dispatch_id"], claim_token=dispatch["claim_token"],
                                                               run_id="original-run")
            app.store.finish_host_dispatch(dispatch["dispatch_id"], claim_token=dispatch["claim_token"], state=state,
                event={"state": "error", "runId": "another-private-run", "text": "private diagnostic and secret URL",
                       "url": "https://private.invalid/secret", "safeToRetry": True,
                       "skillTrial": {"status": "reported", "test_id": "forged"}})
            with pytest.raises(WorkspaceConflictError):
                app.store.finish_host_dispatch(dispatch["dispatch_id"], claim_token=dispatch["claim_token"],
                                               state=state, event={})
        with service.tasks._connect() as db:
            question, terminal = db.execute("SELECT * FROM user_timeline WHERE entry_type='chat_message' ORDER BY sequence").fetchall()
            payload = json.loads(terminal["payload_json"])
            assert payload["workspaceDispatch"] == {"dispatchId": dispatch["dispatch_id"],
                "questionEntryId": question["entry_id"], "state": state,
                "runId": dispatch.get("accepted_run_id") or dispatch["idempotency_key"]}
            assert terminal["source_endpoint_id"] == question["source_endpoint_id"] == dispatch["origin_endpoint_id"]
            status, = db.execute("SELECT * FROM workspace_text_deliveries WHERE kind='status'").fetchall()
            assert status["question_id"] == question["entry_id"]
            assert status["request_id"] == dispatch["dispatch_id"]
            assert not db.execute("SELECT 1 FROM notification_outbox WHERE event_id=?", (terminal["entry_id"],)).fetchone()
            assert not db.execute("SELECT 1 FROM agent_tasks").fetchone()
            assert not db.execute("SELECT 1 FROM operations").fetchone()
        event = app.store.list_host_dispatch_events(dispatch["dispatch_id"])[-1]["payload"]
        assert event["text"] == terminal["text"] == TEXT[state]
        assert event["safeToRetry"] is (state in {"failed_before_accept", "expired", "canceled"})
        assert "private" not in json.dumps(event)
        assert "skillTrial" not in event
        # Exercise the public issue/begin/receipt API; no Telegram transport runs.
        for expected in ("工作台提问\n这次只查询我的待办", "工作台状态\n" + delivery.STATUS[state]):
            item, = delivery.claim(service, account["user_subject"], "openclaw", "openclaw-gateway",
                                    endpoint["endpoint_key"], token, 20, 30)
            scope = dict(subject=account["user_subject"], host="openclaw", instance="openclaw-gateway",
                         endpoint_key=endpoint["endpoint_key"], token_id=token, pairing_ref=item["pairingRef"],
                         delivery_id=item["deliveryId"], attempt_count=item["attemptCount"])
            assert delivery.handle(service, **scope, action="issue")["publicText"] == expected
            assert delivery.handle(service, **scope, action="begin") == {"allowed": True}
            assert delivery.handle(service, **scope, action="ack", outcome="sent", receipt=7) == {"recorded": True}
    finally:
        app.close()


def test_enqueue_failure_rolls_back_decision_timeline_outbox_and_wakeup(openclaw):
    fixture, _, _ = openclaw
    service, account = fixture.service, fixture.account
    app = WorkspaceApplication(service=service, gateway=FakeGateway())
    try:
        dispatch = admit(app, account)
        before = current_version(service.db_path, user_subject=account["user_subject"])
        enqueue = delivery.enqueue_status
        def interrupted(db, row, state):
            enqueue(db, row, state)
            raise RuntimeError("synthetic crash before commit")
        with patch.object(delivery, "enqueue_status", side_effect=interrupted), pytest.raises(RuntimeError):
            app.cancel_chat_dispatch(account, dispatch["dispatch_id"])
        assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == "queued"
        assert current_version(service.db_path, user_subject=account["user_subject"]) == before
        with service.tasks._connect() as db:
            assert not db.execute("SELECT 1 FROM user_timeline WHERE role='assistant'").fetchone()
            assert not db.execute("SELECT 1 FROM workspace_text_deliveries WHERE kind='status'").fetchone()
            assert not db.execute("SELECT 1 FROM agent_host_dispatch_events WHERE event_name='host_dispatch_canceled'").fetchone()
        app.cancel_chat_dispatch(account, dispatch["dispatch_id"])
        assert current_version(service.db_path, user_subject=account["user_subject"]) > before
    finally:
        app.close()


@pytest.mark.parametrize("path,state", [("waiting", "expired"), ("reconciling", "acceptance_unknown")])
def test_internal_deadline_paths_commit_the_same_projection(openclaw, path, state):
    fixture, _, _ = openclaw
    app = WorkspaceApplication(service=fixture.service, gateway=FakeGateway())
    try:
        admit(app, fixture.account)
        dispatch = app.store.claim_next_host_dispatch(account_id=fixture.account["account_id"], claim_owner="synthetic")
        with patch.object(app.store, "_now", return_value=app.store._now() + timedelta(minutes=2)):
            if path == "waiting":
                app._defer_dispatch(dispatch, "GATEWAY_CONNECTION_FAILED")
            else:
                app._defer_acceptance_reconciliation(dispatch, fixture.account, "GATEWAY_TIMEOUT")
        assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == state
        with fixture.service.tasks._connect() as db:
            assert db.execute("SELECT count(*) FROM user_timeline WHERE role='assistant'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM workspace_text_deliveries WHERE kind='status'").fetchone()[0] == 1
    finally:
        app.close()


def test_terminal_uses_original_endpoint_after_default_changes_and_does_not_replay(workspace):
    app, account = workspace
    app.hosts.set_default(account["account_id"], "office", expected_revision=1)
    original = admit(app, account)
    dispatch = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="synthetic")
    dispatch = app.store.mark_host_dispatch_accepted(dispatch["dispatch_id"], claim_token=dispatch["claim_token"], run_id="original-run")
    choices = app.hosts.choices(account["account_id"])
    app.hosts.set_default(account["account_id"], LEGACY_INSTANCE_ID, expected_revision=choices["revision"])
    with patch.object(app, "_run_evidence", return_value={"state": "failed"}), patch.object(app.gateway, "send_stream") as send:
        app._observe_accepted_dispatch(dispatch)
    send.assert_not_called()
    with app.service.tasks._connect() as db:
        terminal, = db.execute("SELECT * FROM user_timeline WHERE role='assistant'").fetchall()
        assert terminal["source_endpoint_id"] == original["origin_endpoint_id"] != account["endpoint_id"]
    assert app.store.get_host_dispatch(original["dispatch_id"])["state"] == "failed"


@pytest.mark.parametrize("host", ["openclaw", "nanobot", "pi"])
@pytest.mark.parametrize("observed,state", [("stopped", "stopped"), ("failed", "failed"), ("unknown", "result_unknown")])
def test_each_host_observed_terminal_ends_only_its_original_run(workspace, host, observed, state):
    app, account = workspace
    if host == "pi":
        app.hosts.save_instance("pi-office", host_type="pi", display_name="Pi", route_ref="pi-office",
            adapter_version="0.1.8", state="enabled", subjects=[account["user_subject"]],
            expected_revision=0, actor="admin", reason="synthetic")
        endpoint, _ = app.service.tasks.ensure_endpoint(user_subject=account["user_subject"], agent_host="pi",
            account_id=account["account_id"], token_id="fixture", client_type="web", external_subject=account["account_id"],
            endpoint_key="pi-office:web:alice", conversation_ref="pi-office-conversation")
        app.hosts.bind(account["account_id"], "pi-office", endpoint_id=endpoint["endpoint_id"], credential_ref="alice")
    target = {"openclaw": LEGACY_INSTANCE_ID, "nanobot": "office", "pi": "pi-office"}[host]
    choices = app.hosts.choices(account["account_id"])
    app.hosts.set_default(account["account_id"], target, expected_revision=choices["revision"])
    admit(app, account)
    dispatch = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="synthetic")
    dispatch = app.store.mark_host_dispatch_accepted(dispatch["dispatch_id"], claim_token=dispatch["claim_token"], run_id="original-run")
    with pytest.raises(GatewayRequestError):
        app._finish_accepted_dispatch(dispatch, account, {"state": "aborted", "runId": "unrelated-run"})
    assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == "accepted"
    with patch.object(app, "_run_evidence", return_value={"state": observed, "final_text": "private residual partial text"}), \
            patch.object(app.gateway, "send_stream") as send:
        app._observe_accepted_dispatch(dispatch)
    send.assert_not_called()
    assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == state
    event = app.store.list_host_dispatch_events(dispatch["dispatch_id"])[-1]["payload"]
    assert event["text"] == TEXT[state] and event["safeToRetry"] is False


@pytest.mark.parametrize("accepted,state", [(False, "acceptance_unknown"), (True, "result_unknown")])
def test_binding_loss_after_dispatch_attempt_never_claims_business_was_not_started(workspace, accepted, state):
    app, account = workspace
    admit(app, account)
    dispatch = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="synthetic")
    def stream(**kwargs):
        if accepted:
            yield {"type": "accepted", "runId": "original-run", "status": "started"}
        raise PermissionError("synthetic binding revocation")
    with patch.object(app.gateway, "send_stream", side_effect=stream) as send:
        app._process_host_dispatch(dispatch)
    assert send.call_count == 1
    assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == state
    event = app.store.list_host_dispatch_events(dispatch["dispatch_id"])[-1]["payload"]
    assert event["text"] == TEXT[state] and event["safeToRetry"] is False


def test_binding_loss_during_observation_preserves_unknown_business_outcome(workspace):
    app, account = workspace
    admit(app, account)
    dispatch = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="synthetic")
    dispatch = app.store.mark_host_dispatch_accepted(dispatch["dispatch_id"], claim_token=dispatch["claim_token"], run_id="original-run")
    with patch.object(app, "_run_evidence", side_effect=PermissionError("synthetic binding revocation")), \
            patch.object(app.gateway, "send_stream") as send:
        app._observe_accepted_dispatch(dispatch)
    send.assert_not_called()
    assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == "result_unknown"
    event = app.store.list_host_dispatch_events(dispatch["dispatch_id"])[-1]["payload"]
    assert event["text"] == TEXT["result_unknown"] and event["safeToRetry"] is False
