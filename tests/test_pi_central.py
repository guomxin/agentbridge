"""Pi uses its own exact deployment, credentials, channel ownership and compatibility evidence."""

import json

import pytest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from agentbridge.core.tasks import TaskIntegrityError
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.authorization_fixtures import grant_permissions
from tests.test_pi_workspace import pi, private_json  # noqa: F401


def test_pi_personal_registration_and_tool_scope_through_mcp(pi):
    service = pi.service
    with service.workspace._connect() as db:
        db.execute("DELETE FROM agent_host_compatibility WHERE implementation_name='pi'")
    identities = McpIdentityTokenStore(service.db_path)
    grant_permissions(service.user_grants, "user-a", ["oa.workflow.read"])
    grant_permissions(service.user_grants, "user-b", [])
    configs = validate_central_mcp_server_config(host="127.0.0.1", port=8790, public_base_url="http://testserver", tls_cert=None, tls_key=None)
    app = create_central_mcp_server(service=service, identity_store=identities, config=configs,
                                    auth_card_base_url="http://127.0.0.1:8780")
    vector = json.loads((Path(__file__).parents[1] / "schemas/agent-host/v1/test-vectors.json").read_text())
    profile = {**vector["profiles"][0]["value"], "implementation": {"name": "pi", "version": "0.1.2"},
               "hostInstanceId": "pi-office"}
    context = {"version": "1", "hostInstanceId": "pi-office", "agentHost": "pi", "hostVersion": "0.1.2"}
    meta = {"io.agentbridge/host-context": context, "io.agentbridge/host-profile": profile}
    def issue(subject, label):
        return identities.issue(user_subject=subject, expected_principal_ref=subject, label=label)["token"]
    first = issue("user-a", "workspace-pi:pi-office:pi-a")
    second = issue("user-b", "workspace-pi:pi-office:pi-b")
    foreign = issue("user-b", "workspace-pi:pi-office:pi-a")
    other_host = issue("user-a", "test-other-host")
    with TestClient(app.streamable_http_app()) as client:
        def call(token, name, args=None, metadata=meta):
            reply = client.post("/mcp", headers={"Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
                json={"jsonrpc":"2.0", "id":1, "method":"tools/call",
                      "params":{"name":name, "arguments":args or {}, "_meta":metadata}})
            assert reply.status_code == 200
            return reply.json()["result"]
        for token in (first, second):
            result = call(token, "agentbridge_host_register")
            assert result["structuredContent"]["acceptedLevel"] == "L1"
        assert not call(first, "oa_session_status").get("isError")
        assert call(first, "oa_session_status", metadata={})["isError"]
        assert call(first, "oa_session_login")["isError"]
        assert call(foreign, "agentbridge_host_register")["isError"]
        assert call(other_host, "agentbridge_host_register")["isError"]
        spoof = {**meta, "io.agentbridge/host-profile": {**profile, "implementation":{"name":"nanobot","version":"0.3.0"}},
                 "io.agentbridge/host-context":{**context,"agentHost":"nanobot","hostVersion":"0.3.0"}}
        assert call(first, "agentbridge_host_register", metadata=spoof)["isError"]
        assert call(other_host, "agentbridge_host_register", metadata=spoof)["isError"]
        assert call(first, "agentbridge_host_telegram_mirror_peers", {"bot_id":"8801"})["isError"]
        for token, owner, permitted in ((first,"user-a",True),(second,"user-b",False)):
            result = call(token, "agentbridge_host_identity_profile", {"agent_host":"pi"})["structuredContent"]
            assert result["identity"]["userSubject"] == owner
            assert ("oa_workflow_pending_list" in result["agentToolAccess"]["allowedToolNames"]) == permitted
        legacy = {"io.agentbridge/host":context}
        assert call(other_host, "oa_session_status", metadata=legacy)["isError"]
        reads = ("taihua_work_log_my_list","yuque_public_books_list","smartlight_system_overview")
        grant_permissions(service.user_grants, "user-a", ["oa.workflow.read", "taihua.work_log.my.read",
                                                          "yuque.content.read", "smartlight.asset.read"])
        with patch.object(service,"invoke") as business:
            for name in reads:
                assert call(first,name)["isError"]
            business.assert_not_called()
        with service.workspace._connect() as db:
            db.execute("INSERT INTO agent_host_compatibility VALUES ('pi','0.1.2','L2','isolated','fixture','now')")
        assert call(first,"agentbridge_host_register")["structuredContent"]["acceptedLevel"] == "L1"
        pi.spec["requiredLevel"] = "L2"
        private_json(pi.route_file,pi.manifest)
        assert call(first,"agentbridge_host_register")["structuredContent"]["acceptedLevel"] == "L2"
        assert call(second,"agentbridge_host_register")["structuredContent"]["acceptedLevel"] == "L2"
        with patch.object(service,"invoke",return_value={"status":"succeeded","items":[]}) as business:
            for name in reads:
                assert not call(first,name).get("isError")
                assert call(second,name)["isError"]
            assert [c.kwargs["capability_name"] for c in business.call_args_list] == [
                "taihua.work_log.my.list","yuque.public_books.list","smartlight.system.overview"]
            assert {c.kwargs["user_subject"] for c in business.call_args_list} == {"user-a"}
            assert call(first,"yuque_document_catalog")["isError"]
            assert business.call_count == 3
        for interaction_kind, marker in (("business_input", True), ("credential", False)):
            record = {"interaction_type":interaction_kind,"resume_spec":{"kind":"session_ready"}}
            with patch.object(service.interactions,"get",return_value=record), patch.object(service,"resume_interaction") as resume:
                reply = call(first,"agentbridge_interaction_resume",{"interaction_id":"original-fixture-interaction"},
                             metadata={**meta,"io.agentbridge/session-ready-only":marker})
            assert reply["isError"] and "Pi" in reply["content"][0]["text"]
            resume.assert_not_called()
        pi.spec["subjects"]["user-a"] = "replaced"
        private_json(pi.route_file, pi.manifest)
        assert call(first, "agentbridge_host_identity_profile", {"agent_host":"pi"})["isError"]
        assert call(first, "oa_session_status")["isError"]


def test_only_current_paired_pi_endpoint_claims_its_workspace_text(pi):
    service = pi.service
    identities = McpIdentityTokenStore(service.db_path)
    first = identities.issue(user_subject="user-a", expected_principal_ref="user-a",
                             label="workspace-pi:pi-office:pi-a")
    links = TelegramPilotLinks(service)
    account = pi.accounts["a"]["account_id"]
    args = (account, "pi-office", "8801")
    challenge = links.begin(*args)
    code = links.present("user-a", "pi-office", "8801", "501", challenge["code"])
    links.confirm(*args, challenge["challenge_id"], code)
    links.ensure_endpoint("user-a", first["token_id"], "pi-office", "8801", "501")
    service.workspace.hosts.set_default(account, "pi-office", expected_revision=1)
    service.workspace.accept_chat_request(account_id=account, user_subject="user-a", message="核对私聊投递",
        idempotency_key="pi-question", attachments=[], tasks=service.tasks, attachment_store=service.timeline_attachments,
        media_base_url="https://test.invalid", instance_id="pi-office")
    args = {"user_subject":"user-a", "endpoint_key":"telegram:8801:501", "host_instance_id":"pi-office",
            "agent_host":"pi"}
    delivered = service.claim_host_notifications(**args, include_workspace_chat=True)["notifications"]
    assert [r["timeline"]["mirrorKind"] for r in delivered] == ["pi_workspace_text_v1"]
    receipt = {"delivery_id":delivered[0]["deliveryId"], "attempt_count":delivered[0]["attemptCount"]}
    assert service.validate_host_notification(**args, **receipt)["allowed"]
    assert not service.tasks.list_outbox(user_subject="user-b")
    del pi.spec["telegram"]["allowedSenders"]
    private_json(pi.route_file, pi.manifest)
    assert not service.validate_host_notification(**args, **receipt)["allowed"]


def test_coordinator_requires_original_pi_endpoint_and_current_pair(pi):
    service = pi.service
    subject = "user-a"
    account = pi.accounts["a"]["account_id"]
    identity = McpIdentityTokenStore(service.db_path).issue(
        user_subject=subject, expected_principal_ref=subject, label="workspace-pi:pi-office:pi-a")
    links = TelegramPilotLinks(service)
    channel = (account, "pi-office", "8801")
    challenge = links.begin(*channel)
    confirmation = links.present(subject, "pi-office", "8801", "501", challenge["code"])
    links.confirm(*channel, challenge["challenge_id"], confirmation)
    endpoint = links.ensure_endpoint(subject, identity["token_id"], "pi-office", "8801", "501")
    task, _ = service.tasks.ensure_task(user_subject=subject, agent_host="pi",
        host_task_key="isolated-lease", origin_endpoint_id=endpoint["endpointId"],
        active_conversation_ref=endpoint["conversationRef"], title="隔离原端点")
    scope = {"user_subject":subject, "task_id":task["task_id"]}
    owner = {"agentHost":"pi", "hostInstanceId":"pi-office"}
    lease = service.acquire_host_coordinator_lease(**scope, registration=owner)
    for wrong in ({"agentHost":"nanobot","hostInstanceId":"pi-office"},
                  {"agentHost":"pi","hostInstanceId":"other-pi"}):
        for method in (service.acquire_host_coordinator_lease, service.get_host_coordinator_lease,
                       service.assert_host_coordinator_lease, service.release_host_coordinator_lease):
            with pytest.raises(TaskIntegrityError):
                method(**scope, registration=wrong)
    assert service.assert_host_coordinator_lease(**scope, registration=owner)["leaseId"] == lease["leaseId"]
    pi.spec["telegram"]["allowedSenders"] = {}
    private_json(pi.route_file, pi.manifest)
    with pytest.raises(TaskIntegrityError):
        service.assert_host_coordinator_lease(**scope, registration=owner)


def test_exact_pi_review_is_needed_for_elevated_registration_and_downgrade(pi):
    from agentbridge.core.host_contract import HostRegistrationRequired

    service = pi.service
    pi.spec["requiredLevel"] = "L3"
    private_json(pi.route_file,pi.manifest)
    with service.workspace._connect() as db:
        db.execute("DELETE FROM agent_host_compatibility WHERE implementation_name='pi'")
    vector = json.loads((Path(__file__).parents[1] / "schemas/agent-host/v1/test-vectors.json").read_text())
    profile = {**vector["profiles"][0]["value"], "implementation":{"name":"pi","version":"0.1.2"},
               "hostInstanceId":"pi-office"}
    def negotiate():
        return service.negotiate_host(user_subject="user-a", token_id="fixture", profile=profile)["acceptedLevel"]
    args = {"user_subject":"user-a","agent_host":"pi","host_instance_id":"pi-office","host_version":"0.1.2"}
    assert negotiate() == "L1"
    with service.workspace._connect() as db:
        db.execute("INSERT INTO agent_host_compatibility VALUES ('pi','another-version','L3','test-only','fixture','now')")
    assert negotiate() == "L1"
    with service.workspace._connect() as db:
        db.execute("INSERT INTO agent_host_compatibility VALUES ('pi','0.1.2','L2','test-only','fixture','now')")
    assert negotiate() == "L2"
    with pytest.raises(HostRegistrationRequired):
        service.require_host_registration(**args,minimum_level="L3")
    with service.workspace._connect() as db:
        db.execute("UPDATE agent_host_compatibility SET accepted_level='L3' WHERE implementation_name='pi' AND implementation_version='0.1.2'")
    assert negotiate() == "L3"
    assert service.require_host_registration(**args,minimum_level="L3")["acceptedLevel"] == "L3"
    pi.spec["requiredLevel"] = "L1"
    private_json(pi.route_file,pi.manifest)
    with pytest.raises(PermissionError):
        service.require_host_registration(**args,minimum_level="L3")
    assert negotiate() == "L1"
    pi.spec["requiredLevel"] = "L3"
    private_json(pi.route_file,pi.manifest)
    assert negotiate() == "L3"
    with service.workspace._connect() as db:
        db.execute("DELETE FROM agent_host_compatibility WHERE implementation_name='pi' AND implementation_version='0.1.2'")
    with pytest.raises(HostRegistrationRequired):
        service.require_host_registration(**args,minimum_level="L3")
    assert negotiate() == "L1"
