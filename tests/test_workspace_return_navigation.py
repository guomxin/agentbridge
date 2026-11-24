"""Trusted deployment-only navigation never supplies a new business capability."""
from datetime import timedelta
from http.client import HTTPConnection
from threading import Thread
from contextlib import ExitStack
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import pytest

from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.flow import TrustedFlowApplication, _workspace_root
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.workspace.host_instances import LEGACY_INSTANCE_ID
from tests import test_trusted_flow as flow_helpers
from tests.test_workspace import _create_account


@pytest.fixture
def flow():
    harness = flow_helpers.TrustedFlowTests(methodName="runTest")
    harness.setUp()
    try:
        harness.account = _create_account(harness.service, user_subject="user-a", username="return-review",
            endpoint_key="telegram:return-review")
        harness.app = TrustedFlowApplication(service=harness.service, field_application=harness.field_app,
            action_application=harness.action_app, workspace_base_url="https://workspace.example.test")
        harness.presentation = harness.service.field_submissions.create_presentation(
            harness.source["submission_id"], user_subject="user-a", endpoint_id=harness.account["endpoint_id"])
        yield harness
    finally:
        harness.doCleanups()


def test_only_actual_workspace_presentation_has_configured_return(flow):
    # The task itself originated elsewhere: the opened presentation is the proof.
    assert flow.endpoint_id != flow.account["endpoint_id"]
    response, _ = flow.open(presentation_id=flow.presentation["presentation_id"])
    assert response.status == 200
    body = response.body.decode()
    assert 'href="https://workspace.example.test/"' in body
    assert '>返回 Workspace</a>' in body
    assert response.headers["Referrer-Policy"] == "no-referrer"
    for presentation_id in (None, flow.service.field_submissions.create_presentation(
            flow.source["submission_id"], user_subject="user-a", endpoint_id=flow.endpoint_id)["presentation_id"]):
        response, _ = flow.open(presentation_id=presentation_id)
        assert response.status == 200
        assert b'id="agentbridge-back"' not in response.body


@pytest.fixture
def nanobot_presentation(flow):
    account = flow.account
    hosts = flow.service.workspace.hosts
    assert hosts.choices(account["account_id"])["default_instance_id"] == LEGACY_INSTANCE_ID
    hosts.save_instance("office", host_type="nanobot", display_name="NanoBot", route_ref="office-route",
                        adapter_version="0.3.0", state="enabled", subjects=[account["user_subject"]],
                        expected_revision=0, actor="test-admin", reason="Verify trusted form navigation")
    key = f"workspace:office:{account['account_id']}"
    endpoint, _ = flow.service.tasks.ensure_endpoint(
        user_subject=account["user_subject"], token_id="fixture-web", agent_host="nanobot",
        client_type="web", account_id=account["account_id"], external_subject=account["account_id"],
        endpoint_key=key, conversation_ref=key,
    )
    hosts.bind(account["account_id"], "office", endpoint_id=endpoint["endpoint_id"], credential_ref="fixture-owner")
    return flow.service.field_submissions.create_presentation(
        flow.source["submission_id"], user_subject=account["user_subject"], endpoint_id=endpoint["endpoint_id"])


@pytest.mark.parametrize("state", ["enabled", "draining"])
def test_nondefault_nanobot_web_presentation_returns_to_deployment_root(flow, nanobot_presentation, state):
    with flow.service.workspace._connect() as db:
        db.execute("UPDATE workspace_host_instances SET state=? WHERE instance_id='office'", (state,))
    flow.app = TrustedFlowApplication(service=flow.service, field_application=flow.field_app,
                                      action_application=flow.action_app,
                                      workspace_base_url="https://workspace.example.test/configured")
    response, _ = flow.open(presentation_id=nanobot_presentation["presentation_id"])
    assert response.status == 200
    body = response.body.decode()
    assert 'id="agentbridge-back" href="https://workspace.example.test/configured/"' in body
    assert '>返回 Workspace</a>' in body
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert flow.service.workspace.hosts.choices(flow.account["account_id"])["default_instance_id"] == LEGACY_INSTANCE_ID


@pytest.mark.parametrize("table,key_column,column,value", [
    ("workspace_accounts", "account_id", "state", "disabled"),
    ("workspace_accounts", "account_id", "user_subject", "different-subject"),
    ("workspace_host_bindings", "endpoint_id", "account_id", "different-account"),
    ("workspace_host_bindings", "endpoint_id", "instance_id", "missing-instance"),
    ("workspace_host_bindings", "endpoint_id", "endpoint_id", "different-endpoint"),
    ("workspace_host_bindings", "endpoint_id", "endpoint_key", "different-key"),
    ("workspace_host_bindings", "endpoint_id", "session_key", "different-session"),
    ("workspace_host_instances", "instance_id", "state", "revoked"),
    ("workspace_host_instances", "instance_id", "host_type", "openclaw"),
    ("workspace_host_visibility", "instance_id", "user_subject", "different-subject"),
    ("client_endpoints", "endpoint_id", "state", "revoked"),
    ("client_endpoints", "endpoint_id", "user_subject", "different-subject"),
    ("client_endpoints", "endpoint_id", "account_id", "different-account"),
    ("client_endpoints", "endpoint_id", "external_subject", "different-account"),
    ("client_endpoints", "endpoint_id", "client_type", "telegram"),
])
def test_invalid_nanobot_binding_omits_return(flow, nanobot_presentation, table, key_column, column, value):
    keys = {"account_id": flow.account["account_id"], "instance_id": "office",
            "endpoint_id": nanobot_presentation["endpoint_id"]}
    with flow.service.workspace._connect() as db:
        changed = db.execute(f"UPDATE {table} SET {column}=? WHERE {key_column}=?", (value, keys[key_column]))
        assert changed.rowcount == 1
    response, _ = flow.open(presentation_id=nanobot_presentation["presentation_id"])
    assert response.status == 200
    assert b'id="agentbridge-back"' not in response.body


@pytest.mark.parametrize("agent_host", ["openclaw", "nanobot"])
def test_telegram_presentation_of_same_users_form_has_no_workspace_return(flow, nanobot_presentation, agent_host):
    endpoint, _ = flow.service.tasks.ensure_endpoint(
        user_subject=flow.account["user_subject"], token_id="fixture-chat", agent_host=agent_host,
        client_type="telegram", external_subject="501", endpoint_key="telegram:8001:501",
        conversation_ref="agent:main:telegram:direct:501", route={"channel": "telegram", "to": "501"},
    )
    presentation = flow.service.field_submissions.create_presentation(
        flow.source["submission_id"], user_subject=flow.account["user_subject"], endpoint_id=endpoint["endpoint_id"])
    response, _ = flow.open(presentation_id=presentation["presentation_id"])
    assert response.status == 200
    assert b'id="agentbridge-back"' not in response.body


@pytest.mark.parametrize("table,column,value", [
    ("workspace_accounts", "state", "disabled"),
    ("workspace_accounts", "user_subject", "other-subject"),
    ("workspace_accounts", "endpoint_key", "another-endpoint"),
    ("workspace_accounts", "openclaw_session_key", "another-session"),
    ("client_endpoints", "state", "revoked"),
    ("client_endpoints", "agent_host", "another-host"),
    ("client_endpoints", "client_type", "telegram"),
    ("client_endpoints", "external_subject", "other-account"),
    ("client_endpoints", "account_id", "other-account"),
    ("client_endpoints", "endpoint_key", "another-endpoint"),
    ("client_endpoints", "conversation_ref", "another-session"),
])
def test_changed_account_or_endpoint_never_gets_workspace_navigation(flow, table, column, value):
    context = flow.app.reader.context("input", flow.source["submission_id"], flow.presentation["presentation_id"])
    assert flow.app._workspace_navigation(context)["back"]["kind"] == "workspace"
    key, target = ("account_id", flow.account["account_id"]) if table == "workspace_accounts" else (
        "endpoint_id", flow.account["endpoint_id"])
    with flow.service.workspace._connect() as connection:
        connection.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (value, target))
    assert flow.app._workspace_navigation(context) is None


def test_legacy_submit_and_readonly_receipt_keep_root_without_extra_authorization(flow):
    flow.service.invoke = Mock(side_effect=AssertionError("navigation cannot invoke"))
    flow.service.resume_interaction = Mock(side_effect=AssertionError("navigation cannot coordinate"))
    response, token = flow.submit_source(presentation_id=flow.presentation["presentation_id"])
    assert '"kind": "workspace"' in response.body.decode()
    response, _ = flow.open(presentation_id=flow.presentation["presentation_id"], cookie=token)
    assert '"url": "https://workspace.example.test/"' in response.body.decode()
    assert "sensitive work content" not in response.body.decode()
    with flow.service.write_authorizations._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM write_authorizations").fetchone()[0] == 0
    flow.service.invoke.assert_not_called()
    flow.service.resume_interaction.assert_not_called()


def test_expired_page_only_has_fixed_home_without_ticket_or_business_material(flow):
    initial_clock = flow.service.field_submissions.clock
    flow.service.field_submissions.clock = lambda: initial_clock() + timedelta(days=1)
    response, _ = flow.open(presentation_id=flow.presentation["presentation_id"])
    assert response.status == 403
    assert b'https://workspace.example.test/' in response.body
    assert flow.source["submission_id"].encode() not in response.body
    assert flow.presentation["presentation_id"].encode() not in response.body
    assert "sensitive work content" not in response.body.decode()
    assert "Set-Cookie" not in response.headers


@pytest.mark.parametrize("value", ["javascript:alert(1)", "//evil.test/", "https://user@workspace.test/",
    "https://workspace.test/?return=https://evil.test", "https://workspace.test/#ticket", "https://workspace.test/?",
    "https://workspace.test/#", "https://workspace.test/\n", "https://workspace.test\\@evil.test", "https://workspace.test:99999"])
def test_unsafe_configured_roots_are_rejected(value):
    with pytest.raises(ValueError):
        _workspace_root(value)


def test_validated_deployment_prefix_remains_fixed():
    assert _workspace_root(None) is None
    assert _workspace_root("https://Workspace.Example.test/workspace") == "https://workspace.example.test/workspace/"


def test_http_does_not_accept_frontend_return_url(flow):
    server = create_auth_http_server(config=validate_auth_server_config(host="127.0.0.1", port=0,
        public_base_url="http://127.0.0.1:0", tls_cert=None, tls_key=None),
        application=TrustedAuthApplication(challenge_store=flow.service.challenges, broker=Mock()),
        field_application=flow.field_app, action_application=flow.action_app, flow_application=flow.app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        path = f'/flow/input/{flow.source["submission_id"]}/present/{flow.presentation["presentation_id"]}'
        connection.request("GET", path + "?" + urlencode({"returnURL": "https://evil.test/steal"}))
        response = connection.getresponse()
        body = response.read()
        assert response.status == 403
        assert b"https://workspace.example.test/" in body
        assert b"evil.test" not in body
        assert not response.getheader("Location")
        assert not response.getheader("Set-Cookie")
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("enabled", [True, False])
def test_central_wires_only_its_workspace_deployment_configuration(flow, enabled):
    from agentbridge.mcp.central import serve_central_mcp, validate_central_mcp_server_config
    from agentbridge.workspace.server import validate_workspace_server_config
    with ExitStack() as stack:
        auth = stack.enter_context(patch("agentbridge.mcp.central.create_auth_http_server"))
        for name in ("create_workspace_http_server", "create_central_mcp_server", "bounded_mcp_app",
                     "CentralSessionKeepalive", "CentralRuntimeGovernanceWorker", "WorkspaceApplication", "uvicorn.run"):
            stack.enter_context(patch("agentbridge.mcp.central." + name))
        serve_central_mcp(service=flow.service, identity_store=Mock(),
            mcp_config=validate_central_mcp_server_config(host="127.0.0.1", port=8790,
                public_base_url=None, tls_cert=None, tls_key=None),
            auth_config=validate_auth_server_config(host="127.0.0.1", port=8780,
                public_base_url=None, tls_cert=None, tls_key=None),
            workspace_config=(validate_workspace_server_config(host="127.0.0.1", port=8788,
                public_base_url="https://workspace.example.test/configured", tls_cert=None, tls_key=None)
                if enabled else None))
        assert auth.call_args.kwargs["flow_application"].workspace_root == (
            "https://workspace.example.test/configured/" if enabled else None)
