"""Resolve a single explicitly referenced, receipted public projection without dispatching work."""

from datetime import timedelta
import hashlib
import json
import re

from . import actionable_results as actions
from . import workspace_query_delivery as query
from . import workspace_text_delivery as texts
from .chat_mirror import PUBLIC_TEXT_MAX_CHARACTERS, public_mirror_text
from .endpoint_instances import endpoint_instances, instance_key


def _reference(value):
    if not isinstance(value, dict) or len(value) > 1:
        raise ValueError("Expected one explicit reference")
    if not value:
        return None, None
    key, item = next(iter(value.items()))
    if key == "telegramReceipt" and type(item) is int and 0 < item < 2**53:
        return key, item
    if key in {"taskId", "entryId"} and isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item):
        return key, item
    raise ValueError("Invalid cross-endpoint reference")


def _entry(entry_id, source, role, value, created):
    return {"entryId": entry_id, "source": {"clientType": source}, "role": role,
            "text": value[:600], "createdAt": created}


def _from_workspace(service, db, endpoint, token, key, ident, cutoff, limit):
    args = (endpoint["user_subject"], endpoint["endpoint_id"], token, cutoff)
    found = []
    # Resolve task references through the admitted question and its original host run.
    # Only a separately confirmed delivery of that question can supply its text.
    text_key, text_id = key, ident
    if key == "taskId":
        task = db.execute("SELECT * FROM agent_tasks WHERE user_subject=? AND task_id=?",
                          (endpoint["user_subject"], ident)).fetchone()
        admitted = query.source(db, task) if task else None
        if admitted:
            text_key, text_id = "entryId", admitted[0]["question_id"]
    if text_key != "taskId":
        condition = {"entryId": "t.entry_id=? AND t.part_index=1",
                     "telegramReceipt": "t.receipt=?"}.get(text_key, "t.kind='user' AND t.part_index=1")
        sql = "SELECT t.*,e.created_at FROM (" + texts._JOIN + """) t
            JOIN user_timeline e ON e.entry_id=t.entry_id AND e.user_subject=t.subject AND e.source_endpoint_id=t.source
            WHERE t.subject=? AND t.endpoint_id=? AND t.token_id=? AND e.created_at>=?
              AND t.state='sent' AND t.receipt>0 AND """ + condition + " ORDER BY e.created_at DESC LIMIT ?"
        rows = db.execute(sql, (*args, *((text_id,) if text_key else ()), limit + 1))
        for row in rows:
            public = texts._current(service, db, row)
            if public:
                found.append(_entry(row["entry_id"], "web", row["kind"], public.partition("\n")[2], row["created_at"]))
    condition = {"telegramReceipt": "d.receipt=?", "taskId": "d.task_id=?",
                 "entryId": "d.question_id=?"}.get(key, "d.source_kind='command'")
    rows = db.execute(query._JOIN + """ WHERE p.subject=? AND p.endpoint_id=? AND p.token_id=?
        AND d.source_finished>=? AND d.state='sent' AND d.receipt>0 AND """ + condition +
        " ORDER BY d.source_finished DESC LIMIT ?", (*args, *((ident,) if key else ()), limit + 1))
    for row in rows:
        if actions._date(row["source_finished"]) + timedelta(seconds=actions.SOURCE_TTL_SECONDS) <= actions._now():
            continue
        try:
            task, _ = query.current(db, row)
            service.user_grants.require_capability(endpoint["user_subject"], query.CAPABILITY)
        except (actions.ActionableResultError, PermissionError):
            continue
        if not any(e["entryId"] == row["question_id"] for e in found):
            state = {"succeeded": "已完成", "failed": "失败", "canceled": "已取消", "outcome_unknown": "结果待核实"}[task["status"]]
            found.append(_entry(row["question_id"], "web", "status", "原 OA 待办查询" + state + "。", row["source_finished"]))
    return found


def _from_telegram(service, db, endpoint, token, ownership, key, ident, cutoff, limit):
    if key == "telegramReceipt":
        return []
    subject = endpoint["user_subject"]
    scope = ownership[endpoint["endpoint_id"]]
    peers = []
    for target in db.execute("SELECT endpoint_id FROM client_endpoints WHERE user_subject=? AND agent_host=? AND client_type='telegram' AND state='active'",
                             (subject, scope[0])):
        paired = query.pair(db, subject, endpoint["endpoint_id"], target[0], ownership)
        if paired and paired[0]["token_id"] == token:
            peers.append(paired)
    if len(peers) != 1:
        return []
    target, proof = peers[0]
    condition = {"entryId": "r.entry_id=?", "taskId": "r.task_id=?"}.get(key, "r.role='user'")
    rows = db.execute("""SELECT e.text,e.created_at,e.payload_json,r.* FROM telegram_history_receipts r
        JOIN user_timeline e ON e.entry_id=r.entry_id AND e.user_subject=r.subject AND e.source_endpoint_id=r.endpoint_id
          AND e.role=r.role AND e.task_id IS r.task_id
        WHERE r.subject=? AND r.endpoint_id=? AND r.authority=? AND e.created_at>=? AND """ + condition +
        " ORDER BY e.created_at DESC LIMIT ?", (subject,target["endpoint_id"], proof,cutoff,*((ident,) if key else ()),limit+1))
    found = []
    for row in rows:
        public = public_mirror_text(row["text"], maximum=PUBLIC_TEXT_MAX_CHARACTERS)
        if not public or hashlib.sha256(public.encode()).hexdigest() != row["text_sha"]:
            continue
        if set(json.loads(row["payload_json"])) - {"attachments"}:
            continue
        if row["task_id"]:
            owned = db.execute("""SELECT status FROM agent_tasks WHERE task_id=? AND user_subject=? AND origin_endpoint_id=?
                AND agent_host=? AND active_conversation_ref=?""", (row["task_id"], subject, row["endpoint_id"],
                    scope[0], target["conversation_ref"])).fetchone()
            if not owned:
                continue
            if db.execute("SELECT 1 FROM task_interactions WHERE task_id=? LIMIT 1", (row["task_id"],)).fetchone():
                public = "原任务含可信交互。请在原会话查看详情。"
            if db.execute("""SELECT 1 FROM task_operations l JOIN operations o USING(operation_id)
                WHERE l.task_id=? AND o.user_subject=? AND o.capability_name=?""", (row["task_id"],subject,query.CAPABILITY)).fetchone():
                try:
                    service.user_grants.require_capability(subject,query.CAPABILITY)
                except PermissionError:
                    continue
        found.append(_entry(row["entry_id"], "telegram", row["role"], public, row["created_at"]))
    return found


def resolve(service, *, subject, host, instance, token, endpoint_key, reference, max_age_minutes=360, limit=8):
    key, ident = _reference(reference)
    limit = max(2, min(8, limit))
    cutoff = (actions._now() - timedelta(minutes=max(1, min(1440, max_age_minutes)))).isoformat()
    scope = instance_key(host, instance)
    found = []
    with service.tasks._connect() as db:
        db.execute("BEGIN")
        ownership = endpoint_instances(db, subject)
        endpoint = db.execute("""SELECT * FROM client_endpoints WHERE user_subject=? AND agent_host=?
            AND endpoint_key=? AND state='active'""", (subject, host, endpoint_key)).fetchone()
        if endpoint and ownership.get(endpoint["endpoint_id"]) == scope and query.registered(db, subject, host, scope, token):
            if endpoint["client_type"] == "telegram" and endpoint["token_id"] == token:
                found = _from_workspace(service, db, endpoint, token, key, ident, cutoff, limit)
            elif endpoint["client_type"] in {"web", "webchat"}:
                found = _from_telegram(service, db, endpoint, token, ownership, key, ident, cutoff, limit)
    found.sort(key=lambda item: item["createdAt"], reverse=True)
    decision = "unavailable" if not found else "selected" if len(found) == 1 else "ambiguous"
    return {"decision": decision, "entries": found[:limit], "allowNewOperation": False}
