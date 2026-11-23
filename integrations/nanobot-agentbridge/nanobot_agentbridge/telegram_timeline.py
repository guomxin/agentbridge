"""Persist confirmed ordinary Telegram turns to the original endpoint's Workspace history."""

import hashlib
import json
import re
import time

from agentbridge.core.chat_mirror import public_mirror_text
from agentbridge.workspace.gateway import GatewayRequestError
from .telegram_text import split_telegram_text


def telegram_text_fits(value):
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or not value.strip():
        return False
    try:
        return len(value.encode("utf-16-le")) <= 8192
    except UnicodeEncodeError:
        return False


def telegram_answer(answer):
    if isinstance(answer, str) and answer.strip() and len(answer) <= 50_000:
        public = public_mirror_text(answer)
        if public is not None:
            return public
        return "原请求已结束，请在本人工作台的可信界面查看受保护的信息。"
    return "原请求已结束，答复未能在 Telegram 完整展示。"


async def publish_timeline(host, record, body):
    from aiohttp import web
    from .telegram_identity import verify_telegram_binding
    from .worker import authenticated_session

    route = re.fullmatch(r"telegram:([1-9][0-9]{0,15}):([1-9][0-9]{0,15})", record["endpoint_key"])
    if not route:
        raise web.HTTPForbidden()
    bot, sender = route.groups()
    message, role, text = body["messageId"], body["role"], public_mirror_text(body["text"])
    if (type(message) is not int or not 0 < message < 2**53 or role not in ("user", "assistant") or not text
            or (role == "user" and not telegram_text_fits(text))):
        raise web.HTTPBadRequest()
    key = f"tg:{bot}:{sender}:{message}"
    entry = host.runs.lookup(key, subject=record["user_subject"], endpoint=record["endpoint_id"],
                             conversation=record["session_key"])
    if not entry or entry["run_id"] != body["runId"]:
        raise web.HTTPForbidden()
    if role == "user":
        if body.get("deliveredMessageIds") != [] or not host.runs.matches_message(entry["run_id"], text):
            raise web.HTTPForbidden()
    else:
        receipts = body.get("deliveredMessageIds")
        if (entry["state"] != "completed" or telegram_answer(entry["answer"]) != text or not isinstance(receipts, list)
                or len(receipts) != sum(1 for _ in split_telegram_text(text))
                or any(type(item) is not int or not 0 < item < 2**53 for item in receipts)
                or len(set(receipts)) != len(receipts)):
            raise web.HTTPForbidden()
    async with authenticated_session(host.worker_payload(record, None)) as (session, meta, level):
        if level != "L3" or not await verify_telegram_binding(
                session, host.instance, record["user_subject"], meta, record["endpoint_key"]):
            raise web.HTTPForbidden()
        material = json.dumps([host.instance, record["endpoint_id"], key, role], separators=(",", ":")).encode()
        result = await session.call_tool("agentbridge_host_timeline_append", {
            "agent_host": "nanobot", "endpoint_key": record["endpoint_key"], "client_type": "telegram",
            "external_subject": sender, "conversation_ref": record["session_key"], "account_id": bot,
            "route": {"channel": "telegram", "accountId": bot, "to": sender},
            "message_key": "nanobot:telegram:" + hashlib.sha256(material).hexdigest(),
            "role": role, "text": text,
        }, meta=meta)
        receipt = result.structuredContent or {}
        endpoint, row = receipt.get("endpoint", {}), receipt.get("entry", {})
        if (result.isError or receipt.get("status") != "succeeded" or endpoint.get("endpointId") != record["endpoint_id"]
                or row.get("role") != role or row.get("text") != text):
            raise web.HTTPServiceUnavailable()
    return {"recorded": True}


class TelegramTimeline:
    def __init__(self, pilot):
        self.pilot = pilot
        pilot.db.execute("PRAGMA secure_delete=ON")
        pilot.db.execute("""CREATE TABLE IF NOT EXISTS telegram_chat_projection (
            bot INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, role TEXT NOT NULL,
            text TEXT NOT NULL, next_attempt REAL NOT NULL DEFAULT 0, PRIMARY KEY(bot,chat,message,role))""")
        pilot.db.execute("""CREATE TABLE IF NOT EXISTS telegram_reply_parts (
            bot INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, part INTEGER NOT NULL,
            answer_digest TEXT NOT NULL, receipt INTEGER NOT NULL,
            PRIMARY KEY(bot,chat,message,part), UNIQUE(bot,chat,receipt))""")

    def queue(self, chat, message, role, text):
        # Called within the intake/confirmed-send transaction. The proof row stores no private card or command.
        text = public_mirror_text(text)
        if text and (role == "assistant" or telegram_text_fits(text)):
            self.pilot.db.execute("""INSERT OR IGNORE INTO telegram_chat_projection(bot,chat,message,role,text)
                                  VALUES (?,?,?,?,?)""", (self.pilot.bot_id, chat, message, role, text))

    def flush(self):
        pilot, db = self.pilot, self.pilot.db
        db.execute("""DELETE FROM telegram_chat_projection AS q WHERE bot=? AND EXISTS (
            SELECT 1 FROM messages m WHERE (m.bot,m.chat,m.message)=(q.bot,q.chat,q.message)
              AND m.state IN ('admission_unknown','binding_changed'))""", (pilot.bot_id,))
        db.commit()
        # An earlier unsynchronized role in the same chat cannot be overtaken after a publication timeout.
        # The second pass allows an immediate confirmed answer after the user entry.
        for _ in range(2):
            rows = db.execute("""SELECT q.rowid AS id,q.*,m.binding_hash,m.run_id FROM telegram_chat_projection q
                JOIN messages m USING(bot,chat,message) WHERE q.bot=? AND q.next_attempt<=? AND m.run_id IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM telegram_chat_projection earlier WHERE earlier.bot=q.bot
                    AND earlier.chat=q.chat AND earlier.rowid<q.rowid)
                ORDER BY q.rowid LIMIT 32""", (pilot.bot_id, time.time())).fetchall()
            for row in rows:
                try:
                    binding, digest = pilot._binding(row["chat"])
                    if binding and digest == row["binding_hash"]:
                        receipts = []
                        if row["role"] == "assistant":
                            receipts = [item[0] for item in db.execute("""SELECT receipt FROM telegram_reply_parts
                                WHERE bot=? AND chat=? AND message=? ORDER BY part""",
                                (pilot.bot_id, row["chat"], row["message"]))]
                        pilot.gateway.publish_telegram_timeline(binding, row["message"], row["role"], row["text"],
                                                                row["run_id"], receipts)
                except (RuntimeError, OSError) as error:
                    if not isinstance(error, GatewayRequestError) or error.code not in {
                            "NANOBOT_BINDING_UNAVAILABLE", "NANOBOT_INVALID_REQUEST"}:
                        with db:
                            db.execute("UPDATE telegram_chat_projection SET next_attempt=? WHERE rowid=?",
                                       (time.time() + 5, row["id"]))
                        continue
                with db:
                    db.execute("DELETE FROM telegram_chat_projection WHERE rowid=?", (row["id"],))
