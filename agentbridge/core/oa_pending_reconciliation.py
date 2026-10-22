"""Retire unused single-item entries only after an authoritative OA completion.

Intent keys deliberately remain independent. This is post-success housekeeping,
never authorization, cancellation of an execution, or a reason to replay a write.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json

from agentbridge.adapters.seeyon_pending_verification import VERIFICATION_METHOD
from agentbridge.adapters.seeyon_meeting_reply import VERSION as MEETING_VERSION
from agentbridge.core.business_draft_profiles import BY_PREPARE
from agentbridge.core.field_submissions import FieldSubmissionStateError, _submission_from_row
from agentbridge.core.interactions import _interaction_from_row, build_interaction_envelope
from agentbridge.core.write_authorizations import WriteAuthorizationStateError, _authorization_from_row
from agentbridge.core.write_catalog import _TRUSTED_WRITE_COMMITS


class _EntryChanged(Exception):
    """Another local decision or task transition won the transaction race."""


def _plan(row):
    encoded = str(row["plan_json"])
    if not hmac.compare_digest(str(row["plan_hash"]), "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()):
        raise ValueError("OA pending authorization integrity mismatch")
    value = json.loads(encoded)
    return value if isinstance(value, dict) else {}


def _completion_kind(capability, profile, definition):
    """Select business completion proof independently of card presentation."""
    if not profile or definition.get("preflight_profile") != profile.review_kind:
        return None
    preflight = definition.get("preflight_function")
    if capability == "oa.meeting.reply" and preflight == "preflight_meeting_reply":
        return "meeting"
    if preflight in {"preflight_pending_action", "preflight_missed_punch_approval"}:
        return "collaboration"
    return None


def _scope(service, operation):
    if operation.get("status") != "succeeded":
        return None
    capability = operation.get("capability_name")
    registered = _TRUSTED_WRITE_COMMITS.get(capability)
    if registered is None:
        return None
    prepare, definition = registered
    profile = BY_PREPARE.get(prepare)
    kind = _completion_kind(capability, profile, definition)
    if kind is None:
        return None
    meeting = kind == "meeting"
    result = operation.get("result") or {}
    verification = result.get("verification") or {}
    if (not isinstance(verification, dict)
            or verification.get("confirmed") is not True
            or (not meeting and (verification.get("method") != VERIFICATION_METHOD
                or verification.get("pending_absence_confirmed") is not True
                or verification.get("done_identity_confirmed") is not True))):
        return None
    with service.tasks._connect() as connection:
        rows = connection.execute(
            "SELECT * FROM write_authorizations WHERE commit_operation_id=? AND user_subject=? "
            "AND system_id='oa' AND capability_name=? AND state='consumed'",
            (operation["operation_id"], operation["user_subject"], capability),
        ).fetchall()
    if len(rows) != 1:
        return None
    authorization = rows[0]
    plan = _plan(authorization)
    target, binding = plan.get("target") or {}, plan.get("session_binding") or {}
    affair = target.get("affair_id")
    if (not isinstance(affair, str) or not affair
            or plan.get("user_subject") != operation["user_subject"]
            or binding.get("session_id") != authorization["session_id"]
            or plan.get("resume_arguments", {}).get("affair_id") != affair):
        return None
    contract = plan.get("action_contract") or {}
    if meeting:
        reply = target.get("reply") or {}
        attitude = plan.get("exact_input", {}).get("attitude")
        if (plan.get("schema_version") != "agentbridge.oa_meeting_reply_plan.v1"
                or contract != {"version": MEETING_VERSION, "verification": "exact_meeting_and_my_reply"}
                or reply.get("feedbackFlag") != -100
                or not target.get("meeting_id") or not reply.get("id") or not reply.get("userId")
                or reply.get("meetingId") != target["meeting_id"]
                or result.get("submitted") is not True or result.get("verified") is not True
                or verification.get("method") != contract["verification"]
                or verification.get("meeting_id") != target["meeting_id"]
                or verification.get("reply_id") != reply["id"]
                or attitude not in {"join", "not_join", "pending"}
                or verification.get("attitude") != attitude
                or result.get("summary") != {"meeting_reply": attitude, "submitted_count": 1}):
            return None
    elif (not target.get("summary_id") or not target.get("process_id")
            or result.get("target", {}).get("affair_id") != affair
            or contract.get("action_kind") != profile.action_kind
            or contract.get("verification") != VERIFICATION_METHOD
            or target.get("profile", profile.review_kind) != profile.review_kind):
        return None
    return {"subject": operation["user_subject"], "session": authorization["session_id"],
            "capability": capability, "affair": affair, "profile": profile.review_kind,
            "binding": binding, "operation": operation["operation_id"],
            "meeting_target": target if meeting else None}


def _eligible(service, connection, candidate, scope, winner_task_id):
    task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=? AND user_subject=?",
                              (candidate["task_id"], scope["subject"])).fetchone()
    if (not task or task["task_id"] == winner_task_id or task["status"] != "waiting_user"
            or task["current_interaction_id"] != candidate["interaction_id"]):
        raise _EntryChanged()
    # Never terminate the rest of a plan or batch, including one which has paused.
    for table, column in (("task_batches", "parent_task_id"), ("task_plans", "parent_task_id"),
                          ("business_draft_decisions", "task_id")):
        if connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (task["task_id"],)).fetchone():
            raise _EntryChanged()
    if connection.execute(
        "SELECT 1 FROM write_authorizations a JOIN task_operations t ON t.operation_id=a.prepare_operation_id "
        "WHERE t.task_id=? AND a.user_subject=? AND a.state IN ('approved','consumed')",
        (task["task_id"], scope["subject"]),
    ).fetchone():
        raise _EntryChanged()
    if connection.execute(
        "SELECT 1 FROM operations o JOIN task_operations t ON t.operation_id=o.operation_id "
        "WHERE t.task_id=? AND o.status IN ('pending','running','unknown')", (task["task_id"],),
    ).fetchone():
        raise _EntryChanged()
    record = _interaction_from_row(service.interactions._select(connection, candidate["interaction_id"]))
    if (record["user_subject"] != scope["subject"] or record["system_id"] != "oa"
            or record["session_id"] != scope["session"]):
        raise _EntryChanged()
    field = record["interaction_type"] == "business_input"
    table, key = ("field_submissions", "submission_id") if field else ("write_authorizations", "authorization_id")
    row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (record["resource_id"],)).fetchone()
    if (not row or row["state"] != "pending" or row["user_subject"] != scope["subject"]
            or row["system_id"] != "oa" or row["session_id"] != scope["session"]
            or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)):
        raise _EntryChanged()
    if field:
        service.field_submissions._verify_integrity(row, include_values=False)
        resource = _submission_from_row(row, include_values=False)
        profile = BY_PREPARE.get(resource["capability_name"])
        if (not profile or profile.commit != scope["capability"] or profile.review_kind != scope["profile"]
                or resource["form_schema"].get("_agentbridge_resume_arguments") != {"affair_id": scope["affair"]}):
            raise _EntryChanged()
    else:
        plan = _plan(row)
        resource = _authorization_from_row(row, include_plan=False)
        if (resource["capability_name"] != scope["capability"]
                or plan.get("target", {}).get("affair_id") != scope["affair"]
                or plan.get("session_binding") != scope["binding"]
                or (not scope["meeting_target"] and plan.get("action_contract", {}).get("action_kind") != BY_PREPARE[
                    _TRUSTED_WRITE_COMMITS[scope["capability"]][0]].action_kind)):
            raise _EntryChanged()
        if scope["meeting_target"] and (
                plan.get("action_contract") != {"version": MEETING_VERSION, "verification": "exact_meeting_and_my_reply"}
                or plan.get("target") != scope["meeting_target"]):
            raise _EntryChanged()
    return record, resource


def reconcile_completed_pending_intents(service, operation, *, task_id=None):
    """Use pending-only store transactions; callers keep cleanup failures diagnostic."""
    scope = _scope(service, operation)
    if scope is None:
        return []
    with service.tasks._connect() as connection:
        candidates = [dict(row) for row in connection.execute(
            "SELECT t.task_id,i.interaction_id,i.interaction_type,i.resource_id FROM agent_tasks t "
            "JOIN interactions i ON i.interaction_id=t.current_interaction_id "
            "WHERE t.user_subject=? AND t.status='waiting_user' AND i.user_subject=? "
            "AND i.system_id='oa' AND i.session_id=? AND i.interaction_type IN ('business_input','execution_authorization')",
            (scope["subject"], scope["subject"], scope["session"]),
        )]
    retired = []
    for candidate in candidates:
        def retire(connection):
            record, resource = _eligible(service, connection, candidate, scope, task_id)
            now = datetime.now(timezone.utc).isoformat()
            draft = connection.execute(
                "SELECT draft_id FROM business_drafts WHERE source_submission_id=? AND user_subject=?",
                (record["resource_id"], scope["subject"]),
            ).fetchone() if record["interaction_type"] == "business_input" else None
            if draft:
                connection.execute("UPDATE business_drafts SET state='expired',values_json='{}',preview_id=NULL,"
                                   "version=version+1,updated_at=? WHERE draft_id=?", (now, draft["draft_id"]))
                connection.execute("DELETE FROM business_draft_previews WHERE draft_id=?", (draft["draft_id"],))
                connection.execute("DELETE FROM business_draft_options WHERE draft_id=?", (draft["draft_id"],))
            envelope = build_interaction_envelope(record, {**resource, "state": "superseded"})
            service.tasks.link_interaction(task_id=candidate["task_id"], user_subject=scope["subject"],
                                           interaction_record=record, interaction=envelope, connection=connection)
            service.tasks._append_event(connection, task_id=candidate["task_id"], user_subject=scope["subject"],
                event_type="task.pending_intent.superseded",
                payload={"reason": "same_oa_affair_verified_in_another_task", "completedTaskId": task_id,
                         "completedOperationId": scope["operation"], "interactionId": record["interaction_id"]},
                causation_ref=scope["operation"], created_at=now)
        store = service.field_submissions if candidate["interaction_type"] == "business_input" else service.write_authorizations
        try:
            store.supersede(candidate["resource_id"], user_subject=scope["subject"],
                            pending_only=True, before_supersede=retire)
        except (_EntryChanged, FieldSubmissionStateError, WriteAuthorizationStateError):
            continue
        retired.append(candidate["task_id"])
    return retired
