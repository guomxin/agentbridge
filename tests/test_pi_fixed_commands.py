"""Check fixed command handoff over the original Pi control connection and sidecar."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.exceptions import HTTPException

from tests.test_pi_control_sessions import channel_grant, control  # noqa: F401 (shared isolated MCP peer)


@pytest.fixture
def commands(tmp_path, control, channel_grant):
    pytest.importorskip("aiohttp.web")
    from nanobot_agentbridge.commands import CommandQueue
    from pi_agentbridge.server import Host, binding_key
    from pi_agentbridge.telegram_routes import PiTelegramRoutes

    sessions, counts, owners, binding = control
    spec = sessions.spec
    spec.update(requiredLevel="L3", coordinatorProgram="/isolated/coordinator",
                trustedCardOrigins=["https://isolated.invalid"],
                telegramPilot={"botId": "8801", "allowedSenders": {"first": "501"}})
    records = [{**binding(subject, char), "endpoint_key": "workspace:pi-test:" + subject}
               for subject, char in (("first", "A"), ("second", "B"))]
    host = object.__new__(Host)
    host.spec, host.control_sessions, host.instance, host.level = spec, sessions, spec["instanceId"], "L3"
    host.bindings = {binding_key(record): record for record in records}
    host.telegram = PiTelegramRoutes(host)
    host.state, host.coordinator, host.cards = tmp_path, SimpleNamespace(program="unused"), SimpleNamespace(origins=[])
    host.commands = CommandQueue(host)
    host.commands.observe = AsyncMock(return_value="done")
    source = SimpleNamespace(task={}, key=str(uuid4()), delivery=str(uuid4()), lease_owner=host.instance,
                             after_claim=lambda: None)

    def issue(record):
        source.task = {"taskId": str(uuid4()), "userSubject": record["user_subject"], "agentHost": "pi",
                       "originEndpointId": record["endpoint_id"], "activeConversationRef": record["session_key"]}
        source.key = str(uuid4())
        return {"binding": record, "taskId": source.task["taskId"], "commandKey": source.key}

    def install(api):
        @api.tool()
        def agentbridge_host_task_snapshot(agent_host: str, endpoint_key: str, task_id: str,
                                           event_limit: int, artifact_limit: int) -> dict[str, Any]:
            assert agent_host == "pi" and event_limit == artifact_limit == 1 and task_id == source.task["taskId"]
            counts["snapshot"] += 1
            return {"task": source.task}

        @api.tool()
        def agentbridge_host_coordinator_lease_get(task_id: str) -> dict[str, Any]:
            counts["lease"] += 1
            assert task_id == source.task["taskId"]
            return {"coordinatorLease": {"version": 7, "hostInstanceId": source.lease_owner}}

        @api.tool()
        def agentbridge_host_notification_claim(agent_host: str, endpoint_key: str, limit: int, lease_seconds: int,
                include_workspace_chat: bool, include_business_watch: bool, include_task_files: bool,
                include_oa_digest: bool, include_workspace_result_views: bool, wait_seconds: float) -> dict[str, Any]:
            assert wait_seconds == 15 and endpoint_key == "telegram:8801:501" and agent_host == "pi"
            counts["claim"] += 1
            source.after_claim()
            item = {"deliveryId": source.delivery, "attemptCount": 1, "deliveryMode": "prepared_command",
                    "task": source.task, "event": {"eventType": "task.prepare.requested", "payload": {"commandKey": source.key}}}
            return {"endpoint": {"endpointId": source.task["originEndpointId"]}, "notifications": [item]}

        @api.tool()
        def agentbridge_host_notification_ack(agent_host: str, endpoint_key: str, delivery_id: str, attempt_count: int,
                                            succeeded: bool, retry_after_seconds: int) -> dict[str, Any]:
            assert succeeded and delivery_id == source.delivery and attempt_count == 1
            counts["ack"] += 1
            return {"delivery": {"deliveryId": delivery_id, "attemptCount": attempt_count}}

    channel_grant["install"] = install
    return host, records, counts, owners, source, issue


def test_web_warm_commands_keep_identity_snapshot_lease_and_local_idempotency(commands):
    host, (first, second), counts, owners, source, issue = commands

    async def check():
        try:
            for _ in range(2):
                body = issue(first)
                assert (await host.action("prepare-command", body))["accepted"]
                await host.commands.jobs[source.key]
            assert counts == {"initialize": 1, "register": 1, "identity": 2, "tools/list": 1, "snapshot": 2, "lease": 2}
            assert (await host.action("prepare-command", body))["accepted"]
            assert counts["identity"] == 3 and counts["snapshot"] == 2 and host.commands.observe.await_count == 2
            with pytest.raises(HTTPException):
                await host.action("prepare-command", {**body, "binding": second})
            cross_subject = issue(first)
            with pytest.raises(HTTPException):
                await host.action("prepare-command", {**cross_subject, "binding": second})
            source.lease_owner = "another-instance"
            with pytest.raises(HTTPException):
                await host.action("prepare-command", issue(first))
            owners.clear()
            with pytest.raises(PermissionError):
                await host.action("prepare-command", issue(first))
            assert host.commands.observe.await_count == 2
            request = host.commands.observe.await_args.args[0]
            assert request["mode"] == "prepare_command" and request["task"]["coordinatorLeaseVersion"] == "7"
        finally:
            await host.control_sessions.close()

    asyncio.run(check())


@pytest.mark.parametrize("change", ["none", "unlink", "relink", "credential", "subject", "origin", "lease"])
def test_tg_claim_rechecks_after_wait_on_same_transport(commands, channel_grant, change):
    host, (first, _), counts, owners, source, issue = commands

    async def check():
        try:
            response = await host.telegram.action("telegram-route", {"botId": "8801", "senderId": "501"})
            record = response["binding"]
            issue(record)
            before = counts.copy()

            def change_during_poll():
                if change == "unlink":
                    channel_grant["active"] = False
                elif change == "relink":
                    channel_grant["generation"] = "b" * 64
                elif change == "credential":
                    Path(first["tokenFile"]).write_text("C" * 32)
                elif change == "subject":
                    owners["A" * 32] = "second"
                elif change == "origin":
                    source.task["activeConversationRef"] = "another-conversation"
                elif change == "lease":
                    source.lease_owner = "another-instance"

            source.after_claim = change_during_poll
            result = await host.action("telegram-notifications", {"binding": record, "waitSeconds": 15})
            assert result == {"deliveries": []}
            new = counts - before
            assert new["claim"] == new["register"] == new["initialize"] == new["tools/list"] == 1
            if change == "none":
                await host.commands.jobs[source.key]
                assert new == {"claim": 1, "register": 1, "initialize": 1, "tools/list": 1, "identity": 2,
                               "endpoint": 2, "snapshot": 1, "lease": 1, "ack": 1}
                request = host.commands.observe.await_args.args[0]
                assert request["pairingRef"] == channel_grant["generation"] and request["task"]["endpointId"] == record["endpoint_id"]
            else:
                assert not host.commands.jobs and not new["ack"]
                assert new["snapshot"] == new["lease"] == int(change == "lease")
        finally:
            await host.control_sessions.close()

    asyncio.run(check())


@pytest.mark.parametrize(("host_name", "mode", "directory_calls"), [
    ("pi", "prepare_command", 0), ("pi", "durable_plan", 1), ("pi", "ordinary", 1), ("nanobot", "prepare_command", 1),
])
def test_only_fixed_pi_sidecar_omits_explicit_model_catalog(tmp_path, monkeypatch, host_name, mode, directory_calls):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "integrations/reference-host"))
    from reference_host import bound_coordinator as sidecar
    from reference_host.task_driver import ReferenceTaskDriver

    result = lambda data: SimpleNamespace(payload=lambda: data, is_error=False)
    current = {"identity": {"userSubject": "first"}, "host": {"acceptedLevel": "L3"},
               "controlledPrepares": {"synthetic_prepare": {"prepareCapability": "demo.prepare", "commitCapability": "demo.commit"}},
               "agentToolAccess": {"allowedToolNames": ["synthetic_prepare"]}}
    mcp = SimpleNamespace(call_tool=AsyncMock(side_effect=[result({"negotiation": {"acceptedLevel": "L3"}}), result(current)]),
                          list_tools=AsyncMock(return_value=[{"name": "synthetic_prepare"}]))
    monkeypatch.setattr(ReferenceTaskDriver, "client", lambda *_: mcp)
    class CheckedProfile(Exception):
        pass
    def stop_at_pin(journal, req, capability):
        assert capability == (None if mode == "prepare_command" else "demo.prepare")
        raise CheckedProfile
    monkeypatch.setattr(sidecar, "pin_run", stop_at_pin)
    request = {"mode": mode, "tool": "synthetic_prepare", "endpointKey": "workspace:pi-test:first", "endpoint": "ep",
               "subject": "first", "userDir": str(tmp_path), "instance": "pi-test", "conversation": "conv", "runId": "run",
               "task": {"conversationRef": "conv", "endpointId": "ep", "hostRunId": "run"},
               "tokenFile": str(tmp_path / "test-token"), "mcpUrl": "http://127.0.0.1/mcp"}
    implementation = sidecar.PI_IMPLEMENTATION if host_name == "pi" else sidecar.IMPLEMENTATION
    with pytest.raises(CheckedProfile):
        asyncio.run(sidecar.run_locked(request, implementation))
    assert [call.args[0] for call in mcp.call_tool.await_args_list] == ["agentbridge_server_profile", "agentbridge_host_identity_profile"]
    assert mcp.list_tools.await_count == directory_calls
