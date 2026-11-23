"""Isolated SDK exercise. All remote MCP traffic must target a test loopback port."""

import argparse
import asyncio
from dataclasses import dataclass, field
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

PROFILE = "agentbridge_server_profile"
SESSION = "oa_session_status"
PENDING = "oa_workflow_pending_list"
QUESTION = "请查询我的办公待办，有权限时列出前两项。"
MODEL = "agentbridge-sdk-probe"


def parse_result(content):
    raw = (
        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    )
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(raw[index:])
            except ValueError:
                continue
            if isinstance(value, dict) and "agentToolAccess" in value:
                return value
    raise ValueError("missing_profile_payload")


@dataclass
class Scenario:
    key: str
    expect_read: bool
    secret: str = field(repr=False)
    capability: str = PENDING
    scope_result: bool | None = None
    tool_requested: int = 0
    verified_model_input: int = 0
    failures: list[str] = field(default_factory=list)
    pending_verified: bool = False
    private_marker: str = ""

    def reply(self, data):
        request_body = json.dumps(data, ensure_ascii=False)
        if self.secret in request_body or (
            self.private_marker and self.private_marker in request_body
        ):
            raise ValueError("private_data_in_model_input")
        self.verified_model_input += 1
        functions = [
            item.get("function", {}).get("name", "") for item in data.get("tools", [])
        ]
        if any(
            name not in {"mcp_ab_" + tool for tool in (PROFILE, SESSION, PENDING, "agentbridge_skill_catalog")}
            for name in functions
        ):
            raise ValueError("unexpected_tool_in_model_catalog")
        target = "mcp_ab_" + PROFILE
        if not data.get("messages"):
            raise ValueError("no_messages")
        tool_messages = [
            item for item in data["messages"] if item.get("role") == "tool"
        ]
        if self.private_marker:
            if tool_messages:
                if "需要可信交互" not in str(tool_messages[-1]["content"]):
                    raise ValueError("missing_public_interaction_status")
                return "请查看个人卡片。", None
            self.tool_requested += 1
            return None, {
                "id": "call_private",
                "type": "function",
                "function": {"name": "mcp_ab_" + SESSION, "arguments": "{}"},
            }
        if tool_messages:
            if len(tool_messages) == 2 and self.scope_result:
                payload = str(tool_messages[-1].get("content", ""))
                self.pending_verified = all(
                    title in payload for title in ("采购申请", "验收申请")
                )
                if not self.pending_verified:
                    raise ValueError("missing_synthetic_read_results")
                return "您有采购申请、验收申请两项待办。", None
            profile = parse_result(tool_messages[-1].get("content", ""))
            allowed = profile["agentToolAccess"]["allowedToolNames"]
            assert type(allowed) is list
            if set(allowed) != {name.removeprefix("mcp_ab_") for name in functions}:
                raise ValueError("model_profile_exceeds_host_tool_registry")
            self.scope_result = self.capability in allowed
            if self.scope_result != self.expect_read:
                raise ValueError("wrong_scope_projection")
            if self.scope_result:
                if "mcp_ab_" + PENDING not in functions:
                    raise ValueError("missing_projected_pending_tool")
                return None, {
                    "id": "call_pending",
                    "type": "function",
                    "function": {
                        "name": "mcp_ab_" + PENDING,
                        "arguments": '{"limit":3}',
                    },
                }
            if "mcp_ab_" + PENDING in functions:
                raise ValueError("unexpected_pending_tool_for_limited_user")
            return "当前账号未获待办查询权限。", None
        if target not in functions:
            raise ValueError("profile_tool_not_registered")
        self.tool_requested += 1
        return None, {
            "id": "call_probe_profile",
            "type": "function",
            "function": {"name": target, "arguments": "{}"},
        }


def model_server(scenario):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            try:
                if self.path != "/v1/chat/completions":
                    raise ValueError("unexpected_model_route")
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content, call = scenario.reply(data)
                base = {
                    "id": "chatcmpl-synthetic",
                    "model": MODEL,
                    "created": int(time.time()),
                }
                finish = "tool_calls" if call else "stop"
                if data.get("stream"):
                    packets = [{"role": "assistant"}]
                    if call:
                        packets.append({"tool_calls": [{"index": 0, **call}]})
                    else:
                        packets.extend(
                            {"content": part} for part in (content[:3], content[3:])
                        )
                    packets.append({})
                    frames = []
                    for index, part in enumerate(packets):
                        chunk = {
                            **base,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": part,
                                    "finish_reason": finish
                                    if index == len(packets) - 1
                                    else None,
                                }
                            ],
                        }
                        frames.append(
                            "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                        )
                    raw = ("".join(frames) + "data: [DONE]\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    item = {"role": "assistant", "content": content}
                    if call:
                        item["tool_calls"] = [call]
                    raw = json.dumps(
                        {
                            **base,
                            "object": "chat.completion",
                            "choices": [
                                {"index": 0, "message": item, "finish_reason": finish}
                            ],
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, ValueError) and str(exc).isidentifier()
                    else type(exc).__name__
                )
                scenario.failures.append(reason)
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                raw = json.dumps({"error": {"message": reason}}).encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)


async def card_session(marker):
    from mcp import types
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session

    fixture = FastMCP("Synthetic card probe")

    @fixture.tool(name=PROFILE, structured_output=True)
    async def profile() -> dict[str, dict[str, list[str]]]:
        return {"agentToolAccess": {"allowedToolNames": [SESSION]}}

    @fixture.tool(name=SESSION)
    async def status() -> types.CallToolResult:
        card = {
            "schemaVersion": "agentbridge.interaction.v1",
            "interactionId": "synthetic-interaction-01",
            "type": "credential",
            "state": "pending",
            "presentation": {
                "owner": "agentbridge",
                "modelMustNotCollectValues": True,
                "url": "https://cards.invalid/flow/input/" + marker,
            },
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="synthetic public message")],
            structuredContent={"public": "synthetic"},
            _meta={"io.agentbridge/interaction": card},
        )

    return create_connected_server_and_client_session(fixture)


async def run_one(args):
    from importlib.metadata import version
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from nanobot.agent.loop import AgentLoop
    from nanobot import Nanobot
    from nanobot.config.loader import load_config, set_config_path

    candidate = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(candidate / "integrations" / "nanobot-agentbridge"))
    from nanobot_agentbridge.runtime import CentralTools, Conversation, run_readonly

    assert version("nanobot-ai") == "0.4.2"
    root = Path(args.data_dir).resolve()
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    secret = Path(args.token_file).read_text().strip()
    assert secret
    scenario = Scenario(args.user, args.expected == "yes", secret)
    httpd = model_server(scenario)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    endpoint = urlsplit(args.mcp_url)
    assert (
        endpoint.scheme == "http"
        and endpoint.hostname == "127.0.0.1"
        and endpoint.port
        and endpoint.path == "/mcp"
    )
    api = {"supportedApis": ["chat_completions"], "preferredApi": "chat_completions"}
    cfg = root / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "providers": {
                    "custom": {
                        "apiKey": "local-synthetic-only",
                        "apiBase": "http://127.0.0.1:" + str(httpd.server_port) + "/v1",
                        "api": api,
                    }
                },
                "agents": {
                    "defaults": {
                        "workspace": str(workspace),
                        "provider": "custom",
                        "model": MODEL,
                        "api": api,
                        "maxToolIterations": 4,
                    }
                },
                "tools": {"restrictToWorkspace": True},
            }
        )
    )
    cfg.chmod(0o600)
    set_config_path(cfg)
    config = load_config(cfg)
    binding = Conversation(args.user, "workspace-" + args.user, "acceptance")
    events = []
    streamed = []

    async def observe(kind):
        events.append(kind)

    async def output(delta):
        streamed.append(delta)
        if args.jsonl:
            print(
                json.dumps({"type": "text.delta", "delta": delta}, ensure_ascii=False),
                flush=True,
            )

    async def ignore(_delta):
        pass

    try:
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={"Authorization": "Bearer " + secret},
                    trust_env=False,
                    follow_redirects=False,
                    timeout=10.0,
                )
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(args.mcp_url, http_client=http)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            async with asyncio.timeout(45):
                answer = await run_readonly(
                    config, session, binding, QUESTION, output, observe=observe
                )
            denied = None
            if not scenario.expect_read:
                forced = await session.call_tool(PENDING, {"limit": 3})
                details = json.dumps(
                    forced.model_dump(by_alias=True), ensure_ascii=False
                )
                denied = (
                    forced.isError
                    or "PERMISSION" in details.upper()
                    or "FORBIDDEN" in details.upper()
                    or '"status": "denied"' in details
                    or "授权" in details
                ) and "采购申请" not in details
                assert denied, "central query denial not observed"
        assert (
            scenario.scope_result == scenario.expect_read
            and scenario.tool_requested == 1
            and not scenario.failures
        )
        assert scenario.pending_verified == scenario.expect_read
        assert (
            "text.delta" in events
            and "tool.started" in events
            and "tool.completed" in events
            and "run.completed" in events
        )
        assert "".join(streamed) == answer
        public_checked = scenario.verified_model_input

        # This second turn intentionally uses a synthetic MCP server and a separate
        # conversation. The private marker may travel only to the authenticated callback.
        scenario.private_marker = uuid.uuid4().hex
        card_binding = Conversation(args.user, binding.endpoint, "private-card")
        delivered = []

        async def deliver(target, card):
            assert target == card_binding
            assert card["presentation"]["url"].endswith(scenario.private_marker)
            delivered.append(target.endpoint)

        async with await card_session(scenario.private_marker) as synthetic:
            card_answer = await run_readonly(
                config,
                synthetic,
                card_binding,
                "请检查登录状态。",
                ignore,
                deliver_private=deliver,
                trusted_origins=frozenset({"https://cards.invalid"}),
            )
        assert card_answer == "请查看个人卡片。" and delivered == [binding.endpoint]
        assert (
            not scenario.failures
            and scenario.verified_model_input == public_checked + 2
        )
        loop = AgentLoop.from_config(config, tool_registry=CentralTools())
        async with Nanobot(loop, config=config) as reopened:
            persisted = await reopened.sessions.get(binding.session_key)
            card_history = await reopened.sessions.get(card_binding.session_key)
            assert persisted and any(
                msg.get("content") == answer for msg in persisted.messages
            )
            assert card_history and scenario.private_marker not in json.dumps(
                card_history.to_dict()
            )
        forbidden = (secret.encode(), scenario.private_marker.encode())
        for file in root.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                if any(value in data for value in forbidden):
                    raise ValueError("private_material_persisted")
        return {
            "user": args.user,
            "events": events,
            "tool": PROFILE,
            "pending_read_granted": scenario.scope_result,
            "pending_rows_verified": scenario.pending_verified,
            "forced_read_denied": denied,
            "model_requests_checked_for_bearer": public_checked,
            "synthetic_private_card": {
                "delivered_to_bound_endpoint": True,
                "model_and_disk_clean": True,
            },
            "answer": answer,
            "reopened_history": True,
            "ok": True,
        }
    finally:
        httpd.shutdown()
        httpd.server_close()
        if scenario.failures:
            print(json.dumps({"model_errors": scenario.failures}), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--expected", choices=["yes", "no"], required=True)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        try:
            receipt = asyncio.run(run_one(args))
        except Exception as exc:

            def report(error):
                stack = traceback.extract_tb(error.__traceback__)
                print(
                    json.dumps(
                        {
                            "failure_type": type(error).__name__,
                            "frames": [
                                (Path(f.filename).name, f.lineno) for f in stack[-4:]
                            ],
                        }
                    ),
                    file=sys.stderr,
                )
                for child in getattr(error, "exceptions", []):
                    report(child)

            report(exc)
            raise SystemExit(1)
        print(
            json.dumps(
                {"type": "receipt", "receipt": receipt} if args.jsonl else receipt,
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    area = Path(args.data_dir).resolve()
    area.mkdir(parents=True, exist_ok=True, mode=0o700)
    isolated = area / "process-home"
    isolated.mkdir(mode=0o700)
    tmp = area / "tmp"
    tmp.mkdir(mode=0o700)
    cmd = [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        *sys.argv[1:],
        "--child",
    ]
    clean_env = {
        "HOME": str(isolated),
        "TMPDIR": str(tmp),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "XDG_CACHE_HOME": str(area / "cache"),
        "XDG_CONFIG_HOME": str(area / "config"),
        "NO_PROXY": "*",
    }
    # Inherit stdout so a supervising Workspace receives real deltas as they arrive.
    out = subprocess.run(
        cmd, cwd=area, env=clean_env, stderr=subprocess.PIPE, text=True, timeout=105
    )
    if out.returncode:
        # Exception groups can include request details; only report safe classifications.
        for line in out.stderr.splitlines():
            stripped = line.strip(" |+-")
            if stripped.startswith(('{"model_errors"', '{"failure_type"')):
                print(stripped[:250], file=sys.stderr)
        raise SystemExit("SDK probe returned " + str(out.returncode))


if __name__ == "__main__":
    main()
