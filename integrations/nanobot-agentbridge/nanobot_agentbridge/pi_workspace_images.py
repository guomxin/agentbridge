"""Upload original Workspace image bytes to the currently paired Pi private chat."""

import base64
import hashlib


EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def poll(pilot, chat, binding, digest):
    from .telegram import TelegramUnavailable
    from .telegram_files import document_receipt

    pilot.db.execute("""CREATE TABLE IF NOT EXISTS pi_workspace_image_receipts (
        delivery TEXT PRIMARY KEY, attempt INTEGER NOT NULL, bot INTEGER NOT NULL, chat INTEGER NOT NULL,
        binding TEXT NOT NULL, receipt INTEGER, acknowledged INTEGER NOT NULL DEFAULT 0)""")

    def request(action, **fields):
        return pilot.gateway._request(
            "telegram-workspace-media", {"binding": binding, "action": action, **fields}, timeout=45
        )

    def acknowledge(delivery, attempt, receipt):
        reply = request(
            "ack",
            delivery_id=delivery,
            attempt=attempt,
            **({"receipt": receipt} if receipt is not None else {}),
        )
        if reply.get("recorded") is True:
            with pilot.db:
                pilot.db.execute(
                    "UPDATE pi_workspace_image_receipts SET acknowledged=1 WHERE delivery=?", (delivery,)
                )

    for saved in pilot.db.execute(
        """SELECT * FROM pi_workspace_image_receipts
        WHERE bot=? AND chat=? AND binding=? AND acknowledged=0 ORDER BY rowid LIMIT 4""",
        (pilot.bot_id, chat, digest),
    ).fetchall():
        acknowledge(saved["delivery"], saved["attempt"], saved["receipt"])
    for _ in range(4):
        claim = request("claim")
        if claim.get("available") is not True:
            return
        delivery, attempt, ordinal = (claim.get(k) for k in ("deliveryId", "attempt", "ordinal"))
        if (
            not isinstance(delivery, str)
            or len(delivery) != 36
            or type(attempt) is not int
            or type(ordinal) is not int
            or not 0 <= ordinal < 4
        ):
            return
        target = dict(delivery_id=delivery, attempt=attempt)
        image = request("read", **target)
        mime, size = image.get("contentType"), image.get("byteSize")
        if (
            image.get("available") is not True
            or mime not in EXTENSIONS
            or type(size) is not int
            or not 0 < size <= 6 * 1024 * 1024
        ):
            return
        try:
            data = base64.b64decode(image["contentBase64"], validate=True)
        except (KeyError, TypeError, ValueError):
            return
        if len(data) != size or hashlib.sha256(data).hexdigest() != image.get("sha256"):
            return
        if (
            pilot._binding(chat) != (binding, digest)
            or request("begin", **target).get("available") is not True
        ):
            return
        with pilot.db:
            reserved = pilot.db.execute(
                """INSERT OR IGNORE INTO pi_workspace_image_receipts
                (delivery,attempt,bot,chat,binding) VALUES (?,?,?,?,?)""",
                (delivery, attempt, pilot.bot_id, chat, digest),
            ).rowcount
        if not reserved:
            return
        receipt = None
        try:
            sent = pilot.api.send_document(chat, f"workspace-{ordinal + 1}.{EXTENSIONS[mime]}", mime, data)
            receipt = document_receipt(sent, pilot.bot_id, chat, size)
        except TelegramUnavailable:
            pass
        with pilot.db:
            if (
                receipt is not None
                and pilot.db.execute(
                    "SELECT 1 FROM pi_workspace_image_receipts WHERE bot=? AND chat=? AND receipt=?",
                    (pilot.bot_id, chat, receipt),
                ).fetchone()
            ):
                receipt = None
            pilot.db.execute(
                "UPDATE pi_workspace_image_receipts SET receipt=? WHERE delivery=?", (receipt, delivery)
            )
        acknowledge(delivery, attempt, receipt)
