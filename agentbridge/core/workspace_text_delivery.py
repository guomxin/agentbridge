"""Deliver public Workspace messages in order to the Telegram pairing pinned at admission."""

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from . import actionable_results as actions
from . import workspace_query_delivery as query
from .chat_mirror import public_mirror_text
from .endpoint_instances import endpoint_instances, instance_key
from .store_transactions import store_transaction


MODE = "workspace_text_v2"
TABLE = "workspace_text_deliveries"
STATUS = {
    "failed_before_accept": "智能体未接收本次工作台请求。",
    "acceptance_unknown": "无法确认智能体是否已接收本次工作台请求，系统已停止重发。",
    "failed": "本次工作台运行失败，请在原会话查看详情。",
    "stopped": "本次工作台运行已停止。如已进入业务操作，请在原任务核对状态。",
    "canceled": "本次工作台请求已取消。",
    "expired": "工作台请求已超时，智能体尚未接收。",
    "result_unknown": "本次工作台运行已接收，但最终结果尚未确认。请在原会话核对。",
}
_JOIN = """SELECT d.*,p.subject,p.source,p.host,p.instance,p.endpoint_id,p.endpoint_key,
                p.token_id,p.pair_proof,p.endpoint_hash,p.question_hash
           FROM workspace_text_deliveries d JOIN workspace_query_destinations p USING(question_id)"""


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_text_deliveries(
            delivery_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, entry_id TEXT NOT NULL,
            kind TEXT NOT NULL, request_id TEXT, part_index INTEGER NOT NULL, part_count INTEGER NOT NULL,
            part_start INTEGER NOT NULL, part_end INTEGER NOT NULL, part_hash TEXT NOT NULL,
            source_hash TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
            due TEXT NOT NULL, issued INTEGER NOT NULL DEFAULT 0, receipt INTEGER, outcome TEXT,
            UNIQUE(question_id,entry_id,part_index));
        CREATE INDEX IF NOT EXISTS workspace_text_due ON workspace_text_deliveries(state,due);
    """)


def split_text(text):
    """Reserve room for a role and part number even after HTML escaping on Telegram."""
    segments = []
    start = used = 0
    escapes = {"&": 5, "<": 4, ">": 4, '"': 6, "'": 6}
    for pos, char in enumerate(text):
        size = escapes.get(char, 2 if ord(char) > 65535 else 1)
        if used + size > 2700:
            segments.append(text[start:pos])
            start, used = pos, 0
        used += size
    segments.append(text[start:])
    return segments


def _record(db, pin, entry_id, kind, request, original):
    parts = split_text(original)
    digest = hashlib.sha256(original.encode()).hexdigest()
    due = actions._now().isoformat()
    count = len(parts)
    start = 0
    for number, part in enumerate(parts, 1):
        db.execute("""INSERT OR IGNORE INTO workspace_text_deliveries(delivery_id,question_id,entry_id,kind,
            request_id,part_index,part_count,part_start,part_end,part_hash,source_hash,state,due)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)""", (str(uuid4()), pin["question_id"], entry_id, kind,
                request, number, count, start, start + len(part), hashlib.sha256(part.encode()).hexdigest(), digest, due))
        start += len(part)


def _question_for_run(db, entry):
    run = entry["dedupe_key"].removeprefix("message:workspace:assistant:")
    rows = db.execute("""SELECT p.*,d.dispatch_id,d.conversation_ref FROM agent_host_dispatches d
        JOIN user_timeline q ON q.user_subject=d.user_subject AND q.source_endpoint_id=d.origin_endpoint_id
            AND q.role='user' AND q.dedupe_key='message:'||d.message_key
        JOIN workspace_query_destinations p ON p.question_id=q.entry_id AND p.subject=q.user_subject
            AND p.source=q.source_endpoint_id AND p.host=d.agent_host
        WHERE d.origin_endpoint_id=? AND d.user_subject=? AND d.accepted_run_id=? LIMIT 2""",
        (entry["source_endpoint_id"], entry["user_subject"], run)).fetchall()
    return rows[0] if len(rows) == 1 else None


def _project_text(db, pin, entry):
    if entry["role"] != "assistant":
        return public_mirror_text(entry["text"], maximum=200_000)
    run = entry["dedupe_key"].removeprefix("message:workspace:assistant:")
    private = db.execute("""SELECT 1 FROM agent_host_dispatch_events WHERE dispatch_id=?
        AND event_name='host_progress' AND json_extract(payload_json,'$.kind')='private_interaction'
        UNION ALL SELECT 1 FROM agent_tasks t JOIN task_interactions i USING(task_id)
        WHERE t.user_subject=? AND i.user_subject=t.user_subject AND t.host_task_key=? AND
              t.agent_host=? AND t.origin_endpoint_id=? AND t.active_conversation_ref=? LIMIT 1""",
        (pin["dispatch_id"],pin["subject"],run,pin["host"],pin["source"],pin["conversation_ref"])).fetchone()
    if not private:
        return public_mirror_text(entry["text"], maximum=200_000)
    from .tasks import TaskHubStore
    if TaskHubStore._workspace_oa_read_after_login(db, pin["subject"], pin["source"], pin, run, pin["host"]):
        safe = public_mirror_text(entry["text"], maximum=200_000)
        if safe:
            return safe
    # A final response exists, but its prose may quote private fields even when
    # it contains no recognizable secret. Only project the original task's status.
    task = db.execute("""SELECT status FROM agent_tasks WHERE user_subject=? AND agent_host=?
        AND host_task_key=? AND origin_endpoint_id=? AND active_conversation_ref=?""",
        (pin["subject"], pin["host"], run, pin["source"], pin["conversation_ref"])).fetchone()
    labels = {"succeeded": "已完成", "partially_succeeded": "部分完成", "failed": "失败",
              "outcome_unknown": "结果尚未确认", "canceled": "已取消", "expired": "已过期",
              "superseded": "已被后续任务替代"}
    label = labels.get(task["status"]) if task else None
    status = f"原任务状态为{label}。" if label else ""
    return f"工作台已收到本次运行的回复。{status}请在原会话查看可信交互和业务详情。"


def enqueue_message(db, entry):
    kind = entry["role"]
    if kind == "user":
        pin = db.execute("SELECT * FROM workspace_query_destinations WHERE question_id=?", (entry["entry_id"],)).fetchone()
    elif kind == "assistant" and entry["dedupe_key"].startswith("message:workspace:assistant:"):
        if not db.execute("SELECT 1 FROM workspace_query_destinations WHERE subject=? AND source=? LIMIT 1",
                          (entry["user_subject"],entry["source_endpoint_id"])).fetchone():
            return
        pin = _question_for_run(db, entry)
    else:
        return
    if not pin:
        return
    # An upgraded destination has exactly one sender. Preserve the old question's
    # pairing evidence for older sibling messages without giving old workers a lease.
    db.execute("""UPDATE notification_outbox SET state='superseded' WHERE event_id=? AND user_subject=?
        AND endpoint_id IN (SELECT endpoint_id FROM client_endpoints WHERE agent_host=? AND client_type='telegram')
        AND payload_type='timeline_message' AND state='pending' AND attempt_count=0""",
        (entry["entry_id"], pin["subject"], pin["host"]))
    if pin["selection"] != "paired":
        return
    metadata = json.loads(entry["payload_json"])
    if entry["task_id"] or set(metadata) - {"attachments"} or metadata.get("attachments"):
        return
    text = _project_text(db, pin, entry)
    if not text:
        return
    _record(db, pin, entry["entry_id"], kind, pin["dispatch_id"] if kind == "assistant" else None, text)


def enqueue_status(db, dispatch, state):
    if state not in STATUS:
        return
    pin = db.execute("""SELECT p.* FROM user_timeline q JOIN workspace_query_destinations p
        ON p.question_id=q.entry_id AND p.source=q.source_endpoint_id AND p.subject=q.user_subject
        WHERE q.user_subject=? AND q.dedupe_key='message:'||? AND q.role='user' AND p.source=? AND p.host=?""",
        (dispatch["user_subject"], dispatch["message_key"], dispatch["origin_endpoint_id"], dispatch["agent_host"])).fetchone()
    if pin and pin["selection"] == "paired":
        _record(db, pin, dispatch["dispatch_id"], "status", dispatch["dispatch_id"], STATUS[state])


def _current(service, db, row):
    subject = row["subject"]
    current = query.pair(db, subject, row["source"], row["endpoint_id"], endpoint_instances(db, subject))
    if not current or current[1] != row["pair_proof"] or current[0]["_binding_hash"] != row["endpoint_hash"]:
        return False
    question = db.execute("SELECT text FROM user_timeline WHERE entry_id=? AND user_subject=? AND source_endpoint_id=?",
                          (row["question_id"], subject, row["source"])).fetchone()
    if not question or actions._hash(question["text"]) != row["question_hash"]:
        return False
    if row["kind"] == "status":
        dispatch = db.execute("""SELECT state FROM agent_host_dispatches WHERE dispatch_id=?
            AND user_subject=? AND agent_host=? AND origin_endpoint_id=?""",
            (row["request_id"], subject, row["host"], row["source"])).fetchone()
        text = STATUS.get(dispatch["state"]) if dispatch else None
    else:
        entry = db.execute("""SELECT * FROM user_timeline WHERE entry_id=? AND user_subject=?
            AND source_endpoint_id=? AND role=?""", (row["entry_id"], subject, row["source"], row["kind"])).fetchone()
        if not entry:
            return False
        pin = row
        if row["kind"] == "assistant":
            pin = _question_for_run(db, entry)
            run = entry["dedupe_key"].removeprefix("message:workspace:assistant:")
            if not pin or pin["question_id"] != row["question_id"]:
                return False
            if db.execute("""SELECT 1 FROM agent_tasks t JOIN task_operations l USING(task_id)
                JOIN operations o USING(operation_id) WHERE t.user_subject=? AND t.agent_host=? AND
                t.origin_endpoint_id=? AND t.host_task_key=? AND o.user_subject=t.user_subject AND o.capability_name=?""",
                (subject,row["host"],row["source"],run,query.CAPABILITY)).fetchone():
                try:
                    service.user_grants.require_capability(subject, query.CAPABILITY)
                except PermissionError:
                    return False
        text = _project_text(db, pin, entry)
    if not text or hashlib.sha256(text.encode()).hexdigest() != row["source_hash"]:
        return False
    part = text[row["part_start"]:row["part_end"]]
    if hashlib.sha256(part.encode()).hexdigest() != row["part_hash"]:
        return False
    role = {"user": "工作台提问", "assistant": "工作台回复", "status": "工作台状态"}[row["kind"]]
    label = role if row["part_count"] == 1 else f"{role}（{row['part_index']}/{row['part_count']}）"
    return label + "\n" + part


def claim(service, subject, host, instance, endpoint, token, limit, lease):
    scope = instance_key(host, instance)
    now = actions._now()
    with store_transaction(service.tasks) as db:
        if not query.registered(db, subject, host, scope, token):
            return []
        rows = db.execute(_JOIN + """ WHERE p.subject=? AND p.host=? AND p.instance=? AND p.endpoint_key=?
            AND p.token_id=? AND d.state IN ('pending','leased','retry_wait') AND d.due<=?
            AND NOT EXISTS (SELECT 1 FROM workspace_text_deliveries previous WHERE previous.question_id=d.question_id
                AND previous.rowid<d.rowid AND previous.state!='sent' AND
                (previous.entry_id=d.entry_id OR previous.state IN ('pending','leased','retry_wait','sending')))
            ORDER BY d.due,d.rowid LIMIT ?""", (subject,host,scope[1],endpoint,token,now.isoformat(),limit)).fetchall()
        result = []
        for row in rows:
            if row["attempt"] >= 5 or not _current(service, db, row):
                db.execute("UPDATE workspace_text_deliveries SET state=? WHERE delivery_id=?",
                           ("expired" if row["attempt"] >= 5 else "revoked", row["delivery_id"]))
                continue
            due = (now + timedelta(seconds=max(5, min(300, lease)))).isoformat()
            db.execute("""UPDATE workspace_text_deliveries SET state='leased',attempt=attempt+1,issued=0,due=?
                WHERE delivery_id=?""", (due, row["delivery_id"]))
            result.append({"deliveryMode":MODE, "sourceEntryId":row["entry_id"], "deliveryId":row["delivery_id"],
                           "pairingRef":row["pair_proof"], "attemptCount":row["attempt"]+1,
                           "partIndex":row["part_index"], "partCount":row["part_count"]})
        return result


def handle(service, *, subject, instance, host, endpoint_key, token_id, pairing_ref, delivery_id,
           attempt_count, action, outcome=None, receipt=None, retry_after_seconds=None):
    key = {"issue":"available", "begin":"allowed", "ack":"recorded"}[action]
    with store_transaction(service.tasks) as db:
        row = db.execute(_JOIN + " WHERE d.delivery_id=?", (delivery_id,)).fetchone()
        if row is None:
            return None
        scope = instance_key(host, instance)
        expected = (subject, host, scope[1], endpoint_key, token_id, pairing_ref, attempt_count)
        if tuple(row[field] for field in ("subject","host","instance","endpoint_key","token_id","pair_proof","attempt")) != expected:
            return {key:False}
        if action == "ack":
            recorded = query.settle(db, row, outcome, receipt, retry_after_seconds, table=TABLE)
            if recorded and outcome in {"unavailable", "rejected", "delivery_unknown", "sent"}:
                # A terminal part prevents later parts of the same message from
                # leaking through. A matched late receipt can release unattempted parts.
                old, new = ("blocked", "pending") if outcome == "sent" else ("pending", "blocked")
                db.execute("""UPDATE workspace_text_deliveries SET state=? WHERE question_id=? AND entry_id=?
                    AND part_index>? AND attempt=0 AND state=?""",
                    (new, row["question_id"], row["entry_id"], row["part_index"], old))
            return {key:recorded}
        if (any(v is not None for v in (outcome, receipt, retry_after_seconds)) or row["state"] != "leased"
                or actions._date(row["due"]) <= actions._now() or not query.registered(db, subject, host, scope, token_id)):
            return {key:False}
        text = _current(service, db, row)
        if not text:
            return {key:False}
        if action == "begin":
            if not row["issued"]:
                return {key:False}
            db.execute("UPDATE workspace_text_deliveries SET state='sending' WHERE delivery_id=?", (delivery_id,))
            return {key:True}
        db.execute("UPDATE workspace_text_deliveries SET issued=1 WHERE delivery_id=?", (delivery_id,))
        return {key:True, "leaseExpiresAt":row["due"], "publicText":text}
