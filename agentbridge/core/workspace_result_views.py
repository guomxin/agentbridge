"""Issue a Telegram selection card for one admitted Pi Workspace query and current private pairing."""
import json
from uuid import uuid4

from agentbridge.adapters.seeyon_pending_verification import _consistent_complete_collection

from . import actionable_results as actions
from .endpoint_instances import endpoint_instances
from .store_transactions import store_transaction

DELIVERY = "workspace_result_view"


def enqueue(db, entry, original, run_id, endpoint_id, proof):
    subject, source = entry["user_subject"], entry["source_endpoint_id"]
    row = db.execute("""SELECT t.task_id, o.operation_id, o.result_json, o.finished_at FROM agent_tasks t
        JOIN operations o ON o.operation_id=t.current_operation_id AND o.user_subject=t.user_subject
        JOIN task_operations l ON l.task_id=t.task_id AND l.operation_id=o.operation_id AND l.user_subject=t.user_subject
        WHERE t.user_subject=? AND t.agent_host='pi' AND t.host_task_key=? AND t.origin_endpoint_id=?
        AND t.active_conversation_ref=? AND t.status='succeeded' AND o.capability_name='oa.workflow.pending.list'
        AND o.status='succeeded' AND json_extract(o.result_json,'$.coverage.status')='complete'
        AND NOT EXISTS (SELECT 1 FROM task_operations other WHERE other.task_id=t.task_id
          AND other.operation_id!=o.operation_id)
        AND json_type(o.result_json,'$.items')='array' AND json_array_length(o.result_json,'$.items') BETWEEN 1 AND 100""",
        (subject, run_id, source, original["conversation_ref"])).fetchone()
    if not row:
        return
    pending = json.loads(row["result_json"])
    items = pending["items"]
    if (pending["coverage"].get("hasMore") not in (None, False)
            or any(not isinstance(item, dict) for item in items)
            or not _consistent_complete_collection(pending, items)):
        return
    payload = {"pairProof": proof, "sourceEndpointId": source, "questionId": original["entry_id"],
               "dispatchId": original["dispatch_id"], "operationId": row["operation_id"],
               "sourceHash": actions._hash(pending), "sourceFinishedAt": row["finished_at"]}
    db.execute("""INSERT OR IGNORE INTO notification_outbox
        (delivery_id,event_id,task_id,endpoint_id,user_subject,payload_type,payload_json,state,
         attempt_count,next_attempt_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',0,?,?,?)""",
        (str(uuid4()), entry["entry_id"], row["task_id"], endpoint_id, subject, DELIVERY, json.dumps(payload),
         entry["created_at"], entry["created_at"], entry["created_at"]))


def checked_source(db, subject, target, source_task_id, delivery_id):
    from agentbridge.workspace.telegram_pilot_links import mirror_binding, mirror_pair_proof, mirror_sender_ids

    row = db.execute("""SELECT d.payload_json, o.result_json, o.finished_at FROM notification_outbox d
        JOIN user_timeline reply ON reply.entry_id=d.event_id AND reply.user_subject=d.user_subject AND reply.role='assistant'
        JOIN agent_tasks t ON t.task_id=d.task_id AND t.user_subject=d.user_subject AND t.agent_host='pi'
        JOIN operations o ON o.operation_id=t.current_operation_id AND o.user_subject=t.user_subject
          AND o.capability_name='oa.workflow.pending.list' AND o.status='succeeded'
        JOIN task_operations l ON l.task_id=t.task_id AND l.user_subject=t.user_subject AND l.operation_id=o.operation_id
        JOIN agent_host_dispatches h ON h.dispatch_id=json_extract(d.payload_json,'$.dispatchId') AND h.agent_host=t.agent_host
          AND h.user_subject=t.user_subject AND h.origin_endpoint_id=t.origin_endpoint_id
          AND h.conversation_ref=t.active_conversation_ref AND h.accepted_run_id=t.host_task_key
        JOIN user_timeline q ON q.entry_id=json_extract(d.payload_json,'$.questionId') AND q.user_subject=t.user_subject
          AND q.dedupe_key='message:'||h.message_key AND q.source_endpoint_id=h.origin_endpoint_id AND q.role='user'
        WHERE d.delivery_id=? AND d.user_subject=? AND d.task_id=? AND d.endpoint_id=? AND d.payload_type=?
          AND t.status='succeeded' AND reply.source_endpoint_id=h.origin_endpoint_id
          AND NOT EXISTS(SELECT 1 FROM task_operations other WHERE other.task_id=t.task_id AND other.operation_id!=o.operation_id)
          AND reply.dedupe_key='message:workspace:assistant:'||h.accepted_run_id
          AND json_extract(d.payload_json,'$.sourceEndpointId')=h.origin_endpoint_id
          AND t.current_operation_id=json_extract(d.payload_json,'$.operationId')
          AND EXISTS(SELECT 1 FROM notification_outbox m WHERE m.event_id=q.entry_id
            AND m.user_subject=d.user_subject AND m.endpoint_id=d.endpoint_id AND m.payload_type='timeline_message'
            AND json_extract(m.payload_json,'$.pairProof')=json_extract(d.payload_json,'$.pairProof')
            AND json_extract(m.payload_json,'$.mirrorKind')='pi_workspace_text_v1')
          AND EXISTS(SELECT 1 FROM notification_outbox m WHERE m.event_id=reply.entry_id
            AND m.user_subject=d.user_subject AND m.endpoint_id=d.endpoint_id AND m.payload_type='timeline_message'
            AND json_extract(m.payload_json,'$.pairProof')=json_extract(d.payload_json,'$.pairProof')
            AND json_extract(m.payload_json,'$.mirrorKind')='pi_workspace_text_v1')""",
        (delivery_id, subject, source_task_id, target["endpoint_id"], DELIVERY)).fetchone()
    if not row or target["agent_host"] != "pi" or not target.get("_instance_id"):
        actions._fail("ACTION_ENDPOINT_INVALID", "工作台原查询与本人私聊不一致。")
    payload = json.loads(row[0])
    if (payload.get("sourceFinishedAt") != row["finished_at"]
            or actions._hash(json.loads(row["result_json"])) != payload.get("sourceHash")):
        actions._fail("ACTION_SOURCE_CHANGED", "原待办查询内容或时间已变化，请重新查询。")
    binding = mirror_binding(db, subject, target["_instance_id"], endpoint_instances(db, subject))
    sender = target["external_subject"]
    if (not binding or binding["host_type"] != "pi" or binding["endpoint_id"] != payload["sourceEndpointId"]
            or binding["bot"] != target["account_id"] or mirror_sender_ids(db, subject, binding) != [sender]
            or mirror_pair_proof(db, subject, binding, sender) != payload["pairProof"]):
        actions._fail("ACTION_ENDPOINT_INVALID", "原工作台或 Telegram 配对已变化。")
    return {**payload, "deliveryId": delivery_id}


def check_command_pair(db, subject, endpoint, delivery_id):
    original = db.execute("""SELECT task_id FROM notification_outbox WHERE delivery_id=?
        AND user_subject=? AND endpoint_id=? AND payload_type=?""",
        (delivery_id, subject, endpoint["endpoint_id"], DELIVERY)).fetchone()
    if not original:
        actions._fail("ACTION_ENDPOINT_INVALID", "原查询投递与本人私聊不一致。")
    checked_source(db, subject, endpoint, original["task_id"], delivery_id)


def direct_source(db, subject, endpoint, task):
    if (task["agent_host"] != endpoint["agent_host"] or task["origin_endpoint_id"] != endpoint["endpoint_id"]
            or task["active_conversation_ref"] != endpoint["conversation_ref"]):
        actions._fail("ACTION_ENDPOINT_INVALID", "办理请求必须来自原 Telegram 私聊。")
    # A read explicitly refreshed from a Workspace ticket keeps the original pairing generation.
    command = json.loads(task["summary_json"]).get("command") or {}
    if "workspaceResultDeliveryId" in command:
        actions._load_command(db, subject, task["task_id"], command.get("commandKey"))
        return {"deliveryId": command["workspaceResultDeliveryId"]}
    return None


def validate_ticket_source(db, subject, endpoint, source_task_id, view_hash):
    """Recheck the signed ticket and current pairing inside the write transaction."""
    record = db.execute("""SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=?
        AND event_type='task.result_view.issued' AND causation_ref=?""", (source_task_id, subject, view_hash)).fetchone()
    if not record:
        actions._fail("ACTION_ENDPOINT_INVALID", "办理入口与原查询不一致。")
    payload = json.loads(record[0])
    if (payload.get("tokenHash") != view_hash or payload.get("endpointBindingHash") != endpoint["_binding_hash"]
            or payload.get("endpointId") != endpoint["endpoint_id"] or actions._date(payload["expiresAt"]) <= actions._now()):
        actions._fail("ACTION_ENDPOINT_INVALID", "原会话或办理入口已变化。")
    if "workspaceDeliveryId" in payload:
        return checked_source(db, subject, endpoint, source_task_id, payload["workspaceDeliveryId"])
    task = actions._owned_task(db, subject, source_task_id)
    return direct_source(db, subject, endpoint, task)


def delivery(service, *, subject, instance, endpoint_key, token_id, pairing_ref, delivery_id, attempt_count,
             action, outcome=None, receipt=None):
    from .telegram_result_views import ResultViewService
    if action == "issue":
        valid = outcome is None and receipt is None
    elif action == "ack" and outcome == "sent":
        valid = type(receipt) is int and 0 < receipt < 2**53
    else:
        valid = action == "ack" and outcome in {"unavailable", "delivery_unknown"} and receipt is None
    if not valid:
        raise ValueError("Invalid Workspace result-view operation")
    unavailable = {"available": False} if action == "issue" else {"recorded": False}
    with store_transaction(service.tasks) as db:
        row = db.execute("""SELECT d.*,e.endpoint_key FROM notification_outbox d JOIN client_endpoints e
            ON e.endpoint_id=d.endpoint_id AND e.user_subject=d.user_subject
            WHERE d.delivery_id=? AND d.user_subject=? AND d.payload_type=? AND e.endpoint_key=?""",
            (delivery_id, subject, DELIVERY, endpoint_key)).fetchone()
        if not row or row["attempt_count"] != attempt_count:
            return unavailable
        try:
            target = actions._telegram_endpoint(db, subject, row["endpoint_id"])
            proof = checked_source(db, subject, target, row["task_id"], delivery_id)
        except actions.ActionableResultError:
            return unavailable
        if (target["_instance_id"] != instance or target["token_id"] != token_id or proof["pairProof"] != pairing_ref):
            return unavailable
        if row["state"] in {"acknowledged", "failed"}:
            payload = json.loads(row["payload_json"])
            if action == "ack" and payload.get("outcome") == outcome and payload.get("telegramReceipt") == receipt:
                return {"recorded": True}
            return unavailable
        if row["state"] != "delivering" or actions._date(row["next_attempt_at"]) <= actions._now():
            return unavailable
        if action == "issue":
            try:
                issued = ResultViewService(service).issue(subject, row["task_id"], row["endpoint_id"],
                    {"endpointId":row["endpoint_id"], "tokenId":token_id}, workspace_delivery_id=delivery_id, connection=db)
            except (actions.ActionableResultError, PermissionError):
                return unavailable
            return {"available": True, "resultView": issued["resultView"], "leaseExpiresAt": row["next_attempt_at"]}
        if outcome != "unavailable" and not db.execute("""SELECT 1 FROM task_events
            WHERE task_id=? AND user_subject=? AND event_type='task.result_view.issued'
            AND json_extract(payload_json,'$.workspaceDeliveryId')=?
            AND json_extract(payload_json,'$.endpointBindingHash')=?""",
            (row["task_id"], subject, delivery_id, target["_binding_hash"])).fetchone():
            return unavailable
        # No unknown Bot send is eligible for another attempt.
        now = actions._now().isoformat()
        state = "acknowledged" if outcome == "sent" else "failed"
        db.execute("""UPDATE notification_outbox SET state=?,updated_at=?,acknowledged_at=?,
            payload_json=json_set(payload_json,'$.outcome',?,'$.telegramReceipt',?) WHERE delivery_id=?""",
            (state, now, now if state == "acknowledged" else None, outcome, receipt, delivery_id))
        return {"recorded": True}
