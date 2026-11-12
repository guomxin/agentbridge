"""Real MCP/CLI watch exports; only the downstream workflow read is synthetic."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from agentbridge.cli.main import main
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from tests.authorization_fixtures import grant_permissions
from tests.test_business_watch_service import context, create, reference, snapshot
from tests import test_central_mcp as mcp_fixtures


WATCH_TOOLS = {"agentbridge_business_watch_"+name for name in ("create", "list", "get", "update")}
STATE_TOOL = "oa_workflow_state_get"


@pytest.fixture
def mcp_context(context):
    ctx = context
    store = McpIdentityTokenStore(ctx.service.db_path)
    ctx.tokens = {}
    profiles = json.loads((Path(__file__).resolve().parents[1]/"schemas/agent-host/v1/test-vectors.json").read_text())["profiles"]
    ctx.profile = deepcopy(profiles[0]["value"])
    ctx.reader_profile = deepcopy(profiles[1]["value"])
    for subject, principal in (("user-a", "Alice"), ("user-b", "Bob")):
        grant_permissions(ctx.service.user_grants, subject, ["oa.workflow.read", "oa.workflow.watch"])
        issued = store.issue(user_subject=subject, expected_principal_ref=principal, ttl_seconds=3600)
        ctx.tokens[subject] = issued["token"]
        ctx.service.negotiate_host(user_subject=subject, token_id=issued["token_id"], profile=ctx.profile)
    ctx.identity_store = store
    ctx.meta = {"io.agentbridge/host-context": {"version": "1", "agentHost": "reference-host",
        "hostInstanceId": ctx.profile["hostInstanceId"], "hostVersion": "0.1.0"}}
    config = validate_central_mcp_server_config(host="127.0.0.1", port=8790,
        public_base_url="http://testserver", tls_cert=None, tls_key=None)
    server = create_central_mcp_server(service=ctx.service, identity_store=store, config=config,
        auth_card_base_url="http://127.0.0.1:8780")
    with patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=lambda *_a, **_k: snapshot()) as read:
        with TestClient(server.streamable_http_app()) as client:
            ctx.client, ctx.read = client, read
            yield ctx


def rpc(ctx, name, arguments=None, *, subject="user-a", meta=True):
    params = {"name": name, "arguments": arguments or {}}
    if meta is not False:
        params["_meta"] = ctx.meta if meta is True else meta
    response = mcp_fixtures.CentralMcpTests._request(ctx.client, "tools/call", request_id="watch-export-test",
        token=ctx.tokens[subject], params=params)
    assert response.status_code == 200
    return response.json()["result"]


def content(result):
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_catalog_exposes_exact_watch_contract_and_mutation_annotations(mcp_context):
    ctx = mcp_context
    response = mcp_fixtures.CentralMcpTests._request(ctx.client, "tools/list", request_id="catalog", token=ctx.tokens["user-a"])
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert WATCH_TOOLS | {STATE_TOOL} <= tools.keys()
    for name in WATCH_TOOLS | {STATE_TOOL}:
        tool = tools[name]
        fields = tool["inputSchema"]["properties"]
        assert not {"user_subject", "url", "source_url", "affair_id", "summary_id", "process_id", "case_id"}.intersection(fields)
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["annotations"]["readOnlyHint"] is (name not in {
            "agentbridge_business_watch_create", "agentbridge_business_watch_update"})
    assert tools["agentbridge_business_watch_create"]["inputSchema"]["properties"]["allow_mark_read"]["default"] is False
    assert set(tools[STATE_TOOL]["inputSchema"]["properties"]) == {"target_ref"}
    assert {"watch_id", "expected_version", "action"} <= set(tools["agentbridge_business_watch_update"]["inputSchema"]["required"])


def test_notification_validation_is_host_control_only_and_requires_host_metadata(mcp_context):
    ctx = mcp_context
    name = "agentbridge_host_notification_validate"
    response = mcp_fixtures.CentralMcpTests._request(ctx.client, "tools/list", request_id="host-catalog", token=ctx.tokens["user-a"])
    assert name in {tool["name"] for tool in response.json()["result"]["tools"]}
    profile = content(rpc(ctx, "agentbridge_server_profile"))
    assert name in profile["toolPlanes"]["hostControl"]["levels"]["L3"]
    assert name not in profile["toolPlanes"]["model"]["toolNames"]
    assert name not in profile["agentToolAccess"]["allowedToolNames"]
    assert rpc(ctx, name, {"agent_host": "reference-host", "endpoint_key": "telegram:*:watchalice",
        "delivery_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "attempt_count": 1}, meta=False)["isError"]
    ctx.read.assert_not_called()


def test_digest_tools_require_registered_host_and_are_not_model_tools(mcp_context):
    ctx = mcp_context
    profile = content(rpc(ctx, "agentbridge_server_profile"))
    for name in ("agentbridge_host_oa_daily_digest", "agentbridge_host_oa_daily_digest_status"):
        assert name in profile["toolPlanes"]["hostControl"]["levels"]["L3"]
        assert name not in profile["agentToolAccess"]["allowedToolNames"]
        arguments = {"agent_host": "openclaw", "telegram_endpoint_key": "telegram:*:1001", "trial": True}
        assert rpc(ctx, name, arguments, meta=False)["isError"]
        assert rpc(ctx, name, arguments)["isError"]
    ctx.read.assert_not_called()


def test_l3_registered_create_default_consent_and_cas_update(mcp_context):
    ctx = mcp_context
    target = reference(ctx)
    rejected = rpc(ctx, "agentbridge_business_watch_create", {"target_ref": target})
    assert rejected["isError"]
    ctx.read.assert_not_called()
    created = content(rpc(ctx, "agentbridge_business_watch_create", {"target_ref": target, "allow_mark_read": True,
        "idempotency_key": "mcp-watch-create-once"}))["watch"]
    assert created["allow_mark_read"] is True and ctx.read.call_count == 1
    updated = content(rpc(ctx, "agentbridge_business_watch_update", {"watch_id": created["watch_id"],
        "expected_version": created["version"], "action": "pause"}))["watch"]
    assert updated["state"] == "paused"
    assert rpc(ctx, "agentbridge_business_watch_update", {"watch_id": created["watch_id"],
        "expected_version": created["version"], "action": "resume"})["isError"]


def test_create_rejects_explicit_empty_event_selection_before_downstream_read(mcp_context):
    ctx = mcp_context
    rejected = rpc(ctx, "agentbridge_business_watch_create", {"target_ref": reference(ctx),
        "allow_mark_read": True, "event_types": []})
    assert rejected["isError"]
    ctx.read.assert_not_called()
    assert ctx.watches.store.list(user_subject="user-a")["items"] == []


def test_create_update_require_current_exact_l3_registration_before_io(mcp_context):
    ctx = mcp_context
    watch = create(ctx)
    ctx.read.reset_mock()
    identity = ctx.identity_store.verify(ctx.tokens["user-a"])
    ctx.service.negotiate_host(user_subject="user-a", token_id=identity["token_id"], profile=ctx.reader_profile)
    l1 = {"io.agentbridge/host-context": {"version": "1", "agentHost": "unknown-reader",
        "hostInstanceId": ctx.reader_profile["hostInstanceId"], "hostVersion": "1.0.0"}}
    wrong_version = deepcopy(ctx.meta)
    wrong_version["io.agentbridge/host-context"]["hostVersion"] = "future-version"
    for meta in (False, l1, wrong_version):
        for name, arguments in (("agentbridge_business_watch_create", {"target_ref": reference(ctx, number=2), "allow_mark_read": True}),
            ("agentbridge_business_watch_update", {"watch_id": watch["watch_id"], "expected_version": watch["version"], "action": "pause"})):
            assert rpc(ctx, name, arguments, meta=meta)["isError"]
    ctx.read.assert_not_called()
    assert ctx.watches.store.get(watch["watch_id"], user_subject="user-a")["state"] == "active"


@pytest.mark.parametrize("missing", ["oa.workflow.read", "oa.workflow.watch"])
def test_platform_prefix_cannot_bypass_either_business_permission(mcp_context, missing):
    ctx = mcp_context
    watch = create(ctx)
    token = reference(ctx, number=2)
    grant_permissions(ctx.service.user_grants, "user-a", sorted({"oa.workflow.read", "oa.workflow.watch"}-{missing}))
    ctx.read.reset_mock()
    cases = [("agentbridge_business_watch_create", {"target_ref": token, "allow_mark_read": True}),
        ("agentbridge_business_watch_list", {}), ("agentbridge_business_watch_get", {"watch_id": watch["watch_id"]}),
        ("agentbridge_business_watch_update", {"watch_id": watch["watch_id"], "expected_version": watch["version"],
            "action": "configure", "interval_seconds": 1800})]
    for name, arguments in cases:
        assert rpc(ctx, name, arguments)["isError"]
    ctx.read.assert_not_called()
    # Revoking read access must still allow stopping the platform observation,
    # with the deliberately minimal receipt established in the first batch.
    stopped = content(rpc(ctx, "agentbridge_business_watch_update", {"watch_id": watch["watch_id"],
        "expected_version": watch["version"], "action": "stop"}))
    assert set(stopped) == {"watch"} and set(stopped["watch"]) == {"watch_id", "state", "version"}


def test_mcp_list_get_derive_owner_from_token_and_do_not_reveal_private_source(mcp_context):
    ctx = mcp_context
    own, other = create(ctx), create(ctx, subject="user-b", number=2)
    ctx.read.reset_mock()
    listed = content(rpc(ctx, "agentbridge_business_watch_list", {}, meta=False))
    assert [watch["watch_id"] for watch in listed["items"]] == [own["watch_id"]]
    detail = content(rpc(ctx, "agentbridge_business_watch_get", {"watch_id": own["watch_id"]}, meta=False))
    assert detail["watch"]["watch_id"] == own["watch_id"]
    assert "source" not in detail["watch"] and "target" not in detail["watch"]["snapshot"]
    assert rpc(ctx, "agentbridge_business_watch_get", {"watch_id": other["watch_id"]})["isError"]
    assert rpc(ctx, "agentbridge_business_watch_get", {"watch_id": own["watch_id"]}, subject="user-b")["isError"]
    ctx.read.assert_not_called()


def test_target_reference_owner_expiry_and_url_substitution_fail_before_io(mcp_context):
    ctx = mcp_context
    candidates = [reference(ctx, subject="user-b"), reference(ctx, expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()),
        "https://untrusted.example.test/workflow", "malformed-reference"]
    for token in candidates:
        assert rpc(ctx, "agentbridge_business_watch_create", {"target_ref": token, "allow_mark_read": True})["isError"]
    ctx.read.assert_not_called()


def test_state_get_uses_read_permission_without_creating_watch_or_exposing_target(mcp_context):
    ctx = mcp_context
    grant_permissions(ctx.service.user_grants, "user-a", ["oa.workflow.read"])
    result = content(rpc(ctx, STATE_TOOL, {"target_ref": reference(ctx)}))
    assert result["status"] == "succeeded" and result["result"]["workflow_state"] == "in_progress"
    assert "target" not in result["result"] and ctx.read.call_count == 1
    assert ctx.watches.store.list(user_subject="user-a")["items"] == []
    grant_permissions(ctx.service.user_grants, "user-a", ["oa.workflow.watch"])
    ctx.read.reset_mock()
    assert rpc(ctx, STATE_TOOL, {"target_ref": reference(ctx)})["isError"]
    ctx.read.assert_not_called()


def test_cli_list_get_use_owned_service_and_reject_other_owner(mcp_context):
    ctx = mcp_context
    watch = create(ctx)
    common = ["--home", str(ctx.service.home), "watch"]
    with patch("agentbridge.cli.main.CentralCapabilityService", return_value=ctx.service):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(common+["list", "--user-subject", "user-a", "--limit", "1", "--base-url", ctx.service.adapter.base_url])
        assert code == 0 and json.loads(output.getvalue())["items"][0]["watch_id"] == watch["watch_id"]
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(common+["get", watch["watch_id"], "--user-subject", "user-a", "--base-url", ctx.service.adapter.base_url])
        assert code == 0 and json.loads(output.getvalue())["watch"]["watch_id"] == watch["watch_id"]
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(common+["get", watch["watch_id"], "--user-subject", "user-b", "--base-url", ctx.service.adapter.base_url])
        assert code == 2 and watch["title"] not in output.getvalue()


def test_real_service_outbox_retry_and_permission_revocation_do_not_replay_oa(mcp_context):
    ctx = mcp_context
    identity = ctx.identity_store.verify(ctx.tokens["user-a"])
    endpoint = ctx.service.tasks.ensure_endpoint(user_subject="user-a", token_id=identity["token_id"],
        agent_host="openclaw", endpoint_key="telegram:default:watch-outbox", client_type="telegram",
        external_subject="1001", account_id="default", conversation_ref="agent:main:telegram:direct:1001",
        route={"channel": "telegram", "to": "telegram:1001", "accountId": "default", "threadId": None},
        capabilities=["direct_status"])[0]
    watch = create(ctx, endpoint_ids=[endpoint["endpoint_id"]])
    ctx.watches.store.apply_observation(watch["watch_id"], user_subject="user-a", expected_version=watch["version"],
        snapshot=snapshot(name="真实outbox合成新节点"))
    ctx.read.reset_mock()
    parameters = {"user_subject": "user-a", "agent_host": "openclaw", "endpoint_key": endpoint["endpoint_key"]}
    claimed = ctx.service.claim_host_notifications(**parameters)
    assert len(claimed["notifications"]) == 1
    notification = claimed["notifications"][0]
    assert notification["deliveryMode"] == "business_watch" and notification["message"] is None
    assert "真实outbox合成新节点" not in json.dumps(notification, ensure_ascii=False)
    delivery_id, attempt = notification["deliveryId"], notification["attemptCount"]
    allowed = ctx.service.validate_host_notification(**parameters, delivery_id=delivery_id, attempt_count=attempt)
    assert allowed["allowed"] is True and "真实outbox合成新节点" in allowed["message"]
    assert ctx.service.validate_host_notification(**parameters, delivery_id=delivery_id, attempt_count=attempt+1) == {"allowed": False, "stale": True}
    retried = ctx.service.acknowledge_host_notification(**parameters, delivery_id=delivery_id,
        succeeded=False, retry_after_seconds=1, attempt_count=attempt)
    assert retried["delivery"]["state"] == "pending"
    with sqlite3.connect(ctx.service.db_path) as db:
        db.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE delivery_id=?",
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), delivery_id))
    second = ctx.service.claim_host_notifications(**parameters)["notifications"][0]
    assert second["deliveryId"] == delivery_id and second["attemptCount"] == attempt+1
    assert ctx.service.validate_host_notification(**parameters, delivery_id=delivery_id, attempt_count=attempt) == {"allowed": False, "stale": True}
    before = ctx.service.tasks.get_delivery(delivery_id)
    stale = ctx.service.acknowledge_host_notification(**parameters, delivery_id=delivery_id,
        succeeded=True, attempt_count=attempt)
    assert stale["delivery"]["state"] == "delivering"
    assert stale["delivery"]["attemptCount"] == second["attemptCount"]
    assert ctx.service.tasks.get_delivery(delivery_id) == before
    with pytest.raises(ValueError):
        ctx.service.acknowledge_host_notification(**parameters, delivery_id=delivery_id, succeeded=True)
    assert ctx.service.tasks.get_delivery(delivery_id) == before
    grant_permissions(ctx.service.user_grants, "user-a", ["oa.workflow.read"])
    assert ctx.service.validate_host_notification(**parameters, delivery_id=delivery_id,
        attempt_count=second["attemptCount"]) == {"allowed": False}
    cancelled = ctx.service.acknowledge_host_notification(**parameters, delivery_id=delivery_id,
        succeeded=False, attempt_count=second["attemptCount"])
    assert cancelled["delivery"]["state"] == "acknowledged"
    assert ctx.service.claim_host_notifications(**parameters)["notifications"] == []
    assert ctx.service.tasks.get_delivery(delivery_id)["state"] == "acknowledged"
    ctx.read.assert_not_called()
