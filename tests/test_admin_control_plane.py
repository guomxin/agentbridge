from __future__ import annotations

from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
from pathlib import Path
import socket
import sqlite3
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bscli.admin.application import AdminControlPlane
from bscli.admin.server import create_admin_http_server, validate_admin_server_config
from bscli.admin.stores import (
    ADMIN_SESSION_IDLE_SECONDS,
    ADMIN_SESSION_TTL_SECONDS,
    AdminAccountStore,
    AdminAuditStore,
    AdminSessionStore,
    GovernancePolicyDenied,
    GovernancePolicyStore,
)
from bscli.core.capability import CapabilityRegistry, CapabilitySpec
from bscli.core.central_service import CentralCapabilityService
from bscli.cli.main import main
from bscli.core.mcp_identities import McpIdentityTokenStore


PASSWORD = "AgentBridge!Admin9"
NEW_PASSWORD = "AgentBridge!Admin10"


class _GatewayDiagnosticsStub:
    def diagnostics(self) -> dict:
        return {
            "configured": True,
            "target": "ws://10.90.20.210:18789",
            "last_attempt_at": "2026-08-09T01:00:00+00:00",
            "last_success_at": "2026-08-09T01:00:01+00:00",
            "last_error_at": None,
            "last_error_code": None,
        }


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class AdminStoreTests(unittest.TestCase):
    def test_admin_session_defaults_allow_seven_days_with_one_day_idle(self) -> None:
        clock = MutableClock()
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            account = AdminAccountStore(db_path, clock=clock).create(
                username="admin", password=PASSWORD, must_change_password=False
            )
            sessions = AdminSessionStore(db_path, clock=clock)
            session = sessions.create(
                account_id=account["account_id"],
                request_ip="127.0.0.1",
                user_agent="test",
            )

            self.assertEqual(sessions.ttl_seconds, 7 * 24 * 60 * 60)
            self.assertEqual(sessions.idle_seconds, 24 * 60 * 60)
            self.assertEqual(sessions.ttl_seconds, ADMIN_SESSION_TTL_SECONDS)
            self.assertEqual(sessions.idle_seconds, ADMIN_SESSION_IDLE_SECONDS)
            self.assertEqual(
                datetime.fromisoformat(session["expires_at"]),
                clock.value + timedelta(days=7),
            )
            clock.value += timedelta(hours=23, minutes=59)
            self.assertIsNotNone(sessions.verify(session["token"]))
            clock.value += timedelta(days=1, seconds=1)
            self.assertIsNone(sessions.verify(session["token"]))

    def test_password_is_hashed_and_first_change_is_enforced(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            store = AdminAccountStore(db_path)
            account = store.create(username="admin", password=PASSWORD)

            self.assertTrue(account["must_change_password"])
            self.assertIsNotNone(store.authenticate(username="ADMIN", password=PASSWORD))
            self.assertIsNone(store.authenticate(username="admin", password="wrong-password"))
            with closing(sqlite3.connect(db_path)) as connection:
                encoded = connection.execute(
                    "SELECT password_hash FROM admin_accounts WHERE account_id = ?",
                    (account["account_id"],),
                ).fetchone()[0]
            self.assertNotIn(PASSWORD, encoded)
            self.assertTrue(encoded.startswith("scrypt$"))

            changed = store.change_password(
                account_id=account["account_id"],
                current_password=PASSWORD,
                new_password=NEW_PASSWORD,
            )
            self.assertFalse(changed["must_change_password"])
            self.assertIsNone(store.authenticate(username="admin", password=PASSWORD))
            self.assertIsNotNone(store.authenticate(username="admin", password=NEW_PASSWORD))
            with self.assertRaises(ValueError):
                store.change_password(
                    account_id=account["account_id"],
                    current_password=NEW_PASSWORD,
                    new_password=NEW_PASSWORD,
                )

    def test_admin_session_requires_matching_csrf_and_honors_idle_timeout(self) -> None:
        clock = MutableClock()
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            account = AdminAccountStore(db_path, clock=clock).create(
                username="admin", password=PASSWORD, must_change_password=False
            )
            sessions = AdminSessionStore(
                db_path,
                clock=clock,
                ttl_seconds=600,
                idle_seconds=120,
            )
            session = sessions.create(
                account_id=account["account_id"],
                request_ip="127.0.0.1",
                user_agent="test",
            )
            self.assertIsNotNone(sessions.verify(session["token"]))
            self.assertIsNone(sessions.verify(session["token"], csrf_token="wrong"))
            self.assertIsNotNone(
                sessions.verify(session["token"], csrf_token=session["csrf_token"])
            )
            clock.value += timedelta(seconds=121)
            self.assertIsNone(sessions.verify(session["token"]))

    def test_admin_audit_is_append_only(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            store = AdminAuditStore(db_path)
            event = store.append(
                actor={"account_id": "a", "username": "admin", "role": "admin"},
                action="test.action",
                target_type="test",
                target_id="one",
                reason="unit test",
                result="succeeded",
                before={"state": "before"},
                after={"state": "after"},
            )
            self.assertEqual(event["after"], {"state": "after"})
            with closing(sqlite3.connect(db_path)) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE admin_audit_events SET result = 'changed' WHERE event_id = ?",
                        (event["event_id"],),
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM admin_audit_events WHERE event_id = ?",
                        (event["event_id"],),
                    )

    def test_governance_policy_matches_global_system_user_and_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            store = GovernancePolicyStore(Path(tmp) / "agentbridge.db")
            system = store.pause(
                scope_type="system",
                scope_value="oa",
                reason="maintenance window",
                actor="admin",
            )
            with self.assertRaises(GovernancePolicyDenied) as caught:
                store.assert_write_allowed(
                    system_id="oa",
                    user_subject="user-a",
                    capability_name="oa.leave.submit",
                    capability_version="1.0.0",
                )
            self.assertEqual(caught.exception.policy["policy_id"], system["policy_id"])
            store.resume(system["policy_id"], reason="maintenance complete", actor="admin")
            store.assert_write_allowed(
                system_id="oa",
                user_subject="user-a",
                capability_name="oa.leave.submit",
                capability_version="1.0.0",
            )


class GovernanceRuntimeTests(unittest.TestCase):
    def test_running_recovery_action_is_reused_without_executing_again(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            first, reused = service.runtime_governance.start_recovery_action(
                action_type="retry_delivery",
                target_type="delivery",
                target_id="delivery-1",
                actor="admin",
                reason="unit test",
                idempotency_key="retry-running-once",
                side_effect_boundary="B0_NO_EFFECT",
            )
            self.assertFalse(reused)
            self.assertEqual(first["status"], "running")

            with patch.object(
                service.tasks,
                "get_delivery",
                return_value={"delivery_id": "delivery-1", "state": "failed"},
            ), patch.object(service.tasks, "requeue_delivery") as requeue:
                second = control.runtime_recovery_action(
                    actor={"account_id": "a", "username": "admin", "role": "admin"},
                    request_ip="127.0.0.1",
                    action_type="retry_delivery",
                    target_id="delivery-1",
                    reason="unit test",
                    idempotency_key="retry-running-once",
                )

            self.assertEqual(second["action_id"], first["action_id"])
            self.assertEqual(second["status"], "running")
            requeue.assert_not_called()

    def test_paused_write_is_recorded_as_write_paused_before_downstream_login(self) -> None:
        registry = CapabilityRegistry()
        registry.register(
            CapabilitySpec(
                name="oa.test.write",
                version="1.0.0",
                description="test write",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object"},
                effect="controlled_write",
                adapter="seeyon-central",
                workflow="test",
            )
        )
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
                registry=registry,
            )
            service.governance_policies.pause(
                scope_type="global",
                scope_value="*",
                reason="emergency stop",
                actor="admin",
            )
            from tests.authorization_fixtures import grant_permissions
            grant_permissions(service.user_grants, "user-a", ["oa.leave.submit"])
            with patch.dict("bscli.core.user_grants.CAPABILITY_PERMISSIONS", {"oa.test.write": "oa.leave.submit"}):
                result = service.invoke(user_subject="user-a", capability_name="oa.test.write", arguments={})

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "WRITE_PAUSED")
            operation = service.operations.get(result["operationId"])
            self.assertEqual(operation["status"], "failed")


class AdminControlPlaneTests(unittest.TestCase):
    def test_runtime_exposes_non_sensitive_coordination_diagnostics(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            identities = McpIdentityTokenStore(service.db_path)
            issued = identities.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                scopes=[],
                ttl_seconds=3600,
            )
            service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id=issued["token_id"],
                agent_host="openclaw",
                endpoint_key="telegram:*:sensitive-peer-uat-9f4c",
                client_type="telegram",
                external_subject="sensitive-peer-uat-9f4c",
                conversation_ref=(
                    "agent:main:telegram:direct:sensitive-peer-uat-9f4c"
                ),
            )

            runtime = AdminControlPlane(
                service=service,
                identity_store=identities,
                workspace_gateway=_GatewayDiagnosticsStub(),
            ).runtime()

        task_hub = runtime["coordination"]["task_hub"]
        self.assertTrue(task_hub["isolation"]["passed"])
        self.assertEqual(task_hub["summary"]["active_endpoints"], 1)
        self.assertEqual(task_hub["users"][0]["user_subject"], "user-a")
        self.assertNotIn("sensitive-peer-uat-9f4c", json.dumps(task_hub))
        self.assertIn("operations", runtime["coordination"]["host_control"])
        self.assertIn("agent_hosts", runtime["coordination"])
        self.assertNotIn("token-a", json.dumps(runtime["coordination"]["agent_hosts"]))
        self.assertEqual(
            runtime["workspace_gateway"]["target"],
            "ws://10.90.20.210:18789",
        )
        self.assertNotIn("gateway-secret", json.dumps(runtime))

    def test_coordination_exposes_only_safe_multichannel_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            identities = McpIdentityTokenStore(service.db_path)
            issued = identities.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                scopes=[],
                ttl_seconds=3600,
            )
            workspace, _ = service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id=issued["token_id"],
                agent_host="openclaw",
                endpoint_key="workspace:secret-account",
                client_type="web",
                external_subject="secret-account",
                conversation_ref="agent:main:web:secret-conversation",
                label="Agent Workspace",
            )
            telegram, _ = service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id=issued["token_id"],
                agent_host="openclaw",
                endpoint_key="openclaw-weixin:*:secret-peer",
                client_type="openclaw-weixin",
                external_subject="secret-peer",
                conversation_ref="agent:main:openclaw-weixin:secret-route",
                label="WeChat",
                capabilities=["direct_status", "trusted_interaction"],
            )
            task, _ = service.tasks.ensure_task(
                user_subject="user-a",
                agent_host="openclaw",
                host_task_key="secret-host-task-key",
                origin_endpoint_id=workspace["endpoint_id"],
                active_conversation_ref="agent:main:web:secret-conversation",
                title="读取 OA 待办",
                summary={"business_secret": "secret-business-field"},
            )
            service.tasks.link_operation(
                task_id=task["task_id"],
                user_subject="user-a",
                operation={
                    "operation_id": "operation-a",
                    "user_subject": "user-a",
                    "capability_name": "oa.workflow.pending.list",
                    "status": "requires_user_action",
                    "error": {"code": "LOGIN_REQUIRED"},
                },
            )
            service.tasks.link_interaction(
                task_id=task["task_id"],
                user_subject="user-a",
                interaction_record={
                    "interaction_id": "interaction-a",
                    "user_subject": "user-a",
                },
                interaction={
                    "interactionId": "interaction-a",
                    "type": "credential",
                    "state": "pending",
                    "url": "https://secret-card.example/input",
                },
            )
            service.tasks.link_artifact(
                task_id=task["task_id"],
                user_subject="user-a",
                artifact={
                    "artifact_type": "certificate_scan",
                    "source_ref": "secret-download-id",
                    "filename": "certificate.pdf",
                    "content_type": "application/pdf",
                    "byte_size": 4096,
                    "download_url": "https://secret-card.example/download/file",
                    "expires_at": "2099-07-30T00:30:00+00:00",
                },
            )
            service.tasks.set_continuation_candidates(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_id=telegram["endpoint_id"],
                candidate_task_ids=[task["task_id"]],
                reason="secret continuation reason",
            )
            plan, _ = service.task_plans.create(
                user_subject="user-a",
                parent_task_id=task["task_id"],
                compiled_plan={
                    "goal": "secret-plan-goal",
                    "planHash": "b" * 64,
                    "riskSummary": {
                        "systems": ["oa", "taihua"],
                        "writeSinkCount": 1,
                    },
                    "steps": [
                        {
                            "stepKey": "read",
                            "ordinal": 1,
                            "kind": "capability",
                            "title": "secret-step-title",
                            "capabilityName": "oa.workflow.done.list",
                            "version": "1",
                            "dependsOn": [],
                            "arguments": {"secret": "secret-plan-argument"},
                            "bindings": {},
                            "effect": "read",
                            "systemId": "oa",
                        }
                    ],
                },
                proposal_source="agent_host",
                coordinator_lease_version=1,
                idempotency_key="admin-plan-1",
            )
            service.task_plans.set_result_projection(
                plan["plan_id"],
                user_subject="user-a",
                projection={
                    "kind": "private_draft",
                    "result": {
                        "draft": "secret-private-plan-draft",
                        "included_count": 3,
                        "excluded_count": 1,
                        "coverage": {"status": "complete"},
                    },
                },
            )
            claimed = service.claim_host_notifications(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="openclaw-weixin:*:secret-peer",
                limit=1,
            )
            service.acknowledge_host_notification(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_key="openclaw-weixin:*:secret-peer",
                delivery_id=claimed["notifications"][0]["deliveryId"],
                succeeded=False,
                defer_until_activity=True,
            )

            result = AdminControlPlane(
                service=service,
                identity_store=identities,
            ).coordination()

        self.assertEqual(result["summary"]["users"], 1)
        self.assertEqual(result["summary"]["active_endpoints"], 2)
        self.assertEqual(result["summary"]["pull_endpoints"], 1)
        self.assertEqual(result["summary"]["direct_endpoints"], 1)
        self.assertEqual(result["summary"]["waiting_tasks"], 1)
        self.assertEqual(result["summary"]["active_plans"], 1)
        self.assertEqual(result["summary"]["active_continuations"], 1)
        self.assertEqual(result["summary"]["ready_artifacts"], 1)
        self.assertGreater(result["summary"]["outstanding_deliveries"], 0)
        self.assertEqual(result["summary"]["deferred_deliveries"], 1)
        self.assertTrue(result["isolation"]["passed"])
        self.assertEqual(
            {item["delivery_mode"] for item in result["endpoints"]},
            {"pull", "direct"},
        )
        telegram_projection = next(
            item
            for item in result["endpoints"]
            if item["delivery_mode"] == "direct"
        )
        self.assertEqual(telegram_projection["delivery_state"], "waiting_activity")
        self.assertEqual(telegram_projection["deferred_delivery_count"], 1)
        self.assertEqual(result["tasks"][0]["title"], "读取 OA 待办")
        self.assertEqual(result["continuations"][0]["candidate_count"], 1)
        self.assertIsNone(result["continuations"][0]["reason"])
        self.assertEqual(result["artifacts"][0]["filename"], "certificate.pdf")
        self.assertEqual(result["plans"][0]["systems"], ["oa", "taihua"])
        self.assertEqual(result["plans"][0]["write_sink_count"], 1)
        self.assertEqual(result["plans"][0]["result_kind"], "private_draft")
        self.assertEqual(result["plans"][0]["coverage_status"], "complete")
        self.assertEqual(result["plans"][0]["included_count"], 3)
        self.assertEqual(result["plans"][0]["excluded_count"], 1)
        self.assertNotIn("download_url", result["artifacts"][0])
        self.assertNotIn("source_ref", result["artifacts"][0])
        serialized = json.dumps(result, ensure_ascii=False)
        for secret in (
            "secret-account",
            "secret-peer",
            "secret-conversation",
            "secret-route",
            "secret-host-task-key",
            "secret-business-field",
            "secret-card.example",
            "secret continuation reason",
            "secret-download-id",
            "secret-plan-goal",
            "secret-step-title",
            "secret-plan-argument",
            "secret-private-plan-draft",
        ):
            self.assertNotIn(secret, serialized)

    def test_coordination_does_not_count_expired_continuations_as_active(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            identities = McpIdentityTokenStore(service.db_path)
            issued = identities.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                scopes=[],
                ttl_seconds=3600,
            )
            endpoint, _ = service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id=issued["token_id"],
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
                client_type="telegram",
                external_subject="1001",
                conversation_ref="agent:main:telegram:direct:1001",
            )
            task, _ = service.tasks.ensure_task(
                user_subject="user-a",
                agent_host="openclaw",
                host_task_key="run-1",
                origin_endpoint_id=endpoint["endpoint_id"],
                active_conversation_ref=endpoint["conversation_ref"],
                title="Read OA pending items",
            )
            service.tasks.select_continuation(
                user_subject="user-a",
                agent_host="openclaw",
                endpoint_id=endpoint["endpoint_id"],
                task_id=task["task_id"],
                execution_mode="follow_up",
            )
            with closing(sqlite3.connect(service.db_path)) as connection:
                connection.execute(
                    "UPDATE task_continuations SET expires_at = ? WHERE endpoint_id = ?",
                    ("2000-01-01T00:00:00+00:00", endpoint["endpoint_id"]),
                )
                connection.commit()

            result = AdminControlPlane(
                service=service,
                identity_store=identities,
            ).coordination()

        self.assertEqual(result["summary"]["active_continuations"], 0)
        self.assertEqual(result["continuations"][0]["state"], "expired")

    def test_operation_effective_status_tracks_the_linked_user_interaction(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            operation, _ = service.operations.create(
                user_subject="user-a",
                capability_name="oa.leave.submit.prepare",
                capability_version="1.0.0",
                input_summary={},
            )
            operation = service.operations.mark_requires_user_action(
                operation["operation_id"],
                code="FIELD_INPUT_REQUIRED",
                message="trusted input is required",
                next_action={},
            )
            submission = service.field_submissions.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                capability_name="oa.leave.submit.prepare",
                capability_version="1.0.0",
                create_operation_id=operation["operation_id"],
                form_schema={
                    "fields": [
                        {"name": "reason", "type": "string", "required": True}
                    ]
                },
                card_base_url="https://agentbridge.example.test",
            )
            service.interactions.register(
                interaction_type="business_input",
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                operation_id=operation["operation_id"],
                resource_id=submission["submission_id"],
                title="填写信息",
                message="complete trusted input",
                display={},
                resume_spec={"kind": "capability"},
                created_at=submission["created_at"],
                expires_at=submission["expires_at"],
            )

            waiting = control.operations()[0]
            self.assertEqual(waiting["status"], "requires_user_action")
            self.assertEqual(waiting["effective_status"], "awaiting_user")
            self.assertTrue(waiting["awaiting_user_action"])
            self.assertEqual(waiting["interaction_state"], "pending")

            with closing(sqlite3.connect(service.db_path)) as connection:
                connection.execute(
                    "UPDATE field_submissions SET state = 'consumed' WHERE submission_id = ?",
                    (submission["submission_id"],),
                )
                connection.commit()
            resumed = control.operations()[0]
            self.assertEqual(resumed["status"], "requires_user_action")
            self.assertEqual(resumed["effective_status"], "resumed")
            self.assertFalse(resumed["awaiting_user_action"])
            self.assertEqual(resumed["interaction_state"], "consumed")

    def test_token_issue_has_no_business_permissions_and_binds_explicit_systems(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
                taihua_base_url="http://127.0.0.1:8001",
            )
            identities = McpIdentityTokenStore(service.db_path)
            control = AdminControlPlane(service=service, identity_store=identities)
            actor = {"account_id": "a", "username": "admin", "role": "admin"}
            issued = control.issue_token(
                actor=actor,
                request_ip="127.0.0.1",
                user_subject="user-a",
                expected_principal_ref="principal-a",
                label="OpenClaw",
                scopes=[],
                principal_bindings={"oa": "principal-a", "taihua": "principal-a"},
                ttl_hours=24,
                reason="client onboarding",
            )

            self.assertTrue(issued["token_secret"].startswith("abmcp_"))
            self.assertEqual(
                set(issued["scopes"]),
                {"agentbridge:connect"},
            )
            self.assertIsNotNone(service.sessions.find(user_subject="user-a", system_id="oa"))
            self.assertIsNotNone(service.sessions.find(user_subject="user-a", system_id="taihua"))
            self.assertNotIn("token_secret", control.list_tokens()[0])
            self.assertNotIn(issued["token_secret"], json.dumps(control.audit.list()))

    def test_admin_renews_original_token_and_changes_user_permissions(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp, base_url="http://127.0.0.1:8000/seeyon",
            )
            identities = McpIdentityTokenStore(service.db_path)
            control = AdminControlPlane(service=service, identity_store=identities)
            actor = {"account_id": "a", "username": "admin", "role": "admin"}
            issued = control.issue_token(
                actor=actor, request_ip="127.0.0.1", user_subject="user-a",
                expected_principal_ref="Alice", label="OpenClaw",
                scopes=[], ttl_hours=1, reason="client onboarding",
            )
            saved = control.save_user_grants(
                actor=actor, request_ip="127.0.0.1", user_subject="user-a",
                permissions=["oa.workflow.read"], expected_revision=0,
                reason="limit to workflow queries",
            )
            self.assertEqual(saved["permissions"], ["oa.workflow.read"])
            renewed = control.renew_token(
                actor=actor, request_ip="127.0.0.1", token_id=issued["token_id"],
                new_expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                expected_revision=0, request_id="renew-admin-1", reason="continue access",
            )
            self.assertEqual(renewed["token_id"], issued["token_id"])
            self.assertEqual(renewed["edit_revision"], 1)
            self.assertFalse(renewed["gateway_restart_required"])
            self.assertNotIn("token_secret", renewed)
            self.assertEqual(identities.verify(issued["token_secret"])["token_id"], issued["token_id"])

    def test_migrated_user_can_hold_identity_token_with_only_database_grants(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp, base_url="http://127.0.0.1:8000/seeyon",
            )
            identities = McpIdentityTokenStore(service.db_path)
            control = AdminControlPlane(service=service, identity_store=identities)
            actor = {"account_id": "a", "username": "admin", "role": "admin"}
            control.issue_token(
                actor=actor, request_ip="127.0.0.1", user_subject="user-a",
                expected_principal_ref="Alice", label="initial", scopes=[],
                ttl_hours=1, reason="initial access",
            )
            control.save_user_grants(
                actor=actor, request_ip="127.0.0.1", user_subject="user-a",
                permissions=[], expected_revision=0, reason="database access only",
            )
            issued = control.issue_token(
                actor=actor, request_ip="127.0.0.1", user_subject="user-a",
                expected_principal_ref=None, label="database client", scopes=[],
                ttl_hours=24, reason="database access",
            )
            self.assertEqual(issued["scopes"], ["agentbridge:connect"])
            self.assertEqual(
                identities.verify(issued["token_secret"])["user_subject"], "user-a",
            )

    def test_token_issue_supports_distinct_system_principal_bindings(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
                taihua_base_url="http://127.0.0.1:8001",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            issued = control.issue_token(
                actor={"account_id": "a", "username": "admin", "role": "admin"},
                request_ip="127.0.0.1",
                user_subject="user-a",
                expected_principal_ref=None,
                principal_bindings={
                    "oa": "Alice OA",
                    "taihua": "alice.worklog",
                },
                label="OpenClaw",
                scopes=[],
                ttl_hours=24,
                reason="multi-system onboarding",
            )

            self.assertEqual(
                issued["principal_bindings"],
                {"oa": "Alice OA", "taihua": "alice.worklog"},
            )
            self.assertEqual(issued["expected_principal_ref"], "Alice OA")
            self.assertEqual(
                service.sessions.find(
                    user_subject="user-a", system_id="oa"
                )["expected_principal_ref"],
                "Alice OA",
            )
            self.assertEqual(
                service.sessions.find(
                    user_subject="user-a", system_id="taihua"
                )["expected_principal_ref"],
                "alice.worklog",
            )
            user = control.users()[0]
            self.assertEqual(
                user["principal_bindings"],
                {
                    "oa": {"expected": "Alice OA", "verified": None},
                    "taihua": {"expected": "alice.worklog", "verified": None},
                },
            )

            rotated = control.issue_token(
                actor={"account_id": "a", "username": "admin", "role": "admin"},
                request_ip="127.0.0.1",
                user_subject="user-a",
                expected_principal_ref=None,
                principal_bindings={},
                label="OpenClaw rotated",
                scopes=[],
                ttl_hours=24,
                reason="credential rotation",
            )
            self.assertEqual(rotated["principal_bindings"], issued["principal_bindings"])

    def test_admin_rebinds_only_one_system_and_clears_its_login_state(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
                taihua_base_url="http://127.0.0.1:8001",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            actor = {"account_id": "a", "username": "admin", "role": "admin"}
            control.issue_token(
                actor=actor,
                request_ip="127.0.0.1",
                user_subject="user-a",
                expected_principal_ref=None,
                principal_bindings={"oa": "Alice OA", "taihua": "alice.worklog"},
                label="OpenClaw",
                scopes=[],
                ttl_hours=24,
                reason="onboarding",
            )
            oa = service.sessions.find(user_subject="user-a", system_id="oa")
            taihua = service.sessions.find(user_subject="user-a", system_id="taihua")
            service.sessions.activate(oa["session_id"], observed_principal_ref="Alice OA")
            service.sessions.activate(
                taihua["session_id"], observed_principal_ref="alice.worklog"
            )
            service.session_states.save(
                taihua["session_id"], {"cookies": [{"owner": "user-a"}]}
            )

            rebound = control.rebind_session_principal(
                actor=actor,
                request_ip="127.0.0.1",
                session_id=taihua["session_id"],
                expected_principal_ref="alice-new",
                reason="account renamed",
            )

            self.assertEqual(rebound["expected_principal_ref"], "alice-new")
            self.assertEqual(rebound["state"], "expired")
            self.assertIsNone(rebound["downstream_principal_ref"])
            self.assertIsNone(service.session_states.load(taihua["session_id"]))
            self.assertEqual(service.sessions.get(oa["session_id"])["state"], "active")
            self.assertEqual(control.audit.list()[0]["action"], "session.principal.rebind")

    def test_session_projection_exposes_keepalive_semantics_and_event_history(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
                session_keepalive_lease_seconds=604_800,
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Alice OA",
            )
            service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Alice OA",
                source="credential_login",
            )
            service.sessions.mark_expired(
                session["session_id"],
                "OA keepalive detected an expired session.",
                source="keepalive",
            )

            projected = control.sessions()[0]
            events = control.session_events(session_id=session["session_id"])

            self.assertEqual(projected["latest_event"]["source"], "keepalive")
            self.assertEqual(projected["latest_event"]["new_state"], "expired")
            self.assertFalse(projected["keepalive_active"])
            self.assertEqual(events[0]["reason"], "OA keepalive detected an expired session.")

    def test_legacy_scope_is_rejected_before_identity_sessions_are_created(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            with self.assertRaises(ValueError):
                control.issue_token(
                    actor={"account_id": "a", "username": "admin", "role": "admin"},
                    request_ip="127.0.0.1",
                    user_subject="user-a",
                    expected_principal_ref="principal-a",
                    label="invalid",
                    scopes=["oa:read"],
                    ttl_hours=24,
                    reason="unit test",
                )

            self.assertIsNone(
                service.sessions.find(user_subject="user-a", system_id="oa")
            )
    def test_auditor_cannot_execute_control_actions(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            with self.assertRaises(PermissionError):
                control.pause_policy(
                    actor={"account_id": "b", "username": "audit", "role": "auditor"},
                    request_ip="127.0.0.1",
                    scope_type="global",
                    scope_value="*",
                    capability_version="*",
                    reason="not allowed",
                )


class AdminHttpServerTests(unittest.TestCase):
    def test_user_grants_and_token_renewal_http_require_admin_and_revision(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(home=tmp, base_url="http://127.0.0.1:1/seeyon")
            identities = McpIdentityTokenStore(service.db_path)
            control = AdminControlPlane(service=service, identity_store=identities)
            issued = control.issue_token(
                actor={"account_id": "a", "username": "admin", "role": "admin"},
                request_ip="127.0.0.1", user_subject="user-a",
                expected_principal_ref="Alice", label="initial", scopes=[],
                ttl_hours=1, reason="initial access",
            )
            for role in ("admin", "auditor"):
                control.accounts.create(
                    username=role, password=PASSWORD, role=role, must_change_password=False,
                )
            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            server = create_admin_http_server(
                control_plane=control,
                config=validate_admin_server_config(
                    host="127.0.0.1", port=port, public_base_url=origin,
                    tls_cert=None, tls_key=None,
                ),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cookies = {}
                for role in ("admin", "auditor"):
                    status, headers, _ = _request(
                        port, "POST", "/api/login", origin=origin,
                        body={"username": role, "password": PASSWORD},
                    )
                    self.assertEqual(status, 200)
                    cookies[role] = _cookies(headers)
                status, _, config = _request(
                    port, "GET", "/api/user-grants?user=user-a",
                    cookies=cookies["auditor"],
                )
                self.assertEqual(status, 200)
                self.assertEqual(config["revision"], 0)
                body = {
                    "user_subject": "user-a", "permissions": ["oa.workflow.read"],
                    "expected_revision": 0, "reason": "workflow only",
                }
                def post(path, payload, role="admin"):
                    return _request(
                        port, "POST", path, origin=origin, cookies=cookies[role],
                        csrf=cookies[role]["agentbridge_admin_csrf"], body=payload,
                    )
                proposal={"name":"周报助手","description":"归纳输入","selection":{"use_when":"周报","not_for":"业务写入","output":"摘要"},"instructions":"根据输入归纳，不编造。"}
                d=service.skill_authoring.save("user-a",proposal=proposal,request_key="http-draft")
                t=service.skill_authoring.test_start("user-a",draft_id=d["draft_id"],expected_revision=1,profile="use",prompt="合成示例",request_key="http-test")
                service.skill_authoring.test_result("user-a",test_id=t["test_id"],output="待复核示例")
                r=service.skill_authoring.submit("user-a",draft_id=d["draft_id"],expected_revision=1,reason="测试",request_key="http-submit")
                decision={"request_id":r["request_id"],"decision":"approve","reason":"已复核","reviewed_tests":True}
                self.assertEqual(post("/api/skill-reviews",decision,"auditor")[0],403)
                self.assertEqual(post("/api/skill-reviews",decision)[0],200)
                status,_,detail=_request(port,"GET","/api/skill-reviews?id="+r["request_id"],cookies=cookies["auditor"])
                self.assertEqual(status,200);self.assertEqual(detail["state"],"published")
                self.assertEqual(post("/api/user-grants", body, "auditor")[0], 403)
                self.assertEqual(post("/api/user-grants", body)[0], 200)
                conflict = post("/api/user-grants", body)
                self.assertEqual(conflict[0], 409)
                self.assertEqual(conflict[2]["error"]["code"], "USER_GRANT_CONFLICT")
                renewed = post(f"/api/tokens/{issued['token_id']}/renew", {
                    "new_expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                    "expected_revision": 0, "request_id": "http-renew-1",
                    "reason": "continued access",
                })
                self.assertEqual(renewed[0], 200)
                self.assertEqual(renewed[2]["token_id"], issued["token_id"])
                self.assertNotIn("token_secret", renewed[2])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_database_grants_http_change_existing_mcp_tokens_immediately(self) -> None:
        from starlette.testclient import TestClient
        from bscli.database.independent import CAPABILITIES, IndependentDatabase
        from bscli.mcp.central import create_central_mcp_server, validate_central_mcp_server_config

        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(home=tmp, base_url="http://127.0.0.1:1/seeyon")
            from tests.database_fixtures import configured_source
            configured_source(tmp)
            identities = McpIdentityTokenStore(service.db_path)
            tokens = {name: identities.issue(user_subject=name, expected_principal_ref="unused")
                      for name in ("guomao", "lishiyu")}
            control = AdminControlPlane(service=service, identity_store=identities)
            for role in ("admin", "auditor"):
                control.accounts.create(username=role, password=PASSWORD, role=role, must_change_password=False)
            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            server = create_admin_http_server(control_plane=control, config=validate_admin_server_config(
                host="127.0.0.1", port=port, public_base_url=origin, tls_cert=None, tls_key=None))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                cookies = {}
                for role in ("admin", "auditor"):
                    status, headers, _ = _request(port, "POST", "/api/login", origin=origin,
                        body={"username": role, "password": PASSWORD})
                    self.assertEqual(status, 200)
                    cookies[role] = _cookies(headers)

                def save(subject, caps, revision, *, role="admin", **overrides):
                    args = {"origin": origin, "cookies": cookies[role],
                            "csrf": cookies[role]["agentbridge_admin_csrf"]}
                    args.update(overrides)
                    return _request(port, "POST", "/api/database-grants", body={
                        "user_subject": subject, "capabilities": caps,
                        "expected_revision": revision, "reason": "dynamic grant acceptance"}, **args)

                self.assertEqual(_request(port, "GET", "/api/database-grants?user=guomao")[0], 401)
                status, _, config = _request(port, "GET", "/api/database-grants?user=guomao", cookies=cookies["auditor"])
                self.assertEqual(status, 200)
                self.assertEqual(config["capabilities"], [])
                self.assertEqual(len(config["available"]), 8)
                self.assertEqual(sum(item["advanced"] for item in config["available"]), 1)
                for kwargs, expected in (({"role": "auditor"}, 403), ({"csrf": "wrong"}, 401), ({"csrf": None}, 401), ({"origin": "https://untrusted.example"}, 403)):
                    self.assertEqual(save("guomao", list(CAPABILITIES), 0, **kwargs)[0], expected)
                self.assertEqual(save("unknown-user", [], 0)[0], 404)
                for caps, revision in ((["unknown"], 0), ([{}], 0), ("database.schema", 0), ([], True), ([], -1)):
                    self.assertEqual(save("guomao", caps, revision)[0], 400)
                self.assertEqual(control.database_grants.get("guomao")["revision"], 0)

                mcp = create_central_mcp_server(service=service, identity_store=identities,
                    config=validate_central_mcp_server_config(host="127.0.0.1", port=8790,
                        public_base_url="http://testserver", tls_cert=None, tls_key=None),
                    auth_card_base_url="http://127.0.0.1:8780")
                with TestClient(mcp.streamable_http_app()) as client, \
                        patch.object(identities, "issue", side_effect=AssertionError("must reuse token")), \
                        patch.object(identities, "revoke", side_effect=AssertionError("must keep token")), \
                        patch.object(IndependentDatabase, "_execute", return_value={
                            "query_id": "fixture", "sql_sha256": "fixture", "truncated": False}) as execute:
                    def call(subject, name, arguments):
                        return client.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                            "Authorization": "Bearer " + tokens[subject]["token"],
                            "MCP-Protocol-Version": "2025-06-18"}, json={"jsonrpc": "2.0", "id": "1",
                            "method": "tools/call", "params": {"name": name, "arguments": arguments}}).json()["result"]["structuredContent"]

                    self.assertEqual(call("guomao", "database_capabilities", {})["sources"], [])
                    for subject in tokens:
                        status, _, saved = save(subject, list(CAPABILITIES), 0)
                        self.assertEqual(status, 200)
                        self.assertFalse(saved["token_reissue_required"])
                        self.assertFalse(saved["gateway_restart_required"])
                        self.assertEqual({item["name"] for item in call(subject, "database_capabilities", {})["sources"][0]["capabilities"]}, set(CAPABILITIES)|{"database.report.download"})
                        for capability in (c for c in CAPABILITIES if c != "database.report.export"):
                            self.assertEqual(call(subject, "database_execute", {"capability": capability, "arguments": {}})["status"], "succeeded")
                    self.assertEqual(save("guomao", [], 0)[0], 409)
                    self.assertEqual(save("guomao", [], 1)[0], 200)
                    count = execute.call_count
                    self.assertEqual(call("guomao", "database_execute", {
                        "capability": "database.free.read", "arguments": {"sql": "select 1"}})["code"], "DATABASE_CAPABILITY_DENIED")
                    self.assertEqual(execute.call_count, count)
                    self.assertEqual(len(call("lishiyu", "database_capabilities", {})["sources"][0]["capabilities"]), 9)
                    self.assertEqual(save("guomao", list(CAPABILITIES), 2)[0], 200)
                    self.assertEqual(len(call("guomao", "database_capabilities", {})["sources"][0]["capabilities"]), 9)

                audit = [item for item in control.audit.list() if item["action"] == "database.grants.update"]
                self.assertEqual(len(audit), 4)
                revoked = next(item for item in audit if item["after"]["capabilities"] == [])
                self.assertEqual(len(revoked["before"]["capabilities"]), 8)
                self.assertEqual(revoked["after"]["revision"], 2)
                with closing(sqlite3.connect(control.database_grants.path)) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM database_audit WHERE capability='database.grants'").fetchone()[0], 4)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_login_cookies_csrf_and_no_secret_retrieval(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            identities = McpIdentityTokenStore(service.db_path)
            control = AdminControlPlane(service=service, identity_store=identities)
            trace, _ = service.runtime_governance.ensure_trace(
                user_subject="user-a",
                request_id="admin-http-test",
                host_type="reference-host",
                request_kind="read",
                system_id="oa",
                capability_name="oa.pending.list",
            )
            incident = service.runtime_governance.upsert_incident(
                rule_id="test_runtime_event",
                severity="P3",
                symptom_code="TEST_SIGNAL",
                actionability="current",
                title="Test runtime event",
                trace_id=trace["trace_id"],
                user_subject="user-a",
            )
            observation, _ = service.runtime_governance.start_observation(
                name="admin-shadow-observation",
                duration_hours=24,
                created_by="admin-test",
            )
            service.runtime_governance.capture_observation_snapshots(force=True)
            account = control.accounts.create(
                username="admin",
                password=PASSWORD,
                must_change_password=False,
            )
            self.assertEqual(account["role"], "admin")
            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            config = validate_admin_server_config(
                host="127.0.0.1",
                port=port,
                public_base_url=origin,
                tls_cert=None,
                tls_key=None,
            )
            server = create_admin_http_server(config=config, control_plane=control)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, _, ready = _request(port, "GET", "/readyz")
                self.assertEqual(status, 200)
                self.assertEqual(ready["status"], "ready")
                status, headers, body = _request(
                    port,
                    "POST",
                    "/api/login",
                    body={"username": "admin", "password": PASSWORD},
                    origin=origin,
                )
                self.assertEqual(status, 200)
                cookies = _cookies(headers)
                self.assertIn("agentbridge_admin_session", cookies)
                self.assertIn("agentbridge_admin_csrf", cookies)
                cookie_headers = " ".join(headers.get_all("Set-Cookie") or [])
                self.assertIn("HttpOnly", cookie_headers)
                self.assertEqual(cookie_headers.count("Max-Age=604800"), 2)

                status, response_headers, session = _request(
                    port,
                    "GET",
                    "/api/session",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(session["account"]["username"], "admin")
                self.assertEqual(session["authorization_model"], "user_grants")
                self.assertNotIn("scopes", session)
                status, _, _ = _request(port, "POST", "/api/tokens",
                    body={"user_subject": "legacy-attempt", "expected_principal_ref": "Alice",
                          "scopes": ["oa:read"], "ttl_hours": 24, "reason": "retired scope rejected"},
                    origin=origin, cookies=cookies, csrf=cookies["agentbridge_admin_csrf"])
                self.assertEqual(status, 400)
                self.assertEqual(control.list_tokens(user_subject="legacy-attempt"), [])
                self.assertEqual(response_headers.get("X-Frame-Options"), "DENY")

                status, _, issued = _request(
                    port,
                    "POST",
                    "/api/tokens",
                    body={
                        "user_subject": "user-a",
                        "expected_principal_ref": "principal-a",
                        "label": "test",
                        "scopes": [],
                        "ttl_hours": 24,
                        "reason": "server test",
                    },
                    origin=origin,
                    cookies=cookies,
                    csrf=cookies["agentbridge_admin_csrf"],
                )
                self.assertEqual(status, 201)
                self.assertTrue(issued["token_secret"].startswith("abmcp_"))

                status, _, listed = _request(
                    port,
                    "GET",
                    "/api/tokens",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertNotIn("token_secret", listed["items"][0])

                status, _, sessions = _request(
                    port,
                    "GET",
                    "/api/sessions",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                session_id = sessions["items"][0]["session_id"]
                status, _, events = _request(
                    port,
                    "GET",
                    f"/api/sessions/{session_id}/events",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertEqual(events["items"][0]["event_type"], "session_created")

                status, _, coordination = _request(
                    port,
                    "GET",
                    "/api/coordination",
                    cookies=cookies,
                )
                self.assertEqual(status, 200)
                self.assertIn("summary", coordination)
                self.assertIn("tasks", coordination)

                status, _, traces = _request(port, "GET", "/api/traces", cookies=cookies)
                self.assertEqual(status, 200)
                self.assertEqual(traces["items"][0]["trace_id"], trace["trace_id"])
                status, _, trace_detail = _request(
                    port, "GET", f"/api/traces/{trace['trace_id']}", cookies=cookies
                )
                self.assertEqual(status, 200)
                self.assertEqual(trace_detail["trace"]["user_subject"], "user-a")
                status, _, governance = _request(
                    port, "GET", "/api/governance", cookies=cookies
                )
                self.assertEqual(status, 200)
                self.assertIn("slo", governance)
                self.assertEqual(
                    governance["observations"][0]["observation_id"],
                    observation["observation_id"],
                )
                status, _, incidents = _request(
                    port, "GET", "/api/incidents", cookies=cookies
                )
                self.assertEqual(status, 200)
                self.assertEqual(incidents["items"][0]["incident_id"], incident["incident_id"])
                status, _, transitioned = _request(
                    port,
                    "POST",
                    f"/api/incidents/{incident['incident_id']}/acknowledge",
                    body={"reason": "HTTP governance test"},
                    origin=origin,
                    cookies=cookies,
                    csrf=cookies["agentbridge_admin_csrf"],
                )
                self.assertEqual(status, 200)
                self.assertEqual(transitioned["state"], "acknowledged")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class AdminBootstrapCliTests(unittest.TestCase):
    def test_bootstrap_password_is_read_from_stdin_and_never_printed(self) -> None:
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            with patch("sys.stdin", io.StringIO(PASSWORD + "\n")):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "admin",
                        "account",
                        "bootstrap",
                        "--username",
                        "admin",
                        "--password-stdin",
                    ]
                )
            output = stdout.getvalue()

            self.assertEqual(exit_code, 0)
            self.assertNotIn(PASSWORD, output)
            payload = json.loads(output)
            self.assertTrue(payload["account"]["must_change_password"])
            self.assertEqual(payload["account"]["role"], "admin")
            self.assertEqual(AdminAccountStore(Path(tmp) / "agentbridge.db").count(), 1)


class AdminFirstLoginHttpTests(unittest.TestCase):
    def test_bootstrap_account_must_change_password_before_overview(self) -> None:
        with TemporaryDirectory() as tmp:
            service = CentralCapabilityService(
                home=tmp,
                base_url="http://127.0.0.1:8000/seeyon",
            )
            control = AdminControlPlane(
                service=service,
                identity_store=McpIdentityTokenStore(service.db_path),
            )
            control.accounts.create(username="admin", password=PASSWORD)
            port = _free_port()
            origin = f"http://127.0.0.1:{port}"
            server = create_admin_http_server(
                config=validate_admin_server_config(
                    host="127.0.0.1",
                    port=port,
                    public_base_url=origin,
                    tls_cert=None,
                    tls_key=None,
                ),
                control_plane=control,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, headers, _ = _request(
                    port,
                    "POST",
                    "/api/login",
                    body={"username": "admin", "password": PASSWORD},
                    origin=origin,
                )
                self.assertEqual(status, 200)
                cookies = _cookies(headers)
                status, _, error = _request(port, "GET", "/api/overview", cookies=cookies)
                self.assertEqual(status, 403)
                self.assertEqual(error["error"]["code"], "PASSWORD_CHANGE_REQUIRED")

                status, _, changed = _request(
                    port,
                    "POST",
                    "/api/account/password",
                    body={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                    origin=origin,
                    cookies=cookies,
                    csrf=cookies["agentbridge_admin_csrf"],
                )
                self.assertEqual(status, 200)
                self.assertFalse(changed["account"]["must_change_password"])
                status, _, overview = _request(port, "GET", "/api/overview", cookies=cookies)
                self.assertEqual(status, 200)
                self.assertIn("summary", overview)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class AdminStaticAssetTests(unittest.TestCase):
    def test_console_icons_are_local_and_packaged(self) -> None:
        import re
        import tomllib
        import xml.etree.ElementTree as ET

        root = Path(__file__).resolve().parents[1]
        static = root / "bscli/admin/static"
        page = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "admin.js").read_text(encoding="utf-8")
        names = set(re.findall(r'/assets/(icon-[a-z-]+\.svg)', page))
        names.update(f"icon-{name}.svg" for name in re.findall(r'icon\("([a-z-]+)"', script))
        self.assertGreater(len(names), 10)
        for name in names:
            with self.subTest(icon=name):
                self.assertTrue((static / name).is_file())
                self.assertTrue(ET.parse(static / name).getroot().tag.endswith("svg"))
        package = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("static/*.svg", package["tool"]["setuptools"]["package-data"]["bscli.admin"])
        self.assertTrue((static / "lucide-LICENSE.txt").is_file())
        self.assertNotIn("https://", page)
        self.assertIn('id="navigation-toggle"', page)
        self.assertIn("signal?.throwIfAborted()", script)

    def test_login_form_survives_async_submit_and_assets_are_csp_clean(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "bscli/admin/static/admin.js").read_text(encoding="utf-8")
        page = (root / "bscli/admin/static/index.html").read_text(encoding="utf-8")
        stylesheet = (root / "bscli/admin/static/admin.css").read_text(encoding="utf-8")

        self.assertIn("const loginForm = event.currentTarget;", script)
        self.assertIn("loginForm.reset();", script)
        self.assertNotIn("event.currentTarget.reset()", script)
        self.assertIn('data-view="coordination"', page)
        self.assertIn('data-view="traces"', page)
        self.assertIn('data-view="incidents"', page)
        self.assertIn("async function renderCoordination()", script)
        self.assertIn("async function renderTraces()", script)
        self.assertIn("async function renderIncidents", script)
        self.assertIn("影子观察", script)
        self.assertIn("governance.observations", script)
        self.assertIn('api("/api/coordination?limit=300")', script)
        self.assertIn("上次确认有效", script)
        self.assertIn("未保活（超期）", script)
        self.assertIn("openSessionHistory", script)
        self.assertIn("data-filter-search", script)
        self.assertNotIn('style="', page)
        self.assertNotIn("font-size: clamp(", stylesheet)
        self.assertNotIn("style='", page)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    origin: str | None = None,
    cookies: dict[str, str] | None = None,
    csrf: str | None = None,
) -> tuple[int, http.client.HTTPMessage, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if origin:
        headers["Origin"] = origin
    if cookies:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
    if csrf:
        headers["X-AgentBridge-CSRF"] = csrf
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, response.headers, parsed
    finally:
        connection.close()


def _cookies(headers: http.client.HTTPMessage) -> dict[str, str]:
    result = {}
    for value in headers.get_all("Set-Cookie") or []:
        pair = value.split(";", 1)[0]
        name, cookie_value = pair.split("=", 1)
        result[name] = cookie_value
    return result


if __name__ == "__main__":
    unittest.main()
