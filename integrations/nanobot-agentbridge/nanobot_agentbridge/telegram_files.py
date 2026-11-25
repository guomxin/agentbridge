"""Upload one authorized source-task document, fencing ambiguous Telegram sends on disk."""

import base64
from pathlib import PurePosixPath


def document_receipt(result, bot, chat, size):
    from .telegram import numeric

    document = result.get("document") if isinstance(result, dict) else None
    sender = result.get("from") if isinstance(result, dict) else None
    destination = result.get("chat") if isinstance(result, dict) else None
    if (
        isinstance(document, dict)
        and isinstance(document.get("file_id"), str)
        and document["file_id"]
        and numeric(result.get("message_id"))
        and isinstance(sender, dict)
        and sender.get("id") == bot
        and isinstance(destination, dict)
        and destination.get("id") == chat
        and destination.get("type") == "private"
        and document.get("file_size", size) == size
    ):
        return result["message_id"]
    return None


def deliver_file(pilot, chat, binding, digest, event):
    from .telegram import TelegramUnavailable

    artifact = event["artifactId"]
    key = (pilot.bot_id, chat, artifact)
    request = {
        "binding": binding,
        "deliveryId": event["deliveryId"],
        "runId": f"artifact:{artifact}",
        "attempt": event["attempt"],
    }

    def acknowledge(delivered, outcome=None):
        pilot.gateway._request(
            "telegram-notification-receipt",
            {
                **request,
                "delivered": delivered,
                **({"artifactOutcome": outcome} if outcome else {}),
            },
        )

    row = pilot.db.execute(
        "SELECT state FROM telegram_artifact_deliveries WHERE bot=? AND chat=? AND artifact=?", key
    ).fetchone()
    if row is None:
        # All network calls before the durable send boundary are idempotent reads.
        response = pilot.gateway._request("telegram-artifact-file", {**request, "withBody": True}, timeout=45)
        file = response.get("file") or {}
        if response.get("allowed") is not True or response.get("artifactId") != artifact:
            acknowledge(False)
            return
        name, mime, size = file.get("filename"), file.get("contentType"), file.get("size")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 240
            or PurePosixPath(name).name != name
            or "\\" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or mime not in {"application/pdf", "text/csv"}
            or type(size) is not int
            or not 0 < size <= 32 * 1024 * 1024
        ):
            acknowledge(False)
            return
        try:
            data = base64.b64decode(file["contentBase64"], validate=True)
        except (KeyError, TypeError, ValueError):
            acknowledge(False)
            return
        if len(data) != size:
            acknowledge(False)
            return
        active, current_digest = pilot._binding(chat)
        if active != binding or current_digest != digest:
            return
        permitted = pilot.gateway._request("telegram-artifact-file", {**request, "withBody": False})
        if permitted.get("allowed") is not True or permitted.get("artifactId") != artifact:
            acknowledge(False)
            return
        with pilot.db:
            reserved = pilot.db.execute(
                "INSERT OR IGNORE INTO telegram_artifact_deliveries(bot,chat,artifact,state) VALUES(?,?,?,'sending')",
                key,
            ).rowcount
        if not reserved:
            return
        receipt = None
        try:
            result = pilot.api.send_document(chat, name, mime, data)
            receipt = document_receipt(result, pilot.bot_id, chat, size)
        except TelegramUnavailable:
            pass
        with pilot.db:
            duplicate = (
                receipt
                and pilot.db.execute(
                    "SELECT 1 FROM telegram_artifact_deliveries WHERE bot=? AND chat=? AND receipt=?",
                    (pilot.bot_id, chat, receipt),
                ).fetchone()
            )
            state = "attachment_sent" if receipt and not duplicate else "delivery_unknown"
            pilot.db.execute(
                "UPDATE telegram_artifact_deliveries SET state=?,receipt=? WHERE bot=? AND chat=? AND artifact=?",
                (state, receipt if state == "attachment_sent" else None, *key),
            )
    else:
        state = row["state"]
    if state != "sending":
        acknowledge(state == "attachment_sent", state)
