"""Project only central watch management onto a verified personal NanoBot turn."""

import hashlib
import json
import re


from .telegram_identity import identifier, verify_telegram_binding

CREATE = "agentbridge_business_watch_create"
UPDATE = "agentbridge_business_watch_update"
WATCH_MUTATIONS = {CREATE, UPDATE}


class WatchActions:
    _plugin_discoverable = False

    def __init__(self, definition, request, context, skill_run, *, error, host_type):
        self.error = error
        self.host_type = host_type
        self.definition = definition
        self.request = request
        self.context = context
        self.skill_run = skill_run

    @property
    def name(self):
        return "mcp_ab_" + self.definition.name

    @property
    def description(self):
        return self.definition.description + (
            " 仅在本人明确要求时设置关注。详情读取可能标为已读，须先取得用户对该副作用的明确同意。"
            "个人 Telegram 提醒须单独明确选择，默认只记录在工作台。不得从普通查询推定同意。"
        )

    @property
    def parameters(self):
        schema = dict(self.definition.inputSchema)
        fields = {k: v for k, v in schema["properties"].items() if k not in {"idempotency_key", "endpoint_ids"}}
        fields["notify_telegram"] = {"type": "boolean", "description": "明确要求向本人绑定的当前宿主私聊通知时为 true。更改只影响这个私聊。"}
        return {**schema, "properties": fields, "additionalProperties": False}

    async def personal_endpoint(self):
        request, session, meta = self.request, self.context.session, self.context.host_meta
        telegram = re.fullmatch(r"telegram:([1-9][0-9]*):([1-9][0-9]*)", request["endpointKey"])
        if telegram:
            if await verify_telegram_binding(session, request["instance"], request["subject"], meta, request["endpointKey"], host_type=self.host_type):
                return request["endpoint"]
            return None
        bot = request.get("telegramBotId")
        if not bot or not request["endpointKey"].startswith("workspace:"):
            return None
        peers = await session.call_tool("agentbridge_host_telegram_mirror_peers", {"bot_id": bot}, meta=meta)
        senders = (peers.structuredContent or {}).get("senderIds")
        if peers.isError or not isinstance(senders, list) or len(senders) != 1:
            return None
        bound = await session.call_tool("agentbridge_host_telegram_endpoint", {"bot_id": bot, "sender_id": senders[0]}, meta=meta)
        record = bound.structuredContent or {}
        if (not bound.isError and record.get("workspaceEndpointId") == request["endpoint"]
                and record.get("workspaceEndpointKey") == request["endpointKey"]
                and record.get("endpointKey") == f"telegram:{bot}:{senders[0]}" and identifier(record.get("endpointId"))):
            return record["endpointId"]
        return None

    async def execute(self, **arguments):
        name = self.definition.name
        if (self.skill_run and (self.skill_run.selection or self.skill_run.pending)
                or set(arguments) - set(self.parameters["properties"])):
            return self.error("当前模式未开放此关注设置。")
        notify = arguments.pop("notify_telegram", None)
        if notify is not None and type(notify) is not bool:
            return self.error("请明确选择本人私聊提醒。")
        if name == CREATE and arguments.get("allow_mark_read") is not True:
            return self.error("创建前请明确同意定时读取详情可能将事项标为已读。")
        session, meta = self.context.session, self.context.host_meta
        try:
            if notify is not None:
                if name == UPDATE and arguments.get("action") != "configure":
                    return self.error("提醒渠道只在调整关注设置时变更。")
                endpoint = await self.personal_endpoint() if notify or name == UPDATE else None
                if (notify or name == UPDATE) and not endpoint:
                    return self.error("无法核验本人 Telegram 通知端点。")
                selected = []
                if name == UPDATE:
                    prior = await session.call_tool("agentbridge_business_watch_get", {"watch_id": arguments["watch_id"]}, meta=meta)
                    watch = (prior.structuredContent or {}).get("watch") or {}
                    if (prior.isError or watch.get("watch_id") != arguments["watch_id"]
                            or watch.get("version") != arguments["expected_version"] or not isinstance(watch.get("endpoint_ids"), list)):
                        return self.error("关注记录已变化，请先查看最新设置。")
                    selected = [value for value in watch["endpoint_ids"] if value != endpoint]
                arguments["endpoint_ids"] = [*selected, endpoint] if notify else selected
            if name == CREATE:
                identity = json.dumps([self.request["runId"], arguments], sort_keys=True).encode()
                arguments["idempotency_key"] = self.host_type + ":watch:" + hashlib.sha256(identity).hexdigest()
            result = await session.call_tool(name, arguments, meta=meta)
            watch = (result.structuredContent or {}).get("watch") or {}
            if result.isError or not identifier(watch.get("watch_id")) or type(watch.get("version")) is not int:
                return self.error("中央未确认关注设置，请重新查看本人关注记录。")
            return json.dumps({key: watch[key] for key in ("watch_id", "title", "version", "state", "interval_seconds",
                              "expires_at", "last_verified_at", "event_types") if key in watch}, ensure_ascii=False)
        except Exception:
            return self.error("关注设置结果待核对，请先查看本人关注记录。")
