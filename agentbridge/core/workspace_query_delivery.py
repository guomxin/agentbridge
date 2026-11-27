"""Project new Workspace queries onto the Telegram destination pinned at admission."""
from datetime import timedelta
import json
from uuid import uuid4

from agentbridge.adapters.seeyon_pending_verification import _consistent_complete_collection
from . import actionable_results as actions
from .endpoint_instances import endpoint_instances, instance_key
from .store_transactions import store_transaction

MODE = "workspace_query_v2"
VERSIONS = {"openclaw": "0.4.134", "nanobot": "0.3.1", "pi": "0.1.8"}
CAPABILITY = "oa.workflow.pending.list"


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_query_destinations (
            question_id TEXT PRIMARY KEY, subject TEXT NOT NULL, source TEXT NOT NULL,
            host TEXT NOT NULL, instance TEXT NOT NULL, endpoint_id TEXT,
            endpoint_key TEXT, token_id TEXT, pair_proof TEXT,
            endpoint_hash TEXT, question_hash TEXT, selection TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_query_deliveries (
            delivery_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
            operation_id TEXT, source_hash TEXT NOT NULL, source_finished TEXT NOT NULL,
            source_kind TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL, issued INTEGER NOT NULL DEFAULT 0,
            card_issued INTEGER NOT NULL DEFAULT 0, receipt INTEGER, outcome TEXT);
        CREATE INDEX IF NOT EXISTS workspace_query_pending ON workspace_query_deliveries(state,due);
    """)


def registered(db, subject, host, scope, token):
    if host not in VERSIONS:
        return False
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_host_registrations'").fetchone()
    if not exists:
        return False
    rows = db.execute("""SELECT r.host_instance_id FROM agent_host_registrations r
        JOIN agent_host_compatibility c ON c.implementation_name=r.agent_host
          AND c.implementation_version=r.host_version AND c.accepted_level='L3' AND c.evidence_digest!=''
        WHERE r.user_subject=? AND r.agent_host=? AND (? IS NULL OR r.token_id=?) AND r.host_version=?
          AND r.accepted_level='L3' AND r.compatibility_status='approved'""", (subject, host, token, token, VERSIONS[host]))
    return any(instance_key(host, row[0]) == scope for row in rows)


def pair(db, subject, source, endpoint, ownership):
    from agentbridge.workspace.telegram_pilot_links import mirror_binding, mirror_pair_proof, mirror_sender_ids
    scope = ownership.get(source)
    if not scope or ownership.get(endpoint) != scope:
        return None
    web = db.execute("""SELECT state,client_type FROM client_endpoints WHERE endpoint_id=? AND user_subject=?""",
                     (source, subject)).fetchone()
    if not web or web["state"] != "active" or web["client_type"] not in {"web", "webchat"}:
        return None
    try:
        target = actions._telegram_endpoint(db, subject, endpoint)
    except actions.ActionableResultError:
        return None
    if scope[0] == "openclaw":
        return target, target["_binding_hash"]
    binding = mirror_binding(db, subject, scope[1], ownership)
    sender = target["external_subject"]
    if (not binding or binding["endpoint_id"] != source or binding["bot"] != target["account_id"]
            or mirror_sender_ids(db, subject, binding) != [sender]):
        return None
    proof = mirror_pair_proof(db, subject, binding, sender)
    return (target, proof) if proof else None


def pin(db, question, subject, source):
    """Called after the question and its eligible private-chat destinations are recorded."""
    candidates = db.execute("""SELECT e.endpoint_id,e.agent_host,e.token_id FROM notification_outbox o
        JOIN client_endpoints e ON e.endpoint_id=o.endpoint_id AND e.user_subject=o.user_subject
        WHERE o.event_id=? AND o.user_subject=? AND o.payload_type='timeline_message'
        AND e.client_type='telegram' AND e.state='active'""", (question, subject)).fetchall()
    ownership = endpoint_instances(db, subject)
    if not _pin_destination(db, question, subject, source, candidates, ownership):
        scope = ownership.get(source)
        if scope and registered(db, subject, scope[0], scope, None):
            db.execute("""INSERT OR IGNORE INTO workspace_query_destinations
                (question_id,subject,source,host,instance,selection) VALUES(?,?,?,?,?,?)""",
                (question,subject,source,*scope,"ambiguous" if len(candidates)>1 else "unpaired"))


def pin_button(db, tasks, event_id, subject, source):
    """A button has a durable command event but no user-authored timeline message."""
    ownership = endpoint_instances(db, subject)
    scope = ownership.get(source)
    if not scope:
        return
    if scope[0] in {"nanobot", "pi"}:
        from agentbridge.workspace.telegram_pilot_links import mirror_binding, mirror_destinations
        binding = mirror_binding(db, subject, scope[1], ownership)
        if not binding or binding["endpoint_id"] != source or not registered(db, subject, scope[0], scope, binding["token_id"]):
            return
        mirror_destinations(db, tasks, subject, binding)
        ownership = endpoint_instances(db, subject)
    candidates = [row for row in db.execute("""SELECT endpoint_id,agent_host,token_id FROM client_endpoints
        WHERE user_subject=? AND agent_host=? AND client_type='telegram' AND state='active'
          AND EXISTS (SELECT 1 FROM json_each(capabilities_json) WHERE value IN ('timeline_message','direct_status'))""",
        (subject, scope[0]))
        if ownership.get(row["endpoint_id"]) == scope]
    _pin_destination(db, event_id, subject, source, candidates, ownership)


def _pin_destination(db, question, subject, source, candidates, ownership):
    if len(candidates) != 1:
        return
    endpoint, host, token = candidates[0]
    scope = ownership.get(source)
    if not scope or not registered(db, subject, host, scope, token):
        return
    identity = pair(db, subject, source, endpoint, ownership)
    if identity:
        target, proof = identity
        message = db.execute("SELECT text FROM user_timeline WHERE entry_id=? AND user_subject=? AND role='user'",
                             (question, subject)).fetchone()
        db.execute("""INSERT OR IGNORE INTO workspace_query_destinations
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'paired')""", (question, subject, source, host, scope[1], endpoint,
            target["endpoint_key"], token, proof, target["_binding_hash"], actions._hash(message["text"]) if message else None))
        return True


def source(db, task):
    """Match either an admitted run or an attested closed quick command to the original question."""
    command = json.loads(task["summary_json"]).get("command") or {}
    params = task["user_subject"], task["origin_endpoint_id"], task["agent_host"]
    if command:
        if (command.get("kind") != "oa_pending_query" or command.get("originKind") == "telegram"
                or command.get("remainingContext") or not task["host_task_key"].startswith("quick-query:")):
            return None
        try:
            _, command = actions._load_command(db, params[0], task["task_id"], command["commandKey"])
        except (actions.ActionableResultError, KeyError):
            return None
        key = "message:workspace:quick-query:user:" + task["host_task_key"].removeprefix("quick-query:")
        request_id = command["commandKey"]
        kind = "command"
        if "messageHash" not in command["requestIdentity"]:
            pin_row = db.execute("""SELECT p.* FROM workspace_query_destinations p
                JOIN task_events e ON e.event_id=p.question_id AND e.user_subject=p.subject
                WHERE p.subject=? AND p.source=? AND p.host=? AND e.task_id=? AND e.event_type='task.prepare.requested'
                    AND e.causation_ref=?""", (*params, task["task_id"], request_id)).fetchone()
            return (pin_row, kind, request_id) if pin_row else None
    else:
        dispatches = db.execute("""SELECT d.dispatch_id,d.message_key FROM agent_host_dispatches d
            WHERE d.user_subject=? AND d.origin_endpoint_id=? AND d.agent_host=? AND d.conversation_ref=?
            AND d.accepted_run_id=? LIMIT 2""", (*params,task["active_conversation_ref"],task["host_task_key"])).fetchall()
        if len(dispatches) != 1:
            return None
        request_id, message_key = dispatches[0]
        key, kind = "message:" + message_key, "dispatch"
    pin_row = db.execute("""SELECT p.*,q.text FROM workspace_query_destinations p JOIN user_timeline q ON
        q.entry_id=p.question_id AND q.user_subject=p.subject AND q.source_endpoint_id=p.source
        JOIN client_endpoints e ON e.endpoint_id=p.source AND e.user_subject=p.subject
        WHERE p.subject=? AND p.source=? AND p.host=? AND q.role='user' AND q.dedupe_key=?
        AND e.conversation_ref=? AND e.client_type IN ('web','webchat') AND e.state='active'""",
        (*params, key, task["active_conversation_ref"])).fetchone()
    if pin_row and (pin_row["selection"] != "paired" or actions._hash(pin_row["text"]) != pin_row["question_hash"]):
        return None
    return (pin_row, kind, request_id) if pin_row else None


def original_operation(db, task):
    rows = db.execute("""SELECT o.* FROM task_operations l JOIN operations o ON o.operation_id=l.operation_id
        AND o.user_subject=l.user_subject WHERE l.task_id=? AND l.user_subject=?
        ORDER BY l.linked_at, l.rowid LIMIT 4""", (task["task_id"], task["user_subject"])).fetchall()
    if not rows or len(rows) > 3 or any(op["capability_name"] != CAPABILITY for op in rows):
        return None
    original, current = rows[0], rows[-1]
    if current["operation_id"] != task["current_operation_id"]:
        return None
    command = json.loads(task["summary_json"]).get("command") or {}
    if command:
        suffix = f"{task['task_id']}:{command.get('commandKey')}"
        if (original["idempotency_key"] != "workspace-prepare:" + suffix
                or original["input_hash"] != actions._hash({})
                or original["capability_version"] != command.get("capabilityVersion")):
            return None
    if len(rows) == 1:
        return current
    # Login attempts may precede the only business result. A second unrelated read,
    # a different filter, an arbitrary task operation, or a different card is ineligible.
    if any(op["error_code"] != "LOGIN_REQUIRED" or op["status"] != "requires_user_action" for op in rows[:-1]):
        return None
    for previous, following in zip(rows, rows[1:]):
        if (following["input_hash"], following["capability_version"]) != (original["input_hash"], original["capability_version"]):
            return None
        if command and following is rows[1] and following["idempotency_key"] == "quick-query-session-ready:" + suffix:
            continue
        interaction = task["current_interaction_id"]
        if not interaction or following["idempotency_key"] != f"login-read:{interaction}:{previous['operation_id']}":
            return None
        proof = db.execute("""SELECT 1 FROM task_interactions c JOIN interactions i USING(interaction_id)
            JOIN auth_challenges a ON a.challenge_id=json_extract(i.resume_json,'$.challengeId')
              AND a.user_subject=i.user_subject AND a.system_id=i.system_id AND a.session_id=i.session_id
            WHERE c.task_id=? AND c.user_subject=? AND i.user_subject=c.user_subject AND i.interaction_id=?
              AND i.system_id='oa' AND i.interaction_type='credential' AND a.state='succeeded'
              AND julianday(a.completed_at)<=julianday(?)""",
            (task["task_id"], task["user_subject"], interaction, following["created_at"])).fetchone()
        if not proof:
            return None
    return current


def project(db, task):
    if task["status"] not in {"succeeded", "failed", "outcome_unknown", "canceled"}:
        return
    # This store also runs without the Workspace subsystem.
    if not db.execute("SELECT 1 FROM workspace_query_destinations WHERE subject=? AND source=? LIMIT 1",
                      (task["user_subject"],task["origin_endpoint_id"])).fetchone():
        return
    origin = source(db, task)
    if not origin:
        return
    op = original_operation(db, task)
    if not op and (origin[1] != "command" or task["status"] == "succeeded" or task["current_operation_id"]):
        return
    pin_row, kind, request = origin
    finished = (op["finished_at"] if op else None) or task["updated_at"]
    digest = _result_digest(task, op)
    db.execute("""INSERT OR IGNORE INTO workspace_query_deliveries(delivery_id,question_id,task_id,
        operation_id,source_hash,source_finished,source_kind,request_id,state,due)
        VALUES(?,?,?,?,?,?,?,?,'pending',?)""", (str(uuid4()),pin_row["question_id"],task["task_id"],
        op["operation_id"] if op else None,digest,finished,kind,request,actions._now().isoformat()))


_JOIN = """SELECT d.*,p.subject,p.source,p.host,p.instance,p.endpoint_id,p.endpoint_key,p.token_id,
    p.pair_proof,p.endpoint_hash FROM workspace_query_deliveries d JOIN workspace_query_destinations p USING(question_id)"""


def _result_digest(task, operation):
    result = None
    if operation:
        result = [operation["operation_id"], operation["status"], operation["error_code"],
                  operation["finished_at"], json.loads(operation["result_json"] or "null")]
    return actions._hash([task["status"], result])


def current(db, row):
    subject = row["subject"]
    ownership = endpoint_instances(db, subject)
    identity = pair(db, subject, row["source"], row["endpoint_id"], ownership)
    if (not identity or identity[1] != row["pair_proof"] or identity[0]["_binding_hash"] != row["endpoint_hash"]
            or ownership.get(row["source"]) != (row["host"],row["instance"])):
        actions._fail("ACTION_ENDPOINT_INVALID", "原工作台或 Telegram 配对已变化。")
    task = actions._owned_task(db, subject, row["task_id"])
    origin = source(db, task)
    op = original_operation(db, task)
    if (not origin or origin[0]["question_id"] != row["question_id"] or origin[1:] != (row["source_kind"],row["request_id"])
            or task["status"] not in {"succeeded","failed","outcome_unknown","canceled"}
            or (op["operation_id"] if op else None) != row["operation_id"]
            or (not op and task["current_operation_id"])):
        actions._fail("ACTION_SOURCE_CHANGED", "原查询来源已变化。")
    if _result_digest(task, op) != row["source_hash"]:
        actions._fail("ACTION_SOURCE_CHANGED", "原查询内容已变化。")
    return task,op


def checked_card(db, subject, target, task, delivery):
    row = db.execute(_JOIN + " WHERE d.delivery_id=? AND p.subject=? AND p.endpoint_id=? AND d.task_id=?",
                     (delivery,subject,target["endpoint_id"],task)).fetchone()
    if not row:
        return None
    current(db, row)
    # Ticket reads may show the original selection after it becomes read-only.
    # Issuance, begin, and every preparation enforce the original source TTL separately.
    op, pending = actions._source(db, subject, task, row["operation_id"], fresh=False)
    items = pending["items"]
    if not items or pending["coverage"].get("hasMore") or not _consistent_complete_collection(pending, items):
        actions._fail("ACTION_SOURCE_INCOMPLETE", "原待办结果不完整。")
    return {"operationId":op["operation_id"],"deliveryId":delivery}


def claim(service, subject, host, instance, endpoint, token, limit, lease):
    now = actions._now()
    with store_transaction(service.tasks) as db:
        rows = db.execute(_JOIN + """ WHERE p.subject=? AND p.host=? AND p.instance=? AND p.endpoint_key=?
            AND p.token_id=? AND d.state IN ('pending','leased','retry_wait') AND d.due<=?
            AND NOT EXISTS (SELECT 1 FROM workspace_text_deliveries m WHERE m.question_id=p.question_id
                AND m.kind='user' AND m.state IN ('pending','leased','retry_wait','sending'))
            ORDER BY d.due,d.rowid LIMIT ?""",
            (subject,host,instance_key(host,instance)[1],endpoint,token,now.isoformat(),max(1,min(limit,100)))).fetchall()
        result = []
        for row in rows:
            if not registered(db,subject,host,(host,row["instance"]),row["token_id"]):
                continue
            if row["attempt"] >= 5 or now >= actions._date(row["source_finished"])+timedelta(seconds=actions.SOURCE_TTL_SECONDS):
                db.execute("UPDATE workspace_query_deliveries SET state='expired' WHERE delivery_id=?", (row["delivery_id"],))
                continue
            try:
                current(db,row)
            except actions.ActionableResultError:
                db.execute("UPDATE workspace_query_deliveries SET state='revoked' WHERE delivery_id=?", (row["delivery_id"],))
                continue
            db.execute("""UPDATE workspace_query_deliveries SET state='leased',attempt=attempt+1,issued=0,
                card_issued=0,due=? WHERE delivery_id=?""",
                ((now+timedelta(seconds=max(5,min(300,lease)))).isoformat(),row["delivery_id"]))
            result.append({"deliveryMode":MODE,"deliveryId":row["delivery_id"],"sourceTaskId":row["task_id"],
                           "attemptCount":row["attempt"]+1,"pairingRef":row["pair_proof"]})
        return result


def settle(db, row, outcome, receipt, delay, *, table="workspace_query_deliveries"):
    if table not in {"workspace_query_deliveries", "workspace_text_deliveries"}:
        raise ValueError("Unknown Workspace delivery ledger")
    target_state = {"sent":"sent", "delivery_unknown":"unknown", "retry_after":"retry_wait",
                    "unavailable":"rejected", "rejected":"rejected"}.get(outcome)
    if outcome == "delivery_unknown" and row["state"] in {"leased", "rejected"}:
        # The host lost its begin response, but this attempt never acquired a
        # permit. Close its local journal without accepting a later 'sent' claim.
        target_state = "rejected"
    if not target_state or (outcome == "sent") != (type(receipt) is int and 0 < receipt < 2**53):
        return False
    if outcome != "sent" and receipt is not None:
        return False
    if outcome == "retry_after":
        if type(delay) is not int or not 1 <= delay <= 86400:
            return False
    elif delay is not None:
        return False
    if row["state"] == target_state:
        return row["outcome"] == outcome and row["receipt"] == receipt
    if not (row["state"] == "sending" or (row["state"] == "leased" and outcome in {"unavailable", "delivery_unknown"})
            or (row["state"] == "unknown" and outcome == "sent")):
        return False
    due = (actions._now()+timedelta(seconds=delay or 0)).isoformat()
    db.execute(f"UPDATE {table} SET state=?,due=?,receipt=?,outcome=? WHERE delivery_id=?",
               (target_state,due,receipt,outcome,row["delivery_id"]))
    return True


def handle(service, *, subject, instance, host, endpoint_key, token_id, pairing_ref, delivery_id,
           attempt_count, action, outcome=None, receipt=None, retry_after_seconds=None):
    """Return None for legacy delivery IDs. Repeated begin can never authorize another send."""
    from .telegram_result_views import ResultViewService
    result_key = {"issue":"available","begin":"allowed","ack":"recorded"}[action]
    refused = {result_key: False}
    from . import workspace_text_delivery
    text_result = workspace_text_delivery.handle(service, subject=subject, host=host, instance=instance,
        endpoint_key=endpoint_key, token_id=token_id, pairing_ref=pairing_ref, delivery_id=delivery_id,
        attempt_count=attempt_count, action=action, outcome=outcome, receipt=receipt, retry_after_seconds=retry_after_seconds)
    if text_result is not None:
        return text_result
    with store_transaction(service.tasks) as db:
        row = db.execute(_JOIN + " WHERE d.delivery_id=?", (delivery_id,)).fetchone()
        if row is None:
            return None
        if (row["subject"], row["host"], row["instance"], row["endpoint_key"], row["token_id"],
                row["pair_proof"], row["attempt"]) != (
                subject,host,instance_key(host,instance)[1],endpoint_key,token_id,pairing_ref,attempt_count):
            return refused
        if action == "ack":
            return {"recorded":settle(db,row,outcome,receipt,retry_after_seconds)}
        if any(value is not None for value in (outcome,receipt,retry_after_seconds)):
            return refused
        now = actions._now()
        if (row["state"] != "leased" or actions._date(row["due"]) <= now or
                actions._date(row["source_finished"])+timedelta(seconds=actions.SOURCE_TTL_SECONDS) <= now
                or not registered(db,subject,host,(host,row["instance"]),token_id)):
            return refused
        try:
            task, op = current(db,row)
            service.user_grants.require_capability(subject,CAPABILITY)
        except (actions.ActionableResultError,PermissionError):
            return refused
        if action == "begin":
            if not row["issued"]:
                return refused
            if row["card_issued"]:
                # Re-evaluate current selection rights as well as the original collection.
                target = actions._telegram_endpoint(db,subject,row["endpoint_id"])
                try:
                    checked_card(db,subject,target,row["task_id"],delivery_id)
                    groups = ResultViewService(service)._groups(subject,row["task_id"])
                except (actions.ActionableResultError,PermissionError):
                    return refused
                if not any(g["operationId"] == row["operation_id"] and any(t["enabled"] for t in g["targets"]) for g in groups):
                    return refused
            db.execute("UPDATE workspace_query_deliveries SET state='sending' WHERE delivery_id=?", (delivery_id,))
            return {"allowed":True}

        reply = {"available":True,"leaseExpiresAt":row["due"]}
        if task["status"] != "succeeded" or not op or op["status"] != "succeeded":
            reply["publicText"] = {
                "canceled": "本次 OA 待办查询已停止。",
                "outcome_unknown": "OA 待办查询结果尚未确认，请在工作台查看原请求。",
            }.get(task["status"], "本次 OA 待办查询未完成，请在工作台查看原请求。")
        else:
            result = json.loads(op["result_json"] or "{}")
            items = result.get("items") if isinstance(result,dict) else None
            coverage = result.get("coverage") if isinstance(result,dict) else None
            complete = (isinstance(items,list) and isinstance(coverage,dict) and coverage.get("status") == "complete"
                        and coverage.get("hasMore") in (False,None)
                        and all(isinstance(x,dict) and isinstance(x.get("affair_id"),str) and x["affair_id"].strip() for x in items)
                        and _consistent_complete_collection(result,items))
            if complete and items:
                try:
                    view = ResultViewService(service).issue(subject,row["task_id"],row["endpoint_id"],
                        {"endpointId":row["endpoint_id"],"tokenId":token_id}, workspace_delivery_id=delivery_id,connection=db)
                    reply["resultView"] = view["resultView"]
                except (actions.ActionableResultError, PermissionError):
                    pass
            if row["source_kind"] == "command" or "resultView" not in reply:
                if complete:
                    reply["publicText"] = f"本次 OA 查询共有 {len(items)} 项待办。" if items else "本次 OA 查询没有待办。"
                else:
                    reply["publicText"] = "本次 OA 待办结果不完整，请在工作台查看原查询。"
        db.execute("UPDATE workspace_query_deliveries SET issued=1,card_issued=max(card_issued,?) WHERE delivery_id=?",
                   (int("resultView" in reply),delivery_id))
        return reply
