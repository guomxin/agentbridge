"""Central-authority and coordination vectors for the candidate NanoBot identity."""

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "integrations/reference-host")]
from reference_host.bound_coordinator import IMPLEMENTATION
from reference_host.host_profile import (
    CoordinatorImplementation,
    host_context_meta,
    registration_meta,
    task_call_meta,
)
from reference_host.mcp_client import AgentBridgeMcpClient
from tests.support.nanobot_controlled_write_probe import run
from tests.support.business_draft_acceptance import OWNER


async def checks(fixture, binding, instance, python):
    import httpx

    base = fixture.home.parent
    spec = json.loads((base / "host.json").read_text())
    client = AgentBridgeMcpClient(
        mcp_url=spec["mcpUrl"],
        token_provider=lambda: (base / "personal.token").read_text().strip(),
        private_transport=True,
    )
    foreign = instance + "-other"
    for host in (instance, foreign):
        r = await client.call_tool(
            "agentbridge_host_register",
            {},
            meta=registration_meta(instance_id=host, implementation=IMPLEMENTATION),
        )
        assert not r.is_error and r.payload()["acceptedLevel"] == "L3"
    unknown = CoordinatorImplementation("nanobot", "0.0-unlisted")
    r = await client.call_tool(
        "agentbridge_host_register",
        {},
        meta=registration_meta(instance_id="unknown-candidate", implementation=unknown),
    )
    assert not r.is_error and r.payload()["acceptedLevel"] == "L1"
    owner_meta = host_context_meta(instance_id=instance, implementation=IMPLEMENTATION)
    foreign_meta = host_context_meta(instance_id=foreign, implementation=IMPLEMENTATION)
    account = fixture.account["account_id"]
    scope = {
        "agent_host": "nanobot",
        "endpoint_key": binding["endpoint_key"],
        "client_type": "web",
        "external_subject": account,
        "account_id": account,
        "conversation_ref": binding["session_key"],
    }
    original = await client.call_tool(
        "agentbridge_host_task_ensure",
        {**scope, "host_task_key": "contract-lease", "title": "隔离租约验收"},
        meta=owner_meta,
    )
    task = original.payload()["task"]["taskId"]
    lease = original.payload()["coordinatorLease"]
    owner_task = task_call_meta(
        task_id=task,
        lease_version=lease["version"],
        endpoint_id=binding["endpoint_id"],
        conversation_ref=binding["session_key"],
        instance_id=instance,
        implementation=IMPLEMENTATION,
    )
    foreign_task = task_call_meta(
        task_id=task,
        lease_version=lease["version"],
        endpoint_id=binding["endpoint_id"],
        conversation_ref=binding["session_key"],
        instance_id=foreign,
        implementation=IMPLEMENTATION,
    )
    denied = await client.call_tool(
        "agentbridge_host_coordinator_lease_acquire",
        {"task_id": task, "takeover": False, "expected_version": lease["version"]},
        meta=foreign_meta,
    )
    assert denied.is_error
    one = dict(scope)
    one.pop("account_id")
    one.update(
        task_id=task,
        message_key="nanobot-contract-first",
        role="assistant",
        text="核验时间线排序",
    )
    for args in (
        one,
        one,
        {**one, "message_key": "nanobot-contract-second", "text": "核验第二条"},
    ):
        value = await client.call_tool(
            "agentbridge_host_timeline_append", args, meta=owner_task
        )
        assert not value.is_error
    snapshot = await client.call_tool(
        "agentbridge_host_task_snapshot",
        {
            "agent_host": "nanobot",
            "endpoint_key": binding["endpoint_key"],
            "task_id": task,
        },
        meta=owner_meta,
    )
    events = snapshot.payload().get("events") or []
    order = [e["sequence"] for e in events]
    assert order == sorted(set(order))
    with sqlite3.connect(fixture.service.db_path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM user_timeline WHERE task_id=? AND text IN (?,?)",
                (task, "核验时间线排序", "核验第二条"),
            ).fetchone()[0]
            == 2
        )
    prepared = await client.call_tool("oa_leave_prepare", {}, meta=owner_task)
    card = (
        prepared.private_meta.get("io.agentbridge/interaction")
        or prepared.payload().get("interaction")
        or {}
    )
    assert card.get("interactionId")
    program = """import asyncio,json,sys
sys.path.insert(0,sys.argv[1])
from mcp import types
from nanobot_agentbridge.runtime import Conversation,project_result
raw=json.load(sys.stdin)
result=asyncio.run(project_result(types.CallToolResult(content=[],_meta={"io.agentbridge/interaction":raw}),
                    Conversation("test","endpoint","session"),None,frozenset()))
assert result.is_error and result.error_code=="HOST_CAPABILITY_REQUIRED" and "https://" not in result
"""
    rejection = await asyncio.create_subprocess_exec(
        str(python),
        "-I",
        "-B",
        "-c",
        program,
        str(ROOT / "integrations/nanobot-agentbridge"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await rejection.communicate(json.dumps(card).encode())
    assert rejection.returncode == 0
    rejected = await client.call_tool(
        "agentbridge_host_interaction_present",
        {
            "agent_host": "nanobot",
            "endpoint_key": binding["endpoint_key"],
            "interaction_id": card["interactionId"],
        },
        meta=host_context_meta(instance_id="unknown-candidate", implementation=unknown),
    )
    assert (
        rejected.is_error
        and not rejected.private_meta
        and "/flow/" not in json.dumps(rejected.structured)
    )
    refused = await client.call_tool(
        "agentbridge_interaction_resume",
        {"interaction_id": card["interactionId"], "idempotency_key": "foreign-probe"},
        meta=foreign_task,
    )
    assert refused.is_error and not fixture.matrix["effects"]
    original_call = client._call_tool_once
    attempts = []

    async def transient(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("Synthetic disconnection")
        return await original_call(*args, **kwargs)

    client._call_tool_once = transient
    retried = await client.call_tool(
        "agentbridge_server_profile", {}, meta=owner_meta, recovery_class="read"
    )
    assert not retried.is_error and len(attempts) == 2
    attempts.clear()
    try:
        await client.call_tool(
            "agentbridge_interaction_resume",
            {"interaction_id": card["interactionId"]},
            meta=owner_task,
            recovery_class="unsafe",
        )
    except httpx.ConnectError:
        pass
    assert len(attempts) == 1 and not fixture.matrix["effects"]
    return {
        "unknown_host_level": "L1",
        "l1_private_presentation_denied": True,
        "timeline_append_two_unique": True,
        "monotonic": True,
        "foreign_owner_acquire_denied": True,
        "foreign_resume_denied": True,
        "safe_read_transient_attempts": 2,
        "unsafe_transport_attempts": 1,
        "effects": 0,
    }


def exercise(python, **args):
    return asyncio.run(
        checks(args["fixture"], args["binding"], args["instance"], python)
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--nanobot-python", type=Path, required=True)
    from functools import partial

    python = p.parse_args().nanobot_python
    print(json.dumps(run(python, False, exercise=partial(exercise, python))))
