"""Deliver a watched change to its selected live Telegram binding, without model IO."""

from .chat_mirror import identifier
from .telegram_timeline import telegram_text_fits


def deliver_watch(terminal, chat, binding, digest, event):
    from .telegram import TelegramUnavailable, numeric

    pilot = terminal.pilot
    delivery, attempt, run = event.get("deliveryId"), event.get("attempt"), event.get("runId")
    if (not identifier(delivery) or type(attempt) is not int or not 1 <= attempt <= 5
            or not isinstance(run, str) or not run.startswith("watch:") or not identifier(run[6:])
            or binding["endpoint_key"] != f"telegram:{pilot.bot_id}:{chat}"):
        return
    key = (pilot.bot_id, chat, "watch:" + delivery)
    prior = pilot.db.execute("SELECT state FROM telegram_card_deliveries WHERE bot=? AND chat=? AND dedupe_key=?", key).fetchone()
    if prior:
        terminal._ack_receipt(binding, delivery, run, attempt, prior[0] == "delivered")
        return
    active, current_digest = pilot._binding(chat)
    if active != binding or current_digest != digest:
        return
    response = pilot.gateway._request("telegram-watch-validate", {"binding": binding, "deliveryId": delivery,
                                                                 "attempt": attempt, "runId": run})
    if response.get("stale") is True:
        return
    if response.get("allowed") is False:
        terminal._ack_receipt(binding, delivery, run, attempt, True)
        return
    if response.get("allowed") is not True or not telegram_text_fits(response.get("message")):
        return
    # Validation failures above consume no attempt to send. Once the Bot API
    # is invoked its receipt may be unknown, so that delivery must never replay.
    with pilot.db:
        inserted = pilot.db.execute("INSERT OR IGNORE INTO telegram_card_deliveries VALUES(?,?,?,?,'sending',NULL)",
                                    (*key, delivery)).rowcount
    if not inserted:
        return
    receipt = None
    try:
        sent = pilot.api.call("sendMessage", chat_id=chat, text=response["message"], link_preview_options={"is_disabled": True})
        if (isinstance(sent, dict) and numeric(sent.get("message_id")) and (sent.get("from") or {}).get("id") == pilot.bot_id
                and (sent.get("chat") or {}).get("type") == "private" and sent["chat"].get("id") == chat):
            receipt = sent["message_id"]
    except TelegramUnavailable:
        pass
    with pilot.db:
        pilot.db.execute("UPDATE telegram_card_deliveries SET state=?,receipt=? WHERE bot=? AND chat=? AND dedupe_key=?",
                         ("delivered" if receipt else "delivery_unknown", receipt, *key))
    terminal._ack_receipt(binding, delivery, run, attempt, bool(receipt))
