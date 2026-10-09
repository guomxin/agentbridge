from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import (
    Callable,
    Iterator,
)
from uuid import uuid4

from agentbridge.adapters.seeyon_pending_batch import (
    PENDING_BATCH_PREPARE_CAPABILITY,
    PendingBatchSelectionError,
    select_pending_batch_items,
)

from agentbridge.adapters.seeyon_central import (
    SeeyonCentralAdapter,
    build_central_capability_registry,
)
from agentbridge.adapters.seeyon_documents import DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY
from agentbridge.adapters.seeyon_addressbook import ADDRESSBOOK_EXPORT_CAPABILITY
from agentbridge.adapters.base import (
    AdapterBusinessRuleRejected,
    AdapterLoginRequired,
    AdapterSessionCheckUnavailable,
)
from agentbridge.adapters.taihua import (
    TAIHUA_ADAPTER_ID,
    TAIHUA_SYSTEM_ID,
    TaihuaCentralAdapter,
    build_taihua_capability_registry,
)
from agentbridge.adapters.smartlight import (
    SMARTLIGHT_ADAPTER_ID,
    SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
    SMARTLIGHT_SYSTEM_ID,
    SmartlightCentralAdapter,
    build_smartlight_capability_registry,
)
from agentbridge.adapters.yuque import (
    YUQUE_ADAPTER_ID,
    YUQUE_SYSTEM_ID,
    YuqueCentralAdapter,
    build_yuque_capability_registry,
)
from agentbridge.adapters.seeyon_missed_punch import (
    MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
    MISSED_PUNCH_APPROVE_CAPABILITY,
    select_missed_punch_approval_batch_items,
)
from agentbridge.admin.stores import GovernancePolicyDenied
from agentbridge.browser.central import (
    AttachedCentralBrowserWorker,
    CentralBrowserWorker,
)
from agentbridge.browser.http import CentralHttpWorker
from agentbridge.core.capability import CapabilityRegistry
from agentbridge.core.business_skills import SkillRejected, validate_binding
from agentbridge.core.capability_runtime import (
    CapabilityRejected,
    CapabilityContext,
    CapabilityEngine,
    RequiresUserAction,
)
from agentbridge.core.central_storage import CentralAccessStores, CentralRuntimeStores
from agentbridge.core.login_recovery import resume_login_read
from agentbridge.core.controlled_write_executor import (
    ControlledWriteDependencies,
    ControlledWriteExecutor,
)
from agentbridge.core.write_catalog import (
    _TRUSTED_WRITE_DEFINITIONS,
    _TRUSTED_WRITE_COMMITS,
    _CAPABILITY_SCOPES,
    capability_required_scopes,
    resolve_write_function,
)
from agentbridge.core.runtime_governance import classify_runtime_error
from agentbridge.core.document_downloads import (
    DocumentDownloadAccessDenied,
    DocumentDownloadIntegrityError,
    DocumentDownloadNotFound,
    DocumentDownloadStateError,
    PREPARED_DOCUMENT_TTL_SECONDS,
)
from agentbridge.core.report_exports import (
    ADDRESSBOOK_REPORT_ARTIFACT_TYPE,
    ADDRESSBOOK_REPORT_CONTENT_TYPE,
    ADDRESSBOOK_REPORT_DOCUMENT_TYPE,
    SMARTLIGHT_REPORT_ARTIFACT_TYPE,
    SMARTLIGHT_REPORT_CONTENT_TYPE,
    SMARTLIGHT_REPORT_DOCUMENT_TYPE,
    addressbook_report_filename,
    addressbook_report_recipe,
    is_addressbook_report_recipe,
    is_smartlight_report_recipe,
    render_addressbook_report_csv,
    render_smartlight_report_csv,
    smartlight_report_filename,
    smartlight_report_recipe,
)
from agentbridge.core.field_submissions import FieldSubmissionStateError
from agentbridge.core.interactions import (
    InteractionIntegrityError,
    build_interaction_envelope,
)
from agentbridge.core.session_secrets import (
    SessionSecretError,
    SessionStateAccessDenied,
    SessionStateStore,
)
from agentbridge.core.tasks import (
    TaskIntegrityError,
    TaskNotFound,
)
from agentbridge.core.planning_catalog import build_planning_catalog
from agentbridge.core.planning_policy import (
    COMPOSED_TASK_POLICY_VERSION,
    authority_snapshot,
    compile_temporal_constraints,
    planning_capability_for_tool,
    planning_descriptor,
)
from agentbridge.core.task_plan_runtime import TaskPlanRuntime
from agentbridge.core.task_plan_validation import (
    PlanValidationError,
    validate_and_compile_task_plan,
)
from agentbridge.core.task_plans import (
    ACTIVE_PLAN_STATES,
    task_plan_response,
)
from agentbridge.core.write_authorizations import WriteAuthorizationStateError


WorkerFactory = Callable[[dict, object], object]
ACTIVITY_GATED_CLIENT_TYPES = {"openclaw-weixin", "wechat", "weixin"}


def _serialize_host_task_calls(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        task_id = kwargs.get("task_id")
        if not task_id:
            return method(self, *args, **kwargs)
        with self._task_call_lock(str(task_id)):
            return method(self, *args, **kwargs)

    return wrapped


class CentralCapabilityService:
    def __init__(
        self,
        *,
        home: Path | str,
        base_url: str,
        taihua_base_url: str | None = None,
        smartlight_base_url: str | None = None,
        smartlight_allow_insecure_http: bool = False,
        yuque_base_url: str | None = None,
        yuque_organization_id: int | None = None,
        registry: CapabilityRegistry | None = None,
        worker_factory: WorkerFactory | None = None,
        session_state_store: SessionStateStore | None = None,
        trusted_card_base_url: str = "http://127.0.0.1:8780",
        session_keepalive_lease_seconds: float | None = None,
    ) -> None:
        self.home = Path(home)
        self.db_path = self.home / "agentbridge.db"
        access_stores = CentralAccessStores.create(self.db_path)
        self.user_grants = access_stores.user_grants
        self.skills = access_stores.skills
        from agentbridge.core.skill_authoring import SkillAuthoring
        self.skill_authoring = SkillAuthoring(self)
        if registry is None:
            self.registry = build_central_capability_registry()
            for spec in build_taihua_capability_registry().list():
                self.registry.register(spec)
            for spec in build_smartlight_capability_registry().list():
                self.registry.register(spec)
            for spec in build_yuque_capability_registry().list():
                self.registry.register(spec)
        else:
            self.registry = registry
        runtime_stores = CentralRuntimeStores.create(
            self.home,
            session_state_store=session_state_store,
            release_id=os.environ.get("AGENTBRIDGE_RELEASE_ID") or "development",
        )
        self.operations = runtime_stores.operations
        self.sessions = runtime_stores.sessions
        self.session_states = runtime_stores.session_states
        self.challenges = runtime_stores.challenges
        self.field_submissions = runtime_stores.field_submissions
        self.document_downloads = runtime_stores.document_downloads
        self.timeline_attachments = runtime_stores.timeline_attachments
        self.write_authorizations = runtime_stores.write_authorizations
        self.interactions = runtime_stores.interactions
        self.tasks = runtime_stores.tasks
        self.task_plans = runtime_stores.task_plans
        self.transforms = runtime_stores.transforms
        self.host_contract = runtime_stores.host_contract
        self.workspace = runtime_stores.workspace
        self.governance_policies = runtime_stores.governance_policies
        self.runtime_governance = runtime_stores.runtime_governance
        self.adapter = SeeyonCentralAdapter(base_url=base_url)
        self.worker_factory = worker_factory or self._default_worker_factory
        self._adapters_by_system: dict[str, object] = {"oa": self.adapter}
        self._worker_factories_by_system: dict[str, WorkerFactory] = {
            "oa": self.worker_factory
        }
        self._adapter_systems: dict[str, str] = {"seeyon-central": "oa"}
        if taihua_base_url:
            taihua_adapter = TaihuaCentralAdapter(base_url=taihua_base_url)
            self._adapters_by_system[TAIHUA_SYSTEM_ID] = taihua_adapter
            self._worker_factories_by_system[TAIHUA_SYSTEM_ID] = (
                self._default_http_worker_factory
            )
            self._adapter_systems[TAIHUA_ADAPTER_ID] = TAIHUA_SYSTEM_ID
        if smartlight_base_url:
            smartlight_adapter = SmartlightCentralAdapter(
                base_url=smartlight_base_url,
                allow_insecure_http=smartlight_allow_insecure_http,
            )
            self._adapters_by_system[SMARTLIGHT_SYSTEM_ID] = smartlight_adapter
            self._worker_factories_by_system[SMARTLIGHT_SYSTEM_ID] = (
                self._default_http_worker_factory
            )
            self._adapter_systems[SMARTLIGHT_ADAPTER_ID] = SMARTLIGHT_SYSTEM_ID
        if yuque_base_url and yuque_organization_id:
            yuque_adapter = YuqueCentralAdapter(
                base_url=yuque_base_url,
                organization_id=yuque_organization_id,
            )
            self._adapters_by_system[YUQUE_SYSTEM_ID] = yuque_adapter
            self._worker_factories_by_system[YUQUE_SYSTEM_ID] = (
                self._default_browser_worker_factory
            )
            self._adapter_systems[YUQUE_ADAPTER_ID] = YUQUE_SYSTEM_ID
        self.trusted_card_base_url = trusted_card_base_url
        if (
            session_keepalive_lease_seconds is not None
            and session_keepalive_lease_seconds <= 0
        ):
            raise ValueError("session keepalive lease must be positive")
        self.session_keepalive_lease_seconds = session_keepalive_lease_seconds
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._task_call_locks = tuple(threading.RLock() for _ in range(64))
        self.task_plan_runtime = TaskPlanRuntime(
            service=self,
            plans=self.task_plans,
            transforms=self.transforms,
        )
        self._task_plan_authority_resolver = None


    def list_capabilities(self, *, system: str | None = None) -> dict:
        return {
            "protocolVersion": "0.1",
            "capabilities": [spec.to_dict() for spec in self.registry.list(system=system)],
        }

    def describe_capability(self, name: str) -> dict:
        return {
            "protocolVersion": "0.1",
            "capability": self.registry.describe(name),
        }

    def planning_required_scopes(self, capability_name: str) -> frozenset[str]:
        if capability_name in _CAPABILITY_SCOPES:
            return _CAPABILITY_SCOPES[capability_name]
        spec = self.registry.get(capability_name)
        if spec.effect != "read":
            raise KeyError(
                f"capability has no planning scope policy: {capability_name}"
            )
        if capability_name.startswith("oa.addressbook."):
            return frozenset({"oa:read:addressbook"})
        return frozenset({f"{spec.system}:read"})

    def planning_catalog(
        self, *, granted_scopes: list[str] | set[str], user_subject: str | None = None,
    ) -> dict:
        catalog = build_planning_catalog(
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=_TRUSTED_WRITE_DEFINITIONS,
            hidden_commit_capabilities=_TRUSTED_WRITE_COMMITS,
            scope_resolver=self.planning_required_scopes,
            granted_scopes=granted_scopes,
        )
        grant = self.user_grants.get(user_subject) if user_subject else None
        if user_subject is not None:
            allowed = set(grant["permissions"] if grant else [])
            from agentbridge.core.user_grants import CAPABILITY_PERMISSIONS
            catalog["capabilities"] = [
                item for item in catalog.get("capabilities", [])
                if CAPABILITY_PERMISSIONS.get(item["name"]) in allowed
            ]
            catalog["examples"] = []
        return catalog

    def task_plan_required_scopes(self, proposal: dict) -> frozenset[str]:
        compiled = validate_and_compile_task_plan(
            proposal,
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=_TRUSTED_WRITE_DEFINITIONS,
            hidden_commit_capabilities=_TRUSTED_WRITE_COMMITS,
            scope_resolver=self.planning_required_scopes,
        )
        return frozenset(compiled["requiredScopes"])

    def prepare_task_plan(
        self,
        *,
        user_subject: str,
        task_id: str,
        proposal: dict,
        granted_scopes: list[str] | set[str],
        authority_identity: dict | None = None,
        idempotency_key: str | None = None,
        coordinator_lease_version: int | None = None,
        proposal_source: str = "agent_host",
    ) -> dict:
        task = self.tasks.get_task(task_id, user_subject=user_subject)
        try:
            compiled_proposal, temporal_context = compile_temporal_constraints(
                proposal,
                accepted_at=task.get("created_at"),
            )
        except ValueError as exc:
            raise PlanValidationError(
                "PLAN_TEMPORAL_CONSTRAINT_INVALID", str(exc)
            ) from exc
        compiled = validate_and_compile_task_plan(
            compiled_proposal,
            registry=self.registry,
            transforms=self.transforms,
            trusted_write_prepares=_TRUSTED_WRITE_DEFINITIONS,
            hidden_commit_capabilities=_TRUSTED_WRITE_COMMITS,
            scope_resolver=self.planning_required_scopes,
            granted_scopes=granted_scopes,
            temporal_context=temporal_context,
        )
        if compiled["proposalSchemaVersion"].endswith(".v2") or authority_identity is not None:
            if not isinstance(authority_identity, dict):
                raise PlanValidationError(
                    "PLAN_AUTHORITY_REQUIRED",
                    "计划必须绑定当前 MCP 执行授权。",
                )
            if authority_identity.get("user_subject") != user_subject:
                raise PlanValidationError(
                    "PLAN_AUTHORITY_INVALID",
                    "计划执行授权与用户不匹配。",
                )
            compiled["authoritySnapshot"] = authority_snapshot(
                authority_identity,
                required_scopes=compiled["requiredScopes"],
            )
        plan, reused = self.task_plans.create(
            user_subject=user_subject,
            parent_task_id=task_id,
            compiled_plan=compiled,
            proposal_source=proposal_source,
            coordinator_lease_version=coordinator_lease_version,
            idempotency_key=idempotency_key,
        )
        if reused:
            response = self.task_plan_runtime.advance(
                plan["plan_id"], user_subject=user_subject
            )
            return {**response, "reused": True}
        self.tasks.record_plan_event(
            task_id=task_id,
            user_subject=user_subject,
            event_type="plan.proposed",
            payload={
                "planId": plan["plan_id"],
                "revision": plan["revision"],
                "stepCount": len(plan["steps"]),
            },
            causation_ref=plan["plan_id"],
        )
        self.tasks.record_plan_event(
            task_id=task_id,
            user_subject=user_subject,
            event_type="plan.validated",
            payload={
                "planId": plan["plan_id"],
                "systems": plan["risk_summary"].get("systems") or [],
                "writeSinkCount": plan["risk_summary"].get(
                    "writeSinkCount", 0
                ),
            },
            causation_ref=plan["plan_id"],
        )
        response = self.task_plan_runtime.start(
            plan["plan_id"], user_subject=user_subject
        )
        return {**response, "reused": False}

    def set_task_plan_authority_resolver(self, resolver) -> None:
        self._task_plan_authority_resolver = resolver

    def validate_task_plan_authority(self, plan: dict) -> None:
        snapshot = plan.get("authority_snapshot")
        if not isinstance(snapshot, dict):
            if str(plan.get("schema_version") or "").endswith(".v2"):
                raise PlanValidationError(
                    "PLAN_AUTHORITY_INVALID", "v2 计划缺少执行授权快照。"
                )
            return
        resolver = self._task_plan_authority_resolver
        if not callable(resolver):
            raise PlanValidationError(
                "PLAN_AUTHORITY_UNAVAILABLE", "中央服务暂时无法复核计划执行授权。"
            )
        try:
            identity = resolver(
                snapshot.get("tokenId"),
                set(snapshot.get("requiredScopes") or []),
            )
        except Exception as exc:
            raise PlanValidationError(
                "PLAN_AUTHORITY_INVALID", "计划执行授权已过期、撤销或缩权。"
            ) from exc
        if identity.get("user_subject") != plan.get("user_subject"):
            raise PlanValidationError(
                "PLAN_AUTHORITY_INVALID", "计划执行授权主体已不匹配。"
            )

    def validate_task_plan_execution(self, plan: dict) -> None:
        try:
            self.validate_skill_task(plan["user_subject"], plan["parent_task_id"])
        except SkillRejected as exc:
            raise PlanValidationError(exc.code, str(exc)) from exc
        if plan["state"] not in ACTIVE_PLAN_STATES:
            raise PlanValidationError("PLAN_NOT_ACTIVE", "任务计划已结束，不能继续执行。")
        self.validate_task_plan_authority(plan)
        self.task_plan_runtime.validate_versions(plan)

    def retire_plan_interactions(self, plan: dict, *, extra: dict | None = None) -> None:
        interaction_ids = {s["interaction_id"] for s in plan["steps"] if s.get("interaction_id")}
        if isinstance(extra, dict) and extra.get("interactionId"):
            interaction_ids.add(extra["interactionId"])
        for interaction_id in interaction_ids:
            record = self.interactions.get(interaction_id, user_subject=plan["user_subject"])
            if record["interaction_type"] == "business_input":
                self.field_submissions.supersede(record["resource_id"], user_subject=plan["user_subject"])
            elif record["interaction_type"] == "execution_authorization":
                self.write_authorizations.supersede(record["resource_id"], user_subject=plan["user_subject"])
            # A login challenge may also serve other reads in the same session.

    def planning_gate_for_call(
        self,
        *,
        user_subject: str,
        task_id: str | None,
        capability_name: str,
        host_type: str,
    ) -> dict | None:
        if not task_id or host_type in {
            "task_plan",
            "batch_coordinator",
            "interaction_resume",
        }:
            return None
        descriptor = planning_descriptor(capability_name)
        if not descriptor:
            return None
        roles = set(descriptor.get("roles", []))
        if not self._task_has_business_source(user_subject=user_subject, task_id=task_id):
            return None
        if "business_source" in roles:
            message = (
                "该请求正在汇聚多个业务来源，必须进入持久计划后再继续读取。"
            )
        elif "write_sink" in roles:
            message = "该写入内容依赖本任务已读取的业务数据，必须进入持久计划。"
        else:
            return None
        return self._planning_control(message)

    def _task_has_business_source(self, *, user_subject: str, task_id: str) -> bool:
        source_names = {
            spec.name for spec in self.registry.list()
            if "business_source" in (planning_descriptor(spec.name) or {}).get("roles", [])
        }
        return self.tasks.task_has_succeeded_capability(
            task_id=task_id, user_subject=user_subject, capability_names=source_names,
        )

    @staticmethod
    def _planning_control(message: str) -> dict:
        return {
            "status": "planning_control",
            "error": {
                "code": "PLAN_REQUIRED",
                "message": message + (
                    " 只读持久计划只保存AgentBridge内部执行状态，不向OA或其他业务系统写入。"
                    "用户要求不写入时，应创建无写入步骤、以结果投影结束的只读计划，"
                    "不要因此停止读取；业务写入仍须单独授权。"
                ),
            },
            "nextAction": {
                "type": "tool_sequence",
                "tool": "agentbridge_task_plan_catalog",
                "then": "agentbridge_task_plan_prepare",
                "doNotRetryCapability": True,
            },
            "recovery": {
                "action": "prepare_task_plan",
                "policyVersion": COMPOSED_TASK_POLICY_VERSION,
                "maximumRepairAttempts": 1,
                "requiredProposalShape": {
                    "schemaVersion": "agentbridge.task-plan.proposal.v2",
                    "readOnlyTerminal": "catalog_result_projection_transform",
                    "bindEveryBusinessSource": True,
                },
            },
        }

    def validate_skill_task(self, user_subject, task_id, *, capability=None, connection=None):
        binding = self.skills.for_task(user_subject, task_id, connection)
        if binding:
            validate_binding(self, user_subject, binding, capability=capability)

    def guard_skill_authorization(self, connection, authorization_id, user_subject):
        rows = connection.execute(
            "SELECT ti.task_id FROM task_interactions ti JOIN interactions i ON i.interaction_id=ti.interaction_id "
            "WHERE i.resource_id=? AND ti.user_subject=?", (authorization_id, user_subject)
        ).fetchall()
        for row in rows:
            self.validate_skill_task(user_subject, row[0], connection=connection)

    def get_task_plan(self, *, user_subject: str, plan_id: str) -> dict:
        plan = self.task_plans.get(plan_id, user_subject=user_subject)
        for step in plan["steps"]:
            capability = step.get("capability_name")
            if capability:
                self.user_grants.require_capability(user_subject, capability, step.get("arguments"))
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "plan": task_plan_response(plan),
        }

    @_serialize_host_task_calls
    def cancel_unsubmitted_task(self, *, user_subject: str, task_id: str) -> dict:
        task = self.tasks.get_task(task_id, user_subject=user_subject)
        plan = self.task_plans.get_for_task(parent_task_id=task_id, user_subject=user_subject)
        if plan:
            return self.cancel_task_plan(user_subject=user_subject, plan_id=plan["plan_id"])
        if task["status"] == "canceled":
            return {"status": "succeeded", "task": task_response(task), "businessWriteOccurred": False}
        interaction_id = task.get("current_interaction_id")
        if task["status"] != "waiting_user" or not interaction_id:
            return {"status": "rejected", "error": {"code": "TASK_NOT_CANCELABLE",
                    "message": "只能取消尚未提交的填写或授权任务；已完成、执行中及结果未知的业务不能通过此入口撤销。"}}
        interaction = self.interactions.get(interaction_id, user_subject=user_subject)
        batch = self.tasks.get_batch_for_task(parent_task_id=task_id, user_subject=user_subject)
        def cancel_batch(connection):
            self.tasks.cancel_task(task_id=task_id, user_subject=user_subject,
                reason="user_canceled_unsubmitted_task", causation_ref=interaction_id, connection=connection)
        retire_options = {"before_supersede": cancel_batch} if batch else {}
        # Check and retire the pending resource in its own write transaction;
        # approval/submission that wins this race must never be called canceled.
        try:
            if interaction["interaction_type"] == "credential":
                preceding = self.tasks.operation_before_interaction(
                    task_id=task_id, user_subject=user_subject, interaction_id=interaction_id,
                )
                original = self.operations.get(preceding["operation_id"]) if preceding else None
                if not original or (
                    self.registry.get(original["capability_name"]).effect != "read"
                    or original["status"] != "requires_user_action"
                    or (original.get("error") or {}).get("code") != "LOGIN_REQUIRED"
                    or task.get("current_operation_id") != original["operation_id"]
                ):
                    return {"status": "rejected", "error": {"code": "TASK_NOT_CANCELABLE",
                            "message": "Only the original waiting read can be canceled during login."}}
                # Authentication can serve other tasks. Cancel only this read;
                # the same task lock serializes cancellation with read resume.
            elif interaction["interaction_type"] == "business_input":
                self.field_submissions.supersede(interaction["resource_id"], user_subject=user_subject, pending_only=True, **retire_options)
            elif interaction["interaction_type"] == "execution_authorization":
                self.write_authorizations.supersede(interaction["resource_id"], user_subject=user_subject, pending_only=True, **retire_options)
            else:
                return {"status": "rejected", "error": {"code": "TASK_NOT_CANCELABLE",
                        "message": "该交互不是独立业务填写或授权卡，不能在此取消。"}}
        except (FieldSubmissionStateError, WriteAuthorizationStateError):
            return {"status": "rejected", "error": {"code": "TASK_ACTION_ALREADY_STARTED",
                    "message": "卡片已提交或授权，不能再当作未提交任务取消；请先核对最新结果。"}}
        try:
            task = self.tasks.cancel_task(task_id=task_id, user_subject=user_subject,
                                         reason="user_canceled_unsubmitted_task", causation_ref=interaction_id)
        except TaskIntegrityError:
            # A poll can observe the resource retired above before this task update.
            # Keep that terminal state, but never swallow a different task outcome.
            task = self.tasks.get_task(task_id, user_subject=user_subject)
            if task["status"] != "superseded" or task.get("current_interaction_id") != interaction_id:
                raise
        return {"status": "succeeded", "task": task_response(task), "businessWriteOccurred": False}

    def cancel_task_plan(
        self,
        *,
        user_subject: str,
        plan_id: str,
        reason: str = "user_requested",
    ) -> dict:
        return self.task_plan_runtime.cancel(
            plan_id,
            user_subject=user_subject,
            reason=reason,
        )

    def recover_task_plans(self, *, limit: int = 100) -> dict:
        return self.task_plan_runtime.recover(limit=limit)

    def adapter_for_system(self, system_id: str) -> object:
        runtime = self._runtime_for_system(system_id)
        if runtime is None:
            raise KeyError(f"central runtime is not configured for {system_id}")
        return runtime[0]

    def _runtime_for_system(
        self,
        system_id: str,
    ) -> tuple[object, WorkerFactory] | None:
        adapter = self._adapters_by_system.get(system_id)
        worker_factory = self._worker_factories_by_system.get(system_id)
        if adapter is None or worker_factory is None:
            return None
        return adapter, worker_factory

    @staticmethod
    def _raise_system_not_configured(system_id: str) -> dict:
        raise CapabilityRejected(
            "SYSTEM_NOT_CONFIGURED",
            f"The central runtime is not configured for {system_id}.",
        )

    @_serialize_host_task_calls
    def invoke(
        self,
        *,
        user_subject: str,
        capability_name: str,
        arguments: dict,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
        host_type: str = "unknown",
        host_instance_id: str | None = None,
        host_run_id: str | None = None,
        origin_endpoint_id: str | None = None,
    ) -> dict:
        if capability_name.startswith('taihua.analytics.'):
            return {'status':'rejected','result':None,'error':{'code':'CAPABILITY_RETIRED','message':'旧数据库分析已退役，请使用 database_capabilities/database_execute。'}}

        self.validate_skill_task(user_subject, task_id, capability=capability_name)
        self.user_grants.require_capability(user_subject, capability_name, arguments)
        grant_before = self.user_grants.get(user_subject)

        engine = CapabilityEngine(registry=self.registry, operation_store=self.operations)
        spec = self.registry.get(capability_name)
        planning_control = self.planning_gate_for_call(
            user_subject=user_subject,
            task_id=task_id,
            capability_name=capability_name,
            host_type=host_type,
        )
        system_id = self._adapter_systems.get(spec.adapter, spec.system)
        effective_request_id = request_id or str(uuid4())
        trace, _trace_reused = self.runtime_governance.ensure_trace(
            user_subject=user_subject,
            request_id=effective_request_id,
            task_id=task_id,
            origin_endpoint_id=origin_endpoint_id,
            host_type=host_type,
            host_instance_id=host_instance_id,
            host_run_id=host_run_id,
            request_kind="read" if spec.effect == "read" else "write",
            system_id=system_id,
            capability_name=capability_name,
        )
        runtime = self._runtime_for_system(system_id)
        if planning_control is not None:
            engine.register_handler(
                capability_name,
                lambda _context, _inputs: (_ for _ in ()).throw(
                    CapabilityRejected(
                        "PLAN_REQUIRED",
                        planning_control["error"]["message"],
                    )
                ),
            )
        elif runtime is None:
            engine.register_handler(
                capability_name,
                lambda _context, _inputs: self._raise_system_not_configured(system_id),
            )
        else:
            adapter, selected_worker_factory = runtime
            if capability_name != DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY:
                self._record_user_activity(
                    user_subject=user_subject,
                    system_id=system_id,
                )
            engine.register_handler(
                capability_name,
                lambda context, inputs: self._invoke_adapter(
                    context=context,
                    user_subject=user_subject,
                    system_id=system_id,
                    adapter=adapter,
                    worker_factory=selected_worker_factory,
                    capability_name=capability_name,
                    arguments=inputs,
                    task_id=task_id,
                ),
            )
        try:
            response = engine.invoke(
                user_subject=user_subject,
                capability_name=capability_name,
                arguments=arguments,
                idempotency_key=idempotency_key,
                request_id=effective_request_id,
                trace_id=trace["trace_id"],
                task_id=task_id,
            )
        except Exception as exc:
            failure = classify_runtime_error(exc)
            self.runtime_governance.record_stage_once(
                trace_id=trace["trace_id"],
                stage="capability.invoke",
                status="failed",
                capability_name=capability_name,
                system_id=system_id,
                error_code=failure["code"],
            )
            self.runtime_governance.update_trace(
                trace["trace_id"],
                status="failed",
                finished=True,
            )
            raise
        operation = self.operations.get(response["operationId"])
        grant_after = self.user_grants.get(user_subject)
        if grant_before != grant_after and spec.effect == "read":
            return {
                **response,
                "status": "rejected", "result": None,
                "error": {"code": "AUTHORIZATION_CHANGED", "message": "User permissions changed during this read."},
                "evidenceRefs": [], "nextAction": None, "interaction": None,
            }
        self.runtime_governance.observe_operation(
            trace_id=trace["trace_id"],
            operation=operation,
            capability_effect=spec.effect,
            commit_capability=capability_name in _TRUSTED_WRITE_COMMITS,
        )
        interaction = response.get("interaction")
        if isinstance(interaction, dict) and interaction.get("interactionId"):
            self.runtime_governance.observe_interaction(
                trace_id=trace["trace_id"],
                interaction_id=interaction["interactionId"],
                interaction_type=str(interaction.get("type") or "unknown"),
                state=str(interaction.get("state") or "pending"),
                system_id=system_id,
            )
        observed_response = {**response, "runtimeTraceId": trace["trace_id"]}
        if (
            planning_control is not None
            and (response.get("error") or {}).get("code") == "PLAN_REQUIRED"
        ):
            observed_response = {
                **observed_response,
                **planning_control,
                "operationId": response.get("operationId"),
                "runtimeTraceId": trace["trace_id"],
            }
        if task_id:
            observed_response = self._apply_batch_operation_response(
                user_subject=user_subject,
                task_id=task_id,
                capability_name=capability_name,
                response=observed_response,
            )
        if task_id and host_type not in {
            "task_plan",
            "batch_coordinator",
            "interaction_resume",
        }:
            linked_interaction = observed_response.get("interaction")
            self.observe_host_task(
                user_subject=user_subject,
                task_id=task_id,
                operation_ids=[operation["operation_id"]],
                interaction_ids=(
                    [linked_interaction["interactionId"]]
                    if isinstance(linked_interaction, dict)
                    and linked_interaction.get("interactionId")
                    else []
                ),
            )
        return observed_response

    def _apply_batch_operation_response(
        self,
        *,
        user_subject: str,
        task_id: str,
        capability_name: str,
        response: dict,
    ) -> dict:
        batch = self.tasks.get_batch_for_task(
            parent_task_id=task_id,
            user_subject=user_subject,
            include_resource_refs=True,
        )
        if batch is None:
            return response
        if (response.get("error") or {}).get("code") in {
            "BATCH_CONTEXT_MISMATCH", "BATCH_TARGET_MISMATCH", "BATCH_CONTEXT_MISSING",
            "BATCH_NOT_ACTIVE", "BATCH_SELECTION_MISMATCH",
        }:
            return {**response, "batch": batch_response(batch)}
        generic = batch["capability_name"] == PENDING_BATCH_PREPARE_CAPABILITY
        current = batch["items"][int(batch["current_ordinal"]) - 1]
        prior_completed = next((item for item in batch["items"]
                                if item.get("operation_id") == response.get("operationId")
                                and item["state"] == "succeeded"), None)
        if prior_completed is not None:
            if batch["state"] == "succeeded":
                return {**response, "batch": batch_response(batch)}
            if batch["state"] in {"running", "waiting_user"}:
                if current.get("operation_id"):
                    latest = self.operations.get(current["operation_id"])
                    return {**operation_response(latest), "reused": True, "batch": batch_response(batch)}
                return self.invoke(
                    user_subject=user_subject, capability_name=batch["capability_name"],
                    arguments={"batch_id": batch["batch_id"], "affair_id": current["resource_ref"],
                               **({} if generic else _batch_item_card_context(batch, current))},
                    idempotency_key=f"batch:{batch['batch_id']}:item:{current['ordinal']}:prepare",
                    task_id=task_id, host_type="batch_coordinator", host_run_id=f"batch:{batch['batch_id']}",
                )
            return {**response, "batch": batch_response(batch)}
        commit_capability = (
            current["display_summary"]["commit_capability"]
            if generic else MISSED_PUNCH_APPROVE_CAPABILITY
        )
        if capability_name not in {batch["capability_name"], commit_capability}:
            return response
        operation_id = str(response.get("operationId") or "").strip() or None
        interaction = response.get("interaction")
        interaction_id = (
            str(interaction.get("interactionId") or "").strip() or None
            if isinstance(interaction, dict)
            else None
        )
        batch = self.tasks.record_batch_item_activity(
            parent_task_id=task_id,
            user_subject=user_subject,
            operation_id=operation_id,
            interaction_id=interaction_id,
        ) or batch
        self.observe_host_task(
            user_subject=user_subject,
            task_id=task_id,
            operation_ids=[operation_id] if operation_id else [],
            interaction_ids=[interaction_id] if interaction_id else [],
        )
        status = str(response.get("status") or "")
        governed_batch_step = capability_name in {batch["capability_name"], commit_capability}
        if governed_batch_step and status in {"failed", "unknown"}:
            error_code = str(
                (response.get("error") or {}).get("code")
                or ("RESULT_UNKNOWN" if status == "unknown" else "BATCH_ITEM_FAILED")
            )
            batch = self.tasks.fail_current_batch_item(
                parent_task_id=task_id,
                user_subject=user_subject,
                operation_id=operation_id,
                expected_ordinal=int(batch["current_ordinal"]),
                item_state="outcome_unknown" if status == "unknown" else "failed",
                error_code=error_code,
            )
            return {**response, "batch": batch_response(batch)}
        if (
            capability_name != commit_capability
            or status != "succeeded"
        ):
            return {**response, "batch": batch_response(batch)}

        completed_ordinal = int(batch["current_ordinal"])
        completion = self.tasks.complete_current_batch_item(
            parent_task_id=task_id,
            user_subject=user_subject,
            operation_id=operation_id or "batch-item-completed",
            expected_ordinal=completed_ordinal,
            result_summary={
                "workflowApproved": bool(
                    (response.get("result") or {}).get("workflow_approved")
                ),
                "verification": (
                    (response.get("result") or {}).get("verification") or {}
                ),
            },
        )
        batch = completion["batch"]
        next_item = completion.get("nextItem")
        if next_item is None:
            result = dict(response.get("result") or {})
            result["batch"] = batch_response(batch)
            return {
                **response,
                "result": result,
                "batch": batch_response(batch),
                "completedBatchItemOrdinal": completed_ordinal,
            }

        next_arguments = {
            "batch_id": batch["batch_id"],
            "affair_id": next_item["resource_ref"],
            **({} if generic else _batch_item_card_context(batch, next_item)),
        }
        next_response = self.invoke(
            user_subject=user_subject,
            capability_name=batch["capability_name"],
            arguments=next_arguments,
            idempotency_key=(
                f"batch:{batch['batch_id']}:item:{next_item['ordinal']}:prepare"
            ),
            task_id=task_id,
            host_type="batch_coordinator",
            host_run_id=f"batch:{batch['batch_id']}",
        )
        return {
            **next_response,
            "batch": next_response.get("batch") or batch_response(batch),
            "completedBatchItemOrdinal": completed_ordinal,
            "previousOperationId": operation_id,
        }

    def session_status(self, *, user_subject: str, system_id: str = "oa") -> dict:
        session = self.sessions.find(user_subject=user_subject, system_id=system_id)
        if session is None:
            return {
                "protocolVersion": "0.1",
                "status": "not_found",
                "systemId": system_id,
                "userSubject": user_subject,
                "statusSource": "registry",
                "checkedAt": None,
            }
        if session["state"] != "active":
            return {
                **self._session_response(session),
                "statusSource": "registry",
                "checkedAt": None,
            }

        live_check = self._reuse_active_session(
            user_subject=user_subject,
            session=session,
            record_verification=False,
            record_activity=True,
            transition_source="session_status",
        )
        checked_at = _utc_now()
        if live_check is None:
            return {
                **self._session_response(self.sessions.get(session["session_id"])),
                "statusSource": "live",
                "checkedAt": checked_at,
            }
        if live_check.get("status") == "succeeded":
            return {
                **live_check["session"],
                "statusSource": "live",
                "checkedAt": checked_at,
            }
        return {
            **live_check,
            "session": self._session_response(self.sessions.get(session["session_id"])),
            "statusSource": "live",
            "checkedAt": checked_at,
        }

    def inspect_session(self, *, user_subject: str, system_id: str) -> dict:
        """Run a live session check without extending the user's activity lease."""
        session = self.sessions.find(user_subject=user_subject, system_id=system_id)
        if session is None:
            return {
                "protocolVersion": "0.1",
                "status": "not_found",
                "systemId": system_id,
                "userSubject": user_subject,
                "statusSource": "registry",
                "checkedAt": None,
            }
        if session["state"] != "active":
            return {
                **self._session_response(session),
                "statusSource": "registry",
                "checkedAt": None,
            }
        live_check = self._reuse_active_session(
            user_subject=user_subject,
            session=session,
            record_verification=False,
            record_activity=False,
            transition_source="admin_live_check",
        )
        checked_at = _utc_now()
        if live_check is None:
            return {
                **self._session_response(self.sessions.get(session["session_id"])),
                "statusSource": "live",
                "checkedAt": checked_at,
            }
        if live_check.get("status") == "succeeded":
            return {
                **live_check["session"],
                "statusSource": "live",
                "checkedAt": checked_at,
            }
        return {
            **live_check,
            "session": self._session_response(self.sessions.get(session["session_id"])),
            "statusSource": "live",
            "checkedAt": checked_at,
        }
    def start_login(
        self,
        *,
        user_subject: str,
        expected_principal_ref: str | None,
        card_base_url: str,
        ttl_seconds: int = 300,
        system_id: str = "oa",
        task_id: str | None = None,
    ) -> dict:
        if task_id:
            self.tasks.get_task(task_id, user_subject=user_subject)
        runtime = self._runtime_for_system(system_id)
        if runtime is None:
            raise ValueError(f"central login is not configured for {system_id}")
        session = self.sessions.find(user_subject=user_subject, system_id=system_id)
        if session is None:
            if not expected_principal_ref:
                raise ValueError(
                    "expected downstream principal is required for a new session"
                )
            session = self.sessions.get_or_create(
                user_subject=user_subject,
                system_id=system_id,
                expected_principal_ref=expected_principal_ref,
            )
        elif expected_principal_ref:
            session = self.sessions.get_or_create(
                user_subject=user_subject,
                system_id=system_id,
                expected_principal_ref=expected_principal_ref,
            )
        expected = str(session.get("expected_principal_ref") or "").strip()
        if not expected:
            raise ValueError("expected downstream principal is not configured")

        if session["state"] == "active":
            reuse_response = self._reuse_active_session(
                user_subject=user_subject,
                session=session,
                transition_source="login_reuse",
            )
            if reuse_response is not None:
                return reuse_response
            session = self.sessions.get(session["session_id"])

        return self._create_login_challenge(
            session=session,
            expected_principal_ref=expected,
            card_base_url=card_base_url,
            ttl_seconds=ttl_seconds,
            task_id=task_id,
        )

    def _reuse_active_session(
        self,
        *,
        user_subject: str,
        session: dict,
        record_verification: bool = True,
        record_activity: bool = True,
        record_keepalive: bool = False,
        transition_source: str = "session_reuse",
    ) -> dict | None:
        runtime = self._runtime_for_system(session["system_id"])
        if runtime is None:
            return _session_check_unavailable_response(
                user_subject,
                session,
                diagnostics=f"system is not configured: {session['system_id']}",
            )
        adapter, selected_worker_factory = runtime
        with self._session_lock(session["session_id"]):
            session = self.sessions.get(session["session_id"])
            if session["state"] != "active":
                return None
            try:
                state = self.session_states.load(session["session_id"])
            except SessionStateAccessDenied:
                return _session_runtime_mismatch_response(user_subject, session)
            except SessionSecretError:
                return _session_state_unavailable_response(user_subject, session)
            if state is None:
                self.sessions.mark_expired(
                    session["session_id"],
                    "Encrypted session state is missing.",
                    source=transition_source,
                )
                return None

            try:
                with selected_worker_factory(session, adapter) as worker:
                    worker.restore_session_state(state)
                    probe_session = adapter.probe_session
                    if record_keepalive:
                        probe_session = getattr(
                            adapter,
                            "keepalive_session",
                            probe_session,
                        )
                    probe = probe_session(worker)
                    self.session_states.save(
                        session["session_id"],
                        worker.capture_session_state(),
                    )
            except AdapterLoginRequired as exc:
                self.sessions.mark_expired(
                    session["session_id"],
                    str(exc),
                    source=transition_source,
                )
                self.session_states.delete(session["session_id"])
                return None
            except AdapterSessionCheckUnavailable as exc:
                self.sessions.record_event(
                    session["session_id"],
                    event_type="check_deferred",
                    source=transition_source,
                    reason=str(exc),
                )
                if record_activity:
                    session = self.sessions.touch_activity(session["session_id"])
                return _session_check_unavailable_response(
                    user_subject,
                    session,
                    diagnostics=str(exc),
                )
            except SessionStateAccessDenied:
                return _session_runtime_mismatch_response(user_subject, session)
            except SessionSecretError:
                return _session_state_unavailable_response(user_subject, session)
            except Exception as exc:
                self.sessions.record_event(
                    session["session_id"],
                    event_type="check_deferred",
                    source=transition_source,
                    reason=f"{exc.__class__.__name__}: {exc}",
                )
                if record_activity:
                    session = self.sessions.touch_activity(session["session_id"])
                return _session_check_unavailable_response(
                    user_subject,
                    session,
                    diagnostics=f"{exc.__class__.__name__}: {exc}",
                )

            if probe.get("session_recovery"):
                self.sessions.record_event(
                    session["session_id"],
                    event_type="session_recovered",
                    source=transition_source,
                    reason=(
                        "Downstream application session was renewed through the "
                        f"existing {probe['session_recovery']} identity session."
                    ),
                )
            if record_verification:
                session = self.sessions.activate(
                    session["session_id"],
                    observed_principal_ref=session.get("downstream_principal_ref"),
                    source=transition_source,
                )
            elif record_activity:
                session = self.sessions.touch_activity(session["session_id"])
            elif record_keepalive:
                session = self.sessions.record_keepalive(session["session_id"])
            else:
                session = self.sessions.get(session["session_id"])
            return {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "sessionId": session["session_id"],
                "session": self._session_response(session),
                "result": {
                    "authenticated": True,
                    "templateCount": probe.get("template_count"),
                    "transport": probe["transport"],
                },
                "nextAction": None,
                "reused": True,
            }

    def run_session_keepalive_cycle(
        self,
        *,
        activity_lease_seconds: float,
        now: datetime | None = None,
    ) -> dict:
        if activity_lease_seconds <= 0:
            raise ValueError("activity lease must be positive")
        checked_at = _as_utc(now or datetime.now(timezone.utc))
        active_sessions = self.sessions.list_active()
        summary = {
            "checkedAt": checked_at.isoformat(),
            "activeSessions": len(active_sessions),
            "eligibleSessions": 0,
            "keptAlive": 0,
            "expired": 0,
            "deferred": 0,
            "outsideLease": 0,
            "inactive": 0,
            "issues": [],
            "expiredTimelineAttachments": self.timeline_attachments.prune_expired(
                now=checked_at
            ),
        }
        lease = timedelta(seconds=activity_lease_seconds)
        for session in active_sessions:
            if self._runtime_for_system(session["system_id"]) is None:
                summary["inactive"] += 1
                continue
            last_activity = _parse_utc(session.get("last_user_activity_at"))
            if last_activity is None or checked_at - last_activity > lease:
                summary["outsideLease"] += 1
                continue
            summary["eligibleSessions"] += 1
            response = self._reuse_active_session(
                user_subject=session["user_subject"],
                session=session,
                record_verification=False,
                record_activity=False,
                record_keepalive=True,
                transition_source="keepalive",
            )
            if response is None:
                current = self.sessions.get(session["session_id"])
                if current["state"] == "expired":
                    summary["expired"] += 1
                    summary["issues"].append(
                        {
                            "userSubject": session["user_subject"],
                            "systemId": session["system_id"],
                            "outcome": "expired",
                            "diagnostics": current.get("last_error"),
                        }
                    )
                else:
                    summary["inactive"] += 1
                    summary["issues"].append(
                        {
                            "userSubject": session["user_subject"],
                            "systemId": session["system_id"],
                            "outcome": "inactive",
                            "diagnostics": current.get("last_error"),
                        }
                    )
            elif response.get("status") == "succeeded":
                summary["keptAlive"] += 1
            else:
                summary["deferred"] += 1
                error = response.get("error")
                result = response.get("result")
                summary["issues"].append(
                    {
                        "userSubject": session["user_subject"],
                        "systemId": session["system_id"],
                        "outcome": "deferred",
                        "errorCode": (
                            error.get("code") if isinstance(error, dict) else None
                        ),
                        "diagnostics": (
                            result.get("diagnostics")
                            if isinstance(result, dict)
                            else (
                                error.get("message")
                                if isinstance(error, dict)
                                else None
                            )
                        ),
                    }
                )
        signal_status = (
            "failed"
            if summary["eligibleSessions"] and not summary["keptAlive"] and summary["expired"]
            else "degraded"
            if summary["expired"] or summary["deferred"] or summary["issues"]
            else "succeeded"
        )
        self.runtime_governance.record_signal(
            signal_type="session.keepalive.cycle",
            source="central_service",
            status=signal_status,
            value={
                key: value
                for key, value in summary.items()
                if key not in {"issues"}
            },
            observed_at=summary["checkedAt"],
        )
        for issue in summary["issues"]:
            self.runtime_governance.record_signal(
                signal_type="session.keepalive.issue",
                source="central_service",
                status=str(issue.get("outcome") or "degraded"),
                system_id=str(issue.get("systemId") or "") or None,
                user_subject=str(issue.get("userSubject") or "") or None,
                value={
                    "outcome": issue.get("outcome"),
                    "errorCode": issue.get("errorCode"),
                    "diagnostics": issue.get("diagnostics"),
                },
                observed_at=summary["checkedAt"],
            )
        return summary

    def _create_login_challenge(
        self,
        *,
        session: dict,
        expected_principal_ref: str,
        card_base_url: str,
        ttl_seconds: int,
        task_id: str | None = None,
    ) -> dict:
        adapter = self.adapter_for_system(session["system_id"])
        contract = adapter.authentication_contract()
        challenge, reused = self.challenges.create_or_reuse(
            user_subject=session["user_subject"],
            system_id=session["system_id"],
            system_name=contract["system_name"],
            session_id=session["session_id"],
            expected_principal_ref=expected_principal_ref,
            origin=contract["origin"],
            page_fingerprint=contract["page_fingerprint"],
            nonce=None,
            fields=contract["fields"],
            card_base_url=card_base_url,
            ttl_seconds=ttl_seconds,
            challenge_type=(
                "interactive_browser_login"
                if contract.get("authentication_mode") == "interactive_browser"
                else "legacy_form_login"
            ),
        )
        interaction = self._credential_interaction(challenge, task_id=task_id)
        return {
            "protocolVersion": "0.1",
            "status": "requires_user_action",
            "sessionId": session["session_id"],
            "challenge": challenge_response(challenge),
            "nextAction": {
                "type": "open_authentication_card",
                "interactionId": interaction["interactionId"],
                "challengeId": challenge["challenge_id"],
                "cardUrl": challenge["card_url"],
                "interaction": interaction,
            },
            "interaction": interaction,
            "reused": reused,
        }

    def get_operation(self, *, user_subject: str, operation_id: str) -> dict:
        operation = self.operations.get(operation_id)
        if operation["user_subject"] != user_subject:
            raise KeyError(f"operation not found: {operation_id}")
        self.user_grants.require_capability(user_subject, operation["capability_name"], operation.get("input_summary"))
        if operation["capability_name"] == "oa.template.list" and isinstance(operation.get("result"), dict):
            operation = {**operation, "result": self.user_grants.filter_templates(user_subject, operation["result"])}
        task_id = self.tasks.task_id_for_operation(
            operation_id,
            user_subject=user_subject,
        )
        return {
            "protocolVersion": "0.1",
            "operation": {
                **operation_response(operation),
                "taskId": task_id,
            },
        }

    def list_operations(self, *, user_subject: str, limit: int = 100) -> dict:
        operations = self.operations.list(user_subject=user_subject, limit=limit)
        visible = []
        for operation in operations:
            try:
                self.user_grants.require_capability(user_subject, operation["capability_name"], operation.get("input_summary"))
                if operation["capability_name"] == "oa.template.list" and isinstance(operation.get("result"), dict):
                    operation = {**operation, "result": self.user_grants.filter_templates(user_subject, operation["result"])}
            except PermissionError:
                continue
            visible.append(operation)
        operations = visible
        return {
            "protocolVersion": "0.1",
            "count": len(operations),
            "operations": [
                {
                    **operation_response(operation),
                    "taskId": self.tasks.task_id_for_operation(
                        operation["operation_id"],
                        user_subject=user_subject,
                    ),
                }
                for operation in operations
            ],
        }

    def get_interaction(self, *, user_subject: str, interaction_id: str) -> dict:
        record, _resource, interaction = self._load_interaction(
            user_subject=user_subject,
            interaction_id=interaction_id,
        )
        if record.get("operation_id"):
            operation = self.operations.get(record["operation_id"])
            self.user_grants.require_capability(user_subject, operation["capability_name"], operation.get("input_summary"))
        task_id = self.tasks.task_id_for_interaction(
            interaction_id,
            user_subject=user_subject,
        )
        if task_id:
            self.tasks.link_interaction(
                task_id=task_id,
                user_subject=user_subject,
                interaction_record=record,
                interaction=interaction,
            )
            if interaction["state"] in {
                "declined",
                "expired",
                "failed",
                "superseded",
            }:
                self.task_plan_runtime.handle_terminal_interaction(
                    user_subject=user_subject,
                    interaction_id=interaction_id,
                    interaction_state=interaction["state"],
                )
                batch = self.tasks.get_batch_for_task(
                    parent_task_id=task_id,
                    user_subject=user_subject,
                )
                if batch is not None and batch["state"] in {
                    "running",
                    "waiting_user",
                    "paused",
                }:
                    current_ordinal = int(batch["current_ordinal"])
                    current_item = next(
                        (
                            item
                            for item in batch.get("items") or []
                            if int(item["ordinal"]) == current_ordinal
                        ),
                        None,
                    )
                    if (
                        current_item is not None
                        and current_item.get("interaction_id") == interaction_id
                    ):
                        self.tasks.fail_current_batch_item(
                            parent_task_id=task_id,
                            user_subject=user_subject,
                            operation_id=record.get("operation_id"),
                            expected_ordinal=current_ordinal,
                            item_state=(
                                "canceled"
                                if interaction["state"] == "declined"
                                else interaction["state"]
                            ),
                            error_code=f"INTERACTION_{interaction['state'].upper()}",
                        )
        return {
            "protocolVersion": "0.1",
            "interaction": {
                **interaction,
                "taskId": task_id,
            },
        }

    def present_interaction(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        interaction_id: str,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        record, resource, interaction = self._load_interaction(
            user_subject=user_subject,
            interaction_id=interaction_id,
        )
        if record.get("operation_id"):
            operation = self.operations.get(record["operation_id"])
            self.user_grants.require_capability(user_subject, operation["capability_name"], operation.get("input_summary"))
        task_id = self.tasks.task_id_for_interaction(
            interaction_id,
            user_subject=user_subject,
        )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "interaction": {
                **self._present_interaction_for_endpoint(
                    record=record,
                    resource=resource,
                    interaction=interaction,
                    endpoint=endpoint,
                ),
                "taskId": task_id,
            },
            "endpoint": endpoint_response(endpoint),
        }

    def claim_host_notifications(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        limit: int = 10,
        lease_seconds: int = 30,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        deliveries = self.tasks.claim_outbox(
            user_subject=user_subject,
            endpoint_id=endpoint["endpoint_id"],
            limit=limit,
            lease_seconds=lease_seconds,
        )
        notifications = []
        for delivery in deliveries:
            trace = (
                self.runtime_governance.trace_for_task(
                    delivery["task_id"], user_subject=user_subject
                )
                if delivery.get("task_id")
                else None
            )
            if trace is not None:
                self.runtime_governance.record_stage_once(
                    trace_id=trace["trace_id"],
                    stage="delivery.claimed",
                    status="succeeded",
                    delivery_id=delivery["delivery_id"],
                    side_effect_boundary=trace["side_effect_boundary"],
                    metadata={
                        "endpointId": endpoint["endpoint_id"],
                        "clientType": endpoint.get("client_type"),
                        "attemptCount": delivery["attempt_count"],
                        "payloadType": delivery.get("payload_type"),
                    },
                )
            event = delivery["payload"]
            if delivery["payload_type"] == "timeline_message":
                notifications.append(
                    {
                        "deliveryId": delivery["delivery_id"],
                        "attemptCount": delivery["attempt_count"],
                        "task": None,
                        "event": None,
                        "deliveryMode": "timeline_message",
                        "interaction": None,
                        "message": _timeline_notification_message(event),
                        "timeline": event,
                        "attachments": list(event.get("attachments") or []),
                    }
                )
                continue
            task = self.tasks.get_task(
                delivery["task_id"],
                user_subject=user_subject,
            )
            item = {
                "deliveryId": delivery["delivery_id"],
                "attemptCount": delivery["attempt_count"],
                "task": task_response(task),
                "event": event,
                "deliveryMode": "no_op",
                "interaction": None,
                "artifact": None,
                "message": None,
            }
            if task["origin_endpoint_id"] == endpoint["endpoint_id"]:
                item["deliveryMode"] = "origin_handled"
            elif event.get("eventType") == "task.artifact.ready":
                # Protected CSV URLs require a Workspace session. Companion
                # notifications cannot fetch them as anonymous media or send
                # them through the generic certificate-link fallback.
                if (event.get("payload") or {}).get("artifactType") not in {"taihua_personal_csv", "database_csv"}:
                    item["deliveryMode"] = "artifact"
                    item["artifact"] = _artifact_notification(event)
            elif event.get("eventType") == "task.interaction.waiting":
                interaction_id = (event.get("payload") or {}).get(
                    "interactionId"
                )
                if interaction_id:
                    try:
                        record, resource, interaction = self._load_interaction(
                            user_subject=user_subject,
                            interaction_id=interaction_id,
                        )
                    except (KeyError, InteractionIntegrityError):
                        interaction = None
                    can_open_interaction = (
                        "trusted_interaction"
                        in set(endpoint.get("capabilities") or [])
                    )
                    if (
                        can_open_interaction
                        and interaction is not None
                        and interaction["state"] in {"pending", "processing"}
                    ):
                        item["deliveryMode"] = "trusted_interaction"
                        item["interaction"] = (
                            self._present_interaction_for_endpoint(
                                record=record,
                                resource=resource,
                                interaction=interaction,
                                endpoint=endpoint,
                            )
                        )
                    else:
                        item["deliveryMode"] = "status"
                        item["message"] = _task_notification_message(
                            task,
                            event,
                        )
            elif event.get("eventType") == "plan.result.ready":
                plan = self.task_plans.get_for_task(
                    parent_task_id=task["task_id"],
                    user_subject=user_subject,
                )
                projection = plan.get("result_projection") if plan else None
                if isinstance(projection, dict):
                    item["deliveryMode"] = "plan_result"
                    item["planResult"] = projection
                    item["message"] = _plan_result_notification_message(
                        task,
                        projection,
                    )
                else:
                    item["deliveryMode"] = "status"
                    item["message"] = _task_notification_message(task, event)
            elif event.get("eventType") in {
                "task.created",
                "task.operation.linked",
                "task.operation.running",
                "task.interaction.completed",
                "task.interaction.expired",
                "task.interaction.failed",
                "task.interaction.superseded",
                "task.canceled",
                "task.completed",
                "task.operation.succeeded",
                "task.operation.failed",
                "task.operation.outcome_unknown",
            }:
                item["deliveryMode"] = "status"
                item["message"] = _task_notification_message(task, event)
            elif str(event.get("eventType") or "").startswith("plan."):
                item["deliveryMode"] = "status"
                item["message"] = _task_notification_message(task, event)
            notifications.append(item)
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "count": len(notifications),
            "endpoint": endpoint_response(endpoint),
            "notifications": notifications,
        }

    def acknowledge_host_notification(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        delivery_id: str,
        succeeded: bool,
        retry_after_seconds: int = 5,
        defer_until_activity: bool = False,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        if (
            defer_until_activity
            and str(endpoint.get("client_type") or "").lower()
            not in ACTIVITY_GATED_CLIENT_TYPES
        ):
            raise ValueError(
                "endpoint does not support activity-gated notification delivery"
            )
        delivery = self.tasks.acknowledge_outbox(
            user_subject=user_subject,
            endpoint_id=endpoint["endpoint_id"],
            delivery_id=delivery_id,
            succeeded=succeeded,
            retry_after_seconds=retry_after_seconds,
            defer_until_activity=defer_until_activity,
        )
        if delivery.get("task_id"):
            trace = self.runtime_governance.trace_for_task(
                delivery["task_id"], user_subject=user_subject
            )
            if trace is not None:
                delivery_state = str(delivery.get("state") or "failed")
                span_status = (
                    "succeeded"
                    if delivery_state == "acknowledged"
                    else "waiting"
                    if delivery_state in {"deferred", "pending"}
                    else "failed"
                )
                self.runtime_governance.record_stage_once(
                    trace_id=trace["trace_id"],
                    stage="delivery.result",
                    status=span_status,
                    delivery_id=delivery["delivery_id"],
                    error_code=None if succeeded else "DELIVERY_FAILED",
                    side_effect_boundary=trace["side_effect_boundary"],
                    metadata={
                        "endpointId": endpoint["endpoint_id"],
                        "clientType": endpoint.get("client_type"),
                        "deliveryState": delivery_state,
                        "attemptCount": delivery.get("attempt_count"),
                    },
                )
                if span_status == "failed":
                    self.runtime_governance.record_signal(
                        signal_type="delivery.failure",
                        source="task_hub",
                        status="failed",
                        user_subject=user_subject,
                        host_type=agent_host,
                        trace_id=trace["trace_id"],
                        value={
                            "deliveryId": delivery["delivery_id"],
                            "endpointId": endpoint["endpoint_id"],
                            "attemptCount": delivery.get("attempt_count"),
                        },
                    )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "delivery": {
                "deliveryId": delivery["delivery_id"],
                "state": delivery["state"],
                "attemptCount": delivery["attempt_count"],
                "nextAttemptAt": delivery["next_attempt_at"],
                "acknowledgedAt": delivery.get("acknowledged_at"),
            },
        }

    def report_host_artifact_delivery(
        self,
        *,
        user_subject: str,
        agent_host: str,
        task_id: str,
        delivery_ref: str,
        channel: str,
        files: list[dict],
    ) -> dict:
        task, event, reused = self.tasks.record_artifact_delivery(
            task_id=task_id,
            user_subject=user_subject,
            agent_host=agent_host,
            delivery_ref=delivery_ref,
            channel=channel,
            files=files,
        )
        trace = self.runtime_governance.trace_for_task(
            task_id, user_subject=user_subject
        )
        if trace is not None:
            states = [str(item.get("state") or "") for item in files]
            succeeded = bool(states) and all(
                state in {"delivered", "succeeded", "acknowledged"}
                for state in states
            )
            self.runtime_governance.record_stage_once(
                trace_id=trace["trace_id"],
                stage="artifact.delivery",
                status="succeeded" if succeeded else "failed",
                delivery_id=delivery_ref,
                artifact_id=(
                    str(files[0].get("artifact_id") or "") or None
                    if files
                    else None
                ),
                error_code=None if succeeded else "ARTIFACT_DELIVERY_FAILED",
                side_effect_boundary=trace["side_effect_boundary"],
                metadata={
                    "channel": channel,
                    "fileCount": len(files),
                    "states": states,
                    "reused": reused,
                },
            )
        return {
            "protocolVersion": "0.1",
            "schemaVersion": "agentbridge.host-artifact-delivery.v1",
            "status": "succeeded",
            "task": task_response(task),
            "delivery": event.get("payload") or {},
            "eventId": event["event_id"],
            "reused": reused,
        }

    def ensure_host_task(
        self,
        *,
        user_subject: str,
        token_id: str,
        agent_host: str,
        host_task_key: str,
        endpoint_key: str,
        client_type: str,
        external_subject: str,
        conversation_ref: str,
        title: str,
        account_id: str | None = None,
        label: str | None = None,
        route: dict | None = None,
        capabilities: list[str] | None = None,
        task_scope: str = "host_run",
        tool_name: str | None = None,
        planning_task_key: str | None = None,
        host_instance_id: str | None = None,
        host_version: str | None = None,
    ) -> dict:
        task_scope = str(task_scope or "host_run").strip()
        if task_scope not in {"host_run", "user_turn", "independent"}:
            raise ValueError("task_scope is invalid")
        try:
            endpoint = self.tasks.endpoint_for_key(
                user_subject=user_subject,
                agent_host=agent_host,
                endpoint_key=endpoint_key,
            )
        except TaskNotFound:
            endpoint, endpoint_reused = self.tasks.ensure_endpoint(
                user_subject=user_subject,
                token_id=token_id,
                agent_host=agent_host,
                endpoint_key=endpoint_key,
                client_type=client_type,
                external_subject=external_subject,
                account_id=account_id,
                conversation_ref=conversation_ref,
                label=label,
                route=route,
                capabilities=capabilities,
            )
        else:
            endpoint_reused = True
            if endpoint["client_type"] == "web":
                if (
                    client_type != "web"
                    or endpoint["conversation_ref"] != conversation_ref
                ):
                    raise TaskIntegrityError(
                        "workspace task context does not match its registered endpoint"
                    )
            else:
                endpoint, endpoint_reused = self.tasks.ensure_endpoint(
                    user_subject=user_subject,
                    token_id=token_id,
                    agent_host=agent_host,
                    endpoint_key=endpoint_key,
                    client_type=client_type,
                    external_subject=external_subject,
                    account_id=account_id,
                    conversation_ref=conversation_ref,
                    label=label,
                    route=route,
                    capabilities=capabilities,
                )
        workspace_query_group = None
        if endpoint["client_type"] == "web":
            turn = self.workspace.resolve_gateway_turn(
                user_subject=user_subject,
                endpoint_key=endpoint_key,
                session_key=conversation_ref,
            )
            if turn is not None:
                canonical_key = (
                    f"{conversation_ref}|workspace:{turn['turn_ref']}"
                )
                if len(canonical_key) > 1024:
                    canonical_key = (
                        "workspace:"
                        + hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
                    )
                planning_task_key = canonical_key
                if tool_name in {"database_capabilities", "database_execute"}:
                    workspace_query_group = {
                        "turnRef": turn["turn_ref"],
                        "toolName": tool_name,
                    }
                if task_scope == "user_turn":
                    host_task_key = canonical_key
        capability_name = planning_capability_for_tool(tool_name)
        plan_upgrade = tool_name == "agentbridge_task_plan_prepare" and task_scope != "independent"
        if capability_name or plan_upgrade:
            source_task = self.tasks.find_host_task(
                user_subject=user_subject, agent_host=agent_host,
                host_task_key=planning_task_key or host_task_key,
            )
            control = self.planning_gate_for_call(
                user_subject=user_subject,
                task_id=source_task["task_id"] if source_task else None,
                capability_name=capability_name, host_type=agent_host,
            ) if capability_name else None
            if (plan_upgrade and source_task and self._task_has_business_source(
                    user_subject=user_subject, task_id=source_task["task_id"])):
                control = self._planning_control("本轮已完成业务读取，请使用独立修复计划继续，不能复用已终结的原子任务。")
            if control:
                return {"protocolVersion": "0.1", "taskId": source_task["task_id"], **control}
        task, task_reused = self.tasks.ensure_task(
            user_subject=user_subject,
            agent_host=agent_host,
            host_task_key=host_task_key,
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref=conversation_ref,
            title=title,
            summary={"workspaceQueryGroup": workspace_query_group}
            if workspace_query_group else None,
        )
        coordinator_lease = None
        if host_instance_id and host_version:
            self.host_contract.require_registration(
                user_subject=user_subject,
                agent_host=agent_host,
                host_instance_id=host_instance_id,
                host_version=host_version,
                minimum_level="L3",
            )
            coordinator_lease = self.host_contract.acquire_coordinator_lease(
                task_id=task["task_id"],
                user_subject=user_subject,
                host_instance_id=host_instance_id,
                agent_host=agent_host,
            )
        trace, _trace_reused = self.runtime_governance.ensure_trace(
            user_subject=user_subject,
            task_id=task["task_id"],
            origin_endpoint_id=endpoint["endpoint_id"],
            host_type=agent_host,
            host_instance_id=host_instance_id or f"{agent_host}:default",
            host_run_id=host_task_key,
            request_kind="interaction",
        )
        self.runtime_governance.record_stage_once(
            trace_id=trace["trace_id"],
            stage="host.accepted",
            side_effect_boundary="B0_NO_EFFECT",
            metadata={
                "clientType": client_type,
                "taskScope": task_scope,
                "reused": task_reused,
            },
        )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "task": task_response(task),
            "endpoint": endpoint_response(endpoint),
            "reused": {
                "task": task_reused,
                "endpoint": endpoint_reused,
            },
            "coordinatorLease": coordinator_lease,
        }

    def negotiate_host(
        self,
        *,
        user_subject: str,
        token_id: str,
        profile: dict,
    ) -> dict:
        negotiation = self.host_contract.negotiate(
            user_subject=user_subject,
            token_id=token_id,
            profile=profile,
        )
        self.runtime_governance.record_signal(
            signal_type="host.contract.negotiated",
            source="agent_host_contract",
            status=(
                "healthy"
                if negotiation["compatibilityStatus"] == "approved"
                else "degraded"
            ),
            user_subject=user_subject,
            host_type=negotiation["implementation"]["name"],
            value={
                "hostInstanceId": negotiation["hostInstanceId"],
                "hostVersion": negotiation["implementation"]["version"],
                "acceptedLevel": negotiation["acceptedLevel"],
                "compatibilityStatus": negotiation["compatibilityStatus"],
            },
        )
        return negotiation

    def require_host_registration(
        self,
        *,
        user_subject: str,
        agent_host: str,
        host_instance_id: str,
        host_version: str,
        minimum_level: str = "L1",
    ) -> dict:
        return self.host_contract.require_registration(
            user_subject=user_subject,
            agent_host=agent_host,
            host_instance_id=host_instance_id,
            host_version=host_version,
            minimum_level=minimum_level,
        )

    def record_host_runtime_snapshot(
        self,
        *,
        user_subject: str,
        registration: dict,
        snapshot: dict,
    ) -> dict:
        result = self.host_contract.record_runtime_snapshot(
            user_subject=user_subject,
            registration=registration,
            snapshot=snapshot,
        )
        self.runtime_governance.record_signal(
            signal_type="host.runtime.snapshot",
            source="agent_host_contract",
            status=str(snapshot.get("status") or "failed"),
            user_subject=user_subject,
            host_type=registration["agentHost"],
            value={
                "hostInstanceId": registration["hostInstanceId"],
                "hostVersion": registration["hostVersion"],
                "snapshotId": result["snapshotId"],
            },
        )
        return result

    def acquire_host_coordinator_lease(
        self,
        *,
        user_subject: str,
        task_id: str,
        registration: dict,
        lease_seconds: int = 60,
        takeover: bool = False,
        expected_version: int | None = None,
    ) -> dict:
        self.tasks.get_task(task_id, user_subject=user_subject)
        return self.host_contract.acquire_coordinator_lease(
            task_id=task_id,
            user_subject=user_subject,
            host_instance_id=registration["hostInstanceId"],
            agent_host=registration["agentHost"],
            lease_seconds=lease_seconds,
            takeover=takeover,
            expected_version=expected_version,
        )

    def assert_host_coordinator_lease(
        self,
        *,
        user_subject: str,
        task_id: str,
        registration: dict,
        expected_version: int | None = None,
    ) -> dict:
        self.tasks.get_task(task_id, user_subject=user_subject)
        return self.host_contract.assert_coordinator_lease(
            task_id=task_id,
            user_subject=user_subject,
            host_instance_id=registration["hostInstanceId"],
            expected_version=expected_version,
        )

    def release_host_coordinator_lease(
        self,
        *,
        user_subject: str,
        task_id: str,
        registration: dict,
        expected_version: int | None = None,
    ) -> dict:
        self.tasks.get_task(task_id, user_subject=user_subject)
        return self.host_contract.release_coordinator_lease(
            task_id=task_id,
            user_subject=user_subject,
            host_instance_id=registration["hostInstanceId"],
            expected_version=expected_version,
        )

    def get_host_coordinator_lease(
        self,
        *,
        user_subject: str,
        task_id: str,
    ) -> dict | None:
        self.tasks.get_task(task_id, user_subject=user_subject)
        return self.host_contract.get_coordinator_lease(
            task_id=task_id,
            user_subject=user_subject,
        )

    def host_runtime_overview(self) -> dict:
        return self.host_contract.runtime_overview()

    def validate_host_call_context(
        self,
        *,
        user_subject: str,
        registration: dict,
        task_id: str | None = None,
        endpoint_id: str | None = None,
        expected_lease_version: int | None = None,
        require_coordinator_lease: bool = False,
    ) -> dict:
        endpoint = None
        task = None
        if endpoint_id:
            endpoint = self.tasks.get_endpoint(
                endpoint_id,
                user_subject=user_subject,
            )
            if endpoint["agent_host"] != registration["agentHost"]:
                raise TaskIntegrityError(
                    "host call endpoint belongs to another agent host"
                )
        if task_id:
            task = self.tasks.get_task(task_id, user_subject=user_subject)
            if task["agent_host"] != registration["agentHost"]:
                raise TaskIntegrityError(
                    "host call task belongs to another agent host"
                )
            if endpoint is not None:
                endpoint_ids = {
                    task.get("origin_endpoint_id"),
                    task.get("active_endpoint_id"),
                }
                if endpoint["endpoint_id"] not in endpoint_ids:
                    raise TaskIntegrityError(
                        "host call endpoint is not bound to the task"
                    )
            if require_coordinator_lease:
                self.assert_host_coordinator_lease(
                    user_subject=user_subject,
                    task_id=task_id,
                    registration=registration,
                    expected_version=expected_lease_version,
                )
        elif require_coordinator_lease:
            raise TaskIntegrityError(
                "task context is required for coordinator-owned calls"
            )
        return {
            "task": task,
            "endpoint": endpoint,
            "registration": registration,
        }

    def append_host_timeline_message(
        self,
        *,
        user_subject: str,
        token_id: str,
        agent_host: str,
        endpoint_key: str,
        client_type: str,
        external_subject: str,
        conversation_ref: str,
        message_key: str,
        role: str,
        text: str,
        account_id: str | None = None,
        label: str | None = None,
        route: dict | None = None,
        task_id: str | None = None,
    ) -> dict:
        endpoint, endpoint_reused = self.tasks.ensure_endpoint(
            user_subject=user_subject,
            token_id=token_id,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
            client_type=client_type,
            external_subject=external_subject,
            account_id=account_id,
            conversation_ref=conversation_ref,
            label=label,
            route=route,
            capabilities=(
                ["workspace.timeline.read"]
                if client_type in {"web", "webchat"}
                else ["direct_status", "timeline_message"]
            ),
        )
        entry, entry_reused = self.tasks.append_timeline_message(
            user_subject=user_subject,
            source_endpoint_id=endpoint["endpoint_id"],
            message_key=message_key,
            role=role,
            text=text,
            task_id=task_id,
        )
        if task_id:
            trace = self.runtime_governance.trace_for_task(
                task_id,
                user_subject=user_subject,
            )
            if trace is not None:
                stage = (
                    "host.first_progress"
                    if role == "assistant"
                    else "ingress.received"
                )
                self.runtime_governance.record_stage_once(
                    trace_id=trace["trace_id"],
                    stage=stage,
                    metadata={"clientType": client_type, "role": role},
                )
        reactivated_deliveries = 0
        if (
            role == "user"
            and not entry_reused
            and str(endpoint.get("client_type") or "").lower()
            in ACTIVITY_GATED_CLIENT_TYPES
        ):
            reactivated_deliveries = self.tasks.reactivate_deferred_outbox(
                user_subject=user_subject,
                endpoint_id=endpoint["endpoint_id"],
                delay_seconds=5,
            )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "entry": timeline_entry_response(entry),
            "endpoint": endpoint_response(endpoint),
            "reused": {
                "entry": entry_reused,
                "endpoint": endpoint_reused,
            },
            "reactivatedDeliveries": reactivated_deliveries,
        }

    def observe_host_task(
        self,
        *,
        user_subject: str,
        task_id: str,
        operation_ids: list[str] | None = None,
        interaction_ids: list[str] | None = None,
    ) -> dict:
        task = self.tasks.get_task(task_id, user_subject=user_subject)
        linked_operations: list[str] = []
        linked_interactions: list[str] = []
        for operation_id in dict.fromkeys(operation_ids or []):
            operation = self.operations.get(operation_id)
            if operation["user_subject"] != user_subject:
                raise KeyError(f"operation not found: {operation_id}")
            try:
                capability_effect = self.registry.get(
                    operation["capability_name"]
                ).effect
            except KeyError:
                capability_effect = None
            task = self.tasks.link_operation(
                task_id=task_id,
                user_subject=user_subject,
                operation={
                    **operation,
                    "capability_effect": capability_effect,
                },
            )
            linked_operations.append(operation_id)
        for interaction_id in dict.fromkeys(interaction_ids or []):
            record, _resource, interaction = self._load_interaction(
                user_subject=user_subject,
                interaction_id=interaction_id,
            )
            task = self.tasks.link_interaction(
                task_id=task_id,
                user_subject=user_subject,
                interaction_record=record,
                interaction=interaction,
            )
            linked_interactions.append(interaction_id)
            trace = self.runtime_governance.trace_for_task(
                task_id,
                user_subject=user_subject,
            )
            if trace is not None:
                self.runtime_governance.observe_interaction(
                    trace_id=trace["trace_id"],
                    interaction_id=interaction_id,
                    interaction_type=str(interaction.get("type") or "unknown"),
                    state=str(interaction.get("state") or "pending"),
                    system_id=record.get("system_id"),
                )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "task": task_response(task),
            "linked": {
                "operationIds": linked_operations,
                "interactionIds": linked_interactions,
            },
        }

    def fail_host_task(
        self,
        *,
        user_subject: str,
        task_id: str,
        error_code: str,
        message: str,
        causation_ref: str | None = None,
    ) -> dict:
        task = self.tasks.fail_task(
            task_id=task_id,
            user_subject=user_subject,
            error_code=error_code,
            message=message,
            causation_ref=causation_ref,
        )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "task": task_response(task),
        }

    def finish_host_task(
        self,
        *,
        user_subject: str,
        task_id: str,
        outcome: str,
        reason: str | None = None,
        error_code: str | None = None,
        message: str | None = None,
        causation_ref: str | None = None,
    ) -> dict:
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome == "succeeded":
            task = self.tasks.complete_task(
                task_id=task_id,
                user_subject=user_subject,
                reason=reason or "host_tool_completed_without_follow_up",
                causation_ref=causation_ref,
            )
        elif normalized_outcome == "failed":
            task = self.tasks.fail_task(
                task_id=task_id,
                user_subject=user_subject,
                error_code=error_code or "HOST_TOOL_FAILED",
                message=(
                    message
                    or "The host tool failed before producing a resumable result."
                ),
                causation_ref=causation_ref,
            )
        else:
            raise ValueError("outcome must be succeeded or failed")
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "task": task_response(task),
        }

    def recover_host_tasks(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        limit: int = 100,
        include_user_endpoints: bool = False,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        endpoints = [endpoint]
        if include_user_endpoints:
            endpoints = [
                candidate
                for candidate in self.tasks.list_endpoints(
                    user_subject=user_subject,
                    active_only=True,
                    limit=500,
                )
                if candidate.get("agent_host") == agent_host
            ]
        recoveries = []
        recovered_task_ids = set()
        for recovery_endpoint in endpoints:
            for candidate in self.tasks.recovery_candidates(
                user_subject=user_subject,
                endpoint_id=recovery_endpoint["endpoint_id"],
                limit=limit,
            ):
                task_id = candidate["task"]["task_id"]
                if task_id in recovered_task_ids:
                    continue
                try:
                    record, _resource, interaction = self._load_interaction(
                        user_subject=user_subject,
                        interaction_id=candidate["interaction_id"],
                    )
                except (KeyError, InteractionIntegrityError):
                    continue
                resumable_completed = (
                    interaction["state"] == "completed"
                    and interaction.get("resume", {}).get("ready") is True
                    and interaction.get("resume", {}).get("completed") is not True
                )
                if (
                    interaction["state"] not in {"pending", "processing"}
                    and not resumable_completed
                ):
                    if interaction["state"] in {
                        "declined",
                        "expired",
                        "failed",
                        "superseded",
                    }:
                        self.tasks.link_interaction(
                            task_id=task_id,
                            user_subject=user_subject,
                            interaction_record=record,
                            interaction=interaction,
                        )
                    continue
                recoveries.append(
                    {
                        "task": task_response(candidate["task"]),
                        "endpoint": endpoint_response(candidate["endpoint"]),
                        "interaction": {
                            **interaction,
                            "taskId": task_id,
                        },
                    }
                )
                recovered_task_ids.add(task_id)
                if len(recoveries) >= limit:
                    break
            if len(recoveries) >= limit:
                break
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "count": len(recoveries),
            "recoveries": recoveries,
        }

    def list_host_tasks(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        active_only: bool = False,
        limit: int = 100,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        tasks = self.tasks.list_tasks(
            user_subject=user_subject,
            endpoint_id=endpoint["endpoint_id"],
            active_only=active_only,
            limit=limit,
        )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "count": len(tasks),
            "endpoint": endpoint_response(endpoint),
            "tasks": [task_response(task) for task in tasks],
        }

    def require_task_result_access(self, *, user_subject: str, task_id: str) -> None:
        plan = self.task_plans.get_for_task(parent_task_id=task_id, user_subject=user_subject)
        transform_operations = {}
        if plan:
            # Transforms inherit access from the owned plan's business sources.
            # A name prefix alone must never exempt an operation from authorization.
            self.get_task_plan(user_subject=user_subject, plan_id=plan["plan_id"])
            transform_operations = {
                step["operation_id"]: f"transform.{step['transform_name']}"
                for step in plan["steps"]
                if step["kind"] == "transform" and step.get("operation_id")
            }
        for operation_id in self.tasks.operation_ids_for_task(task_id=task_id, user_subject=user_subject):
            operation = self.operations.get(operation_id)
            if transform_operations.get(operation_id) == operation["capability_name"]:
                self.transforms.get(operation["capability_name"].removeprefix("transform."))
                continue
            self.user_grants.require_capability(user_subject, operation["capability_name"], operation.get("input_summary"))
            if operation["capability_name"] == "oa.template.list" and isinstance(operation.get("result"), dict):
                visible = self.user_grants.filter_templates(user_subject, operation["result"])
                if visible.get("items") != operation["result"].get("items", []):
                    raise PermissionError("Historical task contains templates outside current permissions")

    def get_host_task_snapshot(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        task_id: str,
        event_limit: int = 100,
        artifact_limit: int = 20,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        task = self.tasks.get_task(task_id, user_subject=user_subject)
        if task["agent_host"] != agent_host:
            raise TaskIntegrityError("task belongs to another agent host")
        self.require_task_result_access(user_subject=user_subject, task_id=task_id)
        events = self.tasks.list_events(
            task_id=task_id,
            user_subject=user_subject,
            limit=event_limit,
        )
        artifacts = self.tasks.list_artifacts(
            task_id=task_id,
            user_subject=user_subject,
            limit=artifact_limit,
        )
        interaction = None
        interaction_id = task.get("current_interaction_id")
        if interaction_id:
            try:
                interaction = self.present_interaction(
                    user_subject=user_subject,
                    agent_host=agent_host,
                    endpoint_key=endpoint_key,
                    interaction_id=interaction_id,
                ).get("interaction")
            except (KeyError, RuntimeError, InteractionIntegrityError):
                interaction = None
        plan = self.task_plans.get_for_task(
            parent_task_id=task_id,
            user_subject=user_subject,
        )
        return {
            "protocolVersion": "0.1",
            "schemaVersion": "agentbridge.host-task-snapshot.v1",
            "status": "succeeded",
            "task": task_response(task),
            "plan": task_plan_response(plan) if plan is not None else None,
            "endpoint": endpoint_response(endpoint),
            "events": [
                {
                    "schema": "agentbridge.timeline-event.v1",
                    "eventId": event["event_id"],
                    "taskId": event["task_id"],
                    "sequence": int(event.get("sequence") or 0),
                    "kind": event["event_type"],
                    "payload": event.get("payload") or {},
                    "causationRef": event.get("causation_ref"),
                    "createdAt": event["created_at"],
                }
                for event in events
            ],
            "interaction": interaction,
            "artifacts": [
                {
                    "schema": "agentbridge.artifact.v1",
                    "artifactId": artifact["artifact_id"],
                    "taskId": artifact["task_id"],
                    "fileName": artifact["filename"],
                    "mediaType": artifact["content_type"],
                    "size": artifact["byte_size"],
                    "status": artifact["state"].upper(),
                    "expiresAt": artifact["expires_at"],
                    "downloadUrl": artifact["download_url"],
                    "regenerable": artifact["artifact_type"]
                    == "certificate_scan",
                }
                for artifact in artifacts
            ],
        }

    def resolve_host_task_continuation(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        task_id: str | None = None,
        ordinal: int | None = None,
        source_client_type: str | None = None,
        cross_endpoint_only: bool = False,
        prefer_active: bool = True,
        prefer_latest: bool = False,
        reuse_selected: bool = True,
        allow_follow_up: bool = False,
        max_age_minutes: int = 1_440,
        limit: int = 8,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        selected_task_id = str(task_id or "").strip() or None
        selection_reason = "explicit_task_id" if selected_task_id else None

        if selected_task_id is None and ordinal is not None:
            pending = self.tasks.get_continuation(
                user_subject=user_subject,
                agent_host=agent_host,
                endpoint_id=endpoint["endpoint_id"],
            )
            candidates = (
                pending.get("candidate_task_ids", [])
                if pending and pending.get("state") == "awaiting_selection"
                else []
            )
            index = int(ordinal) - 1
            if index < 0 or index >= len(candidates):
                return {
                    "protocolVersion": "0.2",
                    "status": "not_found",
                    "reason": "continuation_choice_not_found",
                    "count": 0,
                    "candidates": [],
                }
            selected_task_id = candidates[index]
            selection_reason = "candidate_ordinal"

        if (
            selected_task_id is None
            and reuse_selected
            and not prefer_latest
            and source_client_type is None
        ):
            current = self.tasks.get_continuation(
                user_subject=user_subject,
                agent_host=agent_host,
                endpoint_id=endpoint["endpoint_id"],
            )
            if current and current.get("state") == "selected":
                selected_task_id = current.get("selected_task_id")
                selection_reason = "existing_selection"

        if selected_task_id is None:
            candidate_records = self.tasks.continuation_candidates(
                user_subject=user_subject,
                agent_host=agent_host,
                endpoint_id=endpoint["endpoint_id"],
                active_only=prefer_active,
                cross_endpoint_only=cross_endpoint_only,
                source_client_type=source_client_type,
                max_age_minutes=max_age_minutes,
                limit=limit,
            )
            if prefer_active and not candidate_records:
                candidate_records = self.tasks.continuation_candidates(
                    user_subject=user_subject,
                    agent_host=agent_host,
                    endpoint_id=endpoint["endpoint_id"],
                    active_only=False,
                    cross_endpoint_only=cross_endpoint_only,
                    source_client_type=source_client_type,
                    max_age_minutes=max_age_minutes,
                    limit=limit,
                )
            if not candidate_records:
                return {
                    "protocolVersion": "0.2",
                    "status": "not_found",
                    "reason": "continuation_task_not_found",
                    "count": 0,
                    "candidates": [],
                }
            if len(candidate_records) > 1 and not prefer_latest:
                continuation, reused = self.tasks.set_continuation_candidates(
                    user_subject=user_subject,
                    agent_host=agent_host,
                    endpoint_id=endpoint["endpoint_id"],
                    candidate_task_ids=[
                        item["task"]["task_id"] for item in candidate_records
                    ],
                    reason="multiple_candidates",
                )
                return {
                    "protocolVersion": "0.2",
                    "status": "ambiguous",
                    "reason": "multiple_continuation_tasks",
                    "count": len(candidate_records),
                    "continuation": continuation_response(continuation),
                    "candidates": [
                        task_continuation_candidate_response(item, index + 1)
                        for index, item in enumerate(candidate_records)
                    ],
                    "reused": reused,
                }
            selected_task_id = candidate_records[0]["task"]["task_id"]
            selection_reason = (
                "latest_relative_reference"
                if len(candidate_records) > 1
                else "single_candidate"
            )

        task = self._refresh_host_task_for_continuation(
            user_subject=user_subject,
            task_id=selected_task_id,
        )
        execution_mode = task_continuation_execution_mode(
            task,
            allow_follow_up=allow_follow_up,
        )
        continuation, task, selected_endpoint, reused = (
            self.tasks.select_continuation(
                user_subject=user_subject,
                agent_host=agent_host,
                endpoint_id=endpoint["endpoint_id"],
                task_id=selected_task_id,
                execution_mode=execution_mode,
                reason=selection_reason,
            )
        )
        snapshot = self._host_task_continuation_snapshot(
            user_subject=user_subject,
            task=task,
            endpoint=selected_endpoint,
        )
        presented_interaction = snapshot.pop("interaction", None)
        return {
            "protocolVersion": "0.2",
            "status": "selected",
            "count": 1,
            "task": task_response(task),
            "continuation": continuation_response(continuation),
            "snapshot": snapshot,
            "interaction": presented_interaction,
            "reused": reused,
        }

    def _refresh_host_task_for_continuation(
        self,
        *,
        user_subject: str,
        task_id: str,
    ) -> dict:
        task = self.tasks.get_task(task_id, user_subject=user_subject)
        operation_ids = (
            [task["current_operation_id"]]
            if task.get("current_operation_id")
            else []
        )
        interaction_ids = (
            [task["current_interaction_id"]]
            if task.get("current_interaction_id")
            else []
        )
        if operation_ids or interaction_ids:
            refreshed = self.observe_host_task(
                user_subject=user_subject,
                task_id=task_id,
                operation_ids=operation_ids,
                interaction_ids=interaction_ids,
            )
            return self.tasks.get_task(
                refreshed["task"]["taskId"],
                user_subject=user_subject,
            )
        return task

    def _host_task_continuation_snapshot(
        self,
        *,
        user_subject: str,
        task: dict,
        endpoint: dict,
    ) -> dict:
        origin = next(
            (
                item
                for item in self.tasks.list_endpoints(
                    user_subject=user_subject,
                    active_only=False,
                    limit=500,
                )
                if item["endpoint_id"] == task["origin_endpoint_id"]
            ),
            None,
        )
        operation = None
        if task.get("current_operation_id"):
            try:
                raw_operation = self.operations.get(
                    task["current_operation_id"]
                )
            except KeyError:
                raw_operation = None
            if raw_operation and raw_operation.get("user_subject") == user_subject:
                operation = {
                    "operationId": raw_operation["operation_id"],
                    "capability": raw_operation.get("capability_name"),
                    "status": raw_operation.get("status"),
                    "errorCode": raw_operation.get("error_code"),
                    "createdAt": raw_operation.get("created_at"),
                    "updatedAt": raw_operation.get("updated_at"),
                    "finishedAt": raw_operation.get("finished_at"),
                }
        interaction_summary = None
        presented_interaction = None
        if task.get("current_interaction_id"):
            try:
                _record, _resource, interaction = self._load_interaction(
                    user_subject=user_subject,
                    interaction_id=task["current_interaction_id"],
                )
            except (KeyError, InteractionIntegrityError):
                interaction = None
            if interaction:
                interaction_summary = {
                    "interactionId": interaction.get("interactionId"),
                    "type": interaction.get("type"),
                    "state": interaction.get("state"),
                    "systemId": interaction.get("systemId"),
                    "expiresAt": interaction.get("expiresAt"),
                }
                if interaction.get("state") in {"pending", "processing"}:
                    try:
                        presented = self.present_interaction(
                            user_subject=user_subject,
                            agent_host=endpoint["agent_host"],
                            endpoint_key=endpoint["endpoint_key"],
                            interaction_id=task["current_interaction_id"],
                        )
                    except (KeyError, RuntimeError):
                        presented = None
                    if presented:
                        presented_interaction = presented.get("interaction")
        events = self.tasks.list_events(
            task_id=task["task_id"],
            user_subject=user_subject,
            limit=12,
        )
        return {
            "summary": {
                "taskId": task["task_id"],
                "title": task["title"],
                "status": task["status"],
                "phase": task_continuation_phase(task),
                "origin": (
                    {
                        "clientType": origin.get("client_type"),
                        "label": origin.get("label"),
                    }
                    if origin
                    else None
                ),
                "operation": operation,
                "interaction": interaction_summary,
                "updatedAt": task["updated_at"],
                "finishedAt": task.get("finished_at"),
            },
            "recentEvents": [
                {
                    "eventType": event["event_type"],
                    "createdAt": event["created_at"],
                }
                for event in events[-8:]
            ],
            "interaction": presented_interaction,
        }

    def get_host_cross_endpoint_context(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        max_age_minutes: int = 360,
        limit: int = 12,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        max_age_minutes = min(max(int(max_age_minutes), 1), 1_440)
        limit = min(max(int(limit), 1), 20)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        endpoints = {
            item["endpoint_id"]: item
            for item in self.tasks.list_endpoints(
                user_subject=user_subject,
                active_only=False,
                limit=500,
            )
        }
        selected = []
        for entry in reversed(
            self.tasks.list_timeline(
                user_subject=user_subject,
                limit=min(500, max(limit * 10, 100)),
            )
        ):
            if entry.get("entry_type") != "chat_message":
                continue
            if entry.get("source_endpoint_id") == endpoint["endpoint_id"]:
                continue
            created_at = _parse_utc(entry.get("created_at"))
            if created_at is None or created_at < cutoff:
                continue
            role = str(entry.get("role") or "").strip()
            text = str(entry.get("text") or "").strip()
            if role not in {"user", "assistant"} or not text:
                continue
            source = endpoints.get(entry.get("source_endpoint_id"))
            selected.append(
                {
                    **timeline_entry_response(entry),
                    "source": {
                        "clientType": (
                            source.get("client_type") if source else "unknown"
                        ),
                        "label": source.get("label") if source else None,
                    },
                }
            )
            if len(selected) >= limit:
                break
        selected.reverse()
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "endpoint": {
                "clientType": endpoint["client_type"],
                "label": endpoint.get("label"),
            },
            "maxAgeMinutes": max_age_minutes,
            "count": len(selected),
            "entries": selected,
        }

    def confirm_workspace_link(
        self,
        *,
        user_subject: str,
        token_id: str,
        agent_host: str,
        endpoint_key: str,
        client_type: str,
        external_subject: str,
        conversation_ref: str,
        link_code: str,
        account_id: str | None = None,
        label: str | None = None,
        route: dict | None = None,
    ) -> dict:
        endpoint, _reused = self.tasks.ensure_endpoint(
            user_subject=user_subject,
            token_id=token_id,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
            client_type=client_type,
            external_subject=external_subject,
            account_id=account_id,
            conversation_ref=conversation_ref,
            label=label,
            route=route,
            capabilities=["workspace.link.confirm"],
        )
        link = self.workspace.confirm_link(
            link_code=link_code,
            user_subject=user_subject,
            approver_endpoint_id=endpoint["endpoint_id"],
        )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "link": {
                "challengeId": link["challenge_id"],
                "state": link["state"],
                "expiresAt": link["expires_at"],
            },
        }

    def register_workspace_endpoint(self, *, account_id: str) -> dict:
        account = self.workspace.get_account(account_id)
        endpoint, reused = self.tasks.ensure_endpoint(
            user_subject=account["user_subject"],
            token_id=f"workspace-account:{account_id}",
            agent_host="openclaw",
            endpoint_key=account["endpoint_key"],
            client_type="web",
            external_subject=account_id,
            account_id=account_id,
            conversation_ref=account["openclaw_session_key"],
            label=f"Agent Workspace: {account['username']}",
            route={},
            capabilities=[
                "workspace.chat",
                "workspace.task.read",
                "workspace.interaction.open",
            ],
        )
        account = self.workspace.attach_endpoint(
            account_id=account_id,
            endpoint_id=endpoint["endpoint_id"],
        )
        return {
            "account": account,
            "endpoint": endpoint,
            "reused": reused,
        }

    def redeem_workspace_gateway_grant(
        self,
        *,
        user_subject: str,
        agent_host: str,
        endpoint_key: str,
        session_key: str,
        grant: str,
        turn_ref: str | None = None,
    ) -> dict:
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=endpoint_key,
        )
        if endpoint["client_type"] != "web":
            raise PermissionError(
                "workspace gateway binding requires a web endpoint"
            )
        redeemed = self.workspace.redeem_gateway_grant(
            grant=grant,
            user_subject=user_subject,
            endpoint_key=endpoint_key,
            session_key=session_key,
            turn_ref=turn_ref,
        )
        binding = {
            "endpointKey": redeemed["endpoint_key"],
            "sessionKey": redeemed["session_key"],
        }
        if redeemed["turn_ref"] is not None:
            binding["turnRef"] = redeemed["turn_ref"]
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "binding": binding,
        }

    def resolve_workspace_gateway_session(
        self,
        *,
        user_subject: str,
        agent_host: str,
        session_key: str,
    ) -> dict:
        account = self.workspace.resolve_gateway_session(
            user_subject=user_subject,
            session_key=session_key,
        )
        endpoint = self.tasks.endpoint_for_key(
            user_subject=user_subject,
            agent_host=agent_host,
            endpoint_key=account["endpoint_key"],
        )
        if (
            endpoint["client_type"] != "web"
            or endpoint["endpoint_id"] != account["endpoint_id"]
        ):
            raise PermissionError(
                "workspace gateway session is not linked to an active web endpoint"
            )
        return {
            "protocolVersion": "0.1",
            "status": "succeeded",
            "binding": {
                "endpointKey": account["endpoint_key"],
                "sessionKey": account["openclaw_session_key"],
            },
        }

    def interaction_required_scopes(
        self,
        *,
        user_subject: str,
        interaction_id: str,
    ) -> frozenset[str]:
        record = self.interactions.get(
            interaction_id,
            user_subject=user_subject,
        )
        resume_spec = record.get("resume_spec")
        if not isinstance(resume_spec, dict) or resume_spec.get("kind") != "capability":
            scopes = frozenset({f"{record['system_id']}:read"})
            if isinstance(resume_spec, dict) and resume_spec.get("kind") == "session_ready":
                task_id = self.tasks.task_id_for_interaction(interaction_id, user_subject=user_subject)
                preceding = self.tasks.operation_before_interaction(
                    task_id=task_id, user_subject=user_subject, interaction_id=interaction_id,
                ) if task_id else None
                if preceding is not None:
                    operation = self.operations.get(preceding["operation_id"])
                    spec = self.registry.get(operation["capability_name"])
                    if spec.effect == "read" and spec.system == record["system_id"]:
                        scopes |= self.planning_required_scopes(spec.name)
            return scopes
        capability_name = str(resume_spec.get("capability") or "")
        try:
            spec = self.registry.get(capability_name)
        except KeyError as exc:
            raise InteractionIntegrityError(
                "interaction resume capability is not registered"
            ) from exc
        if spec.effect == "read":
            return self.planning_required_scopes(spec.name)
        try:
            return capability_required_scopes(capability_name)
        except KeyError as exc:
            raise InteractionIntegrityError(str(exc)) from exc

    def resume_interaction(
        self,
        *,
        user_subject: str,
        interaction_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        record, resource, interaction = self._load_interaction(
            user_subject=user_subject,
            interaction_id=interaction_id,
        )
        if not resource.get("commit_operation_id") and not resource.get("consume_operation_id"):
            self.validate_skill_task(user_subject, self.tasks.task_id_for_interaction(interaction_id, user_subject=user_subject))
        plan_binding = self.task_plans.step_for_interaction(
            interaction_id,
            user_subject=user_subject,
        )
        if plan_binding is not None:
            bound_plan, _bound_step = plan_binding
            if bound_plan["state"] not in ACTIVE_PLAN_STATES:
                return {
                    "protocolVersion": "0.1",
                    "status": bound_plan["state"],
                    "interaction": interaction,
                    "plan": task_plan_response(bound_plan),
                    "resumedFromInteractionId": interaction_id,
                    "taskId": bound_plan["parent_task_id"],
                }
            if not bound_plan.get("commit_operation_id"):
                try:
                    self.validate_task_plan_execution(bound_plan)
                except PlanValidationError as exc:
                    return self.task_plan_runtime.reject(bound_plan, exc)
        if interaction["state"] in {
            "declined",
            "expired",
            "failed",
            "superseded",
        }:
            if plan_binding is not None:
                plan_response = self.task_plan_runtime.handle_terminal_interaction(
                    user_subject=user_subject,
                    interaction_id=interaction_id,
                    interaction_state=interaction["state"],
                )
                if plan_response is not None:
                    return {
                        **plan_response,
                        "interaction": interaction,
                        "resumedFromInteractionId": interaction_id,
                        "taskId": bound_plan["parent_task_id"],
                    }
            return _interaction_not_ready_response(interaction)
        if interaction["resume"]["completed"]:
            if plan_binding is not None:
                return {
                    "protocolVersion": "0.1",
                    "status": "already_resumed",
                    "interaction": interaction,
                    "plan": task_plan_response(bound_plan),
                    "resumedFromInteractionId": interaction_id,
                    "taskId": bound_plan["parent_task_id"],
                }
            operation_id = resource.get("consume_operation_id") or resource.get(
                "commit_operation_id"
            )
            operation = self.operations.get(operation_id) if operation_id else None
            if operation and resource.get("commit_operation_id") and operation["status"] == "succeeded":
                batch_task_id = self.tasks.task_id_for_operation(operation_id, user_subject=user_subject)
                if batch_task_id and self.tasks.get_batch_for_task(parent_task_id=batch_task_id, user_subject=user_subject):
                    recovered = self._apply_batch_operation_response(
                        user_subject=user_subject, task_id=batch_task_id,
                        capability_name=operation["capability_name"], response=operation_response(operation),
                    )
                    return {**recovered, "taskId": batch_task_id, "resumedFromInteractionId": interaction_id}
            return {
                "protocolVersion": "0.1",
                "status": "already_resumed",
                "interaction": interaction,
                "operation": operation_response(operation) if operation else None,
            }

        if not interaction["resume"]["ready"]:
            return _interaction_not_ready_response(interaction)

        resume_spec = record["resume_spec"]
        if resume_spec["kind"] == "session_ready":
            session = self.sessions.get(record["session_id"])
            if session["state"] != "active":
                return _interaction_not_ready_response(
                    interaction,
                    code="SESSION_NOT_ACTIVE",
                    message="The authenticated session is no longer active.",
                )
            if plan_binding is not None:
                self.tasks.link_interaction(
                    task_id=bound_plan["parent_task_id"],
                    user_subject=user_subject,
                    interaction_record=record,
                    interaction=interaction,
                )
                plan_response = self.task_plan_runtime.resume_after_session(
                    user_subject=user_subject,
                    interaction_id=interaction_id,
                )
                if plan_response is not None:
                    return {
                        **plan_response,
                        "resumedFromInteractionId": interaction_id,
                        "taskId": bound_plan["parent_task_id"],
                    }
            continued = (
                self._resume_login_read(user_subject=user_subject, record=record)
                if plan_binding is None else None
            )
            if continued is not None:
                return continued
            return {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "interaction": interaction,
                "result": {"session": self._session_response(session)},
                "nextAction": {"type": "retry_original_request"},
            }

        if resume_spec["kind"] != "capability":
            raise InteractionIntegrityError("unsupported interaction resume kind")
        session = self.sessions.get(record["session_id"])
        resume_epoch = session.get("last_verified_at") or session["updated_at"]
        task_id = self.tasks.task_id_for_interaction(
            record["interaction_id"],
            user_subject=user_subject,
        )
        response = self.invoke(
            user_subject=user_subject,
            capability_name=resume_spec["capability"],
            arguments=resume_spec["arguments"],
            idempotency_key=idempotency_key
            or f"interaction-resume:{record['interaction_id']}:{resume_epoch}",
            task_id=task_id,
            host_type="interaction_resume",
            host_run_id=record["interaction_id"],
        )
        if plan_binding is not None:
            self.tasks.link_interaction(
                task_id=bound_plan["parent_task_id"],
                user_subject=user_subject,
                interaction_record=record,
                interaction=interaction,
            )
            plan_response = self.task_plan_runtime.resume_after_capability(
                user_subject=user_subject,
                interaction_id=interaction_id,
                response=response,
            )
            if plan_response is not None:
                return {
                    **plan_response,
                    "resumedFromInteractionId": record["interaction_id"],
                    "taskId": bound_plan["parent_task_id"],
                }
        if task_id:
            operation_id = response.get("operationId")
            next_interaction = response.get("interaction")
            self.observe_host_task(
                user_subject=user_subject,
                task_id=task_id,
                operation_ids=[operation_id] if operation_id else [],
                interaction_ids=(
                    [next_interaction["interactionId"]]
                    if isinstance(next_interaction, dict)
                    and next_interaction.get("interactionId")
                    else []
                ),
            )
        return {
            **response,
            "resumedFromInteractionId": record["interaction_id"],
            "taskId": task_id,
        }

    def _resume_login_read(self, *, user_subject: str, record: dict) -> dict | None:
        return resume_login_read(self, user_subject=user_subject, record=record)

    def _load_interaction(
        self,
        *,
        user_subject: str,
        interaction_id: str,
    ) -> tuple[dict, dict, dict]:
        record = self.interactions.get(
            interaction_id,
            user_subject=user_subject,
        )
        interaction_type = record["interaction_type"]
        if interaction_type == "credential":
            resource = self.challenges.get(self._credential_challenge_id(record))
        elif interaction_type == "business_input":
            resource = self.field_submissions.get(record["resource_id"])
        elif interaction_type == "execution_authorization":
            resource = self.write_authorizations.get(record["resource_id"])
        else:
            raise InteractionIntegrityError("unsupported interaction type")
        if any(
            (
                resource["user_subject"] != record["user_subject"],
                resource["system_id"] != record["system_id"],
                resource["session_id"] != record["session_id"],
            )
        ):
            raise InteractionIntegrityError(
                "interaction binding does not match its trusted resource"
            )
        return record, resource, build_interaction_envelope(record, resource)

    def _present_interaction_for_endpoint(
        self,
        *,
        record: dict,
        resource: dict,
        interaction: dict,
        endpoint: dict,
    ) -> dict:
        if record["interaction_type"] == "business_input":
            presentation = self.field_submissions.create_presentation(
                resource["submission_id"],
                user_subject=record["user_subject"],
                endpoint_id=endpoint["endpoint_id"],
            )
            return {
                **interaction,
                "presentation": {
                    **interaction["presentation"],
                    "url": presentation["card_url"],
                    "endpointId": endpoint["endpoint_id"],
                    "presentationId": presentation["presentation_id"],
                    "individualized": True,
                },
            }
        if record["interaction_type"] != "execution_authorization":
            return {
                **interaction,
                "presentation": {
                    **interaction["presentation"],
                    "endpointId": endpoint["endpoint_id"],
                    "individualized": False,
                },
            }
        presentation = self.write_authorizations.create_presentation(
            resource["authorization_id"],
            user_subject=record["user_subject"],
            endpoint_id=endpoint["endpoint_id"],
        )
        return {
            **interaction,
            "presentation": {
                **interaction["presentation"],
                "url": presentation["card_url"],
                "endpointId": endpoint["endpoint_id"],
                "presentationId": presentation["presentation_id"],
                "individualized": True,
            },
        }

    @staticmethod
    def _credential_challenge_id(record: dict) -> str:
        # Read both legacy records and task-scoped credentials before enabling
        # new writers, so the next release has a compatible rollback target.
        return record["resume_spec"].get("challengeId") or record["resource_id"]

    def _credential_interaction(self, challenge: dict, *, task_id: str | None = None) -> dict:
        record = self.interactions.register(
            interaction_type="credential",
            user_subject=challenge["user_subject"],
            system_id=challenge["system_id"],
            session_id=challenge["session_id"],
            operation_id=None,
            resource_id=(f"{challenge['challenge_id']}:{task_id}" if task_id else challenge["challenge_id"]),
            title=f"登录{challenge['system_name']}",
            message="请在 AgentBridge 安全页面完成登录，凭据不会经过智能体。",
            display={
                "systemName": challenge["system_name"],
                "expectedPrincipalRef": challenge.get("expected_principal_ref"),
            },
            resume_spec={
                "kind": "session_ready",
                "systemId": challenge["system_id"],
                **({"challengeId": challenge["challenge_id"], "taskId": task_id} if task_id else {}),
            },
            created_at=challenge["created_at"],
            expires_at=challenge["expires_at"],
        )
        interaction = build_interaction_envelope(record, challenge)
        if task_id:
            self.tasks.link_interaction(task_id=task_id, user_subject=challenge["user_subject"],
                                        interaction_record=record, interaction=interaction)
        return interaction

    def _controlled_writes(self) -> ControlledWriteExecutor:
        # Bind at each entry so callers replacing a registry/store/callback before
        # invocation use the current collaborators, without rebuilding any stores.
        return ControlledWriteExecutor(ControlledWriteDependencies(
            registry=self.registry,
            tasks=self.tasks,
            field_submissions=self.field_submissions,
            write_authorizations=self.write_authorizations,
            user_grants=self.user_grants,
            task_plans=self.task_plans,
            governance_policies=self.governance_policies,
            trusted_card_base_url=self.trusted_card_base_url,
            business_input_interaction=self._business_input_interaction,
            execution_authorization_interaction=self._execution_authorization_interaction,
            pending_batch_definition=self._pending_batch_definition,
            guard_skill_authorization=self.guard_skill_authorization,
            validate_task_plan_execution=self.validate_task_plan_execution,
        ))

    def _business_input_interaction(self, submission: dict) -> dict:
        schema = submission["form_schema"]
        record = self.interactions.register(
            interaction_type="business_input",
            user_subject=submission["user_subject"],
            system_id=submission["system_id"],
            session_id=submission["session_id"],
            operation_id=submission["create_operation_id"],
            resource_id=submission["submission_id"],
            title=str(schema.get("title") or "填写业务信息"),
            message=str(
                schema.get("notice")
                or "请在 AgentBridge 安全页面填写业务信息。"
            ),
            display={
                "systemName": schema.get("system"),
                "effect": schema.get("effect"),
                "fieldCount": len(schema.get("fields") or []),
            },
            resume_spec={
                "kind": "capability",
                "capability": submission["capability_name"],
                "arguments": {
                    **dict(schema.get("_agentbridge_resume_arguments") or {}),
                    "input_submission_id": submission["submission_id"],
                },
            },
            created_at=submission["created_at"],
            expires_at=submission["expires_at"],
        )
        return build_interaction_envelope(record, submission)

    def _execution_authorization_interaction(self, authorization: dict) -> dict:
        summary = authorization["summary"]
        record = self.interactions.register(
            interaction_type="execution_authorization",
            user_subject=authorization["user_subject"],
            system_id=authorization["system_id"],
            session_id=authorization["session_id"],
            operation_id=authorization["prepare_operation_id"],
            resource_id=authorization["authorization_id"],
            title=str(summary.get("title") or "确认执行计划"),
            message="请核对冻结计划并决定是否允许 AgentBridge 执行。",
            display={
                "systemName": summary.get("system"),
                "effect": summary.get("effect"),
                "fieldCount": len(summary.get("fields") or []),
            },
            resume_spec={
                "kind": "capability",
                "capability": authorization["capability_name"],
                "arguments": {
                    "authorization_id": authorization["authorization_id"],
                },
            },
            created_at=authorization["created_at"],
            expires_at=authorization["expires_at"],
        )
        return build_interaction_envelope(record, authorization)

    @contextmanager
    def authentication_worker(
        self,
        session: dict,
        adapter: object,
    ) -> Iterator[object]:
        runtime = self._runtime_for_system(session["system_id"])
        if runtime is None:
            raise KeyError(
                f"central runtime is not configured for {session['system_id']}"
            )
        registered_adapter, worker_factory = runtime
        if registered_adapter is not adapter:
            raise ValueError("authentication adapter does not match the session system")
        with self._session_lock(session["session_id"]):
            with worker_factory(session, adapter) as worker:
                yield worker

    @contextmanager
    def remote_authentication_worker(
        self,
        session: dict,
        adapter: object,
        cdp_endpoint: str,
    ) -> Iterator[object]:
        runtime = self._runtime_for_system(session["system_id"])
        if runtime is None:
            raise KeyError(
                f"central runtime is not configured for {session['system_id']}"
            )
        registered_adapter, _worker_factory = runtime
        if registered_adapter is not adapter:
            raise ValueError("authentication adapter does not match the session system")
        contract = adapter.authentication_contract()
        allowed_origins = set(contract.get("allowed_origins") or ())
        allowed_origins.add(contract["origin"])
        with self._session_lock(session["session_id"]):
            with AttachedCentralBrowserWorker(
                cdp_endpoint=cdp_endpoint,
                allowed_origins=allowed_origins,
            ) as worker:
                yield worker

    def _invoke_adapter(
        self,
        *args,
        **kwargs,
    ) -> dict:
        context = kwargs.get("context")
        if context is None and args:
            context = args[0]
        trace_id = getattr(context, "trace_id", None)
        if not trace_id:
            return self._invoke_adapter_unobserved(*args, **kwargs)
        capability_name = str(kwargs.get("capability_name") or context.spec.name)
        system_id = str(kwargs.get("system_id") or context.spec.system)
        if context.spec.effect == "read":
            boundary = "B1_READ_ONLY"
        elif capability_name in _TRUSTED_WRITE_COMMITS:
            boundary = "B4_COMMIT_ATTEMPTED"
        else:
            boundary = "B2_INTERACTION_CREATED"
        span = self.runtime_governance.start_span(
            trace_id=trace_id,
            stage="adapter.request",
            operation_id=context.operation_id,
            system_id=system_id,
            capability_name=capability_name,
            side_effect_boundary=boundary,
        )
        try:
            result = self._invoke_adapter_unobserved(*args, **kwargs)
        except Exception as exc:
            failure = classify_runtime_error(exc)
            self.runtime_governance.finish_span(
                span["span_id"],
                status="unknown" if failure["code"] == "RESULT_UNKNOWN" else "failed",
                error_code=failure["code"],
                side_effect_boundary=boundary,
            )
            raise
        else:
            finished_boundary = (
                "B5_VERIFIED"
                if capability_name in _TRUSTED_WRITE_COMMITS
                else boundary
            )
            self.runtime_governance.finish_span(
                span["span_id"],
                status="succeeded",
                side_effect_boundary=finished_boundary,
            )
            return result

    def _invoke_adapter_unobserved(
        self,
        *,
        context: CapabilityContext,
        user_subject: str,
        system_id: str,
        adapter: object,
        worker_factory: WorkerFactory,
        capability_name: str,
        arguments: dict,
        task_id: str | None = None,
    ) -> dict:
        if task_id and (capability_name in _TRUSTED_WRITE_DEFINITIONS or capability_name in _TRUSTED_WRITE_COMMITS):
            parent_plan = self.task_plans.get_for_task(parent_task_id=task_id, user_subject=user_subject)
            if parent_plan is not None:
                try:
                    self.validate_task_plan_execution(parent_plan)
                except PlanValidationError as exc:
                    raise CapabilityRejected(exc.code, exc.message) from exc
        self._controlled_writes().assert_write_allowed(context=context, system_id=system_id)
        session = self.sessions.find(user_subject=user_subject, system_id=system_id)
        if session is None or session["state"] != "active":
            raise login_required_action(user_subject, system_id, session)

        document_search = capability_name == DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY
        with self._session_lock(
            session["session_id"],
            wait_seconds=1.0 if document_search else None,
        ):
            session = self.sessions.get(session["session_id"])
            if session["state"] != "active":
                raise login_required_action(user_subject, system_id, session)
            try:
                state = self.session_states.load(session["session_id"])
            except SessionStateAccessDenied as exc:
                raise _session_runtime_mismatch_action(user_subject, session) from exc
            except SessionSecretError as exc:
                raise _session_state_unavailable_action(user_subject, session) from exc
            if state is None:
                expired_session = self.sessions.mark_expired(
                    session["session_id"],
                    "Encrypted session state is missing.",
                    source=f"capability:{capability_name}",
                )
                raise login_required_action(user_subject, system_id, expired_session)
            prepare_definition = _TRUSTED_WRITE_DEFINITIONS.get(capability_name)
            commit_definition = _TRUSTED_WRITE_COMMITS.get(capability_name)
            field_submission = None
            effective_arguments = arguments
            try:
                if capability_name in {MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY, PENDING_BATCH_PREPARE_CAPABILITY}:
                    with worker_factory(session, adapter) as worker:
                        worker.restore_session_state(state)
                        arguments, empty_batch_result = (
                            self._resolve_approval_batch_context(
                                context=context,
                                session=session,
                                adapter=adapter,
                                worker=worker,
                                arguments=arguments,
                                task_id=task_id,
                            )
                        )
                        state = worker.capture_session_state()
                        self.session_states.save(session["session_id"], state)
                    if empty_batch_result is not None:
                        return empty_batch_result
                    effective_arguments = arguments
                    if capability_name == PENDING_BATCH_PREPARE_CAPABILITY:
                        prepare_definition = self._pending_batch_definition(
                            task_id=task_id, user_subject=user_subject, arguments=arguments,
                        )
                dynamic_field_schema = None
                preflight_result = None
                if (
                    prepare_definition is not None
                    and not str(arguments.get("input_submission_id") or "").strip()
                    and (
                        prepare_definition.get("field_schema_function")
                        or prepare_definition.get("preflight_function")
                    )
                ):
                    with worker_factory(session, adapter) as worker:
                        worker.restore_session_state(state)
                        preflight_function_name = prepare_definition.get(
                            "preflight_function"
                        )
                        if preflight_function_name:
                            preflight_function = resolve_write_function(
                                str(preflight_function_name)
                            )
                            if not callable(preflight_function):
                                raise RuntimeError(
                                    "trusted write preflight function is unavailable"
                                )
                            try:
                                preflight_result = preflight_function(
                                    adapter,
                                    worker,
                                    arguments,
                                    str(prepare_definition["preflight_profile"]),
                                )
                            except prepare_definition["contract_error"] as exc:
                                raise CapabilityRejected(
                                    "WORKFLOW_NOT_SUPPORTED",
                                    str(exc),
                                ) from exc
                        schema_function_name = prepare_definition.get(
                            "field_schema_function"
                        )
                        if schema_function_name:
                            schema_function = resolve_write_function(
                                str(schema_function_name)
                            )
                            if not callable(schema_function):
                                raise RuntimeError(
                                    "trusted field schema function is unavailable"
                                )
                            dynamic_field_schema = schema_function(
                                adapter,
                                worker,
                                arguments,
                            )
                        state = worker.capture_session_state()
                        self.session_states.save(
                            session["session_id"],
                            state,
                        )
                if (
                    isinstance(preflight_result, dict)
                    and isinstance(preflight_result.get("review"), dict)
                ):
                    schema = deepcopy(
                        dynamic_field_schema or prepare_definition["field_schema"]
                    )
                    schema["review"] = deepcopy(preflight_result["review"])
                    review_fingerprint = str(
                        preflight_result.get("review_fingerprint") or ""
                    ).strip()
                    if review_fingerprint:
                        schema["_agentbridge_review_fingerprint"] = review_fingerprint
                    dynamic_field_schema = schema
                if capability_name == PENDING_BATCH_PREPARE_CAPABILITY:
                    schema = deepcopy(dynamic_field_schema or prepare_definition["field_schema"])
                    schema["title"] += f"（第 {arguments['batch_ordinal']}/{arguments['batch_total']} 条）"
                    schema["notice"] = (
                        f"当前事项：{arguments['target_title']} | {arguments['target_sender']} | {arguments['target_date']}。"
                        "本条仍需独立授权；权威核验成功后自动进入下一条。拒绝或失败会停止整个批次。"
                    )
                    dynamic_field_schema = schema
                if prepare_definition is not None:
                    if prepare_definition.get("field_schema") is None:
                        effective_arguments = dict(arguments)
                    else:
                        field_submission, effective_arguments = self._controlled_writes().resolve_field_input(
                            context=context,
                            session=session,
                            arguments=arguments,
                            definition=prepare_definition,
                            form_schema=dynamic_field_schema,
                        )
                with worker_factory(session, adapter) as worker:
                    worker.restore_session_state(state)
                    if prepare_definition is not None:
                        result = self._controlled_writes().prepare(
                            context=context,
                            session=session,
                            adapter=adapter,
                            worker=worker,
                            arguments=effective_arguments,
                            field_submission=field_submission,
                            definition=prepare_definition,
                        )
                    elif commit_definition is not None:
                        prepare_capability, definition = commit_definition
                        result = self._controlled_writes().commit(
                            context=context,
                            session=session,
                            adapter=adapter,
                            worker=worker,
                            arguments=arguments,
                            prepare_capability=prepare_capability,
                            definition=definition,
                        )
                    else:
                        result = adapter.invoke_capability(
                            capability_name,
                            worker,
                            arguments,
                        )
                        if capability_name == "oa.template.list":
                            result = self.user_grants.filter_templates(user_subject, result)
                        if capability_name == DOCUMENT_CERTIFICATE_SEARCH_CAPABILITY:
                            result = self._materialize_document_downloads(
                                session=session,
                                result=result,
                            )
                        elif capability_name == SMARTLIGHT_REPORT_EXPORT_CAPABILITY:
                            result = self._materialize_smartlight_report(
                                session=session,
                                arguments=arguments,
                                report=result,
                                task_id=task_id,
                            )
                        elif capability_name == ADDRESSBOOK_EXPORT_CAPABILITY:
                            result = self._materialize_addressbook_report(
                                session=session,
                                arguments=arguments,
                                report=result,
                                task_id=task_id,
                            )
                    self.session_states.save(
                        session["session_id"],
                        worker.capture_session_state(),
                    )
                    if document_search:
                        self.sessions.touch_activity(session["session_id"])
                    return result
            except AdapterLoginRequired as exc:
                expired_session = self.sessions.mark_expired(
                    session["session_id"],
                    str(exc),
                    source=f"capability:{capability_name}",
                )
                self.session_states.delete(session["session_id"])
                raise login_required_action(user_subject, system_id, expired_session) from exc
            except AdapterSessionCheckUnavailable as exc:
                self.sessions.record_event(
                    session["session_id"],
                    event_type="check_deferred",
                    source=f"capability:{capability_name}",
                    reason=str(exc),
                )
                raise _session_check_unavailable_action(
                    user_subject,
                    session,
                    diagnostics=str(exc),
                ) from exc
            except AdapterBusinessRuleRejected as exc:
                raise CapabilityRejected(
                    exc.error_code,
                    str(exc),
                ) from exc

    def fetch_document_download(self, record: dict) -> dict:
        result = self.fetch_document_downloads([record])[0]
        if isinstance(result, Exception):
            raise result
        return result

    def fetch_document_downloads(
        self,
        records: list[dict],
    ) -> list[dict | Exception]:
        if not records:
            return []
        session = self.sessions.get(records[0]["session_id"])
        for record in records:
            self.document_downloads.require_access(record)
            if any(
                (
                    record["session_id"] != session["session_id"],
                    session["user_subject"] != record["user_subject"],
                    session["system_id"] != record["system_id"],
                    session["state"] != "active",
                )
            ):
                raise ValueError("document download session binding is invalid")
        runtime = self._runtime_for_system(session["system_id"])
        if runtime is None:
            raise ValueError("document download system is not configured")
        adapter, worker_factory = runtime
        with self._session_lock(session["session_id"]):
            state = self.session_states.load(session["session_id"])
            if state is None:
                raise ValueError("document download session state is unavailable")
            try:
                with worker_factory(session, adapter) as worker:
                    worker.restore_session_state(state)
                    fetch_many = getattr(adapter, "fetch_certificate_documents", None)
                    if callable(fetch_many):
                        payloads = fetch_many(
                            worker,
                            [record["document"] for record in records],
                        )
                    else:
                        payloads = []
                        for record in records:
                            try:
                                payloads.append(
                                    adapter.fetch_certificate_document(
                                        worker,
                                        record["document"],
                                    )
                                )
                            except AdapterLoginRequired:
                                raise
                            except Exception as exc:
                                payloads.append(exc)
                    self.session_states.save(
                        session["session_id"],
                        worker.capture_session_state(),
                    )
                    self.sessions.touch_activity(session["session_id"])
                    if len(payloads) != len(records):
                        raise ValueError(
                            "document download batch returned an invalid result count"
                        )
                    for record in records:
                        self.document_downloads.require_access(record)
                    return payloads
            except AdapterLoginRequired:
                self.sessions.mark_expired(
                    session["session_id"],
                    "OA session expired during certificate download.",
                    source="document_download",
                )
                self.session_states.delete(session["session_id"])
                raise

    def _materialize_smartlight_report(
        self,
        *,
        session: dict,
        arguments: dict,
        report: dict,
        task_id: str | None,
    ) -> dict:
        report_type = str(report.get("reportType") or "").strip()
        if report_type != str(arguments.get("report_type") or "").strip():
            raise ValueError("Smartlight report type does not match the request")
        body = render_smartlight_report_csv(report)
        filename = smartlight_report_filename(report)
        download = self.document_downloads.create(
            user_subject=session["user_subject"],
            system_id=session["system_id"],
            session_id=session["session_id"],
            document=smartlight_report_recipe(
                report_type=report_type,
                arguments=arguments,
            ),
            filename=filename,
            document_type=SMARTLIGHT_REPORT_DOCUMENT_TYPE,
            display_size=_display_file_size(len(body)),
            card_base_url=self.trusted_card_base_url,
            ttl_seconds=PREPARED_DOCUMENT_TTL_SECONDS,
        )
        self.document_downloads.claim_for_prepare(
            download["download_id"],
            user_subject=session["user_subject"],
        )
        try:
            ready = self.document_downloads.mark_ready(
                download["download_id"],
                body=body,
                content_type=SMARTLIGHT_REPORT_CONTENT_TYPE,
            )
        except Exception:
            try:
                self.document_downloads.release(download["download_id"])
            except DocumentDownloadStateError:
                pass
            raise

        artifact = None
        artifact_reused = False
        if task_id:
            artifact, artifact_reused = self.tasks.link_artifact(
                task_id=task_id,
                user_subject=session["user_subject"],
                artifact={
                    "artifact_type": SMARTLIGHT_REPORT_ARTIFACT_TYPE,
                    "source_ref": ready["download_id"],
                    "filename": ready["filename"],
                    "content_type": ready["content_type"],
                    "byte_size": ready["prepared_size"],
                    "download_url": f"{ready['card_url']}/file",
                    "expires_at": ready["expires_at"],
                },
            )
            self.tasks.complete_task(
                task_id=task_id,
                user_subject=session["user_subject"],
                reason="smartlight_report_ready",
                causation_ref=ready["download_id"],
            )
        metadata = dict(report.get("metadata") or {})
        return {
            "protocolVersion": "0.1",
            "schemaVersion": "agentbridge.document_delivery.v1",
            "status": "succeeded",
            "report": {
                "reportType": report_type,
                "title": report.get("reportTitle"),
                "rowCount": len(report.get("rows") or []),
                "metadata": metadata,
                "regenerationSemantics": "rerun_current_data_with_original_filters",
            },
            "file": _prepared_document_file(
                ready,
                artifact=artifact,
                artifact_reused=artifact_reused,
            ),
            "hostDelivery": {
                "mode": "direct_attachment",
                "oneFilePerMessage": True,
                "handledByHost": True,
                "state": "prepared",
                "completionMeaning": "file_ready_not_endpoint_acknowledged",
            },
        }

    def _materialize_addressbook_report(
        self,
        *,
        session: dict,
        arguments: dict,
        report: dict,
        task_id: str | None,
    ) -> dict:
        source = str(report.get("reportType") or "").strip()
        if source != str(arguments.get("source") or "").strip():
            raise ValueError("OA address-book report source does not match the request")
        body = render_addressbook_report_csv(report)
        filename = addressbook_report_filename(report)
        download = self.document_downloads.create(
            user_subject=session["user_subject"],
            system_id=session["system_id"],
            session_id=session["session_id"],
            document=addressbook_report_recipe(arguments=arguments),
            filename=filename,
            document_type=ADDRESSBOOK_REPORT_DOCUMENT_TYPE,
            display_size=_display_file_size(len(body)),
            card_base_url=self.trusted_card_base_url,
            ttl_seconds=PREPARED_DOCUMENT_TTL_SECONDS,
        )
        self.document_downloads.claim_for_prepare(
            download["download_id"],
            user_subject=session["user_subject"],
        )
        try:
            ready = self.document_downloads.mark_ready(
                download["download_id"],
                body=body,
                content_type=ADDRESSBOOK_REPORT_CONTENT_TYPE,
            )
        except Exception:
            try:
                self.document_downloads.release(download["download_id"])
            except DocumentDownloadStateError:
                pass
            raise

        artifact = None
        artifact_reused = False
        if task_id:
            artifact, artifact_reused = self.tasks.link_artifact(
                task_id=task_id,
                user_subject=session["user_subject"],
                artifact={
                    "artifact_type": ADDRESSBOOK_REPORT_ARTIFACT_TYPE,
                    "source_ref": ready["download_id"],
                    "filename": ready["filename"],
                    "content_type": ready["content_type"],
                    "byte_size": ready["prepared_size"],
                    "download_url": f"{ready['card_url']}/file",
                    "expires_at": ready["expires_at"],
                },
            )
            self.tasks.complete_task(
                task_id=task_id,
                user_subject=session["user_subject"],
                reason="oa_addressbook_report_ready",
                causation_ref=ready["download_id"],
            )
        metadata = dict(report.get("metadata") or {})
        return {
            "protocolVersion": "0.1",
            "schemaVersion": "agentbridge.document_delivery.v1",
            "status": "succeeded",
            "report": {
                "reportType": source,
                "title": report.get("reportTitle"),
                "rowCount": len(report.get("rows") or []),
                "metadata": metadata,
                "regenerationSemantics": "rerun_current_data_with_original_filters",
            },
            "file": _prepared_document_file(
                ready,
                artifact=artifact,
                artifact_reused=artifact_reused,
            ),
            "hostDelivery": {
                "mode": "direct_attachment",
                "oneFilePerMessage": True,
                "handledByHost": True,
                "state": "prepared",
                "completionMeaning": "file_ready_not_endpoint_acknowledged",
            },
        }

    def prepare_document_download(
        self,
        *,
        user_subject: str,
        download_id: str,
        task_id: str | None = None,
    ) -> dict:
        try:
            existing = self.document_downloads.get(
                download_id,
                include_document=True,
            )
            if existing["user_subject"] != user_subject:
                raise DocumentDownloadAccessDenied(
                    "document download belongs to another user"
                )
            self.document_downloads.require_access(existing)
            if existing["state"] == "ready":
                ready = existing
            else:
                record = self.document_downloads.claim_for_prepare(
                    download_id,
                    user_subject=user_subject,
                )
                try:
                    payload = self.fetch_document_download(record)
                    ready = self.document_downloads.mark_ready(
                        download_id,
                        body=payload.get("body"),
                        content_type=str(payload.get("content_type") or ""),
                    )
                except Exception:
                    try:
                        self.document_downloads.release(download_id)
                    except DocumentDownloadStateError:
                        pass
                    raise
        except Exception as exc:
            error = _document_batch_error(download_id, exc)
            return _document_delivery_failure(
                error["code"],
                retryable=error["retryable"],
            )
        artifact = None
        artifact_reused = False
        if task_id:
            try:
                artifact, artifact_reused = self.tasks.link_artifact(
                    task_id=task_id,
                    user_subject=user_subject,
                    artifact={
                        "artifact_type": "certificate_scan",
                        "source_ref": ready["download_id"],
                        "filename": ready["filename"],
                        "content_type": ready["content_type"],
                        "byte_size": ready["prepared_size"],
                        "download_url": f"{ready['card_url']}/file",
                        "expires_at": ready["expires_at"],
                    },
                )
                self.tasks.complete_task(
                    task_id=task_id,
                    user_subject=user_subject,
                    reason="artifact_ready",
                    causation_ref=ready["download_id"],
                )
            except (KeyError, RuntimeError, ValueError):
                return _document_delivery_failure(
                    "TASK_ARTIFACT_LINK_FAILED",
                    retryable=True,
                )
        return {
            "protocolVersion": "0.1",
            "schemaVersion": "agentbridge.document_delivery.v1",
            "status": "succeeded",
            "file": _prepared_document_file(
                ready,
                artifact=artifact,
                artifact_reused=artifact_reused,
            ),
            "hostDelivery": {
                "mode": "direct_attachment",
                "oneFilePerMessage": True,
                "handledByHost": True,
                "state": "prepared",
                "completionMeaning": "file_ready_not_endpoint_acknowledged",
            },
        }

    def prepare_document_downloads(
        self,
        *,
        user_subject: str,
        download_ids: list[str],
        task_id: str | None = None,
    ) -> dict:
        normalized_ids = _validated_document_download_ids(download_ids)
        ready_by_id: dict[str, dict] = {}
        claimed: list[dict] = []
        errors: list[dict] = []
        for download_id in normalized_ids:
            try:
                existing = self.document_downloads.get(
                    download_id,
                    include_document=True,
                )
                if existing["user_subject"] != user_subject:
                    raise DocumentDownloadAccessDenied(
                        "document download belongs to another user"
                    )
                self.document_downloads.require_access(existing)
                if existing["state"] == "ready":
                    ready_by_id[download_id] = existing
                else:
                    claimed.append(
                        self.document_downloads.claim_for_prepare(
                            download_id,
                            user_subject=user_subject,
                        )
                    )
            except Exception as exc:
                errors.append(_document_batch_error(download_id, exc))

        groups: dict[tuple[str, str], list[dict]] = {}
        for record in claimed:
            groups.setdefault(
                (record["system_id"], record["session_id"]),
                [],
            ).append(record)
        for records in groups.values():
            try:
                payloads = self.fetch_document_downloads(records)
            except Exception as exc:
                payloads = [exc] * len(records)
            for record, payload in zip(records, payloads, strict=True):
                if isinstance(payload, Exception):
                    try:
                        self.document_downloads.release(record["download_id"])
                    except DocumentDownloadStateError:
                        pass
                    errors.append(
                        _document_batch_error(record["download_id"], payload)
                    )
                    continue
                try:
                    ready_by_id[record["download_id"]] = (
                        self.document_downloads.mark_ready(
                            record["download_id"],
                            body=payload.get("body"),
                            content_type=str(payload.get("content_type") or ""),
                        )
                    )
                except Exception as exc:
                    try:
                        self.document_downloads.release(record["download_id"])
                    except DocumentDownloadStateError:
                        pass
                    errors.append(
                        _document_batch_error(record["download_id"], exc)
                    )

        files = []
        for download_id in normalized_ids:
            ready = ready_by_id.get(download_id)
            if ready is None:
                continue
            artifact = None
            artifact_reused = False
            if task_id:
                try:
                    artifact, artifact_reused = self.tasks.link_artifact(
                        task_id=task_id,
                        user_subject=user_subject,
                        artifact={
                            "artifact_type": "certificate_scan",
                            "source_ref": ready["download_id"],
                            "filename": ready["filename"],
                            "content_type": ready["content_type"],
                            "byte_size": ready["prepared_size"],
                            "download_url": f"{ready['card_url']}/file",
                            "expires_at": ready["expires_at"],
                        },
                    )
                except (KeyError, RuntimeError, ValueError) as exc:
                    errors.append(
                        _document_batch_error(
                            download_id,
                            exc,
                            code="TASK_ARTIFACT_LINK_FAILED",
                        )
                    )
                    continue
            files.append(
                _prepared_document_file(
                    ready,
                    artifact=artifact,
                    artifact_reused=artifact_reused,
                )
            )
        if task_id and files:
            try:
                self.tasks.complete_task(
                    task_id=task_id,
                    user_subject=user_subject,
                    reason=(
                        "artifact_batch_ready"
                        if not errors
                        else "artifact_batch_partially_ready"
                    ),
                    causation_ref=files[-1]["downloadId"],
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                return _document_delivery_batch_failure(
                    normalized_ids,
                    _document_batch_error(
                        files[-1]["downloadId"],
                        exc,
                        code="TASK_ARTIFACT_LINK_FAILED",
                    ),
                )
        status = (
            "succeeded"
            if len(files) == len(normalized_ids)
            else "partial"
            if files
            else "failed"
        )
        return {
            "protocolVersion": "0.1",
            "schemaVersion": "agentbridge.document_delivery_batch.v1",
            "status": status,
            "requestedCount": len(normalized_ids),
            "preparedCount": len(files),
            "failedCount": len(errors),
            "files": files,
            "errors": errors,
            "hostDelivery": {
                "mode": "direct_attachment_batch",
                "oneFilePerMessage": True,
                "handledByHost": True,
                "state": "prepared",
                "completionMeaning": "files_ready_not_endpoint_acknowledged",
            },
        }

    def reissue_document_download(
        self,
        *,
        user_subject: str,
        task_id: str,
        artifact_id: str,
    ) -> dict:
        try:
            artifact = self.tasks.get_artifact(
                task_id=task_id,
                artifact_id=artifact_id,
                user_subject=user_subject,
                include_source_ref=True,
            )
            artifact_type = artifact["artifact_type"]
            if artifact_type not in {
                "certificate_scan",
                ADDRESSBOOK_REPORT_ARTIFACT_TYPE,
                SMARTLIGHT_REPORT_ARTIFACT_TYPE,
            }:
                return _document_delivery_failure(
                    "ARTIFACT_REISSUE_UNSUPPORTED",
                    retryable=False,
                )
            source = self.document_downloads.get(
                artifact["source_ref"],
                include_document=True,
            )
            if source["user_subject"] != user_subject:
                raise DocumentDownloadAccessDenied(
                    "document download belongs to another user"
                )
            self.document_downloads.require_access(source)
            if artifact["state"] == "ready":
                return _reissued_document_delivery(artifact, reused=True)
            session = self.sessions.find(
                user_subject=user_subject,
                system_id=source["system_id"],
            )
            if session is None or session["state"] != "active":
                return _document_delivery_failure(
                    "LOGIN_REQUIRED",
                    retryable=True,
                )
            if artifact_type == "certificate_scan":
                replacement = self.document_downloads.create(
                    user_subject=user_subject,
                    system_id=source["system_id"],
                    session_id=session["session_id"],
                    document=source["document"],
                    filename=source["filename"],
                    document_type=source["document_type"],
                    display_size=source["display_size"],
                    card_base_url=self.trusted_card_base_url,
                    ttl_seconds=600,
                )
            elif artifact_type == SMARTLIGHT_REPORT_ARTIFACT_TYPE and (
                source["document_type"] != SMARTLIGHT_REPORT_DOCUMENT_TYPE
                or not is_smartlight_report_recipe(source["document"])
            ):
                return _document_delivery_failure(
                    "ARTIFACT_REISSUE_INVALID",
                    retryable=False,
                )
            elif artifact_type == ADDRESSBOOK_REPORT_ARTIFACT_TYPE and (
                source["document_type"] != ADDRESSBOOK_REPORT_DOCUMENT_TYPE
                or not is_addressbook_report_recipe(source["document"])
            ):
                return _document_delivery_failure(
                    "ARTIFACT_REISSUE_INVALID",
                    retryable=False,
                )
        except (TaskNotFound, DocumentDownloadNotFound):
            return _document_delivery_failure("DOWNLOAD_NOT_FOUND", retryable=False)
        except DocumentDownloadAccessDenied:
            return _document_delivery_failure("DOWNLOAD_ACCESS_DENIED", retryable=False)
        except DocumentDownloadIntegrityError:
            return _document_delivery_failure("DOWNLOAD_INTEGRITY_FAILED", retryable=False)
        except (TaskIntegrityError, ValueError):
            return _document_delivery_failure(
                "ARTIFACT_REISSUE_INVALID",
                retryable=False,
            )

        if artifact_type == "certificate_scan":
            prepared = self.prepare_document_download(
                user_subject=user_subject,
                download_id=replacement["download_id"],
            )
        elif artifact_type == SMARTLIGHT_REPORT_ARTIFACT_TYPE:
            recipe = source["document"]
            report_arguments = dict(recipe["arguments"])
            report_arguments["report_type"] = recipe["reportType"]
            operation = self.invoke(
                user_subject=user_subject,
                capability_name=SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
                arguments=report_arguments,
            )
            if operation.get("status") != "succeeded":
                error_code = str(
                    (operation.get("error") or {}).get("code")
                    or "REPORT_REGENERATION_FAILED"
                )
                return _document_delivery_failure(
                    "LOGIN_REQUIRED"
                    if error_code == "LOGIN_REQUIRED"
                    else "REPORT_REGENERATION_FAILED",
                    retryable=True,
                )
            prepared = operation.get("result") or {}
        else:
            recipe = source["document"]
            operation = self.invoke(
                user_subject=user_subject,
                capability_name=ADDRESSBOOK_EXPORT_CAPABILITY,
                arguments=dict(recipe["arguments"]),
            )
            if operation.get("status") != "succeeded":
                error_code = str(
                    (operation.get("error") or {}).get("code")
                    or "REPORT_REGENERATION_FAILED"
                )
                return _document_delivery_failure(
                    "LOGIN_REQUIRED"
                    if error_code == "LOGIN_REQUIRED"
                    else "REPORT_REGENERATION_FAILED",
                    retryable=True,
                )
            prepared = operation.get("result") or {}
        if prepared.get("status") != "succeeded":
            return prepared
        file = prepared["file"]
        try:
            refreshed = self.tasks.refresh_artifact(
                task_id=task_id,
                artifact_id=artifact_id,
                user_subject=user_subject,
                expected_source_ref=artifact["source_ref"],
                artifact={
                    "source_ref": file["downloadId"],
                    "filename": file["filename"],
                    "content_type": file["contentType"],
                    "byte_size": file["size"],
                    "download_url": file["mediaUrl"],
                    "expires_at": file["expiresAt"],
                },
            )
        except TaskIntegrityError:
            current = self.tasks.get_artifact(
                task_id=task_id,
                artifact_id=artifact_id,
                user_subject=user_subject,
                include_source_ref=True,
            )
            if current["state"] == "ready":
                return _reissued_document_delivery(current, reused=True)
            return _document_delivery_failure(
                "ARTIFACT_REISSUE_CONFLICT",
                retryable=True,
            )
        except (TaskNotFound, ValueError):
            return _document_delivery_failure(
                "TASK_ARTIFACT_REFRESH_FAILED",
                retryable=True,
            )
        return _reissued_document_delivery(refreshed, reused=False)

    def _materialize_document_downloads(self, *, session: dict, result: dict) -> dict:
        public_result = {key: value for key, value in result.items() if key != "items"}
        public_items = []
        for source_item in result.get("items") or []:
            item = dict(source_item)
            reference = item.pop("_download_reference")
            grant = self.document_downloads.create(
                user_subject=session["user_subject"],
                system_id=session["system_id"],
                session_id=session["session_id"],
                document=reference,
                filename=item["filename"],
                document_type=item["document_type"],
                display_size=item.get("display_size") or "",
                card_base_url=self.trusted_card_base_url,
                ttl_seconds=600,
            )
            item["download_id"] = grant["download_id"]
            item["download_url"] = grant["card_url"]
            item["download_expires_at"] = grant["expires_at"]
            public_items.append(item)
        public_result["items"] = public_items
        return public_result

    def _pending_batch_definition(self, *, task_id: str, user_subject: str, arguments: dict) -> dict:
        batch = self.tasks.get_batch_for_task(parent_task_id=task_id, user_subject=user_subject, include_resource_refs=True)
        if (batch is None or batch["capability_name"] != PENDING_BATCH_PREPARE_CAPABILITY
                or batch["batch_id"] != arguments.get("batch_id")
                or batch["state"] not in {"running", "waiting_user"}):
            raise CapabilityRejected("BATCH_NOT_ACTIVE", "批次已停止或上下文不匹配，不能执行旧卡片。")
        current = batch["items"][int(batch["current_ordinal"]) - 1]
        if current["resource_ref"] != arguments.get("affair_id"):
            raise CapabilityRejected("BATCH_TARGET_MISMATCH", "卡片事项不是当前批次条目。")
        display = current["display_summary"]
        for kind in ("prepare", "commit"):
            spec = self.registry.get(display[f"{kind}_capability"])
            self.user_grants.require_capability(user_subject, spec.name)
            if spec.version != display[f"{kind}_version"]:
                raise CapabilityRejected("BATCH_VERSION_CHANGED", "事项能力版本已变化，批次已停止，请重新核对。")
            try:
                self.governance_policies.assert_write_allowed(
                    system_id=batch["system_id"], user_subject=user_subject,
                    capability_name=spec.name, capability_version=spec.version,
                )
            except GovernancePolicyDenied as exc:
                raise CapabilityRejected("WRITE_PAUSED", "批次中当前事项的写能力已暂停。") from exc
        return {
            **_TRUSTED_WRITE_DEFINITIONS[display["prepare_capability"]],
            "context_fields": ("batch_id", "affair_id"),
        }

    def _resolve_approval_batch_context(
        self,
        *,
        context: CapabilityContext,
        session: dict,
        adapter: object,
        worker: object,
        arguments: dict,
        task_id: str | None,
    ) -> tuple[dict, dict | None]:
        if not task_id:
            raise CapabilityRejected(
                "HOST_TASK_REQUIRED",
                "Batch approval requires a durable AgentBridge host task.",
            )
        supplied_batch_id = str(arguments.get("batch_id") or "").strip()
        generic = context.spec.name == PENDING_BATCH_PREPARE_CAPABILITY
        existing = self.tasks.get_batch_for_task(
            parent_task_id=task_id, user_subject=session["user_subject"], include_resource_refs=True,
        )
        if generic and existing is not None and not supplied_batch_id:
            if existing["selection_summary"].get("request") != arguments:
                raise CapabilityRejected("BATCH_CONTEXT_MISMATCH", "当前任务已有不同的冻结清单，不能在续办时更换范围。")
            supplied_batch_id = existing["batch_id"]
        if supplied_batch_id:
            batch = self.tasks.get_batch_for_task(
                parent_task_id=task_id,
                user_subject=session["user_subject"],
                include_resource_refs=True,
            )
            if batch is None or batch["batch_id"] != supplied_batch_id or batch["capability_name"] != context.spec.name:
                raise CapabilityRejected(
                    "BATCH_CONTEXT_MISMATCH",
                    "The batch context does not match the current AgentBridge task.",
                )
            if batch["state"] not in {"running", "waiting_user", "paused"}:
                raise CapabilityRejected(
                    "BATCH_NOT_ACTIVE",
                    "The approval batch is already terminal.",
                )
            current = next(
                (
                    item
                    for item in batch["items"]
                    if int(item["ordinal"]) == int(batch["current_ordinal"])
                ),
                None,
            )
            if current is None:
                raise CapabilityRejected(
                    "BATCH_ITEM_MISSING",
                    "The current approval batch item is unavailable.",
                )
            if generic:
                for kind in ("prepare", "commit"):
                    self.user_grants.require_capability(
                        session["user_subject"], current["display_summary"][f"{kind}_capability"]
                    )
            supplied_affair_id = str(arguments.get("affair_id") or "").strip()
            if supplied_affair_id and supplied_affair_id != current["resource_ref"]:
                raise CapabilityRejected(
                    "BATCH_TARGET_MISMATCH",
                    "The requested affair does not match the frozen batch target.",
                )
            return {
                **arguments,
                "batch_id": batch["batch_id"],
                "affair_id": current["resource_ref"],
                **_batch_item_card_context(batch, current),
            }, None

        if str(arguments.get("input_submission_id") or "").strip():
            raise CapabilityRejected(
                "BATCH_CONTEXT_MISSING",
                "The trusted field submission is missing its batch context.",
            )
        if generic:
            pending = adapter.list_workflows(worker, collection="pending", arguments={"limit": 100})
            try:
                selected = select_pending_batch_items(pending, arguments, self.registry)
            except PendingBatchSelectionError as exc:
                raise CapabilityRejected(exc.code, str(exc)) from exc
            if not selected:
                return {}, {"status": "empty", "frozen_count": 0, "message": "当前待办没有符合指定范围的事项，未执行任何审批。"}
            for item in selected:
                for kind in ("prepare", "commit"):
                    self.user_grants.require_capability(
                        session["user_subject"], item["display_summary"][f"{kind}_capability"]
                    )
            try:
                batch, _reused = self.tasks.create_batch(
                    parent_task_id=task_id, user_subject=session["user_subject"], system_id=session["system_id"],
                    capability_name=PENDING_BATCH_PREPARE_CAPABILITY,
                    selection_summary={"request": arguments, "collection": "pending"},
                    failure_policy="stop_on_failure", items=selected,
                )
            except TaskIntegrityError as exc:
                raise CapabilityRejected("BATCH_ALREADY_ACTIVE", str(exc)) from exc
            current = batch["items"][int(batch["current_ordinal"]) - 1]
            return {"batch_id": batch["batch_id"], "affair_id": current["resource_ref"], **_batch_item_card_context(batch, current)}, None
        limit = arguments.get("limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        failure_policy = str(
            arguments.get("failure_policy") or "stop_on_failure"
        ).strip()
        if failure_policy != "stop_on_failure":
            raise ValueError("failure_policy must be stop_on_failure")
        pending = adapter.list_workflows(
            worker,
            collection="pending",
            arguments={"limit": 100},
        )
        selected = select_missed_punch_approval_batch_items(
            list(pending.get("items") or []),
            limit=limit,
        )
        if not selected:
            return {}, {
                "schema_version": "agentbridge.oa_missed_punch_approval_batch.v1",
                "business_intent": "approve_all_pending_missed_punch_requests",
                "status": "empty",
                "frozen_count": 0,
                "message": "当前没有可处理的补签申请待办。",
            }
        try:
            batch, _reused = self.tasks.create_batch(
                parent_task_id=task_id,
                user_subject=session["user_subject"],
                system_id=session["system_id"],
                capability_name=MISSED_PUNCH_APPROVAL_BATCH_PREPARE_CAPABILITY,
                selection_summary={
                    "workflowType": "missed_punch",
                    "collection": "pending",
                    "limit": limit,
                },
                failure_policy=failure_policy,
                items=selected,
            )
        except TaskIntegrityError as exc:
            raise CapabilityRejected("BATCH_ALREADY_ACTIVE", str(exc)) from exc
        current = batch["items"][int(batch["current_ordinal"]) - 1]
        return {
            "batch_id": batch["batch_id"],
            "affair_id": current["resource_ref"],
            **_batch_item_card_context(batch, current),
        }, None

    @contextmanager
    def _task_call_lock(self, task_id: str) -> Iterator[None]:
        digest = hashlib.sha256(task_id.encode("utf-8")).digest()
        lock = self._task_call_locks[
            int.from_bytes(digest[:4], "big") % len(self._task_call_locks)
        ]
        with lock:
            yield

    @contextmanager
    def _session_lock(
        self,
        session_id: str,
        *,
        wait_seconds: float | None = None,
    ) -> Iterator[None]:
        with self._locks_guard:
            lock = self._session_locks.setdefault(session_id, threading.Lock())
        acquired = (
            lock.acquire()
            if wait_seconds is None
            else lock.acquire(timeout=max(0.0, wait_seconds))
        )
        if not acquired:
            raise CapabilityRejected(
                "SESSION_BUSY",
                "Another OA operation is using this user session. Retry once after it "
                "finishes; batch certificate titles through the names argument instead "
                "of launching parallel searches.",
            )
        try:
            yield
        finally:
            lock.release()

    def _record_user_activity(self, *, user_subject: str, system_id: str) -> None:
        session = self.sessions.find(user_subject=user_subject, system_id=system_id)
        if session is None or session["state"] != "active":
            return
        with self._session_lock(session["session_id"]):
            current = self.sessions.get(session["session_id"])
            if current["state"] == "active":
                self.sessions.touch_activity(current["session_id"])

    def _session_response(self, session: dict) -> dict:
        return session_response(
            session,
            activity_lease_seconds=self.session_keepalive_lease_seconds,
        )

    @staticmethod
    def _default_worker_factory(session: dict, adapter: SeeyonCentralAdapter):
        return CentralBrowserWorker(
            profile_path=session["profile_path"],
            allowed_origins={adapter.origin},
            headless=True,
        )

    @staticmethod
    def _default_browser_worker_factory(session: dict, adapter: object):
        return CentralBrowserWorker(
            profile_path=session["profile_path"],
            allowed_origins=set(
                getattr(adapter, "allowed_origins", None) or {adapter.origin}
            ),
            headless=True,
        )

    @staticmethod
    def _default_http_worker_factory(_session: dict, adapter: object):
        return CentralHttpWorker(allowed_origins={adapter.origin})


def login_required_action(
    user_subject: str,
    system_id: str,
    session: dict | None,
) -> RequiresUserAction:
    return RequiresUserAction(
        "LOGIN_REQUIRED",
        f"The central {system_id} session is not active.",
        next_action={
            "type": "session_login",
            "system": system_id,
            "userSubject": user_subject,
            "sessionState": session["state"] if session else "not_found",
        },
    )


def _interaction_not_ready_response(
    interaction: dict,
    *,
    code: str | None = None,
    message: str | None = None,
) -> dict:
    state = interaction["state"]
    pending = state in {"pending", "processing"}
    effective_code = code or {
        "pending": "INTERACTION_PENDING",
        "processing": "INTERACTION_PROCESSING",
        "declined": "INTERACTION_DECLINED",
        "expired": "INTERACTION_EXPIRED",
        "superseded": "INTERACTION_SUPERSEDED",
        "failed": "INTERACTION_FAILED",
    }.get(state, "INTERACTION_UNAVAILABLE")
    effective_message = message or (
        "The trusted user interaction has not completed yet."
        if pending
        else f"The trusted user interaction is unavailable: {state}."
    )
    return {
        "protocolVersion": "0.1",
        "status": "requires_user_action" if pending else "failed",
        "error": {
            "code": effective_code,
            "message": effective_message,
        },
        "interaction": interaction,
        "nextAction": {
            "type": "wait_for_interaction" if pending else "start_again",
            "interactionId": interaction["interactionId"],
        },
    }


def _session_runtime_mismatch_action(
    user_subject: str,
    session: dict,
) -> RequiresUserAction:
    return RequiresUserAction(
        "SESSION_RUNTIME_MISMATCH",
        (
            "The encrypted OA session is bound to a different OS security "
            "context. Retry through the central runtime that created the session; "
            "do not request a new login card."
        ),
        next_action=_session_runtime_next_action(
            user_subject,
            session,
            action_type="retry_via_bound_central_runtime",
        ),
    )


def _session_state_unavailable_action(
    user_subject: str,
    session: dict,
) -> RequiresUserAction:
    return RequiresUserAction(
        "SESSION_STATE_UNAVAILABLE",
        (
            "The encrypted OA session state cannot be read safely. Retry through "
            "the bound central runtime or ask an administrator to repair the "
            "session store before reauthentication."
        ),
        next_action=_session_runtime_next_action(
            user_subject,
            session,
            action_type="repair_session_runtime",
        ),
    )


def _session_runtime_mismatch_response(user_subject: str, session: dict) -> dict:
    return _session_action_response(
        _session_runtime_mismatch_action(user_subject, session),
        session,
    )


def _session_state_unavailable_response(user_subject: str, session: dict) -> dict:
    return _session_action_response(
        _session_state_unavailable_action(user_subject, session),
        session,
    )


def _session_check_unavailable_action(
    user_subject: str,
    session: dict,
    *,
    diagnostics: str | None = None,
) -> RequiresUserAction:
    detail = f" Diagnostic: {diagnostics}" if diagnostics else ""
    system_label = {
        "oa": "OA",
        "taihua": "Taihua",
        "smartlight": "Smartlight",
        "yuque": "Yuque",
    }.get(str(session.get("system_id") or ""), "Downstream system")
    return RequiresUserAction(
        "SESSION_CHECK_UNAVAILABLE",
        (
            f"{system_label} session validity could not be checked. Retry later through the "
            f"same central runtime; credentials are not required yet.{detail}"
        ),
        next_action=_session_runtime_next_action(
            user_subject,
            session,
            action_type="retry_session_check",
        ),
    )


def _session_check_unavailable_response(
    user_subject: str,
    session: dict,
    *,
    diagnostics: str | None = None,
) -> dict:
    action = _session_check_unavailable_action(
        user_subject,
        session,
        diagnostics=diagnostics,
    )
    return {
        "protocolVersion": "0.1",
        "status": "failed",
        "sessionId": session["session_id"],
        "error": {
            "code": action.code,
            "message": action.message,
            "retryable": True,
        },
        "nextAction": action.next_action,
        "reused": False,
    }


def _session_action_response(action: RequiresUserAction, session: dict) -> dict:
    return {
        "protocolVersion": "0.1",
        "status": "requires_user_action",
        "sessionId": session["session_id"],
        "error": {
            "code": action.code,
            "message": action.message,
        },
        "nextAction": action.next_action,
        "reused": False,
    }


def _session_runtime_next_action(
    user_subject: str,
    session: dict,
    *,
    action_type: str,
) -> dict:
    return {
        "type": action_type,
        "system": session["system_id"],
        "userSubject": user_subject,
        "sessionId": session["session_id"],
        "sessionState": session["state"],
        "sessionPreserved": True,
    }


def session_response(
    session: dict,
    *,
    activity_lease_seconds: float | None = None,
) -> dict:
    last_user_activity = session.get("last_user_activity_at")
    eligible_until = None
    eligible_at = None
    if activity_lease_seconds is not None and last_user_activity:
        activity_at = _parse_utc(last_user_activity)
        if activity_at is not None:
            eligible_at = activity_at + timedelta(seconds=activity_lease_seconds)
            eligible_until = eligible_at.isoformat()

    if activity_lease_seconds is None:
        keepalive_state = "not_configured"
    elif session["state"] != "active":
        keepalive_state = "inactive"
    elif eligible_at is None:
        keepalive_state = "activity_unknown"
    elif datetime.now(timezone.utc) <= eligible_at:
        keepalive_state = "eligible"
    else:
        keepalive_state = "outside_lease"
    if keepalive_state == "eligible":
        session_state_basis = "maintained"
        keepalive_explanation = (
            "Recent user activity is within the controlled keepalive lease."
        )
    elif keepalive_state == "outside_lease":
        session_state_basis = "last_confirmed"
        keepalive_explanation = (
            "Background keepalive is paused because the recent-activity lease ended. "
            "Active is the last confirmed state, not a current live guarantee."
        )
    elif keepalive_state == "activity_unknown":
        session_state_basis = "last_confirmed"
        keepalive_explanation = (
            "Background keepalive eligibility cannot be calculated because recent "
            "user activity is unknown."
        )
    elif keepalive_state == "inactive":
        session_state_basis = "registry"
        keepalive_explanation = "The downstream session is not active."
    else:
        session_state_basis = "registry"
        keepalive_explanation = "Controlled keepalive is not configured."
    return {
        "protocolVersion": "0.1",
        "status": session["state"],
        "sessionId": session["session_id"],
        "systemId": session["system_id"],
        "userSubject": session["user_subject"],
        "expectedPrincipalRef": session.get("expected_principal_ref"),
        "downstreamPrincipalRef": session.get("downstream_principal_ref"),
        "lastVerifiedAt": session.get("last_verified_at"),
        "lastActivityAt": last_user_activity,
        "lastUserActivityAt": last_user_activity,
        "lastKeepaliveAt": session.get("last_keepalive_at"),
        "keepaliveEligibleUntil": eligible_until,
        "keepaliveState": keepalive_state,
        "keepaliveActive": keepalive_state == "eligible",
        "keepaliveExplanation": keepalive_explanation,
        "sessionStateBasis": session_state_basis,
        "expiredAt": session.get("expired_at"),
        "lastError": session.get("last_error"),
    }


def _document_delivery_failure(error_code: str, *, retryable: bool) -> dict:
    return {
        "protocolVersion": "0.1",
        "schemaVersion": "agentbridge.document_delivery.v1",
        "status": "failed",
        "error": {
            "code": error_code,
            "message": "AgentBridge could not prepare the file attachment.",
            "retryable": retryable,
        },
    }


def _document_delivery_batch_failure(
    download_ids: list[str],
    error: dict,
) -> dict:
    return {
        "protocolVersion": "0.1",
        "schemaVersion": "agentbridge.document_delivery_batch.v1",
        "status": "failed",
        "requestedCount": len(download_ids),
        "preparedCount": 0,
        "failedCount": 1,
        "files": [],
        "errors": [error],
        "hostDelivery": {
            "mode": "direct_attachment_batch",
            "oneFilePerMessage": True,
            "handledByHost": True,
        },
    }


def _prepared_document_file(
    ready: dict,
    *,
    artifact: dict | None,
    artifact_reused: bool,
) -> dict:
    return {
        "downloadId": ready["download_id"],
        "filename": ready["filename"],
        "contentType": ready["content_type"],
        "size": ready["prepared_size"],
        "mediaUrl": f"{ready['card_url']}/file",
        "expiresAt": ready["expires_at"],
        "artifactId": artifact["artifact_id"] if artifact is not None else None,
        "artifactReused": artifact_reused,
    }


def _display_file_size(byte_size: int) -> str:
    if byte_size < 1024:
        return f"{byte_size} B"
    if byte_size < 1024 * 1024:
        return f"{byte_size / 1024:.1f} KB"
    return f"{byte_size / (1024 * 1024):.1f} MB"


def _validated_document_download_ids(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ValueError("download_ids must contain between 1 and 20 items")
    result = []
    seen = set()
    for value in values:
        download_id = str(value or "").strip()
        if not 32 <= len(download_id) <= 128 or any(
            character
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in download_id
        ):
            raise ValueError("document download ID is invalid")
        if download_id not in seen:
            seen.add(download_id)
            result.append(download_id)
    return result


def _document_batch_error(
    download_id: str,
    exc: Exception,
    *,
    code: str | None = None,
) -> dict:
    if code is not None:
        error_code = code
        retryable = True
    elif isinstance(exc, DocumentDownloadNotFound):
        error_code = "DOWNLOAD_NOT_FOUND"
        retryable = False
    elif isinstance(exc, DocumentDownloadAccessDenied):
        error_code = "DOWNLOAD_ACCESS_DENIED"
        retryable = False
    elif isinstance(exc, DocumentDownloadIntegrityError):
        error_code = "DOWNLOAD_INTEGRITY_FAILED"
        retryable = False
    elif isinstance(exc, DocumentDownloadStateError):
        error_code = "DOWNLOAD_NOT_READY"
        retryable = True
    elif isinstance(exc, AdapterLoginRequired):
        error_code = "LOGIN_REQUIRED"
        retryable = True
    else:
        error_code = _document_download_error_code(exc)
        retryable = True
    return {
        "downloadId": download_id,
        "code": error_code,
        "message": "AgentBridge could not prepare this file attachment.",
        "retryable": retryable,
    }


def _reissued_document_delivery(artifact: dict, *, reused: bool) -> dict:
    return {
        "protocolVersion": "0.1",
        "schemaVersion": "agentbridge.document_delivery.v1",
        "status": "succeeded",
        "file": {
            "downloadId": artifact.get("source_ref"),
            "filename": artifact["filename"],
            "contentType": artifact["content_type"],
            "size": artifact["byte_size"],
            "mediaUrl": artifact["download_url"],
            "expiresAt": artifact["expires_at"],
            "artifactId": artifact["artifact_id"],
            "artifactReused": reused,
        },
        "hostDelivery": {
            "mode": "direct_attachment",
            "oneFilePerMessage": True,
            "handledByHost": True,
        },
    }


def _document_download_error_code(exc: Exception) -> str:
    name = exc.__class__.__name__
    normalized = "".join(
        character if character.isalnum() else "_"
        for character in name.upper()
    ).strip("_")
    return normalized[:80] or "DOCUMENT_PREPARATION_FAILED"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def operation_response(operation: dict) -> dict:
    next_action = operation.get("next_action")
    interaction = (
        next_action.get("interaction")
        if isinstance(next_action, dict)
        and isinstance(next_action.get("interaction"), dict)
        else None
    )
    return {
        "operationId": operation["operation_id"],
        "requestId": operation["request_id"],
        "userSubject": operation["user_subject"],
        "capability": operation["capability_name"],
        "capabilityVersion": operation["capability_version"],
        "status": operation["status"],
        "result": operation.get("result"),
        "error": operation.get("error"),
        "nextAction": next_action,
        "interaction": interaction,
        "createdAt": operation["created_at"],
        "updatedAt": operation["updated_at"],
        "finishedAt": operation.get("finished_at"),
    }


def task_response(task: dict) -> dict:
    return {
        "taskId": task["task_id"],
        "userSubject": task["user_subject"],
        "agentHost": task["agent_host"],
        "title": task["title"],
        "status": task["status"],
        "summary": task.get("summary") or {},
        "originEndpointId": task["origin_endpoint_id"],
        "currentOperationId": task.get("current_operation_id"),
        "currentInteractionId": task.get("current_interaction_id"),
        "activeConversationRef": task["active_conversation_ref"],
        "version": task["version"],
        "createdAt": task["created_at"],
        "updatedAt": task["updated_at"],
        "finishedAt": task.get("finished_at"),
    }


def batch_response(batch: dict) -> dict:
    return {
        "batchId": batch["batch_id"],
        "parentTaskId": batch["parent_task_id"],
        "systemId": batch["system_id"],
        "capability": batch["capability_name"],
        "state": batch["state"],
        "currentOrdinal": int(batch["current_ordinal"]),
        "totalCount": int(batch["total_count"]),
        "succeededCount": int(batch["succeeded_count"]),
        "failedCount": int(batch["failed_count"]),
        "skippedCount": int(batch["skipped_count"]),
        "failurePolicy": batch["failure_policy"],
        "items": [
            {
                "ordinal": int(item["ordinal"]),
                "state": item["state"],
                "display": item.get("display_summary") or {},
                "errorCode": item.get("error_code"),
            }
            for item in batch.get("items") or []
        ],
        "createdAt": batch["created_at"],
        "updatedAt": batch["updated_at"],
        "finishedAt": batch.get("finished_at"),
    }


def _batch_item_card_context(batch: dict, item: dict) -> dict:
    display = item.get("display_summary") or {}
    return {
        "batch_ordinal": int(item["ordinal"]),
        "batch_total": int(batch["total_count"]),
        "target_title": str(display.get("title") or ""),
        "target_sender": str(display.get("sender") or ""),
        "target_date": str(display.get("date") or ""),
    }


def continuation_response(continuation: dict) -> dict:
    return {
        "endpointId": continuation["endpoint_id"],
        "selectedTaskId": continuation.get("selected_task_id"),
        "candidateTaskIds": continuation.get("candidate_task_ids") or [],
        "state": continuation["state"],
        "executionMode": continuation["execution_mode"],
        "allowNewOperation": continuation.get("allow_new_operation") is True,
        "reason": continuation.get("reason"),
        "expiresAt": continuation["expires_at"],
        "version": continuation["version"],
        "updatedAt": continuation["updated_at"],
    }


def task_continuation_candidate_response(candidate: dict, ordinal: int) -> dict:
    task = candidate["task"]
    origin = candidate["origin_endpoint"]
    return {
        "ordinal": ordinal,
        "taskId": task["task_id"],
        "title": task["title"],
        "status": task["status"],
        "phase": task_continuation_phase(task),
        "origin": {
            "clientType": origin.get("client_type"),
            "label": origin.get("label"),
        },
        "updatedAt": task["updated_at"],
        "finishedAt": task.get("finished_at"),
    }


def task_continuation_phase(task: dict) -> str:
    status = str(task.get("status") or "")
    if status == "waiting_user":
        return "waiting_user"
    if status == "running":
        return "running"
    if status == "active":
        return "ready"
    return "terminal"


def task_continuation_execution_mode(
    task: dict,
    *,
    allow_follow_up: bool,
) -> str:
    status = str(task.get("status") or "")
    if status == "active":
        return "resume"
    if status == "succeeded" and allow_follow_up:
        return "follow_up"
    return "observe_only"


def endpoint_response(endpoint: dict) -> dict:
    return {
        "endpointId": endpoint["endpoint_id"],
        "userSubject": endpoint["user_subject"],
        "agentHost": endpoint["agent_host"],
        "clientType": endpoint["client_type"],
        "externalSubject": endpoint["external_subject"],
        "accountId": endpoint.get("account_id"),
        "conversationRef": endpoint["conversation_ref"],
        "label": endpoint.get("label"),
        "capabilities": endpoint.get("capabilities") or [],
        "route": endpoint.get("route") or {},
        "state": endpoint["state"],
        "createdAt": endpoint["created_at"],
        "updatedAt": endpoint["updated_at"],
        "lastSeenAt": endpoint["last_seen_at"],
    }


def timeline_entry_response(entry: dict) -> dict:
    return {
        "entryId": entry["entry_id"],
        "sequence": entry["sequence"],
        "entryType": entry["entry_type"],
        "taskId": entry.get("task_id"),
        "role": entry.get("role"),
        "text": entry.get("text"),
        "payload": entry.get("payload") or {},
        "createdAt": entry["created_at"],
    }


def _artifact_notification(event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    return {
        "artifactId": payload.get("artifactId"),
        "artifactType": payload.get("artifactType"),
        "filename": payload.get("filename"),
        "contentType": payload.get("contentType"),
        "size": payload.get("size"),
        "mediaUrl": payload.get("downloadUrl"),
        "expiresAt": payload.get("expiresAt"),
    }


def _timeline_notification_message(entry: dict) -> str:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    client_type = str(source.get("clientType") or "unknown")
    source_label = {
        "web": "网页端",
        "webchat": "网页端",
        "telegram": "Telegram",
        "openclaw-weixin": "微信",
    }.get(client_type, str(source.get("label") or "其他端"))
    actor = "你" if entry.get("role") == "user" else "智能体"
    text = str(entry.get("text") or "").strip()
    return f"【{source_label} · {actor}】\n{text}"


_TASK_TITLE_LABELS = {
    "Prepare OA Efficiency-Data Approval": "OA 效能数据审批",
    "Prepare OA Travel-Expense Approval": "OA 差旅费审批",
    "Prepare OA Labor-Contract Renewal Approval": "OA 劳动合同续签审批",
    "Prepare OA Intellectual-Property Declaration Approval": "OA 知识产权申报审批",
    "Prepare OA Attendance Confirmation": "OA 月度考勤确认",
    "Prepare OA Weekly-Report Acknowledgement": "OA 周报阅办",
    "Prepare OA Standard-Collaboration Approval": "OA 普通事项审批",
    "Prepare OA Workflow Revoke": "OA 流程撤销",
    "Prepare OA Business Trip Draft": "OA 出差申请草稿",
    "Prepare OA Business Trip Submission": "OA 出差申请提交",
    "Prepare OA Leave Draft": "OA 请假申请草稿",
    "Prepare OA Leave Submission": "OA 请假申请提交",
    "Prepare OA Missed-Punch Draft": "OA 补签申请草稿",
    "Prepare OA Missed-Punch Approval": "OA 补签申请审批",
    "Prepare All Pending OA Missed-Punch Approvals": "批量处理 OA 补签申请",
    "Prepare OA Meeting Creation": "OA 会议创建",
    "Prepare and Deliver One OA Certificate Scan": "OA 证书文件交付",
    "Prepare Taihua Work Log": "工作日志提交",
}


def _task_notification_message(task: dict, event: dict) -> str:
    event_type = str(event.get("eventType") or "")
    raw_title = str(task.get("title") or "AgentBridge 任务")
    title = _TASK_TITLE_LABELS.get(raw_title, raw_title)
    if event_type == "task.created":
        return f"{title}：任务已在另一端发起。"
    if event_type in {
        "task.operation.linked",
        "task.operation.running",
    }:
        return f"{title}：任务正在执行。"
    if event_type == "task.operation.requires_user_action":
        return f"{title}：任务正在等待用户填写或确认。"
    if event_type == "task.interaction.waiting":
        return f"{title}：任务正在等待用户填写或确认。"
    if event_type == "task.interaction.completed":
        return f"{title}：可信确认已完成。"
    if event_type == "task.operation.succeeded":
        return f"{title}：已完成。"
    if event_type == "task.completed":
        return f"{title}：已完成。"
    if event_type == "task.canceled":
        return f"{title}：用户已取消本次操作。"
    if event_type == "task.interaction.expired":
        return f"{title}：可信确认已过期，请从智能体重新发起。"
    if event_type == "task.interaction.superseded":
        return f"{title}：当前交互已被更新，请使用最新卡片继续。"
    if event_type in {
        "task.interaction.failed",
        "task.operation.failed",
        "task.failed",
    }:
        return f"{title}：执行失败，请查看任务详情。"
    if event_type == "task.operation.outcome_unknown":
        return f"{title}：最终结果未能确认，请先在目标系统核对。"
    if event_type in {"plan.proposed", "plan.validated", "plan.started"}:
        return f"{title}：跨系统任务计划已启动。"
    if event_type == "plan.step.started":
        return f"{title}：正在执行下一步。"
    if event_type == "plan.step.succeeded":
        return f"{title}：当前步骤已完成。"
    if event_type == "plan.result.ready":
        return f"{title}：可核对的组合任务结果已生成。"
    if event_type == "plan.step.resumed":
        return f"{title}：登录恢复后已自动继续。"
    if event_type == "plan.step.recovered":
        return f"{title}：服务恢复后已自动继续。"
    if event_type in {"plan.step.waiting", "plan.authorization.waiting"}:
        return f"{title}：正在等待用户填写或确认。"
    if event_type == "plan.completed":
        return f"{title}：跨系统任务已完成。"
    if event_type == "plan.canceled":
        return f"{title}：跨系统任务已取消。"
    if event_type == "plan.step.failed":
        return f"{title}：跨系统任务执行失败，请查看任务详情。"
    if event_type == "plan.outcome_unknown":
        return f"{title}：最终结果未能确认，请先在目标系统核对。"
    return f"{title}：任务状态已更新为 {task['status']}。"


def _plan_result_notification_message(task: dict, projection: dict) -> str:
    raw_title = str(task.get("title") or "AgentBridge 任务")
    title = _TASK_TITLE_LABELS.get(raw_title, raw_title)
    result = projection.get("result") if isinstance(projection.get("result"), dict) else {}
    draft = str(result.get("draft") or "").strip()
    included = int(result.get("included_count") or 0)
    excluded = int(result.get("excluded_count") or 0)
    if projection.get("kind") == "private_draft" and draft:
        suffix = f"\n\n采用 {included} 项"
        if excluded:
            suffix += f"，排除 {excluded} 项"
        prefix = f"{title}：草稿已生成。\n\n"
        ending = f"{suffix}。"
        maximum_message_chars = 3_800
        available = max(0, maximum_message_chars - len(prefix) - len(ending))
        if len(draft) > available:
            draft = draft[: max(0, available - 7)].rstrip() + "\n（已截断）"
        return f"{prefix}{draft}{ending}"
    return f"{title}：可核对的组合任务结果已生成。"


def challenge_response(challenge: dict) -> dict:
    return {
        "challengeId": challenge["challenge_id"],
        "type": challenge["challenge_type"],
        "state": challenge["state"],
        "systemId": challenge["system_id"],
        "systemName": challenge["system_name"],
        "userSubject": challenge["user_subject"],
        "sessionId": challenge["session_id"],
        "expectedPrincipalRef": challenge.get("expected_principal_ref"),
        "origin": challenge["origin"],
        "cardUrl": challenge["card_url"],
        "expiresAt": challenge["expires_at"],
        "error": challenge.get("error"),
        "result": challenge.get("result"),
    }
