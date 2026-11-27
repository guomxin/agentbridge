"""Durable receipts for public history from an already registered private Telegram source."""
import hashlib
import json

from . import actionable_results as actions
from . import workspace_query_delivery as delivery
from .chat_mirror import PUBLIC_TEXT_MAX_CHARACTERS, public_mirror_text
from .endpoint_instances import endpoint_instances, instance_key
from .store_transactions import store_transaction


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS telegram_history_receipts(
        subject TEXT NOT NULL, message_key TEXT NOT NULL, entry_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL, authority TEXT NOT NULL, role TEXT NOT NULL,
        text_sha TEXT NOT NULL, task_id TEXT, scope_json TEXT, PRIMARY KEY(subject,message_key))""")
    if "scope_json" not in {r[1] for r in db.execute("PRAGMA table_info(telegram_history_receipts)")}:
        db.execute("ALTER TABLE telegram_history_receipts ADD COLUMN scope_json TEXT")


def _source(db, subject, host, instance, token, endpoint_key, sender, account, conversation, route):
    scope = instance_key(host, instance)
    if not instance or not token or not delivery.registered(db, subject, host, scope, token):
        raise PermissionError("History requires the registered private host")
    row = db.execute("SELECT endpoint_id FROM client_endpoints WHERE agent_host=? AND endpoint_key=? AND user_subject=?",
                     (host, endpoint_key, subject)).fetchone()
    if not row:
        raise PermissionError("No original private source")
    endpoint = actions._telegram_endpoint(db, subject, row[0])
    ownership = endpoint_instances(db, subject)
    if (ownership.get(row[0]) != scope or endpoint["token_id"] != token or endpoint["external_subject"] != sender
            or endpoint["conversation_ref"] != conversation or (endpoint["account_id"] or "default") != (account or "default")
            or not isinstance(route, dict) or set(route) - {"channel", "to", "accountId", "threadId"}
            or route.get("channel") != "telegram" or str(route.get("to", "")).removeprefix("telegram:") != sender
            or (route.get("accountId") or "default") != (account or "default") or route.get("threadId") not in (None, "")):
        raise PermissionError("History does not match its original private source")
    if host == "openclaw":
        return endpoint, endpoint["_binding_hash"]
    from agentbridge.workspace.telegram_pilot_links import mirror_binding, mirror_pair_proof
    binding = mirror_binding(db, subject, instance, ownership)
    proof = mirror_pair_proof(db, subject, binding, sender) if binding and binding["bot"] == account else None
    if not proof:
        raise PermissionError("Original private pairing is unavailable")
    return endpoint, proof


def append(service, *, subject, host, instance, token, endpoint_key, sender, account, conversation,
           route, message_key, role, text, task_id, receipt_only=False, pairing_protocol=None,
           source_message_id=None, received_at=None, pairing_ref=None):
    from .task_projections import _endpoint_from_row
    from .central_service import endpoint_response, timeline_entry_response
    if not isinstance(message_key, str) or message_key != message_key.strip() or not message_key or len(message_key) > 768:
        raise ValueError("Invalid private history key")
    public = public_mirror_text(text, maximum=PUBLIC_TEXT_MAX_CHARACTERS)
    if not public or role not in {"assistant", "user"}:
        raise ValueError("Only public conversation text can be projected")
    digest = hashlib.sha256(public.encode()).hexdigest()
    original_scope=json.dumps([subject,host,instance,token,endpoint_key,sender,account,conversation,route,source_message_id,received_at,pairing_ref],sort_keys=True,separators=(",",":"))
    with store_transaction(service.tasks) as db:
        previous=db.execute("SELECT * FROM telegram_history_receipts WHERE subject=? AND message_key=?",(subject,message_key)).fetchone()
        if receipt_only and not previous:
            return {"status":"unavailable","recorded":False}
        if receipt_only and previous["scope_json"] is not None:
            if (not delivery.registered(db,subject,host,instance_key(host,instance),token)
                    or previous["scope_json"]!=original_scope
                    or (previous["role"],previous["text_sha"],previous["task_id"])!=(role,digest,task_id)):
                raise PermissionError("Original history receipt identity or payload changed")
            return {"protocolVersion":"0.1","status":"succeeded","recorded":True,
                    "endpoint":{"endpointId":previous["endpoint_id"]},
                    "entry":{"entryId":previous["entry_id"],"role":role,"retained":False},
                    "reused":{"entry":True,"endpoint":True},"reactivatedDeliveries":0,
                    "projectionReceipt":{"entryId":previous["entry_id"],"messageKey":message_key,"textSha256":digest,
                        "sourceEndpointId":previous["endpoint_id"],"role":role}}
        from agentbridge.workspace.telegram_pair_migration import permits_delivery
        if not permits_delivery(db,host,subject,instance,endpoint_key,token,pairing_protocol):
            raise PermissionError("Current private history protocol required")
        endpoint, proof = _source(db, subject, host, instance, token, endpoint_key, sender, account, conversation, route)
        endpoint_id = endpoint["endpoint_id"]
        if not previous and host in {'nanobot', 'pi'}:
            from agentbridge.workspace.telegram_pair_migration import reception_boundary
            boundary = reception_boundary(db, subject, instance, account, sender, token)
            if boundary['generationScoped']:
                from .telegram_media_receipts import _received_time
                if type(source_message_id) is not int or not 0 < source_message_id < 2**53:
                    raise PermissionError("Original platform source is required for first history")
                stamp = _received_time(received_at)
                original = db.execute("""SELECT endpoint_id,token_id,pair_proof,received_at FROM telegram_media_sources
                    WHERE subject=? AND host=? AND instance=? AND endpoint_key=? AND source_message_id=?""",
                    (subject,host,instance,endpoint_key,source_message_id)).fetchone()
                if (not original or tuple(original) != (endpoint_id,token,proof,received_at) or pairing_ref != proof
                        or stamp.microsecond or stamp.timestamp() <= boundary['receptionFloor']):
                    raise PermissionError("Original history source does not prove the current grant")
        if task_id and not db.execute("""SELECT 1 FROM agent_tasks WHERE task_id=? AND user_subject=?
                AND origin_endpoint_id=? AND agent_host=? AND active_conversation_ref=?""",
                (task_id, subject, endpoint_id, host, conversation)).fetchone():
            raise PermissionError("History task belongs to another source")
        previous = db.execute("SELECT * FROM telegram_history_receipts WHERE subject=? AND message_key=?",
                              (subject, message_key)).fetchone()
        reused = bool(previous)
        if previous:
            facts = tuple(previous[k] for k in ("endpoint_id", "authority", "role", "text_sha", "task_id"))
            if (facts != (endpoint_id, proof, role, digest, task_id)
                    or previous["scope_json"] not in (None,original_scope)):
                raise ValueError("Original history identity or payload changed")
            entry_id = previous["entry_id"]
            row = db.execute("SELECT * FROM user_timeline WHERE entry_id=? AND user_subject=?", (entry_id,subject)).fetchone()
            if (row and row["source_endpoint_id"] == endpoint_id and row["role"] == role
                    and hashlib.sha256(row["text"].encode()).hexdigest() == digest):
                from .task_projections import _timeline_from_row
                entry = timeline_entry_response(_timeline_from_row(row))
            else:
                entry = {"entryId": entry_id, "role": role, "retained": False}
        else:
            source, reused = service.tasks._append_timeline_message_in_connection(db, user_subject=subject,
                source_endpoint_id=endpoint_id, message_key=message_key, role=role, text=public, task_id=task_id,
                _telegram_projection=True, enqueue_notifications=False)
            if (source["source_endpoint_id"], source["role"], source["text"], source["task_id"]) != (endpoint_id,role,public,task_id):
                raise ValueError("Existing history has a different source or payload")
            entry_id = source["entry_id"]
            db.execute("INSERT INTO telegram_history_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                       (subject,message_key,entry_id,endpoint_id,proof,role,digest,task_id,original_scope))
            entry = timeline_entry_response(source)
        # The result is returned to the transport only after the transaction exits.
        raw = db.execute("SELECT * FROM client_endpoints WHERE endpoint_id=?", (endpoint_id,)).fetchone()
        record = endpoint_response(_endpoint_from_row(raw))
    return {"protocolVersion":"0.1", "status":"succeeded", "entry":entry, "endpoint":record,
            "reused":{"entry":reused, "endpoint":True}, "reactivatedDeliveries":0,
            "projectionReceipt":{"entryId":entry_id, "messageKey":message_key, "textSha256":digest,
                                 "sourceEndpointId":endpoint_id, "role":role}}
