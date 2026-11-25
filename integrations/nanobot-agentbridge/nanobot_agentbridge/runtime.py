"""Run one AgentBridge conversation in its owner's isolated NanoBot worker."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from mcp import ClientSession, types
from nanobot import Nanobot
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import Config
from .coordinated_tools import CoordinatorTool
from .provider import CheckedProvider
from .draft_tools import AUTHORING, DraftTool
from .plans import PLAN,PlanTool
from .watch_tools import WATCH_MUTATIONS, WatchTool
from .business_reads import BUSINESS_READS, CONTEXT_FREE_READS
from .file_generation import DESCRIPTIONS, FILE_TOOLS, SEARCH, TaskFiles, parameters


READ_TOOLS = frozenset(
    {
        "agentbridge_server_profile",
        "agentbridge_skill_catalog",
        "oa_session_status",
        "oa_workflow_pending_list",
    }
)
PRIVATE_INTERACTION = "io.agentbridge/interaction"


@dataclass(frozen=True)
class Conversation:
    subject: str
    endpoint: str
    conversation: str

    @property
    def session_key(self) -> str:
        identity = [self.subject, self.endpoint, self.conversation]
        if not all(isinstance(item, str) and item.strip() for item in identity):
            raise ValueError("Incomplete conversation binding")
        encoded = json.dumps(identity, ensure_ascii=False).encode()
        return "sdk:agentbridge:" + hashlib.sha256(encoded).hexdigest()


PrivateDelivery = Callable[[Conversation, dict], Awaitable[str | None]]


def _valid_card(card: object, trusted_origins: frozenset[str]) -> bool:
    if (
        not isinstance(card, dict)
        or card.get("schemaVersion") != "agentbridge.interaction.v1"
    ):
        return False
    view = card.get("presentation")
    if (
        not isinstance(view, dict)
        or view.get("owner") != "agentbridge"
        or view.get("modelMustNotCollectValues") is not True
    ):
        return False
    url = view.get("url")
    if (
        not isinstance(url, str)
        or len(url) > 4096
        or any(c.isspace() or c == "\\" or ord(c) < 32 for c in url)
    ):
        return False
    try:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        valid_origin = (
            parsed.scheme == "https" and parsed.hostname and origin in trusted_origins
        )
    except ValueError:
        return False
    return bool(
        valid_origin
        and not (parsed.username or parsed.password or parsed.fragment)
        and isinstance(card.get("interactionId"), str)
        and 16 <= len(card["interactionId"]) <= 128
        and card.get("type")
        in {"credential", "business_input", "execution_authorization"}
        and card.get("state") in {"pending", "processing"}
    )


async def project_result(
    result: types.CallToolResult,
    binding: Conversation,
    deliver_private: PrivateDelivery | None,
    trusted_origins: frozenset[str],
    public_tool_names: set[str] | None = None,
) -> str:
    """Deliver the private envelope before constructing any model-facing text."""
    meta = result.meta or {}
    if PRIVATE_INTERACTION in meta:
        card = meta[PRIVATE_INTERACTION]
        if deliver_private is None:
            return ToolResult.error("此端点需要可信私有交互。",error_code="HOST_CAPABILITY_REQUIRED")
        if (
            result.isError
            or not _valid_card(card, trusted_origins)
        ):
            return ToolResult.error("可信交互无法交付。请在原任务中核对状态。")
        outcome = await deliver_private(binding, card)
        if outcome == "completed":
            return "凭证卡已由中央核验并续办。"
        if outcome == "closed":
            return ToolResult.error("凭证卡已关闭或过期。")
        if outcome is not None:
            return ToolResult.error("可信交互结果有待核对。请在原任务中查看。")
        # Neither textual fallbacks nor structured fields from a private delivery
        # belong in tool logs, model inputs, memory, or the public event stream.
        return "需要可信交互，请在本人私有端点查看卡片。"
    if (
        result.isError
        or "io.agentbridge/ui" in meta
        or (result.structuredContent or {}).get("interaction")
    ):
        return ToolResult.error("中央服务未返回可公开展示的结果。")
    if public_tool_names is not None:
        access = (result.structuredContent or {}).get("agentToolAccess", {})
        allowed = access.get("allowedToolNames") if isinstance(access, dict) else None
        if not isinstance(allowed, list) or not all(isinstance(name, str) for name in allowed):
            return ToolResult.error("无法核对此次会话的可用工具。")
        current = sorted(public_tool_names.intersection(allowed))
        return json.dumps({"agentToolAccess": {"allowedToolNames": current, "allowedToolCount": len(current)}})
    return "\n".join(
        item.text for item in result.content if isinstance(item, types.TextContent)
    )


SESSION_SYSTEMS = frozenset({"oa", "smartlight", "taihua", "yuque"})
SESSION_LOGINS = {f"{system}_session_login" for system in SESSION_SYSTEMS}


class CentralReadTool(Tool):
    _plugin_discoverable = False

    def __init__(self, definition, session, binding, deliver_private, trusted_origins, registry):
        self.definition = definition
        self.session = session
        self.binding = binding
        self.deliver_private = deliver_private
        self.trusted_origins = trusted_origins
        self.registry = registry

    @property
    def name(self) -> str:
        return "mcp_ab_" + self.definition.name

    @property
    def description(self) -> str:
        if self.definition.name == "agentbridge_server_profile":
            return "列出当前用户在此 NanoBot 会话可调用的工具。"
        if self.registry.task_files and self.definition.name in DESCRIPTIONS:
            return DESCRIPTIONS[self.definition.name]
        return self.definition.description or self.definition.name

    @property
    def parameters(self) -> dict:
        if self.registry.task_files:
            return parameters(self.definition.name, self.definition.inputSchema)
        return self.definition.inputSchema

    async def credential_prerequisite(self, system):
        status_name, login_name = f"{system}_session_status", f"{system}_session_login"
        if (system in self.registry.checked_sessions or self.deliver_private is None
                or "mcp_ab_" + status_name not in self.registry.tool_names
                or "mcp_ab_" + login_name not in self.registry.tool_names):
            return None
        status = await self.session.call_tool(status_name, {})
        observed = status.structuredContent or {}
        if status.isError or (observed.get("userSubject"), observed.get("systemId")) != (self.binding.subject, system):
            return ToolResult.error("无法确认本人在该系统的会话。")
        if observed.get("status") == "active":
            self.registry.checked_sessions.add(system)
            return None
        if observed.get("status") not in {"expired", "not_found", "login_required"}:
            return ToolResult.error("当前会话状态待核对，请稍后查看。")
        if system in self.registry.attempted_logins:
            return ToolResult.error("此次登录已请求，请查看原任务中的凭证卡。")
        self.registry.attempted_logins.add(system)
        # The original L2 OA route uses a standalone credential. L3 business reads
        # pin the login prerequisite to their existing task and coordinator lease.
        task_meta = None
        if system != "oa" and self.registry.business_context is not None:
            task_meta = await self.registry.business_context()
        options = {"meta": {**task_meta, "io.agentbridge/continue-after-login": True}} if task_meta else {}
        login = await self.session.call_tool(login_name, {}, **options)
        if PRIVATE_INTERACTION in (login.meta or {}):
            async def deliver(binding, card):
                if task_meta:
                    return await self.deliver_private(binding, card, task_meta=task_meta)
                return await self.deliver_private(binding, card)
            outcome = await project_result(login, self.binding, deliver, self.trusted_origins)
            if outcome != "凭证卡已由中央核验并续办。":
                return outcome
        elif login.isError or (login.structuredContent or {}).get("status") != "succeeded":
            return ToolResult.error("中央未确认本人的登录结果。")
        self.registry.checked_sessions.add(system)
        return None

    async def execute(self, **kwargs) -> str:
        tracker = self.registry.read_task
        receipt = None
        try:
            skill = self.registry.skill_run
            name = self.definition.name
            system = name.partition("_")[0]
            needs_session = name == "oa_workflow_pending_list" or (
                system in SESSION_SYSTEMS - {"oa"} and name in (BUSINESS_READS - CONTEXT_FREE_READS) | FILE_TOOLS
            )
            if needs_session and not (skill is not None and skill.selection):
                outcome = await self.credential_prerequisite(system)
                if outcome is not None:
                    return outcome
            if self.registry.task_files and self.definition.name in FILE_TOOLS | {SEARCH}:
                receipt, public = await self.registry.task_files.execute(
                    self.definition.name, kwargs, session=self.session, business_context=self.registry.business_context,
                    skill=skill, binding=self.binding)
                return json.dumps(public, ensure_ascii=False)
            if skill is not None and (skill.selection or self.definition.name == "agentbridge_skill_get"):
                result = await skill.call(self.definition.name, kwargs)
            elif self.registry.business_context and self.definition.name in BUSINESS_READS - CONTEXT_FREE_READS:
                meta = await self.registry.business_context()
                result = await self.session.call_tool(self.definition.name, arguments=kwargs, meta=meta)
            else:
                result=await self.session.call_tool(self.definition.name,arguments=kwargs)
            receipt = result
            if self.definition.name == "database_execute" and kwargs.get("capability") == "database.report.download":
                file_receipt = (result.structuredContent or {}).get("result") or {}
                if result.isError or not file_receipt.get("artifact_id"):
                    receipt = None
                    return ToolResult.error("中央未确认原任务的文件交付。")
                return json.dumps({"status": "succeeded", "file": {
                    k: file_receipt[k] for k in ("filename", "byte_size", "download_expires_at") if k in file_receipt
                }}, ensure_ascii=False)
            accessible = None
            if self.definition.name == "agentbridge_server_profile":
                accessible = {name.removeprefix("mcp_ab_") for name in self.registry.tool_names
                              if name.startswith("mcp_ab_") and (self.registry.skill_run is None or self.registry.skill_run.permits(name.removeprefix("mcp_ab_")))}
            return await project_result(result, self.binding, self.deliver_private,
                                        self.trusted_origins, public_tool_names=accessible)
        except Exception:
            # The SDK records tool errors in model history. Transport details
            # and presenter exceptions can include private data.
            receipt = None
            return ToolResult.error("无法确认此次查询的结果。请在原任务中核对。")
        finally:
            if tracker is not None:
                tracker.observe_read(self.definition.name, receipt)


class CentralTools(ToolRegistry):
    """NanoBot also attempts to register built-in file, shell and agent tools."""

    def __init__(self, skill_run=None, business_context=None, read_task=None, task_files=None):
        super().__init__()
        self.skill_run=skill_run
        self.checked_sessions = set()
        self.attempted_logins = set()
        self.business_context = business_context
        self.read_task = read_task
        self.task_files = task_files

    def get_definitions(self):
        definitions=super().get_definitions()
        if self.skill_run is None:
            return definitions
        return [item for item in definitions if self.skill_run.permits(item["function"]["name"].removeprefix("mcp_ab_"))]

    def register(self, tool: Tool) -> None:
        if isinstance(tool, (CoordinatorTool,PlanTool,WatchTool,DraftTool)):
            super().register(tool)
        elif isinstance(tool, CentralReadTool) and (
            tool.definition.name in READ_TOOLS | SESSION_LOGINS | {"agentbridge_skill_get"}
            or (self.business_context and (tool.definition.name in BUSINESS_READS or self.task_files and tool.definition.name in FILE_TOOLS)
                and tool.definition.annotations and tool.definition.annotations.readOnlyHint is True)
        ):
            super().register(tool)


async def run_readonly(
    config: Config,
    session: ClientSession,
    binding: Conversation,
    question: str,
    emit: Callable[[str], Awaitable[None]],
    *,
    deliver_private: PrivateDelivery | None = None,
    trusted_origins: frozenset[str] = frozenset(),
    channel: str = "workspace",
    credential_login: bool = False,
    media: list[str] | None = None,
    coordinator_tool: tuple | None = None,
    skill_run=None,
    business_context=None,
    observe: Callable[[str], Awaitable[None]] | None = None,
    allowed_tools: list[str] | None = None,
) -> str:
    """The worker owns both its NanoBot state directory and the user's MCP session."""
    session_key = binding.session_key
    if question.lstrip().startswith("/"):
        raise ValueError("NanoBot management commands are unavailable in Workspace")
    if isinstance(allowed_tools, list) and all(isinstance(item, str) for item in allowed_tools):
        allowed = allowed_tools
    else:
        profile = await session.call_tool("agentbridge_server_profile", {})
        access = (profile.structuredContent or {}).get("agentToolAccess", {})
        allowed = access.get("allowedToolNames") if isinstance(access, dict) else None
        if (
            profile.isError
            or not isinstance(allowed, list)
            or not all(isinstance(item, str) for item in allowed)
        ):
            raise ValueError("Central MCP did not confirm this user's tool access")
    candidates = READ_TOOLS | SESSION_LOGINS if credential_login and deliver_private else READ_TOOLS
    if skill_run is not None:
        candidates=candidates | {"agentbridge_skill_get"}
    if business_context is not None:
        candidates = candidates | BUSINESS_READS
    task_files = TaskFiles() if channel == "telegram" and coordinator_tool and business_context else None
    if task_files is not None:
        candidates = candidates | FILE_TOOLS
    projected = candidates.intersection(allowed) | {"agentbridge_server_profile"}
    read_task = coordinator_tool[1] if coordinator_tool else None
    tools = CentralTools(skill_run, business_context, read_task, task_files)
    governed = {}
    if coordinator_tool is not None:
        host_request, task_context = coordinator_tool
        governed = host_request["controlledPrepares"]
    cursor = None
    seen = set()
    while True:
        page = await session.list_tools(cursor=cursor)
        for definition in page.tools:
            if (coordinator_tool and skill_run is not None and channel == "telegram"
                    and definition.name == AUTHORING and AUTHORING in allowed):
                tools.register(DraftTool(config, host_request, task_context, skill_run, registry=tools))
            if coordinator_tool and definition.name in WATCH_MUTATIONS and definition.name in allowed:
                tools.register(WatchTool(definition, host_request, task_context, skill_run))
            if coordinator_tool and definition.name==PLAN and PLAN in allowed:
                tools.register(PlanTool(host_request,task_context,definition,skill_run,projected))
            if definition.name in governed and definition.name in allowed:
                tools.register(CoordinatorTool(host_request,task_context,definition,skill_run))
            if definition.name in projected:
                tools.register(
                    CentralReadTool(
                        definition, session, binding, deliver_private, trusted_origins, tools
                    )
                )
        cursor = page.nextCursor
        if cursor is None:
            break
        if cursor in seen:
            raise ValueError("Invalid central tool catalog cursor")
        seen.add(cursor)
    provider = CheckedProvider(config)
    loop = AgentLoop.from_config(config, tool_registry=tools,provider=provider)
    async with Nanobot(loop, config=config) as bot:
        run = await bot.run_streamed(
            question,
            session_key=session_key,
            channel=channel,
            chat_id=binding.endpoint,
            sender_id=binding.subject,
            media=media,
        )
        try:
            async for event in run.stream_events():
                if observe is not None:
                    await observe(event.type)
                if event.type == "text.delta" and event.delta and not provider.failed:
                    await emit(event.delta)
            answer = provider.result(await run.wait())
            if read_task is not None and (skill_run is None or not skill_run.selection
                                          or (not skill_run.pending and skill_run.effects == {"read"})):
                await read_task.finish_confirmed_read()
            return answer
        finally:
            if not run.done:
                await run.cancel()
