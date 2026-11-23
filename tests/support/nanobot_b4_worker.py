"""Run the actual NanoBot host behind the isolated B4 Workspace/Admin and Telegram simulation."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from uuid import uuid4

from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "integrations/nanobot-agentbridge")]
from nanobot_agentbridge.server import create_app

from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from tests.support.nanobot_sdk_probe import MODEL, QUESTION, Scenario, model_server
from tests.support.nanobot_telegram_console import run_console


class Model(Scenario):
    def reply(self, body):
        content, call = self._reply(body)
        if call:
            call = {**call, "id": "synthetic-b4-" + uuid4().hex}
        return content, call

    def _reply(self, body):
        if self.secret in json.dumps(body):
            raise ValueError("private user identity in model request")
        messages = body["messages"]
        start = max(
            (i for i, m in enumerate(messages) if m.get("role") == "user"), default=0
        )
        turn = messages[start:]
        content = turn[0].get("content", "")
        text = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False)
        )
        if "合成长文私聊" in text:
            return "合成长文开始" + "甲🟢" * 2200 + "合成长文结束", None
        for question, answer in (("合成网页问答", "合成网页答复"), ("合成电报问答", "合成电报答复")):
            if question in text:
                return answer, None
        names = {t["function"]["name"] for t in body.get("tools", [])}
        if not names and any(
            m.get("role") == "system" and "业务方法编辑器" in str(m.get("content"))
            for m in messages
        ):
            return json.dumps(
                {"kind": "fact", "summary": "合成材料已归类", "proposal": None},
                ensure_ascii=False,
            ), None
        if isinstance(content, list):
            images = [
                part["image_url"]["url"]
                for part in content
                if part.get("type") == "image_url"
            ]
            if images:
                for uri in images:
                    if not uri.startswith("data:image/"):
                        raise ValueError("Expected an isolated inline image")
                blob = base64.b64decode(images[0].split(",", 1)[1], validate=True)
                return (
                    f"已收到 {len(images)} 张图片，首张 {len(blob)} 字节，SHA-256 前八位 {hashlib.sha256(blob).hexdigest()[:8]}。",
                    None,
                )
        if '"sample":' in text and '"resources":' in text:
            if names:
                raise ValueError("Native tools reached an isolated sample")
            return "计划：修复登录问题，尚未完成。", None

        def invoke(name, params):
            return None, {
                "id": "call_b4_" + str(len(turn)),
                "type": "function",
                "function": {"name": "mcp_ab_" + name, "arguments": json.dumps(params)},
            }

        tools = [m for m in turn if m.get("role") == "tool"]
        if "技能" in text or "业务助手" in text:
            if not tools:
                return invoke("agentbridge_skill_catalog", {})
            if len(tools) == 1:
                if "oa-work-log" not in str(tools[0]["content"]):
                    return "当前账号的目录没有分配该助手。", None
                if "mcp_ab_agentbridge_skill_get" not in names:
                    return "当前仅可查看目录。", None
                return invoke(
                    "agentbridge_skill_get",
                    {
                        "skill_id": "oa-work-log",
                        "profile": "preview",
                        "expected_version": "1.1.2",
                    },
                )
            if len(tools) == 2:
                if "SKILL.md" not in str(tools[-1]["content"]):
                    raise ValueError("No fixed skill bundle")
                return invoke(
                    "agentbridge_skill_get",
                    {
                        "skill_id": "oa-work-log",
                        "profile": "fill",
                        "expected_version": "1.1.2",
                    },
                )
            if len(tools) == 3:
                if "中央服务未返回" not in str(tools[-1]["content"]):
                    raise ValueError("Scope change was not rejected")
                return invoke("oa_workflow_pending_list", {"limit": 3})
            if "采购申请" not in str(tools[-1]["content"]):
                raise ValueError("Expected original bound read")
            return (
                "已按工作日志助手预览模式核对采购申请、验收申请两项合成待办；改选填写模式的调用已拒绝。",
                None,
            )
        original = {
            **body,
            "messages": turn,
            "tools": [
                t
                for t in body.get("tools", [])
                if t["function"]["name"]
                in {
                    "mcp_ab_agentbridge_server_profile",
                    "mcp_ab_agentbridge_skill_catalog",
                    "mcp_ab_oa_session_status",
                    "mcp_ab_oa_workflow_pending_list",
                }
            ],
        }
        if len(tools) == 1 and "agentToolAccess" in str(tools[0].get("content", "")):
            if self.expect_read:
                return invoke("oa_workflow_pending_list", {"limit": 3})
            return "当前账号未获待办查询权限。", None
        if len(tools) >= 2 and "采购申请" in str(tools[-1]["content"]):
            return "您有采购申请、验收申请两项待办。", None
        return super().reply(original)


async def main():
    opts = argparse.ArgumentParser()
    opts.add_argument("--home", type=Path, required=True)
    opts.add_argument("--coordinator", type=Path, required=True)
    args = opts.parse_args()
    home = args.home
    info = json.loads((home / "public.json").read_text())
    bindings = json.loads((home / "bindings.json").read_text())
    folder = home / "b4host"
    folder.mkdir(mode=0o700, exist_ok=True)
    models = []
    servers = []
    specs = []
    try:
        for index, binding in enumerate(bindings):
            label = ("a", "b")[index]
            key = home / "mcp" / (label + ".token")
            scenario = Model(
                binding["user_subject"], index == 0, key.read_text().strip()
            )
            model = model_server(scenario)
            models.append(scenario)
            servers.append(model)
            threading.Thread(target=model.serve_forever, daemon=True).start()
            api = {
                "preferredApi": "chat_completions",
                "supportedApis": ["chat_completions"],
            }
            config = folder / f"{label}.json"
            config.write_text(
                json.dumps(
                    {
                        "providers": {
                            "custom": {
                                "apiKey": "synthetic-fixture",
                                "apiBase": f"http://127.0.0.1:{model.server_port}/v1",
                                "api": api,
                            }
                        },
                        "agents": {
                            "defaults": {
                                "provider": "custom",
                                "model": MODEL,
                                "api": api,
                                "maxToolIterations": 6,
                            }
                        },
                    }
                )
            )
            config.chmod(0o600)
            specs.append(
                {**binding, "tokenFile": str(key), "modelConfigFile": str(config)}
            )
        instance = "nanobot-office"
        app = create_app(
            {
                "instanceId": instance,
                "stateDir": str(folder / "state"),
                "serviceTokenFile": str(home / "service.token"),
                "trustedCardOrigins": ["https://localhost:41449"],
                "coordinatorProgram": str(args.coordinator),
                "enableSkills": True,
                "enableSkillTrials": True,
                "telegramPilot": {"botId": 8001, "username": "fixture_bot"},
                "mcpUrl": info["mcp"],
                "bindings": specs,
            }
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", info["worker_port"])
        await site.start()
        client = NanoBotGatewayClient(
            url=f"http://127.0.0.1:{info['worker_port']}",
            instance_id=instance,
            token_file=home / "service.token",
        )
        try:
            await run_console(
                folder / "telegram", client, bindings, QUESTION, models, pairing=True
            )
        finally:
            await runner.cleanup()
    finally:
        for model in servers:
            model.shutdown()
            model.server_close()


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(main())
