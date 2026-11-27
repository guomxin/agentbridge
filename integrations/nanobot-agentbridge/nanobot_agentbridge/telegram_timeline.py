"""Persist confirmed ordinary Telegram turns to the original endpoint's Workspace history."""

import hashlib
import json
import re
import time

from agentbridge.core.chat_mirror import PUBLIC_TEXT_MAX_CHARACTERS, public_mirror_text
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
    if isinstance(answer, str) and answer.strip() and len(answer) <= PUBLIC_TEXT_MAX_CHARACTERS:
        public = public_mirror_text(answer, maximum=PUBLIC_TEXT_MAX_CHARACTERS)
        if public is not None:
            return public
        return "原请求已结束，请在本人工作台的可信界面查看受保护的信息。"
    if isinstance(answer, str) and len(answer) > PUBLIC_TEXT_MAX_CHARACTERS:
        return "原请求已结束，答复超过 200,000 字符的展示上限。请在本人工作台的原对话查看可用记录，必要时缩小问题范围后分次查询。"
    return "原请求已结束，未取得可展示的答复。请在本人工作台的原对话查看状态。"


def terminal_answer(state):
    return {"stopped": "原请求的生成已停止，请在本人工作台的原对话查看进度；业务状态以核验结果为准。",
            "unknown": "原请求的结果暂时无法确认，请在本人工作台的原对话核对；系统不会自动重新办理。"}[state]


def record_received(host, record, key, text):
    """Authenticated sole poller intake fact; deliberately independent of model admission."""
    db = getattr(host.runs, "_db", None)
    if db is None:
        db = host.runs.db
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_received_sources (
        subject TEXT NOT NULL, endpoint TEXT NOT NULL, conversation TEXT NOT NULL, request_key TEXT NOT NULL,
        credential_ref TEXT NOT NULL, text_digest TEXT NOT NULL, PRIMARY KEY(subject,request_key))""")
    scope = (record["user_subject"], record["endpoint_id"], record["session_key"], key,
             record["credential_ref"], hashlib.sha256(text.encode()).hexdigest())
    with db:
        db.execute("INSERT OR IGNORE INTO telegram_received_sources VALUES(?,?,?,?,?,?)", scope)
        row = db.execute("SELECT * FROM telegram_received_sources WHERE subject=? AND request_key=?", (scope[0], key)).fetchone()
        return tuple(row) == scope


async def publish_timeline(host, record, body):
    from aiohttp import web
    from .telegram_identity import verify_telegram_binding
    from .session_pool import open_host_session

    route = re.fullmatch(r"telegram:([1-9][0-9]{0,15}):([1-9][0-9]{0,15})", record["endpoint_key"])
    if not route:
        raise web.HTTPForbidden()
    bot, sender = route.groups()
    message, role = body.get("messageId"), body.get("role")
    mode = body.get("projectionMode")
    if mode not in {None, "telegram_received_v2", "host_result_v2"}:
        raise web.HTTPBadRequest()
    text = public_mirror_text(body.get("text"), maximum=PUBLIC_TEXT_MAX_CHARACTERS if mode else 50_000)
    if (type(message) is not int or not 0 < message < 2**53 or role not in ("user", "assistant") or not text
            or (role == "user" and not telegram_text_fits(text))):
        raise web.HTTPBadRequest()
    body = {**body, "text": text}
    key = f"tg:{bot}:{sender}:{message}"
    entry = host.runs.lookup(key, subject=record["user_subject"], endpoint=record["endpoint_id"],
                             conversation=record["session_key"])
    received = mode == "telegram_received_v2"
    if received and (role != "user" or body.get("deliveredMessageIds") != [] or body.get("sourceState") is not None):
        raise web.HTTPForbidden()
    if not received and (not entry or entry["run_id"] != body.get("runId")):
        raise web.HTTPForbidden()
    if role == "user":
        if mode == "host_result_v2" or (not received and (
                body.get("deliveredMessageIds") != [] or not host.runs.matches_message(entry["run_id"], text))):
            raise web.HTTPForbidden()
        if received and entry and not host.runs.matches_message(entry["run_id"], text):
            raise web.HTTPForbidden()
    else:
        receipts = body.get("deliveredMessageIds")
        if mode == "host_result_v2":
            state = body.get("sourceState")
            if state not in {"completed", "stopped", "unknown"} or state != entry["state"]:
                raise web.HTTPForbidden()
            expected = telegram_answer(entry["answer"]) if state == "completed" else terminal_answer(state)
            if expected != text:
                raise web.HTTPForbidden()
        elif entry["state"] != "completed" or telegram_answer(entry["answer"]) != text:
            raise web.HTTPForbidden()
        if (not isinstance(receipts, list)
                or (mode is None and len(receipts) != sum(1 for _ in split_telegram_text(text)))
                or any(type(item) is not int or not 0 < item < 2**53 for item in receipts)
                or len(set(receipts)) != len(receipts)):
            raise web.HTTPForbidden()
    from .telegram_reception import validate
    source = await validate(host, record, key)
    payload = host.worker_payload(record, None)
    host_type = payload.get("hostType", "nanobot")
    async with open_host_session(host, payload, lane="control") as (session, meta, level):
        if level != "L3" or not await verify_telegram_binding(
                session, host.instance, record["user_subject"], meta, record["endpoint_key"], host_type=host_type):
            raise web.HTTPForbidden()
        if received and not record_received(host, record, key, text):
            raise web.HTTPForbidden()
        from .telegram_history_receipts import freeze
        freeze(host, record, key, body, source)
        return await _central_publish(session, meta, host.instance, record, body, host_type, source=source)


async def _central_publish(session, meta, instance, record, body, host_type, *, receipt_only=False, source=None):
    from aiohttp import web
    _, bot, sender = record["endpoint_key"].split(":")
    key = f"tg:{bot}:{sender}:{body['messageId']}"
    mode, role, text = body.get("projectionMode"), body["role"], body["text"]
    material = json.dumps([instance, record["endpoint_id"], key, role], separators=(",", ":")).encode()
    message_key = host_type + ":telegram:" + hashlib.sha256(material).hexdigest()
    result = await session.call_tool("agentbridge_host_timeline_append", {
        "agent_host": host_type, "endpoint_key": record["endpoint_key"], "client_type": "telegram",
        "external_subject": sender, "conversation_ref": record["session_key"], "account_id": bot,
        "route": {"channel": "telegram", "accountId": bot, "to": sender},
        "message_key": message_key,
        "role": role, "text": text,
        **({"projection_v2": True} if mode else {}),
        **({"projection_receipt_only": True} if receipt_only else {}),
        **({"source_message_id": source["messageId"], "received_at": source["receivedAt"],
            "pairing_ref": source["pairingRef"]} if source is not None else {}),
    }, meta=meta)
    receipt = result.structuredContent or {}
    if receipt_only and not result.isError and receipt.get("status") == "unavailable" and receipt.get("recorded") is False:
        return {"recorded": False}
    endpoint, row = receipt.get("endpoint", {}), receipt.get("entry", {})
    if (result.isError or receipt.get("status") != "succeeded" or endpoint.get("endpointId") != record["endpoint_id"]
            or row.get("role") != role or (not mode and row.get("text") != text)):
        raise web.HTTPServiceUnavailable()
    if mode:
        proof = receipt.get("projectionReceipt", {})
        if (not isinstance(proof, dict) or not isinstance(proof.get("entryId"), str) or not proof["entryId"]
                or proof["entryId"] != row.get("entryId")
                or proof.get("messageKey") != message_key or proof.get("sourceEndpointId") != record["endpoint_id"]
                or proof.get("role") != role or proof.get("textSha256") != hashlib.sha256(text.encode()).hexdigest()
                or (row.get("retained") is not False and row.get("text") != text)):
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
        columns = {row[1] for row in pilot.db.execute("PRAGMA table_info(telegram_chat_projection)")}
        for name, kind in (("projection_mode", "TEXT"), ("source_state", "TEXT"),
                           ("attempt_count", "INTEGER NOT NULL DEFAULT 0"), ("created_at", "REAL NOT NULL DEFAULT 0")):
            if name not in columns:
                pilot.db.execute(f"ALTER TABLE telegram_chat_projection ADD COLUMN {name} {kind}")
        pilot.db.execute("""CREATE TABLE IF NOT EXISTS telegram_projection_outcomes (
            bot INTEGER NOT NULL,chat INTEGER NOT NULL,message INTEGER NOT NULL,role TEXT NOT NULL,
            status TEXT NOT NULL,reason TEXT,PRIMARY KEY(bot,chat,message,role))""")
        pilot.db.commit()

    def queue(self, chat, message, role, text, *, projection_mode=None, source_state=None):
        # Called within the intake/confirmed-send transaction. The proof row stores no private card or command.
        text = public_mirror_text(text, maximum=PUBLIC_TEXT_MAX_CHARACTERS if projection_mode else 50_000)
        if text and (role == "assistant" or telegram_text_fits(text)):
            key = (self.pilot.bot_id, chat, message, role)
            if self.pilot.db.execute("SELECT 1 FROM telegram_projection_outcomes WHERE bot=? AND chat=? AND message=? AND role=?", key).fetchone():
                return
            self.pilot.db.execute("""INSERT OR IGNORE INTO telegram_chat_projection
                (bot,chat,message,role,text,projection_mode,source_state,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (*key, text, projection_mode, source_state, time.time()))

    def _finish(self, row, status, reason=None):
        with self.pilot.db:
            self.pilot.db.execute("INSERT OR REPLACE INTO telegram_projection_outcomes VALUES(?,?,?,?,?,?)",
                (row["bot"],row["chat"],row["message"],row["role"],status,reason))
            self.pilot.db.execute("DELETE FROM telegram_chat_projection WHERE rowid=?", (row["id"],))

    def _recover(self):
        """Original frozen credentials can query only a previously committed receipt."""
        pilot, db = self.pilot, self.pilot.db
        blocked = set()
        rows = db.execute("""SELECT q.rowid AS id,q.*,m.run_id,s.binding_json FROM telegram_chat_projection q
            JOIN messages m USING(bot,chat,message) JOIN telegram_inbound_sources s USING(bot,chat,message)
            WHERE q.bot=? AND q.projection_mode IS NOT NULL ORDER BY q.rowid LIMIT 32""", (pilot.bot_id,)).fetchall()
        for row in rows:
            if row["attempt_count"] >= 24 or time.time() - row["created_at"] >= 86400:
                self._finish(row, "expired", "projection_retry_budget")
                continue
            if row["next_attempt"] > time.time():
                blocked.add(row["id"])
                continue
            try:
                recorded = pilot.gateway.publish_telegram_timeline(json.loads(row["binding_json"]), row["message"],
                    row["role"], row["text"], row["run_id"], [], projection_mode=row["projection_mode"],
                    source_state=row["source_state"], receipt_only=True)
                if recorded:
                    self._finish(row, "acknowledged")
            except (RuntimeError, OSError):
                blocked.add(row["id"])
                with db:
                    db.execute("UPDATE telegram_chat_projection SET next_attempt=?,attempt_count=attempt_count+1 WHERE rowid=?",
                        (time.time() + min(300, 5 * (2 ** min(row["attempt_count"], 6))), row["id"]))
        return blocked

    def flush(self):
        pilot, db = self.pilot, self.pilot.db
        blocked = self._recover()
        stale = db.execute("""SELECT q.rowid AS id,q.*,m.state AS message_state FROM telegram_chat_projection AS q
            JOIN messages m USING(bot,chat,message) WHERE q.bot=? AND (
              m.state='binding_changed' OR (m.state='admission_unknown' AND q.projection_mode IS NULL))""", (pilot.bot_id,)).fetchall()
        for row in stale:
            if row["id"] in blocked:
                continue
            self._finish(row, "revoked" if row["message_state"] == "binding_changed" else "source_unavailable", row["message_state"])
        # An earlier unsynchronized role in the same chat cannot be overtaken after a publication timeout.
        # The second pass allows an immediate confirmed answer after the user entry.
        for _ in range(2):
            rows = db.execute("""SELECT q.rowid AS id,q.*,m.binding_hash,m.run_id FROM telegram_chat_projection q
                JOIN messages m USING(bot,chat,message) WHERE q.bot=? AND q.next_attempt<=?
                AND (m.run_id IS NOT NULL OR q.projection_mode='telegram_received_v2')
                AND NOT EXISTS (SELECT 1 FROM telegram_chat_projection earlier WHERE earlier.bot=q.bot
                    AND earlier.chat=q.chat AND earlier.rowid<q.rowid
                    AND (q.projection_mode IS NULL OR earlier.message=q.message))
                ORDER BY q.rowid LIMIT 32""", (pilot.bot_id, time.time())).fetchall()
            for row in rows:
                if row["id"] in blocked:
                    continue
                if row["projection_mode"] and (row["attempt_count"] >= 24 or time.time() - row["created_at"] >= 86400):
                    self._finish(row, "expired", "projection_retry_budget")
                    continue
                try:
                    binding, digest = pilot._binding(row["chat"])
                    if not binding or digest != row["binding_hash"]:
                        self._finish(row, "revoked", "binding_changed")
                        continue
                    if binding:
                        receipts = []
                        if row["role"] == "assistant":
                            receipts = [item[0] for item in db.execute("""SELECT receipt FROM telegram_reply_parts
                                WHERE bot=? AND chat=? AND message=? ORDER BY part""",
                                (pilot.bot_id, row["chat"], row["message"]))]
                        options = {"projection_mode":row["projection_mode"], "source_state":row["source_state"]} if row["projection_mode"] else {}
                        pilot.gateway.publish_telegram_timeline(binding, row["message"], row["role"], row["text"],
                                                                row["run_id"], receipts, **options)
                except (RuntimeError, OSError) as error:
                    if not isinstance(error, GatewayRequestError) or error.code not in {
                            pilot.host_type.upper() + "_BINDING_UNAVAILABLE", pilot.host_type.upper() + "_INVALID_REQUEST"}:
                        with db:
                            db.execute("UPDATE telegram_chat_projection SET next_attempt=?,attempt_count=attempt_count+1 WHERE rowid=?",
                                       (time.time() + min(300, 5 * (2 ** min(row["attempt_count"], 6))), row["id"]))
                        continue
                    self._finish(row, "rejected", "source_or_binding_rejected")
                    continue
                self._finish(row, "acknowledged")
