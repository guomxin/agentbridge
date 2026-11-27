"""Durable original Telegram replies. Definite rejections and unknown sends stay distinct."""

import hashlib
import json
import time

from .telegram_presentation import format_ordinary_telegram_message

FORMAT = "telegram-public-text-v2"
MAX_ATTEMPTS = 5
LIFETIME_SECONDS = 3600


class DirectReplies:
    def __init__(self, pilot):
        self.pilot, self.db = pilot, pilot.db
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_direct_replies(
                    bot INTEGER NOT NULL,chat INTEGER NOT NULL,message INTEGER NOT NULL,binding_hash TEXT NOT NULL,
                    digest TEXT,answer TEXT,format_version TEXT NOT NULL,parts_json TEXT,expires_at REAL,
                    state TEXT NOT NULL DEFAULT 'awaiting_answer',reason TEXT,
                    PRIMARY KEY(bot,chat,message));
                CREATE TABLE IF NOT EXISTS telegram_direct_reply_attempts(
                    bot INTEGER NOT NULL,chat INTEGER NOT NULL,message INTEGER NOT NULL,part INTEGER NOT NULL,
                    state TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,not_before REAL NOT NULL DEFAULT 0,
                    receipt INTEGER,PRIMARY KEY(bot,chat,message,part),UNIQUE(bot,chat,receipt));
                UPDATE telegram_direct_reply_attempts SET state='delivery_unknown' WHERE state='sending';
            """)

    def admit(self, chat, message, binding_hash):
        # Same transaction as new inbound admission. Existing messages never
        # acquire this protocol merely because a newer consumer starts.
        self.db.execute("""INSERT OR IGNORE INTO telegram_direct_replies(bot,chat,message,binding_hash,format_version)
            VALUES(?,?,?,?,?)""", (self.pilot.bot_id, chat, message, binding_hash, FORMAT))

    def _row(self, chat, message):
        return self.db.execute("SELECT * FROM telegram_direct_replies WHERE bot=? AND chat=? AND message=?",
                               (self.pilot.bot_id, chat, message)).fetchone()

    def _finish(self, key, state, reason=None):
        with self.db:
            self.db.execute("UPDATE telegram_direct_replies SET state=?,reason=? WHERE bot=? AND chat=? AND message=?",
                            (state, reason, *key))
        target = "binding_changed" if state == "revoked" else state
        self.pilot._state(key[1], key[2], target)

    def send(self, chat, message, answer, binding_hash):
        from agentbridge.core.chat_mirror import PUBLIC_TEXT_MAX_CHARACTERS, public_mirror_text, split_public_text
        from .telegram import TelegramRejected, TelegramUnavailable

        row = self._row(chat, message)
        if not row:
            return False
        key = (self.pilot.bot_id, chat, message)
        if row["binding_hash"] != binding_hash:
            self._finish(key, "revoked", "binding_changed")
            return True
        if row["state"] in {"delivered", "delivery_unknown", "rejected", "revoked", "expired"}:
            return True
        public = public_mirror_text(answer, maximum=PUBLIC_TEXT_MAX_CHARACTERS)
        if public is None:
            self._finish(key, "rejected", "private_or_oversized_answer")
            return True
        digest = hashlib.sha256(public.encode()).hexdigest()
        if row["digest"] is None:
            parts = [format_ordinary_telegram_message(part) for part in split_public_text(public)]
            with self.db:
                self.db.execute("""UPDATE telegram_direct_replies SET digest=?,answer=?,parts_json=?,expires_at=?,state='working'
                    WHERE bot=? AND chat=? AND message=? AND digest IS NULL""",
                    (digest, public, json.dumps(parts, ensure_ascii=False), time.time() + LIFETIME_SECONDS, *key))
                self.db.executemany("""INSERT OR IGNORE INTO telegram_direct_reply_attempts(bot,chat,message,part,state)
                    VALUES(?,?,?,?,'pending')""", [(*key, part) for part in range(len(parts))])
            row = self._row(chat, message)
        if row["digest"] != digest:
            self._finish(key, "rejected", "source_content_changed")
            return True
        parts = json.loads(row["parts_json"])
        for index, formatted in enumerate(parts):
            part = self.db.execute("""SELECT * FROM telegram_direct_reply_attempts
                WHERE bot=? AND chat=? AND message=? AND part=?""", (*key, index)).fetchone()
            if part["state"] == "delivered":
                continue
            if part["state"] == "delivery_unknown":
                self._finish(key, "delivery_unknown", "platform_receipt_missing")
                return True
            if part["state"] == "rejected":
                self._finish(key, "rejected", "platform_rejected")
                return True
            if time.time() >= row["expires_at"]:
                self._finish(key, "expired", "send_window_expired")
                return True
            if part["not_before"] > time.time():
                return True
            try:
                active, current = self.pilot._binding(chat)
            except TelegramUnavailable:
                return True  # No reservation and no Bot call.
            if not active or current != binding_hash:
                self._finish(key, "revoked", "binding_changed")
                return True
            with self.db:
                changed = self.db.execute("""UPDATE telegram_direct_reply_attempts SET state='sending',attempt=attempt+1
                    WHERE bot=? AND chat=? AND message=? AND part=? AND state IN ('pending','retry_wait') AND attempt<?""",
                    (*key, index, MAX_ATTEMPTS)).rowcount
            if not changed:
                self._finish(key, "rejected", "send_attempt_budget_exhausted")
                return True
            try:
                receipt = self.pilot._confirmed_send(chat, message, formatted["text"],
                    parse_mode=formatted.get("parse_mode"), preserve_rejections=True)
            except TelegramRejected as error:
                retry = error.error_code == 429 and error.retry_after is not None and part["attempt"] + 1 < MAX_ATTEMPTS
                next_attempt = time.time() + error.retry_after if retry else 0
                retry = retry and next_attempt < row["expires_at"]
                with self.db:
                    self.db.execute("""UPDATE telegram_direct_reply_attempts SET state=?,not_before=?
                        WHERE bot=? AND chat=? AND message=? AND part=?""",
                        ("retry_wait" if retry else "rejected", next_attempt, *key, index))
                if not retry:
                    self._finish(key, "rejected", "platform_rejected_or_retry_budget")
                return True
            duplicate = receipt is not None and self.db.execute("""SELECT 1 FROM telegram_reply_parts
                WHERE bot=? AND chat=? AND receipt=?""", (key[0], chat, receipt)).fetchone()
            if not receipt or duplicate:
                with self.db:
                    self.db.execute("""UPDATE telegram_direct_reply_attempts SET state='delivery_unknown'
                        WHERE bot=? AND chat=? AND message=? AND part=?""", (*key, index))
                self._finish(key, "delivery_unknown", "platform_receipt_missing_or_duplicated")
                return True
            with self.db:
                self.db.execute("""UPDATE telegram_direct_reply_attempts SET state='delivered',receipt=?
                    WHERE bot=? AND chat=? AND message=? AND part=?""", (receipt, *key, index))
                self.db.execute("INSERT INTO telegram_reply_parts VALUES(?,?,?,?,?,?)", (*key, index, digest, receipt))
        receipt = self.db.execute("""SELECT receipt FROM telegram_direct_reply_attempts
            WHERE bot=? AND chat=? AND message=? ORDER BY part DESC LIMIT 1""", key).fetchone()[0]
        with self.db:
            self.db.execute("UPDATE telegram_direct_replies SET state='delivered',reason=NULL WHERE bot=? AND chat=? AND message=?", key)
            self.pilot._state(chat, message, "delivered", receipt=receipt)
        return True
