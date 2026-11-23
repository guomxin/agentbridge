"""Exercise the released SDK's stateless workbench calls against a local B4 MCP fixture."""

import argparse
import asyncio
import json
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "integrations/nanobot-agentbridge")]
from nanobot_agentbridge.server import create_app

from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from tests.support.nanobot_sdk_probe import MODEL, model_server
from tests.support.nanobot_transport_probe import write_private


class Model:
    def __init__(self, label, token):
        self.label = label
        self.token = token
        self.calls = 0
        self.failures = []

    def reply(self, body):
        messages = body["messages"]
        assert [item["role"] for item in messages] == ["system", "user"]
        assert self.token not in str(body) and not body.get("tools")
        assert messages[1]["content"] == "合成材料-" + self.label
        self.calls += 1
        return "已处理-" + self.label, None


async def run(home, coordinator):
    info = json.loads((home / "public.json").read_text())
    bindings = json.loads((home / "bindings.json").read_text())
    if not info["mcp"].startswith("http://127.0.0.1:") or len(bindings) != 2:
        raise ValueError("Only the local two-user synthetic fixture is accepted")
    with tempfile.TemporaryDirectory(prefix="agentbridge-skill-model-test-") as temp:
        root = Path(temp).resolve()
        token = root / "service.token"
        write_private(token, secrets.token_urlsafe(36))
        servers, models, records = [], [], []
        runner = None
        try:
            for label, binding in zip(("a", "b"), bindings, strict=True):
                personal = home / "mcp" / (label + ".token")
                scenario = Model(label, personal.read_text().strip())
                model = model_server(scenario)
                servers.append(model)
                models.append(scenario)
                threading.Thread(target=model.serve_forever, daemon=True).start()
                api = {
                    "preferredApi": "chat_completions",
                    "supportedApis": ["chat_completions"],
                }
                config = root / (label + ".json")
                write_private(
                    config,
                    json.dumps(
                        {
                            "providers": {
                                "custom": {
                                    "apiKey": "synthetic",
                                    "apiBase": f"http://127.0.0.1:{model.server_port}/v1",
                                    "api": api,
                                }
                            },
                            "agents": {
                                "defaults": {
                                    "provider": "custom",
                                    "model": MODEL,
                                    "api": api,
                                }
                            },
                        }
                    ),
                )
                records.append(
                    {
                        **binding,
                        "tokenFile": str(personal),
                        "modelConfigFile": str(config),
                    }
                )
            spec = {
                "instanceId": "nanobot-office",
                "stateDir": str(root / "state"),
                "serviceTokenFile": str(token),
                "mcpUrl": info["mcp"],
                "bindings": records,
                "trustedCardOrigins": ["https://localhost:41449"],
                "coordinatorProgram": str(coordinator),
                "enableSkills": True,
            }
            runner = web.AppRunner(create_app(spec), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            client = NanoBotGatewayClient(
                url=f"http://127.0.0.1:{port}",
                instance_id=spec["instanceId"],
                token_file=token,
            )
            for label, binding in zip(("a", "b"), bindings, strict=True):
                out = await asyncio.to_thread(
                    client.skill_completion,
                    binding,
                    "提炼限定的合成文字",
                    "合成材料-" + label,
                )
                assert (
                    out["text"] == "已处理-" + label
                    and out["execution"] == "tool_free_completion"
                )
            try:
                await asyncio.to_thread(
                    client.skill_completion,
                    {**bindings[0], "user_subject": bindings[1]["user_subject"]},
                    "系统",
                    "材料",
                )
            except GatewayRequestError:
                pass
            else:
                raise AssertionError("Mismatched skill model binding was accepted")
            assert [m.calls for m in models] == [1, 1] and not any(
                m.failures for m in models
            )
            assert not any((root / "state/users").rglob("*.jsonl"))
            print(
                json.dumps(
                    {
                        "verifiedSubjects": 2,
                        "textOnlyModelRequests": 2,
                        "presentedTools": 0,
                        "crossSubjectRejected": True,
                        "chatHistoriesWritten": 0,
                    }
                )
            )
        finally:
            if runner:
                await runner.cleanup()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--coordinator", required=True, type=Path)
    opts = parser.parse_args()
    asyncio.run(run(opts.home, opts.coordinator))
