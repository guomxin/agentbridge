"""Real central stores and plan runner; all downstream systems are simulated."""
from tests.authorization_fixtures import authorized_service, grant_permissions
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from bscli.adapters import taihua, smartlight
from bscli.core.capability import CapabilityRegistry
from bscli.core.central_service import CentralCapabilityService
from bscli.core.field_submissions import FieldSubmissionStateError
from bscli.core.mcp_identities import McpIdentityTokenStore
from bscli.core.task_plan_runtime import TaskPlanRuntime
from bscli.core.task_plans import step_idempotency_key
from bscli.core.tasks import TaskNotFound
from tests import test_central_service as central_fixtures
from tests import test_taihua_adapter as taihua_fixtures
from tests import test_task_plan_runtime as runtime_fixtures


class PlanWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        worker = central_fixtures.FakeWorker()
        self.service = authorized_service(
            home=Path(self.temp.name), base_url=central_fixtures.BASE_URL,
            taihua_base_url="http://taihua.example.test", worker_factory=lambda *_: worker,
        )
        self.service._worker_factories_by_system["taihua"] = lambda *_: worker
        for system in ("oa", "taihua"):
            session = self.service.sessions.get_or_create(
                user_subject="user-a", system_id=system, expected_principal_ref="Alice",
            )
            self.service.sessions.activate(session["session_id"], observed_principal_ref="Alice")
            self.service.session_states.save(session["session_id"], {"cookies": []})
        self.write = taihua_fixtures.FakeWriteAdapter()
        self.service._adapters_by_system["taihua"] = self.write
        self.tokens = McpIdentityTokenStore(self.service.db_path)
        self.origin = self.tokens.issue(user_subject="user-a", expected_principal_ref="Alice",
            ttl_seconds=300)
        self.other = self.tokens.issue(user_subject="user-a", expected_principal_ref="Alice",
            )
        self.origin = self.tokens.resolve_client(self.origin["token_id"])
        self.other = self.tokens.resolve_client(self.other["token_id"])
        self.service.set_task_plan_authority_resolver(
            lambda token, scopes: self.tokens.resolve_client(token, required_scopes=scopes))
        self.read_calls = 0

    def new_task(self, key):
        return self.service.ensure_host_task(user_subject="user-a", token_id=self.origin["token_id"],
            agent_host="test-host", host_task_key=key, endpoint_key="audit-endpoint", client_type="web",
            external_subject="user-a", conversation_ref="audit", title="离线组合任务")["task"]["taskId"]

    def read(self, name, _worker, _arguments):
        self.read_calls += 1
        collection = "done" if ".done." in name else "sent"
        return {"collection": collection, "items": [{"affair_id": collection, "title": "离线事项",
            "date": "2026-08-30", "category": "审批"}], "coverage": {
                "status": "complete", "queryApplied": True, "dateBasis": "processed_at" if collection == "done" else "initiated_at",
                "requestedRange": {"start": "2026-08-30", "end": "2026-08-30"},
                "scannedCount": 1, "matchedCount": 1, "hasMore": False, "completionReason": "test",
                "observedAt": "2026-08-31T09:00:00+08:00", "queryHash": "sha256:" + collection}}

    def start(self):
        self.task_id = self.new_task("composed")
        proposal = {"schemaVersion": "agentbridge.task-plan.proposal.v2", "goal": "汇总事项并写日志",
            "constraints": {"temporal": {"kind": "absolute_range", "start": "2026-08-30", "end": "2026-08-30"}},
            "steps": [
                {"stepKey": "done", "kind": "capability", "capabilityName": "oa.workflow.done.list"},
                {"stepKey": "sent", "kind": "capability", "capabilityName": "oa.workflow.sent.list"},
                {"stepKey": "merge", "kind": "transform", "transformName": "merge_work_items.v1",
                 "dependsOn": ["done", "sent"], "bindings": {"sources": {"mode": "many", "items": [
                     {"step": "done", "pointer": ""}, {"step": "sent", "pointer": ""}]}}},
                {"stepKey": "draft", "kind": "transform", "transformName": "work_items_to_log_draft.v2",
                 "dependsOn": ["merge"], "bindings": {"bundle": {"mode": "single", "step": "merge", "pointer": ""}}},
                {"stepKey": "write", "kind": "capability", "capabilityName": "taihua.work_log.create.prepare",
                 "dependsOn": ["draft"], "arguments": {"log_date": "2026-08-30", "hours": 1},
                 "bindings": {"content": {"mode": "single", "step": "draft", "pointer": "/draft"}}},
            ]}
        with patch.object(self.service.adapter, "invoke_capability", side_effect=self.read):
            self.waiting = self.service.prepare_task_plan(user_subject="user-a", task_id=self.task_id,
                proposal=proposal, granted_scopes=self.origin["scopes"], authority_identity=self.origin,
                idempotency_key="plan")
        self.assertEqual(self.waiting["status"], "requires_user_action", self.waiting)
        self.plan_id = self.waiting["plan"]["planId"]
        return self.waiting

    def fields(self):
        submission = self.waiting["nextAction"]["inputSubmissionId"]
        csrf = self.service.field_submissions.issue_csrf(submission)
        self.service.field_submissions.submit(submission, csrf_token=csrf, csrf_cookie=csrf,
            values={"log_date": "2026-08-30", "hours": 1, "content": "离线日志", "project": ""})

    def resume(self, response):
        return self.service.resume_interaction(user_subject="user-a", interaction_id=response["interaction"]["interactionId"])

    def authorize(self):
        self.fields()
        self.authorization = self.resume(self.waiting)
        self.assertEqual(self.authorization["status"], "requires_user_action", self.authorization)
        self.auth_id = self.authorization["nextAction"]["authorizationId"]
        csrf = self.service.write_authorizations.issue_csrf(self.auth_id)
        self.service.write_authorizations.decide(self.auth_id, decision="approve", csrf_token=csrf, csrf_cookie=csrf)

    def cancel_from_other_thread(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(self.service.cancel_task_plan, user_subject="user-a", plan_id=self.plan_id).result(timeout=5)

    def test_plan_details_allow_bound_transforms_but_recheck_source_permissions(self):
        self.start()
        self.service.require_task_result_access(user_subject="user-a", task_id=self.task_id)
        snapshot = self.service.get_host_task_snapshot(user_subject="user-a", agent_host="test-host",
            endpoint_key="audit-endpoint", task_id=self.task_id)
        self.assertEqual(snapshot["plan"]["state"], "waiting_user")
        self.assertIsNotNone(snapshot["interaction"])
        grant_permissions(self.service.user_grants, "user-a", ["taihua.work_log.create"])
        with self.assertRaises(PermissionError):
            self.service.require_task_result_access(user_subject="user-a", task_id=self.task_id)

    def test_cancel_retires_pending_field_card_and_prevents_submission(self):
        self.start()
        submission = self.waiting["nextAction"]["inputSubmissionId"]
        csrf = self.service.field_submissions.issue_csrf(submission)
        self.assertEqual(self.cancel_from_other_thread()["status"], "canceled")
        with self.assertRaises(FieldSubmissionStateError):
            self.service.field_submissions.submit(submission, csrf_token=csrf, csrf_cookie=csrf,
                values={"log_date": "2026-08-30", "hours": 1, "content": "test", "project": ""})
        self.assertEqual(self.service.field_submissions.get(submission)["state"], "superseded")
        observed = self.service.get_interaction(user_subject="user-a", interaction_id=self.waiting["interaction"]["interactionId"])
        self.assertEqual(observed["interaction"]["state"], "superseded")
        self.assertEqual(self.service.tasks.get_task(self.task_id, user_subject="user-a")["status"], "canceled")

    def start_atomic(self):
        self.task_id = self.new_task("atomic")
        self.waiting = self.service.invoke(user_subject="user-a", task_id=self.task_id,
            capability_name="taihua.work_log.create.prepare", arguments={
                "log_date": "2026-08-30", "hours": 1, "content": "独立日志", "project": ""})
        self.assertEqual(self.waiting["status"], "requires_user_action", self.waiting)

    def test_task_cancel_routes_plan_id_and_rejects_foreign_user(self):
        self.start()
        with self.assertRaises(TaskNotFound):
            self.service.cancel_unsubmitted_task(user_subject="user-b", task_id=self.task_id)
        result = self.service.cancel_unsubmitted_task(user_subject="user-a", task_id=self.task_id)
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(self.service.task_plans.get(self.plan_id, user_subject="user-a")["state"], "canceled")

    def test_task_cancel_retires_atomic_field_card_and_is_idempotent(self):
        self.start_atomic()
        submission = self.waiting["nextAction"]["inputSubmissionId"]
        for _ in range(2):
            result = self.service.cancel_unsubmitted_task(user_subject="user-a", task_id=self.task_id)
            self.assertEqual(result["status"], "succeeded")
            self.assertFalse(result["businessWriteOccurred"])
        self.assertEqual(self.service.field_submissions.get(submission)["state"], "superseded")
        with self.assertRaises(FieldSubmissionStateError):
            self.fields()
        self.service.get_interaction(user_subject="user-a", interaction_id=self.waiting["interaction"]["interactionId"])
        self.assertEqual(self.service.tasks.get_task(self.task_id, user_subject="user-a")["status"], "canceled")
        self.assertIsNone(self.write.created_payload)

    def test_task_cancel_refuses_already_submitted_atomic_fields(self):
        self.start_atomic()
        self.fields()
        result = self.service.cancel_unsubmitted_task(user_subject="user-a", task_id=self.task_id)
        self.assertEqual(result["error"]["code"], "TASK_ACTION_ALREADY_STARTED")
        self.assertEqual(self.service.field_submissions.get(self.waiting["nextAction"]["inputSubmissionId"])["state"], "submitted")

    def test_poll_between_resource_retirement_and_task_cancel_keeps_a_terminal_result(self):
        self.start_atomic()
        cancel = self.service.tasks.cancel_task
        def observe_then_cancel(**kwargs):
            self.service.get_interaction(user_subject="user-a",
                interaction_id=self.waiting["interaction"]["interactionId"])
            return cancel(**kwargs)
        with patch.object(self.service.tasks, "cancel_task", side_effect=observe_then_cancel):
            result = self.service.cancel_unsubmitted_task(user_subject="user-a", task_id=self.task_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["businessWriteOccurred"])
        self.assertEqual(self.service.tasks.get_task(self.task_id, user_subject="user-a")["status"], "superseded")
        self.assertIsNone(self.write.created_payload)

    def test_task_cancel_pending_atomic_authorization_but_not_approved(self):
        self.start_atomic()
        self.fields()
        authorization = self.resume(self.waiting)
        auth_id = authorization["nextAction"]["authorizationId"]
        result = self.service.cancel_unsubmitted_task(user_subject="user-a", task_id=self.task_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.service.write_authorizations.get(auth_id)["state"], "superseded")

    def test_task_cancel_does_not_revoke_approved_atomic_authorization(self):
        self.start_atomic()
        self.authorize()
        result = self.service.cancel_unsubmitted_task(user_subject="user-a", task_id=self.task_id)
        self.assertEqual(result["error"]["code"], "TASK_ACTION_ALREADY_STARTED")
        self.assertEqual(self.service.write_authorizations.get(self.auth_id)["state"], "approved")
        self.assertIsNone(self.write.created_payload)

    def test_composed_executor_uses_current_guards_in_the_authorization_transaction(self):
        self.start()
        self.authorize()
        skill_guard = self.service.guard_skill_authorization
        plan_guard = self.service.task_plans.guard_authorization_consumption
        connections = []

        def guard_skill(connection, authorization_id, user_subject):
            self.assertTrue(connection.in_transaction)
            connections.append(connection)
            skill_guard(connection, authorization_id, user_subject)

        def guard_plan(connection, **kwargs):
            self.assertIs(connection, connections[0])
            plan_guard(connection, **kwargs)

        def commit(*_, enter_commit_boundary):
            enter_commit_boundary()
            return {"status": "created", "verification": {"matched": True}}

        with patch.object(self.service, "guard_skill_authorization", side_effect=guard_skill) as skill, \
                patch.object(self.service.task_plans, "guard_authorization_consumption", side_effect=guard_plan) as plan, \
                patch("bscli.core.write_catalog.commit_taihua_work_log_create", side_effect=commit):
            result = self.resume(self.authorization)
        self.assertEqual(result["status"], "succeeded")
        skill.assert_called_once()
        plan.assert_called_once()
        self.assertEqual(self.service.write_authorizations.get(self.auth_id)["state"], "consumed")

    def test_cancel_before_commit_wins_against_real_authorization_consumption(self):
        self.start()
        self.authorize()
        effects = []
        def commit(*_, enter_commit_boundary):
            self.assertEqual(self.cancel_from_other_thread()["status"], "canceled")
            enter_commit_boundary()
            effects.append(True)
        with patch("bscli.core.write_catalog.commit_taihua_work_log_create", side_effect=commit):
            result = self.resume(self.authorization)
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(effects, [])
        self.assertEqual(self.service.write_authorizations.get(self.auth_id)["state"], "superseded")

    def test_cancel_after_commit_is_refused_and_verified_result_is_preserved(self):
        self.start()
        self.authorize()
        def commit(*_, enter_commit_boundary):
            enter_commit_boundary()
            replay = self.service.task_plan_runtime.advance(self.plan_id, user_subject="user-a")
            self.assertEqual(replay["status"], "running")
            canceled = self.cancel_from_other_thread()
            self.assertEqual(canceled["error"]["code"], "PLAN_COMMIT_IN_PROGRESS")
            return {"status": "created", "verification": {"matched": True}}
        with patch("bscli.core.write_catalog.commit_taihua_work_log_create", side_effect=commit):
            result = self.resume(self.authorization)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.service.write_authorizations.get(self.auth_id)["state"], "consumed")
        self.assertEqual(self.cancel_from_other_thread()["status"], "succeeded")

    def test_revoked_origin_token_blocks_fields_before_generating_authorization(self):
        self.start()
        self.fields()
        self.tokens.revoke(self.origin["token_id"])
        result = self.resume(self.waiting)
        self.assertEqual(result["error"]["code"], "PLAN_AUTHORITY_INVALID")
        self.assertIsNone(self.write.created_payload)
        self.assertEqual(self.write.read_count, 0)

    def test_other_valid_write_token_cannot_resume_revoked_origin_authority(self):
        self.start()
        self.authorize()
        self.tokens.revoke(self.origin["token_id"])
        scopes = self.service.interaction_required_scopes(user_subject="user-a",
            interaction_id=self.authorization["interaction"]["interactionId"])
        self.tokens.resolve_client(self.other["token_id"], required_scopes=set(scopes))
        result = self.resume(self.authorization)
        self.assertEqual(result["error"]["code"], "PLAN_AUTHORITY_INVALID")
        self.assertIsNone(self.write.created_payload)
        self.assertEqual(self.service.write_authorizations.get(self.auth_id)["state"], "superseded")

    def test_origin_expiry_and_user_permission_reduction_block_authorization_resume(self):
        for condition in ("expired", "reduced"):
            with self.subTest(condition=condition):
                case = PlanWriteBoundaryTests()
                case.setUp()
                try:
                    case.start()
                    case.authorize()
                    if condition == "expired":
                        case.tokens.clock = lambda: datetime.now(timezone.utc) + timedelta(seconds=301)
                    else:
                        grant_permissions(case.service.user_grants, "user-a", ["taihua.work_log.create"])
                    result = case.resume(case.authorization)
                    self.assertEqual(result["error"]["code"], "PLAN_AUTHORITY_INVALID")
                    self.assertIsNone(case.write.created_payload)
                finally:
                    case.doCleanups()

    def test_revocation_during_adapter_precheck_is_rechecked_at_commit_boundary(self):
        self.start()
        self.authorize()
        def commit(*_, enter_commit_boundary):
            self.tokens.revoke(self.origin["token_id"])
            enter_commit_boundary()
            self.fail("revoked authority crossed the effect boundary")
        with patch("bscli.core.write_catalog.commit_taihua_work_log_create", side_effect=commit):
            result = self.resume(self.authorization)
        self.assertEqual(result["error"]["code"], "PLAN_AUTHORITY_INVALID")
        self.assertNotEqual(self.service.write_authorizations.get(self.auth_id)["state"], "consumed")
        self.assertIsNone(self.service.task_plans.get(self.plan_id, user_subject="user-a")["commit_operation_id"])

    def test_commit_marker_rolls_back_with_failed_authorization_transaction(self):
        self.start()
        self.authorize()
        auth = self.service.write_authorizations.get(self.auth_id)
        def guard(connection):
            self.service.task_plans.guard_authorization_consumption(connection, authorization_id=self.auth_id,
                user_subject="user-a", operation_id="rollback-operation", validate=self.service.validate_task_plan_execution)
            raise RuntimeError("injected storage failure")
        with self.assertRaisesRegex(RuntimeError, "injected storage failure"):
            self.service.write_authorizations.consume(self.auth_id, user_subject="user-a", system_id="taihua",
                session_id=auth["session_id"], capability_name=auth["capability_name"], capability_version=auth["capability_version"],
                commit_operation_id="rollback-operation", before_consume=guard)
        self.assertEqual(self.service.write_authorizations.get(self.auth_id)["state"], "approved")
        self.assertIsNone(self.service.task_plans.get(self.plan_id, user_subject="user-a")["commit_operation_id"])
        self.assertEqual(self.cancel_from_other_thread()["status"], "canceled")

    def test_unhandled_post_boundary_error_enters_unknown_and_never_replays(self):
        self.start()
        self.authorize()
        def commit(*_, enter_commit_boundary):
            enter_commit_boundary()
            raise ConnectionError("response lost")
        with patch("bscli.core.write_catalog.commit_taihua_work_log_create", side_effect=commit) as call:
            result = self.resume(self.authorization)
            self.assertEqual(result["status"], "outcome_unknown")
            self.assertEqual(result["error"]["code"], "RESULT_UNKNOWN")
            self.resume(self.authorization)
            self.service.recover_task_plans()
            self.assertEqual(call.call_count, 1)

    def test_restart_reconciles_committed_operation_without_replaying_write(self):
        for terminal in (False, True):
            with self.subTest(verified=terminal):
                case = PlanWriteBoundaryTests()
                case.setUp()
                try:
                    case.start()
                    case.authorize()
                    service = case.service
                    auth = service.write_authorizations.get(case.auth_id)
                    operation, _ = service.operations.create(user_subject="user-a", capability_name=auth["capability_name"],
                        capability_version=auth["capability_version"], input_summary={})
                    operation_id = operation["operation_id"]
                    service.operations.mark_running(operation_id)
                    service.write_authorizations.consume(case.auth_id, user_subject="user-a", system_id="taihua",
                        session_id=auth["session_id"], capability_name=auth["capability_name"],
                        capability_version=auth["capability_version"], commit_operation_id=operation_id,
                        before_consume=lambda conn: service.task_plans.guard_authorization_consumption(conn,
                            authorization_id=case.auth_id, user_subject="user-a", operation_id=operation_id,
                            validate=service.validate_task_plan_execution))
                    if terminal:
                        service.operations.mark_succeeded(operation_id, {"status": "created", "verification": {"matched": True}})
                    service.task_plan_runtime = TaskPlanRuntime(service=service, plans=service.task_plans, transforms=service.transforms)
                    service.recover_task_plans()
                    state = service.task_plans.get(case.plan_id, user_subject="user-a")["state"]
                    self.assertEqual(state, "succeeded" if terminal else "outcome_unknown")
                    self.assertIsNone(case.write.created_payload)
                finally:
                    case.doCleanups()

    def test_independent_identical_read_plans_get_distinct_operations(self):
        operations = []
        proposal = {"schemaVersion": "agentbridge.task-plan.proposal.v1", "goal": "查询待办",
            "steps": [{"stepKey": "read", "kind": "capability", "capabilityName": "oa.workflow.pending.list"}]}
        with patch.object(self.service.adapter, "invoke_capability", return_value={"items": []}) as read:
            for index in (1, 2):
                task_id = self.new_task(f"read-{index}")
                values = dict(user_subject="user-a", task_id=task_id, proposal=proposal,
                    granted_scopes={"oa:read"}, idempotency_key="request")
                result = self.service.prepare_task_plan(**values)
                self.assertEqual(result["status"], "succeeded")
                replay = self.service.prepare_task_plan(**values)
                self.assertEqual(replay["plan"]["planId"], result["plan"]["planId"])
                plan = self.service.task_plans.get(result["plan"]["planId"], user_subject="user-a")
                operations.append(plan["steps"][0]["operation_id"])
            self.assertEqual(read.call_count, 2)
        self.assertNotEqual(*operations)

    def test_source_version_change_while_waiting_stops_before_write(self):
        self.start()
        self.authorize()
        registry = CapabilityRegistry()
        for spec in self.service.registry.list():
            registry.register(replace(spec, version="9.0.0") if spec.name == "oa.workflow.done.list" else spec)
        self.service.registry = registry
        result = self.resume(self.authorization)
        self.assertEqual(result["error"]["code"], "PLAN_VERSION_MISMATCH")
        self.assertIsNone(self.write.created_payload)

    def test_v1_mcp_plan_also_binds_current_authority(self):
        task_id = self.new_task("v1")
        proposal = {"schemaVersion": "agentbridge.task-plan.proposal.v1", "goal": "读取待办",
            "steps": [{"stepKey": "read", "kind": "capability", "capabilityName": "oa.workflow.pending.list"}]}
        self.tokens.revoke(self.origin["token_id"])
        with patch.object(self.service.adapter, "invoke_capability") as read:
            response = self.service.prepare_task_plan(user_subject="user-a", task_id=task_id,
                proposal=proposal, granted_scopes=self.origin["scopes"], authority_identity=self.origin)
        self.assertEqual(response["error"]["code"], "PLAN_AUTHORITY_INVALID")
        read.assert_not_called()


class AdapterPostBoundaryTests(unittest.TestCase):
    def test_submit_and_readback_transport_errors_are_unknown(self):
        for kind in ("taihua", "smartlight"):
            for phase in ("submit", "readback"):
                with self.subTest(kind=kind, phase=phase):
                    adapter = MagicMock()
                    if kind == "taihua":
                        fn, error = taihua.commit_taihua_work_log_create, taihua.TaihuaWorkLogOutcomeUnknown
                        plan = {"schema_version": "agentbridge.taihua_work_log_create_plan.v1",
                            "exact_input": {"log_date": "2026-09-05", "hours": 1, "content": "audit"}}
                        adapter.work_logs_for_date.side_effect = [[], ConnectionError("readback")]
                        adapter.create_work_log.return_value = {"id": "fake"}
                        if phase == "submit":
                            adapter.create_work_log.side_effect = ConnectionError("submit")
                    else:
                        fn, error = smartlight.commit_smartlight_alarm_remark_update, smartlight.SmartlightAlarmRemarkOutcomeUnknown
                        plan = {"schema_version": "agentbridge.smartlight_alarm_remark_update_plan.v1",
                            "exact_input": {"alarm_id": "fake", "remark": "new"}, "exact_payload": {},
                            "target": {"rtuId": "rtu"}, "preconditions": {"previous_remark": "old"}}
                        adapter.alarm_remark_snapshot.side_effect = [{"rtuId": "rtu", "remark": "old"}, ConnectionError("readback")]
                        if phase == "submit":
                            adapter.save_alarm_remark.side_effect = ConnectionError("submit")
                    boundary = MagicMock()
                    with self.assertRaises(error):
                        fn(adapter, object(), plan, enter_commit_boundary=boundary)
                    boundary.assert_called_once()


class PlanVersionAndMigrationTests(unittest.TestCase):
    def setUp(self):
        self.case = runtime_fixtures.TaskPlanRuntimeTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)

    def test_changed_version_is_rejected_before_execution_and_on_recovery(self):
        case = self.case
        plan = case.create_plan(include_sink=False)
        registry = CapabilityRegistry()
        for spec in case.service.registry.list():
            registry.register(replace(spec, version="2.0.0") if spec.name == "source.items" else spec)
        case.service.registry = registry
        case.runtime.recover()
        self.assertEqual(case.plans.get(plan["plan_id"], user_subject="user-1")["terminal_reason"], "PLAN_VERSION_MISMATCH")
        self.assertEqual(case.service.source_calls, 0)

    def test_legacy_inflight_operation_key_is_preserved_after_migration(self):
        case = self.case
        plan = case.create_plan(include_sink=False)
        with case.plans._connect() as connection:
            connection.execute("UPDATE task_plans SET operation_key_prefix = NULL WHERE plan_id = ?", (plan["plan_id"],))
        plan = case.plans.get(plan["plan_id"], user_subject="user-1")
        step = case.plans.begin_next_step(plan["plan_id"], user_subject="user-1")
        expected_key = f"task-plan:{plan['plan_hash']}:{step['step_key']}:attempt:1"
        self.assertEqual(step_idempotency_key(plan, step), expected_key)
        old = case.service.invoke(user_subject="user-1", capability_name="source.items", arguments={}, idempotency_key=expected_key)
        runtime = TaskPlanRuntime(service=case.service, plans=case.plans, transforms=case.transforms)
        runtime.recover()
        stored = case.plans.get(plan["plan_id"], user_subject="user-1")
        self.assertEqual(stored["state"], "succeeded")
        self.assertEqual(stored["steps"][0]["operation_id"], old["operationId"])
        self.assertEqual(case.service.source_calls, 1)

    def test_unavailable_frozen_transform_stops_before_any_business_read(self):
        case = self.case
        plan = case.create_plan(include_sink=False)
        name = plan["steps"][1]["transform_name"]
        case.transforms._specs.pop(name)
        response = case.runtime.start(plan["plan_id"], user_subject="user-1")
        self.assertEqual(response["error"]["code"], "PLAN_VERSION_MISMATCH")
        self.assertEqual(case.service.source_calls, 0)
