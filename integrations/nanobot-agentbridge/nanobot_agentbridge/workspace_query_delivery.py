"""One authorized Telegram delivery for a Workspace query's public result and card."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

from agentbridge.core.chat_mirror import public_mirror_text
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.nanobot_gateway import PiGatewayClient

from .pi_workspace_result_cards import timestamp
from .telegram_identity import identifier
from .telegram_presentation import (normalize_result_view, render_result_view_message, normalize_interaction,
                                    interaction_can_open, render_interaction_message)
from .private_files import private_text

BINDING_FIELDS = ("user_subject", "endpoint_id", "endpoint_key", "session_key", "credential_ref")


def pairing_ref(value):
    return isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value) is not None


def source_fields(body):
    if body.get("deliveryMode",body.get("kind")) == "workspace_media_v2":
        index,count=body.get("itemIndex"),body.get("itemCount")
        if (any(k in body for k in ("sourceTaskId","sourceEventId","sourceEntryId"))
                or type(index) is not int or type(count) is not int or not 1<=index<=count<=4):
            return None
        return body.get("deliveryId"),"workspace_media_v2",index,count
    if "sourceEventId" in body:
        if ("sourceEntryId" in body or not identifier(body.get("sourceTaskId"))
                or body.get("notificationKind") not in {"public_status", "trusted_interaction"}):
            return None
        return body["sourceEventId"], "host_notification_v2", 1, 1
    if "sourceEntryId" in body:
        if "sourceTaskId" in body:
            return None
        index, count = body.get("partIndex"), body.get("partCount")
        if type(index) is not int or type(count) is not int or not 1 <= index <= count <= 1000:
            return None
        return body["sourceEntryId"], "workspace_text_v2", index, count
    return body.get("sourceTaskId"), "workspace_query_v2", 1, 1


def record_claim(path, endpoint, delivery, attempt, task, pairing, *, record=None,
                 mode="workspace_query_v2", part_index=1, part_count=1, event_task=None, notification_kind=None):
    """Persist only the proof returned by this endpoint's authenticated central claim."""
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS workspace_query_claims(
            delivery TEXT PRIMARY KEY, endpoint TEXT NOT NULL, task TEXT NOT NULL,
            pairing TEXT NOT NULL, attempt INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'workspace_query_v2',
            part_index INTEGER NOT NULL DEFAULT 1, part_count INTEGER NOT NULL DEFAULT 1)""")
        # An interrupted candidate can have a prior v2 claim ledger. Add only
        # metadata; never reset receipts or reinterpret its existing source.
        columns = {row[1] for row in db.execute("PRAGMA table_info(workspace_query_claims)")}
        for name, definition in (("mode", "TEXT NOT NULL DEFAULT 'workspace_query_v2'"),
                                 ("part_index", "INTEGER NOT NULL DEFAULT 1"), ("part_count", "INTEGER NOT NULL DEFAULT 1")):
            if name not in columns:
                db.execute(f"ALTER TABLE workspace_query_claims ADD COLUMN {name} {definition}")
        db.execute("""INSERT INTO workspace_query_claims VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(delivery) DO UPDATE SET attempt=excluded.attempt
            WHERE endpoint=excluded.endpoint AND task=excluded.task AND pairing=excluded.pairing
                AND mode=excluded.mode AND part_index=excluded.part_index AND part_count=excluded.part_count
                AND attempt<=excluded.attempt""", (delivery, endpoint, task, pairing, attempt, mode, part_index, part_count))
        if mode == "host_notification_v2":
            if not identifier(event_task) or notification_kind not in {"public_status", "trusted_interaction"}:
                raise ValueError("Invalid original notification source")
            db.execute("""CREATE TABLE IF NOT EXISTS telegram_notification_claim_sources(
                delivery TEXT PRIMARY KEY,event_task TEXT NOT NULL,kind TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO telegram_notification_claim_sources VALUES(?,?,?)",
                       (delivery,event_task,notification_kind))
        if record and all(isinstance(record.get(k), str) and record[k] for k in (
                *BINDING_FIELDS, "workspaceEndpointId", "workspaceEndpointKey", "tokenFile")):
            token_hash = hashlib.sha256(private_text(Path(record["tokenFile"])).strip().encode()).hexdigest()
            pin = {k: record[k] for k in (*BINDING_FIELDS, "workspaceEndpointId", "workspaceEndpointKey", "tokenFile")}
            db.execute("""CREATE TABLE IF NOT EXISTS workspace_query_receipt_routes(
                delivery TEXT PRIMARY KEY, route_json TEXT NOT NULL, token_hash TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO workspace_query_receipt_routes VALUES(?,?,?)",
                       (delivery, json.dumps(pin, sort_keys=True), token_hash))


def receipt_route(host, body):
    """Recover only an exact previously claimed receipt under its original credential."""
    from aiohttp import web

    if body.get("action") != "ack" or not pairing_ref(body.get("pairingRef")) or not isinstance(body.get("binding"), dict):
        raise web.HTTPForbidden()
    source = source_fields(body)
    if not source:
        raise web.HTTPForbidden()
    binding = body["binding"]
    for original in host.bindings.values():
        if original.get("user_subject") != binding.get("user_subject") or original.get("endpoint_key", "").startswith("telegram:"):
            continue
        request = host.worker_payload(original, None)
        path = Path(request["userDir"]) / "coordinated-runs.sqlite3"
        if not path.is_file():
            continue
        with sqlite3.connect(path) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_query_receipt_routes'").fetchone()
            row = db.execute("""SELECT r.route_json,r.token_hash,c.endpoint,c.task,c.pairing,c.attempt,c.mode,c.part_index,c.part_count
                FROM workspace_query_receipt_routes r JOIN workspace_query_claims c USING(delivery)
                WHERE r.delivery=?""", (body.get("deliveryId"),)).fetchone() if exists else None
        if not row or (row[2], row[3], row[4], row[5], *row[6:]) != (
                binding.get("endpoint_id"), source[0], body.get("pairingRef"), body.get("attempt"), *source[1:]):
            continue
        pin = json.loads(row[0])
        if ({k: pin.get(k) for k in BINDING_FIELDS} != binding
                or pin.get("workspaceEndpointId") != original["endpoint_id"]
                or pin.get("workspaceEndpointKey") != original["endpoint_key"]
                or pin.get("tokenFile") != original["tokenFile"]):
            continue
        current_hash = hashlib.sha256(private_text(Path(original["tokenFile"])).strip().encode()).hexdigest()
        if current_hash != row[1]:
            continue
        return original, {**original, **pin}, path
    raise web.HTTPForbidden()


async def acknowledge_receipt(host, body, *, host_type):
    """No pairing lookup, issuance or send permission is available through this route."""
    from aiohttp import web
    from .session_pool import open_host_session

    original, record, path = receipt_route(host, body)
    req = host.worker_payload(record, None)
    req["hostType"] = host_type
    origins = frozenset(getattr(host, "card_origins", None) or req.get("trustedCardOrigins") or ())
    if host_type == "pi":
        async with host.control_sessions.borrow(original, lane="workspace-query-receipt") as central:
            if central.level != "L3":
                raise web.HTTPForbidden()
            return await handle_request(central.session, central.meta, req, path, body, origins)
    async with open_host_session(host, host.worker_payload(original, None), lane="workspace-query-receipt") as (session, meta, level):
        if level != "L3":
            raise web.HTTPForbidden()
        return await handle_request(session, meta, req, path, body, origins)


async def handle_request(session, meta, req, path, body, origins):
    from aiohttp import web

    delivery, attempt = body.get("deliveryId"), body.get("attempt")
    source = source_fields(body)
    if not source:
        raise web.HTTPBadRequest()
    task, mode, part_index, part_count = source
    pairing, host = body.get("pairingRef"), req.get("hostType", "nanobot")
    if host not in {"nanobot", "pi"} or not pairing_ref(pairing):
        raise web.HTTPForbidden()
    if not identifier(delivery) or not identifier(task) or type(attempt) is not int or not 1 <= attempt <= 5:
        raise web.HTTPBadRequest()
    if not path.is_file():
        raise web.HTTPForbidden()
    with sqlite3.connect(path) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_query_claims'").fetchone()
        claim = db.execute("SELECT endpoint,task,pairing,attempt,mode,part_index,part_count FROM workspace_query_claims WHERE delivery=?",
                           (delivery,)).fetchone() if exists else None
    if claim != (req["endpoint"], task, pairing, attempt, mode, part_index, part_count):
        raise web.HTTPForbidden()
    if mode == "host_notification_v2":
        with sqlite3.connect(path) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telegram_notification_claim_sources'").fetchone()
            original = db.execute("SELECT event_task,kind FROM telegram_notification_claim_sources WHERE delivery=?",
                                  (delivery,)).fetchone() if exists else None
        if original != (body.get("sourceTaskId"),body.get("notificationKind")):
            raise web.HTTPForbidden()
    action = body.get("action")
    args = {"agent_host": host, "action": action, "endpoint_key": req["endpointKey"],
            "pairing_ref": pairing, "delivery_id": delivery, "attempt_count": attempt}
    if action == "ack":
        outcome, receipt, delay = body.get("outcome"), body.get("receipt"), body.get("retryAfterSeconds")
        if outcome == "sent" and type(receipt) is int and 0 < receipt < 2**53 and delay is None:
            args["receipt"] = receipt
        elif outcome == "retry_after" and receipt is None and type(delay) is int and 1 <= delay <= 86400:
            args["retry_after_seconds"] = delay
        elif outcome not in {"unavailable", "delivery_unknown", "rejected"} or receipt is not None or delay is not None:
            raise web.HTTPBadRequest()
        args["outcome"] = outcome
    elif action not in {"issue", "begin"}:
        raise web.HTTPBadRequest()
    tool = {"host_notification_v2":"agentbridge_host_notification_delivery",
            "workspace_media_v2":"agentbridge_host_workspace_media_delivery"}.get(mode,"agentbridge_host_workspace_result_view")
    reply = await session.call_tool(tool, args, meta=meta)
    value = reply.structuredContent
    if reply.isError or not isinstance(value, dict):
        raise web.HTTPBadGateway()
    if action in {"ack", "begin"}:
        key = "recorded" if action == "ack" else "allowed"
        if type(value.get(key)) is not bool:
            raise web.HTTPBadGateway()
        return {key: value[key]}
    if value.get("available") is False:
        return {"available": False}
    if value.get("available") is not True or timestamp(value.get("leaseExpiresAt")) is None:
        raise web.HTTPBadGateway()
    result = {"available": True, "leaseExpiresAt": value["leaseExpiresAt"]}
    if mode == "workspace_media_v2":
        from .workspace_media_delivery import resource
        item=resource(value.get("resource"))
        if not item or any(value.get(k) is not None for k in ("publicText","resultView","interaction")):
            raise web.HTTPBadGateway()
        return {**result,"resource":item}
    if mode == "host_notification_v2":
        if (value.get("sourceEventId"),value.get("sourceTaskId"),value.get("notificationKind")) != (task,*original):
            raise web.HTTPBadGateway()
        result.update(sourceEventId=task, sourceTaskId=original[0], notificationKind=original[1])
        if value.get("resultView") is not None:
            raise web.HTTPBadGateway()
        if original[1] == "trusted_interaction":
            raw = value.get("interaction")
            card = normalize_interaction(raw,origins)
            if (value.get("publicText") is not None or not card or not interaction_can_open(card)
                    or (raw.get("presentation") or {}).get("endpointId") != req["endpoint"]):
                raise web.HTTPBadGateway()
            result["interaction"] = card
            return result
        if value.get("interaction") is not None:
            raise web.HTTPBadGateway()
    if value.get("publicText") is not None:
        text = public_mirror_text(value["publicText"])
        if text is None:
            raise web.HTTPBadGateway()
        result["publicText"] = text
    if value.get("resultView") is not None:
        if mode == "workspace_text_v2":
            raise web.HTTPBadGateway()
        view = normalize_result_view(value["resultView"], source_task_id=task, endpoint_id=req["endpoint"],
                                     allowed_origins=origins)
        if not view or view["selectionAvailable"] is not True or timestamp(view.get("sourceExpiresAt")) is None:
            raise web.HTTPBadGateway()
        result["resultView"] = view
    if "publicText" not in result and "resultView" not in result:
        raise web.HTTPBadGateway()
    return result


def compact_text(text):
    size = 0
    for index, char in enumerate(text):
        size += 2 if ord(char) > 0xffff else 1
        if size > 3000:
            return text[:index] + "\n……完整结果请在工作台查看。"
    return text


@dataclass
class Claim:
    chat: int
    binding: dict
    digest: str
    delivery: str
    attempt: int
    task: str
    pairing: str
    entry: str | None = None
    part_index: int = 1
    part_count: int = 1
    event_task: str | None = None
    notification_kind: str | None = None
    media: bool = False

    @property
    def dedupe_key(self):
        if self.media:
            return "workspace_media:" + self.delivery
        if self.event_task:
            return "notification_event:" + self.task
        if self.entry:
            return "workspace_text:" + self.entry + ":" + str(self.part_index)
        # Shares the old Pi/card receipt namespace. A new profile cannot replay it.
        return "result_view:" + self.task

    def payload(self):
        source = {"sourceEntryId": self.entry, "partIndex": self.part_index, "partCount": self.part_count} if self.entry else {"sourceTaskId": self.task}
        if self.event_task:
            source = {"sourceEventId":self.task,"sourceTaskId":self.event_task,"notificationKind":self.notification_kind}
        if self.media:
            source={"deliveryMode":"workspace_media_v2","itemIndex":self.part_index,"itemCount":self.part_count}
        return {"binding": self.binding, "deliveryId": self.delivery, "attempt": self.attempt,
                **source, "pairingRef": self.pairing}


class WorkspaceQueries:
    """Central I/O runs off-thread; only the poll owner touches Telegram and its ledger."""

    def __init__(self, terminal):
        self.terminal, self.pilot = terminal, terminal.pilot
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace-query")
        self.pending, self.issued = {}, {}
        with self.pilot.db:
            self.pilot.db.execute("""CREATE TABLE IF NOT EXISTS telegram_workspace_query_attempts(
                bot INTEGER NOT NULL, chat INTEGER NOT NULL, delivery TEXT NOT NULL, attempt INTEGER NOT NULL,
                retry_seconds INTEGER, not_before REAL, claim_json TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0, next_ack REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(bot,chat,delivery))""")

    def _request(self, claim, action, **fields):
        if action == "ack":
            with self.pilot.db:
                self.pilot.db.execute("""UPDATE telegram_workspace_query_attempts SET next_ack=?
                    WHERE bot=? AND chat=? AND delivery=? AND attempt=?""",
                    (time.time() + 5, self.pilot.bot_id, claim.chat, claim.delivery, claim.attempt))
        options = {"timeout": PiGatewayClient.result_view_timeout} if action == "issue" else {}
        route = "telegram-workspace-media-v2" if claim.media else "telegram-workspace-result-view"
        future = self.executor.submit(self.pilot.gateway._request, route,
                                      {**claim.payload(), "action": action, **fields}, **options)
        self.pending[claim.delivery] = (claim, action, future)

    def _receipt(self, claim):
        return self.pilot.db.execute(
            "SELECT delivery,state,receipt FROM telegram_card_deliveries WHERE bot=? AND chat=? AND dedupe_key=?",
            (self.pilot.bot_id, claim.chat, claim.dedupe_key)).fetchone()

    def _retry(self, claim):
        return self.pilot.db.execute("""SELECT attempt,retry_seconds,not_before FROM telegram_workspace_query_attempts
            WHERE bot=? AND chat=? AND delivery=?""", (self.pilot.bot_id, claim.chat, claim.delivery)).fetchone()

    def _ack_known(self, claim, row):
        delivery, state, receipt = row
        if delivery != claim.delivery:
            self._request(claim, "ack", outcome="unavailable")
        elif state == "delivered" and type(receipt) is int and 0 < receipt < 2**53:
            self._request(claim, "ack", outcome="sent", receipt=receipt)
        elif state == "retry_after":
            retry = self._retry(claim)
            if retry and type(retry[1]) is int and 1 <= retry[1] <= 86400:
                self._request(claim, "ack", outcome="retry_after", retryAfterSeconds=retry[1])
        else:
            outcome = "rejected" if state == "rejected" else (
                "unavailable" if state == "unavailable" else "delivery_unknown")
            self._request(claim, "ack", outcome=outcome)

    def claim(self, chat, binding, digest, event):
        delivery, attempt, pairing = (event.get(k) for k in ("deliveryId", "attempt", "pairingRef"))
        source = source_fields(event)
        if not source:
            return
        task, mode, part_index, part_count = source
        if (not identifier(delivery) or not identifier(task) or not pairing_ref(pairing)
                or type(attempt) is not int or not 1 <= attempt <= 5):
            return
        old = self.pending.get(delivery)
        if old:
            if old[0].attempt >= attempt:
                return
            del self.pending[delivery]
            old[2].cancel()
            self.issued.pop(delivery, None)
        claim = Claim(chat, dict(binding), digest, delivery, attempt, task, pairing,
                      task if mode == "workspace_text_v2" else None, part_index, part_count,
                      event.get("sourceTaskId") if mode == "host_notification_v2" else None,
                      event.get("notificationKind") if mode == "host_notification_v2" else None, mode=="workspace_media_v2")
        row = self._receipt(claim)
        if row and row[0] == delivery and row[1] == "retry_after":
            retry = self._retry(claim)
            if retry and attempt > retry[0] and time.time() >= retry[2]:
                with self.pilot.db:
                    self.pilot.db.execute("DELETE FROM telegram_card_deliveries WHERE bot=? AND chat=? AND dedupe_key=?",
                                          (self.pilot.bot_id, chat, claim.dedupe_key))
                row = None
        if row:
            self._ack_known(claim, row)
        else:
            self._request(claim, "issue")

    def _message(self, claim, value):
        if value.get("available") is not True:
            return None
        if claim.media:
            from .workspace_media_delivery import message
            media=message(value)
            lease=timestamp(value.get("leaseExpiresAt"))
            if not media or lease is None or time.time()>=min(lease,media[1]):
                return None
            return media[0],min(lease,media[1])
        text = public_mirror_text(value.get("publicText"))
        if value.get("publicText") is not None and text is None:
            return None
        view = value.get("resultView")
        deadlines = [timestamp(value.get("leaseExpiresAt"))]
        if claim.event_task:
            if (value.get("sourceEventId"),value.get("sourceTaskId"),value.get("notificationKind")) != (
                    claim.task,claim.event_task,claim.notification_kind) or view is not None:
                return None
            if claim.notification_kind == "trusted_interaction":
                raw = value.get("interaction")
                url = urlsplit(str(((raw or {}).get("presentation") or {}).get("url") or "")) if isinstance(raw,dict) else None
                # The private handler already validated the configured HTTPS origin.
                origins = frozenset({f"https://{url.netloc}"}) if url and url.scheme == "https" and url.netloc else frozenset()
                card = normalize_interaction(raw,origins)
                if text is not None or not card or not interaction_can_open(card):
                    return None
                params = render_interaction_message(card,origins)
                deadlines.append(timestamp(card.get("expiresAt")))
                if not params or None in deadlines or time.time() >= min(deadlines):
                    return None
                return params,min(deadlines)
            if value.get("interaction") is not None or not text:
                return None
        if claim.entry and (view is not None or text is None or len(text.encode("utf-16-le")) // 2 > 4096):
            return None
        if view is not None:
            if not isinstance(view, dict) or view.get("sourceTaskId") != claim.task or view.get("selectionAvailable") is not True:
                return None
            deadlines.extend([timestamp(view.get("sourceExpiresAt")), timestamp(view.get("expiresAt"))])
            params = render_result_view_message(view, **({"summary": compact_text(text)} if text else {}))
        elif text:
            params = {"text": text if claim.entry else compact_text(text), "link_preview_options": {"is_disabled": True}}
        else:
            return None
        if None in deadlines or time.time() >= min(deadlines):
            return None
        return params, min(deadlines)

    def _state(self, claim, state, *, receipt=None, retry_seconds=None):
        with self.pilot.db:
            self.pilot.db.execute("""UPDATE telegram_card_deliveries SET state=?,receipt=?
                WHERE bot=? AND chat=? AND dedupe_key=? AND delivery=?""",
                (state, receipt, self.pilot.bot_id, claim.chat, claim.dedupe_key, claim.delivery))
            if claim.entry and state == "delivered" and receipt:
                self.pilot.navigation.remember(claim.chat, receipt, claim.digest,
                    {"kind": "workspace_public_message", "receipt": receipt})
            if retry_seconds is not None:
                self.pilot.db.execute("""UPDATE telegram_workspace_query_attempts SET retry_seconds=?,not_before=?
                    WHERE bot=? AND chat=? AND delivery=?""",
                    (retry_seconds, time.time() + retry_seconds, self.pilot.bot_id, claim.chat, claim.delivery))

    def _reserve(self, claim, issued):
        active, proof = self.pilot._binding(claim.chat)
        if active != claim.binding or proof != claim.digest or time.time() >= issued[1]:
            return False
        with self.pilot.db:
            inserted = self.pilot.db.execute("INSERT OR IGNORE INTO telegram_card_deliveries VALUES(?,?,?,?,'beginning',NULL)",
                                           (self.pilot.bot_id, claim.chat, claim.dedupe_key, claim.delivery)).rowcount
            if inserted:
                self.pilot.db.execute("""INSERT OR REPLACE INTO telegram_workspace_query_attempts
                    (bot,chat,delivery,attempt,retry_seconds,not_before,claim_json,acknowledged,next_ack)
                    VALUES(?,?,?,?,NULL,NULL,?,0,0)""",
                    (self.pilot.bot_id, claim.chat, claim.delivery, claim.attempt, json.dumps(asdict(claim))))
        return bool(inserted)

    def _send(self, claim, issued):
        from .telegram import TelegramRejected, TelegramUnavailable, numeric

        params, deadline = issued
        # A binding outage before calling Telegram is a definite non-send.
        try:
            active, proof = self.pilot._binding(claim.chat)
        except TelegramUnavailable:
            self._state(claim, "retry_after", retry_seconds=5)
            return
        if active != claim.binding or proof != claim.digest or time.time() >= deadline:
            self._state(claim, "unavailable")
            return
        self._state(claim, "sending")
        state, receipt, delay = "delivery_unknown", None, None
        try:
            if claim.media:
                from .telegram_files import document_receipt
                result=self.pilot.api.send_document(claim.chat,**params)
                receipt=document_receipt(result,self.pilot.bot_id,claim.chat,len(params["data"]))
                if receipt and not self.pilot.db.execute("""SELECT 1 FROM telegram_card_deliveries
                        WHERE bot=? AND chat=? AND receipt=? AND dedupe_key!=?""",
                        (self.pilot.bot_id,claim.chat,receipt,claim.dedupe_key)).fetchone():
                    state="delivered"
                else:
                    receipt=None
            else:
                result = self.pilot.api.call("sendMessage", chat_id=claim.chat, **params)
                if (isinstance(result, dict) and numeric(result.get("message_id"))
                        and (result.get("from") or {}).get("id") == self.pilot.bot_id
                        and (result.get("chat") or {}).get("id") == claim.chat
                        and (result.get("chat") or {}).get("type") == "private"):
                    state, receipt = "delivered", result["message_id"]
        except TelegramRejected as exc:
            delay = exc.retry_after if exc.error_code == 429 else None
            state = "retry_after" if delay is not None else "rejected"
        except TelegramUnavailable:
            pass
        self._state(claim, state, receipt=receipt, retry_seconds=delay)
        if receipt and not claim.entry and not claim.media:
            with self.pilot.db:
                self.pilot.navigation.remember(claim.chat, receipt, claim.digest,
                    {"kind": "task" if claim.event_task else "result", "id": claim.event_task or claim.task})

    def drain(self):
        from .telegram import TelegramUnavailable

        for delivery, (claim, action, future) in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[delivery]
            try:
                value = future.result()
            except GatewayRequestError:
                if action == "begin":
                    self.issued.pop(delivery, None)
                    self._state(claim, "delivery_unknown")
                    self._ack_known(claim, self._receipt(claim))
                continue
            if action == "ack":
                if isinstance(value, dict) and value.get("recorded") is True:
                    with self.pilot.db:
                        self.pilot.db.execute("""UPDATE telegram_workspace_query_attempts SET acknowledged=1
                            WHERE bot=? AND chat=? AND delivery=? AND attempt=?""",
                            (self.pilot.bot_id, claim.chat, delivery, claim.attempt))
                continue
            if action == "begin":
                issued = self.issued.pop(delivery, None)
                if not isinstance(value, dict) or type(value.get("allowed")) is not bool:
                    self._state(claim, "delivery_unknown")
                elif not value["allowed"] or issued is None:
                    self._state(claim, "unavailable")
                else:
                    self._send(claim, issued)
                self._ack_known(claim, self._receipt(claim))
                continue
            issued = self._message(claim, value) if isinstance(value, dict) else None
            if issued is None:
                self._request(claim, "ack", outcome="unavailable")
                continue
            try:
                if self._reserve(claim, issued):
                    self.issued[delivery] = issued
                    self._request(claim, "begin")
                else:
                    row = self._receipt(claim)
                    if row:
                        self._ack_known(claim, row)
            except TelegramUnavailable:
                # A failed preflight has neither a reservation nor a Telegram send.
                continue
        self.recover()

    def recover(self):
        """Reconcile durable receipts without asking central for another send claim."""
        rows = self.pilot.db.execute("""SELECT chat,delivery,attempt,claim_json FROM telegram_workspace_query_attempts
            WHERE bot=? AND acknowledged=0 AND next_ack<=? ORDER BY next_ack LIMIT 32""",
            (self.pilot.bot_id, time.time())).fetchall()
        for chat, delivery, attempt, raw in rows:
            if delivery in self.pending:
                continue
            try:
                claim = Claim(**json.loads(raw))
            except (ValueError, TypeError):
                continue
            if (claim.chat != chat or claim.delivery != delivery or claim.attempt != attempt
                    or not identifier(claim.task) or not pairing_ref(claim.pairing) or not isinstance(claim.binding, dict)):
                continue
            receipt = self._receipt(claim)
            if receipt and receipt[0] == delivery and receipt[1] not in {"beginning", "sending"}:
                self._ack_known(claim, receipt)
