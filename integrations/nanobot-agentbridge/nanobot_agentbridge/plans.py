"""Submit a durable plan against the original task, with stable identity and no blind replay."""

import hashlib
import json
from pathlib import Path
import sqlite3

from nanobot.agent.tools.base import Tool
from .coordinated_tools import PROTECTED_ARGUMENTS, run_governed

PLAN = "agentbridge_task_plan_prepare"


class PlanTool(Tool):
    _plugin_discoverable = False

    def __init__(self, request, context, definition, skill_scope, allowed_model_tools, allowed_prepares=None):
        self.request = request
        self.context = context
        self.definition = definition
        self.scope = skill_scope
        self.allowed_model_tools = allowed_model_tools
        self.allowed_prepares = allowed_prepares or {}

    @property
    def name(self):
        return "mcp_ab_" + PLAN

    @property
    def description(self):
        return "为本次请求提交一个可恢复的中央 V1/V2 任务计划。可组合已开放的读取与转换，最后可包含一次本人可信卡确认的受控准备。"

    @property
    def parameters(self):
        schema = dict(self.definition.inputSchema)
        schema["properties"] = {
            k: v
            for k, v in schema.get("properties", {}).items()
            if k != "idempotency_key"
        }
        schema["additionalProperties"] = False
        return schema

    async def execute(self, **proposal):
        try:
            return await self.submit(proposal)
        except Exception:
            return "原计划的受理结果待核对。"

    async def submit(self, proposal):
        selected = self.scope is not None and self.scope.selection is not None
        if self.scope is not None and not self.scope.permits(PLAN):
            return "此业务助手未开放复合计划。"
        if not proposal.get("steps") or not set(proposal) <= set(
            self.parameters["properties"]
        ):
            return "任务计划参数无效。"
        sink_tool = None
        skill_pin = None
        for index, step in enumerate(proposal["steps"]):
            if not isinstance(step, dict):
                return "任务计划格式无效。"
            if step.get("kind") == "capability":
                name = str(step.get("capabilityName", "")).replace(".", "_")
                if (
                    selected
                    and step.get("capabilityName") not in self.scope.capabilities
                ):
                    return "此步骤不在原业务助手的固定模式内。"
                capability = step.get("capabilityName")
                if name in self.allowed_prepares and capability == self.allowed_prepares[name]["prepareCapability"]:
                    if sink_tool or index != len(proposal["steps"]) - 1:
                        return "受控准备只可出现在计划最后一步。"
                    if any(PROTECTED_ARGUMENTS.intersection(step.get(field, {})) for field in ("arguments", "bindings")):
                        return "受保护的办理参数须由可信卡提供。"
                    if selected:
                        skill_pin = self.scope.private_prepare_scope(capability)
                        if not skill_pin:
                            return "此步骤不在原业务助手的固定模式内。"
                    sink_tool = name
                elif name not in self.allowed_model_tools or name.endswith(("_prepare", "_login")):
                    return "计划含有本宿主尚未开放的步骤。"
            elif step.get("kind") != "transform":
                return "任务计划只接受中央能力和已登记转换。"
        req = self.request
        ledger = Path(req["userDir"]) / "plans.sqlite3"
        self.context.delegated = True
        meta = await self.context.get()
        if selected:
            meta = {**meta, "agentbridge/skill": {"bindingId": self.scope.binding}}
        task = meta["io.agentbridge/task"]["taskId"]
        digest = hashlib.sha256(
            json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with sqlite3.connect(ledger) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS plans(run TEXT PRIMARY KEY,task TEXT NOT NULL,digest TEXT NOT NULL,plan TEXT)"
            )
            new = db.execute(
                "INSERT OR IGNORE INTO plans VALUES(?,?,?,NULL)",
                (req["runId"], task, digest),
            ).rowcount
            prior = db.execute(
                "SELECT task,digest,plan FROM plans WHERE run=?", (req["runId"],)
            ).fetchone()
        if prior[:2] != (task, digest):
            return "原任务已登记其他计划。"
        if new and sink_tool:
            return await run_governed(req, self.context, sink_tool, proposal, skill_pin, plan=True)
        if new:
            raw = await self.context.session.call_tool(
                PLAN,
                {**proposal, "idempotency_key": "nanobot:" + req["runId"]},
                meta=meta,
            )
            data = raw.structuredContent or {}
            plan = data.get("plan") or {}
            ref = plan.get("planId")
            if raw.isError or not ref or plan.get("taskId") != task:
                return "原计划的受理结果待核对。"
            with sqlite3.connect(ledger) as db:
                db.execute(
                    "UPDATE plans SET plan=? WHERE run=? AND task=?",
                    (ref, req["runId"], task),
                )
        else:
            ref = prior[2]
        if not ref:
            return "原计划的受理结果待核对。"
        status = await self.context.session.call_tool(
            "agentbridge_task_plan_get", {"plan_id": ref}, meta=meta
        )
        plan = (status.structuredContent or {}).get("plan") or {}
        if status.isError or plan.get("taskId") != task or plan.get("planId") != ref:
            return "原计划状态待核对。"
        return json.dumps(
            {"计划状态": plan["state"], "步骤数": len(plan.get("steps", []))},
            ensure_ascii=False,
        )
