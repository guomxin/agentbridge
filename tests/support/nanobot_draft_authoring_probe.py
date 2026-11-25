"""Run the Telegram SDK entry point, central authoring and a loopback no-tools model."""

import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "integrations/nanobot-agentbridge"), str(ROOT / "scripts")]
from mcp.types import Tool as Definition
from nanobot.config.loader import set_config_path
from nanobot.config.schema import Config
from nanobot_agentbridge.draft_tools import AUTHORING, DraftTool
from nanobot_agentbridge.runtime import Conversation, run_readonly
from nanobot_agentbridge.skills import SkillRun
from agentbridge.core.business_skills import skill_catalog
from agentbridge.core.central_service import CentralCapabilityService
from tests.support.nanobot_sdk_probe import MODEL, model_server
from validation_plan import test_environment


PROPOSAL = {"name": "合成周报", "description": "从提供的文字生成周报",
            "selection": {"use_when": "整理周报", "not_for": "审批业务", "output": "完成事项和待办"},
            "instructions": "只按输入归纳，缺少依据则标记未知。"}
SAMPLE = "合成样本：测试功能已完成。"


class Model:
    def __init__(self):
        self.failures = []
        self.samples = 0
        self.refuse = False

    def reply(self, body):
        if not body.get("tools"):
            assert [m["role"] for m in body["messages"]] == ["system", "user"]
            assert body["messages"][1]["content"] == SAMPLE
            assert "SKILL.md" in body["messages"][0]["content"]
            self.samples += 1
            if self.refuse:
                return None, {"id": "refused", "type": "function",
                              "function": {"name": "oa_submit", "arguments": "{}"}}
            return "合成完成事项。", None
        names = [t["function"]["name"] for t in body["tools"]]
        tool_messages = [item for item in body["messages"] if item["role"] == "tool"]
        if tool_messages:
            assert "draft_saved" in str(body["messages"])
            if len(tool_messages) == 1:
                return None, {"id": "stale", "type": "function",
                              "function": {"name": "mcp_ab_oa_session_status", "arguments": "{}"}}
            assert "not found" in str(tool_messages[-1])
            return "个人草稿已保存。", None
        assert set(names) == {DraftTool.name, "mcp_ab_oa_session_status"}
        return None, {"id": "save", "type": "function", "function": {"name": DraftTool.name,
            "arguments": json.dumps({"action": "save", "data": {"proposal": PROPOSAL}}, ensure_ascii=False)}}


class Session:
    def __init__(self, service, subject):
        self.service = service
        self.subject = subject
        self.paired = True
        self.actions = []

    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=[Definition(name=name, inputSchema={"type": "object"}) for name in (AUTHORING, "oa_session_status")], nextCursor=None)

    async def call_tool(self, name, arguments, *, meta):
        if name == "agentbridge_host_identity_profile":
            sender = "501" if self.subject == "alice" else "502"
            expected = {"botId": "8001", "senderId": sender}
            assert meta["io.agentbridge/telegram-identity"] == expected
            value = {"host": {"hostInstanceId": "office"}, "identity": {"userSubject": self.subject},
                     "telegramIdentityVerified": self.paired}
        else:
            assert name == AUTHORING
            self.actions.append(arguments["action"])
            value = self.service.skill_authoring.dispatch(self.subject, arguments["action"], arguments["data"])
        return SimpleNamespace(isError=False, structuredContent=value)


async def noop(*args):
    pass


async def run(home, config, model):
    service = CentralCapabilityService(home=home / "central", base_url="http://127.0.0.1:1")
    def user(subject, sender):
        req = {"subject": subject, "instance": "office", "endpointKey": "telegram:8001:" + sender,
               "endpoint": str(uuid4()), "runId": str(uuid4()), "controlledPrepares": {}}
        meta = {"agentHost": "nanobot", "hostInstanceId": "office"}
        session = Session(service, subject)
        ctx = SimpleNamespace(session=session, host_meta=meta, finish_confirmed_read=noop)
        skill = SkillRun(session, noop)
        return req, ctx, skill, DraftTool(config, req, ctx, skill)
    req, ctx, skill, alice = user("alice", "501")
    _, _, _, bob = user("bob", "502")
    answer = await run_readonly(config, ctx.session, Conversation("alice", req["endpoint"], "private"),
        "把归纳周报的方法存为个人助手。", noop, channel="telegram", coordinator_tool=(req, ctx),
        skill_run=skill, allowed_tools=[AUTHORING, "oa_session_status"])
    assert "保存" in answer
    async def call(action, data):
        return json.loads(await alice.execute(action=action, data=data))
    draft, = (await call("list", {}))["items"]
    target = {"draft_id": draft["draft_id"], "expected_revision": draft["revision"]}
    assert draft["revision"] == 1
    assert (await call("save", {"proposal": PROPOSAL}))["draft_id"] == draft["draft_id"]
    assert (await bob.execute(action="get", data={"draft_id": draft["draft_id"]})).is_error
    assert (await bob.execute(action="list", data={})) == '{"items": []}'
    assert (await alice.execute(action="test_result", data={"test_id": "invented", "output": "伪造"})).is_error
    base = {**target, "reason": "本人使用", "expected_report_job_id": None}
    assert not alice.validate_params({"action": "submit", "data": base})
    assert (await alice.execute(action="submit", data=base)).is_error
    sample = {**target, "profile": "use", "prompt": SAMPLE}
    original = await call("sample", sample)
    assert original["output"] == "合成完成事项。"
    assert (await call("sample", sample))["test_id"] == original["test_id"] and model.samples == 1
    case = {"profile": "use", "kind": "output", "prompt": SAMPLE, "contains": ["完成"]}
    job = await call("evaluate", {**target, "cases": [case]})
    assert job["state"] == "queued"
    bench = service.skill_authoring.workbench
    while bench.run_once(lambda system, prompt: {"text": "合成完成事项。"}):
        pass
    assert (await call("jobs", {"job_id": job["job_id"]}))["state"] == "succeeded"
    inspection = await call("inspect", {"draft_id": draft["draft_id"]})
    assert inspection["publication_evidence"]["report_job_id"] == job["job_id"]
    assert (await alice.execute(action="submit", data=base)).is_error
    assert (await alice.execute(action="submit", data={**base, "audience": ["bob"]})).is_error
    submitted = await call("submit", {**base, "expected_report_job_id": job["job_id"]})
    assert submitted["status"] == "submitted" and submitted["published"] is False
    assert (await call("submit", {**base, "expected_report_job_id": job["job_id"]})) == submitted
    assert service.skill_authoring.review_detail(submitted["request_id"])["audience"] == ["alice"]
    assert not skill_catalog(service, "alice")["items"]
    updated = await call("save", {**target, "proposal": {**PROPOSAL, "name": "修订后的周报"}})
    assert updated["revision"] == 2
    assert (await alice.execute(action="sample", data=sample)).is_error
    model.refuse = True
    assert (await alice.execute(action="sample", data={**sample, "expected_revision": 2})).is_error
    detail = service.skill_authoring.get("alice", draft["draft_id"])
    assert not detail["tested_profiles"]
    count = len(ctx.session.actions)
    ctx.session.paired = False
    assert (await alice.execute(action="list", data={})).is_error
    ctx.session.paired = True
    skill.pending = True
    assert (await alice.execute(action="list", data={})).is_error
    assert len(ctx.session.actions) == count and "decide" not in ctx.session.actions
    assert model.samples == 2


def main():
    with test_environment() as env, tempfile.TemporaryDirectory() as temp:
        os.environ["AGENTBRIDGE_SESSION_KEY_FILE"] = env["AGENTBRIDGE_SESSION_KEY_FILE"]
        home = Path(temp)
        model = Model()
        http = model_server(model)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        try:
            api = {"preferredApi": "chat_completions", "supportedApis": ["chat_completions"]}
            set_config_path(home / "config.json")
            config = Config.model_validate({"providers": {"custom": {"apiKey": "synthetic", "api": api,
                    "apiBase": f"http://127.0.0.1:{http.server_port}/v1"}},
                "agents": {"defaults": {"model": MODEL, "provider": "custom", "api": api, "workspace": str(home / "sdk")}}})
            config.bind_source_path(home / "config.json")
            asyncio.run(run(home, config, model))
            assert not model.failures
        finally:
            http.shutdown()
            http.server_close()
    print("Telegram SDK draft creation, isolated samples/evaluation, owner isolation, review and replay passed.")


if __name__ == "__main__":
    main()
