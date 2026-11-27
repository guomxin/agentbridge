"""Use the common delivery protocol with one exact Pi identity, endpoint and pairing generation."""
from contextlib import asynccontextmanager
import json

from starlette.exceptions import HTTPException

from .server import binding_key


class Delivery:
    def __init__(self, host, record, action):
        self.host = host
        self.record = record
        self.lane = {
            "telegram-notifications": "notifications",
            "telegram-workspace-media": "workspace-media",
            "telegram-workspace-result-view": "workspace-result-view",
        }.get(action, "control")
        self.instance = host.instance
        self.coordinator = host.coordinator
        self.commands = host.commands
        self.card_origins = host.cards.origins
        self.runs = self
        self.db = host.ledger.db
        self.binding = json.dumps(binding_key(record), ensure_ascii=False)

    def lookup(self, key, *, subject, endpoint, conversation):
        if (subject, endpoint, conversation) != tuple(self.record[k] for k in ("user_subject", "endpoint_id", "session_key")):
            return None
        row = self.host.ledger.lookup(self.binding, key)
        return {**dict(row), "run_id": row["id"]} if row else None

    def matches_message(self, run, text):
        return self.host.ledger.db.execute("SELECT 1 FROM runs WHERE id=? AND binding=? AND prompt=?",
                                          (run, self.binding, text)).fetchone() is not None

    def worker_payload(self, record, run):
        if record is not self.record:
            raise HTTPException(403)
        return {**self.host.worker_payload(record, run), "hostType": "pi"}

    @asynccontextmanager
    async def private_session(self, request):
        if request["endpointKey"] != self.record["endpoint_key"]:
            raise HTTPException(403)
        async with self.host.control_sessions.borrow(self.record, lane=self.lane) as central:
            yield central.session, central.meta, central.level

    @asynccontextmanager
    async def notification_transport(self):
        async with self.host.control_sessions.checkout(self.record, lane=self.lane) as check_identity:
            @asynccontextmanager
            async def paired_session(request):
                if request["endpointKey"] != self.record["endpoint_key"]:
                    raise HTTPException(403)
                central = await check_identity()
                if central.level != "L3":
                    raise HTTPException(403)
                current = await self.host.telegram.current(central.session, central.meta)
                if not current or current != self.record:
                    raise HTTPException(403)
                yield central.session, central.meta, central.level

            yield paired_session

    @asynccontextmanager
    async def paired_notification_session(self, request):
        async with self.notification_transport() as paired_session, paired_session(request) as opened:
            yield opened

    async def standalone_card(self, run, request_key):
        result = await self.host.action("card", {"binding": self.record, "runId": run, "requestKey": request_key})
        return {"interaction": result["interaction"]} if result.get("available") else {"state": "unavailable"}


async def dispatch(host, record, action, body):
    from aiohttp import web
    from nanobot_agentbridge import artifacts, notifications, telegram_tasks, telegram_timeline

    delivery = Delivery(host, record, action)
    try:
        if action == "artifact-reissue":
            key, run = body.get("requestKey"), body.get("runId")
            entry = host.ledger.lookup(delivery.binding, key) if isinstance(key, str) else None
            if not entry or entry["id"] != run:
                raise HTTPException(403)
            return await artifacts.reissue(delivery, record, body, run)
        if not record["endpoint_key"].startswith("telegram:"):
            raise HTTPException(403)
        if action == "telegram-workspace-media":
            if set(body) - {"instance", "binding", "action", "delivery_id", "attempt", "receipt"}:
                raise HTTPException(400)
            async with delivery.paired_notification_session(delivery.worker_payload(record, None)) as (session, meta, _):
                reply = await session.call_tool("agentbridge_host_telegram_workspace_media", {
                    **{k: body[k] for k in ("action", "delivery_id", "attempt", "receipt") if k in body},
                    "endpoint_key": record["endpoint_key"], "pairing_ref": record["credential_ref"].removeprefix("tg-"),
                }, meta=meta)
                if reply.isError or not isinstance(reply.structuredContent, dict):
                    raise HTTPException(403)
                return reply.structuredContent
        if action == "telegram-task":
            return await telegram_tasks.navigate(delivery, record, body)
        if action == "telegram-timeline":
            return await telegram_timeline.publish_timeline(delivery, record, body)
        async with delivery.notification_transport() as paired_session:
            return await notifications.notifications(delivery, record, action, body, paired_session=paired_session)
    except web.HTTPException as exc:
        raise HTTPException(exc.status) from None
