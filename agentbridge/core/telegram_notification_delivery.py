"""New task events retain one original private destination and send-attempt ledger.

Opt-in is prospective. Legacy outbox entries are never adopted or reinterpreted.
Trusted interactions keep their existing presentation and authorization services.
"""
from datetime import timedelta
import json
from uuid import uuid4

from . import actionable_results as actions
from .endpoint_instances import endpoint_instances, instance_key
from .store_transactions import store_transaction
from .workspace_query_delivery import pair, registered, settle

MODE = "host_notification_v2"
TABLE = "telegram_notification_deliveries"
PUBLIC = {
    "task.interaction.expired": "可信确认已过期，请在原工作台查看状态。",
    "task.interaction.failed": "可信交互未完成，请在原工作台查看状态。",
    "task.interaction.superseded": "可信交互已更新，请使用最新卡片继续。",
    "task.canceled": "本次任务已取消。",
    "task.completed": "本次任务已完成，请在原工作台查看结果。",
    "task.operation.succeeded": "本次操作已完成，请在原工作台查看结果。",
    "task.failed": "本次任务未完成，请在原工作台查看状态。",
    "task.operation.failed": "本次操作未完成，请在原工作台查看状态。",
    "task.operation.outcome_unknown": "本次操作的最终结果尚未确认，请先核验原业务状态，不要重复提交。",
    "plan.step.waiting": "任务正在等待本人填写或确认，请查看原工作台。",
    "plan.authorization.waiting": "任务正在等待本人授权，请查看原工作台。",
    "plan.completed": "跨系统任务已完成，请在原工作台查看结果。",
    "plan.canceled": "跨系统任务已取消。",
    "plan.step.failed": "跨系统任务未完成，请在原工作台查看状态。",
    "plan.outcome_unknown": "跨系统任务的最终结果尚未确认，请先核验原业务状态，不要重复提交。",
}


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_notification_consumers(
            subject TEXT NOT NULL, host TEXT NOT NULL, instance TEXT NOT NULL, endpoint_id TEXT PRIMARY KEY,
            source TEXT NOT NULL, token_id TEXT NOT NULL, pair_proof TEXT NOT NULL, endpoint_hash TEXT NOT NULL,
            protocol TEXT NOT NULL, activated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS telegram_notification_deliveries(
            delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, task_id TEXT NOT NULL,
            subject TEXT NOT NULL, host TEXT NOT NULL, instance TEXT NOT NULL, source TEXT NOT NULL,
            endpoint_id TEXT NOT NULL, endpoint_key TEXT NOT NULL, token_id TEXT NOT NULL,
            pair_proof TEXT NOT NULL, endpoint_hash TEXT NOT NULL, source_hash TEXT NOT NULL,
            kind TEXT NOT NULL, interaction_id TEXT, public_text TEXT,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, protocol TEXT NOT NULL,
            state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL,
            issued INTEGER NOT NULL DEFAULT 0, receipt INTEGER, outcome TEXT,
            observation_hash TEXT, covered_by TEXT,
            UNIQUE(event_id,endpoint_id));
        CREATE INDEX IF NOT EXISTS telegram_notification_pending
            ON telegram_notification_deliveries(subject,host,instance,state,due);
    """)


def activate(service, subject, host, instance, endpoint_key, token):
    """Only a currently qualified original consumer can select future v2 events."""
    with store_transaction(service.tasks) as db:
        scope = instance_key(host, instance)
        if not registered(db, subject, host, scope, token):
            return
        endpoint = db.execute("SELECT * FROM client_endpoints WHERE user_subject=? AND agent_host=? AND endpoint_key=?",
                              (subject, host, endpoint_key)).fetchone()
        if not endpoint or endpoint["token_id"] != token:
            return
        ownership = endpoint_instances(db, subject)
        candidates = [row[0] for row in db.execute("SELECT endpoint_id FROM client_endpoints WHERE user_subject=? AND client_type IN ('web','webchat') AND state='active'", (subject,))
                      if ownership.get(row[0]) == scope]
        pairs = [(source, pair(db, subject, source, endpoint["endpoint_id"], ownership)) for source in candidates]
        pairs = [(source, identity) for source, identity in pairs if identity]
        if len(pairs) != 1:
            return
        source, (target, proof) = pairs[0]
        db.execute("""INSERT INTO telegram_notification_consumers VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(endpoint_id) DO UPDATE SET source=excluded.source,token_id=excluded.token_id,
            pair_proof=excluded.pair_proof,endpoint_hash=excluded.endpoint_hash,protocol=excluded.protocol,
            activated_at=excluded.activated_at
            WHERE subject=excluded.subject AND host=excluded.host AND instance=excluded.instance
              AND (source!=excluded.source OR token_id!=excluded.token_id OR pair_proof!=excluded.pair_proof
                   OR endpoint_hash!=excluded.endpoint_hash OR protocol!=excluded.protocol)""",
            (subject,host,scope[1],endpoint["endpoint_id"],source,token,proof,target["_binding_hash"],MODE,actions._now().isoformat()))


def enqueue(db, *, subject, source, endpoint, event, ownership, created_at):
    """Called in the event transaction. A new event gets a new immutable identity."""
    event_type = event["eventType"]
    if event_type not in PUBLIC and event_type != "task.interaction.waiting":
        return False
    consumer = db.execute("SELECT * FROM telegram_notification_consumers WHERE endpoint_id=? AND subject=? AND source=? AND protocol=?",
                          (endpoint,subject,source,MODE)).fetchone()
    if not consumer or not registered(db,subject,consumer["host"],(consumer["host"],consumer["instance"]),consumer["token_id"]):
        return False
    identity = pair(db,subject,source,endpoint,ownership)
    if (not identity or identity[1] != consumer["pair_proof"] or identity[0]["_binding_hash"] != consumer["endpoint_hash"]):
        return False
    target, proof = identity
    interaction = (event.get("payload") or {}).get("interactionId") if event_type == "task.interaction.waiting" else None
    if event_type == "task.interaction.waiting" and not isinstance(interaction,str):
        return False
    kind = "trusted_interaction" if interaction else "public_status"
    observation_hash = None
    operation_id = (event.get("payload") or {}).get("operationId")
    if event_type in {"task.operation.succeeded", "task.operation.failed", "task.operation.outcome_unknown"} and operation_id:
        op = db.execute("""SELECT o.* FROM operations o JOIN task_operations l USING(operation_id)
            WHERE o.operation_id=? AND o.user_subject=? AND l.user_subject=o.user_subject AND l.task_id=?""",
            (operation_id,subject,event["taskId"])).fetchone()
        if op and op["status"] == (event.get("payload") or {}).get("status") and op["finished_at"]:
            observation_hash = actions._hash([op["operation_id"],op["status"],op["error_code"],op["finished_at"],op["result_json"]])
    db.execute("""INSERT OR IGNORE INTO telegram_notification_deliveries
        (delivery_id,event_id,task_id,subject,host,instance,source,endpoint_id,endpoint_key,token_id,
         pair_proof,endpoint_hash,source_hash,kind,interaction_id,public_text,created_at,expires_at,protocol,state,due,observation_hash)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
        (str(uuid4()),event["eventId"],event["taskId"],subject,consumer["host"],consumer["instance"],source,endpoint,
         target["endpoint_key"],consumer["token_id"],proof,target["_binding_hash"],actions._hash(event),kind,interaction,
         PUBLIC.get(event_type),created_at,(actions._date(created_at)+timedelta(hours=24)).isoformat(),MODE,created_at,observation_hash))
    return True


def _current(db,row):
    subject = row["subject"]
    ownership = endpoint_instances(db,subject)
    identity = pair(db,subject,row["source"],row["endpoint_id"],ownership)
    if (not identity or identity[1] != row["pair_proof"] or identity[0]["_binding_hash"] != row["endpoint_hash"]
            or ownership.get(row["source"]) != (row["host"],row["instance"])):
        return False
    event = db.execute("""SELECT e.* FROM task_events e JOIN agent_tasks t USING(task_id)
        WHERE e.event_id=? AND e.task_id=? AND e.user_subject=? AND t.user_subject=e.user_subject
          AND t.origin_endpoint_id=? AND t.agent_host=?""",
        (row["event_id"],row["task_id"],subject,row["source"],row["host"])).fetchone()
    if not event or actions._hash({"eventId":event["event_id"],"taskId":event["task_id"],
            "eventType":event["event_type"],"payload":json.loads(event["payload_json"])}) != row["source_hash"]:
        return False
    expected_interaction = (json.loads(event["payload_json"]) or {}).get("interactionId") if event["event_type"] == "task.interaction.waiting" else None
    expected_kind = "trusted_interaction" if expected_interaction else "public_status"
    if (row["protocol"] != MODE or row["kind"] != expected_kind or row["interaction_id"] != expected_interaction
            or row["public_text"] != PUBLIC.get(event["event_type"])):
        return False
    if row["kind"] == "trusted_interaction":
        if "trusted_interaction" not in json.loads(identity[0]["capabilities_json"]):
            return False
        return bool(db.execute("""SELECT 1 FROM interactions i JOIN task_interactions l USING(interaction_id)
            JOIN agent_tasks t ON t.task_id=l.task_id AND t.user_subject=l.user_subject
            WHERE i.interaction_id=? AND i.user_subject=? AND l.user_subject=i.user_subject AND l.task_id=?
              AND t.current_interaction_id=i.interaction_id AND l.last_state IN ('pending','processing')
              AND t.status IN ('active','running','waiting_user')
              AND julianday(i.expires_at)>julianday(?)""",
            (row["interaction_id"],subject,row["task_id"],actions._now().isoformat())).fetchone())
    return True


def _covered(service,db,row):
    """Suppress only a proven same destination/fact responsibility, never another task."""
    if row["kind"] != "public_status":
        return None
    if row["observation_hash"]:
        prior=db.execute("""SELECT delivery_id FROM telegram_notification_deliveries
            WHERE subject=? AND task_id=? AND endpoint_id=? AND token_id=? AND pair_proof=? AND endpoint_hash=?
              AND observation_hash=? AND rowid<(SELECT rowid FROM telegram_notification_deliveries WHERE delivery_id=?)
            ORDER BY rowid LIMIT 1""",(row["subject"],row["task_id"],row["endpoint_id"],row["token_id"],row["pair_proof"],
                row["endpoint_hash"],row["observation_hash"],row["delivery_id"])).fetchone()
        if prior:
            return prior["delivery_id"]
    event=db.execute("SELECT event_type FROM task_events WHERE event_id=?",(row["event_id"],)).fetchone()
    status={"task.completed":"succeeded","task.operation.succeeded":"succeeded",
            "task.failed":"failed","task.operation.failed":"failed","task.canceled":"canceled",
            "task.operation.outcome_unknown":"outcome_unknown"}.get(event["event_type"] if event else None)
    task=db.execute("SELECT * FROM agent_tasks WHERE task_id=? AND user_subject=?",(row["task_id"],row["subject"])).fetchone()
    if not status or not task or task["status"] != status:
        return None
    from . import workspace_query_delivery as query, workspace_text_delivery as text
    def same(pin):
        return all(pin[k]==row[k] for k in ("subject","source","host","instance","endpoint_id","token_id","pair_proof","endpoint_hash"))
    query_row=db.execute(query._JOIN+" WHERE d.task_id=? AND p.endpoint_id=?",(row["task_id"],row["endpoint_id"])).fetchone()
    if query_row and same(query_row) and query_row["state"] in {"pending","leased","retry_wait","sending","sent","unknown"}:
        try:
            query.current(db,query_row)
            return query_row["delivery_id"]
        except actions.ActionableResultError:
            pass
    # An already admitted original public answer/status can own the same terminal.
    dispatch_state={"succeeded":"completed","failed":"failed","canceled":"canceled","outcome_unknown":"result_unknown"}[status]
    dispatch=db.execute("""SELECT dispatch_id FROM agent_host_dispatches WHERE user_subject=? AND agent_host=?
        AND origin_endpoint_id=? AND conversation_ref=? AND accepted_run_id=? AND state=? LIMIT 2""",
        (row["subject"],row["host"],row["source"],task["active_conversation_ref"],task["host_task_key"],dispatch_state)).fetchall()
    if len(dispatch)!=1:
        return None
    parts=db.execute(text._JOIN+" WHERE d.request_id=? AND p.endpoint_id=? AND d.kind=? ORDER BY d.part_index",
                     (dispatch[0][0],row["endpoint_id"],"assistant" if status=="succeeded" else "status")).fetchall()
    if (parts and len(parts)==parts[0]["part_count"] and [p["part_index"] for p in parts]==list(range(1,len(parts)+1))
            and all(same(p) and p["state"] in {"pending","leased","retry_wait","sending","sent","unknown"}
                    and text._current(service,db,p) for p in parts)):
        return parts[0]["delivery_id"]
    return None


def _mark_covered(service,db,row):
    covered=_covered(service,db,row)
    if covered:
        db.execute("UPDATE telegram_notification_deliveries SET state='covered',covered_by=? WHERE delivery_id=?",
                   (covered,row["delivery_id"]))
    return bool(covered)


def claim(service, subject, host, instance, endpoint, token, limit, lease):
    now = actions._now()
    with store_transaction(service.tasks) as db:
        rows = db.execute("""SELECT * FROM telegram_notification_deliveries WHERE subject=? AND host=? AND instance=?
            AND endpoint_key=? AND token_id=? AND protocol=? AND state IN ('pending','leased','retry_wait')
            AND due<=? ORDER BY created_at,rowid LIMIT ?""",
            (subject,host,instance_key(host,instance)[1],endpoint,token,MODE,now.isoformat(),max(1,min(limit,100)))).fetchall()
        result = []
        for row in rows:
            if not registered(db,subject,host,(host,row["instance"]),token):
                continue
            state = "expired" if row["attempt"] >= 5 or now >= actions._date(row["expires_at"]) else (
                None if _current(db,row) else "revoked")
            if state:
                db.execute("UPDATE telegram_notification_deliveries SET state=? WHERE delivery_id=?",(state,row["delivery_id"]))
                continue
            if _mark_covered(service,db,row):
                continue
            db.execute("""UPDATE telegram_notification_deliveries SET state='leased',attempt=attempt+1,issued=0,due=?
                WHERE delivery_id=?""",((now+timedelta(seconds=max(5,min(300,lease)))).isoformat(),row["delivery_id"]))
            result.append({"deliveryMode":MODE,"deliveryId":row["delivery_id"],"attemptCount":row["attempt"]+1,
                "sourceEventId":row["event_id"],"sourceTaskId":row["task_id"],"notificationKind":row["kind"],"pairingRef":row["pair_proof"]})
        return result


def _eligible(db,row):
    now=actions._now()
    return (row["state"] == "leased" and now < actions._date(row["due"]) and now < actions._date(row["expires_at"])
            and registered(db,row["subject"],row["host"],(row["host"],row["instance"]),row["token_id"]) and _current(db,row))


def handle(service, *, subject, host, instance, endpoint_key, token_id, pairing_ref, delivery_id, attempt_count,
           action, outcome=None, receipt=None, retry_after_seconds=None):
    refused={"issue":"available","begin":"allowed","ack":"recorded"}
    if action not in refused:
        raise ValueError("Unsupported notification action")
    denied={refused[action]:False}
    with store_transaction(service.tasks) as db:
        row=db.execute("SELECT * FROM telegram_notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        if not row or (row["subject"],row["host"],row["instance"],row["endpoint_key"],row["token_id"],row["pair_proof"],row["attempt"]) != (
                subject,host,instance_key(host,instance)[1],endpoint_key,token_id,pairing_ref,attempt_count):
            return denied
        if action == "ack":
            if row["state"] == "covered":
                return {"recorded":outcome in {"unavailable","delivery_unknown"} and receipt is None and retry_after_seconds is None}
            # Exact original credentials can reconcile after revocation; this never grants a send.
            return {"recorded":settle(db,row,outcome,receipt,retry_after_seconds,table=TABLE)}
        if any(x is not None for x in (outcome,receipt,retry_after_seconds)) or not _eligible(db,row):
            return denied
        if _mark_covered(service,db,row):
            return denied
        if action == "begin" and not row["issued"]:
            return denied
        snapshot=dict(row)
    reply={"available":True,"leaseExpiresAt":snapshot["due"],"sourceEventId":snapshot["event_id"],
           "sourceTaskId":snapshot["task_id"],"notificationKind":snapshot["kind"]}
    if snapshot["kind"] == "public_status":
        reply["publicText"]=snapshot["public_text"]
    else:
        # Presentation services own their writer transactions and all existing card governance.
        from agentbridge.core.interactions import InteractionIntegrityError
        try:
            record,resource,interaction=service._load_interaction(user_subject=subject,interaction_id=snapshot["interaction_id"])
            endpoint=service.tasks.endpoint_for_key(user_subject=subject,agent_host=host,endpoint_key=endpoint_key)
            if "trusted_interaction" not in endpoint["capabilities"] or interaction["state"] not in {"pending","processing"}:
                return denied
            if action == "issue":
                reply["interaction"]=service._present_interaction_for_endpoint(record=record,resource=resource,interaction=interaction,endpoint=endpoint)
        except (KeyError,PermissionError,InteractionIntegrityError):
            return denied
    with store_transaction(service.tasks) as db:
        current=db.execute("SELECT * FROM telegram_notification_deliveries WHERE delivery_id=?",(delivery_id,)).fetchone()
        if current["attempt"] != attempt_count or current["due"] != snapshot["due"] or not _eligible(db,current):
            return denied
        if _mark_covered(service,db,current):
            return denied
        if action == "begin":
            if not current["issued"]:
                return denied
            db.execute("UPDATE telegram_notification_deliveries SET state='sending' WHERE delivery_id=?",(delivery_id,))
            return {"allowed":True}
        db.execute("UPDATE telegram_notification_deliveries SET issued=1 WHERE delivery_id=?",(delivery_id,))
    return reply
