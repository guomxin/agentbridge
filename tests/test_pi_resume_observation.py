"""Fixed Pi resumes may reuse only this call's authenticated observation."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agentbridge.core import pi_resume_observation as proof
from agentbridge.mcp import central


NONCE = "b" * 32
TASK = "task-for-original-governed-preparation"
SUCCESSOR = {"type": "execution_authorization", "interactionId": "successor"}
SCOPE = dict(nonce=NONCE, resumed_from="original", task_id=TASK, subject="first",
             endpoint="web-first", conversation="first-chat", minimum_version=7)


def response(*, terminal=False):
    payload = {"taskId": TASK, "operationId": "operation", "resumedFromInteractionId": "original",
               "status": "succeeded" if terminal else "requires_user_action"}
    if not terminal:
        payload.update(interaction=SUCCESSOR, nextAction={"type": "resume_interaction", "interaction": SUCCESSOR})
    task = {"taskId": TASK, "status": "succeeded" if terminal else "waiting_user", "version": 8,
            "userSubject": "first", "agentHost": "pi", "originEndpointId": "web-first", "activeConversationRef": "first-chat",
            "currentOperationId": "operation", "currentInteractionId": None if terminal else "successor"}
    return payload, {"task": task, "linked": {"operationIds": ["operation"], "interactionIds": [] if terminal else ["successor"]}}


@pytest.mark.parametrize(("change", "expected_remote"), [
    ("valid", False), ("terminal", False), ("terminal-original", False),
    ("terminal-other-interaction", True), ("terminal-missing-interaction-field", True), ("terminal-with-next-action", True),
    ("no-proof", True), ("previous-nonce", True), ("foreign-host", True),
    ("foreign-subject", True), ("foreign-endpoint", True), ("foreign-conversation", True), ("foreign-task", True),
    ("wrong-original", True), ("wrong-version", True), ("boolean-version", True), ("regressed-version", True), ("no-original-version", True), ("wrong-status", True),
    ("wrong-operation", True), ("extra-operation", True), ("duplicate-interaction", True),
    ("missing-interaction", True), ("private-card-mismatch", True), ("nested-operation", True),
    ("oversized", True), ("is-error", True), ("unknown", True), ("not-structured", True), ("nanobot", True),
])
def test_consumer_falls_back_to_authenticated_observe(monkeypatch, change, expected_remote):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "integrations/reference-host"))
    from reference_host.host_profile import PRIVATE_INTERACTION_META_KEY
    from reference_host.mcp_client import McpCallResult
    from reference_host import pi_resume_observation as client_proof
    from reference_host.task_driver import ReferenceTaskDriver

    terminal = change.startswith("terminal")
    payload, observation = response(terminal=terminal)
    receipt = proof.receipt_for(observation, NONCE, "original")
    private = {proof.META_KEY: receipt}
    if not terminal:
        private[PRIVATE_INTERACTION_META_KEY] = dict(SUCCESSOR)
    changed_fields = {"previous-nonce": "nonce", "wrong-original": "resumedFrom", "wrong-version": "taskVersion"}
    changed_task_fields = {"foreign-host": "agentHost", "foreign-subject": "userSubject", "foreign-endpoint": "originEndpointId",
                           "foreign-task": "taskId", "foreign-conversation": "activeConversationRef", "wrong-status": "status",
                           "wrong-operation": "currentOperationId"}
    if change in changed_fields:
        receipt[changed_fields[change]] = 9 if change == "wrong-version" else "different"
    if change in changed_task_fields:
        receipt["task"][changed_task_fields[change]] = "different"
    if change == "terminal-original":
        receipt["task"]["currentInteractionId"] = "original"
    if change == "terminal-other-interaction":
        receipt["task"]["currentInteractionId"] = "different"
    if change == "terminal-missing-interaction-field":
        del receipt["task"]["currentInteractionId"]
    if change == "terminal-with-next-action":
        payload["nextAction"] = {"type": "retry_original_request"}
    if change == "boolean-version":
        receipt["taskVersion"] = receipt["task"]["version"] = True
    if change == "regressed-version":
        receipt["taskVersion"] = receipt["task"]["version"] = 6
    if change == "extra-operation":
        receipt["operationIds"].append("other")
    if change == "duplicate-interaction":
        receipt["interactionIds"] *= 2
    if change == "missing-interaction":
        receipt["interactionIds"] = []
    if change == "private-card-mismatch":
        private[PRIVATE_INTERACTION_META_KEY]["interactionId"] = "another"
    if change == "nested-operation":
        payload["result"] = {"history": [{"operationId": "unaccounted"}]}
    if change == "oversized":
        payload["result"] = [None] * 4097
    if change == "no-proof":
        private.pop(proof.META_KEY)
    if change == "unknown":
        payload["status"] = "outcome_unknown"
    if change == "not-structured":
        import json
        content, structured = [{"type": "text", "text": json.dumps(payload)}], {}
    else:
        content, structured = [], payload

    # The external coordinator owns an independent wire validator. Neither a
    # valid issuer receipt nor any mutation may change meaning across packages.
    assert (client_proof.META_KEY, client_proof.SCHEMA) == (proof.META_KEY, proof.SCHEMA)
    check = {**SCOPE, "minimum_version": 0 if change == "no-original-version" else 7,
             "private_interaction": private.get(PRIVATE_INTERACTION_META_KEY)}
    assert client_proof.matches(private.get(proof.META_KEY), structured, **check) == proof.matches(
        private.get(proof.META_KEY), structured, **check
    )

    driver = Mock(spec=ReferenceTaskDriver)
    driver._pi_task_versions = {} if change == "no-original-version" else {"local": 7}
    driver.implementation = SimpleNamespace(name="nanobot" if change == "nanobot" else "pi")
    driver.identities = SimpleNamespace(get=lambda _: SimpleNamespace(expected_subject="first"))
    driver.state = Mock()
    driver.state.get_task.return_value = {"identity_label": "owner", "task_id": TASK, "endpoint_id": "web-first",
                                         "conversation_ref": "first-chat"}
    driver.call_for_task = AsyncMock()
    driver.present_interaction = AsyncMock(return_value=SUCCESSOR)
    driver.interactions = Mock()
    driver.refresh_task_snapshot = AsyncMock()
    driver._plans = {}
    result = McpCallResult(content, structured, private, change == "is-error")
    asyncio.run(ReferenceTaskDriver.process_tool_result(driver, "local", result, resumed_from="original", resume_nonce=NONCE))
    assert driver.call_for_task.await_count == int(expected_remote)
    if expected_remote:
        assert driver.call_for_task.await_args.args[1] == "agentbridge_host_task_observe"
    if terminal:
        driver.refresh_task_snapshot.assert_awaited_once_with("local")
    else:
        driver.present_interaction.assert_awaited_once()


def test_reference_host_observation_contract_runs_without_the_central_package():
    import json
    import subprocess
    import sys

    payload, observation = response()
    receipt = proof.receipt_for(observation, NONCE, "original")
    program = """
import importlib.abc, json, sys
class DenyCentral(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'agentbridge', 'bscli'}:
            raise AssertionError('Reference Host must not import the central package')
sys.meta_path.insert(0, DenyCentral())
sys.path.insert(0, sys.argv[1])
from reference_host.task_driver import ReferenceTaskDriver
from reference_host.pi_resume_observation import matches
data = json.load(sys.stdin)
assert matches(data['receipt'], data['payload'], private_interaction=data['private'], **data['scope'])
print('independent protocol accepted')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program,
         str(Path(__file__).resolve().parents[1] / "integrations/reference-host")],
        input=json.dumps({"payload": payload, "receipt": receipt, "private": SUCCESSOR, "scope": SCOPE}),
        text=True, capture_output=True, check=True, timeout=15,
    )
    assert result.stdout.strip() == "independent protocol accepted"


@pytest.mark.parametrize("change", ["none", "subject", "lease", "endpoint", "conversation", "kind", "source", "bad-nonce"])
def test_central_checks_provenance_after_resume(monkeypatch, change):
    payload, observation = response()
    context = {"taskId": TASK, "endpointId": "web-first", "conversationRef": "first-chat", "coordinatorLeaseVersion": "2"}
    request = SimpleNamespace(request_context=SimpleNamespace(meta={central.TASK_CONTEXT_META_KEY: context,
                                                                    proof.META_KEY: NONCE}))
    task = {"agent_host": "pi", "origin_endpoint_id": "web-first", "active_conversation_ref": "first-chat",
            "summary": {"command": {"kind": "oa_pending_prepare"}}}
    if change == "kind":
        task["summary"]["command"]["kind"] = "other"
    if change in {"endpoint", "conversation"}:
        task["origin_endpoint_id" if change == "endpoint" else "active_conversation_ref"] = "other"
    if change == "source":
        payload["resumedFromInteractionId"] = "other"
    if change == "bad-nonce":
        request.request_context.meta[proof.META_KEY] = []
    service = SimpleNamespace(tasks=SimpleNamespace(get_task=Mock(return_value=task)), observe_host_task=Mock(return_value=observation))
    monkeypatch.setattr(central, "_request_identity", lambda _: {"user_subject": "another" if change == "subject" else "first"})
    check = AsyncMock(side_effect=PermissionError("lease changed") if change == "lease" else None)
    monkeypatch.setattr(central, "_require_registered_host_call", check)
    attempt = central._pi_resume_receipt(request, service, object(), "first", TASK, "original", payload)
    if change == "lease":
        with pytest.raises(PermissionError):
            asyncio.run(attempt)
    else:
        receipt = asyncio.run(attempt)
        if change == "none":
            assert proof.matches(receipt, payload, private_interaction=SUCCESSOR, **SCOPE)
            assert check.await_args.kwargs["require_coordinator_lease"] is True
        else:
            assert receipt is None
    assert service.observe_host_task.call_count == int(change == "none")



def test_task_version_floor_uses_only_matching_authenticated_original_snapshots(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "integrations/reference-host"))
    from reference_host.mcp_client import McpCallResult
    from reference_host.task_driver import ReferenceTaskDriver

    driver = Mock(spec=ReferenceTaskDriver)
    driver._pi_task_versions = {}
    driver.implementation = SimpleNamespace(name="pi")
    driver.identities = SimpleNamespace(get=lambda _: SimpleNamespace(expected_subject="first", endpoint_key="web-key"))
    driver.state = Mock()
    driver.state.get_task.return_value = {"identity_label": "owner", "task_id": TASK, "endpoint_id": "web-first",
                                         "conversation_ref": "first-chat"}
    driver.artifacts = Mock()
    client = Mock()
    driver.client.return_value = client
    client.call_tool = AsyncMock()

    _, observation = response()
    task = observation["task"]
    for change, error in [({"userSubject": "other"}, False), ({}, True), ({}, False),
                          ({"version": 6}, False), ({"taskId": "another", "version": 99}, False)]:
        client.call_tool.return_value = McpCallResult([], {"task": {**task, **change}}, {}, error)
        asyncio.run(ReferenceTaskDriver.refresh_task_snapshot(driver, "local"))
        if error or change.get("userSubject") == "other":
            assert not driver._pi_task_versions
        else:
            assert driver._pi_task_versions == {"local": 8}
