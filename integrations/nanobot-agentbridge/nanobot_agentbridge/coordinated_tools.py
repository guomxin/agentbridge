"""Fixed model-facing prepare entry. The sidecar alone owns authorization, resume and verification."""

import asyncio
import json
from pathlib import Path

from nanobot.agent.tools.base import Tool


PROTECTED_ARGUMENTS = frozenset({"idempotency_key","authorization_id","input_submission_id","approved","decision"})


class CoordinatorTool(Tool):
    _plugin_discoverable = False
    def __init__(self, request, context, definition, skill_scope):
        self.request = request
        self.context = context
        self.definition = definition
        self.skill_scope = skill_scope
        schema=dict(definition.inputSchema)
        props=schema.get("properties",{})
        self.schema={**schema,"properties":{k:v for k,v in props.items() if k not in PROTECTED_ARGUMENTS},
                     "required":[k for k in schema.get("required",[]) if k not in PROTECTED_ARGUMENTS],
                     "additionalProperties":False}

    @property
    def name(self):
        return "mcp_ab_"+self.definition.name

    @property
    def description(self):
        return (self.definition.description or self.definition.name)+" 所有业务确认均须通过本人可信卡。"

    @property
    def parameters(self):
        return self.schema

    async def execute(self, **kwargs):
        skill_pin=None
        if (
            getattr(self, "skill_scope", None) is not None
            and self.skill_scope.selection is not None
        ):
            governed=self.request["controlledPrepares"].get(self.definition.name,{})
            skill_pin=self.skill_scope.private_prepare_scope(governed.get("prepareCapability"))
            if not skill_pin:
                return "本技能的运行范围没有开放此次受控操作。"

        if not set(kwargs)<=set(self.schema["properties"]):
            return "业务参数不属于此受控工具。"
        return await run_governed(self.request, self.context, self.definition.name, kwargs, skill_pin)


async def run_governed(request, context, tool_name, arguments, skill_pin, *, plan=False):
    """Hand the fixed run to the private coordinator. Only a public outcome returns to the model."""
    try:
        context.delegated = True
        meta = await context.get()
        payload = {
            k: request[k]
            for k in (
                "runId",
                "subject",
                "endpoint",
                "endpointKey",
                "conversation",
                "instance",
                "mcpUrl",
                "caFile",
                "tokenFile",
                "userDir",
                "trustedCardOrigins",
            )
        }
        for key in ("workspaceEndpointId","workspaceEndpointKey"):
            if key in request:
                payload[key]=request[key]
        if skill_pin:
            payload["skillPin"]=skill_pin
        payload.update(task=meta["io.agentbridge/task"], tool=tool_name, arguments=arguments)
        if plan:
            payload["mode"] = "durable_plan"
        proc = await asyncio.create_subprocess_exec(
            request["coordinatorProgram"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=Path(request["userDir"]),
            limit=8192,
        )
        try:
            async with asyncio.timeout(244):
                data, _ = await proc.communicate(json.dumps(payload).encode())
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        outcome = (
            json.loads(data).get("outcome")
            if proc.returncode == 0 and len(data) < 200
            else "unknown"
        )
        if plan and outcome in {"expired", "canceled", "failed"}:
            return {"expired": "原计划的可信卡已过期。", "canceled": "原计划已取消。", "failed": "原计划未能完成。"}[outcome]
        return {
            "verified": ("中央业务操作记录已核验请假草稿保存成功，流程尚未提交。" if tool_name=="oa_leave_prepare" else "中央已核验原任务的业务操作成功。"),
            "closed": "可信交互已关闭，本次未核验写入。",
            "waiting": "可信办理卡仍在等待你的确认，后台将继续监听；请在卡片有效期内打开核对并确认。",
        }.get(outcome, "本次业务结果有待核对，请在原任务中查看。")
    except Exception:
        return "本次业务结果有待核对，请在原任务中查看。"
