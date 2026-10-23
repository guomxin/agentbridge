"""Explicit, bounded new attempts for one unresolved OA collaboration.

Old operations and consumed authorizations are immutable. A server-issued child
permit is attested by TaskHub events; no client flag can relax duplicate guards.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import time
from uuid import uuid4

from agentbridge.adapters.seeyon_pending_verification import read_pending_done, verify_pending_action
from agentbridge.browser.read_budget import (
    ReadBudgetExceeded, check_read_deadline, read_deadline_scope, remaining_seconds,
)
from agentbridge.core.write_catalog import _TRUSTED_WRITE_COMMITS, resolve_write_function


ACK_VERSION = "oa_unknown_new_attempt_v1"
ASSESSMENT_TTL_SECONDS = 120
RECOVERY_READ_BUDGET_SECONDS = 30
PERMIT_KIND = "oa_pending_recovery_v1"
_IDENTITY = ("affair_id", "summary_id", "process_id", "template_id", "form_app_id",
             "form_record_id", "node_policy", "node_policy_name", "body_type", "profile")


class OaPendingRecoveryError(RuntimeError):
    def __init__(self, code, message, *, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _fail(code, message):
    raise OaPendingRecoveryError(code, message)


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _principal(binding):
    expected = str(binding.get("expected_principal_ref") or "").strip()
    actual = str(binding.get("downstream_principal_ref") or "").strip()
    return expected if expected and expected == actual else None


def _session_hash(session):
    return _hash({key: session.get(key) for key in
                  ("user_subject", "session_id", "authority_generation", "expected_principal_ref", "downstream_principal_ref")})


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{16,128}", value):
        _fail("OA_RECOVERY_REQUEST_INVALID", "恢复请求标识无效。")
    return value


def _endpoint(connection, subject, endpoint_id):
    row = connection.execute("SELECT * FROM client_endpoints WHERE endpoint_id=? AND user_subject=?",
                             (endpoint_id, subject)).fetchone()
    if not row or row["state"] != "active":
        _fail("OA_RECOVERY_ENDPOINT_INVALID", "当前会话不可用于恢复此任务。")
    return dict(row)


def _endpoint_hash(endpoint):
    return _hash({key: endpoint.get(key) for key in ("endpoint_id", "user_subject", "agent_host",
        "client_type", "external_subject", "conversation_ref", "state")})


def _origin(service, connection, subject, task_id, *, current=True):
    task = service.tasks._select_owned_task(connection, task_id, subject)
    if json.loads(task["summary_json"]).get("oaPendingRecovery"):
        _fail("OA_RECOVERY_CHAIN_UNSUPPORTED", "这次恢复仍未确定结果，请核对该恢复任务；不会再自动创建新的尝试。")
    if task["status"] != "outcome_unknown":
        _fail("OA_RECOVERY_NOT_UNKNOWN", "原任务不是待核对的未知提交。")
    row = connection.execute(
        "SELECT o.* FROM operations o JOIN task_operations t USING(operation_id) "
        "WHERE t.task_id=? AND t.user_subject=? AND o.user_subject=? AND o.operation_id=? AND o.status='unknown'",
        (task_id, subject, subject, task["current_operation_id"]),
    ).fetchone()
    registered = _TRUSTED_WRITE_COMMITS.get(row["capability_name"]) if row else None
    if not registered or registered[1].get("preflight_function") not in {
            "preflight_pending_action", "preflight_missed_punch_approval"}:
        _fail("OA_RECOVERY_UNSUPPORTED", "此类业务暂不支持原事项恢复，请到业务系统核对。")
    authorizations = connection.execute(
        "SELECT * FROM write_authorizations WHERE commit_operation_id=? AND user_subject=? AND system_id='oa' AND state='consumed'",
        (row["operation_id"], subject),
    ).fetchall()
    if len(authorizations) != 1:
        _fail("OA_RECOVERY_ORIGIN_INVALID", "原提交的唯一授权记录不完整。")
    auth = authorizations[0]
    if not hmac.compare_digest(auth["plan_hash"], "sha256:" + hashlib.sha256(auth["plan_json"].encode()).hexdigest()):
        _fail("OA_RECOVERY_ORIGIN_INVALID", "原提交的冻结计划校验失败。")
    plan = json.loads(auth["plan_json"])
    target = plan.get("target") or {}
    principal = _principal(plan.get("session_binding") or {})
    if (not principal or not all(target.get(key) for key in ("affair_id", "summary_id", "process_id"))
            or plan.get("user_subject") != subject or auth["capability_name"] != row["capability_name"]
            or (plan.get("resume_arguments") or {}).get("affair_id") != target["affair_id"]):
        _fail("OA_RECOVERY_ORIGIN_INVALID", "原提交缺少可核对的身份或事项绑定。")
    session_row = connection.execute("SELECT * FROM sessions WHERE user_subject=? AND system_id='oa'", (subject,)).fetchone()
    session = dict(session_row) if session_row else {}
    if current and (session.get("state") != "active" or _principal(session) != principal):
        _fail("OA_RECOVERY_IDENTITY_CHANGED", "请先恢复与原提交相同的 OA 账号；不能替另一账号继续办理。")
    prepare, definition = registered
    if current:
        service.validate_skill_task(subject, task_id, capability=row["capability_name"], connection=connection)
    skill = service.skills.for_task(subject, task_id, connection)
    if current:
        service.user_grants.require_capability(subject, prepare)
        service.user_grants.require_capability(subject, row["capability_name"])
    return {"task": dict(task), "operation": dict(row), "target": target, "plan": plan,
            "prepare": prepare, "definition": definition, "session": session,
            "scopeHash": _hash({"subject": subject, "principal": principal, "prepare": prepare,
                                "commit": row["capability_name"], "identity": {key: target.get(key) for key in _IDENTITY}}),
            "planHash": auth["plan_hash"], "skill": skill}


def can_recover_pending(service, *, subject, task_id):
    try:
        with service.tasks._connect() as connection:
            _origin(service, connection, subject, task_id)
        return True
    except (OaPendingRecoveryError, KeyError, PermissionError, ValueError):
        return False


def recovery_source_context(service, connection, subject, task_id):
    """Read an attested child-to-origin edge, never an execution permission.

    Historical progress must still invalidate its visible parents after login or
    grant changes. Every subsequent action rechecks current authority in _origin;
    this reader validates immutable ownership, identity and both server events.
    """
    child = service.tasks._select_owned_task(connection, task_id, subject)
    permit = json.loads(child["summary_json"]).get("oaPendingRecovery")
    if not isinstance(permit, dict):
        return None
    original_id = permit.get("originTaskId")
    if not isinstance(original_id, str) or original_id == task_id:
        _fail("OA_RECOVERY_PERMIT_INVALID", "恢复任务关联校验失败。")
    origin = _origin(service, connection, subject, original_id, current=False)
    linked = recovery_child_for_origin(service, subject=subject, task_id=original_id, connection=connection)
    if (linked != task_id or permit.get("kind") != PERMIT_KIND or permit.get("acknowledgement") != ACK_VERSION
            or permit.get("originOperationId") != origin["operation"]["operation_id"]
            or permit.get("originPlanHash") != origin["planHash"] or permit.get("scopeHash") != origin["scopeHash"]
            or permit.get("affairId") != origin["target"]["affair_id"]
            or permit.get("prepareCapability") != origin["prepare"]
            or permit.get("commitCapability") != origin["operation"]["capability_name"]):
        _fail("OA_RECOVERY_PERMIT_INVALID", "恢复任务与原事项绑定不一致。")
    return {"origin": origin, "child": dict(child)}


def recovery_origin_source_context(service, connection, subject, task_id):
    """A persisted read-only assessment invalidates its owned query snapshots."""
    if not connection.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? "
        "AND event_type IN ('task.oa_recovery.assessed','task.oa_recovery.child_created') LIMIT 1",
        (task_id, subject)).fetchone():
        return None
    return _origin(service, connection, subject, task_id, current=False)


def fresh_recovery_completion(service, connection, subject, task_id):
    """Project a fresh attested read-only success, never rewrite the attempt.

    The query row may show this current conclusion only while the same session
    generation, principal, grants and short assessment lifetime remain valid.
    This does not issue any of the credentials accepted by prepare.
    """
    row = connection.execute(
        "SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? "
        "AND event_type='task.oa_recovery.assessed' ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (task_id, subject)).fetchone()
    if not row:
        return None
    assessment = json.loads(row[0])
    digest = assessment.pop("assessmentHash", None)
    if assessment.get("status") != "verified_complete" or not isinstance(digest, str):
        return None
    origin = _origin(service, connection, subject, task_id)
    observed = _projection_time(assessment.get("observedAt"))
    expires = _projection_time(assessment.get("expiresAt"))
    if (not observed or not expires or datetime.fromisoformat(expires) <= _now()
            or not hmac.compare_digest(digest, _hash(assessment))
            or assessment.get("originOperationId") != origin["operation"]["operation_id"]
            or assessment.get("originPlanHash") != origin["planHash"]
            or assessment.get("scopeHash") != origin["scopeHash"]
            or assessment.get("sessionHash") != _session_hash(origin["session"])):
        return None
    verified = _events(connection, task_id, subject, "task.oa_recovery.verified_complete", assessment.get("assessmentId"))
    if (len(verified) != 1 or verified[0].get("assessmentId") != assessment.get("assessmentId")
            or verified[0].get("assessmentHash") != digest
            or verified[0].get("originOperationId") != origin["operation"]["operation_id"]
            or verified[0].get("method") != "complete_pending_absence_and_done_identity"):
        return None
    return {"observedAt": observed, "expiresAt": expires}


_PUBLIC_REASONS = frozenset({
    "not_checked", "verified_complete", "still_pending_previous_unknown", "read_unavailable",
    "read_or_contract_unavailable", "not_verified", "conflicting_or_completed_evidence",
    "pending_coverage_incomplete", "target_identity_changed", "pending_response_invalid",
    "pending_coverage_inconsistent", "done_response_invalid", "done_affair_not_found",
    "done_affair_ambiguous", "done_title_mismatch", "done_detail_affair_mismatch",
    "done_detail_identity_unavailable", "done_identity_mismatch", "frozen_identity_incomplete",
    "done_scan_unproven", "read_budget_exceeded",
})


def _projection_time(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed.tzinfo is not None else None
    except ValueError:
        return None


def pending_recovery_presentation(service, *, subject, task_id):
    """Persisted current result, separate from the immutable unknown attempt.

    GET never reads OA, restores an assessment nonce, or grants preparation. Only
    an explicit fresh probe can produce the bounded evidence used by prepare.
    """
    result = {"originalTaskId": task_id, "status": "not_checked", "availability": "can_probe",
              "reason": "not_checked", "canProbe": True, "canPrepare": False, "canExecute": False,
              "nextAction": "probe", "stale": False}
    with service.tasks._connect() as connection:
        try:
            origin = _origin(service, connection, subject, task_id, current=False)
        except OaPendingRecoveryError as error:
            reason = {"OA_RECOVERY_CHAIN_UNSUPPORTED": "recovery_chain_unsupported",
                      "OA_RECOVERY_UNSUPPORTED": "unsupported", "OA_RECOVERY_NOT_UNKNOWN": "not_unknown"}.get(error.code, "origin_evidence_invalid")
            return {**result, "status": "blocked", "availability": "unsupported" if reason in {
                "recovery_chain_unsupported", "unsupported", "not_unknown"} else "blocked",
                "reason": reason, "canProbe": False, "nextAction": "inspect_oa"}
        try:
            _origin(service, connection, subject, task_id)
        except OaPendingRecoveryError as error:
            login = error.code == "OA_RECOVERY_IDENTITY_CHANGED" and origin["session"].get("state") != "active"
            result.update(availability="login_required" if login else "blocked", canProbe=False,
                          nextAction="restore_login" if login else "inspect_oa",
                          reason="login_required" if login else "identity_changed")
        except (PermissionError, ValueError):
            result.update(availability="blocked", canProbe=False, nextAction="inspect_oa", reason="permission_required")
        assessment_row = connection.execute(
            "SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? "
            "AND event_type='task.oa_recovery.assessed' ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (task_id, subject)).fetchone()
        if assessment_row:
            assessment = json.loads(assessment_row[0])
            digest = assessment.pop("assessmentHash", None)
            valid = (isinstance(digest, str) and hmac.compare_digest(digest, _hash(assessment))
                and assessment.get("originOperationId") == origin["operation"]["operation_id"]
                and assessment.get("originPlanHash") == origin["planHash"] and assessment.get("scopeHash") == origin["scopeHash"]
                and assessment.get("status") in {"verified_complete", "unresolved_still_pending", "blocked"})
            if valid and assessment["status"] == "verified_complete":
                verified = _events(connection, task_id, subject, "task.oa_recovery.verified_complete", assessment.get("assessmentId"))
                valid = (len(verified) == 1 and verified[0].get("assessmentHash") == digest
                    and verified[0].get("assessmentId") == assessment.get("assessmentId")
                    and verified[0].get("originOperationId") == origin["operation"]["operation_id"]
                    and verified[0].get("method") == "complete_pending_absence_and_done_identity")
            if valid:
                observed = _projection_time(assessment.get("observedAt"))
                expires = _projection_time(assessment.get("expiresAt"))
                result.update(status=assessment["status"], stale=not expires or datetime.fromisoformat(expires) <= _now()
                              or assessment.get("sessionHash") != _session_hash(origin["session"]) or not result["canProbe"])
                if result["canProbe"]:
                    result["reason"] = assessment.get("reason") if assessment.get("reason") in _PUBLIC_REASONS else "not_verified"
                if observed:
                    result["observedAt"] = observed
                if expires:
                    result["expiresAt"] = expires
                if assessment.get("doneCoverage") in {"complete", "partial"}:
                    result["doneCoverage"] = assessment["doneCoverage"]
            else:
                result.update(status="blocked", reason="assessment_evidence_invalid", stale=True)
        try:
            child_id = recovery_child_for_origin(service, subject=subject, task_id=task_id, connection=connection)
            linked = recovery_source_context(service, connection, subject, child_id) if child_id else None
        except (OaPendingRecoveryError, KeyError, ValueError):
            return {**result, "status": "blocked", "reason": "recovery_evidence_invalid", "canProbe": False,
                    "availability": "blocked", "nextAction": "inspect_oa"}
        if linked:
            child = linked["child"]
            child_status = child["status"]
            preparation = _child_preparation(connection, child_id)
            result.update(recoveryTaskId=child_id, recoveryTaskStatus=child_status, childPreparation=preparation,
                          nextAction="probe" if preparation == "retryable" and result["canProbe"] else "open_recovery")
            updated = _projection_time(child.get("updated_at"))
            if updated:
                result["recoveryUpdatedAt"] = updated
            if child_status == "succeeded":
                from agentbridge.core.oa_pending_reconciliation import _scope
                operation = service.operations.get(child["current_operation_id"]) if child["current_operation_id"] else {}
                scope = _scope(service, operation)
                proven = (scope and scope["subject"] == subject and scope["affair"] == origin["target"]["affair_id"]
                    and scope["capability"] == origin["operation"]["capability_name"]
                    and _principal(scope["binding"]) == _principal(origin["plan"].get("session_binding") or {}))
                result.update(status="recovery_succeeded" if proven else "recovery_unknown",
                              reason="recovery_verified_complete" if proven else "recovery_result_unverified")
                if proven:
                    result["stale"] = not result["canProbe"]
                    result.pop("expiresAt", None)
                    result.pop("observedAt", None)
                    readback = ((operation.get("result") or {}).get("execution") or {}).get("readback") or {}
                    observed = _projection_time(readback.get("last_observed_at"))
                    if observed:
                        result["observedAt"] = observed
            elif child_status == "outcome_unknown":
                result.update(status="recovery_unknown", reason="recovery_outcome_unknown")
            elif child_status in {"failed", "expired", "canceled", "superseded"}:
                result.update(status="recovery_failed", reason="recovery_prepare_retryable" if preparation == "retryable" else "recovery_stopped")
            else:
                result.update(status="recovery_in_progress", reason="recovery_in_progress")
        return result


def _local_verified(service, origin):
    from agentbridge.core.oa_pending_reconciliation import _scope
    with service.tasks._connect() as connection:
        rows = connection.execute(
            "SELECT DISTINCT o.operation_id FROM operations o JOIN write_authorizations a ON a.commit_operation_id=o.operation_id "
            "WHERE o.user_subject=? AND o.capability_name=? AND o.status='succeeded' "
            "AND json_extract(a.plan_json,'$.target.affair_id')=?",
            (origin["operation"]["user_subject"], origin["operation"]["capability_name"], origin["target"]["affair_id"]),
        ).fetchall()
    for row in rows:
        scope = _scope(service, service.operations.get(row["operation_id"]))
        if scope and _principal(scope["binding"]) == _principal(origin["session"]):
            return True
    return False


def _observe(service, origin, adapter, worker, *, deadline):
    target = origin["target"]
    check_read_deadline(deadline=deadline)
    with worker.fork_page() as reader, adapter.capability_scope(reader):
        pending = adapter.list_workflows(reader, collection="pending", arguments={"limit": 100})
        check_read_deadline(deadline=deadline)
        done = read_pending_done(adapter, reader, target, deadline=deadline)
        from agentbridge.adapters.seeyon_pending_verification import _rows
        pending_rows, done_rows = _rows(pending), _rows(done)
        if pending_rows is None or done_rows is None:
            return {"status": "blocked", "reason": "read_unavailable"}
        evidence = done.get("query_evidence")
        if isinstance(evidence, dict) and evidence.get("completionReason") not in {
                "server_target_found", "server_source_exhausted", "server_filtered_source_exhausted",
                "server_query_scan_budget_reached"}:
            return {"status": "blocked", "reason": "done_scan_unproven", "doneCoverage": "partial"}
        present = any(str(row.get("affair_id") or "") == target["affair_id"] for row in pending_rows)
        done_present = any(str(row.get("affair_id") or "") == target["affair_id"] for row in done_rows)
        done_coverage = (done.get("coverage") or {}).get("status", "partial")
        if _local_verified(service, origin) or (present and done_present):
            return {"status": "blocked", "reason": "conflicting_or_completed_evidence", "doneCoverage": done_coverage}
        if not present:
            proof = verify_pending_action(adapter, reader, target, pending_snapshot=pending,
                                          done_snapshot=done, deadline=deadline)
            return {"status": "verified_complete" if proof.get("confirmed") is True else "blocked",
                    "reason": "verified_complete" if proof.get("confirmed") is True else proof.get("reason", "not_verified"),
                    "doneCoverage": done_coverage}
        from agentbridge.adapters.seeyon_pending_verification import _consistent_complete_collection
        coverage = pending.get("coverage") or {}
        if coverage.get("status") != "complete" or coverage.get("hasMore") is True or not _consistent_complete_collection(pending, pending_rows):
            return {"status": "blocked", "reason": "pending_coverage_incomplete", "doneCoverage": done_coverage}
        # Registered preparation is read-only at adapter level. Its temporary
        # opinion is never saved or used in the new card, authorization or write.
        prepared = resolve_write_function(origin["definition"]["prepare_function"])(
            adapter, reader, {"affair_id": target["affair_id"], "opinion": "恢复只读核对"})
        check_read_deadline(deadline=deadline)
        current = prepared["plan"]["target"]
        if any(str(current.get(key) or "") != str(target.get(key) or "") for key in _IDENTITY):
            return {"status": "blocked", "reason": "target_identity_changed", "doneCoverage": done_coverage}
        return {"status": "unresolved_still_pending", "reason": "still_pending_previous_unknown",
                "doneCoverage": done_coverage, "currentTargetHash": _hash(current)}


def _live(service, origin, *, adapter=None, worker=None):
    # One observation owns one deadline, including lock wait. A commit's live
    # recovery guard inherits any smaller remaining preflight budget.
    with read_deadline_scope(time.monotonic() + RECOVERY_READ_BUDGET_SECONDS) as deadline:
        if adapter is not None and worker is not None:
            return _observe(service, origin, adapter, worker, deadline=deadline)
        session = origin["session"]
        runtime = service._runtime_for_system("oa")
        if runtime is None:
            _fail("OA_RECOVERY_RUNTIME_UNAVAILABLE", "OA 只读核对暂不可用。")
        adapter, factory = runtime
        with service._session_lock(session["session_id"],
                wait_seconds=remaining_seconds(RECOVERY_READ_BUDGET_SECONDS),
                purpose="oa_recovery_probe"), ExitStack() as stack:
            check_read_deadline()
            state = service.session_states.load(session["session_id"])
            if state is None:
                _fail("OA_RECOVERY_SESSION_REQUIRED", "请先恢复原 OA 账号登录。")
            worker = stack.enter_context(factory(session, adapter))
            check_read_deadline()
            worker.restore_session_state(state)
            stack.enter_context(adapter.capability_scope(worker))
            probe = adapter.probe_business_draft_session(worker)
            check_read_deadline()
            if str(probe.get("observed_principal_ref") or "").strip() != _principal(session):
                _fail("OA_RECOVERY_IDENTITY_CHANGED", "实际 OA 登录与原提交身份不一致。")
            return _observe(service, origin, adapter, worker, deadline=deadline)


def _events(connection, task_id, subject, kind, causation=None):
    query = "SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? AND event_type=?"
    args = (task_id, subject, kind)
    if causation is not None:
        query += " AND causation_ref=?"
        args += (causation,)
    return [json.loads(row[0]) for row in connection.execute(query, args)]


def _child_preparation(connection, child):
    if not child:
        return None
    task = connection.execute("SELECT status FROM agent_tasks WHERE task_id=?", (child,)).fetchone()
    if not task or task["status"] in {"outcome_unknown", "succeeded", "canceled"}:
        return "locked"
    if connection.execute("SELECT 1 FROM write_authorizations a JOIN task_operations t ON t.operation_id=a.prepare_operation_id WHERE t.task_id=?", (child,)).fetchone():
        return "locked"
    if connection.execute("SELECT 1 FROM field_submissions f JOIN task_operations t ON t.operation_id=f.create_operation_id WHERE t.task_id=? AND f.state='pending' AND f.expires_at>?", (child, _now().isoformat())).fetchone():
        return "ready"
    if connection.execute("SELECT 1 FROM operations o JOIN task_operations t USING(operation_id) WHERE t.task_id=? AND o.status IN ('pending','running')", (child,)).fetchone():
        return "locked"
    return "retryable"


def _public(assessment, child=None, preparation=None):
    return {key: assessment[key] for key in ("status", "assessmentId", "assessmentHash", "revision", "observedAt", "expiresAt", "reason", "doneCoverage") if key in assessment} | {
        "canPrepare": assessment["status"] == "unresolved_still_pending" and (child is None or preparation == "retryable"),
        "canExecute": False, "previousOutcome": "unknown", **({"recoveryTaskId": child, "childPreparation": preparation} if child else {})}


def probe_unknown_pending(service, *, subject, task_id, request_key, endpoint_id):
    request_key = _key(request_key)
    causation = "oa-recovery-probe:" + _hash({"subject": subject, "task": task_id, "key": request_key})
    request_hash = _hash({"subject": subject, "task": task_id, "endpoint": endpoint_id, "key": request_key})
    with service.tasks._connect() as connection:
        endpoint = _endpoint(connection, subject, endpoint_id)
        origin = _origin(service, connection, subject, task_id)
        previous = _events(connection, task_id, subject, "task.oa_recovery.assessed", causation)
        if previous:
            if len(previous) != 1 or previous[0]["requestHash"] != request_hash:
                _fail("OA_RECOVERY_REQUEST_CONFLICT", "此恢复请求已绑定另一上下文。")
            child = recovery_child_for_origin(service, subject=subject, task_id=task_id, connection=connection)
            return _public(previous[0], child, _child_preparation(connection, child))
    try:
        observation = _live(service, origin)
    except OaPendingRecoveryError:
        raise
    except ReadBudgetExceeded:
        observation = {"status": "blocked", "reason": "read_budget_exceeded"}
    except Exception:
        observation = {"status": "blocked", "reason": "read_or_contract_unavailable"}
    assessment = {**observation, "assessmentId": str(uuid4()), "revision": 1,
                  "originOperationId": origin["operation"]["operation_id"], "originPlanHash": origin["planHash"],
                  "scopeHash": origin["scopeHash"], "sessionHash": _session_hash(origin["session"]),
                  "endpointId": endpoint_id, "requestHash": request_hash, "observedAt": _now().isoformat(),
                  "endpointHash": _endpoint_hash(endpoint),
                  "expiresAt": (_now() + timedelta(seconds=ASSESSMENT_TTL_SECONDS)).isoformat()}
    assessment["assessmentHash"] = _hash(assessment)
    with service.tasks._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        fresh = _origin(service, connection, subject, task_id)
        if (fresh["scopeHash"] != origin["scopeHash"] or _session_hash(fresh["session"]) != assessment["sessionHash"]
                or _endpoint_hash(_endpoint(connection, subject, endpoint_id)) != assessment["endpointHash"]):
            _fail("OA_RECOVERY_IDENTITY_CHANGED", "核对期间 OA 登录上下文变化。")
        existing = _events(connection, task_id, subject, "task.oa_recovery.assessed", causation)
        if existing:
            if len(existing) != 1 or existing[0].get("requestHash") != request_hash:
                _fail("OA_RECOVERY_REQUEST_CONFLICT", "此恢复请求已绑定另一上下文。")
            assessment = existing[0]
        else:
            service.tasks._append_event(connection, task_id=task_id, user_subject=subject,
                event_type="task.oa_recovery.assessed", payload=assessment, causation_ref=causation, created_at=_now().isoformat())
        if assessment["status"] == "verified_complete":
            _record_verified(service, connection, subject, task_id, assessment)
        child = recovery_child_for_origin(service, subject=subject, task_id=task_id, connection=connection)
        preparation = _child_preparation(connection, child)
    return _public(assessment, child, preparation)


def recovery_child_for_origin(service, *, subject, task_id, connection=None):
    if connection is None:
        with service.tasks._connect() as owned:
            return recovery_child_for_origin(service, subject=subject, task_id=task_id, connection=owned)
    links = _events(connection, task_id, subject, "task.oa_recovery.child_created")
    if not links:
        return None
    if len(links) != 1:
        _fail("OA_RECOVERY_PERMIT_INVALID", "恢复任务关联不唯一。")
    child = service.tasks._select_owned_task(connection, links[0]["taskId"], subject)
    permit = json.loads(child["summary_json"]).get("oaPendingRecovery") or {}
    issued = _events(connection, child["task_id"], subject, "task.oa_recovery.permit_issued")
    if (permit.get("originTaskId") != task_id or permit.get("newTaskId") != child["task_id"]
            or _hash(permit) != links[0]["permitHash"] or len(issued) != 1
            or issued[0].get("permitHash") != _hash(permit)):
        _fail("OA_RECOVERY_PERMIT_INVALID", "恢复任务关联校验失败。")
    return child["task_id"]


def recovery_permit(service, *, subject, task_id, session, connection=None):
    if not task_id:
        return None
    if connection is None:
        with service.tasks._connect() as owned:
            return recovery_permit(service, subject=subject, task_id=task_id, session=session, connection=owned)
    task = service.tasks._select_owned_task(connection, task_id, subject)
    permit = json.loads(task["summary_json"]).get("oaPendingRecovery")
    if permit is None:
        return None
    events = _events(connection, task_id, subject, "task.oa_recovery.permit_issued")
    if (not isinstance(permit, dict) or permit.get("kind") != PERMIT_KIND or len(events) != 1
            or events[0].get("permitHash") != _hash(permit) or permit.get("newTaskId") != task_id
            or permit.get("acknowledgement") != ACK_VERSION or permit.get("sessionHash") != _session_hash(session)
            or permit.get("endpointId") != task["origin_endpoint_id"]):
        _fail("OA_RECOVERY_PERMIT_INVALID", "恢复许可与当前任务或 OA 登录不一致。")
    origin = _origin(service, connection, subject, permit["originTaskId"])
    if (permit.get("originOperationId") != origin["operation"]["operation_id"] or permit.get("scopeHash") != origin["scopeHash"]
            or permit.get("originPlanHash") != origin["planHash"]
            or permit.get("sessionHash") != _session_hash(origin["session"])
            or permit.get("endpointHash") != _endpoint_hash(_endpoint(connection, subject, permit["endpointId"]))
            or permit.get("affairId") != origin["target"]["affair_id"]
            or permit.get("prepareCapability") != origin["prepare"]
            or permit.get("commitCapability") != origin["operation"]["capability_name"]
            or permit.get("skillBindingHash") != (_hash(origin["skill"]) if origin["skill"] else None)
            or permit.get("skillBindingHash") != (_hash(service.skills.for_task(subject, task_id, connection)) if origin["skill"] else None)
            or recovery_child_for_origin(service, subject=subject, task_id=permit["originTaskId"], connection=connection) != task_id):
        _fail("OA_RECOVERY_PERMIT_INVALID", "原提交与恢复许可不一致。")
    return permit


def guard_recovery_live(service, *, subject, task_id, session, adapter, worker, target=None):
    permit = recovery_permit(service, subject=subject, task_id=task_id, session=session)
    if permit is None:
        return
    with service.tasks._connect() as connection:
        origin = _origin(service, connection, subject, permit["originTaskId"])
    observation = _live(service, origin, adapter=adapter, worker=worker)
    if (observation["status"] != "unresolved_still_pending"
            or observation.get("currentTargetHash") != permit.get("currentTargetHash")
            or target is not None and _hash(target) != permit.get("currentTargetHash")):
        _fail("OA_RECOVERY_RESULT_CHANGED", "原事项结果或身份已变化，已停止新的提交；请重新只读核对原任务。")


def _record_verified(service, connection, subject, task_id, assessment):
    causation = assessment["assessmentId"]
    if not _events(connection, task_id, subject, "task.oa_recovery.verified_complete", causation):
        service.tasks._append_event(connection, task_id=task_id, user_subject=subject,
            event_type="task.oa_recovery.verified_complete",
            payload={"assessmentId": causation, "assessmentHash": assessment["assessmentHash"],
                     "originOperationId": assessment["originOperationId"], "verifiedAt": _now().isoformat(),
                     "method": "complete_pending_absence_and_done_identity"},
            causation_ref=causation, created_at=_now().isoformat())


def _prepare_child(service, subject, child, origin):
    """Retry only read-only source preparation inside the same attested child."""
    with service.tasks._connect() as connection:
        task = service.tasks._select_owned_task(connection, child, subject)
        recovery_permit(service, subject=subject, task_id=child, session=origin["session"], connection=connection)
        if task["status"] in {"outcome_unknown", "succeeded", "canceled"}:
            return
        if connection.execute("SELECT 1 FROM write_authorizations a JOIN task_operations t ON t.operation_id=a.prepare_operation_id WHERE t.task_id=?", (child,)).fetchone():
            return
        if connection.execute("SELECT 1 FROM field_submissions f JOIN task_operations t ON t.operation_id=f.create_operation_id WHERE t.task_id=? AND f.state='pending' AND f.expires_at>?", (child, _now().isoformat())).fetchone():
            return
        operations = connection.execute("SELECT o.operation_id,o.status FROM operations o JOIN task_operations t USING(operation_id) WHERE t.task_id=? AND o.capability_name=? ORDER BY o.created_at,o.rowid", (child, origin["prepare"])).fetchall()
        if operations and operations[-1]["status"] in {"pending", "running"}:
            return
        generation = len(operations)
        endpoint = task["origin_endpoint_id"]
    response = service.invoke(user_subject=subject, task_id=child, capability_name=origin["prepare"],
        arguments={"affair_id": origin["target"]["affair_id"]},
        idempotency_key="oa-recovery-source:" + child + ":" + str(generation),
        host_type="workspace", origin_endpoint_id=endpoint)
    if response.get("status") != "requires_user_action":
        _fail("OA_RECOVERY_PREPARE_FAILED", "恢复任务已保存，但新材料暂未准备好；重新核对后可在同一恢复任务再次准备。")


def _record_prepare_key(service, connection, subject, task_id, causation, request_hash, child):
    _check_prepare_key(connection, subject, causation, request_hash)
    if not _events(connection, task_id, subject, "task.oa_recovery.prepare_requested", causation):
        service.tasks._append_event(connection, task_id=task_id, user_subject=subject,
            event_type="task.oa_recovery.prepare_requested", payload={"requestHash": request_hash, "taskId": child},
            causation_ref=causation, created_at=_now().isoformat())


def prepare_recovery_attempt(service, *, subject, task_id, assessment_id, assessment_hash, expected_revision,
                             request_key, acknowledgement, endpoint_id):
    request_key = _key(request_key)
    request_causation = "oa-recovery-prepare:" + _hash({"subject": subject, "key": request_key})
    request_hash = _hash({"taskId": task_id, "assessmentId": assessment_id, "assessmentHash": assessment_hash,
                          "revision": expected_revision, "acknowledgement": acknowledgement, "endpointId": endpoint_id})
    if acknowledgement != ACK_VERSION or expected_revision != 1 or isinstance(expected_revision, bool):
        _fail("OA_RECOVERY_ACK_REQUIRED", "请明确确认已了解原结果未知，并希望以新的一次办理继续。")
    with service.tasks._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        endpoint = _endpoint(connection, subject, endpoint_id)
        _check_prepare_key(connection, subject, request_causation, request_hash)
        origin = _origin(service, connection, subject, task_id)
        assessments = [row for row in _events(connection, task_id, subject, "task.oa_recovery.assessed") if row.get("assessmentId") == assessment_id]
        if len(assessments) != 1:
            _fail("OA_RECOVERY_ASSESSMENT_INVALID", "原只读核对记录不存在。")
        assessment = assessments[0]
        if (assessment["assessmentHash"] != assessment_hash or _hash({k: v for k, v in assessment.items() if k != "assessmentHash"}) != assessment_hash
                or assessment["endpointId"] != endpoint_id or assessment["scopeHash"] != origin["scopeHash"]
                or assessment.get("endpointHash") != _endpoint_hash(endpoint)
                or assessment["sessionHash"] != _session_hash(origin["session"])):
            _fail("OA_RECOVERY_ASSESSMENT_INVALID", "只读核对与当前账号、事项或会话不一致。")
        existing = recovery_child_for_origin(service, subject=subject, task_id=task_id, connection=connection)
        if existing:
            recovery_permit(service, subject=subject, task_id=existing, session=origin["session"], connection=connection)
            if _child_preparation(connection, existing) == "retryable" and (
                    assessment["status"] != "unresolved_still_pending" or _now() >= datetime.fromisoformat(assessment["expiresAt"])):
                _fail("OA_RECOVERY_ASSESSMENT_STALE", "请重新只读核对，再准备同一恢复任务的材料。")
            _record_prepare_key(service, connection, subject, task_id, request_causation, request_hash, existing)
        elif assessment["status"] != "unresolved_still_pending" or _now() >= datetime.fromisoformat(assessment["expiresAt"]):
            _fail("OA_RECOVERY_ASSESSMENT_STALE", "请先重新只读核对原事项，再明确确认新的办理。")
    if existing:
        _prepare_child(service, subject, existing, origin)
        return {"status": "existing", "taskId": existing, "originalTaskId": task_id, "assessmentId": assessment_id}
    observation = _live(service, origin)
    if observation["status"] == "verified_complete":
        with service.tasks._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fresh = _origin(service, connection, subject, task_id)
            if (_session_hash(fresh["session"]) != assessment["sessionHash"]
                    or _endpoint_hash(_endpoint(connection, subject, endpoint_id)) != assessment["endpointHash"]):
                _fail("OA_RECOVERY_IDENTITY_CHANGED", "核验完成后当前登录或会话发生变化，请重新核对。")
            _record_verified(service, connection, subject, task_id, assessment)
        return {"status": "verified_complete", "taskId": task_id, "originalTaskId": task_id, "assessmentId": assessment_id}
    if observation["status"] != "unresolved_still_pending" or observation.get("currentTargetHash") != assessment.get("currentTargetHash"):
        _fail("OA_RECOVERY_RESULT_CHANGED", "只读核对后事项或材料已变化，请重新核对。")
    with service.tasks._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _check_prepare_key(connection, subject, request_causation, request_hash)
        fresh = _origin(service, connection, subject, task_id)
        if (fresh["scopeHash"] != origin["scopeHash"] or _session_hash(fresh["session"]) != assessment["sessionHash"]
                or _endpoint_hash(_endpoint(connection, subject, endpoint_id)) != assessment["endpointHash"]):
            _fail("OA_RECOVERY_IDENTITY_CHANGED", "准备恢复期间 OA 登录发生变化。")
        if _now() >= datetime.fromisoformat(assessment["expiresAt"]):
            _fail("OA_RECOVERY_ASSESSMENT_STALE", "核对记录已到期，请重新核对。")
        existing = recovery_child_for_origin(service, subject=subject, task_id=task_id, connection=connection)
        if existing:
            recovery_permit(service, subject=subject, task_id=existing, session=origin["session"], connection=connection)
            child_id = existing
        else:
            child, reused = service.tasks.ensure_task(user_subject=subject, agent_host=endpoint["agent_host"],
                host_task_key="oa-recovery-child:" + origin["operation"]["operation_id"], origin_endpoint_id=endpoint_id,
                active_conversation_ref=endpoint["conversation_ref"], title="重新核对并办理：" + str(origin["target"].get("title") or "OA待办")[:200],
                connection=connection)
            if reused:
                _fail("OA_RECOVERY_PERMIT_INVALID", "恢复任务标识已存在且未获服务器许可。")
            child_id = child["task_id"]
            if origin["skill"]:
                service.skills.attach(subject, child_id, origin["skill"]["binding_id"], connection=connection)
            permit = {"kind": PERMIT_KIND, "newTaskId": child_id, "originTaskId": task_id,
                      "originOperationId": origin["operation"]["operation_id"], "originPlanHash": origin["planHash"],
                      "scopeHash": origin["scopeHash"], "sessionHash": assessment["sessionHash"], "endpointId": endpoint_id,
                      "endpointHash": assessment["endpointHash"],
                      "assessmentId": assessment_id, "assessmentHash": assessment_hash, "acknowledgement": acknowledgement,
                      "currentTargetHash": assessment["currentTargetHash"],
                      "skillBindingHash": _hash(origin["skill"]) if origin["skill"] else None,
                      "affairId": origin["target"]["affair_id"], "prepareCapability": origin["prepare"],
                      "commitCapability": origin["operation"]["capability_name"], "issuedAt": _now().isoformat()}
            summary = {"oaPendingRecovery": permit}
            connection.execute("UPDATE agent_tasks SET summary_json=? WHERE task_id=?", (_json(summary), child_id))
            for target_task, kind, payload in (
                (child_id, "task.oa_recovery.permit_issued", {"permitHash": _hash(permit)}),
                (task_id, "task.oa_recovery.child_created", {"taskId": child_id, "permitHash": _hash(permit)})):
                service.tasks._append_event(connection, task_id=target_task, user_subject=subject, event_type=kind,
                    payload=payload, causation_ref=origin["operation"]["operation_id"], created_at=_now().isoformat())
        _record_prepare_key(service, connection, subject, task_id, request_causation, request_hash, child_id)
    _prepare_child(service, subject, child_id, origin)
    return {"status": "existing" if existing else "prepared", "taskId": child_id, "originalTaskId": task_id, "assessmentId": assessment_id}


def _check_prepare_key(connection, subject, causation, request_hash):
    rows = connection.execute("SELECT payload_json FROM task_events WHERE user_subject=? AND event_type='task.oa_recovery.prepare_requested' AND causation_ref=?", (subject, causation)).fetchall()
    if rows and (len(rows) != 1 or json.loads(rows[0][0]).get("requestHash") != request_hash):
        _fail("OA_RECOVERY_REQUEST_CONFLICT", "此恢复确认请求已绑定另一核对记录。")
