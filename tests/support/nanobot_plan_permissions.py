"""Run with the pinned NanoBot SDK; rejected plans never allocate an AgentTask or contact MCP."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "integrations/nanobot-agentbridge")
)
from nanobot_agentbridge.plans import PLAN, PlanTool


async def run():
    request = {"runId": "synthetic"}
    context = SimpleNamespace(
        get=AsyncMock(side_effect=AssertionError("Unexpected central admission"))
    )
    definition = SimpleNamespace(
        inputSchema={"properties": {"steps": {}, "goal": {}, "idempotency_key": {}}}
    )
    sink = "oa.leave.prepare"
    read = "oa.workflow.pending.list"
    allowed = {
        sink.replace(".", "_"): {
            "prepareCapability": sink,
            "commitCapability": "oa.leave.save_draft",
        }
    }
    scope = Mock(
        selection=("synthetic", "fill", "1", "pin"),
        binding="private",
        capabilities={read, sink},
    )
    scope.permits.side_effect = lambda name: name == PLAN
    scope.private_prepare_scope.return_value = None
    step = lambda capability: {
        "kind": "capability",
        "stepKey": capability,
        "capabilityName": capability,
        "arguments": {},
    }
    base = PlanTool(
        request, context, definition, None, {read.replace(".", "_")}, allowed
    )
    scoped = PlanTool(
        request, context, definition, scope, {read.replace(".", "_")}, allowed
    )
    assert "idempotency_key" not in base.parameters["properties"]
    checks = [
        (base, [step(sink), step(read)], "最后一步"),
        (base, [step(sink), step(sink)], "最后一步"),
        (base, [step("oa.leave.save_draft")], "尚未开放"),
        (base, [step("oa.session.login")], "尚未开放"),
        (base, [step("other.system.prepare")], "尚未开放"),
        (base, [dict(step(sink), arguments={"approved": True})], "受保护"),
        (base, [dict(step(sink), bindings={"authorization_id": {}})], "受保护"),
        (scoped, [step(sink)], "固定模式"),
        (scoped, [step("oa.workflow.sent.list")], "固定模式"),
    ]
    for tool, steps, expected in checks:
        assert expected in await tool.submit({"goal": "合成范围校验", "steps": steps})
    scope.permits.return_value = False
    scope.permits.side_effect = None
    assert "未开放复合计划" in await scoped.submit({"steps": [step(read)]})
    context.get.assert_not_awaited()
    return {
        "deniedBeforeTaskCreation": len(checks) + 1,
        "privateIdempotencyKeyHidden": True,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run())))
