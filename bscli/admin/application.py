from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
import secrets
import sqlite3

from bscli.admin.stores import (
    AdminAccountStore,
    AdminAuditStore,
    AdminSessionStore,
)
from bscli.core.central_service import CentralCapabilityService, session_response
from bscli.core.mcp_identities import McpIdentityTokenStore
from bscli.core.user_grants import PERMISSIONS
from bscli.core.runtime_diagnostics import HOST_CONTROL_DIAGNOSTICS
from bscli.core.sessions import SessionPrincipalMismatch
from bscli.workspace.gateway import OpenClawGatewayClient
from bscli.database.independent import DatabaseGrants, CAPABILITIES as DATABASE_CAPABILITIES


SYSTEM_LABELS = {
    "oa": "致远 OA",
    "taihua": "日志系统",
    "yuque": "部门信息库",
    "smartlight": "照明实验室测试系统",
}


class AdminControlPlane:
    def __init__(
        self,
        *,
        service: CentralCapabilityService,
        identity_store: McpIdentityTokenStore,
        workspace_gateway: OpenClawGatewayClient | None = None,
        started_at: str | None = None,
    ) -> None:
        self.service = service
        self.identity_store = identity_store
        self.db_path = service.db_path
        self.accounts = AdminAccountStore(self.db_path)
        self.admin_sessions = AdminSessionStore(self.db_path)
        self.audit = AdminAuditStore(self.db_path)
        self.policies = service.governance_policies
        self.workspace_gateway = workspace_gateway
        self.started_at = started_at or _utc_now()
        self.release_id = os.environ.get("AGENTBRIDGE_RELEASE_ID") or "development"
        self.database_grants = DatabaseGrants(service.home / 'database' / 'grants.sqlite3')
        self.user_grants = service.user_grants

    def list_admin_accounts(self) -> list[dict]:
        return self.accounts.list()

    def create_admin_account(
        self,
        *,
        actor: dict,
        request_ip: str,
        username: str,
        role: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        bootstrap_password = f"Ab9!{secrets.token_urlsafe(18)}"
        account = self.accounts.create(
            username=username,
            password=bootstrap_password,
            role=role,
            must_change_password=True,
        )
        self.audit.append(
            actor=actor,
            action="admin.account.create",
            target_type="admin_account",
            target_id=account["account_id"],
            request_ip=request_ip,
            reason=reason,
            result="succeeded",
            after=account,
        )
        return {**account, "bootstrap_password": bootstrap_password}
    def overview(self) -> dict:
        tokens = self.identity_store.list(limit=1000)
        sessions = self.service.sessions.list(limit=1000)
        operations = self.service.operations.list(limit=500)
        policies = self.policies.list(state="paused")
        now = datetime.now(timezone.utc)
        recent_cutoff = now - timedelta(hours=24)
        recent_operations = [
            item for item in operations if _parse_time(item["created_at"]) >= recent_cutoff
        ]
        status_counts = Counter(item["status"] for item in recent_operations)
        system_counts: dict[str, Counter] = defaultdict(Counter)
        for session in sessions:
            system_counts[session["system_id"]][session["state"]] += 1
        systems = []
        configured = set(self.service._adapters_by_system)
        for system_id in sorted(configured | set(system_counts)):
            counts = system_counts[system_id]
            systems.append(
                {
                    "system_id": system_id,
                    "label": SYSTEM_LABELS.get(system_id, system_id),
                    "configured": system_id in configured,
                    "active_sessions": counts["active"],
                    "attention_sessions": sum(
                        count for state, count in counts.items() if state not in {"active", "new"}
                    ),
                    "total_sessions": sum(counts.values()),
                }
            )
        runtime = self.runtime()
        task_hub = runtime["coordination"]["task_hub"]
        task_summary = task_hub["summary"]
        waiting_tasks = sum(
            int(user.get("task_statuses", {}).get("waiting_user", 0))
            for user in task_hub.get("users", [])
        )
        return {
            "generated_at": _utc_now(),
            "runtime": runtime,
            "summary": {
                "users": len(self._users(tokens=tokens, sessions=sessions)),
                "active_tokens": sum(
                    1
                    for item in tokens
                    if item["state"] == "active" and _parse_time(item["expires_at"]) > now
                ),
                "active_sessions": sum(1 for item in sessions if item["state"] == "active"),
                "paused_policies": len(policies),
                "operations_24h": len(recent_operations),
                "failed_operations_24h": status_counts["failed"] + status_counts["unknown"],
                "active_tasks": task_summary.get("active_tasks", 0),
                "waiting_tasks": waiting_tasks,
                "active_endpoints": task_summary.get("active_endpoints", 0),
                "outstanding_deliveries": task_summary.get("outstanding_deliveries", 0),
                "active_host_dispatches": task_summary.get(
                    "active_host_dispatches", 0
                ),
                "waiting_host_dispatches": task_summary.get(
                    "waiting_host_dispatches", 0
                ),
                "acceptance_unknown_dispatches": task_summary.get(
                    "acceptance_unknown_dispatches", 0
                ),
                "isolation_violations": task_summary.get("isolation_violation_count", 0),
                "open_incidents": runtime["governance"]["open_incidents"],
                "critical_incidents": runtime["governance"]["critical_incidents"],
            },
            "operation_statuses_24h": dict(status_counts),
            "systems": systems,
            "recent_operations": self._operation_projections(recent_operations[:8]),
            "paused_policies": policies[:8],
        }

    def runtime(self) -> dict:
        governance = self.service.runtime_governance.summary()
        return {
            "service": "agentbridge",
            "release_id": self.release_id,
            "started_at": self.started_at,
            "admin_api": "v2",
            "database": "available",
            "systems": [
                {
                    "system_id": system_id,
                    "label": SYSTEM_LABELS.get(system_id, system_id),
                    "configured": True,
                }
                for system_id in sorted(self.service._adapters_by_system)
            ],
            "session_keepalive": {
                "enabled": self.service.session_keepalive_lease_seconds is not None,
                "activity_lease_seconds": self.service.session_keepalive_lease_seconds,
            },
            "workspace_gateway": (
                self.workspace_gateway.diagnostics()
                if self.workspace_gateway is not None
                else {
                    "configured": False,
                    "target": None,
                    "last_attempt_at": None,
                    "last_success_at": None,
                    "last_error_at": None,
                    "last_error_code": None,
                }
            ),
            "coordination": {
                "task_hub": self.service.tasks.runtime_diagnostics(),
                "host_control": HOST_CONTROL_DIAGNOSTICS.snapshot(),
                "agent_hosts": self.service.host_runtime_overview(),
            },
            "governance": governance,
        }

    def readiness(self) -> dict:
        return self.service.runtime_governance.readiness()

    def runtime_governance(self, *, evaluate: bool = False) -> dict:
        evaluation = None
        if evaluate:
            evaluation = self.service.runtime_governance.evaluate_incidents(
                task_diagnostics=self.service.tasks.runtime_diagnostics(),
            )
        slo = self.service.runtime_governance.refresh_slo_rollups(hours=24)
        return {
            "generated_at": _utc_now(),
            "summary": self.service.runtime_governance.summary(),
            "evaluation": evaluation,
            "slo": slo,
            "observations": self.service.runtime_governance.list_observations(limit=20),
            "recent_signals": self.service.runtime_governance.list_signals(limit=30),
            "recent_recoveries": self.service.runtime_governance.list_recovery_actions(limit=30),
        }

    def runtime_traces(
        self,
        *,
        user_subject: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return self.service.runtime_governance.list_traces(
            user_subject=user_subject,
            status=status,
            limit=limit,
        )

    def runtime_trace(self, trace_id: str) -> dict:
        return self.service.runtime_governance.trace_detail(trace_id)

    def runtime_incidents(
        self,
        *,
        state: str | None = None,
        severity: str | None = None,
        limit: int = 300,
    ) -> list[dict]:
        return self.service.runtime_governance.list_incidents(
            state=state,
            severity=severity,
            limit=limit,
        )

    def transition_runtime_incident(
        self,
        *,
        actor: dict,
        request_ip: str,
        incident_id: str,
        state: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        before = next(
            (
                item
                for item in self.service.runtime_governance.list_incidents(limit=1000)
                if item["incident_id"] == incident_id
            ),
            None,
        )
        after = self.service.runtime_governance.transition_incident(
            incident_id,
            state=state,
            actor=actor["username"],
            reason=reason,
        )
        self.audit.append(
            actor=actor,
            action=f"runtime.incident.{state}",
            target_type="runtime_incident",
            target_id=incident_id,
            request_ip=request_ip,
            reason=reason,
            before=before,
            after=after,
            result="succeeded",
        )
        return after

    def runtime_recovery_action(
        self,
        *,
        actor: dict,
        request_ip: str,
        action_type: str,
        target_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict:
        _require_admin(actor)
        allowed = {"retry_delivery", "archive_delivery", "evaluate_runtime"}
        if action_type not in allowed:
            raise ValueError("runtime recovery action is not allowed")
        target_type = "runtime" if action_type == "evaluate_runtime" else "delivery"
        before = (
            {"status": "requested"}
            if target_type == "runtime"
            else self.service.tasks.get_delivery(target_id)
        )
        action, reused = self.service.runtime_governance.start_recovery_action(
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            actor=actor["username"],
            reason=reason,
            idempotency_key=idempotency_key,
            side_effect_boundary="B0_NO_EFFECT",
            before=before,
        )
        # A repeated request must never execute the recovery body again, including
        # while the original request is still running.
        if reused:
            return action
        try:
            if action_type == "retry_delivery":
                after = self.service.tasks.requeue_delivery(target_id)
            elif action_type == "archive_delivery":
                after = self.service.tasks.archive_delivery(target_id)
            else:
                after = self.service.runtime_governance.evaluate_incidents(
                    task_diagnostics=self.service.tasks.runtime_diagnostics(),
                )
        except Exception as exc:
            finished = self.service.runtime_governance.finish_recovery_action(
                action["action_id"],
                status="failed",
                error_code=exc.__class__.__name__,
            )
            self.audit.append(
                actor=actor,
                action=f"runtime.recovery.{action_type}",
                target_type=target_type,
                target_id=target_id,
                request_ip=request_ip,
                reason=reason,
                before=before,
                after=finished,
                result="failed",
                error=str(exc),
            )
            raise
        finished = self.service.runtime_governance.finish_recovery_action(
            action["action_id"],
            status="succeeded",
            after=after,
        )
        self.audit.append(
            actor=actor,
            action=f"runtime.recovery.{action_type}",
            target_type=target_type,
            target_id=target_id,
            request_ip=request_ip,
            reason=reason,
            before=before,
            after=after,
            result="succeeded",
        )
        return finished

    def coordination(
        self,
        *,
        user_subject: str | None = None,
        task_status: str | None = None,
        limit: int = 200,
    ) -> dict:
        limit = min(max(int(limit), 1), 500)
        diagnostics = self.service.tasks.runtime_diagnostics()
        subjects = [
            item["user_subject"]
            for item in diagnostics.get("users", [])
            if not user_subject or item["user_subject"] == user_subject
        ]
        endpoints: list[dict] = []
        tasks: list[dict] = []
        deliveries: list[dict] = []
        continuations: list[dict] = []
        artifacts: list[dict] = []
        plans: list[dict] = []
        for subject in subjects:
            subject_endpoints = self.service.tasks.list_endpoints(
                user_subject=subject,
                active_only=False,
                limit=500,
            )
            endpoint_index = {
                item["endpoint_id"]: item for item in subject_endpoints
            }
            endpoints.extend(
                self._endpoint_projection(item) for item in subject_endpoints
            )
            subject_tasks = self.service.tasks.list_tasks(
                user_subject=subject,
                limit=limit,
            )
            if task_status:
                subject_tasks = [
                    item for item in subject_tasks
                    if item["status"] == task_status
                ]
            tasks.extend(
                self._task_projection(
                    item,
                    origin_endpoint=endpoint_index.get(item["origin_endpoint_id"]),
                )
                for item in subject_tasks
            )
            deliveries.extend(
                self._delivery_projection(item)
                for item in self.service.tasks.list_outbox(
                    user_subject=subject,
                    limit=limit,
                    newest_first=True,
                )
            )
            continuations.extend(
                self._continuation_projection(
                    item,
                    endpoint=endpoint_index.get(item["endpoint_id"]),
                )
                for item in self.service.tasks.list_continuations(
                    user_subject=subject,
                    limit=limit,
                )
            )
            artifacts.extend(
                self._artifact_projection(item)
                for item in self.service.tasks.list_user_artifacts(
                    user_subject=subject,
                    limit=limit,
                )
            )
        plans.extend(
            self._task_plan_projection(item)
            for item in self.service.task_plans.list(limit=limit)
            if item["user_subject"] in subjects
        )
        tasks.sort(key=lambda item: item["updated_at"], reverse=True)
        plans.sort(key=lambda item: item["updated_at"], reverse=True)
        deliveries.sort(key=lambda item: item["updated_at"], reverse=True)
        continuations.sort(key=lambda item: item["updated_at"], reverse=True)
        artifacts.sort(key=lambda item: item["created_at"], reverse=True)
        deferred_by_endpoint = Counter(
            item["endpoint_id"]
            for item in deliveries
            if item["state"] == "deferred"
        )
        for endpoint in endpoints:
            deferred_count = deferred_by_endpoint.get(endpoint["endpoint_id"], 0)
            endpoint["deferred_delivery_count"] = deferred_count
            endpoint["delivery_state"] = (
                "waiting_activity"
                if deferred_count
                else "ready"
                if endpoint["state"] == "active"
                else "inactive"
            )
        active_endpoint_count = sum(
            1 for item in endpoints if item["state"] == "active"
        )
        pull_endpoint_count = sum(
            1
            for item in endpoints
            if item["state"] == "active" and item["delivery_mode"] == "pull"
        )
        scoped_users = [
            item for item in diagnostics.get("users", [])
            if item["user_subject"] in subjects
        ]
        task_statuses = Counter(
            {
                status: sum(
                    int(item.get("task_statuses", {}).get(status, 0))
                    for item in scoped_users
                )
                for status in {
                    status
                    for item in scoped_users
                    for status in item.get("task_statuses", {})
                }
            }
        )
        outstanding = sum(
            sum(
                int(count)
                for state, count in item.get("outbox_states", {}).items()
                if state in {"pending", "delivering", "deferred"}
            )
            for item in scoped_users
        )
        failed = sum(
            int(item.get("outbox_states", {}).get("failed", 0))
            for item in scoped_users
        )
        deferred = sum(
            int(item.get("outbox_states", {}).get("deferred", 0))
            for item in scoped_users
        )
        return {
            "generated_at": _utc_now(),
            "summary": {
                "users": len(subjects),
                "active_endpoints": active_endpoint_count,
                "pull_endpoints": pull_endpoint_count,
                "direct_endpoints": active_endpoint_count - pull_endpoint_count,
                "active_tasks": sum(
                    task_statuses.get(status, 0)
                    for status in {"active", "waiting_user", "running"}
                ),
                "waiting_tasks": task_statuses.get("waiting_user", 0),
                "active_plans": sum(
                    1
                    for item in plans
                    if item["state"] in {"validated", "running", "waiting_user"}
                ),
                "waiting_plans": sum(
                    1 for item in plans if item["state"] == "waiting_user"
                ),
                "active_continuations": sum(
                    1
                    for item in continuations
                    if item["state"] in {"selected", "awaiting_selection"}
                ),
                "outstanding_deliveries": outstanding,
                "deferred_deliveries": deferred,
                "failed_deliveries": failed,
                "active_host_dispatches": diagnostics.get("summary", {}).get(
                    "active_host_dispatches", 0
                ),
                "waiting_host_dispatches": diagnostics.get("summary", {}).get(
                    "waiting_host_dispatches", 0
                ),
                "acceptance_unknown_dispatches": diagnostics.get(
                    "summary", {}
                ).get("acceptance_unknown_dispatches", 0),
                "ready_artifacts": sum(
                    1 for item in artifacts if item["state"] == "ready"
                ),
                "expired_artifacts": sum(
                    1 for item in artifacts if item["state"] == "expired"
                ),
                "isolation_violations": diagnostics.get("summary", {}).get(
                    "isolation_violation_count", 0
                ),
            },
            "task_statuses": dict(task_statuses),
            "isolation": diagnostics.get("isolation", {}),
            "tasks": tasks[:limit],
            "plans": plans[:limit],
            "endpoints": endpoints[:limit],
            "deliveries": deliveries[:limit],
            "continuations": continuations[:limit],
            "artifacts": artifacts[:limit],
        }

    def users(self) -> list[dict]:
        users = self._users(
            tokens=self.identity_store.list(limit=1000),
            sessions=self.service.sessions.list(limit=1000),
        )
        return [{**user, 'database_grants': self.database_grants.get(user['user_subject'])} for user in users]

    def database_sources(self):
        from bscli.database.sources import Sources
        sources=Sources(self.service.home)
        return {'items':[sources.public(r) for r in sources.list()]}

    def save_database_source(self, *, actor, request_ip, body):
        from bscli.database.sources import Sources
        _require_admin(actor)
        allowed={'source_id','action','expected_revision','reason','config','password'}
        if set(body)-allowed: raise ValueError('数据源请求字段无效')
        sources=Sources(self.service.home)
        sources.migrate()
        result=sources.write(body.get('source_id'),body.get('action'),revision=body.get('expected_revision'),
            actor=actor['username'],reason=body.get('reason'),config=body.get('config'),password=body.get('password'))
        self.audit.append(actor=actor,action='database.source.'+body['action'],target_type='database_source',
            target_id=body['source_id'],request_ip=request_ip,reason=body['reason'],result='succeeded',after={'revision':result['revision'],'state':result['state'],'preflight':result.get('preflight',{}).get('status')})
        return result

    def database_grant_config(self, user_subject: str, source_id='taihua_primary') -> dict:
        if not isinstance(user_subject, str) or not any(user['user_subject'] == user_subject for user in self.users()):
            raise KeyError('中央账号不存在')
        from bscli.database.sources import Sources
        sources=Sources(self.service.home)
        sources.migrate()
        record=sources.get(source_id)
        available=sources.capabilities(record['active'] or record['draft'])
        return {'user_subject': user_subject, 'source_id':source_id, **self.database_grants.get(user_subject,source_id),
                'available': [{'name':name,'description':description,'advanced':name == 'database.free.read'}
                              for name,description in DATABASE_CAPABILITIES.items() if name in available],
                'effective': 'next_call', 'token_reissue_required': False, 'gateway_restart_required': False}

    def skill_config(self, user_subject=None):
        from bscli.core.business_skills import skill_catalog
        if user_subject:
            self.user_grant_config(user_subject)
            return {"user_subject": user_subject, **skill_catalog(self.service, user_subject, include_all=True),
                    **self.service.skills.config("user:" + user_subject)}
        return {**self.service.skills.config("global"), "items": [
            {"id": sid, "name": item["manifest"]["name"], "version": item["manifest"]["version"],
             "default_status": item["manifest"].get("status", "trial")}
            for sid, item in self.service.skills.all_items().items()]}

    def decide_skill(self, *, actor, request_ip, body):
        _require_admin(actor)
        if set(body) - {"request_id", "decision", "reason", "reviewed_tests", "manual_quality_reason"}:
            raise ValueError("审批字段无效")
        return self.service.skill_authoring.decide(actor=actor, **body,
            audit_callback=lambda connection, before, after: self.audit.append(
                actor=actor, action="skills.release.review", target_type="skill_review", target_id=body["request_id"],
                request_ip=request_ip, reason=body["reason"], before=before, after=after,
                result="succeeded", connection=connection))

    def save_skill_config(self, *, actor, request_ip, user_subject, value, expected_revision, reason):
        _require_admin(actor)
        if user_subject is not None:
            self.user_grant_config(user_subject)
        owner = "user:" + user_subject if user_subject else "global"
        saved = self.service.skills.save(owner, value, expected_revision=expected_revision,
            actor=actor["username"], reason=reason,
            audit_callback=lambda connection, before, after: self.audit.append(
                actor=actor, action="skills.config.update", target_type="skill_config", target_id=owner,
                request_ip=request_ip, reason=reason, before=before, after=after, result="succeeded", connection=connection))
        with closing(self.service.skills.connect()) as db:
            rows = db.execute("SELECT t.task_id,t.user_subject FROM skill_task_bindings t JOIN skill_bindings b ON b.binding_id=t.binding_id WHERE b.revoked=1" + (" AND t.user_subject=?" if user_subject else ""), (user_subject,) if user_subject else ()).fetchall()
        cleanup = []
        for row in rows:
            try:
                task = self.service.tasks.get_task(row["task_id"], user_subject=row["user_subject"])
                if task["status"] == "waiting_user":
                    result = self.service.cancel_unsubmitted_task(user_subject=row["user_subject"], task_id=row["task_id"])
                    cleanup.append({"task_id": row["task_id"], "status": result["status"]})
            except (KeyError, ValueError, PermissionError):
                cleanup.append({"task_id": row["task_id"], "status": "guarded_pending_cleanup"})
        return {**saved, "task_cleanup": cleanup}

    def user_grant_config(self, user_subject: str) -> dict:
        if not isinstance(user_subject, str) or not any(
            user["user_subject"] == user_subject for user in self.users()
        ):
            raise KeyError("中央账号不存在")
        saved = self.user_grants.get(user_subject)
        return {
            "user_subject": user_subject,
            "permissions": saved["permissions"] if saved else [],
            "revision": saved["revision"] if saved else 0,
            "authorization_model": "user_grants",
            "configured": saved is not None,
            "available": [
                {"id": item["id"], "label": item["label"],
                 "system": item["id"].split(".", 1)[0],
                 "grantable": True}
                for item in PERMISSIONS.values()
            ],
        }

    def save_user_grants(
        self, *, actor: dict, request_ip: str, user_subject: str,
        permissions: list[str], expected_revision: int, reason: str,
    ) -> dict:
        _require_admin(actor)
        self.user_grant_config(user_subject)
        after = self.user_grants.save(
            user_subject, permissions, expected_revision=expected_revision,
            actor=str(actor.get("username") or actor.get("account_id") or ""), reason=reason,
            audit_callback=lambda connection, before, after: self.audit.append(
                actor=actor, action="user.business_grants.update", target_type="central_user",
                target_id=user_subject, request_ip=request_ip, reason=reason,
                before=before, after=after, result="succeeded", connection=connection,
            ),
        )
        return {**after, "effective": "next_call", "token_reissue_required": False,
                "gateway_restart_required": False}

    def save_database_grants(self, *, actor: dict, request_ip: str, user_subject: str,
                             capabilities: list[str], expected_revision: int, reason: str, source_id='taihua_primary') -> dict:
        _require_admin(actor)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('授权版本无效')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('请填写不超过 1000 字的变更原因')
        before = self.database_grant_config(user_subject,source_id)
        if not isinstance(capabilities,list) or any(not isinstance(n,str) or n not in {x['name'] for x in before['available']} for n in capabilities):
            raise ValueError('此数据源不支持所选能力')
        change_actor = {k:actor.get(k) for k in ('account_id','username','role')}
        after = self.database_grants.set(user_subject, capabilities, expected_revision=expected_revision,
                                        change_actor=change_actor, reason=reason.strip(), source_id=source_id)
        self.audit.append(actor=actor, action='database.grants.update', target_type='central_user',
                          target_id=user_subject, request_ip=request_ip, reason=reason.strip(), result='succeeded',
                          before={k:before[k] for k in ('capabilities','revision')}, after=after)
        return {'user_subject':user_subject, 'source_id':source_id, **after, 'effective':'next_call',
                'token_reissue_required':False, 'gateway_restart_required':False}

    def _users(self, *, tokens: list[dict], sessions: list[dict]) -> list[dict]:
        records: dict[str, dict] = {}
        for token in tokens:
            user = records.setdefault(token["user_subject"], _empty_user(token["user_subject"]))
            user["token_count"] += 1
            if token["state"] == "active" and _parse_time(token["expires_at"]) > datetime.now(timezone.utc):
                user["active_token_count"] += 1
        for session in sessions:
            user = records.setdefault(session["user_subject"], _empty_user(session["user_subject"]))
            for principal in (
                session.get("expected_principal_ref"),
                session.get("downstream_principal_ref"),
            ):
                if principal:
                    user["principal_refs"].add(principal)
            user["principal_bindings"][session["system_id"]] = {
                "expected": session.get("expected_principal_ref"),
                "verified": session.get("downstream_principal_ref"),
            }
            user["sessions"][session["system_id"]] = session["state"]
        return sorted(
            ({**item, "principal_refs": sorted(item["principal_refs"])} for item in records.values()),
            key=lambda item: item["user_subject"],
        )

    def list_tokens(self, *, user_subject: str | None = None, limit: int = 500) -> list[dict]:
        return self.identity_store.list(user_subject=user_subject, limit=limit)

    def issue_token(
        self,
        *,
        actor: dict,
        request_ip: str,
        user_subject: str,
        expected_principal_ref: str | None,
        principal_bindings: dict[str, str] | None = None,
        label: str | None,
        scopes: list[str],
        ttl_hours: int,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        if ttl_hours < 1 or ttl_hours > 90 * 24:
            raise ValueError("token lifetime must be between 1 hour and 90 days")
        if scopes:
            raise ValueError("令牌不再接受 Scope 授权，请在用户业务能力中配置权限")
        grant = self.user_grants.get(user_subject)
        system_ids = {permission.split(".", 1)[0] for permission in grant["permissions"]} if grant else set()
        system_ids.update((principal_bindings or {}).keys())
        system_ids.update(session["system_id"] for session in self.service.sessions.list(user_subject=user_subject))
        if expected_principal_ref:
            system_ids.add("oa")
        try:
            resolved_bindings = (
                self.service.sessions.ensure_principal_bindings(
                    user_subject=user_subject,
                    system_ids=system_ids,
                    principal_bindings=principal_bindings,
                    fallback_principal_ref=expected_principal_ref,
                ) if system_ids else {}
            )
        except SessionPrincipalMismatch as exc:
            raise ValueError(str(exc)) from exc
        token_principal = (
            str(expected_principal_ref or "").strip()
            or resolved_bindings.get("oa")
            or (resolved_bindings[sorted(resolved_bindings)[0]] if resolved_bindings else user_subject)
        )
        issued = self.identity_store.issue(
            user_subject=user_subject,
            expected_principal_ref=token_principal,
            label=label,
            scopes=["agentbridge:connect"],
            ttl_seconds=ttl_hours * 3600,
        )
        secret = issued.pop("token")
        public_issued = {**issued, "principal_bindings": resolved_bindings}
        self.audit.append(
            actor=actor,
            action="mcp.token.issue",
            target_type="mcp_token",
            target_id=issued["token_id"],
            request_ip=request_ip,
            reason=reason,
            result="succeeded",
            after=public_issued,
        )
        return {**public_issued, "token_secret": secret}

    def revoke_token(
        self,
        *,
        actor: dict,
        request_ip: str,
        token_id: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        before = self.identity_store.get(token_id)
        after = self.identity_store.revoke(token_id)
        self.audit.append(
            actor=actor,
            action="mcp.token.revoke",
            target_type="mcp_token",
            target_id=token_id,
            request_ip=request_ip,
            reason=reason,
            before=before,
            after=after,
            result="succeeded",
        )
        return after

    def renew_token(
        self, *, actor: dict, request_ip: str, token_id: str,
        new_expires_at: str, expected_revision: int, request_id: str, reason: str,
    ) -> dict:
        _require_admin(actor)
        after = self.identity_store.renew(
            token_id,
            new_expires_at=new_expires_at,
            expected_revision=expected_revision,
            request_id=request_id,
            actor=str(actor.get("username") or actor.get("account_id") or ""),
            reason=reason,
            audit_callback=lambda connection, before, after: self.audit.append(
                actor=actor, action="mcp.token.renew", target_type="mcp_token",
                target_id=token_id, request_ip=request_ip, reason=reason,
                before={"expires_at": before["expires_at"], "edit_revision": before["edit_revision"]},
                after={"expires_at": after["expires_at"], "edit_revision": after["edit_revision"]},
                result="succeeded", connection=connection,
            ),
        )
        return {**after, "token_reissue_required": False, "gateway_restart_required": False}

    def sessions(self) -> list[dict]:
        return [self._session_projection(item) for item in self.service.sessions.list(limit=1000)]

    def session_events(self, *, session_id: str, limit: int = 100) -> list[dict]:
        self.service.sessions.get(session_id)
        return [
            {
                "event_id": item["event_id"],
                "session_id": item["session_id"],
                "event_type": item["event_type"],
                "source": item["source"],
                "previous_state": item.get("previous_state"),
                "new_state": item.get("new_state"),
                "reason": item.get("reason"),
                "created_at": item["created_at"],
            }
            for item in self.service.sessions.list_events(
                session_id=session_id,
                limit=limit,
            )
        ]

    def invalidate_session(
        self,
        *,
        actor: dict,
        request_ip: str,
        session_id: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        before = self.service.sessions.get(session_id)
        after = self.service.sessions.mark_expired(
            session_id,
            f"Administratively invalidated: {reason}",
            source="admin_invalidate",
        )
        self.service.session_states.delete(session_id)
        public_before = self._session_projection(before)
        public_after = self._session_projection(after)
        self.audit.append(
            actor=actor,
            action="session.invalidate",
            target_type="downstream_session",
            target_id=session_id,
            request_ip=request_ip,
            reason=reason,
            before=public_before,
            after=public_after,
            result="succeeded",
        )
        return public_after

    def rebind_session_principal(
        self,
        *,
        actor: dict,
        request_ip: str,
        session_id: str,
        expected_principal_ref: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        before = self.service.sessions.get(session_id)
        self.service.session_states.delete(session_id)
        after = self.service.sessions.rebind_expected_principal(
            session_id,
            expected_principal_ref=expected_principal_ref,
            reason=reason,
        )
        public_before = self._session_projection(before)
        public_after = self._session_projection(after)
        self.audit.append(
            actor=actor,
            action="session.principal.rebind",
            target_type="downstream_session",
            target_id=session_id,
            request_ip=request_ip,
            reason=reason,
            before=public_before,
            after=public_after,
            result="succeeded",
        )
        return public_after

    def inspect_session(
        self,
        *,
        actor: dict,
        request_ip: str,
        session_id: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        session = self.service.sessions.get(session_id)
        result = self.service.inspect_session(
            user_subject=session["user_subject"],
            system_id=session["system_id"],
        )
        self.audit.append(
            actor=actor,
            action="session.live_check",
            target_type="downstream_session",
            target_id=session_id,
            request_ip=request_ip,
            reason=reason,
            result="succeeded",
            after={
                "status": result.get("status"),
                "status_source": result.get("statusSource"),
                "checked_at": result.get("checkedAt"),
            },
        )
        return result

    def capabilities(self) -> list[dict]:
        policies = self.policies.list(state="paused")
        result = []
        for spec in self.service.registry.list():
            matching = [
                item
                for item in policies
                if item["scope_type"] == "global"
                or (item["scope_type"] == "system" and item["scope_value"] == spec.system)
                or (
                    item["scope_type"] == "capability"
                    and item["scope_value"] == spec.name
                    and item["capability_version"] in {"*", spec.version}
                )
            ]
            result.append({**spec.to_dict(), "paused_by": matching})
        return result

    def pause_policy(
        self,
        *,
        actor: dict,
        request_ip: str,
        scope_type: str,
        scope_value: str,
        capability_version: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        before = next(
            (
                item
                for item in self.policies.list()
                if item["scope_type"] == scope_type
                and item["scope_value"] == ("*" if scope_type == "global" else scope_value)
                and item["capability_version"] == (capability_version if scope_type == "capability" else "*")
            ),
            None,
        )
        after = self.policies.pause(
            scope_type=scope_type,
            scope_value=scope_value,
            capability_version=capability_version,
            reason=reason,
            actor=actor["username"],
        )
        self.audit.append(
            actor=actor,
            action="governance.write.pause",
            target_type="governance_policy",
            target_id=after["policy_id"],
            request_ip=request_ip,
            reason=reason,
            before=before,
            after=after,
            result="succeeded",
        )
        return after

    def resume_policy(
        self,
        *,
        actor: dict,
        request_ip: str,
        policy_id: str,
        reason: str,
    ) -> dict:
        _require_admin(actor)
        before = self.policies.get(policy_id)
        after = self.policies.resume(policy_id, reason=reason, actor=actor["username"])
        self.audit.append(
            actor=actor,
            action="governance.write.resume",
            target_type="governance_policy",
            target_id=policy_id,
            request_ip=request_ip,
            reason=reason,
            before=before,
            after=after,
            result="succeeded",
        )
        return after

    def operations(
        self,
        *,
        user_subject: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        records = self.service.operations.list(
            user_subject=user_subject,
            limit=min(max(limit, 1), 1000),
        )
        if status:
            records = [item for item in records if item["status"] == status]
        return self._operation_projections(records)

    def interactions(
        self,
        *,
        user_subject: str | None = None,
        interaction_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        records = self.service.interactions.list_all(limit=min(max(limit, 1), 500))
        if user_subject:
            records = [item for item in records if item["user_subject"] == user_subject]
        if interaction_type:
            records = [item for item in records if item["interaction_type"] == interaction_type]
        return [self._interaction_projection(item) for item in records]

    def _operation_projections(self, records: list[dict]) -> list[dict]:
        interaction_states = self._operation_interaction_states(
            [
                item["operation_id"]
                for item in records
                if item["status"] == "requires_user_action"
            ]
        )
        return [
            self._operation_projection(
                item,
                interaction=interaction_states.get(item["operation_id"]),
            )
            for item in records
        ]

    def _operation_interaction_states(
        self,
        operation_ids: list[str],
    ) -> dict[str, dict]:
        normalized = list(dict.fromkeys(str(item) for item in operation_ids if item))
        if not normalized:
            return {}
        records: list[sqlite3.Row] = []
        with closing(sqlite3.connect(self.db_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            for offset in range(0, len(normalized), 400):
                chunk = normalized[offset : offset + 400]
                placeholders = ",".join("?" for _ in chunk)
                records.extend(
                    connection.execute(
                        f"""
                        SELECT interaction.operation_id,
                               interaction.interaction_type,
                               interaction.created_at,
                               interaction.expires_at,
                               CASE interaction.interaction_type
                                 WHEN 'credential' THEN challenge.state
                                 WHEN 'business_input' THEN submission.state
                                 WHEN 'execution_authorization' THEN authorization.state
                               END AS resource_state
                        FROM interactions AS interaction
                        LEFT JOIN auth_challenges AS challenge
                          ON interaction.interaction_type = 'credential'
                         AND challenge.challenge_id = interaction.resource_id
                        LEFT JOIN field_submissions AS submission
                          ON interaction.interaction_type = 'business_input'
                         AND submission.submission_id = interaction.resource_id
                        LEFT JOIN write_authorizations AS authorization
                          ON interaction.interaction_type = 'execution_authorization'
                         AND authorization.authorization_id = interaction.resource_id
                        WHERE interaction.operation_id IN ({placeholders})
                        ORDER BY interaction.created_at DESC
                        """,
                        chunk,
                    ).fetchall()
                )
        result: dict[str, dict] = {}
        for row in sorted(records, key=lambda item: item["created_at"], reverse=True):
            result.setdefault(
                row["operation_id"],
                {
                    "interaction_type": row["interaction_type"],
                    "state": row["resource_state"] or "unavailable",
                    "expires_at": row["expires_at"],
                },
            )
        return result

    @staticmethod
    def _operation_projection(
        record: dict,
        *,
        interaction: dict | None = None,
    ) -> dict:
        error = record.get("error") or {}
        effective_status = _effective_operation_status(
            record["status"],
            interaction=interaction,
        )
        return {
            "operation_id": record["operation_id"],
            "request_id": record["request_id"],
            "user_subject": record["user_subject"],
            "capability_name": record["capability_name"],
            "capability_version": record["capability_version"],
            "status": record["status"],
            "effective_status": effective_status,
            "awaiting_user_action": effective_status == "awaiting_user",
            "interaction_type": (
                interaction.get("interaction_type") if interaction else None
            ),
            "interaction_state": interaction.get("state") if interaction else None,
            "error_code": error.get("code"),
            "error_message": error.get("message"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "finished_at": record.get("finished_at"),
        }

    def _interaction_projection(self, record: dict) -> dict:
        resource_state = "unknown"
        try:
            if record["interaction_type"] == "credential":
                resource_state = self.service.challenges.get(record["resource_id"])["state"]
            elif record["interaction_type"] == "business_input":
                resource_state = self.service.field_submissions.get(
                    record["resource_id"], include_values=False
                )["state"]
            elif record["interaction_type"] == "execution_authorization":
                resource_state = self.service.write_authorizations.get(
                    record["resource_id"], include_plan=False
                )["state"]
        except Exception:
            resource_state = "unavailable"
        return {
            "interaction_id": record["interaction_id"],
            "interaction_type": record["interaction_type"],
            "user_subject": record["user_subject"],
            "system_id": record["system_id"],
            "session_id": record["session_id"],
            "operation_id": record.get("operation_id"),
            "title": record["title"],
            "state": resource_state,
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
        }

    def _session_projection(self, record: dict) -> dict:
        status = session_response(
            record,
            activity_lease_seconds=self.service.session_keepalive_lease_seconds,
        )
        latest_event = self.service.sessions.latest_event(record["session_id"])
        return {
            "session_id": record["session_id"],
            "user_subject": record["user_subject"],
            "system_id": record["system_id"],
            "expected_principal_ref": record.get("expected_principal_ref"),
            "downstream_principal_ref": record.get("downstream_principal_ref"),
            "state": record["state"],
            "last_error": record.get("last_error"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "last_verified_at": record.get("last_verified_at"),
            "last_user_activity_at": record.get("last_user_activity_at"),
            "last_keepalive_at": record.get("last_keepalive_at"),
            "keepalive_eligible_until": status.get("keepaliveEligibleUntil"),
            "keepalive_state": status.get("keepaliveState"),
            "keepalive_active": status.get("keepaliveActive"),
            "keepalive_explanation": status.get("keepaliveExplanation"),
            "session_state_basis": status.get("sessionStateBasis"),
            "latest_event": (
                {
                    "event_type": latest_event["event_type"],
                    "source": latest_event["source"],
                    "previous_state": latest_event.get("previous_state"),
                    "new_state": latest_event.get("new_state"),
                    "reason": latest_event.get("reason"),
                    "created_at": latest_event["created_at"],
                }
                if latest_event
                else None
            ),
            "expired_at": record.get("expired_at"),
        }

    @staticmethod
    def _endpoint_projection(record: dict) -> dict:
        client_type = str(record["client_type"])
        return {
            "endpoint_id": record["endpoint_id"],
            "user_subject": record["user_subject"],
            "agent_host": record["agent_host"],
            "client_type": client_type,
            "label": record.get("label"),
            "delivery_mode": (
                "pull" if client_type.lower() in {"web", "webchat"} else "direct"
            ),
            "capabilities": list(record.get("capabilities") or []),
            "state": record["state"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "last_seen_at": record["last_seen_at"],
        }

    @staticmethod
    def _task_projection(
        record: dict,
        *,
        origin_endpoint: dict | None,
    ) -> dict:
        return {
            "task_id": record["task_id"],
            "user_subject": record["user_subject"],
            "agent_host": record["agent_host"],
            "title": record["title"],
            "status": record["status"],
            "origin_endpoint_id": record["origin_endpoint_id"],
            "origin_client_type": (
                origin_endpoint.get("client_type") if origin_endpoint else None
            ),
            "origin_label": origin_endpoint.get("label") if origin_endpoint else None,
            "current_operation_id": record.get("current_operation_id"),
            "current_interaction_id": record.get("current_interaction_id"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "finished_at": record.get("finished_at"),
        }

    @staticmethod
    def _task_plan_projection(record: dict) -> dict:
        risk = record.get("risk_summary") or {}
        temporal = record.get("temporal_context") or {}
        result_projection = record.get("result_projection") or {}
        result = result_projection.get("result") or {}
        coverage = result.get("coverage") or {}
        return {
            "plan_id": record["plan_id"],
            "task_id": record["parent_task_id"],
            "user_subject": record["user_subject"],
            "revision": record["revision"],
            "schema_version": record.get("schema_version"),
            "state": record["state"],
            "current_step_key": record.get("current_step_key"),
            "step_count": len(record.get("steps") or []),
            "step_counts": dict(record.get("step_counts") or {}),
            "systems": list(risk.get("systems") or []),
            "write_sink_count": int(risk.get("writeSinkCount") or 0),
            "effect_outcome": record.get("effect_outcome"),
            "temporal_range": temporal.get("absoluteRange"),
            "result_kind": result_projection.get("kind"),
            "coverage_status": coverage.get("status"),
            "included_count": int(result.get("included_count") or 0),
            "excluded_count": int(result.get("excluded_count") or 0),
            "terminal_reason": _runtime_code(record.get("terminal_reason")),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "finished_at": record.get("finished_at"),
        }

    @staticmethod
    def _delivery_projection(record: dict) -> dict:
        payload = record.get("payload") or {}
        return {
            "delivery_id": record["delivery_id"],
            "task_id": record["task_id"],
            "endpoint_id": record["endpoint_id"],
            "user_subject": record["user_subject"],
            "payload_type": record["payload_type"],
            "event_type": str(payload.get("eventType") or "")[:120] or None,
            "state": record["state"],
            "attempt_count": record["attempt_count"],
            "next_attempt_at": (
                None if record["state"] == "deferred"
                else record["next_attempt_at"]
            ),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "acknowledged_at": record.get("acknowledged_at"),
        }

    @staticmethod
    def _artifact_projection(record: dict) -> dict:
        return {
            "artifact_id": record["artifact_id"],
            "task_id": record["task_id"],
            "user_subject": record["user_subject"],
            "artifact_type": record["artifact_type"],
            "filename": record["filename"],
            "content_type": record["content_type"],
            "byte_size": record["byte_size"],
            "state": record["state"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "expires_at": record["expires_at"],
        }

    @staticmethod
    def _continuation_projection(
        record: dict,
        *,
        endpoint: dict | None,
    ) -> dict:
        return {
            "endpoint_id": record["endpoint_id"],
            "user_subject": record["user_subject"],
            "agent_host": record["agent_host"],
            "client_type": endpoint.get("client_type") if endpoint else None,
            "selected_task_id": record.get("selected_task_id"),
            "candidate_count": len(record.get("candidate_task_ids") or []),
            "state": record["state"],
            "execution_mode": record["execution_mode"],
            "reason": _runtime_code(record.get("reason")),
            "expires_at": record["expires_at"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }


def _empty_user(user_subject: str) -> dict:
    return {
        "user_subject": user_subject,
        "principal_refs": set(),
        "principal_bindings": {},
        "token_count": 0,
        "active_token_count": 0,
        "sessions": {},
    }


def _require_admin(actor: dict) -> None:
    if actor.get("role") != "admin":
        raise PermissionError("administrator role is required")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 120:
        return None
    if any(
        not (character.isascii() and (character.isalnum() or character in "._:-"))
        for character in normalized
    ):
        return None
    return normalized


def _effective_operation_status(
    status: str,
    *,
    interaction: dict | None,
) -> str:
    if status != "requires_user_action":
        return status
    if not interaction:
        return "user_action_handoff"
    resource_state = str(interaction.get("state") or "unavailable")
    if resource_state == "pending":
        expires_at = interaction.get("expires_at")
        if expires_at and _parse_time(str(expires_at)) <= datetime.now(timezone.utc):
            return "user_action_expired"
        return "awaiting_user"
    if resource_state in {"claimed", "processing"}:
        return "awaiting_user"
    return {
        "submitted": "user_action_completed",
        "approved": "user_action_completed",
        "completed": "user_action_completed",
        "consumed": "resumed",
        "expired": "user_action_expired",
        "rejected": "user_action_rejected",
        "declined": "user_action_rejected",
        "superseded": "user_action_superseded",
        "failed": "user_action_failed",
    }.get(resource_state, "user_action_handoff")
