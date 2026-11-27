"""Original Workspace images delivered once to the Web owner's current Pi private chat."""

import base64
import time
from uuid import uuid4

from agentbridge.core.endpoint_instances import endpoint_instances
from agentbridge.core.timeline_attachments import (
    TimelineAttachmentExpired,
    TimelineAttachmentIntegrityError,
    TimelineAttachmentNotFound,
)
from .telegram_pilot_links import mirror_binding, mirror_destinations


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pi_workspace_image_deliveries (
            delivery_id TEXT PRIMARY KEY, dispatch_id TEXT NOT NULL, entry_id TEXT NOT NULL, subject TEXT NOT NULL,
            instance TEXT NOT NULL, source TEXT NOT NULL, destination TEXT NOT NULL, pair_proof TEXT NOT NULL,
            attachment_id TEXT NOT NULL, ordinal INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            attempt INTEGER NOT NULL DEFAULT 0, lease_until REAL, receipt INTEGER,
            token_id TEXT, endpoint_key TEXT,
            UNIQUE(dispatch_id,destination,ordinal));
        CREATE INDEX IF NOT EXISTS pi_workspace_images_destination
            ON pi_workspace_image_deliveries(subject,instance,destination,pair_proof,state);
    """)
    columns = {row[1] for row in db.execute('PRAGMA table_info(pi_workspace_image_deliveries)')}
    for name in ('token_id', 'endpoint_key'):
        if name not in columns:
            db.execute(f'ALTER TABLE pi_workspace_image_deliveries ADD COLUMN {name} TEXT')


def enqueue(db, tasks, dispatch, entry, images):
    subject, instance = dispatch["user_subject"], dispatch["instance_id"]
    web = mirror_binding(db, subject, instance, endpoint_instances(db, subject))
    if not web or web["host_type"] != "pi" or web["endpoint_id"] != dispatch["origin_endpoint_id"]:
        return
    for endpoint, proof in mirror_destinations(db, tasks, subject, web):
        identity = db.execute('SELECT token_id,endpoint_key FROM client_endpoints WHERE endpoint_id=? AND user_subject=?',
                              (endpoint, subject)).fetchone()
        if not identity or not identity['token_id']:
            continue
        for image in images:
            db.execute(
                """INSERT OR IGNORE INTO pi_workspace_image_deliveries
                (delivery_id,dispatch_id,entry_id,subject,instance,source,destination,pair_proof,attachment_id,ordinal,token_id,endpoint_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid4()),
                    dispatch["dispatch_id"],
                    entry["entry_id"],
                    subject,
                    instance,
                    web["endpoint_id"],
                    endpoint,
                    proof,
                    image["attachment_id"],
                    image["ordinal"],
                    identity['token_id'],
                    identity['endpoint_key'],
                ),
            )


def _scope(service, db, subject, instance, endpoint_key, pairing_ref, token_id=None):
    ownership = endpoint_instances(db, subject)
    web = mirror_binding(db, subject, instance, ownership)
    endpoint = db.execute(
        """SELECT endpoint_id,token_id FROM client_endpoints WHERE user_subject=? AND agent_host='pi'
        AND client_type='telegram' AND endpoint_key=? AND state='active'""",
        (subject, endpoint_key),
    ).fetchone()
    if (web and web["host_type"] == "pi" and endpoint and ownership.get(endpoint[0]) == ("pi", instance)
            and (token_id is None or endpoint['token_id'] == token_id)):
        for destination, proof in mirror_destinations(db, service.tasks, subject, web):
            if destination == endpoint[0] and pairing_ref == proof:
                return (subject, instance, web["endpoint_id"], destination, proof)
    raise PermissionError("未获得当前 Pi 私聊与网页配对授权。")


_SCOPE = "subject=? AND instance=? AND source=? AND destination=? AND pair_proof=?"


def _original_image(db, row):
    return db.execute(
        """SELECT i.* FROM timeline_attachments i
        JOIN agent_host_dispatches d ON d.dispatch_id=? AND d.agent_host='pi' AND d.user_subject=i.user_subject
            AND d.message_key=i.message_key AND d.instance_id=? AND d.origin_endpoint_id=?
        JOIN user_timeline t ON t.entry_id=? AND t.user_subject=d.user_subject AND t.source_endpoint_id=d.origin_endpoint_id
            AND t.role='user' AND t.entry_type='chat_message' AND t.task_id IS NULL AND t.dedupe_key='message:'||d.message_key
        WHERE i.attachment_id=? AND i.ordinal=? AND i.state='ready' AND julianday(i.expires_at)>julianday('now')
            AND i.user_subject=? AND json_extract(d.attachment_refs_json,'$['||i.ordinal||']')=i.attachment_id
            AND json_extract(t.payload_json,'$.attachments['||i.ordinal||'].attachmentId')=i.attachment_id""",
        (
            row["dispatch_id"],
            row["instance"],
            row["source"],
            row["entry_id"],
            row["attachment_id"],
            row["ordinal"],
            row["subject"],
        ),
    ).fetchone()


def _v2_predecessor_pending(db, row):
    # Old-only databases retain their original queue. For a partly handed-over
    # group, a pending v2 predecessor still owns its place in the original order.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_media_deliveries'").fetchone():
        return False
    return db.execute('''SELECT 1 FROM workspace_media_deliveries v
        JOIN workspace_query_destinations p ON p.question_id=v.question_id
        JOIN pi_workspace_image_deliveries previous ON previous.dispatch_id=v.dispatch_id
            AND previous.entry_id=v.question_id AND previous.ordinal=v.ordinal
            AND previous.attachment_id=v.attachment_id AND previous.destination=p.endpoint_id
        WHERE v.dispatch_id=? AND v.question_id=? AND p.endpoint_id=? AND v.ordinal<?
            AND previous.state='superseded' AND v.state IN ('pending','leased','retry_wait','sending')
        LIMIT 1''', (row['dispatch_id'],row['entry_id'],row['destination'],row['ordinal'])).fetchone() is not None


def _claim(db, scope):
    now = time.time()
    db.execute(
        f"""UPDATE pi_workspace_image_deliveries SET state='unknown' WHERE {_SCOPE}
        AND state='sending' AND lease_until<=?""",
        (*scope, now),
    )
    while True:
        row = db.execute(
            f"""SELECT * FROM pi_workspace_image_deliveries WHERE {_SCOPE}
            AND state IN ('pending','claimed','sending') ORDER BY rowid LIMIT 1""",
            scope,
        ).fetchone()
        if not row or row["state"] == "sending" or row["state"] == "claimed" and row["lease_until"] > now:
            return {"available": False}
        if _v2_predecessor_pending(db, row):
            return {"available": False}
        if not _original_image(db, row):
            db.execute(
                "UPDATE pi_workspace_image_deliveries SET state='unavailable' WHERE delivery_id=?",
                (row["delivery_id"],),
            )
            continue
        attempt = row["attempt"] + 1
        db.execute(
            "UPDATE pi_workspace_image_deliveries SET state='claimed',attempt=?,lease_until=? WHERE delivery_id=?",
            (attempt, now + 60, row["delivery_id"]),
        )
        return {
            "available": True,
            "deliveryId": row["delivery_id"],
            "attempt": attempt,
            "ordinal": row["ordinal"],
        }


def delivery(
    service,
    *,
    subject,
    instance,
    endpoint_key,
    pairing_ref,
    action,
    delivery_id=None,
    attempt=None,
    receipt=None,
    token_id=None,
):
    if action not in ("claim", "read", "begin", "ack"):
        raise ValueError("未知的原图投递操作。")
    params = (subject, instance, endpoint_key, pairing_ref, token_id)
    with service.workspace._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = None
        if action == 'ack' and token_id is not None:
            # Settle only a previously frozen send under its original personal
            # credential. This path cannot issue bytes or authorize another send.
            row = db.execute("""SELECT * FROM pi_workspace_image_deliveries WHERE subject=? AND instance=?
                AND endpoint_key=? AND pair_proof=? AND token_id=? AND delivery_id=? AND attempt=?""",
                (subject, instance, endpoint_key, pairing_ref, token_id, delivery_id, attempt)).fetchone()
        scope = _scope(service, db, *params) if row is None else None
        if action == "claim":
            return _claim(db, scope)
        if row is None:
            row = db.execute(
                f"SELECT * FROM pi_workspace_image_deliveries WHERE {_SCOPE} AND delivery_id=? AND attempt=?",
                (*scope, delivery_id, attempt),
            ).fetchone()
            if row is not None and token_id is not None and row['token_id'] not in (None, token_id):
                row = None
        if not row:
            raise PermissionError("找不到当前申请的原图投递。")
        if action == "ack":
            state = "sent" if receipt is not None else "unknown"
            if receipt is not None and (type(receipt) is not int or not 0 < receipt < 2**53):
                raise ValueError("无效的 Telegram 送达回执。")
            if row["state"] == "sending" or row["state"] == "unknown" and receipt is not None:
                db.execute(
                    "UPDATE pi_workspace_image_deliveries SET state=?,receipt=? WHERE delivery_id=?",
                    (state, receipt, delivery_id),
                )
                return {"recorded": True}
            return {"recorded": row["state"] == state and row["receipt"] == receipt}
        if row["state"] != "claimed" or row["lease_until"] <= time.time():
            return {"available": False}
        if _v2_predecessor_pending(db, row):
            return {"available": False}
        image = _original_image(db, row)
        if not image:
            db.execute(
                "UPDATE pi_workspace_image_deliveries SET state='unavailable' WHERE delivery_id=?",
                (delivery_id,),
            )
            return {"available": False}
        if action == "begin":
            # No ambiguous Bot API send can be retried, including after a process restart.
            db.execute(
                "UPDATE pi_workspace_image_deliveries SET state='sending',lease_until=? WHERE delivery_id=?",
                (time.time() + 120, delivery_id),
            )
            return {"available": True}
    try:
        material = service.timeline_attachments.ready_payload(image["attachment_id"])
    except (TimelineAttachmentExpired, TimelineAttachmentIntegrityError, TimelineAttachmentNotFound):
        with service.workspace._connect() as db:
            db.execute(
                "UPDATE pi_workspace_image_deliveries SET state='unavailable' WHERE delivery_id=? AND attempt=? AND state='claimed'",
                (delivery_id, attempt),
            )
        return {"available": False}
    if material["content_hash"] != image["content_hash"] or material["user_subject"] != subject:
        return {"available": False}
    return {
        "available": True,
        "contentType": material["content_type"],
        "byteSize": material["byte_size"],
        "sha256": material["content_hash"],
        "contentBase64": base64.b64encode(material["body"]).decode("ascii"),
    }
