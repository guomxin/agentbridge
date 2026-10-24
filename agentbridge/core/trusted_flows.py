"""Read-only continuation of one trusted write, never a second coordinator.

The source card session is the capability for this view. Successors are resolved
from immutable operation lineage, not the task's mutable current interaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets


FLOW_SCHEMA = "agentbridge.trusted-flow.v1"


class TrustedFlowDenied(PermissionError):
    pass


@dataclass(frozen=True)
class FlowContext:
    kind: str
    resource: dict
    interaction: dict
    task_id: str
    presentation_id: str | None
    endpoint_id: str | None
    resume_arguments: dict
    actionable: bool


class TrustedFlowReader:
    def __init__(self, service) -> None:
        self.service = service

    def context(self, kind: str, resource_id: str, presentation_id: str | None) -> FlowContext:
        store, interaction_type = self._store(kind)
        resource = store.get(resource_id, **({"include_plan": True} if kind == "authorize" else {}))
        subject = resource["user_subject"]
        interaction = self.service.interactions.find_by_resource(
            interaction_type=interaction_type, resource_id=resource_id, user_subject=subject,
        )
        if any(interaction[key] != resource[key] for key in ("user_subject", "system_id", "session_id")):
            raise TrustedFlowDenied("interaction binding differs from its card")
        task_id = self.service.tasks.task_id_for_interaction(
            interaction["interaction_id"], user_subject=subject,
        )
        if not task_id:
            raise TrustedFlowDenied("a continuous flow requires an owned task")
        self.service.tasks.get_task(task_id, user_subject=subject)
        operation_id = resource["create_operation_id" if kind == "input" else "prepare_operation_id"]
        if interaction.get("operation_id") != operation_id:
            raise TrustedFlowDenied("interaction operation differs from its card")
        operation = self.service.operations.get(operation_id)
        if operation["user_subject"] != subject or self.service.tasks.task_id_for_operation(
            operation_id, user_subject=subject,
        ) != task_id:
            raise TrustedFlowDenied("the card preparation belongs to another task")
        if kind == "input" and any(operation[key] != resource[key] for key in ("capability_name", "capability_version")):
            raise TrustedFlowDenied("field preparation capability differs")
        self.service.user_grants.require_capability(subject, operation["capability_name"], operation.get("input_summary"))
        self.service.validate_skill_task(subject, task_id)
        endpoint_id = None
        if presentation_id is not None:
            presentation = store.get_presentation(resource_id, presentation_id)
            if presentation["user_subject"] != subject:
                raise TrustedFlowDenied("presentation belongs to another user")
            endpoint_id = presentation["endpoint_id"]
        arguments = (
            resource["form_schema"].get("_agentbridge_resume_arguments")
            if kind == "input" else resource["plan"].get("resume_arguments")
        ) or {}
        if not isinstance(arguments, dict):
            raise TrustedFlowDenied("invalid workflow context")
        batch = self.service.tasks.get_batch_for_task(
            parent_task_id=task_id, user_subject=subject, include_resource_refs=True,
        )
        actionable = True
        if batch is not None:
            matches = [item for item in batch["items"] if item["resource_ref"] == arguments.get("affair_id")]
            if arguments.get("batch_id") != batch["batch_id"] or len(matches) != 1:
                raise TrustedFlowDenied("card does not belong to a frozen batch item")
            actionable = (batch["state"] in {"running", "waiting_user"}
                          and int(matches[0]["ordinal"]) == int(batch["current_ordinal"])
                          and matches[0]["state"] in {"queued", "preparing", "waiting_user"})
        elif arguments.get("batch_id"):
            raise TrustedFlowDenied("batch context has no owning batch")
        return FlowContext(kind, resource, interaction, task_id, presentation_id, endpoint_id, arguments, actionable)

    def authenticate(self, context: FlowContext, *, token: str, cookie: str) -> None:
        if not token or not cookie or not hmac.compare_digest(token, cookie):
            raise TrustedFlowDenied("flow session token is required")
        store, _ = self._store(context.kind)
        table, id_column = (
            ("field_submission_card_sessions", "submission_id") if context.kind == "input"
            else ("write_authorization_card_sessions", "authorization_id")
        )
        with store._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {id_column} = ? AND presentation_id IS ? AND csrf_hash = ?",
                (context.resource[id_column], context.presentation_id, hashlib.sha256(token.encode()).hexdigest()),
            ).fetchone()
        now = store.clock().astimezone(timezone.utc)
        if (row is None or row["state"] not in {"pending", "consumed"}
                or now >= datetime.fromisoformat(row["expires_at"])
                or now >= datetime.fromisoformat(context.resource["expires_at"])
                or row["endpoint_id"] != context.endpoint_id):
            raise TrustedFlowDenied("flow card session is unavailable")

    def issue_read_session(self, context: FlowContext) -> tuple[str, int]:
        """Reopen an already decided opaque card on another trusted endpoint.

        A consumed card session is read-only: existing submission/decision code
        only accepts pending sessions. No resource state or lifetime changes.
        """
        store, _ = self._store(context.kind)
        now = store.clock().astimezone(timezone.utc)
        expires = datetime.fromisoformat(context.resource["expires_at"])
        if context.resource["state"] in {"pending", "expired"} or now >= expires:
            raise TrustedFlowDenied("cannot reopen this flow")
        if context.resource["state"] == "superseded" and self._warning_successor(context) is None:
            raise TrustedFlowDenied("cannot reopen this flow")
        token = secrets.token_urlsafe(32)
        table, id_column = (
            ("field_submission_card_sessions", "submission_id") if context.kind == "input"
            else ("write_authorization_card_sessions", "authorization_id")
        )
        with store._connect() as connection:
            connection.execute(
                f"INSERT INTO {table} (card_session_id, {id_column}, presentation_id, endpoint_id, "
                "csrf_hash, state, created_at, expires_at, consumed_at) VALUES (?, ?, ?, ?, ?, 'consumed', ?, ?, ?)",
                (secrets.token_urlsafe(24), context.resource[id_column], context.presentation_id,
                 context.endpoint_id, hashlib.sha256(token.encode()).hexdigest(), now.isoformat(),
                 context.resource["expires_at"], now.isoformat()),
            )
        return token, max(1, int((expires - now).total_seconds()))

    def status(self, context: FlowContext) -> dict:
        resource = context.resource
        resource_state = resource["state"]
        if resource_state == "pending" and not context.actionable:
            return self._view(context, "superseded")
        warning = self._warning_successor(context)
        if warning is not None:
            return warning
        if resource_state in {"expired", "superseded", "rejected"}:
            state = "declined" if resource_state == "rejected" else resource_state
            return self._view(context, state)
        if context.kind == "input":
            operation_id = resource.get("consume_operation_id")
            if operation_id:
                return self._successor(context, operation_id)
            # A rejected prepare does not consume the source fields. Match the
            # exact resume arguments, including this opaque submission ID.
            operation = self._resumed_operation(context)
            if operation and operation["status"] in {"failed", "unknown"}:
                return self._view(context, operation["status"], operation_id=operation["operation_id"])
            return self._view(context, "waiting_coordinator")
        operation_id = resource.get("commit_operation_id")
        if not operation_id:
            # Governance, session or contract checks may reject a commit before
            # consuming its authorization. Observe only this exact resume call.
            operation = self._resumed_operation(context) if resource_state == "approved" else None
            if operation and operation["status"] in {"failed", "unknown"}:
                return self._view(context, operation["status"], operation_id=operation["operation_id"])
            return self._view(context, "authorized" if resource_state == "approved" else "review_ready")
        operation = self.service.operations.get(operation_id)
        if any(operation[key] != resource[key] for key in ("user_subject", "capability_name", "capability_version")):
            raise TrustedFlowDenied("commit operation binding differs")
        operation_task = self.service.tasks.task_id_for_operation(operation_id, user_subject=resource["user_subject"])
        if operation_task is None:
            return self._view(context, "processing", operation_id=operation_id)
        if operation_task != context.task_id:
            raise TrustedFlowDenied("commit operation belongs to another task")
        state = operation["status"]
        if state == "requires_user_action":
            # Some registered workflows create an additional frozen warning
            # authorization. This follows its exact origin; it never replays a
            # pending action whose result is unknown.
            return self._successor(context, operation_id)
        projected = {"succeeded": "completed", "failed": "failed", "unknown": "unknown"}.get(state, "processing")
        view = self._view(context, projected, operation_id=operation_id)
        return self._batch_continuation(context, view) if projected == "completed" else view

    def _warning_successor(self, context: FlowContext) -> dict | None:
        # A recognized OA warning may supersede the original authorization
        # before its write boundary. Follow only its exact resumed operation.
        if (context.kind != "authorize" or context.resource["state"] != "superseded"
                or context.resource.get("commit_operation_id")):
            return None
        operation = self._resumed_operation(context)
        if (operation and operation["status"] == "requires_user_action"
                and operation.get("error_code") == "OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED"):
            return self._successor(context, operation["operation_id"])
        return None

    def _batch_continuation(self, context: FlowContext, view: dict) -> dict:
        """Offer a separately confirmed current item after this exact item succeeded.

        This is a read projection, not a coordinator: no task resumption or
        business operation runs here, and a stale receipt cannot supply a target.
        """
        batch_id = context.resume_arguments.get("batch_id")
        if not batch_id:
            return view
        subject = context.resource["user_subject"]
        task = self.service.tasks.get_task(context.task_id, user_subject=subject)
        batch = self.service.tasks.get_batch_for_task(parent_task_id=context.task_id,
            user_subject=subject, include_resource_refs=True)
        if not batch or batch["batch_id"] != batch_id:
            raise TrustedFlowDenied("batch continuation binding differs")
        source_items = [item for item in batch["items"] if item["resource_ref"] == context.resume_arguments.get("affair_id")]
        if len(source_items) != 1:
            raise TrustedFlowDenied("batch source is ambiguous")
        source = source_items[0]
        progress = {**(view.get("batch") or {}), "completedCount": batch["succeeded_count"], "totalCount": batch["total_count"]}
        view = {**view, "batch": progress}
        if (task["status"] not in {"active", "running", "waiting_user"}
                or batch["state"] not in {"running", "waiting_user"}):
            return view
        if source["state"] != "succeeded":
            # The coordinator has not yet recorded this confirmed result.
            return {**view, "terminal": False}
        if source["operation_id"] != view.get("operationId"):
            raise TrustedFlowDenied("completed batch operation differs")
        current = next((item for item in batch["items"] if item["ordinal"] == batch["current_ordinal"]), None)
        if not current or current["ordinal"] <= source["ordinal"]:
            return view
        if current["state"] in {"queued", "preparing"}:
            return {**view, "terminal": False}
        if (current["state"] != "waiting_user" or not current.get("interaction_id")
                or current["interaction_id"] != task.get("current_interaction_id")):
            return view
        interaction = self.service.interactions.get(current["interaction_id"], user_subject=subject)
        if interaction["interaction_type"] != "business_input":
            return view
        following = self.context("input", interaction["resource_id"], None)
        if (following.task_id != context.task_id or following.resource["state"] != "pending" or not following.actionable
                or following.resource["user_subject"] != subject
                or following.resource["system_id"] != context.resource["system_id"]
                or following.resume_arguments.get("batch_id") != batch_id
                or following.resume_arguments.get("affair_id") != current["resource_ref"]):
            raise TrustedFlowDenied("next batch item binding differs")
        path = f"/flow/input/{interaction['resource_id']}"
        if context.endpoint_id is not None:
            presentation = self.service.field_submissions.create_presentation(interaction["resource_id"],
                user_subject=subject, endpoint_id=context.endpoint_id)
            path += f"/present/{presentation['presentation_id']}"
        return {**view, "continueUrl": path, "batch": {**progress,
            "nextOrdinal": current["ordinal"],
            "nextTitle": str(current["display_summary"].get("title") or following.resource["form_schema"].get("title") or "下一项")}}

    def _successor(self, context: FlowContext, operation_id: str) -> dict:
        resource = context.resource
        with self.service.write_authorizations._connect() as connection:
            rows = connection.execute(
                "SELECT authorization_id FROM write_authorizations WHERE prepare_operation_id = ?",
                (operation_id,),
            ).fetchall()
        if not rows:
            return self._view(context, "waiting_coordinator")
        if len(rows) != 1:
            raise TrustedFlowDenied("ambiguous continuation")
        authorization_id = rows[0]["authorization_id"]
        authorization = self.service.write_authorizations.get(authorization_id, include_plan=True)
        expected_prepare = resource["capability_name"] if context.kind == "input" else resource["plan"].get("prepare_capability")
        if (any(authorization[key] != resource[key] for key in ("user_subject", "system_id", "session_id"))
                or authorization["plan"].get("resume_arguments", {}) != context.resume_arguments
                or authorization["plan"].get("prepare_capability") != expected_prepare):
            raise TrustedFlowDenied("continuation context differs")
        # The original coordinator links the completed prepare and its successor
        # after invocation. During that small gap, continue waiting, not guessing.
        try:
            record = self.service.interactions.find_by_resource(
                interaction_type="execution_authorization", resource_id=authorization_id,
                user_subject=resource["user_subject"],
            )
        except KeyError:
            return self._view(context, "waiting_coordinator")
        successor_task = self.service.tasks.task_id_for_interaction(record["interaction_id"], user_subject=resource["user_subject"])
        operation_task = self.service.tasks.task_id_for_operation(operation_id, user_subject=resource["user_subject"])
        if successor_task is None or operation_task is None:
            return self._view(context, "waiting_coordinator")
        if successor_task != context.task_id or operation_task != context.task_id:
            raise TrustedFlowDenied("continuation belongs to another task")
        path = f"/flow/authorize/{authorization_id}"
        if context.endpoint_id is not None:
            presentation = self.service.write_authorizations.create_presentation(
                authorization_id, user_subject=resource["user_subject"], endpoint_id=context.endpoint_id,
            )
            path += f"/present/{presentation['presentation_id']}"
        return {**self._view(context, "review_ready"), "nextUrl": path}

    def _resumed_operation(self, context: FlowContext) -> dict | None:
        arguments = context.interaction["resume_spec"].get("arguments") or {}
        identity = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.service.operations._connect() as connection:
            row = connection.execute(
                "SELECT o.operation_id FROM operations o JOIN task_operations t ON t.operation_id=o.operation_id "
                "WHERE t.task_id=? AND o.user_subject=? AND o.capability_name=? AND o.capability_version=? AND o.input_hash=? "
                "ORDER BY o.created_at DESC, o.rowid DESC LIMIT 1",
                (context.task_id, context.resource["user_subject"], context.resource["capability_name"],
                 context.resource["capability_version"], hashlib.sha256(identity.encode()).hexdigest()),
            ).fetchone()
        return self.service.operations.get(row["operation_id"]) if row else None

    def _store(self, kind: str):
        if kind == "input":
            return self.service.field_submissions, "business_input"
        if kind == "authorize":
            return self.service.write_authorizations, "execution_authorization"
        raise TrustedFlowDenied("unsupported flow source")

    def _view(self, context: FlowContext, state: str, *, operation_id: str | None = None) -> dict:
        labels = {
            "waiting_coordinator": ("字段已保存", "正在等待原任务继续准备，请保留此页。连接恢复后会自动显示授权内容。"),
            "review_ready": ("请核对本次操作", "已准备本次操作的授权内容。"),
            "authorized": ("已授权，等待执行", "授权已记录，原任务将继续执行；请勿重复提交。"),
            "processing": ("提交与核验中", "正在处理本次操作并核对业务结果，请勿重复提交。"),
            "completed": ("办理完成", "本次业务操作已完成，可在原任务详情查看结果。"),
            "failed": ("本次办理未完成", "请在原任务详情查看原因，再决定如何处理。此页不会自动重试。"),
            "unknown": ("结果待核对", "本次操作尚未取得确定结果。请通过原任务详情和只读业务查询核对，不要重复提交。"),
            "declined": ("本次操作已取消", "这份操作授权已被拒绝。"),
            "expired": ("办理页面已过期", "请从原任务重新打开当前有效入口。"),
            "superseded": ("该版本已失效", "请从原任务打开当前版本，旧页面不会接续其他操作。"),
        }
        title, message = labels[state]
        result = {
            "schemaVersion": FLOW_SCHEMA, "state": state, "title": title, "message": message,
            "terminal": state in {"completed", "failed", "unknown", "declined", "expired", "superseded"},
            "taskId": context.task_id, "interactionId": context.interaction["interaction_id"],
        }
        if operation_id:
            result["operationId"] = operation_id
        if context.task_id and context.resume_arguments.get("batch_id"):
            from agentbridge.core.task_projections import trusted_batch_projection
            from agentbridge.core.oa_batch_remaining import remaining_presentation
            batch = self.service.tasks.get_batch_for_task(parent_task_id=context.task_id, user_subject=context.resource["user_subject"])
            if batch and batch["batch_id"] == context.resume_arguments["batch_id"]:
                result["batch"] = trusted_batch_projection(batch)
                result["oaBatchRemaining"] = remaining_presentation(self.service, context.resource["user_subject"], context.task_id)
        return result
