"""Trusted field preparation and single-use authorized write execution."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Callable, Protocol

import hashlib

import json

from bscli.adapters.seeyon_pending_batch import PENDING_BATCH_PREPARE_CAPABILITY

from bscli.adapters.base import AdapterBusinessRuleRejected

from bscli.adapters.seeyon_submit_phases import SeeyonBusinessValidationRequired

from bscli.admin.stores import GovernancePolicyDenied, GovernancePolicyStore
from bscli.core.capability import CapabilityRegistry

from bscli.core.capability_runtime import (
    CapabilityRejected,
    CapabilityContext,
    OutcomeUnknown,
    RequiresUserAction,
)

from bscli.core.write_catalog import resolve_write_function

from bscli.core.field_submissions import (
    FieldSubmissionAccessDenied,
    FieldSubmissionIntegrityError,
    FieldSubmissionNotFound,
    FieldSubmissionStateError,
    FieldSubmissionStore,
)

from bscli.core.task_plan_validation import PlanValidationError
from bscli.core.task_plans import TaskPlanStore
from bscli.core.tasks import TaskHubStore
from bscli.core.user_grants import UserGrants

from bscli.core.write_authorizations import (
    WriteAuthorizationAccessDenied,
    WriteAuthorizationNotFound,
    WriteAuthorizationStateError,
    WriteAuthorizationStore,
)

TRUSTED_WRITE_INTERACTION_TTL_SECONDS = 1800


class PendingBatchDefinition(Protocol):
    def __call__(
        self, *, task_id: str, user_subject: str, arguments: dict,
    ) -> dict: ...


@dataclass(frozen=True)
class ControlledWriteDependencies:
    """Existing stores and narrow callbacks; construction performs no I/O.

    The skill guard must use the connection supplied by authorization consumption.
    Neither it nor plan validation may move that guard outside the transaction.
    """

    registry: CapabilityRegistry
    tasks: TaskHubStore
    field_submissions: FieldSubmissionStore
    write_authorizations: WriteAuthorizationStore
    user_grants: UserGrants
    task_plans: TaskPlanStore
    governance_policies: GovernancePolicyStore
    trusted_card_base_url: str
    business_input_interaction: Callable[[dict], dict]
    execution_authorization_interaction: Callable[[dict], dict]
    pending_batch_definition: PendingBatchDefinition
    guard_skill_authorization: Callable[[Connection, str, str], None]
    validate_task_plan_execution: Callable[[dict], None]


class ControlledWriteExecutor:
    """Execute trusted writes using explicitly supplied collaborators."""

    def __init__(self, dependencies: ControlledWriteDependencies) -> None:
        self._dependencies = dependencies

    def prepare(
        self,
        *,
        context: CapabilityContext,
        session: dict,
        adapter: object,
        worker: object,
        arguments: dict,
        field_submission: dict | None,
        definition: dict,
    ) -> dict:
        prepare_function = resolve_write_function(str(definition["prepare_function"]))
        if not callable(prepare_function):
            raise RuntimeError("trusted write prepare function is unavailable")
        self.assert_write_allowed(context=context, system_id=session["system_id"])
        prepared = prepare_function(adapter, worker, arguments)
        review_changed = False
        if field_submission is not None:
            expected_review_fingerprint = str(
                field_submission.get("form_schema", {}).get(
                    "_agentbridge_review_fingerprint"
                )
                or ""
            ).strip()
            actual_review_fingerprint = str(
                prepared.get("plan", {}).get("target", {}).get(
                    "review_fingerprint"
                )
                or ""
            ).strip()
            review_changed = bool(
                expected_review_fingerprint
                and actual_review_fingerprint
                and expected_review_fingerprint != actual_review_fingerprint
            )
        if field_submission is None:
            resume_arguments = {
                name: arguments[name]
                for name in definition.get("context_fields") or ()
                if name in arguments
            }
            if len(resume_arguments) != len(definition.get("context_fields") or ()):
                raise ValueError("trusted write is missing its target context")
        else:
            resume_arguments = dict(
                field_submission.get("form_schema", {}).get(
                    "_agentbridge_resume_arguments"
                )
                or {}
            )
        plan = {
            **prepared["plan"],
            "user_subject": session["user_subject"],
            "prepare_capability": context.spec.name,
            "resume_arguments": resume_arguments,
            "session_binding": {
                "session_id": session["session_id"],
                "expected_principal_ref": session.get("expected_principal_ref"),
                "downstream_principal_ref": session.get("downstream_principal_ref"),
                "last_verified_at": session.get("last_verified_at"),
            },
        }
        summary = {
            **prepared["summary"],
            "system": (definition.get("field_schema") or {}).get("system")
            or prepared["summary"].get("system")
            or session["system_id"],
            "principal": session.get("downstream_principal_ref")
            or session.get("expected_principal_ref")
            or session["user_subject"],
        }
        if review_changed:
            summary["fields"] = [
                {
                    "label": "详情变化",
                    "value": "填写意见后单据内容发生变化，请按当前详情重新核对本次授权。",
                },
                *list(summary.get("fields") or []),
            ]
            summary["authorization_notice"] = (
                "单据内容在填写意见后发生变化；本授权卡展示并绑定最新详情。"
                "请重新核对后再决定是否授权。"
            )
        if context.spec.name == PENDING_BATCH_PREPARE_CAPABILITY:
            batch = self._dependencies.tasks.get_batch_for_task(
                parent_task_id=context.task_id, user_subject=session["user_subject"],
            )
            summary["title"] = f"{summary['title']}（第 {batch['current_ordinal']}/{batch['total_count']} 条）"
        commit_spec = self._dependencies.registry.get(str(definition["commit_capability"]))
        authorization = self._dependencies.write_authorizations.create(
            user_subject=session["user_subject"],
            system_id=session["system_id"],
            session_id=session["session_id"],
            capability_name=commit_spec.name,
            capability_version=commit_spec.version,
            prepare_operation_id=context.operation_id,
            supersession_key=_trusted_write_supersession_key(resume_arguments),
            plan=plan,
            summary=summary,
            card_base_url=self._dependencies.trusted_card_base_url,
            ttl_seconds=TRUSTED_WRITE_INTERACTION_TTL_SECONDS,
        )
        interaction = self._dependencies.execution_authorization_interaction(authorization)
        if field_submission is not None:
            try:
                self._dependencies.field_submissions.consume(
                    field_submission["submission_id"],
                    user_subject=session["user_subject"],
                    system_id=session["system_id"],
                    session_id=session["session_id"],
                    capability_name=context.spec.name,
                    capability_version=context.spec.version,
                    consume_operation_id=context.operation_id,
                )
            except (
                FieldSubmissionAccessDenied,
                FieldSubmissionIntegrityError,
                FieldSubmissionStateError,
            ) as exc:
                raise ValueError(str(exc)) from exc
        raise RequiresUserAction(
            "WRITE_AUTHORIZATION_REQUIRED",
            str(definition["authorization_message"]),
            next_action={
                "type": "open_write_authorization_card",
                "interactionId": interaction["interactionId"],
                "authorizationId": authorization["authorization_id"],
                "cardUrl": authorization["card_url"],
                "planHash": authorization["plan_hash"],
                "expiresAt": authorization["expires_at"],
                "display": {
                    "title": summary.get("title"),
                    "effect": summary.get("effect"),
                    "fieldCount": len(summary.get("fields") or []),
                },
                "then": {
                    "capability": commit_spec.name,
                    "arguments": {"authorization_id": authorization["authorization_id"]},
                },
                "interaction": interaction,
            },
        )

    def resolve_field_input(
        self,
        *,
        context: CapabilityContext,
        session: dict,
        arguments: dict,
        definition: dict,
        form_schema: dict | None = None,
    ) -> tuple[dict, dict]:
        submission_id = str(arguments.get("input_submission_id") or "").strip()
        context_arguments = {
            name: arguments[name]
            for name in definition.get("context_fields") or ()
            if name in arguments
        }
        if len(context_arguments) != len(definition.get("context_fields") or ()):
            raise ValueError("trusted field input is missing its workflow target context")
        if not submission_id:
            selected_schema = (
                form_schema if form_schema is not None else definition["field_schema"]
            )
            submission_schema = {
                **_prefill_trusted_field_schema(selected_schema, arguments),
                "_agentbridge_resume_arguments": context_arguments,
            }
            submission = self._dependencies.field_submissions.create(
                user_subject=session["user_subject"],
                system_id=session["system_id"],
                session_id=session["session_id"],
                capability_name=context.spec.name,
                capability_version=context.spec.version,
                create_operation_id=context.operation_id,
                supersession_key=_trusted_write_supersession_key(context_arguments),
                form_schema=submission_schema,
                card_base_url=self._dependencies.trusted_card_base_url,
                ttl_seconds=TRUSTED_WRITE_INTERACTION_TTL_SECONDS,
            )
            raise self._field_input_required(submission, definition)
        try:
            submission = self._dependencies.field_submissions.get(submission_id, include_values=True)
        except (FieldSubmissionNotFound, FieldSubmissionIntegrityError) as exc:
            raise self._field_input_unavailable(
                "not_found",
                context.spec.name,
                context_arguments,
            ) from exc
        bindings_match = all(
            (
                submission["user_subject"] == session["user_subject"],
                submission["system_id"] == session["system_id"],
                submission["session_id"] == session["session_id"],
                submission["capability_name"] == context.spec.name,
                submission["capability_version"] == context.spec.version,
                submission.get("form_schema", {}).get("_agentbridge_resume_arguments")
                == context_arguments,
            )
        )
        if not bindings_match:
            raise self._field_input_unavailable(
                "binding_mismatch",
                context.spec.name,
                context_arguments,
            )
        if submission["state"] == "pending":
            raise self._field_input_required(submission, definition)
        if submission["state"] != "submitted" or not isinstance(submission.get("values"), dict):
            raise self._field_input_unavailable(
                submission["state"],
                context.spec.name,
                context_arguments,
            )
        return submission, {**context_arguments, **submission["values"]}

    def _field_input_required(self, submission: dict, definition: dict) -> RequiresUserAction:
        interaction = self._dependencies.business_input_interaction(submission)
        resume_arguments = {
            **dict(
                submission.get("form_schema", {}).get("_agentbridge_resume_arguments")
                or {}
            ),
            "input_submission_id": submission["submission_id"],
        }
        return RequiresUserAction(
            "FIELD_INPUT_REQUIRED",
            str(definition["field_message"]),
            next_action={
                "type": "open_field_input_card",
                "interactionId": interaction["interactionId"],
                "inputSubmissionId": submission["submission_id"],
                "cardUrl": submission["card_url"],
                "expiresAt": submission["expires_at"],
                "then": {
                    "capability": submission["capability_name"],
                    "arguments": resume_arguments,
                },
                "interaction": interaction,
            },
        )

    @staticmethod
    def _field_input_unavailable(
        state: str,
        prepare_capability: str,
        resume_arguments: dict,
    ) -> RequiresUserAction:
        return RequiresUserAction(
            "FIELD_INPUT_UNAVAILABLE",
            f"The trusted field submission is unavailable: {state}.",
            next_action={
                "type": "prepare_again",
                "capability": prepare_capability,
                "arguments": dict(resume_arguments),
            },
        )

    def commit(
        self,
        *,
        context: CapabilityContext,
        session: dict,
        adapter: object,
        worker: object,
        arguments: dict,
        prepare_capability: str,
        definition: dict,
    ) -> dict:
        authorization_id = str(arguments.get("authorization_id") or "").strip()
        if not authorization_id:
            raise ValueError("authorization_id is required")
        try:
            authorization = self._dependencies.write_authorizations.get(
                authorization_id,
                include_plan=True,
            )
        except WriteAuthorizationNotFound as exc:
            raise KeyError("write authorization not found") from exc
        if authorization["user_subject"] != session["user_subject"]:
            raise KeyError("write authorization not found")
        if authorization["state"] == "pending":
            interaction = self._dependencies.execution_authorization_interaction(authorization)
            raise RequiresUserAction(
                "WRITE_AUTHORIZATION_REQUIRED",
                "The trusted action card has not been approved.",
                next_action={
                    "type": "open_write_authorization_card",
                    "interactionId": interaction["interactionId"],
                    "authorizationId": authorization_id,
                    "cardUrl": authorization["card_url"],
                    "planHash": authorization["plan_hash"],
                    "expiresAt": authorization["expires_at"],
                    "interaction": interaction,
                },
            )
        plan = authorization["plan"]
        trusted_prepare_capability = str(
            plan.get("prepare_capability") or prepare_capability
        )
        if trusted_prepare_capability == PENDING_BATCH_PREPARE_CAPABILITY:
            batch_task_id = self._dependencies.tasks.task_id_for_operation(
                authorization["prepare_operation_id"], user_subject=session["user_subject"],
            )
            checked = self._dependencies.pending_batch_definition(
                task_id=batch_task_id, user_subject=session["user_subject"],
                arguments=plan.get("resume_arguments") or {},
            )
            if checked["commit_capability"] != context.spec.name:
                raise CapabilityRejected("BATCH_CAPABILITY_MISMATCH", "授权能力与冻结批次条目不一致。")
        if authorization["state"] != "approved":
            raise RequiresUserAction(
                "WRITE_AUTHORIZATION_UNAVAILABLE",
                f"The write authorization is {authorization['state']}.",
                next_action={
                    "type": "prepare_again",
                    "capability": trusted_prepare_capability,
                    "arguments": dict(plan.get("resume_arguments") or {}),
                },
            )
        if not self._trusted_write_session_binding_matches(plan, session):
            raise ValueError(
                "the downstream session changed after the write plan was authorized"
            )

        self.assert_write_allowed(context=context, system_id=session["system_id"])

        boundary_entered = False

        def enter_commit_boundary() -> None:
            nonlocal boundary_entered
            self._dependencies.user_grants.require_capability(
                context.user_subject, context.spec.name, arguments
            )
            self._dependencies.write_authorizations.consume(
                authorization_id,
                user_subject=session["user_subject"],
                system_id=session["system_id"],
                session_id=session["session_id"],
                capability_name=context.spec.name,
                capability_version=context.spec.version,
                commit_operation_id=context.operation_id,
                before_consume=lambda connection: (self._dependencies.guard_skill_authorization(
                    connection, authorization_id, session["user_subject"]
                ), self._dependencies.task_plans.guard_authorization_consumption(
                    connection, authorization_id=authorization_id,
                    user_subject=session["user_subject"], operation_id=context.operation_id,
                    validate=self._dependencies.validate_task_plan_execution,
                )),
            )
            boundary_entered = True

        commit_function = resolve_write_function(str(definition["commit_function"]))
        if not callable(commit_function):
            raise RuntimeError("trusted write commit function is unavailable")
        try:
            return commit_function(
                adapter,
                worker,
                plan,
                enter_commit_boundary=enter_commit_boundary,
            )
        except SeeyonBusinessValidationRequired as exc:
            validation = exc.validation
            continued_plan = deepcopy(plan)
            existing_validations = continued_plan.get("business_validation_overrides")
            if not isinstance(existing_validations, list):
                legacy_validation = continued_plan.get("business_validation_override")
                existing_validations = (
                    [dict(legacy_validation)]
                    if isinstance(legacy_validation, dict)
                    else []
                )
            existing_validations = [
                dict(item) for item in existing_validations if isinstance(item, dict)
            ]
            if validation["fingerprint"] in {
                item.get("fingerprint") for item in existing_validations
            }:
                raise ValueError("the OA confirmation was already authorized") from exc
            if len(existing_validations) >= 5:
                raise ValueError("too many chained OA confirmations") from exc
            continued_plan.pop("business_validation_override", None)
            continued_plan["business_validation_overrides"] = [
                *existing_validations,
                dict(validation),
            ]
            continued_summary = deepcopy(authorization["summary"])
            original_title = str(
                continued_summary.get("title") or "OA 写操作"
            ).strip()
            continued_summary.update(
                {
                    "title": f"确认 OA 提示并继续{original_title}",
                    "effect": "仅在再次出现完全相同的 OA 提示时继续执行已授权操作",
                    "authorization_notice": (
                        "OA 返回了一条可继续的提交提示。授权后，AgentBridge "
                        "仅在再次出现完全相同的提示时点击“继续”并完成正式提交。"
                    ),
                    "authorize_label": "确认警告并继续提交",
                }
            )
            continued_summary["fields"] = [
                *list(continued_summary.get("fields") or []),
                {"label": "OA 提交提示", "value": validation["message"]},
            ]
            continued_authorization = self._dependencies.write_authorizations.create(
                user_subject=session["user_subject"],
                system_id=session["system_id"],
                session_id=session["session_id"],
                capability_name=context.spec.name,
                capability_version=context.spec.version,
                prepare_operation_id=context.operation_id,
                supersession_key=_trusted_write_supersession_key(
                    dict(continued_plan.get("resume_arguments") or {})
                ),
                plan=continued_plan,
                summary=continued_summary,
                card_base_url=self._dependencies.trusted_card_base_url,
                ttl_seconds=TRUSTED_WRITE_INTERACTION_TTL_SECONDS,
            )
            interaction = self._dependencies.execution_authorization_interaction(
                continued_authorization
            )
            raise RequiresUserAction(
                "OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED",
                "OA returned a continuable business-validation warning.",
                next_action={
                    "type": "open_write_authorization_card",
                    "interactionId": interaction["interactionId"],
                    "authorizationId": continued_authorization["authorization_id"],
                    "cardUrl": continued_authorization["card_url"],
                    "planHash": continued_authorization["plan_hash"],
                    "expiresAt": continued_authorization["expires_at"],
                    "display": {
                        "title": continued_summary["title"],
                        "effect": continued_summary["effect"],
                        "fieldCount": len(continued_summary["fields"]),
                        "validationCode": validation["code"],
                    },
                    "then": {
                        "capability": context.spec.name,
                        "arguments": {
                            "authorization_id": continued_authorization[
                                "authorization_id"
                            ]
                        },
                    },
                    "interaction": interaction,
                },
            ) from exc
        except definition["outcome_error"] as exc:
            raise OutcomeUnknown("RESULT_UNKNOWN", str(exc)) from exc
        except AdapterBusinessRuleRejected as exc:
            raise CapabilityRejected(
                exc.error_code,
                str(exc),
            ) from exc
        except definition["contract_error"] as exc:
            if boundary_entered:
                raise OutcomeUnknown("RESULT_UNKNOWN", "写入已进入提交边界，但结果契约不匹配；请先对账。") from exc
            raise ValueError(str(exc)) from exc
        except (WriteAuthorizationAccessDenied, WriteAuthorizationStateError) as exc:
            raise ValueError(str(exc)) from exc
        except PlanValidationError as exc:
            raise CapabilityRejected(exc.code, exc.message) from exc
        except Exception as exc:
            if boundary_entered:
                raise OutcomeUnknown(
                    "RESULT_UNKNOWN",
                    f"写入已进入提交边界，但未取得权威结果（{type(exc).__name__}）；请先对账，不能自动重试。",
                ) from exc
            raise

    def assert_write_allowed(self, *, context: CapabilityContext, system_id: str) -> None:
        if context.spec.effect == "read":
            return
        try:
            self._dependencies.governance_policies.assert_write_allowed(
                system_id=system_id,
                user_subject=context.user_subject,
                capability_name=context.spec.name,
                capability_version=context.spec.version,
            )
        except GovernancePolicyDenied as exc:
            policy = exc.policy
            raise CapabilityRejected(
                "WRITE_PAUSED",
                "Write operation is paused by governance policy "
                f"{policy['scope_type']}:{policy['scope_value']}. "
                f"Reason: {policy['reason']}",
            ) from exc
    @staticmethod
    def _trusted_write_session_binding_matches(plan: dict, session: dict) -> bool:
        binding = plan.get("session_binding") if isinstance(plan.get("session_binding"), dict) else {}
        return all(
            (
                plan.get("user_subject") == session["user_subject"],
                binding.get("session_id") == session["session_id"],
                binding.get("expected_principal_ref") == session.get("expected_principal_ref"),
                binding.get("downstream_principal_ref") == session.get("downstream_principal_ref"),
                binding.get("last_verified_at") == session.get("last_verified_at"),
            )
        )


def _prefill_trusted_field_schema(schema: dict, arguments: dict) -> dict:
    selected = deepcopy(schema)
    for field in selected.get("fields") or []:
        if not isinstance(field, dict) or "value" in field:
            continue
        name = str(field.get("name") or "")
        if name and name in arguments and arguments[name] is not None:
            field["value"] = arguments[name]
    return selected

def _trusted_write_supersession_key(arguments: dict) -> str:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
