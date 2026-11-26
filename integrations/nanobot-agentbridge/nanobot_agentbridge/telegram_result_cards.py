"""Recoverable Pi result-view reads, separate from ordinary Telegram reply receipts."""

from datetime import datetime
import time

from agentbridge.workspace.gateway import GatewayRequestError


class PiResultCards:
    # Only the owning Bot consumer advances the queue. An interrupted issue may
    # mint another view, but can never repeat the original business operation.
    RETRY_DELAYS = (5, 20, 60)
    WINDOW = 300

    def __init__(self, pilot):
        self.pilot = pilot
        self.db = pilot.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pi_result_card_requests(
                bot INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL,
                run_id TEXT NOT NULL, binding_hash TEXT NOT NULL, source_task TEXT,
                state TEXT NOT NULL, last_error TEXT, attempts INTEGER NOT NULL, next_attempt REAL NOT NULL,
                deadline REAL NOT NULL, PRIMARY KEY(bot,chat,message));
            CREATE INDEX IF NOT EXISTS pi_result_card_due ON pi_result_card_requests(bot,state,next_attempt);
        """)

    def remember(self, chat, message, run_id, digest):
        now = time.time()
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO pi_result_card_requests
                (bot,chat,message,run_id,binding_hash,state,attempts,next_attempt,deadline)
                VALUES(?,?,?,?,?,'pending',0,?,?)""",
                (self.pilot.bot_id, chat, message, run_id, digest, now, now + self.WINDOW),
            )

    def flush_one(self):
        now = time.time()
        # A crash after claiming the last attempt also consumes that attempt.
        with self.db:
            self.db.execute(
                """UPDATE pi_result_card_requests SET state='expired'
                WHERE bot=? AND state='pending' AND deadline<=?""",
                (self.pilot.bot_id, now),
            )
            self.db.execute(
                """UPDATE pi_result_card_requests SET state='exhausted'
                WHERE bot=? AND state='pending' AND attempts>?""",
                (self.pilot.bot_id, len(self.RETRY_DELAYS)),
            )
            row = self.db.execute(
                """SELECT * FROM pi_result_card_requests
                WHERE bot=? AND state='pending' AND next_attempt<=? ORDER BY next_attempt,rowid LIMIT 1""",
                (self.pilot.bot_id, now),
            ).fetchone()
            if not row:
                return
            key = (row["bot"], row["chat"], row["message"])
            delay = self.RETRY_DELAYS[min(row["attempts"], len(self.RETRY_DELAYS) - 1)]
            self.db.execute(
                """UPDATE pi_result_card_requests SET attempts=attempts+1,next_attempt=?
                WHERE bot=? AND chat=? AND message=?""",
                (now + delay, *key),
            )
        from .telegram import TelegramUnavailable

        last_error = None
        try:
            state = self._deliver(row, key)
        except GatewayRequestError as error:
            if error.code in {
                "GATEWAY_CONNECTION_FAILED",
                "GATEWAY_CONNECTION_CLOSED",
                "GATEWAY_RESPONSE_INVALID",
            }:
                state, last_error = "pending", error.code
            else:
                state, last_error = "unavailable", "HOST_REJECTED"
        except TelegramUnavailable:
            # Binding verification failed before entering the Bot send boundary.
            state, last_error = "pending", "BINDING_CHECK_UNAVAILABLE"
        with self.db:
            self.db.execute(
                """UPDATE pi_result_card_requests SET state=?,last_error=?,next_attempt=?
                WHERE bot=? AND chat=? AND message=?""",
                (state, last_error, time.time() + delay, *key),
            )

    def _deliver(self, row, key):
        bot, chat, message = key
        original = self.pilot._record(chat, message)
        if (
            not original
            or original["run_id"] != row["run_id"]
            or original["binding_hash"] != row["binding_hash"]
        ):
            return "binding_changed"
        if (
            row["source_task"]
            and self.db.execute(
                """SELECT 1 FROM telegram_card_deliveries
                WHERE bot=? AND chat=? AND dedupe_key=?""",
                (bot, chat, "result_view:" + row["source_task"]),
            ).fetchone()
        ):
            return "handled"
        binding, digest = self.pilot._binding(chat)
        if not binding or digest != row["binding_hash"]:
            return "binding_changed"
        request = {"idempotency_key": self.pilot._key(chat, message)}
        result = self.pilot.gateway.result_view(request, binding, row["run_id"])
        view = result.get("resultView") if result.get("available") else None
        if (
            not isinstance(view, dict)
            or not isinstance(view.get("sourceTaskId"), str)
            or not view["sourceTaskId"]
        ):
            return "unavailable"
        source = view["sourceTaskId"]
        if row["source_task"] and row["source_task"] != source:
            return "unavailable"
        # Save only the task reference. A crash after sending uses the shared
        # (bot, chat, task) ledger, including unknown and in-flight receipts.
        with self.db:
            self.db.execute(
                "UPDATE pi_result_card_requests SET source_task=? WHERE bot=? AND chat=? AND message=?",
                (source, *key),
            )
        try:
            expires = datetime.fromisoformat(view["sourceExpiresAt"].replace("Z", "+00:00"))
        except (AttributeError, KeyError, TypeError, ValueError):
            return "unavailable"
        if expires.tzinfo is None:
            return "unavailable"
        deadline = min(row["deadline"], expires.timestamp())
        return self.pilot._send_result_view(chat, message, binding, digest, result, deadline=deadline)
