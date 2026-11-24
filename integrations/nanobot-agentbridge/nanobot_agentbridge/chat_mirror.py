"""Send new, centrally authorized Workspace chat events through the bound private bot."""

import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from agentbridge.core.chat_mirror import public_mirror_text

from .telegram_text import split_telegram_text


def identifier(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def projected_chat(item, workspace_endpoint):
    if not isinstance(item, dict) or not isinstance(item.get("timeline"), dict):
        return None
    line = item["timeline"]
    source = line.get("source")
    if not isinstance(source, dict):
        return None
    role, text = line.get("role"), public_mirror_text(line.get("text"))
    if (item.get("deliveryMode") != "timeline_message" or line.get("mirrorKind") != "nanobot_workspace_text_v1"
            or item.get("requiresRouteCheck") is not True or item.get("interaction") or item.get("task")
            or item.get("event") or item.get("attachments") or line.get("attachments")
            or role not in ("user", "assistant") or text is None or not identifier(line.get("entryId"))
            or source.get("endpointId") != workspace_endpoint or source.get("clientType") not in ("web", "webchat")):
        return None
    return {"entryId": line["entryId"], "role": role, "text": text}


def chat_claim(database, endpoint, delivery, attempt, *, register=False):
    """Only a lease observed for this mirror may be validated or acknowledged here."""
    path = Path(database) / "telegram-mirror-claims.sqlite3"
    if not register and not path.exists():
        return False
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS claims(
            delivery TEXT PRIMARY KEY, endpoint TEXT NOT NULL, attempt INTEGER NOT NULL)""")
        if register:
            db.execute("""INSERT INTO claims VALUES(?,?,?) ON CONFLICT(delivery)
                          DO UPDATE SET attempt=excluded.attempt
                          WHERE endpoint=excluded.endpoint AND attempt<=excluded.attempt""", (delivery, endpoint, attempt))
        return db.execute("SELECT 1 FROM claims WHERE delivery=? AND endpoint=? AND attempt=?",
                          (delivery, endpoint, attempt)).fetchone() is not None


def telegram_parts(role, text):
    prefix = "【网页端 · " + ("你" if role == "user" else "智能体") + "】\n"
    return split_telegram_text(text, prefix=prefix)


class ChatMirrors:
    def __init__(self, pilot):
        self.pilot = pilot
        with pilot.db:
            pilot.db.executescript("""
                CREATE TABLE IF NOT EXISTS chat_mirror_receipts(
                    bot INTEGER, chat INTEGER, delivery TEXT, entry TEXT, digest TEXT NOT NULL,
                    PRIMARY KEY(bot,chat,delivery), UNIQUE(bot,chat,entry));
                CREATE TABLE IF NOT EXISTS chat_mirror_parts(
                    bot INTEGER, chat INTEGER, delivery TEXT, part INTEGER, state TEXT NOT NULL, receipt INTEGER,
                    PRIMARY KEY(bot,chat,delivery,part), UNIQUE(bot,chat,receipt));
                UPDATE chat_mirror_parts SET state='delivery_unknown' WHERE state='sending';
            """)

    def poll(self, chat, binding, binding_digest, event):
        from .telegram import TelegramUnavailable, numeric

        pilot = self.pilot
        entry, delivery, attempt = event.get("entryId"), event.get("deliveryId"), event.get("attempt")
        role, text = event.get("role"), public_mirror_text(event.get("text"))
        if (not identifier(delivery) or not identifier(entry) or type(attempt) is not int or not 1 <= attempt <= 5
                or role not in ("user", "assistant") or text is None
                or binding.get("endpoint_key") != f"telegram:{pilot.bot_id}:{chat}"):
            return
        key = (pilot.bot_id, chat, delivery)
        digest = hashlib.sha256(json.dumps([binding_digest, entry, role, text], ensure_ascii=False).encode()).hexdigest()
        with pilot.db:
            pilot.db.execute("INSERT OR IGNORE INTO chat_mirror_receipts VALUES(?,?,?,?,?)", (*key, entry, digest))
            proof = pilot.db.execute("SELECT digest FROM chat_mirror_receipts WHERE bot=? AND chat=? AND delivery=?", key).fetchone()
        if not proof or proof[0] != digest:
            return
        params = {"binding": binding, "deliveryId": delivery, "attempt": attempt}
        delivered = True
        for part, chunk in enumerate(telegram_parts(role, text)):
            segment = (*key, part)
            prior = pilot.db.execute("SELECT state FROM chat_mirror_parts WHERE bot=? AND chat=? AND delivery=? AND part=?", segment).fetchone()
            if prior:
                if prior[0] == "delivered":
                    continue
                delivered = False
                break
            permission = pilot.gateway._request("telegram-chat-validate", params)
            if permission.get("allowed") is not True:
                return
            with pilot.db:
                inserted = pilot.db.execute("INSERT OR IGNORE INTO chat_mirror_parts VALUES(?,?,?,?,'sending',NULL)", segment).rowcount
            if not inserted:
                return
            from .telegram_presentation import format_ordinary_telegram_message
            formatted = format_ordinary_telegram_message(chunk)
            receipt = None
            try:
                send_params = {"chat_id": chat, "text": formatted["text"], "link_preview_options": {"is_disabled": True}}
                if formatted.get("parse_mode"):
                    send_params["parse_mode"] = formatted["parse_mode"]
                result = pilot.api.call("sendMessage", **send_params)
                if (isinstance(result, dict) and numeric(result.get("message_id"))
                        and isinstance(result.get("chat"), dict) and result["chat"].get("id") == chat
                        and result["chat"].get("type") == "private" and isinstance(result.get("from"), dict)
                        and result["from"].get("id") == pilot.bot_id):
                    receipt = result["message_id"]
            except TelegramUnavailable:
                pass
            with pilot.db:
                duplicate = receipt is not None and pilot.db.execute(
                    "SELECT 1 FROM chat_mirror_parts WHERE bot=? AND chat=? AND receipt=?", (pilot.bot_id, chat, receipt)).fetchone()
                delivered = receipt is not None and not duplicate
                pilot.db.execute("""UPDATE chat_mirror_parts SET state=?,receipt=?
                    WHERE bot=? AND chat=? AND delivery=? AND part=?""",
                    ("delivered" if delivered else "delivery_unknown", receipt if delivered else None, *segment))
            if not delivered:
                break
        pilot.gateway._request("telegram-chat-receipt", {**params, "delivered": bool(delivered)})
