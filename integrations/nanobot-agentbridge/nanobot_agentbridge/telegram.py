"""Private-chat Telegram Bot API consumer for a single reviewed host instance.

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
import socket
import sqlite3
import threading
import time
from functools import partial
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
from . import telegram_media
from .telegram_text import split_telegram_text
from .telegram_navigation import TelegramNavigation, command as navigation_command
from .telegram_timeline import TelegramTimeline, telegram_answer, telegram_text_fits


def numeric(value):
    return type(value) is int and 0 < value < 2**53


class TelegramUnavailable(RuntimeError):
    """The request or its acknowledgement failed. Never report the Bot API URL."""


class TelegramRejected(TelegramUnavailable):
    """An authenticated Bot API response explicitly refused this request."""

    def __init__(self, error_code, retry_after=None):
        super().__init__("Telegram rejected the request")
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramAPI:
    _SAFE_RECONNECT_METHODS = frozenset({"getMe", "getWebhookInfo", "getUpdates", "getFile", "sendChatAction"})

    def __init__(self, token, connection_factory=None, *, private_sender=None):
        if (
            not isinstance(token, str)
            or not token
            or not all(c.isascii() and (c.isalnum() or c in "_-:") for c in token)
        ):
            raise ValueError("Invalid Telegram credential")
        self._token = token
        self._private_sender = private_sender
        self._connect = connection_factory or (
            lambda: http.client.HTTPSConnection("api.telegram.org", timeout=8)
        )
        self._lock = threading.Lock()
        self._conn = None
        self._last_used = 0.0
        self._updates_stopped = False

    def interrupt_updates(self):
        # Only the dedicated getUpdates transport calls this. Its reader holds
        # _lock while waiting, so shutdown the socket before joining the worker.
        self._updates_stopped = True
        stream = getattr(self._conn, "sock", None)
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _close_conn(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._close_conn()

    def call(self, method, **params):
        payload = json.dumps(params).encode()
        headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
        return self._post(method, payload, headers, chat=params.get("chat_id"))

    def send_document(self, chat, filename, content_type, data):
        import httpx
        upload = httpx.Request("POST", "https://api.telegram.org",
                               data={"chat_id": str(chat)}, files={"document": (filename, data, content_type)})
        return self._post("sendDocument", upload.read(), {"Content-Type": upload.headers["Content-Type"],
                                                         "Connection": "keep-alive"}, chat=chat)

    def _post(self, method, payload, headers, *, chat=None):
        path = f"/bot{self._token}/{method}"
        with self._lock:
            for attempt in range(2):
                if method == "getUpdates" and self._updates_stopped:
                    raise TelegramUnavailable("Update reader stopped")
                if (self._private_sender is not None and method not in {"getMe", "getWebhookInfo", "getUpdates", "getFile"}
                        and not self._private_sender(chat)):
                    raise TelegramUnavailable("Private channel destination is no longer authorized")
                # Avoid writing a non-idempotent sendMessage onto a socket nearing server idle close.
                if (
                    self._conn is not None
                    and method not in self._SAFE_RECONNECT_METHODS
                    and (time.monotonic() - self._last_used) >= 40.0
                ):
                    self._close_conn()
                reused = self._conn is not None
                connection = self._conn if reused else self._connect()
                self._conn = connection
                try:
                    connection.request("POST", path, payload, headers)
                    response = connection.getresponse()
                    raw = response.read(1_048_577)
                    will_close = getattr(response, "will_close", True)
                    if will_close:
                        self._close_conn()
                    else:
                        self._last_used = time.monotonic()
                    if len(raw) > 1_048_576:
                        raise TelegramUnavailable("Telegram request was not confirmed")
                    data = json.loads(raw)
                    # Only a complete, consistent Bot API rejection proves that no
                    # message was accepted. HTTP errors or broken receipts alone do not.
                    if (isinstance(data, dict) and data.get("ok") is False
                            and type(data.get("error_code")) is int
                            and 400 <= data["error_code"] < 500
                            and response.status == data["error_code"]):
                        parameters = data.get("parameters")
                        delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
                        delay = delay if type(delay) is int and 1 <= delay <= 86400 else None
                        raise TelegramRejected(data["error_code"], delay)
                    if response.status != 200:
                        raise TelegramUnavailable("Telegram request was not confirmed")
                    if (
                        not isinstance(data, dict)
                        or data.get("ok") is not True
                        or "result" not in data
                    ):
                        raise TelegramUnavailable("Invalid Telegram receipt")
                    return data["result"]
                except (
                    http.client.RemoteDisconnected,
                    http.client.CannotSendRequest,
                    ConnectionResetError,
                    BrokenPipeError,
                ):
                    self._close_conn()
                    if attempt == 0 and reused and method in self._SAFE_RECONNECT_METHODS:
                        continue
                    raise TelegramUnavailable(
                        "Telegram request could not be reconciled"
                    ) from None
                except (OSError, ValueError, http.client.HTTPException):
                    self._close_conn()
                    raise TelegramUnavailable(
                        "Telegram request could not be reconciled"
                    ) from None


class TelegramPilot:
    def __init__(
        self, database, *, api, bot_id, instance, gateway, bindings, bot_username=None,
        allow_pairing=False, sender_subject=None, update_reader=None
    ):
        self.api, self.bot_id, self.instance = api, bot_id, instance
        self.gateway, self.bindings = gateway, bindings
        self.host_type = getattr(gateway, "host_type", "nanobot")
        if self.host_type not in {"nanobot", "pi"} or (self.host_type == "pi" and not callable(sender_subject)):
            raise ValueError("Pi Telegram requires a reviewed private sender")
        if update_reader is not None and self.host_type != "pi":
            raise ValueError("Independent update reads require the Pi channel")
        self.update_reader = update_reader
        self.sender_subject = sender_subject
        self.bot_username = bot_username
        self.allow_pairing = allow_pairing
        self.next_notifications = 0.0
        self.next_workspace_images = 0.0
        self._typing_at: dict[int, float] = {}
        self._paired_bindings: dict[int, dict] = {}
        self._mirror_peers_cache: list[int] = []
        self._mirror_peers_at: float = 0.0
        self._mirror_peers_interval: float = 10.0
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

        self.result_cards = None
        if self.host_type == "pi":
            from .telegram_result_cards import PiResultCards
            self.result_cards = PiResultCards(self)

        self.navigation = TelegramNavigation(self)
        # No host admission or bot send has started while in reading_media.
        with self.db:
            self.db.execute("DELETE FROM messages WHERE bot=? AND state='reading_media'", (self.bot_id,))
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
        if self.sender_subject is not None and self.sender_subject(chat) is None:
            return None, None
        static_binding = self.bindings().get(str(chat))
        binding = static_binding
        used_cached_pair = False
        try:
            if not binding and self.allow_pairing:
                cached = self._paired_bindings.get(chat)
                if cached is not None:
                    binding = cached
                    used_cached_pair = True
                else:
                    binding = self.gateway.resolve_telegram_sender(self.bot_id, chat)
                    if binding:
                        self._paired_bindings[chat] = binding
            if not binding:
                return None, None
            try:
                verified = self.gateway.verify_telegram_binding(binding)
            except GatewayRequestError as exc:
                if exc.code == self.host_type.upper() + "_BINDING_UNAVAILABLE" and used_cached_pair:
                    # Host process may have restarted and cleared in-memory routes; re-resolve once.
                    self._paired_bindings.pop(chat, None)
                    binding = self.gateway.resolve_telegram_sender(self.bot_id, chat)
                    if not binding:
                        return None, None
                    self._paired_bindings[chat] = binding
                    verified = self.gateway.verify_telegram_binding(binding)
                else:
                    raise
            if not verified:
                self._paired_bindings.pop(chat, None)
                if used_cached_pair:
                    binding = self.gateway.resolve_telegram_sender(self.bot_id, chat)
                    if not binding:
                        return None, None
                    if not self.gateway.verify_telegram_binding(binding):
                        return None, None
                    self._paired_bindings[chat] = binding
                else:
                    return None, None
        except GatewayRequestError as exc:
            if exc.code == self.host_type.upper() + "_BINDING_UNAVAILABLE":
                self._paired_bindings.pop(chat, None)
                return None, None
            raise TelegramUnavailable(
                "Central Telegram identity temporarily unavailable"
            ) from None
        if self.sender_subject is not None and binding.get("user_subject") != self.sender_subject(chat):
            self._paired_bindings.pop(chat, None)
            return None, None
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
            or (not any(k in msg for k in telegram_media.MEDIA_FIELDS) and not telegram_text_fits(msg.get("text")))
        ):
            return
        if self.sender_subject is not None and self.sender_subject(chat_id) is None:
            return
        if self._record(chat_id,message_id):
            return
        if any(k in msg for k in telegram_media.MEDIA_FIELDS):
            telegram_media.receive(self, msg, chat_id, message_id)
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
                    self._mirror_peers_at = 0.0
                    self._send(chat_id,message_id,"身份已确认，请回到原工作台页面完成账号设置。")
                else:
                    self._state(chat_id,message_id,"enrollment_denied")
            return
        if self.allow_pairing and (match:=re.fullmatch(r"/start ([A-Za-z0-9_-]{32})",msg["text"])):
            self._mirror_peers_at = 0.0
            self._pair(chat_id,message_id,match[1])
            return
        binding, digest = self._binding(chat_id)
        if binding is None:
            return
        text = msg["text"].strip()
        navigation = navigation_command(text, msg.get("reply_to_message"))
        plain = not navigation and not text.startswith("/") and "reply_to_message" not in msg
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
            if navigation:
                self.navigation.handle(chat_id, message_id, binding, digest, msg.get("reply_to_message"), *navigation)
            elif plain:
                self.deliver(chat_id, message_id, "请在本人工作台的可信界面填写受保护的信息。")
            elif self.host_type == "pi" and text in {"/agentbridge", "/agentbridge status"}:
                status = self.gateway.readiness()
                state = "已就绪" if status.get("available") is True else "暂不可用"
                self.deliver(chat_id, message_id, f"Pi 私聊已绑定，宿主{state}。使用 /pending 查看本人待处理任务。")
            elif text == "/start":
                name = "Pi" if self.host_type == "pi" else "NanoBot"
                self.deliver(
                    chat_id, message_id, f"已连接 {name} 助手，可以直接提问、查询待办或发起业务办理。用 /pending 找回原卡，回复原消息并发送“继续”或“停止”可处理原任务。"
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
        self._dispatch_ordinary(chat_id, message_id, binding, text, [])

    def _dispatch_ordinary(self, chat_id, message_id, binding, text, attachments):
        key = self._key(chat_id, message_id)
        try:
            # The same accepted run continues in the host after this observer closes.
            generator = self.gateway.send_dispatch(
                {"idempotency_key": key}, binding, text, attachments, None
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
        if self.sender_subject is not None and self.sender_subject(chat) is None:
            return None
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

    def _deliver_card_once(self, chat, message, binding, binding_digest, dedupe_key, params, *, result_deadline=None):
        if result_deadline is not None:
            # A slow or failed authority check is still before the durable Bot send boundary.
            if self._binding(chat) != (binding, binding_digest):
                return "binding_changed"
            if time.time() >= result_deadline:
                return "expired"
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO telegram_card_deliveries VALUES(?,?,?,NULL,'sending',NULL)",
                (self.bot_id, chat, dedupe_key),
            ).rowcount
        if not inserted:
            return "handled"
        status = "delivery_unknown"
        receipt = None
        try:
            if result_deadline is None and self._binding(chat) != (binding, binding_digest):
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
            original = self._record(chat, message)
            if original and original["run_id"]:
                target = {"kind": "run", "id": original["run_id"], "requestKey": self._key(chat, message)}
                if dedupe_key.startswith("result_view:"):
                    target = {"kind": "result", "id": dedupe_key.split(":", 1)[1]}
                self.navigation.remember(chat, receipt, binding_digest, target)
            self.db.execute(
                "UPDATE telegram_card_deliveries SET state=?,receipt=? WHERE bot=? AND chat=? AND dedupe_key=?",
                (status, receipt, self.bot_id, chat, dedupe_key),
            )
        return "handled"

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
        self._send_result_view(chat, message, binding, digest, rv)

    def _send_result_view(self, chat, message, binding, digest, rv, *, deadline=None):
        view = rv["resultView"]
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
        return self._deliver_card_once(chat, message, binding, digest, f"result_view:{view['sourceTaskId']}",
                                       rendered, result_deadline=deadline)

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
            has_view = evidence.get("has_result_view")
            if self.result_cards and has_view and evidence["state"] in {"accepted", "completed"}:
                self.result_cards.remember(chat, message, run_id, digest)
            if evidence["state"] == "accepted":
                self._typing(chat)
                if evidence.get("has_private_card"):
                    self._deliver_active_card(chat, message, binding, digest, key, run_id)
                if has_view and not self.result_cards:
                    self._deliver_result_view(chat, message, binding, digest, key, run_id)
            elif evidence["state"] == "completed":
                self.deliver(chat, message, telegram_answer(evidence.get("final_text")), ordinary=True)
                if has_view and not self.result_cards:
                    self._deliver_result_view(chat, message, binding, digest, key, run_id)
                if self.terminal:
                    self.terminal.flush_deferred(chat=chat, run_id=run_id)
            elif evidence["state"] in {"stopped", "unknown"}:
                self.deliver(chat, message, "原请求已停止或结果待核对。")
        if self.result_cards:
            self.result_cards.flush_one()
        if self.terminal:
            self.terminal.flush_deferred()
        self.timeline.flush()

    def _discover_mirror_peers(self) -> list[int]:
        now = time.monotonic()
        if self._mirror_peers_at > 0.0 and (now - self._mirror_peers_at) < self._mirror_peers_interval:
            return list(self._mirror_peers_cache)
        try:
            peers = self.gateway._request("telegram-mirror-peers", {"botId": str(self.bot_id)})
            discovered = []
            for sender in peers.get("senderIds", []):
                if isinstance(sender, str) and re.fullmatch(r"[1-9][0-9]{0,15}", sender) and numeric(int(sender)):
                    discovered.append(int(sender))
            self._mirror_peers_cache = discovered
            self._mirror_peers_at = now
        except GatewayRequestError:
            pass
        return list(self._mirror_peers_cache)

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
        has_active = bool(
            self.db.execute(
                "SELECT 1 FROM messages WHERE bot=? AND state IN ('accepting','working') LIMIT 1",
                (self.bot_id,),
            ).fetchone()
        )
        # The next read is submitted only on a later owner tick, after the
        # previous page has been processed and its durable offset committed.
        fetch = self.update_reader.poll if self.update_reader is not None else partial(self.api.call, "getUpdates")
        updates = fetch(
            offset=offset,
            timeout=0 if has_active else 1,
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
            active_now = bool(
                self.db.execute(
                    "SELECT 1 FROM messages WHERE bot=? AND state IN ('accepting','working') LIMIT 1",
                    (self.bot_id,),
                ).fetchone()
            )
            self.next_notifications = time.monotonic() + (0.4 if active_now else 0.5)
            rows = self.db.execute("SELECT DISTINCT chat FROM messages WHERE bot=? AND run_id IS NOT NULL", (self.bot_id,))
            recipients = {row["chat"] for row in rows.fetchall()}
            recipients.update(self._discover_mirror_peers())
            sorted_recipients = sorted(recipients)
            use_long_poll = (
                not active_now
                and len(sorted_recipients) == 1
                and isinstance(self.gateway, NanoBotGatewayClient)
            )
            for chat in sorted_recipients:
                try:
                    route, digest = self._binding(chat)
                except (TelegramUnavailable, GatewayRequestError):
                    continue
                if not route:
                    continue
                try:
                    self.terminal.poll(chat, route, digest, wait_seconds=1 if use_long_poll else 0)
                except (TelegramUnavailable, GatewayRequestError):
                    pass
                if self.host_type == "pi" and time.monotonic() >= self.next_workspace_images:
                    from .pi_workspace_images import poll as poll_workspace_images
                    self.next_workspace_images = time.monotonic() + 2
                    try:
                        poll_workspace_images(self, chat, route, digest)
                    except (TelegramUnavailable, GatewayRequestError):
                        pass


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
    host_type = config.get("hostType", "nanobot")
    gateway_args = dict(url=config["hostUrl"], token_file=Path(config["hostTokenFile"]), instance_id=instance)
    sender_subject = None
    if host_type == "nanobot":
        gateway = NanoBotGatewayClient(**gateway_args)
    elif host_type == "pi":
        from agentbridge.workspace.nanobot_gateway import PiGatewayClient
        from .pi_channel import private_sender_policy

        sender_subject = private_sender_policy(filename, config)
        gateway = PiGatewayClient(**gateway_args, adapter_version=config["adapterVersion"])
        if gateway.readiness()["available"] is not True:
            raise ValueError("Pi host identity or readiness did not match")
    else:
        raise ValueError("Unknown Telegram host")

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
            api=TelegramAPI(token, private_sender=sender_subject),
            bot_id=bot_id,
            instance=instance,
            gateway=gateway,
            bindings=bindings,
            bot_username=config.get("botUsername"),
            allow_pairing=config.get("allowWorkspacePairing") is True, sender_subject=sender_subject,
        )
        try:
            consumer.verify()
            if host_type == "pi":
                from .pi_telegram_updates import PiUpdateReader
                # getUpdates holds a TLS connection for the whole poll. Keep all
                # sends on the original checked connection and ledger-owning thread.
                inbound_api = TelegramAPI(token, private_sender=lambda _: False)
                consumer.update_reader = PiUpdateReader(partial(inbound_api.call, "getUpdates"),
                                                        interrupt=inbound_api.interrupt_updates)
            while True:
                started = time.monotonic()
                try:
                    consumer.poll_once()
                except TelegramUnavailable:
                    time.sleep(2)
                    continue
                elapsed = time.monotonic() - started
                if elapsed < 0.25:
                    time.sleep(0.25 - elapsed)
        finally:
            if consumer.update_reader is not None:
                consumer.update_reader.close()
                inbound_api.close()
            consumer.api.close()
            consumer.db.close()


if __name__ == "__main__":
    main()
