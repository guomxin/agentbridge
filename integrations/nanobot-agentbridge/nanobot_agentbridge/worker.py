"""An isolated SDK worker owns its subject's state and authenticated MCP session."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import re
import ssl
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from nanobot.config.loader import set_config_path
from nanobot.config.schema import Config

from nanobot_agentbridge.cards import wait_for_credential
from nanobot_agentbridge.private_files import private_text
from nanobot_agentbridge.runtime import Conversation, run_readonly


VERSION = "0.2.0"


def channel_for(endpoint_key: str) -> str:
    return "telegram" if re.fullmatch(r"telegram:[0-9]+:[0-9]+",endpoint_key) else "workspace"


async def verify_subject(session, request) -> tuple[dict,str]:
    instance = request["instance"]
    coordinator = bool(request.get("coordinatorProgram"))
    context = {"version":"1","agentHost":"nanobot","hostInstanceId":instance,"hostVersion":"0.3.2" if coordinator else VERSION}
    names = ("mcpApps","privateResultMeta","interactionPollResume","taskTimeline","proactiveDelivery","artifactDelivery",
             "restartRecovery","coordinatorLease","batchTaskTimeline","runtimeSignals","boundedTransportRecovery")
    capabilities = dict.fromkeys(names,False)
    levels = ["L1"]
    if request.get("trustedCardOrigins"):
        levels.append("L2")
        capabilities.update(privateResultMeta=True,interactionPollResume=True)
    if coordinator:
        levels.append("L3")
        capabilities.update(dict.fromkeys(names[3:],True))
    profile = {"schema":"agentbridge.host.v1","hostInstanceId":instance,"levels":levels,
               "implementation":{"name":"nanobot","version":context["hostVersion"]},
               "endpointTypes":["web_private","telegram_private"],"capabilities":capabilities}
    meta = {"io.agentbridge/host-context":context, "io.agentbridge/telegram-pairing-version":2}
    registration = await session.call_tool("agentbridge_host_register",{},meta={**meta,"io.agentbridge/host-profile":profile})
    if registration.isError:
        raise PermissionError("Central host registration failed")
    profile_meta = dict(meta)
    tg_match = re.fullmatch(r"telegram:([0-9]+):([0-9]+)", str(request.get("endpointKey") or ""))
    if tg_match:
        profile_meta["io.agentbridge/telegram-identity"] = {
            "botId": tg_match.group(1),
            "senderId": tg_match.group(2),
        }
    result = await session.call_tool("agentbridge_host_identity_profile",{"agent_host":"nanobot"},meta=profile_meta)
    payload = result.structuredContent or {}
    if result.isError or payload.get("identity",{}).get("userSubject") != request["subject"]:
        raise PermissionError("Central token subject does not match the provisioned binding")
    accepted = payload.get("host",{}).get("acceptedLevel")
    request["controlledPrepares"] = payload.get("controlledPrepares",{}) if accepted=="L3" else {}
    access = payload.get("agentToolAccess")
    allowed = access.get("allowedToolNames") if isinstance(access, dict) else None
    if isinstance(allowed, list) and all(isinstance(item, str) for item in allowed):
        request["allowedToolNames"] = allowed
    if tg_match:
        request["telegramIdentityVerified"] = payload.get("telegramIdentityVerified") is True
    if accepted not in levels:
        raise PermissionError("Unexpected central host compatibility level")
    return meta,accepted


@asynccontextmanager
async def authenticated_session(request):
    token = private_text(Path(request["tokenFile"])).strip()
    tls = ssl.create_default_context(cafile=request.get("caFile"))
    async with httpx.AsyncClient(headers={"Authorization":"Bearer "+token},trust_env=False,
                                 verify=tls,follow_redirects=False,timeout=30,
                                 limits=httpx.Limits(keepalive_expiry=3.5)) as http:
        async with streamable_http_client(request["mcpUrl"],http_client=http) as (read,write,_):
            # The Telegram notification claim can long-poll for 15 seconds. Business workers
            # have their own longer deadlines for interactive operations.
            deadline = timedelta(seconds=30) if request.get("pooledSession") else None
            async with ClientSession(read, write, read_timeout_seconds=deadline) as session:
                await session.initialize()
                meta,level = await verify_subject(session,request)
                yield session,meta,level


_ORIGINAL_AUTHENTICATED_SESSION = authenticated_session


async def run(request: dict) -> None:
    source = json.loads(private_text(Path(request["modelConfigFile"])))
    directory = Path(request["userDir"])
    config_path = directory/"config.json"
    set_config_path(config_path)
    config = Config.model_validate(source)
    config.bind_source_path(config_path)
    config.agents.defaults.workspace = str(directory/"workspace")
    config.tools.restrict_to_workspace = True
    binding = Conversation(request["subject"],request["endpoint"],request["conversation"])
    channel = channel_for(request["endpointKey"])

    async def emit(delta: str) -> None:
        print(json.dumps({"kind":"delta","text":delta},ensure_ascii=False),flush=True)
    async with authenticated_session(request) as (session,meta,level):
        if channel=="telegram":
            if "telegramIdentityVerified" in request:
                verified = request["telegramIdentityVerified"] is True
            else:
                from nanobot_agentbridge.telegram_identity import verify_telegram_binding
                verified = await verify_telegram_binding(session,request["instance"],binding.subject,meta,request["endpointKey"])
            if not verified:
                raise PermissionError("Telegram identity has no active central binding")
        async def deliver(target, card, *, task_meta=None):
            if target!=binding:
                raise PermissionError("Card binding unavailable")
            return await wait_for_credential(session,request,task_meta or meta,card)
        can_present = level in {"L2","L3"} and channel in {"workspace","telegram"}
        from . import cross_endpoint_reference
        selection = cross_endpoint_reference.parse(request["message"], channel)
        opposite = "workspace" if channel == "telegram" else "telegram"
        wrong_source = selection is None and cross_endpoint_reference.parse(request["message"], opposite) is not None
        if selection is not None or wrong_source:
            if wrong_source:
                answer = "请在另一端引用这条消息。"
            elif request.get("media") or request.get("trial") is not None:
                answer = "请单独发送原消息引用。"
            elif level != "L3":
                answer = "当前宿主无法核对原消息。"
            else:
                from .skill_model import provider_completion
                async def summarize(system, prompt):
                    completion = await provider_completion(config, system, prompt)
                    return completion["text"]
                reference, question = selection
                answer = await cross_endpoint_reference.answer(session, meta, host="nanobot", endpoint=request["endpointKey"],
                    reference=reference, question=question, complete=summarize)
            print(json.dumps({"kind":"final","text":answer},ensure_ascii=False),flush=True)
            return
        if request.get("trial") is not None:
            if channel!="workspace":
                raise PermissionError("Workspace trial binding required")
            from .skills import run_workspace_trial
            answer = await run_workspace_trial(config,session,binding,request,meta,emit)
            print(json.dumps({"kind":"final","text":answer},ensure_ascii=False),flush=True)
            return
        coordinator_tool = None
        skill_args = {}
        if level=="L3" and request.get("coordinatorProgram") and (channel=="workspace" or request.get("workspaceEndpointId")):
            from .task_context import TaskContext
            context = TaskContext(session,request,meta)
            skill_args["business_context"] = context.get
            coordinator_tool = (request,context)
            if request.get("enableSkills") and (channel=="workspace" or request.get("workspaceEndpointId")):
                from .skills import SkillRun
                skill_args["skill_run"] = SkillRun(session,context.get)
        message = request["message"]
        media = request.get("media")
        if channel == "telegram" and media:
            from .media import telegram_document_context
            try:
                message, media = telegram_document_context(message, media)
            except Exception:
                # Parser errors can contain filenames or document bytes.
                print(json.dumps({"kind": "final", "text": "无法读取附件正文，请发送含文字的 PDF、UTF-8 文本或清晰截图。"}, ensure_ascii=False), flush=True)
                return
        answer = await run_readonly(config,session,binding,message,emit,
                    channel=channel,media=media,credential_login=can_present,coordinator_tool=coordinator_tool,deliver_private=deliver if can_present else None,
                    trusted_origins=frozenset(request.get("trustedCardOrigins",[])),allowed_tools=request.get("allowedToolNames"),**skill_args)
    print(json.dumps({"kind":"final","text":answer},ensure_ascii=False),flush=True)


if __name__=="__main__":
    try:
        asyncio.run(run(json.load(sys.stdin)))
    except Exception:
        # A provider exception can include tool results. It must not enter public logs.
        raise SystemExit(1) from None
