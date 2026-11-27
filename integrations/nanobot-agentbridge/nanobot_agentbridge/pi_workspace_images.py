"""Upload original Workspace image bytes to the currently paired Pi private chat."""

import base64
import hashlib
import json
import re
import time


EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
BINDING_FIELDS = {'user_subject', 'endpoint_id', 'endpoint_key', 'session_key', 'credential_ref'}


def initialize(db):
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS pi_workspace_image_receipts (
            delivery TEXT PRIMARY KEY, attempt INTEGER NOT NULL, bot INTEGER NOT NULL, chat INTEGER NOT NULL,
            binding TEXT NOT NULL, receipt INTEGER, acknowledged INTEGER NOT NULL DEFAULT 0,
            binding_json TEXT, pairing_ref TEXT, next_ack REAL NOT NULL DEFAULT 0)""")
        columns = {row[1] for row in db.execute('PRAGMA table_info(pi_workspace_image_receipts)')}
        for name, definition in (('binding_json','TEXT'), ('pairing_ref','TEXT'), ('next_ack','REAL NOT NULL DEFAULT 0')):
            if name not in columns:
                db.execute(f'ALTER TABLE pi_workspace_image_receipts ADD COLUMN {name} {definition}')


def _acknowledge(pilot, row):
    binding = json.loads(row['binding_json'])
    digest = hashlib.sha256(json.dumps([pilot.instance,pilot.bot_id,row['chat'],binding],
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if (not isinstance(binding, dict) or set(binding) != BINDING_FIELDS or digest != row['binding']
            or binding.get('credential_ref') != 'tg-'+str(row['pairing_ref'])
            or not re.fullmatch(r'[0-9a-f]{64}', str(row['pairing_ref']))):
        return
    with pilot.db:
        pilot.db.execute('UPDATE pi_workspace_image_receipts SET next_ack=? WHERE delivery=?',
                         (time.time()+5,row['delivery']))
    reply = pilot.gateway._request('telegram-workspace-media', {'binding':binding, 'action':'ack',
        'delivery_id':row['delivery'], 'attempt':row['attempt'], 'pairingRef':row['pairing_ref'],
        **({'receipt':row['receipt']} if row['receipt'] is not None else {})}, timeout=8)
    if reply.get('recorded') is True:
        with pilot.db:
            pilot.db.execute('UPDATE pi_workspace_image_receipts SET acknowledged=1 WHERE delivery=?', (row['delivery'],))


def recover_receipts(pilot):
    """Flush frozen receipts before current pairing lookup; never call the Bot API."""
    if not pilot.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pi_workspace_image_receipts'").fetchone():
        return
    initialize(pilot.db)
    for row in pilot.db.execute('''SELECT * FROM pi_workspace_image_receipts
            WHERE bot=? AND acknowledged=0 AND binding_json IS NOT NULL AND next_ack<=? ORDER BY rowid LIMIT 4''',
            (pilot.bot_id,time.time())).fetchall():
        try:
            _acknowledge(pilot,row)
        except (RuntimeError, ValueError):
            continue


def poll(pilot, chat, binding, digest):
    from .telegram import TelegramUnavailable
    from .telegram_files import document_receipt

    initialize(pilot.db)
    recover_receipts(pilot)

    def request(action, **fields):
        return pilot.gateway._request(
            "telegram-workspace-media", {"binding": binding, "action": action, **fields}, timeout=45
        )

    def acknowledge(delivery, attempt, receipt):
        row = pilot.db.execute('SELECT * FROM pi_workspace_image_receipts WHERE delivery=?', (delivery,)).fetchone()
        if row['binding_json'] is not None:
            return _acknowledge(pilot,row)
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
        WHERE bot=? AND chat=? AND binding=? AND acknowledged=0 AND binding_json IS NULL ORDER BY rowid LIMIT 4""",
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
            proof = binding.get('credential_ref','').removeprefix('tg-')
            freeze = set(binding) == BINDING_FIELDS and re.fullmatch(r'[0-9a-f]{64}', proof)
            reserved = pilot.db.execute(
                """INSERT OR IGNORE INTO pi_workspace_image_receipts
                (delivery,attempt,bot,chat,binding,binding_json,pairing_ref) VALUES (?,?,?,?,?,?,?)""",
                (delivery, attempt, pilot.bot_id, chat, digest,
                 json.dumps(binding, sort_keys=True) if freeze else None, proof if freeze else None),
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
