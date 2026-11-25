"""Personal Telegram draft operations. Sample generation has no access to the chat tools."""

import hashlib
import json


from .skill_scope import sample_resources
from .telegram_identity import identifier, verify_telegram_binding


AUTHORING = "agentbridge_skill_authoring"
ACTIONS = {
    "list": ((), ()),
    "get": (("draft_id",), ()),
    "save": (("proposal",), ("draft_id", "expected_revision")),
    "sample": (("draft_id", "expected_revision", "profile", "prompt"), ()),
    "evaluate": (("draft_id", "expected_revision", "cases"), ("repeats",)),
    "jobs": ((), ("job_id",)),
    "inspect": (("draft_id",), ()),
    "submit": (("draft_id", "expected_revision", "reason", "expected_report_job_id"), ("profiles",)),
}
DESCRIPTION = (
    "在本人已配对的 Telegram 私聊创作业务助手。list/get 查自己的草稿。"
    "save 新建或修改，proposal 包含 name、description、selection（use_when/not_for/output）、"
    "instructions，可选 profiles（纯文字默认 use），修改须给 draft_id 与当前 expected_revision。"
    "sample 用指定模式和合成 prompt 试用固定修订，独立模型无业务工具。"
    "evaluate 后台独立评测，cases 每题含 profile、prompt、kind 和 contains/excludes 字符串数组，"
    "output 至少一条断言，trigger 需 expected_trigger。jobs 查进度和结果。"
    "inspect 查看最新修订的送审依据和报告编号。本人明确要求送审时用 submit，传入刚查看的"
    " expected_report_job_id（无报告时 null）及理由。仅个人范围，须管理员另行审批。"
)


class DraftActions:
    _plugin_discoverable = False
    name = "mcp_ab_" + AUTHORING
    description = DESCRIPTION

    def __init__(self, request, context, skill_run, *, registry=None, complete, error, host_type):
        self.complete = complete
        self.error = error
        self.host_type = host_type
        self.request = request
        self.context = context
        self.skill_run = skill_run
        self.registry = registry

    @property
    def parameters(self):
        data = {
            "draft_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1},
            "proposal": {"type": "object"}, "profile": {"type": "string"}, "prompt": {"type": "string"},
            "cases": {"type": "array", "items": {"type": "object"}}, "repeats": {"type": "integer"},
            "job_id": {"type": "string"}, "reason": {"type": "string"},
            "expected_report_job_id": {"type": ["string", "null"]},
            "profiles": {"type": "array", "items": {"type": "string"}},
        }
        return {"type": "object", "properties": {"action": {"type": "string", "enum": list(ACTIONS)},
                "data": {"type": "object", "properties": data, "additionalProperties": False}},
                "required": ["action", "data"], "additionalProperties": False}

    async def check_binding(self):
        if not await verify_telegram_binding(self.context.session, self.request["instance"],
                self.request["subject"], self.context.host_meta, self.request["endpointKey"], host_type=self.host_type):
            raise PermissionError("本人私聊配对不可用。")

    async def call(self, action, data):
        meta = self.context.host_meta
        if self.host_type == "pi" and action == "evaluate":
            meta = {**meta, "io.agentbridge/skill-telegram-origin": {
                "endpointId": self.request["endpoint"], "endpointKey": self.request["endpointKey"],
                "conversationRef": self.request["conversation"], "pairingRef": self.request["pairingRef"]}}
        reply = await self.context.session.call_tool(AUTHORING, {"action": action, "data": data}, meta=meta)
        if reply.isError or not isinstance(reply.structuredContent, dict):
            raise ValueError("中央未确认此次草稿操作。")
        return reply.structuredContent

    async def sample(self, data):
        start = await self.call("test", data)
        test_id = start.get("test_id")
        if (not identifier(test_id) or start.get("status") != "running"
                or start.get("mode") != "isolated_sample" or start.get("prompt") != data["prompt"]):
            raise ValueError("样例收据不匹配。")
        # test_start returns the original receipt on replay. The durable row owns the output.
        draft = await self.call("get", {"draft_id": data["draft_id"]})
        row = next((row for row in draft.get("tests", []) if row.get("test_id") == test_id), None)
        if (not row or draft.get("owner_subject") != self.request["subject"]
                or draft.get("revision") != data["expected_revision"] or
                any(row.get(k) != v for k, v in {"draft_id": data["draft_id"], "owner_subject": self.request["subject"],
                    "revision": data["expected_revision"], "prompt": data["prompt"], "profile": data["profile"]}.items())):
            raise ValueError("样例与当前私有草稿修订不符。")
        if row.get("status") == "reported":
            return {"test_id": test_id, "status": "reported", "output": row["output"]}
        if row.get("status") != "running":
            raise ValueError("样例状态已变。")
        resources = sample_resources(start["bundle"], data["profile"])
        system = "\n\n".join([start["instruction"], "草稿模式 " + data["profile"],
                              *(path + "\n" + content for path, content in resources.items())])
        result = await self.complete(system, data["prompt"])
        output = result["text"]
        if result.get("execution") != "tool_free_completion" or not 0 < len(output.strip()) <= 16000:
            raise ValueError("隔离样例未返回完整输出。")
        await self.check_binding()
        saved = await self.call("test_result", {"test_id": test_id, "output": output})
        if saved.get("test_id") != test_id or saved.get("status") != "reported":
            raise ValueError("样例结果待核对。")
        return {**saved, "output": output}

    async def execute(self, action=None, data=None, **extra):
        if (extra or not isinstance(action, str) or action not in ACTIONS or not isinstance(data, dict)
                or self.skill_run is None or self.skill_run.pending or self.skill_run.selection):
            return self.error("当前模式未开放此私有草稿操作。")
        required, optional = ACTIONS[action]
        if (not set(required) <= data.keys() <= set(required + optional)
                or (action == "save" and ("draft_id" in data) != ("expected_revision" in data))):
            return self.error("请核对本动作的草稿参数及当前修订。")
        # A model may repeat a stale business call after reading a draft or its sample.
        # Keep the rest of this SDK turn scoped to authoring before any private read.
        if self.registry is not None:
            for name in self.registry.tool_names:
                if name != self.name:
                    self.registry.unregister(name)
        data = dict(data)
        try:
            await self.check_binding()
            if action in {"save", "sample", "evaluate", "submit"}:
                key = json.dumps([self.request["runId"], action, data], sort_keys=True).encode()
                data["request_key"] = self.host_type + ":draft:" + hashlib.sha256(key).hexdigest()
            if action == "sample":
                value = await self.sample(data)
            else:
                value = await self.call(action, data)
            if action == "submit" and (not identifier(value.get("request_id")) or value.get("published") is not False
                                        or value.get("status") != "submitted"):
                raise ValueError("管理员队列回执待核对。")
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return self.error("中央未确认该草稿操作。请查看本人草稿、修订和最新报告；送审仍须通过当前修订的检查。")
