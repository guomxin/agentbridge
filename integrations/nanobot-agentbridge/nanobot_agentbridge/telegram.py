"""One private-chat Telegram Bot API consumer for an independent NanoBot instance.

Start with python -m nanobot_agentbridge.telegram --config /absolute/private/config.json.
The AgentBridge wheel supplies the same authenticated local host client as Workspace.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from urllib.parse import urlsplit

from agentbridge.core.chat_mirror import public_mirror_text
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient

from .private_files import private_directory, private_text
from .telegram_credentials import bot_token
from .telegram_presentation import (
    format_ordinary_telegram_message,
    render_interaction_message,
    render_result_view_message,
)
from .telegram_text import split_telegram_text
from .telegram_timeline import TelegramTimeline, telegram_answer, telegram_text_fits


def numeric(value):
    return type(value) is int and 0 < value < 2**53


class TelegramUnavailable(RuntimeError):
    """The request or its acknowledgement failed. Never report the Bot API URL."""


class TelegramAPI:
    def __init__(self, token, connection_factory=None):
        if (
            not isinstance(token, str)
            or not token
            or not all(c.isascii() and (c.isalnum() or c in "_-:") for c in token)
        ):
            raise ValueError("Invalid Telegram credential")
        self._token = token
        self._connect = connection_factory or (
            lambda: http.client.HTTPSConnection("api.telegram.org", timeout=8)
        )

    def call(self, method, **params):
        connection = self._connect()
        try:
            connection.request(
                "POST",
                f"/bot{self._token}/{method}",
                json.dumps(params).encode(),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(1_048_577)
            if response.status != 200 or len(raw) > 1_048_576:
                raise TelegramUnavailable("Telegram request was not confirmed")
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get("ok") is not True
                or "result" not in data
            ):
                raise TelegramUnavailable("Invalid Telegram receipt")
            return data["result"]
        except (OSError, ValueError, http.client.HTTPException):
            raise TelegramUnavailable(
                "Telegram request could not be reconciled"
            ) from None
        finally:
            connection.close()


class TelegramPilot:
    def __init__(
        self, database, *, api, bot_id, instance, gateway, bindings, bot_username=None, allow_pairing=False
    ):
        self.api, self.bot_id, self.instance = api, bot_id, instance
        self.gateway, self.bindings = gateway, bindings
        self.bot_username = bot_username
        self.allow_pairing = allow_pairing
        self.next_notifications = 0.0
        self._typing_at: dict[int, float] = {}
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS channel_meta(bot INTEGER PRIMARY KEY, next_update INTEGER, observed_at REAL);
            CREATE TABLE IF NOT EXISTS messages(
                bot INTEGER, chat INTEGER, message INTEGER, subject TEXT, binding_hash TEXT,
                state TEXT, run_id TEXT, receipt INTEGER, PRIMARY KEY(bot,chat,message));
            CREATE INDEX IF NOT EXISTS messages_waiting ON messages(bot,state);
            CREATE TABLE IF NOT EXISTS telegram_card_deliveries(
                bot INTEGER NOT NULL, chat INTEGER NOT NULL, dedupe_key TEXT NOT NULL,
                delivery TEXT, state TEXT NOT NULL, receipt INTEGER,
                PRIMARY KEY(bot,chat,dedupe_key));
            UPDATE telegram_card_deliveries SET state='delivery_unknown' WHERE state='sending';
        """)

        self.timeline = TelegramTimeline(self)
        self.terminal = None
        if allow_pairing:
            from .notifications import TerminalMessages
            self.terminal = TerminalMessages(self)

    def _typing(self, chat: int) -> None:
        now = time.monotonic()
        if now - self._typing_at.get(chat, 0.0) < 4.0:
            return
        self._typing_at[chat] = now
        try:
            self.api.call("sendChatAction", chat_id=chat, action="typing")
        except TelegramUnavailable:
            pass

    def verify(self):
        me = self.api.call("getMe")
        if (
            not isinstance(me, dict)
            or me.get("is_bot") is not True
            or me.get("id") != self.bot_id
            or (
                self.bot_username is not None
                and me.get("username") != self.bot_username
            )
        ):
            raise ValueError("Telegram bot identity did not match the configured pilot")
        hook = self.api.call("getWebhookInfo")
        if not isinstance(hook, dict) or hook.get("url") != "":
            raise ValueError(
                "This pilot requires an independent polling bot without a webhook"
            )

    def _binding(self, chat):
        binding = self.bindings().get(str(chat))
        try:
            if not binding and self.allow_pairing:
                binding=self.gateway.resolve_telegram_sender(self.bot_id,chat)
            if not binding:
                return None,None
            if not self.gateway.verify_telegram_binding(binding):
                return None, None
        except GatewayRequestError as exc:
            if exc.code == "NANOBOT_BINDING_UNAVAILABLE":
                return None, None
            raise TelegramUnavailable(
                "Central Telegram identity temporarily unavailable"
            ) from None
        digest = hashlib.sha256(
            json.dumps(
                [self.instance, self.bot_id, chat, binding],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return binding, digest

    def _key(self, chat, message):
        return f"tg:{self.bot_id}:{chat}:{message}"

    def _state(self, chat, message, state, *, run=None, receipt=None, public_answer=None, confirmed_part=None):
        with self.db:
            if confirmed_part:
                self.db.execute("INSERT INTO telegram_reply_parts VALUES(?,?,?,?,?,?)",
                                (self.bot_id, chat, message, *confirmed_part))
            self.db.execute(
                """UPDATE messages SET state=?,run_id=COALESCE(?,run_id),receipt=COALESCE(?,receipt)
                WHERE bot=? AND chat=? AND message=?""",
                (state, run, receipt, self.bot_id, chat, message),
            )
            if public_answer is not None:
                self.timeline.queue(chat, message, "assistant", public_answer)

    def _record(self, chat, message):
        return self.db.execute(
            "SELECT * FROM messages WHERE bot=? AND chat=? AND message=?",
            (self.bot_id, chat, message),
        ).fetchone()

    def ingest(self, update):
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return
        chat_id, message_id = chat.get("id"), msg.get("message_id")
        if (
            chat.get("type") != "private"
            or not numeric(chat_id)
            or sender.get("is_bot") is not False
            or not numeric(sender.get("id"))
            or sender["id"] != chat_id
            or not numeric(message_id)
            or any(
                k in msg
                for k in (
                    "sender_chat",
                    "business_connection_id",
                    "forward_origin",
                    "via_bot",
                    "message_thread_id",
                )
            )
            or not telegram_text_fits(msg.get("text"))
        ):
            return
        if self._record(chat_id,message_id):
            return
        enrollment = re.fullmatch(r"/agentbridge link ([A-Z2-9]{8})", msg["text"].strip())
        if enrollment and "reply_to_message" not in msg:
            with self.db:
                accepted = self.db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,'enrolling',NULL,NULL)",
                    (self.bot_id,chat_id,message_id,"","")).rowcount
            if accepted:
                try:
                    confirmed = self.gateway.confirm_telegram_enrollment(self.bot_id,chat_id,enrollment[1])
                except GatewayRequestError:
                    self._state(chat_id,message_id,"enrollment_unknown")
                    return
                if confirmed:
                    self._send(chat_id,message_id,"身份已确认，请回到原工作台页面完成账号设置。")
                else:
                    self._state(chat_id,message_id,"enrollment_denied")
            return
        if self.allow_pairing and (match:=re.fullmatch(r"/start ([A-Za-z0-9_-]{32})",msg["text"])):
            self._pair(chat_id,message_id,match[1])
            return
        binding, digest = self._binding(chat_id)
        if binding is None:
            return
        text = msg["text"].strip()
        plain = not text.startswith("/") and "reply_to_message" not in msg
        ordinary = plain and public_mirror_text(text) is not None
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,'accepting',NULL,NULL)",
                (self.bot_id, chat_id, message_id, binding["user_subject"], digest),
            ).rowcount
            if inserted and ordinary:
                self.timeline.queue(chat_id, message_id, "user", text)
        if not inserted:
            return
        if not ordinary:
            # Buttons, replies and slash commands never authorize or resume a business task.
            if plain:
                self.deliver(chat_id, message_id, "请在本人工作台的可信界面填写受保护的信息。")
            elif text == "/start":
                self.deliver(
                    chat_id, message_id, "已连接 NanoBot 助手，可以直接提问、查询待办或发起业务办理。"
                )
            elif text == "/status" and isinstance(msg.get("reply_to_message"), dict):
                original = msg["reply_to_message"]
                prior = self.db.execute(
                    """SELECT m.state FROM messages m WHERE m.bot=? AND m.chat=? AND m.binding_hash=?
                       AND (m.receipt=? OR EXISTS (SELECT 1 FROM telegram_reply_parts p WHERE
                         (p.bot,p.chat,p.message)=(m.bot,m.chat,m.message) AND p.receipt=?))""",
                    (self.bot_id, chat_id, digest, original.get("message_id"), original.get("message_id")),
                ).fetchone()
                if prior and (original.get("from") or {}).get("id") == self.bot_id:
                    self.deliver(
                        chat_id, message_id, "原消息投递状态：" + prior["state"]
                    )
                else:
                    self._state(chat_id, message_id, "unrecognized_reply")
            else:
                self._state(chat_id, message_id, "unsupported")
            return
        key = self._key(chat_id, message_id)
        try:
            # The same accepted run continues in the host after this observer closes.
            generator = self.gateway.send_dispatch(
                {"idempotency_key": key}, binding, text, [], None
            )
            try:
                accepted = next(generator)
            finally:
                generator.close()
            if accepted.get("type") != "accepted" or not accepted.get("runId"):
                return
            self._state(chat_id, message_id, "working", run=accepted["runId"])
            self._typing(chat_id)
        except RuntimeError:
            # On failure after admission, lookup is the only permitted next step.
            return

    def _pair(self,chat,message,code):
        # No business dispatch is created. A lost acknowledgement cannot replay
        # this one-time proof; the owner can request a new challenge in Workspace.
        with self.db:
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,'pairing',NULL,NULL)",
                            (self.bot_id,chat,message,"",""))
        try:
            proof=self.gateway.present_telegram_challenge(self.bot_id,chat,code)
        except GatewayRequestError:
            self._state(chat,message,"pairing_unknown")
            return
        if not isinstance(proof,str) or not re.fullmatch(r"[0-9]{8}",proof):
            self._state(chat,message,"pairing_denied")
            return
        self._send(chat,message,"请在本人已登录的 Workspace 中输入本次确认码 "+proof+"。若非本人发起，请忽略。")

    def deliver(self, chat, message, answer, *, ordinary=False):
        row = self._record(chat, message)
        binding, digest = self._binding(chat)
        if binding is None or digest != row["binding_hash"]:
            self._state(chat, message, "binding_changed")
            return
        if ordinary:
            self._send_public_reply(chat, message, answer, digest)
        else:
            self._send(chat, message, answer)

    def _confirmed_send(self, chat, message, answer, *, parse_mode=None, reply_markup=None):
        # Telegram sendMessage has no idempotency key. A lost reply after this
        # durable boundary leaves the receipt unknown and cannot trigger a resend.
        params = {
            "chat_id": chat,
            "text": answer,
            "reply_parameters": {
                "message_id": message,
                "allow_sending_without_reply": False,
            },
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            result = self.api.call("sendMessage", **params)
            if (
                not isinstance(result, dict)
                or not numeric(result.get("message_id"))
                or not isinstance(result.get("chat"), dict)
                or result["chat"].get("id") != chat or result["chat"].get("type") != "private"
                or not isinstance(result.get("from"), dict) or result["from"].get("id") != self.bot_id
                or not isinstance(result.get("reply_to_message"), dict)
                or result["reply_to_message"].get("message_id") != message
            ):
                raise TelegramUnavailable("Reply identity not confirmed")
        except TelegramUnavailable:
            return None
        return result["message_id"]

    def _send(self, chat, message, answer):
        self._state(chat, message, "sending")
        receipt = self._confirmed_send(chat, message, answer)
        self._state(chat, message, "delivered" if receipt else "delivery_unknown", receipt=receipt)

    def _deliver_card_once(self, chat, message, binding, binding_digest, dedupe_key, params):
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO telegram_card_deliveries VALUES(?,?,?,NULL,'sending',NULL)",
                (self.bot_id, chat, dedupe_key),
            ).rowcount
        if not inserted:
            return
        status = "delivery_unknown"
        receipt = None
        try:
            active, digest = self._binding(chat)
            if active != binding or digest != binding_digest:
                status = "binding_changed"
            else:
                receipt = self._confirmed_send(
                    chat,
                    message,
                    params["text"],
                    parse_mode=params.get("parse_mode"),
                    reply_markup=params.get("reply_markup"),
                )
                if receipt is not None:
                    status = "delivered"
        except TelegramUnavailable:
            pass
        with self.db:
            self.db.execute(
                "UPDATE telegram_card_deliveries SET state=?,receipt=? WHERE bot=? AND chat=? AND dedupe_key=?",
                (status, receipt, self.bot_id, chat, dedupe_key),
            )

    def _send_public_reply(self, chat, message, answer, binding_digest):
        key = (self.bot_id, chat, message)
        parts = list(split_telegram_text(answer))
        content_digest = hashlib.sha256(answer.encode()).hexdigest()
        for index, text in enumerate(parts):
            prior = self.db.execute("""SELECT answer_digest FROM telegram_reply_parts
                                    WHERE bot=? AND chat=? AND message=? AND part=?""", (*key, index)).fetchone()
            if prior:
                if prior[0] != content_digest:
                    self._state(chat, message, "delivery_unknown")
                    return
                continue
            if index:
                try:
                    binding, digest = self._binding(chat)
                except TelegramUnavailable:
                    return  # Previous segments are known; a later poll can check this unsent segment.
                if not binding or digest != binding_digest:
                    self._state(chat, message, "binding_changed")
                    return
            formatted = format_ordinary_telegram_message(text)
            self._state(chat, message, "sending")
            receipt = self._confirmed_send(
                chat, message, formatted["text"], parse_mode=formatted.get("parse_mode")
            )
            if not receipt or self.db.execute("SELECT 1 FROM telegram_reply_parts WHERE bot=? AND chat=? AND receipt=?",
                                              (self.bot_id, chat, receipt)).fetchone():
                self._state(chat, message, "delivery_unknown")
                return
            final = index == len(parts) - 1
            self._state(chat, message, "delivered" if final else "working", receipt=receipt if final else None,
                        confirmed_part=(index, content_digest, receipt), public_answer=answer if final else None)

    def _deliver_active_card(self, chat, message, binding, digest, key, run_id):
        try:
            card_resp = self.gateway.private_card({"idempotency_key": key}, binding, run_id)
        except (RuntimeError, GatewayRequestError):
            return
        card = card_resp.get("interaction") if card_resp.get("available") else None
        if not isinstance(card, dict) or not card.get("interactionId"):
            return
        parsed = urlsplit(str((card.get("presentation") or {}).get("url") or ""))
        origins = frozenset({f"https://{parsed.netloc}"}) if parsed.scheme == "https" and parsed.netloc else frozenset()
        rendered = render_interaction_message(card, origins)
        if rendered:
            self._deliver_card_once(chat, message, binding, digest, f"interaction:{card['interactionId']}", rendered)

    def _deliver_result_view(self, chat, message, binding, digest, key, run_id):
        try:
            rv = self.gateway.result_view({"idempotency_key": key}, binding, run_id)
        except (RuntimeError, GatewayRequestError):
            return
        view = rv.get("resultView") if rv.get("available") else None
        if not isinstance(view, dict) or not view.get("sourceTaskId"):
            return
        count = rv.get("count")
        if type(count) is int and count > 0:
            summary = (
                f"已查询，发现 {count} 项 OA 待办，可从下方进入选择与办理。"
                if rv.get("complete")
                else f"已读取 {count} 项 OA 待办（结果尚不完整），可从下方查看或重新查询。"
            )
        else:
            summary = "OA 待办已查询，可从下方进入选择与办理。"
        rendered = render_result_view_message(view, summary=summary)
        self._deliver_card_once(chat, message, binding, digest, f"result_view:{view['sourceTaskId']}", rendered)

    def reconcile(self):
        # Only the single owning consumer calls this. A surviving sending row
        # is from a prior process and may already have reached Telegram.
        with self.db:
            self.db.execute(
                "UPDATE messages SET state='delivery_unknown' WHERE bot=? AND state='sending'",
                (self.bot_id,),
            )
        rows = self.db.execute(
            "SELECT * FROM messages WHERE bot=? AND state IN ('accepting','working') ORDER BY rowid",
            (self.bot_id,),
        ).fetchall()
        for row in rows:
            chat, message = row["chat"], row["message"]
            try:
                binding, digest = self._binding(chat)
            except TelegramUnavailable:
                continue
            if digest != row["binding_hash"] or binding is None:
                self._state(chat, message, "binding_changed")
                continue
            key = self._key(chat, message)
            try:
                evidence = self.gateway.dispatch_evidence(
                    {"idempotency_key": key}, binding, row["run_id"] or key
                )
            except RuntimeError:
                continue
            if not evidence.get("prompt_observed"):
                self._state(chat, message, "admission_unknown")
                continue
            run_id = evidence["observed_run_id"]
            if row["run_id"] and row["run_id"] != run_id:
                self._state(chat, message, "admission_unknown")
                continue
            self._state(chat, message, "working", run=run_id)
            if evidence["state"] == "accepted":
                self._typing(chat)
                if evidence.get("has_private_card"):
                    self._deliver_active_card(chat, message, binding, digest, key, run_id)
            elif evidence["state"] == "completed":
                self.deliver(chat, message, telegram_answer(evidence.get("final_text")), ordinary=True)
                if evidence.get("has_result_view"):
                    self._deliver_result_view(chat, message, binding, digest, key, run_id)
            elif evidence["state"] in {"stopped", "unknown"}:
                self.deliver(chat, message, "原请求已停止或结果待核对。")
        self.timeline.flush()

    def poll_once(self):
        state = self.db.execute(
            "SELECT * FROM channel_meta WHERE bot=?", (self.bot_id,)
        ).fetchone()
        # Telegram can randomize update IDs after a week without updates. Our
        # durable message keys still prevent re-delivery when offset is reset.
        offset = (
            state["next_update"]
            if state and time.time() - state["observed_at"] < 6 * 86400
            else 0
        )
        updates = self.api.call(
            "getUpdates",
            offset=offset,
            timeout=2,
            limit=100,
            allowed_updates=["message"],
        )
        if not isinstance(updates, list):
            raise TelegramUnavailable("Update list invalid")
        can_advance = True
        for update in updates:
            number = update.get("update_id") if isinstance(update, dict) else None
            if type(number) is not int or number < offset:
                continue
            try:
                self.ingest(update)
            except TelegramUnavailable:
                # The failed update remains unacknowledged. Later users in this
                # batch may still finish; durable message keys handle their replay.
                can_advance = False
                continue
            if not can_advance:
                continue
            offset = number + 1
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO channel_meta VALUES (?,?,?)",
                    (self.bot_id, offset, time.time()),
                )
        self.reconcile()
        if self.terminal and time.monotonic() >= self.next_notifications:
            self.next_notifications = time.monotonic() + 2
            rows = self.db.execute("SELECT DISTINCT chat FROM messages WHERE bot=? AND run_id IS NOT NULL", (self.bot_id,))
            recipients = {row["chat"] for row in rows.fetchall()}
            try:
                peers = self.gateway._request("telegram-mirror-peers", {"botId": str(self.bot_id)})
                for sender in peers.get("senderIds", []):
                    if isinstance(sender, str) and re.fullmatch(r"[1-9][0-9]{0,15}", sender) and numeric(int(sender)):
                        recipients.add(int(sender))
            except GatewayRequestError:
                pass
            for chat in sorted(recipients):
                try:
                    route, digest = self._binding(chat)
                    if route:
                        self.terminal.poll(chat, route, digest)
                except (TelegramUnavailable,GatewayRequestError):
                    continue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    filename = parser.parse_args().config
    os.umask(0o077)
    config = json.loads(private_text(filename))
    bot_id, instance = config["botId"], config["instanceId"]
    if not numeric(bot_id) or not isinstance(instance, str) or not instance.strip():
        raise ValueError("Invalid bot or instance identity")
    home = private_directory(Path(config["dataDir"]))
    token = bot_token(config)
    gateway = NanoBotGatewayClient(
        url=config["hostUrl"],
        token_file=Path(config["hostTokenFile"]),
        instance_id=instance,
    )

    def bindings():
        current = json.loads(private_text(filename))
        if any(
            current.get(key) != config.get(key)
            for key in (
                "botId",
                "botUsername",
                "allowWorkspacePairing",
                "instanceId",
                "hostUrl",
                "hostTokenFile",
                "botTokenFile",
                "botCredential",
                "dataDir",
            )
        ):
            return {}
        routes = current["bindings"]
        keys = (
            "user_subject",
            "endpoint_id",
            "endpoint_key",
            "session_key",
            "credential_ref",
        )
        seen = set()
        for uid, route in routes.items():
            if (
                not uid.isascii()
                or not uid.isdecimal()
                or str(int(uid)) != uid
                or not numeric(int(uid))
            ):
                raise ValueError("Invalid bound Telegram user")
            if not isinstance(route, dict) or any(
                not isinstance(route.get(k), str) or not route[k].strip() for k in keys
            ):
                raise ValueError("Incomplete Telegram host binding")
            if (
                route["endpoint_id"] in seen
                or route["endpoint_key"] != f"telegram:{bot_id}:{uid}"
            ):
                raise ValueError(
                    "Telegram endpoint must match the bot and the private sender"
                )
            seen.add(route["endpoint_id"])
        return routes

    bindings()
    with (home / "poll.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        consumer = TelegramPilot(
            home / "messages.sqlite3",
            api=TelegramAPI(token),
            bot_id=bot_id,
            instance=instance,
            gateway=gateway,
            bindings=bindings,
            bot_username=config.get("botUsername"),
            allow_pairing=config.get("allowWorkspacePairing") is True,
        )
        try:
            consumer.verify()
            while True:
                try:
                    consumer.poll_once()
                except TelegramUnavailable:
                    time.sleep(2)
        finally:
            consumer.db.close()


if __name__ == "__main__":
    main()
