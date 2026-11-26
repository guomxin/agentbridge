"""Use the common delivery protocol with one exact Pi identity, endpoint and pairing generation."""
from contextlib import asynccontextmanager
import json

from aiohttp import web
from starlette.exceptions import HTTPException

from .central import connect
from .server import bearer, binding_key


class Delivery:
    def __init__(self, host, record):
        self.host = host
        self.record = record
        self.instance = host.instance
        self.coordinator = host.coordinator
        self.commands = host.commands
        self.card_origins = host.cards.origins
        self.runs = self
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
        async with connect(self.host.spec, self.record, bearer(self.record["tokenFile"])) as central:
            yield central.session, central.meta, central.level

    async def standalone_card(self, run, request_key):
        result = await self.host.action("card", {"binding": self.record, "runId": run, "requestKey": request_key})
        return {"interaction": result["interaction"]} if result.get("available") else {"state": "unavailable"}


async def dispatch(host, record, action, body):
    from nanobot_agentbridge import artifacts, notifications, telegram_tasks, telegram_timeline

    delivery = Delivery(host, record)
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
            async with connect(host.spec, record, bearer(record["tokenFile"])) as central:
                if central.level != "L3":
                    raise HTTPException(403)
                reply = await central.session.call_tool("agentbridge_host_telegram_workspace_media", {
                    **{k: body[k] for k in ("action", "delivery_id", "attempt", "receipt") if k in body},
                    "endpoint_key": record["endpoint_key"], "pairing_ref": record["credential_ref"].removeprefix("tg-"),
                }, meta=central.meta)
                if reply.isError or not isinstance(reply.structuredContent, dict):
                    raise HTTPException(403)
                return reply.structuredContent
        if action == "telegram-task":
            return await telegram_tasks.navigate(delivery, record, body)
        if action == "telegram-timeline":
            return await telegram_timeline.publish_timeline(delivery, record, body)
        return await notifications.notifications(delivery, record, action, body)
    except web.HTTPException as exc:
        raise HTTPException(exc.status) from None
