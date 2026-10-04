from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from bscli.core.task_projections import build_plan_result_projection, plan_result_response
from bscli.core.task_plan_validation import (
    PlanValidationError,
    resolve_json_pointer,
    validate_runtime_arguments,
)
from bscli.core.task_plans import (
    ACTIVE_PLAN_STATES,
    TaskPlanCommitInProgress,
    TaskPlanIntegrityError,
    TaskPlanStore,
    json_hash,
    step_idempotency_key,
    task_plan_response,
)
from bscli.core.transforms import TransformRegistry, TransformRejected
from bscli.core.capability_runtime import CapabilityRejected


class TaskPlanRuntime:
    def __init__(
        self,
        *,
        service: Any,
        plans: TaskPlanStore,
        transforms: TransformRegistry,
    ) -> None:
        self.service = service
        self.plans = plans
        self.transforms = transforms
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._started_at = datetime.now(timezone.utc)

    def start(self, plan_id: str, *, user_subject: str) -> dict[str, Any]:
        plan = self.plans.get(plan_id, user_subject=user_subject)
        self._event(plan, "plan.started", {"status": "running"})
        return self.advance(plan_id, user_subject=user_subject)

    def advance(self, plan_id: str, *, user_subject: str) -> dict[str, Any]:
        with self._plan_lock(plan_id):
            while True:
                plan = self.plans.get(plan_id, user_subject=user_subject)
                if plan["state"] not in ACTIVE_PLAN_STATES:
                    return self._plan_result(plan)
                if plan.get("commit_operation_id"):
                    if all(s["state"] in {"succeeded", "skipped"} for s in plan["steps"]):
                        return self._complete_plan(plan_id, user_subject=user_subject, reason="all_steps_succeeded")
                    return {
                        "protocolVersion": "0.1", "status": "running",
                        "operationId": plan["commit_operation_id"], "plan": task_plan_response(plan),
                        "nextAction": {"type": "wait_for_verification", "doNotRetry": True},
                    }
                step = self.plans.begin_next_step(
                    plan_id, user_subject=user_subject
                )
                if step is None:
                    return self._complete_plan(
                        plan_id,
                        user_subject=user_subject,
                        reason="all_steps_succeeded",
                    )
                if step["state"] == "waiting_user":
                    return self._waiting_result(plan, step)
                if step.get("just_started"):
                    self._event(
                        plan,
                        "plan.step.started",
                        self._step_event_payload(plan, step),
                    )
                try:
                    authority_validator = getattr(
                        self.service, "validate_task_plan_authority", None
                    )
                    if callable(authority_validator):
                        authority_validator(plan)
                    self.validate_versions(plan)
                    arguments = self._resolve_step_arguments(
                        plan=plan,
                        step=step,
                    )
                    if step["kind"] == "capability":
                        response = self._invoke_capability(plan, step, arguments)
                    else:
                        response = self._invoke_transform(plan, step, arguments)
                    result = self._consume_step_response(plan, step, response)
                except (PlanValidationError, TransformRejected) as exc:
                    return self._fail_plan(
                        plan,
                        step,
                        operation_id=None,
                        state="failed",
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                except Exception as exc:
                    return self._fail_plan(
                        plan,
                        step,
                        operation_id=None,
                        state="failed",
                        error_code="PLAN_STEP_EXECUTION_FAILED",
                        error_message=str(exc) or exc.__class__.__name__,
                    )
                if result is not None:
                    return result

    def resume_after_session(
        self,
        *,
        user_subject: str,
        interaction_id: str,
    ) -> dict[str, Any] | None:
        bound = self.plans.step_for_interaction(
            interaction_id, user_subject=user_subject
        )
        if bound is None:
            return None
        plan, _step = bound
        with self._plan_lock(plan["plan_id"]):
            plan = self.plans.get(plan["plan_id"], user_subject=user_subject)
            if plan["state"] not in ACTIVE_PLAN_STATES:
                return self._plan_result(plan)
            self.plans.reset_step_after_session(
                plan["plan_id"],
                user_subject=user_subject,
                interaction_id=interaction_id,
            )
            self._event(
                plan,
                "plan.step.resumed",
                {
                    "planId": plan["plan_id"],
                    "stepKey": _step["step_key"],
                    "reason": "session_ready",
                },
                causation_ref=interaction_id,
            )
            return self.advance(plan["plan_id"], user_subject=user_subject)

    def resume_after_capability(
        self,
        *,
        user_subject: str,
        interaction_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        bound = self.plans.step_for_interaction(
            interaction_id, user_subject=user_subject
        )
        if bound is None:
            return None
        plan, step = bound
        with self._plan_lock(plan["plan_id"]):
            plan = self.plans.get(plan["plan_id"], user_subject=user_subject)
            if plan["state"] not in ACTIVE_PLAN_STATES:
                self._retire_interactions(plan, extra=response.get("interaction"))
                return self._plan_result(plan)
            result = self._consume_step_response(plan, step, response)
            if result is not None:
                return result
            return self.advance(plan["plan_id"], user_subject=user_subject)

    def handle_terminal_interaction(
        self,
        *,
        user_subject: str,
        interaction_id: str,
        interaction_state: str,
    ) -> dict[str, Any] | None:
        bound = self.plans.step_for_interaction(
            interaction_id, user_subject=user_subject
        )
        if bound is None:
            return None
        plan, step = bound
        with self._plan_lock(plan["plan_id"]):
            plan = self.plans.get(plan["plan_id"], user_subject=user_subject)
            if plan["state"] not in ACTIVE_PLAN_STATES:
                return self._plan_result(plan)
            if interaction_state == "declined":
                return self.cancel(
                    plan["plan_id"],
                    user_subject=user_subject,
                    reason="interaction_declined",
                )
            state = "failed"
            error_code = {
                "declined": "INTERACTION_DECLINED",
                "expired": "INTERACTION_EXPIRED",
                "failed": "INTERACTION_FAILED",
                "superseded": "INTERACTION_SUPERSEDED",
            }.get(interaction_state, "INTERACTION_NOT_READY")
            return self._fail_plan(
                plan,
                step,
                operation_id=step.get("operation_id"),
                state=state,
                error_code=error_code,
                error_message=f"可信交互已结束：{interaction_state}。",
            )

    def cancel(
        self,
        plan_id: str,
        *,
        user_subject: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._plan_lock(plan_id):
            current = self.plans.get(plan_id, user_subject=user_subject)
            if current["state"] not in ACTIVE_PLAN_STATES:
                return self._plan_result(current)
            try:
                plan = self.plans.cancel(
                    plan_id, user_subject=user_subject, reason=reason,
                )
            except TaskPlanCommitInProgress as exc:
                return {
                    **self._plan_result(self.plans.get(plan_id, user_subject=user_subject)),
                    "status": "running",
                    "error": {"code": "PLAN_COMMIT_IN_PROGRESS", "message": str(exc)},
                    "nextAction": {"type": "wait_for_verification", "doNotRetry": True},
                }
            self._event(
                plan,
                "plan.canceled",
                {"planId": plan_id, "status": "canceled"},
            )
            try:
                self.service.tasks.cancel_task(
                    task_id=plan["parent_task_id"],
                    user_subject=user_subject,
                    reason="task_plan_canceled",
                    causation_ref=plan_id,
                )
            except Exception:
                pass
            self._retire_interactions(current)
            return self._plan_result(plan)

    def recover(self, *, limit: int = 100) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "candidates": 0,
            "advanced": 0,
            "restarted": 0,
            "waiting": 0,
            "completed": 0,
            "canceled": 0,
            "failed": 0,
            "deferred": 0,
            "issues": [],
        }
        for candidate in self.plans.recovery_candidates(limit=limit):
            summary["candidates"] += 1
            plan_id = candidate["plan_id"]
            try:
                with self._plan_lock(plan_id):
                    plan = self.plans.get(
                        plan_id, user_subject=candidate["user_subject"]
                    )
                    if plan.get("commit_operation_id"):
                        step = next(s for s in plan["steps"] if (
                            s["step_key"] == plan["current_step_key"]
                            or s.get("operation_id") == plan["commit_operation_id"]
                        ))
                        if step["state"] == "succeeded":
                            self._complete_plan(plan_id, user_subject=plan["user_subject"], reason="all_steps_succeeded")
                            summary["completed"] += 1
                            continue
                        operation = self.service.operations.get(plan["commit_operation_id"])
                        if operation["status"] in {"pending", "running"}:
                            if not self._belongs_to_previous_runtime({"updated_at": operation["updated_at"]}):
                                summary["deferred"] += 1
                                continue
                            operation = self.service.operations.mark_unknown(
                                operation["operation_id"], code="RESULT_UNKNOWN",
                                message="提交边界已消费，但旧运行时未留下权威结果；须人工对账。",
                            )
                        response = self._consume_step_response(plan, step, {
                            "operationId": operation["operation_id"],
                            "status": operation["status"],
                            "error": operation.get("error"),
                            "interaction": (operation.get("next_action") or {}).get("interaction"),
                        })
                        if response is None:
                            response = self.advance(plan_id, user_subject=plan["user_subject"])
                        summary["completed" if response.get("status") == "succeeded" else "failed"] += 1
                        continue
                    try:
                        self.validate_versions(plan)
                        validator = getattr(self.service, "validate_task_plan_authority", None)
                        if callable(validator):
                            validator(plan)
                    except PlanValidationError as exc:
                        self.reject(plan, exc)
                        summary["failed"] += 1
                        continue
                    if plan["state"] == "waiting_user":
                        step = next(
                            (
                                item for item in plan["steps"]
                                if item["state"] == "waiting_user"
                            ),
                            None,
                        )
                        if step is not None and step.get("interaction_id"):
                            _record, _resource, interaction = self.service._load_interaction(
                                user_subject=plan["user_subject"],
                                interaction_id=step["interaction_id"],
                            )
                            if interaction["state"] in {
                                "declined", "expired", "failed", "superseded"
                            }:
                                result = self.handle_terminal_interaction(
                                    user_subject=plan["user_subject"],
                                    interaction_id=step["interaction_id"],
                                    interaction_state=interaction["state"],
                                )
                                if result is not None:
                                    outcome = (
                                        "canceled"
                                        if result["status"] == "canceled"
                                        else "failed"
                                    )
                                    summary[outcome] += 1
                                    continue
                        # Reconciliation never consumes an approval or replays a write.
                        summary["waiting"] += 1
                        continue
                    if plan["state"] not in {"validated", "running"}:
                        summary["deferred"] += 1
                        continue
                    step = self._current_running_step(plan)
                    if step is not None and self._belongs_to_previous_runtime(step):
                        operation = self._find_step_operation(plan, step)
                        if operation is None or operation["status"] in {
                            "pending",
                            "running",
                        }:
                            if operation is not None:
                                self.service.operations.mark_failed(
                                    operation["operation_id"],
                                    code="PLAN_RECOVERY_REPLACED",
                                    message=(
                                        "The unfinished operation belonged to a previous "
                                        "AgentBridge runtime and was replaced safely."
                                    ),
                                )
                            self.plans.reset_running_step_for_recovery(
                                plan_id,
                                user_subject=plan["user_subject"],
                                step_key=step["step_key"],
                                expected_attempt_count=step["attempt_count"],
                            )
                            self._event(
                                plan,
                                "plan.step.recovered",
                                {
                                    **self._step_event_payload(plan, step),
                                    "previousAttempt": step["attempt_count"],
                                },
                                causation_ref=(
                                    operation.get("operation_id")
                                    if operation is not None
                                    else plan_id
                                ),
                            )
                            summary["restarted"] += 1
                    response = self.advance(
                        plan_id, user_subject=plan["user_subject"]
                    )
                    summary["advanced"] += 1
                    status = response.get("status")
                    if status == "requires_user_action":
                        summary["waiting"] += 1
                    elif status == "succeeded":
                        summary["completed"] += 1
                    elif status in {"failed", "outcome_unknown"}:
                        summary["failed"] += 1
                    else:
                        summary["deferred"] += 1
            except Exception as exc:
                summary["failed"] += 1
                summary["issues"].append(
                    {
                        "planId": plan_id,
                        "errorCode": exc.__class__.__name__,
                    }
                )
        return summary

    def _invoke_capability(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        capability_name = step["capability_name"]
        spec = self.service.registry.get(capability_name)
        validate_runtime_arguments(
            arguments,
            schema=spec.input_schema,
            step_key=step["step_key"],
        )
        return self.service.invoke(
            user_subject=plan["user_subject"],
            capability_name=capability_name,
            arguments=arguments,
            idempotency_key=step_idempotency_key(plan, step),
            task_id=plan["parent_task_id"],
            host_type="task_plan",
            host_run_id=plan["plan_id"],
        )

    def _invoke_transform(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        transform_name = step["transform_name"]
        transform = self.transforms.get(transform_name)
        validate_runtime_arguments(
            arguments,
            schema=transform.input_schema,
            step_key=step["step_key"],
        )
        operation, reused = self.service.operations.create(
            user_subject=plan["user_subject"],
            capability_name=f"transform.{transform_name}",
            capability_version=transform.version,
            input_summary={
                "transform": transform_name,
                "itemCount": (
                    transform.input_item_counter(arguments)
                    if transform.input_item_counter is not None
                    else len(arguments.get("items") or [])
                ),
            },
            input_identity=arguments,
            idempotency_key=step_idempotency_key(plan, step),
        )
        if not reused:
            self.service.operations.mark_running(operation["operation_id"])
            try:
                result = self.transforms.invoke(transform_name, arguments)
            except (TransformRejected, CapabilityRejected) as exc:
                operation = self.service.operations.mark_failed(
                    operation["operation_id"],
                    code=exc.code,
                    message=exc.message,
                )
            except Exception as exc:
                operation = self.service.operations.mark_failed(
                    operation["operation_id"],
                    code="TRANSFORM_EXECUTION_FAILED",
                    message=str(exc) or exc.__class__.__name__,
                )
            else:
                operation = self.service.operations.mark_succeeded(
                    operation["operation_id"], result
                )
        else:
            operation = self.service.operations.get(operation["operation_id"])
        return {
            "protocolVersion": "0.1",
            "operationId": operation["operation_id"],
            "status": operation["status"],
            "result": operation.get("result"),
            "error": operation.get("error"),
            "interaction": None,
            "nextAction": operation.get("next_action"),
            "reused": reused,
        }

    def _consume_step_response(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        operation_id = str(response.get("operationId") or "").strip()
        if not operation_id:
            return self._fail_plan(
                plan,
                step,
                operation_id=None,
                state="failed",
                error_code="PLAN_OPERATION_MISSING",
                error_message="步骤没有返回权威 Operation。",
            )
        operation = self.service.operations.get(operation_id)
        self.service.tasks.link_plan_operation(
            task_id=plan["parent_task_id"],
            user_subject=plan["user_subject"],
            operation=operation,
            plan_id=plan["plan_id"],
            step_key=step["step_key"],
        )
        status = str(response.get("status") or operation["status"])
        interaction = response.get("interaction")
        if status in {"pending", "running"}:
            return {
                "protocolVersion": "0.1",
                "status": "running",
                "operationId": operation_id,
                "interaction": None,
                "plan": task_plan_response(
                    self.plans.get(
                        plan["plan_id"], user_subject=plan["user_subject"]
                    )
                ),
            }
        if status == "requires_user_action":
            if not isinstance(interaction, dict) or not interaction.get(
                "interactionId"
            ):
                return self._fail_plan(
                    plan,
                    step,
                    operation_id=operation_id,
                    state="failed",
                    error_code="PLAN_INTERACTION_MISSING",
                    error_message="等待用户时没有返回可信交互引用。",
                )
            interaction_id = str(interaction["interactionId"])
            updated = self.plans.mark_step_waiting(
                plan["plan_id"],
                user_subject=plan["user_subject"],
                step_key=step["step_key"],
                operation_id=operation_id,
                interaction_id=interaction_id,
                input_hash=operation.get("input_hash"),
            )
            record, _resource, envelope = self.service._load_interaction(
                user_subject=plan["user_subject"],
                interaction_id=interaction_id,
            )
            self.service.tasks.link_interaction(
                task_id=plan["parent_task_id"],
                user_subject=plan["user_subject"],
                interaction_record=record,
                interaction=envelope,
            )
            event_type = (
                "plan.authorization.waiting"
                if envelope.get("type") == "execution_authorization"
                else "plan.step.waiting"
            )
            self._event(
                updated,
                event_type,
                {
                    **self._step_event_payload(updated, step),
                    "interactionId": interaction_id,
                    "interactionType": envelope.get("type"),
                },
                causation_ref=interaction_id,
            )
            return {
                "protocolVersion": "0.1",
                "status": "requires_user_action",
                "operationId": operation_id,
                "interaction": interaction,
                "nextAction": response.get("nextAction"),
                "plan": task_plan_response(updated),
            }
        if status == "succeeded":
            result = operation.get("result")
            if step["kind"] == "transform" and isinstance(result, dict):
                self._store_result_projection(
                    plan,
                    step,
                    operation_id=operation_id,
                    result=result,
                )
                transform = self.transforms.get(step["transform_name"])
                if transform.halts_on_incomplete and (
                    result.get("source_incomplete") is True
                    or (result.get("coverage") or {}).get("status") != "complete"
                ) and self._has_write_sink(plan):
                    return self._fail_plan(
                        plan,
                        step,
                        operation_id=operation_id,
                        state="failed",
                        error_code="PLAN_SOURCE_INCOMPLETE",
                        error_message=(
                            "业务来源未完整覆盖请求范围，计划已停止且不会进入写入。"
                        ),
                    )
            updated = self.plans.mark_step_succeeded(
                plan["plan_id"],
                user_subject=plan["user_subject"],
                step_key=step["step_key"],
                operation_id=operation_id,
                input_hash=operation.get("input_hash"),
                output_hash=json_hash(result),
            )
            self._event(
                updated,
                "plan.step.succeeded",
                self._step_event_payload(updated, step),
                causation_ref=operation_id,
            )
            if step["kind"] == "transform":
                transform = self.transforms.get(step["transform_name"])
                if (
                    transform.halts_on_empty
                    and isinstance(result, dict)
                    and result.get("empty") is True
                ):
                    return self._complete_plan(
                        plan["plan_id"],
                        user_subject=plan["user_subject"],
                        reason="no_eligible_source_items",
                        skip_queued=True,
                    )
            return None
        error = response.get("error") or operation.get("error") or {}
        state = "outcome_unknown" if status in {"unknown", "outcome_unknown"} else "failed"
        return self._fail_plan(
            plan,
            step,
            operation_id=operation_id,
            state=state,
            error_code=str(error.get("code") or "PLAN_STEP_FAILED"),
            error_message=str(error.get("message") or "计划步骤执行失败。"),
        )

    def _resolve_step_arguments(
        self,
        *,
        plan: dict[str, Any],
        step: dict[str, Any],
    ) -> dict[str, Any]:
        arguments = dict(step.get("arguments") or {})
        for target_name, binding in (step.get("bindings") or {}).items():
            references = (
                binding.get("items") or []
                if binding.get("mode") == "many"
                else [binding]
            )
            values = []
            for reference in references:
                operation_id = self.plans.step_output_operation(
                    plan["plan_id"],
                    user_subject=plan["user_subject"],
                    step_key=reference["step"],
                )
                operation = self.service.operations.get(operation_id)
                if operation["user_subject"] != plan["user_subject"]:
                    raise TaskPlanIntegrityError("bound operation belongs to another user")
                values.append(
                    resolve_json_pointer(operation.get("result"), reference["pointer"])
                )
            arguments[target_name] = values if binding.get("mode") == "many" else values[0]
        return arguments

    def _complete_plan(
        self,
        plan_id: str,
        *,
        user_subject: str,
        reason: str,
        skip_queued: bool = False,
    ) -> dict[str, Any]:
        current = self.plans.get(plan_id, user_subject=user_subject)
        if reason == "no_eligible_source_items":
            effect_outcome = "no_effect"
        elif any(
            step["effect"] != "read" and step["state"] == "succeeded"
            for step in current["steps"]
        ):
            effect_outcome = "write_verified"
        else:
            effect_outcome = "preview_ready"
        plan = self.plans.complete(
            plan_id,
            user_subject=user_subject,
            reason=reason,
            skip_queued=skip_queued,
            effect_outcome=effect_outcome,
        )
        self._event(
            plan,
            "plan.completed",
            {
                "planId": plan_id,
                "status": "succeeded",
                "reason": reason,
            },
            causation_ref=plan_id,
        )
        try:
            self.service.tasks.complete_task(
                task_id=plan["parent_task_id"],
                user_subject=user_subject,
                reason="task_plan_completed",
                causation_ref=plan_id,
            )
        except Exception:
            pass
        return self._plan_result(plan)

    def _fail_plan(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        *,
        operation_id: str | None,
        state: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        current = self.plans.get(plan["plan_id"], user_subject=plan["user_subject"])
        if current["state"] not in ACTIVE_PLAN_STATES:
            return self._plan_result(current)
        updated = self.plans.mark_step_failed(
            plan["plan_id"],
            user_subject=plan["user_subject"],
            step_key=step["step_key"],
            operation_id=operation_id,
            state=state,
            error_code=error_code,
            error_message=error_message,
            effect_outcome=(
                "source_incomplete"
                if error_code == "PLAN_SOURCE_INCOMPLETE"
                else "outcome_unknown"
                if state == "outcome_unknown"
                else None
            ),
        )
        self._event(
            updated,
            "plan.outcome_unknown" if state == "outcome_unknown" else "plan.step.failed",
            {
                **self._step_event_payload(updated, step),
                "errorCode": error_code,
            },
            causation_ref=operation_id or plan["plan_id"],
        )
        if state == "outcome_unknown":
            try:
                self.service.tasks.mark_task_outcome_unknown(
                    task_id=plan["parent_task_id"],
                    user_subject=plan["user_subject"],
                    error_code=error_code,
                    causation_ref=operation_id,
                )
            except Exception:
                pass
        else:
            try:
                self.service.tasks.fail_task(
                    task_id=plan["parent_task_id"],
                    user_subject=plan["user_subject"],
                    error_code=error_code,
                    message=error_message[:500],
                    causation_ref=operation_id,
                )
            except Exception:
                pass
        self._retire_interactions(current)
        response = {
            "protocolVersion": "0.1",
            "status": state,
            "operationId": operation_id,
            "error": {"code": error_code, "message": error_message[:500]},
            "plan": task_plan_response(updated),
        }
        if error_code == "PLAN_SOURCE_INCOMPLETE":
            response["nextAction"] = {
                "type": "report_plan_failure",
                "doNotRetryAtomicTools": True,
                "businessWriteOccurred": False,
                "source": "plan.resultProjection.result.source_summaries",
            }
        return response

    def validate_versions(self, plan: dict) -> None:
        for step in plan["steps"]:
            try:
                target = (
                    self.service.registry.get(step["capability_name"])
                    if step["kind"] == "capability"
                    else self.transforms.get(step["transform_name"])
                )
            except KeyError as exc:
                raise PlanValidationError("PLAN_VERSION_MISMATCH", "计划绑定的能力或转换已不可用，须重新规划。") from exc
            if target.version != step["target_version"]:
                raise PlanValidationError(
                    "PLAN_VERSION_MISMATCH",
                    f"步骤 {step['step_key']} 的冻结版本 {step['target_version']} 与当前版本 {target.version} 不匹配，须重新规划。",
                )

    def reject(self, plan: dict, error: PlanValidationError) -> dict:
        with self._plan_lock(plan["plan_id"]):
            current = self.plans.get(plan["plan_id"], user_subject=plan["user_subject"])
            if current["state"] not in ACTIVE_PLAN_STATES:
                return self._plan_result(current)
            step = next(s for s in current["steps"] if s["state"] in {"queued", "running", "waiting_user"})
            return self._fail_plan(current, step, operation_id=step.get("operation_id"),
                state="failed", error_code=error.code, error_message=error.message)

    def _retire_interactions(self, plan: dict, *, extra: dict | None = None) -> None:
        retire = getattr(self.service, "retire_plan_interactions", None)
        if callable(retire):
            retire(plan, extra=extra)

    def _has_write_sink(self, plan: dict[str, Any]) -> bool:
        for step in plan.get("steps") or []:
            capability_name = step.get("capability_name")
            if not capability_name:
                continue
            try:
                spec = self.service.registry.get(capability_name)
            except KeyError:
                continue
            if spec.effect != "read":
                return True
        return False

    def _store_result_projection(
        self,
        plan: dict[str, Any],
        step: dict[str, Any],
        *,
        operation_id: str,
        result: dict[str, Any],
    ) -> None:
        transform = self.transforms.get(step["transform_name"])
        if not transform.result_projection:
            return
        projection = build_plan_result_projection(
            projection_kind=transform.result_projection, step=step,
            operation_id=operation_id, result=result,
        )
        updated = self.plans.set_result_projection(
            plan["plan_id"],
            user_subject=plan["user_subject"],
            projection=projection,
        )
        if transform.result_projection == "private_draft":
            self._event(
                updated,
                "plan.result.ready",
                {
                    "planId": plan["plan_id"],
                    "stepKey": step["step_key"],
                    "kind": transform.result_projection,
                    "resultHash": projection["resultHash"],
                    "includedCount": int(result.get("included_count") or 0),
                    "excludedCount": int(result.get("excluded_count") or 0),
                },
                causation_ref=operation_id,
            )

    def _waiting_result(
        self, plan: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        interaction = None
        if step.get("interaction_id"):
            _record, _resource, interaction = self.service._load_interaction(
                user_subject=plan["user_subject"],
                interaction_id=step["interaction_id"],
            )
        return {
            "protocolVersion": "0.1",
            "status": "requires_user_action",
            "operationId": step.get("operation_id"),
            "interaction": interaction,
            "plan": task_plan_response(plan),
        }

    @staticmethod
    def _plan_result(plan: dict[str, Any]) -> dict[str, Any]:
        return plan_result_response(plan)

    def _event(
        self,
        plan: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        causation_ref: str | None = None,
    ) -> None:
        self.service.tasks.record_plan_event(
            task_id=plan["parent_task_id"],
            user_subject=plan["user_subject"],
            event_type=event_type,
            payload=payload,
            causation_ref=causation_ref,
        )

    @staticmethod
    def _step_event_payload(
        plan: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "planId": plan["plan_id"],
            "stepKey": step["step_key"],
            "ordinal": step["ordinal"],
            "systemId": step["system_id"],
            "kind": step["kind"],
        }

    def _plan_lock(self, plan_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(plan_id, threading.RLock())

    @staticmethod
    def _current_running_step(plan: dict[str, Any]) -> dict[str, Any] | None:
        current = plan.get("current_step_key")
        return next(
            (
                step
                for step in plan.get("steps") or []
                if step.get("step_key") == current and step.get("state") == "running"
            ),
            None,
        )

    def _belongs_to_previous_runtime(self, step: dict[str, Any]) -> bool:
        try:
            updated_at = datetime.fromisoformat(str(step["updated_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return updated_at < self._started_at

    def _find_step_operation(
        self, plan: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any] | None:
        if step["kind"] == "capability":
            capability_name = step["capability_name"]
        else:
            capability_name = f"transform.{step['transform_name']}"
        return self.service.operations.find_idempotent(
            user_subject=plan["user_subject"],
            capability_name=capability_name,
            capability_version=step["target_version"],
            idempotency_key=step_idempotency_key(plan, step),
        )
