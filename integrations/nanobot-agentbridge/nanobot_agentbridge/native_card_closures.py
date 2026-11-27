"""Prospective native card watches, positively receipted once-only safe closes."""
import json
import time

from agentbridge.core.telegram_interaction_closures import LABELS, STATES, text
from agentbridge.workspace.gateway import GatewayRequestError
from .chat_mirror import identifier


class NativeCardClosures:
    def __init__(self, pilot):
        self.pilot = pilot
        self.next_poll = 0.0
        with pilot.db:
            pilot.db.executescript("""
                CREATE TABLE IF NOT EXISTS telegram_native_card_watches(
                    bot INTEGER,chat INTEGER,card_key TEXT,digest TEXT NOT NULL,source_json TEXT NOT NULL,
                    checked_at REAL NOT NULL,PRIMARY KEY(bot,chat,card_key));
                CREATE TABLE IF NOT EXISTS telegram_native_card_closures(
                    bot INTEGER,chat INTEGER,card_key TEXT,closed_state TEXT NOT NULL,state TEXT NOT NULL,
                    receipt INTEGER,attempt INTEGER NOT NULL,not_before REAL NOT NULL,PRIMARY KEY(bot,chat,card_key));
                UPDATE telegram_native_card_closures SET state='delivery_unknown' WHERE state='sending';
            """)

    def arm(self, chat, digest, key, source):
        # Only called in the transaction that wins a NEW original send reservation.
        if (not isinstance(source, dict) or set(source) != {"sourceRef", "interactionId", "type", "taskId", "runId"}
                or not identifier(source.get("sourceRef")) or source.get("type") not in LABELS
                or key != "interaction:" + str(source.get("interactionId"))):
            return
        now = time.time()
        self.pilot.db.execute("INSERT OR IGNORE INTO telegram_native_card_watches VALUES(?,?,?,?,?,?)",
            (self.pilot.bot_id,chat,key,digest,json.dumps(source, sort_keys=True),now))

    def poll(self):
        from .telegram import TelegramRejected, TelegramUnavailable, numeric
        pilot = self.pilot
        now = time.time()
        if now < self.next_poll:
            return
        self.next_poll = now + 2
        rows = pilot.db.execute("""SELECT w.*,d.receipt AS original_receipt FROM telegram_native_card_watches w
            JOIN telegram_card_deliveries d ON (d.bot,d.chat,d.dedupe_key)=(w.bot,w.chat,w.card_key)
            WHERE w.bot=? AND d.state='delivered' AND d.receipt>0
            AND NOT EXISTS(SELECT 1 FROM telegram_card_deliveries other
                WHERE other.bot=d.bot AND other.chat=d.chat AND other.receipt=d.receipt
                  AND other.dedupe_key!=d.dedupe_key)
            AND NOT EXISTS(SELECT 1 FROM telegram_native_card_closures c
                WHERE (c.bot,c.chat,c.card_key)=(w.bot,w.chat,w.card_key)
                AND (c.state!='retry_wait' OR c.not_before>? OR c.attempt>=5))
            ORDER BY w.checked_at,w.rowid LIMIT 8""", (pilot.bot_id,now)).fetchall()
        for row in rows:
            key = (pilot.bot_id,row["chat"],row["card_key"])
            with pilot.db:
                pilot.db.execute("UPDATE telegram_native_card_watches SET checked_at=? WHERE bot=? AND chat=? AND card_key=?",
                                 (now,*key))
            try:
                binding, digest = pilot._binding(row["chat"])
                if not binding or digest != row["digest"] or not numeric(row["original_receipt"]):
                    continue
                source = json.loads(row["source_json"])
                result = pilot.gateway._request("telegram-task", {"binding": binding, "operation": "card_state", "source": source})
                state = result.get("state")
                if result.get("source") != source or state not in STATES:
                    continue
                if pilot._binding(row["chat"]) != (binding,digest):
                    continue
                with pilot.db:
                    inserted = pilot.db.execute("INSERT OR IGNORE INTO telegram_native_card_closures VALUES(?,?,?,?,'sending',NULL,1,0)",
                                                (*key,state)).rowcount
                    if not inserted:
                        inserted = pilot.db.execute("""UPDATE telegram_native_card_closures SET state='sending',attempt=attempt+1
                            WHERE bot=? AND chat=? AND card_key=? AND closed_state=? AND state='retry_wait'
                              AND not_before<=? AND attempt<5""", (*key,state,now)).rowcount
                if not inserted:
                    continue
                receipt, retry_at = None, 0
                outcome = "delivery_unknown"
                try:
                    receipt = pilot._confirmed_send(row["chat"],row["original_receipt"],text(source["type"],state),
                                                    preserve_rejections=True)
                    reused = receipt and pilot.db.execute("""SELECT 1 FROM telegram_card_deliveries
                        WHERE bot=? AND chat=? AND receipt=? UNION ALL SELECT 1 FROM telegram_native_card_closures
                        WHERE bot=? AND chat=? AND receipt=? LIMIT 1""",
                        (pilot.bot_id,row["chat"],receipt,pilot.bot_id,row["chat"],receipt)).fetchone()
                    if reused:
                        receipt = None
                    if receipt:
                        outcome = "delivered"
                except TelegramRejected as error:
                    outcome = "rejected"
                    if error.retry_after is not None:
                        outcome, retry_at = "retry_wait", time.time() + error.retry_after
                with pilot.db:
                    pilot.db.execute("UPDATE telegram_native_card_closures SET state=?,receipt=?,not_before=? WHERE bot=? AND chat=? AND card_key=?",
                                     (outcome,receipt,retry_at,*key))
            except (TelegramUnavailable, GatewayRequestError):
                # A persisted sending state is never retried, including after restart.
                continue
