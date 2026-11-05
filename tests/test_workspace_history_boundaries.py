"""Durable reply and current-interaction boundaries, using isolated ledgers."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch
from uuid import uuid4

import pytest

from agentbridge.core.tasks import TaskNotFound, WORKSPACE_RESPONSE_MAX_CHARACTERS
from agentbridge.workspace.application import WorkspaceApplication
from tests.test_workspace import FakeGateway, _create_account, _service


@pytest.fixture
def context(tmp_path):
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject="user-a", username="alice", endpoint_key="telegram:*:alice")
    endpoint, _ = service.tasks.ensure_endpoint(
        user_subject="user-a", token_id="token-alice", agent_host="openclaw",
        endpoint_key="telegram:*:alice", client_type="telegram", external_subject="alice",
        conversation_ref="agent:main:telegram:direct:alice", capabilities=["timeline_message"],
    )
    gateway = FakeGateway()
    app = WorkspaceApplication(service=service, gateway=gateway)
    try:
        with patch.object(app, "_ensure_dispatch_worker"):
            yield app, account, gateway, endpoint
    finally:
        app.close()


def _claim(app, account, key="boundary-request"):
    stream = app.send_chat_stream(account, message="Synthetic long reply", idempotency_key=key)
    stream.close()
    dispatch = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="boundary-test")
    assert dispatch is not None
    return dispatch


def _authority_text(text):
    # Use the actual authoritative-final helper, whose budget differs from
    # normalized streaming candidates. No model or downstream business is used.
    source = """
import { historyEvidenceForRun } from './agentbridge/workspace/gateway_run_guard.mjs';
let input='';for await(const chunk of process.stdin) input+=chunk;
const text=JSON.parse(input);
const evidence=historyEvidenceForRun({messages:[
  {role:'user',runId:'boundary-request',content:'Synthetic'},
  {role:'assistant',runId:'boundary-request',stopReason:'stop',content:text}
]},'boundary-request');
process.stdout.write(evidence.final_text);
"""
    return subprocess.run(["node", "--input-type=module", "-e", source],
        input=json.dumps(text), text=True, capture_output=True, check=True,
        cwd=Path(__file__).resolve().parents[1], timeout=10).stdout


def test_authoritative_long_reply_survives_refresh_and_bounded_push_mirror(context):
    app, account, gateway, endpoint = context
    text = _authority_text("A" * 60_000 + "完整末尾")
    gateway.stream_events = [{"type": "chat", "state": "final", "text": text}]
    dispatch = _claim(app, account)
    app._process_host_dispatch(dispatch)

    assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == "completed"
    # A fresh application reads the persisted answer without Gateway history.
    restored = WorkspaceApplication(service=app.service)
    try:
        answers = [row for row in restored.current(account)["timeline"]["items"] if row.get("role") == "assistant"]
        assert len(answers) == 1 and answers[0]["text"] == text
    finally:
        restored.close()
    mirrors = [row["payload"] for row in app.service.tasks.list_outbox(
        user_subject="user-a", endpoint_id=endpoint["endpoint_id"])
        if row["payload_type"] == "timeline_message" and row["payload"]["role"] == "assistant"]
    assert len(mirrors) == 1
    assert len(mirrors[0]["text"]) <= 50_000
    assert mirrors[0]["textTruncated"] is True
    assert mirrors[0]["originalTextLength"] == len(text)
    assert "完整回复请回 Workspace 查看" in mirrors[0]["text"]
    assert len([call for call in gateway.calls if call[0] == "send_stream"]) == 1


@pytest.mark.parametrize("failure_point", ["timeline", "terminal_receipt"])
def test_failed_final_persistence_reconciles_same_run_without_resend(context, failure_point):
    app, account, gateway, endpoint = context
    text = "B" * 60_000 + "可恢复末尾"
    gateway.stream_events = [{"type": "chat", "state": "final", "text": text}]
    dispatch = _claim(app, account)
    target, method = ((app.service.tasks, "append_workspace_response") if failure_point == "timeline"
                      else (app.store, "finish_host_dispatch"))
    with patch.object(target, method, side_effect=sqlite3.OperationalError("synthetic persistence interruption")):
        with pytest.raises(sqlite3.OperationalError) as failure:
            app._process_host_dispatch(dispatch)
        app._finish_dispatch_internal_error(dispatch, failure.value)
    pending = app.store.get_host_dispatch(dispatch["dispatch_id"])
    assert pending["state"] == "accepted" and pending["claim_token"] is None
    assert pending["accepted_run_id"] == "boundary-request"

    later = datetime.now(timezone.utc) + timedelta(seconds=11)
    with patch.object(app.store, "clock", return_value=later):
        claimed = app.store.claim_next_host_dispatch(account_id=account["account_id"], claim_owner="recovery-test")
    assert claimed is not None and claimed["state"] == "accepted"
    gateway.run_history_evidence = lambda **_: {"final_text": text, "observed_run_id": "boundary-request"}
    app._process_host_dispatch(claimed)
    assert app.store.get_host_dispatch(dispatch["dispatch_id"])["state"] == "completed"
    answers = [row for row in app.list_timeline(account, entry_type="chat_message") if row["role"] == "assistant"]
    assert len(answers) == 1 and answers[0]["text"] == text
    assert len([call for call in gateway.calls if call[0] == "send_stream"]) == 1
    assert len([row for row in app.service.tasks.list_outbox(user_subject="user-a", endpoint_id=endpoint["endpoint_id"])
                if row["payload_type"] == "timeline_message" and row["payload"]["role"] == "assistant"]) == 1


def test_workspace_response_budget_does_not_expand_general_tool_or_foreign_sources(context):
    app, account, _, endpoint = context
    arguments = dict(user_subject="user-a", source_endpoint_id=account["endpoint_id"],
        message_key="workspace:assistant:budget", text="C" * 50_001)
    with pytest.raises(ValueError):
        app.service.tasks.append_timeline_message(**arguments, role="assistant")
    for changes, error in [({"text": "C" * (WORKSPACE_RESPONSE_MAX_CHARACTERS + 1)}, ValueError),
                           ({"source_endpoint_id": endpoint["endpoint_id"]}, ValueError),
                           ({"message_key": "host:assistant:budget"}, ValueError),
                           ({"user_subject": "user-b"}, TaskNotFound)]:
        with pytest.raises(error):
            app.service.tasks.append_workspace_response(**(arguments | changes))
    saved, _ = app.service.tasks.append_workspace_response(**(arguments | {"text": "C" * WORKSPACE_RESPONSE_MAX_CHARACTERS}))
    assert len(saved["text"]) == WORKSPACE_RESPONSE_MAX_CHARACTERS


def _task(app, account):
    task, _ = app.service.tasks.ensure_task(user_subject=account["user_subject"], agent_host="openclaw",
        host_task_key=str(uuid4()), origin_endpoint_id=account["endpoint_id"],
        active_conversation_ref=account["openclaw_session_key"], title="Synthetic large batch")
    return task


def _authorization(app, task, subject="user-a"):
    operation, _ = app.service.operations.create(user_subject=subject, capability_name="oa.efficiency_data.approve",
        capability_version="1", input_summary={})
    authorization = app.service.write_authorizations.create(user_subject=subject, system_id="oa", session_id="synthetic-session",
        capability_name="oa.efficiency_data.approve", capability_version="1", prepare_operation_id=operation["operation_id"],
        plan={"target": operation["operation_id"]}, summary={"title": "Synthetic current item", "fields": []},
        card_base_url="https://cards.example.test")
    interaction = app.service._execution_authorization_interaction(authorization)
    app.service.observe_host_task(user_subject=subject, task_id=task["task_id"], interaction_ids=[interaction["interactionId"]])
    return interaction


def test_current_interaction_after_first_hundred_is_present_in_snapshot_and_detail(context):
    app, account, _, _ = context
    task = _task(app, account)
    interactions = [_authorization(app, task) for _ in range(103)]
    current_id = interactions[-1]["interactionId"]
    for detail in (app.task_detail(account, task["task_id"]), app.task_projections(account, [task["task_id"]])["items"][0]["detail"]):
        assert detail["interaction"]["interactionId"] == current_id
        assert detail["interaction"]["presentation"]["individualized"] is True
        assert detail["interaction"]["presentation"]["endpointId"] == account["endpoint_id"]
        assert detail["interactionsHasMore"] is True
        assert detail["interactionHistoryLimit"] == 100
        assert len(detail["interactions"]) == 101
        assert [row["interactionId"] for row in detail["interactions"][:100]] == [row["interactionId"] for row in interactions[:100]]
    with app.service.write_authorizations._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_authorizations WHERE state='consumed'").fetchone()[0] == 0


@pytest.mark.parametrize("foreign_subject", [False, True])
def test_current_pointer_cannot_expose_an_interaction_from_another_task(context, foreign_subject):
    app, account, _, _ = context
    task = _task(app, account)
    other_account = (_create_account(app.service, user_subject="user-b", username="bob", endpoint_key="telegram:*:bob")
                     if foreign_subject else account)
    other_task = _task(app, other_account)
    other = _authorization(app, other_task, subject=other_account["user_subject"])
    with app.service.tasks._connect() as connection:
        connection.execute("UPDATE agent_tasks SET current_interaction_id=? WHERE task_id=?", (other["interactionId"], task["task_id"]))
    with patch.object(app.service, "get_interaction", wraps=app.service.get_interaction) as read:
        detail = app.task_detail(account, task["task_id"])
    assert detail["interaction"] is None and detail["interactions"] == []
    assert detail["interactionsHasMore"] is False
    assert other["interactionId"] not in [call.kwargs.get("interaction_id") for call in read.call_args_list]
