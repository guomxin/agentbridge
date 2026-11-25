"""Authenticated, per-binding MCP. Only the public projection can cross the SDK pipe."""

from contextlib import asynccontextmanager
from datetime import timedelta
import json
import ssl

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import VERSION
from .coordinator import PROTECTED


FLAGS = ("mcpApps", "privateResultMeta", "interactionPollResume", "taskTimeline", "proactiveDelivery",
         "artifactDelivery", "restartRecovery", "coordinatorLease", "batchTaskTimeline", "runtimeSignals",
         "boundedTransportRecovery")
L1_READS = {"oa_session_status", "oa_workflow_pending_list", "agentbridge_server_profile"}
L2_BUSINESS_READS = {"oa_workflow_pending_list", "taihua_work_log_my_list", "yuque_public_books_list", "smartlight_system_overview"}
SYSTEMS = {"oa", "taihua", "smartlight", "yuque"}
LOGINS = {system + "_session_login" for system in SYSTEMS}
PLAN = "agentbridge_task_plan_prepare"
PLAN_CATALOG = "agentbridge_task_plan_catalog"
PRIVATE_KEYS = {"_meta", "interaction", "presentation", "authorization_token", "password", "cookie",
                "accesstoken", "access_token", "refreshtoken", "refresh_token", "downloadurl", "download_url"}
PRIVATE_STATUS = "当前宿主需要可信私有交互。请在已获准的原端点查看。"


def is_private(value):
    if isinstance(value, dict):
        return any((v is not None and (k.lower() in PRIVATE_KEYS or k.startswith("io.agentbridge/"))) or is_private(v)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(is_private(v) for v in value)
    return False


class CentralTools:
    def __init__(self, session, meta, definitions, level, subject, governed=None, contextual_reads=()):
        self.session = session
        self.meta = meta
        self.definitions = definitions
        self.level = level
        self.subject = subject
        self.handle_card = None
        self.handle_prepare = None
        self.plan = None
        self.repair = None
        self.skill = None
        self.private_tools = {}
        self.files = None
        self.file_tools = set()
        self.authoring = False
        self.origins = ()
        self.governed = governed or {}
        self.governed_result = None
        self.governed_fingerprint = None
        self.task_context = None
        self.contextual_reads = contextual_reads
        self.attempted_logins = set()
        self.checked_sessions = set()
        self.binding_check = None

    async def require_binding(self):
        if self.binding_check and not await self.binding_check():
            raise PermissionError("Original Pi Telegram pairing is no longer active")

    def visible_tools(self):
        if self.repair and self.repair.source:
            return self.repair.visible(self.definitions)
        if self.authoring:
            return ["agentbridge_skill_authoring"]
        if self.skill:
            return sorted(name for name in self.definitions if self.skill.permits(name))
        return sorted(self.definitions)

    async def _login(self, name, *, prerequisite=False):
        if name in self.attempted_logins:
            return "unknown"
        self.attempted_logins.add(name)
        task_meta = await self.task_context.get() if prerequisite and self.task_context else None
        meta = {**task_meta, "io.agentbridge/continue-after-login": True} if task_meta else self.meta
        result = await self.session.call_tool(name, {}, meta=meta)
        card = (result.meta or {}).get("io.agentbridge/interaction")
        if card and self.handle_card:
            return await self.handle_card(card, task_meta=task_meta)
        data = result.structuredContent or {}
        return "completed" if not result.isError and data.get("status") == "succeeded" else "unknown"

    async def call(self, name, arguments):
        await self.require_binding()
        if name not in self.visible_tools() or not isinstance(arguments, dict):
            return "此用户没有该工具的调用权限。"
        if self.repair and self.repair.source:
            return await self.repair.handle(name, arguments)
        if self.repair:
            control = await self.repair.preflight(name)
            if control:
                return control
        if name in self.private_tools:
            if name == "agentbridge_skill_authoring":
                self.authoring = True
            return await self.private_tools[name].execute(**arguments)
        if name == "agentbridge_server_profile":
            return json.dumps({"agentToolAccess": {"allowedToolNames": self.visible_tools()}}, ensure_ascii=False)
        if name in self.governed:
            if not self.handle_prepare or not set(arguments) <= set(self.definitions[name]["parameters"]["properties"]):
                return "业务参数不属于本次获准的准备工具。"
            pin = None
            if self.skill and self.skill.selection:
                pin = self.skill.private_prepare_scope(self.governed[name]["prepareCapability"])
                if not pin:
                    return "本次技能未开放该办理能力。"
            outcome = await self.handle_prepare(name, arguments, pin)
            return {"verified": "中央已核验原任务的业务操作成功。", "closed": "可信交互已关闭。",
                    "conflict": "此任务已登记另一项办理，本项未执行。",
                    "waiting": "请在原任务中打开可信办理卡。"}.get(outcome, "本次办理结果有待核对，请在原任务中查看。")
        if name == PLAN:
            return await self.plan.execute(**arguments) if self.plan else "当前任务未开放中央计划。"
        if name in LOGINS:
            if arguments or not self.handle_card:
                return PRIVATE_STATUS
            state = await self._login(name)
            return {"completed": "凭证卡已由中央核验并续办。", "closed": "凭证卡已关闭或过期。"}.get(state, "凭证状态待核对。")
        system = name.split("_", 1)[0]
        login = system + "_session_login"
        if (name in L2_BUSINESS_READS or name in self.contextual_reads | self.file_tools and system in SYSTEMS - {"oa"}) and (
                self.handle_card and login in self.definitions and system not in self.checked_sessions and not (self.skill and self.skill.selection)):
            # Check the prerequisite before the business read so a successful login
            # performs the original exact query once. Indeterminate checks never prompt for credentials.
            response = await self.session.call_tool(system + "_session_status", {}, meta=self.meta)
            status = response.structuredContent or {}
            if response.isError or (status.get("userSubject"), status.get("systemId")) != (self.subject, system):
                return "本人会话状态待核对。"
            if status.get("status") in {"expired", "not_found", "login_required"}:
                state = await self._login(login, prerequisite=True)
                if state != "completed":
                    return "凭证卡已关闭或过期。" if state == "closed" else "凭证状态待核对。"
            elif status.get("status") != "active":
                return "本人会话状态待核对。"
            self.checked_sessions.add(system)
        if self.files and name in self.file_tools:
            result, public = await self.files.execute(name, arguments, session=self.session, business_context=self.task_context.get,
                                                       skill=self.skill, binding=self.file_binding)
            self.task_context.observe_read(name, result)
            return json.dumps(public, ensure_ascii=False)
        meta = self.meta
        if name in self.contextual_reads:
            if not self.task_context:
                return "本次查询缺少原任务身份。"
            meta = await self.task_context.get()
        if self.skill and (self.skill.selection or name == "agentbridge_skill_get"):
            result = await self.skill.call(name, arguments)
        else:
            result = await self.session.call_tool(name, arguments, meta=meta)
        if name in self.contextual_reads:
            self.task_context.observe_read(name, result)
            if self.repair and not result.isError:
                original = meta["io.agentbridge/task"]["taskId"]
                data = result.structuredContent or {}
                observed = data.get("taskObservation") or {}
                if observed.get("taskId") == original and data.get("operationId") in (observed.get("operationIds") or []):
                    if data.get("status") == "succeeded":
                        self.repair.read_tools.add(name)
                    control = self.repair.capture({**data, "taskId":original}, original, name)
                    if control:
                        return control
        private = result.meta or {}
        if "io.agentbridge/interaction" in private or "io.agentbridge/ui" in private:
            return PRIVATE_STATUS
        if result.isError:
            return "中央服务未确认本次读取结果。"
        data = result.structuredContent
        if name == "database_execute" and arguments.get("capability") == "database.report.download":
            receipt = (data or {}).get("result") or {}
            if not receipt.get("artifact_id"):
                return "中央未确认原任务的文件交付。"
            return json.dumps({"status":"succeeded", "file":{k:receipt[k] for k in ("filename","byte_size","download_expires_at") if k in receipt}}, ensure_ascii=False)
        if not isinstance(data, dict) or is_private(data):
            return PRIVATE_STATUS
        public = json.dumps(data, ensure_ascii=False)
        if any(origin in public for origin in self.origins):
            return PRIVATE_STATUS
        if len(public) > 100_000:
            return "本次查询结果过大，请缩小范围。"
        return public


def declared_profile(spec):
    profile = {"schema": "agentbridge.host.v1", "hostInstanceId": spec["instanceId"], "levels": ["L1"],
               "implementation": {"name": "pi", "version": VERSION}, "endpointTypes": ["web_private"],
               "capabilities": dict.fromkeys(FLAGS, False)}
    if spec.get("telegramPilot"):
        profile["endpointTypes"].append("telegram_private")
    maximum = spec.get("requiredLevel", "L1")
    if maximum not in {"L1", "L2", "L3"}:
        raise ValueError("Invalid configured Pi host level")
    if maximum in {"L2", "L3"} and spec.get("trustedCardOrigins"):
        profile["levels"].append("L2")
        profile["capabilities"].update(privateResultMeta=True, interactionPollResume=True)
        if maximum == "L3" and spec.get("coordinatorProgram"):
            profile["levels"].append("L3")
            profile["capabilities"].update({key: True for key in FLAGS if key != "mcpApps"})
    return profile


@asynccontextmanager
async def connect(spec, record, token, *, catalog=True):
    tls = ssl.create_default_context(cafile=spec.get("caFile"))
    def client_factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, verify=tls,
                                 follow_redirects=False, trust_env=False)
    instance = spec["instanceId"]
    context = {"version": "1", "agentHost": "pi", "hostInstanceId": instance, "hostVersion": VERSION}
    meta = {"io.agentbridge/host-context": context}
    profile = declared_profile(spec)
    async with streamablehttp_client(spec["mcpUrl"], headers={"Authorization": "Bearer " + token},
                                     httpx_client_factory=client_factory) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=45)) as session:
            await session.initialize()
            registered = await session.call_tool("agentbridge_host_register", {}, meta={**meta, "io.agentbridge/host-profile": profile})
            if registered.isError:
                raise PermissionError("Pi registration failed")
            identity = await session.call_tool("agentbridge_host_identity_profile", {"agent_host": "pi"}, meta=meta)
            payload = identity.structuredContent or {}
            host = payload.get("host") or {}
            if (identity.isError or (payload.get("identity") or {}).get("userSubject") != record["user_subject"]
                    or host.get("hostInstanceId") != instance or host.get("hostVersion") != VERSION
                    or host.get("acceptedLevel") not in profile["levels"]
                    or host.get("acceptedLevel") != spec.get("requiredLevel", "L1")):
                raise PermissionError("Unexpected identity or Pi compatibility level")
            allowed = payload.get("agentToolAccess", {}).get("allowedToolNames")
            if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
                raise PermissionError("Missing current subject permissions")
            level = host["acceptedLevel"]
            if not catalog:
                yield CentralTools(session, meta, {}, level, record["user_subject"])
                return
            candidates = L1_READS if level == "L1" else L1_READS | L2_BUSINESS_READS | LOGINS | {s + "_session_status" for s in SYSTEMS}
            contextual_reads, governed, private_names, file_tools = set(), {}, set(), set()
            if level == "L3":
                from nanobot_agentbridge.business_reads import BUSINESS_READS, CONTEXT_FREE_READS
                from nanobot_agentbridge.watch_actions import WATCH_MUTATIONS
                contextual_reads = BUSINESS_READS - CONTEXT_FREE_READS
                contracts = payload.get("controlledPrepares") or {}
                governed = {k: v for k, v in contracts.items() if k in allowed and isinstance(v, dict)
                            and v.get("prepareCapability") and v.get("commitCapability")}
                candidates = candidates.union(BUSINESS_READS, governed, {PLAN, PLAN_CATALOG, "agentbridge_skill_catalog", "agentbridge_skill_get"})
                private_names = set(WATCH_MUTATIONS)
                if record["endpoint_key"].startswith("telegram:"):
                    from nanobot_agentbridge.file_generation import FILE_TOOLS, SEARCH
                    private_names.add("agentbridge_skill_authoring")
                    file_tools = FILE_TOOLS | {SEARCH}
                candidates = candidates.union(private_names, file_tools)
            names = candidates.intersection(allowed) | {"agentbridge_server_profile"}
            definitions, cursor, seen = {}, None, set()
            while True:
                page = await session.list_tools(cursor=cursor)
                for tool in page.tools:
                    if tool.name in names and (tool.name in LOGINS | private_names | file_tools or tool.name in governed or tool.name in {PLAN, "agentbridge_skill_get"} or tool.annotations and tool.annotations.readOnlyHint is True):
                        schema = tool.inputSchema
                        if tool.name in LOGINS:
                            schema = {"type":"object", "properties":{}, "additionalProperties":False}
                        elif tool.name in governed or tool.name == PLAN:
                            schema = {**schema, "properties": {k:v for k,v in schema.get("properties", {}).items() if k not in PROTECTED},
                                      "required": [k for k in schema.get("required", []) if k not in PROTECTED], "additionalProperties": False}
                        definitions[tool.name] = {"name": tool.name, "description": tool.description or tool.name,
                                                  "parameters": schema}
                cursor = page.nextCursor
                if cursor is None:
                    break
                if cursor in seen:
                    raise ValueError("Repeated MCP catalog cursor")
                seen.add(cursor)
            tools = CentralTools(session, meta, definitions, level, record["user_subject"], governed, contextual_reads)
            tools.origins = spec.get("trustedCardOrigins", [])
            tools.file_tools = file_tools
            yield tools
