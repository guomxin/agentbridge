from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from uuid import UUID, uuid4

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.user_grants import UserGrantConflict
from agentbridge.core.task_plans import task_plan_response
from agentbridge.core.task_projections import workspace_batch_projection
from agentbridge.core.timeline_attachments import (
    TimelineAttachmentExpired,
    TimelineAttachmentIntegrityError,
    TimelineAttachmentNotFound,
    public_attachment,
)
from agentbridge.workspace.gateway import GatewayRequestError, OpenClawGatewayClient
from agentbridge.workspace.stores import normalize_skill_trial
from agentbridge.workspace.performance import WorkspacePerformance


_LOG = logging.getLogger(__name__)

MAX_CHAT_ATTACHMENTS = 4
MAX_CHAT_ATTACHMENT_BYTES = 6 * 1024 * 1024
MAX_CHAT_ATTACHMENTS_TOTAL_BYTES = 12 * 1024 * 1024
_CHAT_IMAGE_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/webp": (b"RIFF", ".webp"),
}
_HOST_DISPATCH_RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0, 10.0)
_HOST_TRANSPORT_ERROR_CODES = {
    "GATEWAY_CONNECTION_CLOSED",
    "GATEWAY_CONNECTION_FAILED",
    "GATEWAY_PROCESS_FAILED",
    "GATEWAY_TIMEOUT",
}
_HOST_SAFE_PRE_ACCEPT_STAGES = {"connect", "preflight_abort", "bind"}
_HOST_DISPATCH_TERMINAL_STATES = {
    "completed",
    "failed",
    "expired",
    "failed_before_accept",
    "acceptance_unknown",
    "result_unknown",
    "canceled",
}


class WorkspaceArtifactError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceChatResult:
    run_id: str | None
    status: str


class WorkspaceApplication:
    def __init__(
        self,
        *,
        service: CentralCapabilityService,
        gateway: OpenClawGatewayClient | None = None,
    ) -> None:
        self.service = service
        self.store = service.workspace
        self.gateway = gateway
        self.performance = WorkspacePerformance(service.runtime_governance)
        self._dispatch_instance_id = f"workspace-{uuid4()}"
        self._dispatch_stop = threading.Event()
        self._dispatch_workers_lock = threading.Lock()
        self._dispatch_workers: dict[str, threading.Thread] = {}
        self._preparation_workers: dict[str, threading.Thread] = {}
        self._preparation_queue: dict[str, tuple[dict, dict]] = {}
        self._readiness_lock = threading.Lock()
        self._readiness_cache: tuple[float, dict] | None = None
        if gateway is not None:
            for account_id in self.store.recover_host_dispatches():
                self._ensure_dispatch_worker(account_id)

    def start_workbench(self):
        if self.gateway is not None:
            self.service.skill_authoring.workbench.start(self._skill_completion, self._capture_skill_method)

    def _skill_completion(self, system: str, prompt: str) -> dict:
        return self._gateway().call('agentbridge.skills.complete', {'system': system, 'prompt': prompt}, timeout_seconds=100)

    def _capture_skill_method(self, capture):
        try:
            dispatch = self.store.get_host_dispatch(capture['dispatch_id'], user_subject=capture['owner_subject'])
            if self.store.skill_trial_for_dispatch(dispatch['dispatch_id'], user_subject=capture['owner_subject']):
                return
            account = self.store.get_account(dispatch['account_id'])
            material, attachments = self._host_dispatch_payload(dispatch)
            if not attachments and not re.search(r'(创建|做个|发布|审批|评测|试用|沉淀).{0,10}(助手|skill|草稿)|草稿', material, re.I):
                self.service.skill_authoring.workbench.generate(account['user_subject'],
                    material=('用户：' + material + '\n助手：' + capture['assistant_text'])[:16000],
                    request_key='workspace:' + dispatch['dispatch_id'], automatic=True,
                    scope='workspace:' + account['account_id'])
        except (PermissionError, ValueError, KeyError, UserGrantConflict):
            return  # Opt-out, unavailable material and quota cannot affect a completed task.

    def start_enrollment(self) -> dict:
        return self.store.start_link()

    def enrollment_status(self, enrollment_token: str) -> dict | None:
        return self.store.link_status(enrollment_token)

    def complete_enrollment(
        self,
        *,
        enrollment_token: str,
        username: str,
        password: str,
    ) -> dict:
        account = self.store.create_account(
            enrollment_token=enrollment_token,
            username=username,
            password=password,
        )
        registered = self.service.register_workspace_endpoint(
            account_id=account["account_id"],
        )
        session = self.store.create_session(account["account_id"])
        return {
            "account": _public_account(registered["account"]),
            "session": session,
        }

    def login(self, *, username: str, password: str) -> dict | None:
        account = self.store.authenticate(
            username=username,
            password=password,
        )
        if account is None:
            return None
        account = self.store.record_login(account["account_id"])
        session = self.store.create_session(account["account_id"])
        return {
            "account": _public_account(account),
            "session": session,
        }

    def session(
        self,
        session_token: str | None,
        *,
        csrf_token: str | None = None,
        touch: bool = True,
    ) -> dict | None:
        account = self.store.verify_session(
            session_token,
            csrf_token=csrf_token,
            touch=touch,
        )
        if account is not None and touch and account.get("endpoint_id"):
            try:
                self.service.tasks.touch_endpoint(
                    endpoint_id=account["endpoint_id"],
                    user_subject=account["user_subject"],
                )
            except (KeyError, RuntimeError, ValueError, sqlite3.Error):
                # Session verification remains available during optional
                # Task Hub projection failures.
                pass
        return account

    @staticmethod
    def public_account(account: dict | None) -> dict | None:
        return _public_account(account) if account else None

    def logout(self, session_token: str | None) -> None:
        self.store.revoke_session(session_token)

    def list_tasks(
        self,
        account: dict,
        *,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        return [
            _public_task(task)
            for task in self.service.tasks.list_tasks(
                user_subject=account["user_subject"],
                active_only=active_only,
                limit=limit,
            )
        ]

    def task_detail(self, account: dict, task_id: str) -> dict:
        return self._task_projection(account, task_id, include_history=True)

    def task_history(self, account: dict, *, before_updated_at: str | None = None,
                     before_task_id: str | None = None, limit: int = 32) -> dict:
        if (before_updated_at is None) != (before_task_id is None):
            raise ValueError("both task cursor fields are required")
        if before_updated_at is not None:
            if (not isinstance(before_updated_at, str) or len(before_updated_at) > 40
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:\+00:00|Z)", before_updated_at)):
                raise ValueError("task cursor timestamp must be UTC ISO-8601")
            before_updated_at = datetime.fromisoformat(before_updated_at.replace("Z", "+00:00")).isoformat()
            if not isinstance(before_task_id, str) or str(UUID(before_task_id)) != before_task_id:
                raise ValueError("task cursor id must be a canonical UUID")
        limit = min(max(int(limit), 1), 32)
        tasks = self.service.tasks.list_tasks(user_subject=account["user_subject"],
            before_updated_at=before_updated_at, before_task_id=before_task_id, limit=limit + 1)
        has_more = len(tasks) > limit
        items = tasks[:limit]
        return {"items": [_public_task(task) for task in items], "hasMore": has_more,
                "nextCursor": {"updatedAt": items[-1]["updated_at"], "taskId": items[-1]["task_id"]}
                if has_more else None}

    def _task_projection(self, account: dict, task_id: str, *, include_history: bool) -> dict:
        self.service.require_task_result_access(user_subject=account["user_subject"], task_id=task_id)
        task = self.service.tasks.get_task(
            task_id,
            user_subject=account["user_subject"],
        )
        events = self.service.tasks.list_events(
            task_id=task_id,
            user_subject=account["user_subject"],
            limit=200,
        ) if include_history else []
        artifacts = self.service.tasks.list_artifacts(
            task_id=task_id,
            user_subject=account["user_subject"],
            limit=100 if include_history else 6,
        )
        artifacts_have_more = not include_history and len(artifacts) > 5
        if not include_history:
            artifacts = artifacts[:5]
        artifacts.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("artifact_id") or ""),
            )
        )
        interactions = []
        for linked in self.service.tasks.list_task_interactions(
            task_id=task_id,
            user_subject=account["user_subject"],
            limit=100,
        ):
            try:
                response = self.service.get_interaction(
                    user_subject=account["user_subject"],
                    interaction_id=linked["interaction_id"],
                )
                interaction = response["interaction"]
                if interaction.get("state") in {"pending", "processing"}:
                    response = self.service.present_interaction(
                        user_subject=account["user_subject"],
                        agent_host="openclaw",
                        endpoint_key=account["endpoint_key"],
                        interaction_id=linked["interaction_id"],
                        continuous_flow=True,
                    )
                    interaction = response["interaction"]
                interactions.append(
                    {
                        **interaction,
                        "linkedAt": linked["linked_at"],
                        "lastObservedAt": linked["last_observed_at"],
                    }
                )
            except (KeyError, RuntimeError):
                continue
        interaction = next(
            (
                item
                for item in interactions
                if item.get("interactionId")
                == task.get("current_interaction_id")
            ),
            None,
        )
        plan = self.service.task_plans.get_for_task(
            parent_task_id=task_id,
            user_subject=account["user_subject"],
        )
        batch = self.service.tasks.get_batch_for_task(
            parent_task_id=task_id,
            user_subject=account["user_subject"],
        )
        from agentbridge.core.actionable_results import preparation_source_task_ids
        with self.service.tasks._connect() as connection:
            actionable_sources = preparation_source_task_ids(connection, account["user_subject"], task_id, service=self.service)
        business = self._business_task_presentation(account, task, interactions)
        return {
            "task": _public_task(task),
            "actionableResults": self.actionable_results(account, task_id),
            "actionableSourceTaskIds": actionable_sources,
            "skill": self.service.skills.task_presentation(account["user_subject"], task_id),
            "feedback": self.service.skill_authoring.workbench.task_feedback(account["user_subject"], task_id),
            "plan": task_plan_response(plan) if plan is not None else None,
            "events": [_public_event(event) for event in events],
            "artifacts": [_public_artifact(item) for item in artifacts],
            "interaction": interaction,
            "interactions": interactions,
            "batch": workspace_batch_projection(batch),
            **business,
            **({"artifactsHasMore": artifacts_have_more, "historyIncluded": False} if not include_history else {}),
        }

    def _business_task_presentation(self, account: dict, task: dict, interactions: list[dict]) -> dict:
        """Display durable confirmations without rewriting the original contracts.

        A trusted business-view entry identifies the one-confirmation form; only
        its task- and owner-bound decision proves that confirmation was recorded.
        No form values, authorization IDs, error messages or source JSON escape.
        """
        user_subject, task_id = account["user_subject"], task["task_id"]
        inputs = {item["interactionId"]: item for item in interactions
                  if item.get("type") == "business_input"
                  and item.get("display", {}).get("businessView", {}).get("schemaVersion")
                  == "agentbridge.business-view.v1"}
        result = {}
        decisions, operation_linked = [], False
        if inputs:
            with self.service.tasks._connect() as connection:
                rows = connection.execute(
                    "SELECT i.interaction_id,i.resume_json,s.schema_json,d.created_at AS confirmed_at,"
                    "d.interaction_id AS authorization_interaction_id,a.authorization_id,"
                    "a.state AS authorization_state,a.commit_operation_id,a.consumed_at,"
                    "a.capability_name AS authorization_capability,a.capability_version AS authorization_version "
                    "FROM task_interactions t "
                    "JOIN interactions i ON i.interaction_id=t.interaction_id AND i.user_subject=t.user_subject "
                    "JOIN field_submissions s ON s.submission_id=i.resource_id AND s.user_subject=i.user_subject "
                    "LEFT JOIN business_draft_decisions d ON d.source_submission_id=s.submission_id "
                    "AND d.user_subject=t.user_subject AND d.task_id=t.task_id "
                    "LEFT JOIN write_authorizations a ON a.authorization_id=d.authorization_id AND a.user_subject=d.user_subject "
                    "WHERE t.task_id=? AND t.user_subject=? AND i.interaction_type='business_input' "
                    "ORDER BY i.created_at DESC LIMIT 100", (task_id, user_subject)).fetchall()
                if task.get("status") == "failed":
                    operation_linked = connection.execute(
                        "SELECT 1 FROM task_operations WHERE task_id=? AND user_subject=? AND operation_id=?",
                        (task_id, user_subject, task.get("current_operation_id"))).fetchone() is not None
            for row in rows:
                item = inputs.get(row["interaction_id"])
                if item is None or json.loads(row["resume_json"]).get("businessDraft") != "v1":
                    continue
                recorded = row["confirmed_at"] is not None
                # A legacy submit may have used a once-capable form without a
                # draft decision. Its completed history keeps the old meaning.
                if recorded or item.get("state") in {"pending", "processing"}:
                    item["confirmation"] = {"mode": "single", "state": "recorded" if recorded else "pending"}
                if recorded:
                    decisions.append(row)
                    for authorization in interactions:
                        if (authorization.get("interactionId") == row["authorization_interaction_id"]
                                and authorization.get("type") == "execution_authorization"):
                            authorization["confirmation"] = {"mode": "single", "state": "recorded",
                                "sourceInteractionId": row["interaction_id"]}
                schema = json.loads(row["schema_json"])
                title = (schema.get("review") or {}).get("title")
                if not result.get("businessTitle") and isinstance(title, str) and title.strip():
                    batch = (task.get("summary") or {}).get("batch") or {}
                    if not batch or batch.get("totalCount") == 1:
                        result["businessTitle"] = _safe_text(title, 300)
        operation_id = task.get("current_operation_id")
        # An in-flight commit is linked by the host only after it returns. Even
        # when progress storage fails, its exact owned decision/input binding
        # identifies the current attempt without inventing a phase or replay.
        if decisions and task.get("status") not in {"succeeded", "failed", "outcome_unknown", "canceled", "expired"}:
            with self.service.tasks._connect() as connection:
                for decision in decisions:
                    if decision["authorization_state"] not in {"approved", "consumed"}:
                        continue
                    exact_input = json.dumps({"authorization_id":decision["authorization_id"]},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    running = connection.execute("SELECT operation_id FROM operations WHERE user_subject=? AND capability_name=? "
                        "AND capability_version=? AND input_hash=? AND status='running' AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                        (user_subject, decision["authorization_capability"], decision["authorization_version"],
                         hashlib.sha256(exact_input.encode()).hexdigest(), decision["confirmed_at"])).fetchone()
                    if running and (decision["authorization_state"] == "approved" or decision["commit_operation_id"] == running["operation_id"]):
                        operation_id = running["operation_id"]
                        break
        from agentbridge.core.business_execution_progress import pending_progress
        progress = pending_progress(self.service.tasks, user_subject=user_subject, task_id=task_id)
        if progress:
            observed_operation = self.service.operations.get(progress["operationId"])
            previous = self.service.operations.get(operation_id) if operation_id else None
            if previous is None or observed_operation["created_at"] >= previous["created_at"]:
                operation_id = progress["operationId"]
        if operation_id:
            operation = self.service.operations.get(operation_id)
            if operation.get("user_subject") == user_subject:
                execution = _public_business_execution(operation, task)
                if execution:
                    if operation["status"] == "running":
                        execution.update(operationId=operation_id, requestId=operation["request_id"],
                            operationStartedAt=operation["created_at"])
                        if not progress or progress["operationId"] != operation_id:
                            execution["phase"] = "awaiting_progress"
                    if progress and progress["operationId"] == operation_id:
                        execution.update(operationId=operation_id, requestId=operation["request_id"],
                            operationStartedAt=operation["created_at"], actualPhase=progress["phase"],
                            phaseStartedAt=progress["phaseStartedAt"], phaseObservedAt=progress["phaseObservedAt"],
                            observedPhases=progress["observedPhases"])
                        if operation["status"] == "running" and task.get("status") not in {"succeeded", "failed", "outcome_unknown", "canceled", "expired"}:
                            execution["phase"] = {"verification_completed":"verifying_result"}.get(progress["phase"], progress["phase"])
                        if "request_observed" in progress["observedPhases"]:
                            execution["requestObserved"] = True
                        if operation["status"] == "running":
                            if "submission_invoked" in progress["observedPhases"]:
                                execution["submissionState"] = "invoked"
                            elif "submission_scheduled" in progress["observedPhases"]:
                                execution["submissionState"] = "scheduled"
                    if operation_linked and any(_unconsumed_pending_attempt(operation, row) for row in decisions):
                        execution.update(phase="not_submitted", reason="pre_submit_validation_failed")
                    result["businessExecution"] = execution
                    if execution.get("readOnlyQuery") == "oa_pending" and task.get("status") == "outcome_unknown":
                        from agentbridge.core.oa_pending_recovery import pending_recovery_presentation
                        recovery = pending_recovery_presentation(self.service, subject=user_subject, task_id=task_id)
                        result["oaPendingRecovery"] = recovery
                        result["oaPendingRecoveryAvailable"] = recovery["canProbe"]
        result["businessTiming"] = self._business_journey_timing(user_subject, task, operation_id, progress)
        return result

    def _business_journey_timing(self, subject, task, operation_id, progress):
        """Read existing durable boundaries; never infer a missing event time."""
        command = (task.get("summary") or {}).get("command") or {}
        stages = [{"stage":"task_created", "observedAt":task["created_at"]}]
        requested = _public_observation_time(command.get("requestedAt"))
        if requested:
            stages.append({"stage":"preparation_requested", "observedAt":requested})
        with self.service.tasks._connect() as connection:
            ready = connection.execute("SELECT MAX(i.created_at) FROM task_interactions t JOIN interactions i "
                "ON i.interaction_id=t.interaction_id AND i.user_subject=t.user_subject "
                "WHERE t.task_id=? AND t.user_subject=? AND i.interaction_type='business_input'",
                (task["task_id"], subject)).fetchone()[0]
            decision = connection.execute("SELECT MAX(created_at) FROM business_draft_decisions WHERE task_id=? AND user_subject=?",
                (task["task_id"], subject)).fetchone()[0]
        if ready:
            stages.append({"stage":"form_ready", "observedAt":ready})
        if decision and (not ready or decision >= ready):
            stages.append({"stage":"decision_recorded", "observedAt":decision})
        operation = self.service.operations.get(operation_id) if operation_id else None
        if operation:
            stages.append({"stage":"operation_started", "observedAt":operation["created_at"]})
            if operation.get("finished_at"):
                stages.append({"stage":"operation_terminal", "observedAt":operation["finished_at"]})
        stages.extend({"stage":item["phase"], "observedAt":item["observedAt"]} for item in (progress or {}).get("stages", []))
        expected = ["task_created"]
        if command:
            expected.append("preparation_requested")
        if ready or decision:
            expected.extend(["form_ready", "decision_recorded"])
        from agentbridge.adapters.seeyon_pending_actions import PendingActionContractMismatch
        from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
        pending_commits = {item["commit_capability"] for item in _TRUSTED_WRITE_DEFINITIONS.values()
                           if item["contract_error"] is PendingActionContractMismatch}
        if operation and (operation["capability_name"] in pending_commits or progress) and decision:
            expected.extend(["operation_started", "waiting_business_page", "final_revalidation", "submission_scheduled", "verifying_result"])
        if task["status"] in {"succeeded", "failed", "outcome_unknown", "canceled", "expired", "partially_succeeded"} and operation:
            expected.append("operation_terminal")
        present = {item["stage"] for item in stages}
        return {"schemaVersion":"agentbridge.business-timing.v1", "taskId":task["task_id"],
            "operationId":operation_id, "requestId":operation.get("request_id") if operation else command.get("commandKey"),
            "clockSource":"central.wall", "status":task["status"], "stages":stages,
            "missingStages":[stage for stage in expected if stage not in present]}

    def task_projections(self, account: dict, task_ids: list[str], *, versions: list[int] | None = None) -> dict:
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 32:
            raise ValueError("task ids must contain between 1 and 32 identifiers")
        if versions is not None and (not isinstance(versions, list) or len(versions) != len(task_ids)
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in versions)):
            raise ValueError("versions must match the bounded task identifiers")
        normalized = []
        for task_id in task_ids:
            if not isinstance(task_id, str):
                raise ValueError("task id must be a UUID")
            try:
                canonical = str(UUID(task_id))
            except (ValueError, AttributeError) as exc:
                raise ValueError("task id must be a UUID") from exc
            if canonical != task_id.lower():
                raise ValueError("task id must be a UUID")
            if canonical not in normalized:
                normalized.append(canonical)
        items, unavailable = [], []
        for task_id in normalized:
            try:
                # Version hints never bypass live access, presentation expiry,
                # actionable source TTL or a freshly signed active interaction.
                detail = self._task_projection(account, task_id, include_history=False)
            except (KeyError, PermissionError):
                unavailable.append(task_id)
                continue
            items.append({"taskId": task_id, "version": detail["task"]["version"],
                          "unchanged": False, "detail": detail})
        return {"items": items, "unavailableTaskIds": unavailable}

    def current(self, account: dict, *, active_limit=20, recent_limit=8, message_limit=80) -> dict:
        active_limit = min(max(int(active_limit), 1), 20)
        recent_limit = min(max(int(recent_limit), 0), 8)
        message_limit = min(max(int(message_limit), 1), 80)
        # Watermarks precede every snapshot read. Bound chat to this watermark;
        # subsequent commits are replayed by SSE, including an explicit zero.
        timeline_cursor = self.timeline_cursor(account)
        event_cursor = self.event_cursor(account)
        active = self.service.tasks.list_tasks(user_subject=account["user_subject"], active_only=True,
                                               limit=active_limit + 1)
        recent = self.service.tasks.list_tasks(user_subject=account["user_subject"], terminal_only=True,
                                               limit=recent_limit + 1) if recent_limit else []
        selected = active[:active_limit] + recent[:recent_limit]
        projections = self.task_projections(account, [task["task_id"] for task in selected]) if selected else {
            "items": [], "unavailableTaskIds": []}
        messages = self.list_timeline(account, entry_type="chat_message", before_sequence=timeline_cursor + 1,
                                      limit=message_limit + 1)
        more_messages = len(messages) > message_limit
        messages = messages[-message_limit:]
        return {"schemaVersion": "agentbridge.workspace-current.v1",
                "cards": [item["detail"] for item in projections["items"]],
                "unavailableTaskIds": projections["unavailableTaskIds"],
                "timeline": {"items": messages, "cursor": timeline_cursor,
                             "oldestSequence": messages[0]["sequence"] if messages else None,
                             "hasMore": more_messages},
                "dispatches": {"items": self.list_chat_dispatches(account, active_only=True, limit=20)},
                "cursors": {"timeline": timeline_cursor, "event": event_cursor},
                "hasMoreActive": len(active) > active_limit, "hasMoreRecent": len(recent) > recent_limit}

    def actionable_results(self, account: dict, task_id: str) -> list[dict]:
        from agentbridge.core.actionable_results import list_actionable_results
        return list_actionable_results(self.service, account["user_subject"], task_id)

    def prepare_selected_results(self, account: dict, task_id: str, body: dict) -> dict:
        from agentbridge.core.actionable_results import queue_preparation
        with self._time_business_action(account, "selection", body, task_id) as timing:
            result = queue_preparation(self.service, account, task_id, body)
            timing["task_id"] = result["taskId"]
            return self._queued_command(account, result)

    def probe_oa_pending_recovery(self, account: dict, task_id: str, body: dict) -> dict:
        from agentbridge.core.oa_pending_recovery import probe_unknown_pending
        self._validate_oa_recovery_body(body, prepare=False)
        self.service.require_task_result_access(user_subject=account["user_subject"], task_id=task_id)
        with self._time_business_action(account, "recovery_probe", body, task_id):
            result = probe_unknown_pending(self.service, subject=account["user_subject"], task_id=task_id,
                request_key=body["requestKey"], endpoint_id=account["endpoint_id"])
            return self._public_oa_recovery(result)

    def prepare_oa_pending_recovery(self, account: dict, task_id: str, body: dict) -> dict:
        from agentbridge.core.oa_pending_recovery import prepare_recovery_attempt
        self._validate_oa_recovery_body(body, prepare=True)
        self.service.require_task_result_access(user_subject=account["user_subject"], task_id=task_id)
        with self._time_business_action(account, "recovery_prepare", body, task_id) as timing:
            result = prepare_recovery_attempt(self.service, subject=account["user_subject"], task_id=task_id,
                assessment_id=body["assessmentId"], assessment_hash=body["assessmentHash"],
                expected_revision=body["expectedRevision"], request_key=body["requestKey"],
                acknowledgement=body["acknowledgement"], endpoint_id=account["endpoint_id"])
            timing["task_id"] = result.get("taskId", task_id)
            return self._public_oa_recovery(result)

    @contextmanager
    def _time_business_action(self, account, kind, body, task_id=None):
        started, started_at = time.perf_counter(), datetime.now(timezone.utc).isoformat()
        timing = {"task_id":task_id}
        status = "failed"
        try:
            yield timing
            status = "succeeded"
        finally:
            self.performance.record_action(account, kind, body.get("requestKey") if isinstance(body, dict) else None,
                started=started, started_at=started_at, task_id=timing["task_id"], status=status)

    @staticmethod
    def _validate_oa_recovery_body(body: dict, *, prepare: bool) -> None:
        from agentbridge.core.oa_pending_recovery import ACK_VERSION
        fields = {"requestKey", "assessmentId", "assessmentHash", "expectedRevision", "acknowledgement"} if prepare else {"requestKey"}
        if not isinstance(body, dict) or set(body) != fields:
            raise ValueError("原事项核对请求字段无效。")
        if not isinstance(body["requestKey"], str) or not re.fullmatch(r"[a-zA-Z0-9_:-]{16,128}", body["requestKey"]):
            raise ValueError("原事项核对请求标识无效。")
        if prepare and (not isinstance(body["assessmentId"], str)
                or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", body["assessmentId"])
                or not isinstance(body["assessmentHash"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", body["assessmentHash"])
                or not isinstance(body["expectedRevision"], int) or isinstance(body["expectedRevision"], bool)
                or body["expectedRevision"] < 1 or body["acknowledgement"] != ACK_VERSION):
            raise ValueError("请先核对原事项，并明确确认重新准备的风险。")

    @staticmethod
    def _public_oa_recovery(result: dict) -> dict:
        # The core owns the assessment. Never expose its persisted principal,
        # original authorization, captured materials or recovery plan here.
        allowed = {"status", "assessmentId", "assessmentHash", "revision", "observedAt", "expiresAt", "reason",
            "canPrepare", "previousOutcome", "recoveryTaskId", "taskId", "originalTaskId"}
        public = {key: value for key, value in result.items() if key in allowed}
        if isinstance(result.get("doneCoverage"), str) and result["doneCoverage"] in {"complete", "partial", "incomplete", "unknown"}:
            public["doneCoverage"] = result["doneCoverage"]
        if isinstance(result.get("childPreparation"), str) and result["childPreparation"] in {"retryable", "ready", "locked"}:
            public["childPreparation"] = result["childPreparation"]
        public["canExecute"] = False
        return public

    def query_oa_pending(self, account: dict, body: dict) -> dict:
        from agentbridge.core.quick_queries import queue_query
        with self._time_business_action(account, "query", body) as timing:
            result = queue_query(self.service, account, body)
            timing["task_id"] = result["taskId"]
            message_key = result.pop("userMessageKey", None)
            response = self._queued_command(account, result)
            if message_key:
                entry = self.service.tasks.get_timeline_message(user_subject=account["user_subject"], message_key=message_key)
                endpoint = self.service.tasks.get_endpoint(account["endpoint_id"], user_subject=account["user_subject"])
                response["userMessage"] = _public_timeline_entry(entry, source=endpoint, is_origin=True)
            return response

    def _queued_command(self, account: dict, result: dict) -> dict:
        queued_task = self.service.tasks.get_task(result["taskId"], user_subject=account["user_subject"])
        try:
            requested_at = queued_task["summary"]["command"]["requestedAt"]
            governance = self.service.runtime_governance
            trace, _ = governance.ensure_trace(user_subject=account["user_subject"], task_id=result["taskId"],
                request_id=result["commandKey"], origin_endpoint_id=account["endpoint_id"],
                host_type="openclaw", request_kind="interaction", lock_timeout=.02)
            governance.record_timing(trace_id=trace["trace_id"], stage="workspace.preparation.queued",
                started_at=requested_at, finished_at=requested_at, duration_ms=0,
                metadata={"clockSource": "central.wall", "measurementKind": "event", "requestId": result["commandKey"]},
                dedupe_key="preparation-queued:" + result["commandKey"])
        except Exception as exc:
            _LOG.debug("Workspace preparation timing unavailable: %s", type(exc).__name__)
        # The HTTP request records an intent. Only the original host obtains a
        # coordinator lease and prepares the fixed, source-bound business action.
        if self.gateway is not None and queued_task["summary"]["command"]["state"] == "pending":
            with self._dispatch_workers_lock:
                if result["taskId"] not in self._preparation_workers and len(self._preparation_queue) < 32:
                    self._preparation_queue[result["taskId"]] = (dict(account), dict(result))
                    self._start_preparation_workers_locked()
        return {**result, "detail": self.task_detail(account, result["taskId"])}

    def _start_preparation_workers_locked(self) -> None:
        while self._preparation_queue and len(self._preparation_workers) < 4 and not self._dispatch_stop.is_set():
            task_id = next(iter(self._preparation_queue))
            account, command = self._preparation_queue.pop(task_id)
            worker = threading.Thread(target=self._wake_preparation, args=(account, command), daemon=True)
            self._preparation_workers[task_id] = worker
            worker.start()

    def _wake_preparation(self, account: dict, command: dict) -> None:
        try:
            grant = self.store.issue_gateway_grant(account["account_id"])
            self._gateway().call("agentbridge.task.wake", {
                "sessionKey": grant["session_key"], "endpointKey": grant["endpoint_key"],
                "grant": grant["grant"], "taskId": command["taskId"], "commandKey": command["commandKey"],
            }, timeout_seconds=60)
        except Exception as exc:
            # The committed command remains recoverable through the host runtime
            # channel. Never execute a business action from this worker.
            _LOG.debug("Workspace preparation wake unavailable: %s", type(exc).__name__)
        finally:
            with self._dispatch_workers_lock:
                self._preparation_workers.pop(command["taskId"], None)
                self._start_preparation_workers_locked()

    def task_feedback(self, account: dict, task_id: str, body: dict) -> dict:
        if not isinstance(body, dict) or set(body) != {"rating", "comment", "requestKey"}:
            raise ValueError("原任务反馈只能包含评价、补充说明和请求标识。")
        receipt = self.service.skill_authoring.workbench.record_task_feedback(account["user_subject"], task_id,
            rating=body["rating"], comment=body["comment"], request_key=body["requestKey"])
        return {"feedback": {"available": False, "receipt": receipt}}

    def artifact_history(self, account: dict, *, limit: int = 20) -> list[dict]:
        limit = min(max(int(limit), 1), 50)
        artifacts = self.service.tasks.list_user_artifacts(
            user_subject=account["user_subject"],
            limit=min(max(limit * 10, 100), 500),
        )
        artifacts.sort(
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        grouped: dict[str, list[dict]] = {}
        for artifact in artifacts:
            grouped.setdefault(artifact["task_id"], []).append(artifact)
        items = []
        for task_id, task_artifacts in grouped.items():
            if len(items) >= limit:
                break
            task = self.service.tasks.get_task(
                task_id,
                user_subject=account["user_subject"],
            )
            task_artifacts.sort(
                key=lambda item: (
                    str(item.get("created_at") or ""),
                    str(item.get("artifact_id") or ""),
                )
            )
            items.append(
                {
                    "task": _public_task(task),
                    "plan": None,
                    "events": [],
                    "artifacts": [
                        _public_artifact(item) for item in task_artifacts
                    ],
                    "interaction": None,
                }
            )
        return items

    def analytics_report(self, account: dict, report_id: str) -> dict:
        raise WorkspaceArtifactError('CAPABILITY_RETIRED', '旧数据库报告入口已退役。请重新查询导出。')

    def database_report(self, account: dict, source_id: str, report_id: str) -> dict:
        from agentbridge.database.independent import IndependentDatabase, DatabaseRejected
        from agentbridge.database.reports import Reports
        try:
            return Reports(IndependentDatabase(self.service.home)).web_download(account['user_subject'],source_id,report_id)
        except DatabaseRejected as exc:
            raise WorkspaceArtifactError(str(exc),'此文件不可下载，请检查权限或重新导出。') from exc

    def database_original(self, account: dict, source_id: str, log_id: str) -> dict:
        from agentbridge.database.independent import IndependentDatabase
        return IndependentDatabase(self.service.home).read_original(account['user_subject'], source_id, log_id)

    def reissue_artifact(
        self,
        account: dict,
        *,
        task_id: str,
        artifact_id: str,
    ) -> dict:
        result = self.service.reissue_document_download(
            user_subject=account["user_subject"],
            task_id=task_id,
            artifact_id=artifact_id,
        )
        if result.get("status") != "succeeded":
            code = str(result.get("error", {}).get("code") or "REISSUE_FAILED")
            messages = {
                "LOGIN_REQUIRED": "对应系统登录已失效，请先登录后再重新生成下载。",
                "DOWNLOAD_NOT_FOUND": "原文件记录不存在，无法重新生成下载。",
                "DOWNLOAD_ACCESS_DENIED": "无权重新生成这份文件。",
                "DOWNLOAD_INTEGRITY_FAILED": "原文件记录校验失败，无法重新生成下载。",
                "ARTIFACT_REISSUE_UNSUPPORTED": "这个文件暂不支持重新生成下载。",
                "ARTIFACT_REISSUE_CONFLICT": "文件已在另一端更新，请刷新后重试。",
                "REPORT_REGENERATION_FAILED": "报告数据暂时无法重新读取，请稍后再试。",
            }
            raise WorkspaceArtifactError(
                code,
                messages.get(code, "重新生成文件失败，请稍后再试。"),
            )
        return self.task_detail(account, task_id)

    def continue_task(self, account: dict, task_id: str) -> dict:
        task = self.service.tasks.get_task(task_id, user_subject=account["user_subject"])
        if task.get("status") == "outcome_unknown":
            # A conversational continuation can choose a write path. Unknown
            # outcomes only expose the separate, closed read-only query action.
            raise ValueError("原提交结果仍待核对，请使用只读查询或到业务系统核对；不要重复提交。")
        response = self.service.resolve_host_task_continuation(
            user_subject=account["user_subject"],
            agent_host="openclaw",
            endpoint_key=account["endpoint_key"],
            task_id=task_id,
            reuse_selected=False,
            allow_follow_up=False,
        )
        if response.get("status") != "selected":
            raise KeyError(f"task not found: {task_id}")
        task = response["task"]
        return {
            "status": "selected",
            "task": {
                "task_id": task["taskId"],
                "title": task["title"],
                "status": task["status"],
            },
            "continuation": response["continuation"],
            "message": f"继续刚才选择的“{task['title']}”任务。",
        }

    def list_events(
        self,
        account: dict,
        *,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        return [
            _public_event(event)
            for event in self.service.tasks.list_user_events(
                user_subject=account["user_subject"],
                after_event_id=after_event_id,
                limit=limit,
            )
        ]

    def latest_event_id(self, account: dict) -> str | None:
        return self.service.tasks.latest_user_event_id(
            user_subject=account["user_subject"],
        )

    def event_cursor(self, account: dict) -> str:
        return self.service.tasks.current_user_event_cursor(
            user_subject=account["user_subject"],
        )

    def list_timeline(
        self,
        account: dict,
        *,
        after_sequence: int | None = None,
        before_sequence: int | None = None,
        entry_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        endpoints = {
            endpoint["endpoint_id"]: endpoint
            for endpoint in self.service.tasks.list_endpoints(
                user_subject=account["user_subject"],
                active_only=False,
                limit=100,
            )
        }
        return [
            _public_timeline_entry(
                entry,
                source=endpoints.get(entry.get("source_endpoint_id")),
                is_origin=(
                    entry.get("source_endpoint_id") == account.get("endpoint_id")
                ),
            )
            for entry in self.service.tasks.list_timeline(
                user_subject=account["user_subject"],
                after_sequence=after_sequence,
                before_sequence=before_sequence,
                entry_type=entry_type,
                limit=limit,
            )
        ]

    def timeline_cursor(self, account: dict) -> int:
        return self.service.tasks.latest_timeline_sequence(
            user_subject=account["user_subject"],
        )

    def timeline_attachment(self, account: dict, attachment_id: str) -> dict:
        try:
            attachment = self.service.timeline_attachments.ready_payload(
                attachment_id
            )
        except TimelineAttachmentNotFound as exc:
            raise WorkspaceArtifactError(
                "TIMELINE_ATTACHMENT_NOT_FOUND",
                "The timeline attachment was not found.",
            ) from exc
        except TimelineAttachmentExpired as exc:
            raise WorkspaceArtifactError(
                "TIMELINE_ATTACHMENT_EXPIRED",
                "The timeline attachment has expired.",
            ) from exc
        except TimelineAttachmentIntegrityError as exc:
            raise WorkspaceArtifactError(
                "TIMELINE_ATTACHMENT_INTEGRITY_FAILED",
                "The timeline attachment failed its integrity check.",
            ) from exc
        if attachment["user_subject"] != account["user_subject"]:
            raise PermissionError("timeline attachment belongs to another user")
        return attachment

    def list_endpoints(self, account: dict) -> list[dict]:
        return [
            _public_endpoint(endpoint)
            for endpoint in self.service.tasks.list_endpoints(
                user_subject=account["user_subject"],
                active_only=False,
                limit=100,
            )
        ]

    def gateway_status(self) -> dict:
        if self.gateway is None:
            return {
                "available": False,
                "code": "GATEWAY_NOT_CONFIGURED",
            }
        try:
            result = self.gateway.call(
                "system.info",
                {},
                timeout_seconds=8,
            )
        except GatewayRequestError as exc:
            return {
                "available": False,
                "code": exc.code,
            }
        return {
            "available": True,
            "version": _safe_text(
                result.get("version") if isinstance(result, dict) else None,
                80,
            ),
        }

    def chat_history(self, account: dict, *, limit: int = 100) -> dict:
        gateway = self._gateway()
        result = gateway.call(
            "chat.history",
            {
                "sessionKey": account["openclaw_session_key"],
                "limit": min(max(int(limit), 1), 200),
                "maxChars": 200_000,
            },
            timeout_seconds=20,
        )
        return {
            "messages": _visible_messages(result),
            "sessionId": (
                _safe_text(result.get("sessionId"), 256)
                if isinstance(result, dict)
                else None
            ),
        }

    def chat_stream(
        self,
        account: dict,
        *,
        timeout_seconds: float = 25,
    ) -> Iterator[dict[str, Any]]:
        return self._gateway().stream(
            session_key=account["openclaw_session_key"],
            timeout_seconds=timeout_seconds,
        )

    def send_chat_stream(
        self,
        account: dict,
        *,
        message: str,
        idempotency_key: str | None = None,
        attachments: list[dict] | None = None,
        skill_trial: dict | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._gateway()
        message = str(message or "").strip()
        normalized_attachments = _validated_chat_attachments(attachments)
        skill_trial = normalize_skill_trial(skill_trial)
        if skill_trial is not None and (not message or len(message) > 3000 or '\0' in message or normalized_attachments):
            raise ValueError('短试用仅支持1至3000字的文本，不支持附件')
        if not message and normalized_attachments:
            message = "请处理附加图片中的内容。"
        if not message or len(message) > 20_000:
            raise ValueError("chat message is empty or too long")
        effective_key = _safe_text(idempotency_key, 128) or str(uuid4())
        dispatch, _reused = self.store.accept_chat_request(
            account_id=account["account_id"],
            user_subject=account["user_subject"],
            message=message,
            idempotency_key=effective_key,
            attachments=normalized_attachments,
            tasks=self.service.tasks,
            attachment_store=self.service.timeline_attachments,
            media_base_url=self.service.trusted_card_base_url,
            skill_trial=skill_trial,
            validate_skill_trial=lambda db, trial: self.service.skill_authoring.validate_workspace_trial(
                db, account['user_subject'], trial),
        )
        self._ensure_dispatch_worker(account["account_id"])
        return self._stream_host_dispatch(
            user_subject=account["user_subject"],
            dispatch_id=dispatch["dispatch_id"],
        )

    def list_chat_dispatches(
        self,
        account: dict,
        *,
        active_only: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        items = []
        for dispatch in self.store.list_host_dispatches(
            user_subject=account["user_subject"],
            active_only=active_only,
            limit=limit,
        ):
            try:
                message = self.service.tasks.get_timeline_message(
                    user_subject=account["user_subject"],
                    message_key=dispatch["message_key"],
                )
            except Exception:
                message = {"text": ""}
            items.append(
                {
                    "dispatchId": dispatch["dispatch_id"],
                    "state": dispatch["state"],
                    "runId": dispatch["accepted_run_id"],
                    "idempotencyKey": dispatch["idempotency_key"],
                    "requestMessage": message.get("text") or "",
                    "attemptCount": dispatch["attempt_count"],
                    "retryCount": dispatch["retry_count"],
                    "createdAt": dispatch["created_at"],
                    "updatedAt": dispatch["updated_at"],
                    "deadlineAt": dispatch["deadline_at"],
                    "lastErrorCode": dispatch["last_error_code"],
                }
            )
        return items

    def cancel_chat_dispatch(
        self,
        account: dict,
        dispatch_id: str,
    ) -> dict:
        dispatch = self.store.cancel_host_dispatch(
            dispatch_id,
            user_subject=account["user_subject"],
        )
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text="已取消等待，本次请求未进入业务系统。",
        )
        return {
            "dispatchId": dispatch["dispatch_id"],
            "state": dispatch["state"],
        }

    def close(self) -> None:
        self.service.skill_authoring.workbench.close()
        self._dispatch_stop.set()
        with self._dispatch_workers_lock:
            self._preparation_queue.clear()
            workers = [*self._dispatch_workers.values(), *self._preparation_workers.values()]
        for worker in workers:
            worker.join(timeout=2)

    def _ensure_dispatch_worker(self, account_id: str) -> None:
        if self.gateway is None or self._dispatch_stop.is_set():
            return
        with self._dispatch_workers_lock:
            existing = self._dispatch_workers.get(account_id)
            if existing is not None and existing.is_alive():
                return
            worker = threading.Thread(
                target=self._dispatch_worker,
                args=(account_id,),
                name=f"workspace-dispatch-{account_id[:8]}",
                daemon=True,
            )
            self._dispatch_workers[account_id] = worker
            worker.start()

    def _dispatch_worker(self, account_id: str) -> None:
        current = threading.current_thread()
        try:
            while not self._dispatch_stop.is_set():
                dispatch = self.store.claim_next_host_dispatch(
                    account_id=account_id,
                    claim_owner=self._dispatch_instance_id,
                )
                if dispatch is None:
                    if not self.store.host_dispatch_work_pending(account_id):
                        return
                    self._dispatch_stop.wait(0.2)
                    continue
                try:
                    self._process_host_dispatch(dispatch)
                except Exception as exc:
                    _LOG.exception(
                        "Workspace durable dispatch failed dispatch_id=%s "
                        "error=%s",
                        dispatch["dispatch_id"],
                        exc.__class__.__name__,
                    )
                    self._finish_dispatch_internal_error(dispatch, exc)
        finally:
            with self._dispatch_workers_lock:
                if self._dispatch_workers.get(account_id) is current:
                    self._dispatch_workers.pop(account_id, None)

    def _process_host_dispatch(self, dispatch: dict) -> None:
        self.performance.record(dispatch, "workspace.dispatch.claimed")
        if dispatch["state"] == "accepted":
            self._observe_accepted_dispatch(dispatch)
            return
        if self.store.host_dispatch_deadline_passed(dispatch):
            account = self.store.get_account(dispatch["account_id"])
            if dispatch.get("request_sent_at"):
                self._finish_acceptance_unknown(dispatch, account)
            else:
                self._finish_expired_dispatch(dispatch, account)
            return
        if dispatch["state"] == "reconciling_acceptance":
            self._reconcile_host_acceptance(dispatch)
            return

        account = self.store.get_account(dispatch["account_id"])
        readiness_started = time.perf_counter()
        ready = self._host_readiness()
        self.performance.record(dispatch, "workspace.gateway.readiness", started=readiness_started,
                                status="succeeded" if ready["available"] else "failed")
        if not ready["available"]:
            if ready["recoverable"]:
                self._defer_dispatch(dispatch, ready["code"])
            else:
                self._fail_dispatch_before_accept(
                    dispatch,
                    account,
                    code=ready["code"],
                    text="智能体连接配置无效，本次请求未进入业务系统。",
                )
            return

        try:
            message, attachments = self._host_dispatch_payload(dispatch)
            trial = self.store.skill_trial_for_dispatch(dispatch['dispatch_id'], user_subject=dispatch['user_subject'])
        except Exception as exc:
            self._fail_dispatch_before_accept(
                dispatch,
                account,
                code="HOST_DISPATCH_PAYLOAD_UNAVAILABLE",
                text="原消息或附件已不可用，本次请求未进入业务系统。",
            )
            _LOG.warning(
                "Workspace durable payload unavailable dispatch_id=%s error=%s",
                dispatch["dispatch_id"],
                exc.__class__.__name__,
            )
            return

        dispatch = self.store.record_host_dispatch_attempt(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
        )
        accepted = False
        terminal = False
        run_id = ""
        source = None
        try:
            grant = self.store.issue_gateway_grant(account["account_id"])
            source = self._gateway().send_stream(
                session_key=grant["session_key"],
                endpoint_key=grant["endpoint_key"],
                grant=grant["grant"],
                binding_grant_provider=lambda: self.store.issue_gateway_grant(
                    account["account_id"]
                )["grant"],
                message=message,
                idempotency_key=dispatch["idempotency_key"],
                attachments=attachments,
                timeout_seconds=300,
                retry_before_accept=False,
                abort_accepted_on_close=False,
                **({'require_trial_binding': True} if trial else {}),
            )
            for item in source:
                item_type = item.get("type")
                if item_type == "accepted":
                    run_id = (
                        _safe_text(item.get("runId"), 256)
                        or dispatch["idempotency_key"]
                    )
                    dispatch = self.store.mark_host_dispatch_accepted(
                        dispatch["dispatch_id"],
                        claim_token=dispatch["claim_token"],
                        run_id=run_id,
                        status=_safe_text(item.get("status"), 80)
                        or "started",
                        recovered_from_run_id=_safe_text(item.get("recoveredFromRunId"), 256) or None,
                    )
                    accepted = True
                    self.performance.record(dispatch, "workspace.host.accepted")
                    recovery_attempts = dispatch["retry_count"] + max(
                        dispatch["attempt_count"] - 1,
                        0,
                    )
                    if recovery_attempts:
                        self._record_gateway_recovery(
                            account=account,
                            effective_key=dispatch["idempotency_key"],
                            attempt=recovery_attempts,
                            status="succeeded",
                        )
                    continue
                if item_type == "progress":
                    if item.get("kind") == "tool":
                        self.performance.record(dispatch, "workspace.host.first_tool")
                    self.store.append_host_dispatch_stream_event(
                        dispatch["dispatch_id"],
                        claim_token=dispatch["claim_token"],
                        event_name="host_progress",
                        payload=item,
                        had_tool_activity=(item.get("kind") == "tool"),
                    )
                    continue
                if item_type != "chat":
                    continue
                state = item.get("state")
                if item.get("text"):
                    self.performance.record(dispatch, "workspace.host.first_text")
                if state not in {"final", "error", "aborted"}:
                    self.store.append_host_dispatch_stream_event(
                        dispatch["dispatch_id"],
                        claim_token=dispatch["claim_token"],
                        event_name="host_chat_delta",
                        payload=item,
                    )
                    continue
                self._finish_accepted_dispatch(
                    dispatch,
                    account,
                    item,
                )
                terminal = True
                return
            if accepted and not terminal:
                self._observe_accepted_dispatch(dispatch)
                return
            if not accepted:
                self._reconcile_host_acceptance(dispatch)
        except GatewayRequestError as exc:
            _LOG.warning(
                "Workspace durable Gateway failure dispatch_id=%s run_id=%s "
                "code=%s stage=%s accepted=%s",
                dispatch["dispatch_id"],
                run_id,
                exc.code,
                _safe_text(exc.details.get("stage"), 80),
                accepted,
            )
            if accepted:
                self._observe_accepted_dispatch(dispatch)
            elif _is_safe_recoverable_pre_accept_error(exc):
                self._defer_dispatch(dispatch, exc.code)
            elif _may_have_reached_host(exc):
                dispatch = self.store.mark_host_dispatch_reconciling(
                    dispatch["dispatch_id"],
                    claim_token=dispatch["claim_token"],
                    error_code=exc.code,
                )
                self._reconcile_host_acceptance(dispatch)
            else:
                self._fail_dispatch_before_accept(
                    dispatch,
                    account,
                    code=exc.code,
                    text=_workspace_gateway_failure_text(exc),
                )
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()

    def _host_readiness(self) -> dict:
        now = time.monotonic()
        with self._readiness_lock:
            if self._readiness_cache and now - self._readiness_cache[0] <= 2:
                return dict(self._readiness_cache[1])
        try:
            self._gateway().call("system.info", {}, timeout_seconds=5)
            result = {"available": True, "recoverable": False, "code": None}
        except GatewayRequestError as exc:
            result = {
                "available": False,
                "recoverable": exc.code in _HOST_TRANSPORT_ERROR_CODES,
                "code": exc.code,
            }
        with self._readiness_lock:
            self._readiness_cache = (time.monotonic(), dict(result))
        return result

    def _host_dispatch_payload(
        self,
        dispatch: dict,
    ) -> tuple[str, list[dict]]:
        entry = self.service.tasks.get_timeline_message(
            user_subject=dispatch["user_subject"],
            message_key=dispatch["message_key"],
        )
        attachments = []
        for attachment_id in dispatch["attachment_refs"]:
            record = self.service.timeline_attachments.ready_payload(
                attachment_id
            )
            if (
                record["user_subject"] != dispatch["user_subject"]
                or record["message_key"] != dispatch["message_key"]
            ):
                raise PermissionError(
                    "host dispatch attachment belongs to another message"
                )
            attachments.append(
                {
                    "type": "image",
                    "mimeType": record["content_type"],
                    "fileName": record["filename"],
                    "content": base64.b64encode(record["body"]).decode("ascii"),
                }
            )
        return str(entry["text"]), attachments

    def _defer_dispatch(self, dispatch: dict, error_code: str) -> None:
        retry_count = max(int(dispatch.get("retry_count") or 0), 0)
        delay = _HOST_DISPATCH_RETRY_DELAYS[
            min(retry_count, len(_HOST_DISPATCH_RETRY_DELAYS) - 1)
        ]
        updated = self.store.mark_host_dispatch_waiting(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            error_code=error_code,
            delay_seconds=delay,
        )
        if updated["state"] == "expired":
            account = self.store.get_account(updated["account_id"])
            self._append_workspace_failure(
                account,
                effective_key=updated["idempotency_key"],
                text=(
                    "连接暂未恢复，本次请求未进入业务系统，请稍后重试。"
                ),
            )

    def _reconcile_host_acceptance(self, dispatch: dict) -> None:
        account = self.store.get_account(dispatch["account_id"])
        try:
            evidence = self._gateway().run_history_evidence(
                session_key=dispatch["conversation_ref"],
                run_ref=dispatch["idempotency_key"],
                timeout_seconds=8,
            )
        except AttributeError:
            self._finish_acceptance_unknown(dispatch, account)
            return
        except GatewayRequestError as exc:
            if exc.code in _HOST_TRANSPORT_ERROR_CODES:
                self._defer_acceptance_reconciliation(dispatch, account, exc.code)
            else:
                self._finish_acceptance_unknown(dispatch, account)
            return
        if evidence.get("prompt_observed"):
            run_id = (
                _safe_text(evidence.get("observed_run_id"), 256)
                or dispatch["idempotency_key"]
            )
            dispatch = self.store.mark_host_dispatch_accepted(
                dispatch["dispatch_id"],
                claim_token=dispatch["claim_token"],
                run_id=run_id,
                status="recovered",
            )
            if evidence.get("final_text"):
                self._finish_accepted_dispatch(
                    dispatch,
                    account,
                    {
                        "type": "chat",
                        "runId": run_id,
                        "state": "final",
                        "text": evidence["final_text"],
                        "recovered": True,
                    },
                )
            else:
                self._observe_accepted_dispatch(dispatch)
            return
        self._defer_acceptance_reconciliation(
            dispatch,
            account,
            "HOST_ACCEPTANCE_NOT_OBSERVED",
        )

    def _defer_acceptance_reconciliation(
        self,
        dispatch: dict,
        account: dict,
        error_code: str,
    ) -> None:
        updated = self.store.defer_host_dispatch_reconciliation(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            error_code=error_code,
            delay_seconds=2,
        )
        if updated["state"] == "acceptance_unknown":
            self._append_workspace_failure(
                account,
                effective_key=dispatch["idempotency_key"],
                text=(
                    "无法确认智能体是否已接收，已停止重发，"
                    "请等待系统核对。"
                ),
            )

    def _finish_acceptance_unknown(
        self,
        dispatch: dict,
        account: dict,
    ) -> None:
        text = "无法确认智能体是否已接收，已停止重发，请等待系统核对。"
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text=text,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="acceptance_unknown",
            error_code="HOST_ACCEPTANCE_UNKNOWN",
            event={
                "type": "chat",
                "runId": dispatch["idempotency_key"],
                "state": "error",
                "text": text,
                "safeToRetry": False,
            },
        )

        self.performance.record(dispatch, "workspace.host.terminal", status="unknown", terminal=True)

    def _observe_accepted_dispatch(self, dispatch: dict) -> None:
        account = self.store.get_account(dispatch["account_id"])
        run_id = dispatch["accepted_run_id"] or dispatch["idempotency_key"]
        evidence = None
        if self._dispatch_stop.is_set():
            return
        try:
            evidence = self._gateway().run_history_evidence(
                session_key=dispatch["conversation_ref"], run_ref=run_id, timeout_seconds=8,
            )
        except (AttributeError, GatewayRequestError):
            pass
        if evidence and evidence.get("final_text"):
            self._finish_accepted_dispatch(
                dispatch, account,
                {"type": "chat", "runId": run_id, "state": "final",
                 "text": evidence["final_text"], "recovered": True},
            )
            return
        if not self.store.host_dispatch_result_deadline_passed(dispatch):
            self.store.defer_host_dispatch_result(
                dispatch["dispatch_id"], claim_token=dispatch["claim_token"],
                had_tool_activity=bool(evidence and evidence.get("had_tool_activity")),
            )
            return
        # Saving a sample happens before the host finishes presenting it. Wait
        # for the real final text so history and the Workspace receipt agree.
        # Only use the saved sample after the normal recovery deadline expires.
        trial = self.store.skill_trial_for_dispatch(dispatch['dispatch_id'], user_subject=account['user_subject'],
                                                   account_id=account['account_id'])
        sample = self.service.skill_authoring.workspace_trial_receipt(account['user_subject'], trial) if trial else None
        if sample and sample['status'] == 'reported' and sample['output']:
            self._finish_accepted_dispatch(dispatch, account, {
                'type': 'chat', 'runId': run_id, 'state': 'final', 'text': sample['output'], 'recovered': True})
            return
        text = (
            "请求已接收，但在结果恢复时限内未能确认最终结果。"
            "这不代表后台任务失败；请核对任务状态，系统不会自动重发。"
        )
        verified = self._verified_trial_event(dispatch, {'state': 'error', 'text': text})
        text = verified['text']
        trial_payload = {'skillTrial': verified['skillTrial']} if verified.get('skillTrial') else {}
        self._append_workspace_message(
            account,
            role="assistant",
            text=text,
            message_key=f"workspace:assistant:error:{run_id}",
            payload=trial_payload,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="result_unknown",
            error_code="HOST_RUN_RESULT_UNAVAILABLE",
            event={
                "type": "chat",
                "runId": run_id,
                "state": "error",
                "text": text,
                "safeToRetry": False,
                **trial_payload,
                "hadToolActivity": bool(
                    dispatch.get("had_tool_activity") or (evidence and evidence.get("had_tool_activity"))
                ),
            },
        )

    def _verified_trial_event(self, dispatch: dict, item: dict) -> dict:
        # Even a host-supplied status object must never become a saved receipt.
        item = {key: value for key, value in item.items() if key != 'skillTrial'}
        trial = self.store.skill_trial_for_dispatch(dispatch['dispatch_id'], user_subject=dispatch['user_subject'],
                                                   account_id=dispatch['account_id'])
        if trial:
            sample = self.service.skill_authoring.workspace_trial_receipt(dispatch['user_subject'], trial)
            reported = bool(sample and sample['status'] == 'reported' and sample['output'])
            item['skillTrial'] = {
                'draft_id': trial['draft_id'], 'name': trial['name'],
                'revision': trial['expected_revision'], 'profile': trial['profile'],
                'dispatch_id': trial['dispatch_id'], 'test_id': sample['test_id'] if sample else None,
                'status': 'reported' if reported else 'not_reported',
            }
            # Preserve diagnostic/model text; only this separately verified
            # status can tell the client that the sample was saved.
            item['text'] = (_safe_text(item.get('text'), 200_000) or
                            (sample['output'] if reported else _terminal_chat_failure_text(item)))
            # The host history and Workspace timeline deduplicate by text.
            # Keep the existing text normalization; the verified
            # skillTrial card, not a suffix, communicates whether saving worked.
        return item

    def _finish_accepted_dispatch(
        self,
        dispatch: dict,
        account: dict,
        item: dict,
    ) -> None:
        item = self._verified_trial_event(dispatch, item)
        trial_payload = {'skillTrial': item['skillTrial']} if item.get('skillTrial') else {}
        state = item.get("state")
        run_id = (
            _safe_text(item.get("runId"), 256)
            or dispatch["accepted_run_id"]
            or dispatch["idempotency_key"]
        )
        if state == "final":
            text = _safe_text(item.get("text"), 200_000)
            if text:
                self._append_workspace_message(
                    account,
                    role="assistant",
                    text=text,
                    message_key=f"workspace:assistant:{run_id}",
                    payload=trial_payload,
                )
            terminal_state = "completed"
        else:
            text = item['text'] if trial_payload else _terminal_chat_failure_text(item)
            self._append_workspace_message(
                account,
                role="assistant",
                text=text,
                message_key=f"workspace:assistant:error:{run_id}",
                payload=trial_payload,
            )
            terminal_state = "failed"
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state=terminal_state,
            error_code=(
                None if terminal_state == "completed" else "HOST_RUN_FAILED"
            ),
            event={**item, "runId": run_id, "text": text},
        )
        self.performance.record(dispatch, "workspace.host.terminal",
                                status="succeeded" if terminal_state == "completed" else "failed", terminal=True)

    def _fail_dispatch_before_accept(
        self,
        dispatch: dict,
        account: dict,
        *,
        code: str,
        text: str,
    ) -> None:
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text=text,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="failed_before_accept",
            error_code=code,
            event={
                "type": "chat",
                "runId": dispatch["idempotency_key"],
                "state": "error",
                "text": text,
                "safeToRetry": True,
            },
        )

        self.performance.record(dispatch, "workspace.host.terminal", status="failed", terminal=True)

    def _finish_expired_dispatch(
        self,
        dispatch: dict,
        account: dict,
    ) -> None:
        text = "连接暂未恢复，本次请求未进入业务系统，请稍后重试。"
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text=text,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="expired",
            error_code="HOST_ACCEPT_TIMEOUT",
            event={
                "type": "chat",
                "runId": dispatch["idempotency_key"],
                "state": "error",
                "text": text,
                "safeToRetry": True,
            },
        )

        self.performance.record(dispatch, "workspace.host.terminal", status="failed", terminal=True)

    def _finish_dispatch_internal_error(
        self,
        dispatch: dict,
        exc: Exception,
    ) -> None:
        try:
            current = self.store.get_host_dispatch(dispatch["dispatch_id"])
            if current["state"] in _HOST_DISPATCH_TERMINAL_STATES:
                return
            if not current.get("claim_token") or current["claim_token"] != dispatch.get("claim_token"):
                return
            if current["state"] == "accepted":
                self.store.defer_host_dispatch_result(
                    current["dispatch_id"], claim_token=current["claim_token"],
                )
                return
            account = self.store.get_account(current["account_id"])
            if current.get("request_sent_at"):
                self._finish_acceptance_unknown(current, account)
                return
            self._fail_dispatch_before_accept(
                current,
                account,
                code="HOST_DISPATCH_INTERNAL_ERROR",
                text="智能体请求协调失败，本次请求已安全停止。",
            )
        except Exception:
            _LOG.error(
                "Workspace durable dispatch could not record internal failure "
                "dispatch_id=%s original_error=%s",
                dispatch.get("dispatch_id"),
                exc.__class__.__name__,
            )

    def _stream_host_dispatch(
        self,
        *,
        user_subject: str,
        dispatch_id: str,
    ) -> Iterator[dict[str, Any]]:
        sequence = 0
        dispatch = self.store.get_host_dispatch(dispatch_id, user_subject=user_subject)

        def public_event(event):
            payload = {key: value for key, value in event['payload'].items() if key != 'skillTrial'}
            if payload.get('type') == 'chat' and payload.get('state') in {'final', 'error', 'aborted'}:
                payload = self._verified_trial_event(dispatch, payload)
            return {**payload, 'dispatchId': dispatch_id}

        while True:
            events = self.store.list_host_dispatch_events(
                dispatch_id,
                after_sequence=sequence,
                limit=200,
            )
            for event in events:
                sequence = event["sequence"]
                yield public_event(event)
            dispatch = self.store.get_host_dispatch(
                dispatch_id,
                user_subject=user_subject,
            )
            if dispatch["state"] in _HOST_DISPATCH_TERMINAL_STATES:
                while True:
                    terminal_events = self.store.list_host_dispatch_events(
                        dispatch_id,
                        after_sequence=sequence,
                        limit=200,
                    )
                    if not terminal_events:
                        break
                    for event in terminal_events:
                        sequence = event["sequence"]
                        yield public_event(event)
                return
            time.sleep(0.1)

    def _record_gateway_recovery(
        self,
        *,
        account: dict,
        effective_key: str,
        attempt: int,
        status: str,
        error_code: str | None = None,
    ) -> None:
        try:
            action, reused = self.service.runtime_governance.start_recovery_action(
                action_type="workspace_host_pre_accept_recovery",
                target_type="workspace_account",
                target_id=str(account["account_id"]),
                actor="agentbridge",
                reason="Agent Host recovered before accepting the Workspace request.",
                idempotency_key=(
                    f"workspace:{account['account_id']}:{effective_key}:"
                    f"gateway-recovery:{max(attempt, 1)}"
                ),
                side_effect_boundary="B0_NO_EFFECT",
                before={"attempt": max(attempt - 1, 0)},
            )
            if reused and action["status"] != "running":
                return
            self.service.runtime_governance.finish_recovery_action(
                action["action_id"],
                status=status,
                after={"attempt": max(attempt, 1)},
                error_code=error_code,
            )
        except Exception as exc:
            _LOG.warning(
                "Workspace Gateway recovery ledger failed: error=%s",
                exc.__class__.__name__,
            )

    def send_chat(
        self,
        account: dict,
        *,
        message: str,
        idempotency_key: str | None = None,
        attachments: list[dict] | None = None,
        skill_trial: dict | None = None,
    ) -> WorkspaceChatResult:
        run_id = ""
        status = "completed"
        for item in self.send_chat_stream(
            account,
            message=message,
            idempotency_key=idempotency_key,
            attachments=attachments,
            skill_trial=skill_trial,
        ):
            if item.get("type") != "accepted":
                continue
            run_id = _safe_text(item.get("runId"), 256)
            status = _safe_text(item.get("status"), 80) or "accepted"
        if not run_id:
            raise GatewayRequestError(
                "GATEWAY_RESPONSE_INVALID",
                "OpenClaw Gateway did not accept the Workspace run.",
            )
        return WorkspaceChatResult(run_id=run_id, status=status)

    def _append_workspace_message(
        self,
        account: dict,
        *,
        role: str,
        text: str,
        message_key: str,
        payload: dict[str, Any] | None = None,
        required: bool = False,
    ) -> None:
        endpoint_id = _safe_text(account.get("endpoint_id"), 128)
        if not endpoint_id:
            if required:
                raise RuntimeError("workspace endpoint is unavailable")
            return
        try:
            self.service.tasks.append_timeline_message(
                user_subject=account["user_subject"],
                source_endpoint_id=endpoint_id,
                message_key=message_key,
                role=role,
                text=text,
                payload=payload,
            )
        except (KeyError, RuntimeError, ValueError, sqlite3.Error):
            if required:
                raise
            # Chat remains available if the optional cross-end projection fails.
            return

    def _append_workspace_failure(
        self,
        account: dict,
        *,
        effective_key: str,
        text: str,
    ) -> None:
        trial = self.store.skill_trial_for_turn(account_id=account['account_id'],
                                                user_subject=account['user_subject'], turn_ref=effective_key)
        payload = {}
        if trial:
            dispatch = self.store.get_host_dispatch(trial['dispatch_id'], user_subject=account['user_subject'])
            event = self._verified_trial_event(dispatch, {'state': 'error', 'text': text})
            text = event['text']
            payload = {'skillTrial': event['skillTrial']}
        self._append_workspace_message(
            account,
            role="assistant",
            text=text,
            message_key=f"workspace:assistant:error:{effective_key}",
            payload=payload,
        )

    def _gateway(self) -> OpenClawGatewayClient:
        if self.gateway is None:
            raise GatewayRequestError(
                "GATEWAY_NOT_CONFIGURED",
                "OpenClaw Gateway is not configured.",
            )
        return self.gateway


def _is_safe_recoverable_pre_accept_error(
    error: GatewayRequestError,
) -> bool:
    if error.code not in _HOST_TRANSPORT_ERROR_CODES:
        return False
    stage = _safe_text(error.details.get("stage"), 80)
    if stage:
        return stage in _HOST_SAFE_PRE_ACCEPT_STAGES
    return error.code in {"GATEWAY_CONNECTION_FAILED", "GATEWAY_PROCESS_FAILED"}


def _may_have_reached_host(error: GatewayRequestError) -> bool:
    stage = _safe_text(error.details.get("stage"), 80)
    if stage in _HOST_SAFE_PRE_ACCEPT_STAGES:
        return False
    return error.code in {
        "GATEWAY_CONNECTION_CLOSED",
        "GATEWAY_RESPONSE_INVALID",
        "GATEWAY_TIMEOUT",
    }


def _validated_chat_attachments(values: list[dict] | None) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_CHAT_ATTACHMENTS:
        raise ValueError("chat attachments must contain at most 4 images")
    normalized = []
    total_bytes = 0
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError("chat attachment is invalid")
        mime_type = str(value.get("mimeType") or "").strip().lower()
        signature = _CHAT_IMAGE_TYPES.get(mime_type)
        if signature is None:
            raise ValueError("chat attachment type is unsupported")
        content = str(value.get("content") or "").strip()
        if not content or content.startswith("data:"):
            raise ValueError("chat attachment content is invalid")
        try:
            body = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("chat attachment content is invalid") from exc
        if not body or len(body) > MAX_CHAT_ATTACHMENT_BYTES:
            raise ValueError("chat attachment exceeds the per-image size limit")
        if mime_type == "image/webp":
            magic_matches = (
                body.startswith(signature[0])
                and len(body) >= 12
                and body[8:12] == b"WEBP"
            )
        else:
            magic_matches = body.startswith(signature[0])
        if not magic_matches:
            raise ValueError("chat attachment content does not match its image type")
        total_bytes += len(body)
        if total_bytes > MAX_CHAT_ATTACHMENTS_TOTAL_BYTES:
            raise ValueError("chat attachments exceed the total size limit")
        supplied_name = str(value.get("fileName") or "").replace("\\", "/")
        supplied_name = supplied_name.rsplit("/", 1)[-1].strip()
        if not supplied_name or any(ord(character) < 32 for character in supplied_name):
            supplied_name = f"image-{index + 1}{signature[1]}"
        normalized.append(
            {
                "type": "image",
                "mimeType": mime_type,
                "fileName": supplied_name[:120],
                "content": content,
            }
        )
    return normalized


def _workspace_gateway_failure_text(error: GatewayRequestError) -> str:
    code = str(error.code or "GATEWAY_REQUEST_FAILED")
    details = error.details if isinstance(error.details, dict) else {}
    if code == "WORKSPACE_RUN_IN_PROGRESS":
        return "\u4e0a\u4e00\u6761\u7f51\u9875\u4efb\u52a1\u4ecd\u5728\u5904\u7406\uff0c\u672c\u6b21\u8bf7\u6c42\u6ca1\u6709\u6392\u961f\u3002"
    if code == "GATEWAY_RUN_TIMEOUT_ABORTED":
        if details.get("hadToolActivity") is True:
            return "\u667a\u80fd\u4f53\u8fd0\u884c\u8d85\u65f6\uff0c\u5df2\u505c\u6b62\u540e\u7eed\u5904\u7406\uff1b\u5982\u6d89\u53ca\u5199\u64cd\u4f5c\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u7ed3\u679c\u3002"
        return "\u667a\u80fd\u4f53\u8fd0\u884c\u8d85\u65f6\uff0c\u5df2\u5b89\u5168\u4e2d\u6b62\uff1b\u672c\u6b21\u5c1a\u672a\u8c03\u7528\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_RUN_TIMEOUT_ABORT_UNCONFIRMED":
        return "OpenClaw \u6682\u65f6\u65e0\u54cd\u5e94\uff0c\u672c\u6b21\u8bf7\u6c42\u5df2\u505c\u6b62\u7ee7\u7eed\u6392\u961f\uff1b\u5982\u6d89\u53ca\u5199\u64cd\u4f5c\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u7ed3\u679c\u3002"
    if code == "GATEWAY_SESSION_NOT_IDLE":
        return "\u4e0a\u4e00\u6761\u667a\u80fd\u4f53\u4efb\u52a1\u672a\u80fd\u53ca\u65f6\u7ed3\u675f\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_SESSION_STATE_UNAVAILABLE":
        return "\u6682\u65f6\u65e0\u6cd5\u786e\u8ba4\u667a\u80fd\u4f53\u4f1a\u8bdd\u662f\u5426\u7a7a\u95f2\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_START_STALLED_ABORTED":
        return "\u667a\u80fd\u4f53\u81ea\u52a8\u6062\u590d\u540e\u4ecd\u672a\u80fd\u542f\u52a8\uff0c\u5df2\u5b89\u5168\u4e2d\u6b62\uff1b\u672c\u6b21\u5c1a\u672a\u8c03\u7528\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_START_STALLED_ABORT_UNCONFIRMED":
        return "\u667a\u80fd\u4f53\u542f\u52a8\u72b6\u6001\u65e0\u6cd5\u786e\u8ba4\uff0c\u5df2\u505c\u6b62\u81ea\u52a8\u6062\u590d\uff1b\u8bf7\u7a0d\u540e\u67e5\u8be2\u4efb\u52a1\u72b6\u6001\u3002"
    if code == "GATEWAY_START_RECOVERY_BLOCKED_TOOL_ACTIVITY":
        return "\u667a\u80fd\u4f53\u542f\u52a8\u5f02\u5e38\uff0c\u4f46\u68c0\u6d4b\u5230\u672c\u8f6e\u5df2\u5c1d\u8bd5\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff1b\u5df2\u505c\u6b62\u81ea\u52a8\u91cd\u653e\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u72b6\u6001\u3002"
    if code == "GATEWAY_START_RECOVERY_EVIDENCE_UNAVAILABLE":
        return "\u667a\u80fd\u4f53\u542f\u52a8\u5f02\u5e38\uff0c\u4e14\u65e0\u6cd5\u786e\u8ba4\u672c\u8f6e\u662f\u5426\u5df2\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff1b\u5df2\u505c\u6b62\u81ea\u52a8\u91cd\u653e\u3002"
    if code.startswith("GATEWAY_"):
        if details.get("hadToolActivity") is True:
            return (
                "OpenClaw \u5b9e\u65f6\u8fde\u63a5\u5df2\u4e2d\u65ad\uff1b\u672c\u6b21\u5df2\u8c03\u7528\u4e1a\u52a1\u80fd\u529b\uff0c"
                "\u4f46\u672a\u80fd\u53d6\u5f97\u6700\u7ec8\u7b54\u590d\u3002AgentBridge \u4e0d\u4f1a\u81ea\u52a8\u91cd\u653e\uff1b"
                "\u8bf7\u5237\u65b0\u67e5\u770b\u5df2\u843d\u76d8\u7ed3\u679c\uff0c\u6d89\u53ca\u5199\u64cd\u4f5c\u65f6\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u3002"
            )
        return "OpenClaw \u6682\u65f6\u65e0\u54cd\u5e94\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u7ee7\u7eed\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002"
    return f"\u667a\u80fd\u4f53\u672a\u80fd\u5b8c\u6210\u672c\u6b21\u8bf7\u6c42\uff08\u9519\u8bef\u7801\uff1a{code}\uff09\u3002"


def _terminal_chat_failure_text(item: dict[str, Any]) -> str:
    supplied = _safe_text(item.get("text"), 2_000)
    if supplied:
        return supplied
    if item.get("state") == "aborted":
        if item.get("hadToolActivity") is True:
            return "\u667a\u80fd\u4f53\u8fd0\u884c\u5df2\u505c\u6b62\uff1b\u4efb\u52a1\u5df2\u7ecf\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u72b6\u6001\u3002"
        return "\u667a\u80fd\u4f53\u8fd0\u884c\u5df2\u505c\u6b62\uff0c\u5c1a\u672a\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff0c\u53ef\u4ee5\u5b89\u5168\u5730\u91cd\u65b0\u53d1\u9001\u3002"
    return "\u667a\u80fd\u4f53\u672a\u80fd\u5b8c\u6210\u672c\u6b21\u8bf7\u6c42\u3002"


def _public_account(account: dict) -> dict:
    return {
        "accountId": account["account_id"],
        "username": account["username"],
        "state": account["state"],
        "lastLoginAt": account["last_login_at"],
    }


def _public_task(task: dict) -> dict:
    names = (
        "task_id",
        "agent_host",
        "title",
        "status",
        "summary",
        "current_operation_id",
        "current_interaction_id",
        "version",
        "created_at",
        "updated_at",
        "finished_at",
    )
    result = {name: task.get(name) for name in names}
    summary = dict(task.get("summary") or {})
    recovery = summary.pop("oaPendingRecovery", None)
    if (isinstance(recovery, dict) and isinstance(recovery.get("originTaskId"), str)
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", recovery["originTaskId"])):
        summary["oaPendingRecovery"] = {"originalTaskId": recovery["originTaskId"]}
    command = summary.pop("command", None)
    if isinstance(command, dict):
        summary["preparation"] = {"state": command.get("state"),
            "selectedCount": len(command.get("selectedItems") or [])}
        if command.get("kind") in {"oa_pending_query", "oa_pending_prepare"}:
            summary["preparation"]["kind"] = command["kind"]
    result["summary"] = summary
    return result


_BUSINESS_READBACK_REASONS = frozenset({
    "read_budget_exceeded", "done_scan_unproven",
    "still_pending", "pending_response_invalid", "pending_coverage_incomplete",
    "pending_coverage_inconsistent", "done_response_invalid", "done_affair_not_found",
    "done_affair_ambiguous", "done_title_mismatch", "done_detail_affair_mismatch",
    "done_detail_identity_unavailable", "done_identity_mismatch", "frozen_identity_incomplete",
})
_BUSINESS_READINESS_REASONS = frozenset({
    "body_loading", "content_loading", "submit_entry_unavailable", "page_unavailable", "page_loading",
})


def _unconsumed_pending_attempt(operation: dict, confirmation: sqlite3.Row) -> bool:
    """Prove this failed attempt stayed before its exact authorization boundary."""
    from agentbridge.adapters.seeyon_pending_actions import PendingActionContractMismatch
    from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
    pending_commits = {item["commit_capability"] for item in _TRUSTED_WRITE_DEFINITIONS.values()
                       if item["contract_error"] is PendingActionContractMismatch}
    if (operation.get("status") != "failed" or operation.get("error_code") != "CAPABILITY_EXECUTION_FAILED"
            or operation.get("capability_name") not in pending_commits
            or confirmation["authorization_state"] != "approved"
            or confirmation["commit_operation_id"] is not None or confirmation["consumed_at"] is not None
            or confirmation["authorization_capability"] != operation.get("capability_name")
            or confirmation["authorization_version"] != operation.get("capability_version")):
        return False
    exact_input = json.dumps({"authorization_id": confirmation["authorization_id"]},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(exact_input.encode()).hexdigest() == operation.get("input_hash")


def _business_error_evidence(operation: dict) -> dict:
    """Read only a bounded trailing object emitted by the controlled executor.

    Historic OA errors combine prose and diagnostic JSON. Neither the prose nor
    arbitrary nested fields are public API; callers separately whitelist values.
    """
    if operation.get("error_code") not in {"RESULT_UNKNOWN", "OA_PENDING_ACTION_NOT_READY"}:
        return {}
    message = operation.get("error_message")
    if not isinstance(message, str) or len(message) > 65_536:
        return {}
    decoder = json.JSONDecoder()
    starts = [match.start() for match in re.finditer(r"\{", message)][:8]
    for start in starts:
        try:
            evidence, end = decoder.raw_decode(message, start)
        except (ValueError, RecursionError):
            continue
        if isinstance(evidence, dict) and not message[end:].strip():
            return evidence
    return {}


def _public_observation_time(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:\+00:00|Z)", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _public_business_execution(operation: dict, task: dict) -> dict | None:
    from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
    commits = {definition["commit_capability"] for definition in _TRUSTED_WRITE_DEFINITIONS.values()}
    capability = operation.get("capability_name")
    if capability not in commits:
        return None
    status = operation.get("status")
    phase = {"running": "preparing_submission", "created": "preparing_submission",
        "unknown": "outcome_unknown", "succeeded": "verified", "failed": "failed"}.get(status)
    if operation.get("error_code") == "OA_PENDING_ACTION_NOT_READY" and status == "failed":
        phase = "not_submitted"
    if task.get("status") == "outcome_unknown":
        phase = "outcome_unknown"
    elif task.get("status") == "failed" and phase == "verified":
        phase = "failed"
    if phase is None:
        return None
    result = {"schemaVersion": "agentbridge.business-execution.v1", "phase": phase}
    updated_at = _public_observation_time(operation.get("updated_at"))
    if updated_at:
        result["updatedAt"] = updated_at
    evidence = operation.get("result") if status == "succeeded" else _business_error_evidence(operation)
    evidence = evidence if isinstance(evidence, dict) else {}
    execution = evidence.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    readback = execution.get("readback", evidence.get("readback"))
    readback = readback if isinstance(readback, dict) else {}
    readiness = execution.get("readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    reason = readiness.get("reason") if phase == "not_submitted" else readback.get("reason")
    allowed = _BUSINESS_READINESS_REASONS if phase == "not_submitted" else _BUSINESS_READBACK_REASONS
    if isinstance(reason, str) and reason in allowed:
        result["reason"] = reason
    observed_at = _public_observation_time(readback.get("last_observed_at"))
    if observed_at:
        result["lastObservedAt"] = observed_at
    attempts = readback.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool) and 0 <= attempts <= 1000:
        result["observationCount"] = attempts
    dispatch = execution.get("dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    if dispatch.get("state") in {"scheduled", "invoked", "not_invoked", "observation_unavailable"}:
        result["submissionState"] = dispatch["state"]
    # Querying current pending items is read-only and never changes the original
    # unknown result or proves success merely because the item is absent.
    if capability.startswith("oa.") and task.get("status") == "outcome_unknown":
        result["readOnlyQuery"] = "oa_pending"
    return result


def _public_event(event: dict) -> dict:
    names = (
        "event_id",
        "task_id",
        "event_type",
        "created_at",
    )
    return {name: event.get(name) for name in names}


def _public_artifact(artifact: dict) -> dict:
    state = artifact.get("state")
    return {
        "artifact_id": artifact.get("artifact_id"),
        "task_id": artifact.get("task_id"),
        "artifact_type": artifact.get("artifact_type"),
        "filename": artifact.get("filename"),
        "content_type": artifact.get("content_type"),
        "byte_size": artifact.get("byte_size"),
        "download_url": (
            artifact.get("download_url") if state == "ready" else None
        ),
        "state": state,
        "created_at": artifact.get("created_at"),
        "expires_at": artifact.get("expires_at"),
    }


def _public_timeline_entry(
    entry: dict,
    *,
    source: dict | None,
    is_origin: bool,
) -> dict:
    client_type = str((source or {}).get("client_type") or "system")
    display_label = (source or {}).get("label") or {
        "web": "Agent Workspace",
        "webchat": "Agent Workspace",
        "telegram": "Telegram",
        "openclaw-weixin": "微信",
        "system": "AgentBridge",
    }.get(client_type, "已关联客户端")
    dedupe_key = str(entry.get("dedupe_key") or "")
    message_key = (
        dedupe_key.removeprefix("message:")
        if entry.get("entry_type") == "chat_message"
        and dedupe_key.startswith("message:")
        else None
    )
    payload = entry.get("payload") or {}
    event_type = payload.get("eventType") if isinstance(payload, dict) else None
    if entry.get("entry_type") == "task_event" and isinstance(event_type, str) and event_type.startswith("task.oa_recovery."):
        payload = {"eventType": event_type, "taskId": entry.get("task_id")}
    if entry.get("entry_type") == "task_event" and event_type == "task.business.progress":
        from agentbridge.core.business_execution_progress import PENDING_PHASES
        observation = payload.get("payload") or {}
        if not isinstance(observation, dict):
            observation = {}
        payload = {"eventType":event_type, "taskId":entry.get("task_id")}
        if observation.get("phase") in PENDING_PHASES:
            payload["phase"] = observation["phase"]
        for key in ("operationId", "requestId"):
            value = observation.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
                payload[key] = value
        observed = _public_observation_time(observation.get("observedAt"))
        if observed:
            payload["observedAt"] = observed
    return {
        "entry_id": entry["entry_id"],
        "sequence": entry["sequence"],
        "entry_type": entry["entry_type"],
        "message_key": message_key,
        "task_id": entry.get("task_id"),
        "role": entry.get("role"),
        "text": entry.get("text"),
        "payload": payload,
        "created_at": entry["created_at"],
        "source": {
            "client_type": client_type,
            "display_label": display_label,
            "is_origin": bool(is_origin),
        },
    }


def _public_endpoint(endpoint: dict) -> dict:
    client_type = str(endpoint.get("client_type") or "unknown")
    display_label = endpoint.get("label") or {
        "web": "Agent Workspace",
        "telegram": "Telegram",
        "openclaw-weixin": "微信",
    }.get(client_type, "已关联客户端")
    return {
        "endpoint_id": endpoint.get("endpoint_id"),
        "client_type": client_type,
        "display_label": display_label,
        "capabilities": list(endpoint.get("capabilities") or []),
        "state": endpoint.get("state"),
        "created_at": endpoint.get("created_at"),
        "updated_at": endpoint.get("updated_at"),
        "last_seen_at": endpoint.get("last_seen_at"),
    }


def _visible_messages(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    values = result.get("messages")
    if not isinstance(values, list):
        return []
    messages: list[dict] = []
    for value in values[-200:]:
        if not isinstance(value, dict):
            continue
        role = str(value.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(value.get("content"))
        if not text:
            continue
        messages.append(
            {
                "id": _safe_text(
                    value.get("id") or value.get("messageId"),
                    256,
                ),
                "role": role,
                "text": text,
                "timestamp": _safe_text(
                    _visible_timestamp(
                        value.get("timestamp")
                        or value.get("createdAt")
                        or value.get("created_at")
                    ),
                    80,
                ),
            }
        )
    return messages


def _visible_timestamp(value: Any) -> str | None:
    text = _safe_text(value, 80)
    if not text:
        return None
    if text.isdigit():
        numeric = int(text)
        if 1_000_000_000 <= numeric < 10_000_000_000:
            seconds = float(numeric)
        elif 1_000_000_000_000 <= numeric < 10_000_000_000_000:
            seconds = numeric / 1_000
        else:
            return text
        try:
            return datetime.fromtimestamp(
                seconds,
                tz=timezone.utc,
            ).isoformat(timespec="milliseconds")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content[:50_000]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "input_text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if sum(len(part) for part in parts) >= 50_000:
            break
    return "\n".join(parts)[:50_000]


def _safe_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    normalized = str(value).strip()
    return normalized[:maximum] if normalized else None
