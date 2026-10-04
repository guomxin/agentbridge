from tests.authorization_fixtures import authorized_service
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import MagicMock, patch

from bscli.adapters.seeyon_business_trip import BusinessTripOutcomeUnknown
from bscli.adapters.smartlight import (
    SMARTLIGHT_ALARM_REMARK_FIELD_CARD_SCHEMA,
    SmartlightBusinessRuleRejected,
)
from bscli.adapters.seeyon_business_trip_submit import (
    BusinessTripBusinessValidationRequired,
    BusinessTripSubmissionBlocked,
)
from bscli.adapters.seeyon_leave_submit import LeaveBusinessValidationRequired
from bscli.adapters.seeyon_meeting import MEETING_FIELD_CARD_SCHEMA
from bscli.adapters.seeyon_meeting_room_application import (
    MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA,
)
from bscli.adapters.seeyon_pending_actions import PendingActionContractMismatch
from bscli.adapters.seeyon_central import (
    SeeyonLoginRequired,
    SeeyonSessionCheckUnavailable,
)
from bscli.core.central_service import (
    CentralCapabilityService,
    _task_notification_message,
    capability_required_scopes,
    session_response,
)
from bscli.core.interactions import InteractionNotFound
from bscli.core.operations import OperationStore
from bscli.core.session_secrets import SessionStateAccessDenied
from bscli.core.tasks import TaskIntegrityError, TaskNotFound
from tests.test_smartlight_adapter import FakeSmartlightWorker


BASE_URL = "http://oa.example.test/seeyon/main.do?method=main"
SMARTLIGHT_ACTION_CASES = (
    ("smartlight.alarm.work_area.submit", "/rHisHitchAlarm/updateIsSubmitWorkArea",
     "isSubmitWorkArea", 1),
    ("smartlight.alarm.work_area.revoke", "/rHisHitchAlarm/cancleSubmitWorkArea",
     "isSubmitWorkArea", 0),
    ("smartlight.alarm.dispose", "/rHisHitchAlarm/setRtuConductStatusDisposed",
     "conductStatue", 3),
)


class CentralCapabilityServiceTests(unittest.TestCase):
    def test_task_notifications_use_business_labels(self):
        self.assertEqual(
            _task_notification_message(
                {"title": "Prepare OA Missed-Punch Approval", "status": "succeeded"},
                {"eventType": "task.operation.succeeded"},
            ),
            "OA 补签申请审批：已完成。",
        )

    def test_host_call_context_rejects_cross_user_and_cross_host_endpoints(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, MagicMock())
            owned = service.ensure_host_task(
                user_subject="user-a",
                token_id="token-a",
                agent_host="reference-host",
                host_task_key="reference-task-a",
                endpoint_key="reference:endpoint:a",
                client_type="web",
                external_subject="user-a",
                conversation_ref="reference:conversation:a",
                title="Reference task",
            )
            foreign_user, _ = service.tasks.ensure_endpoint(
                user_subject="user-b",
                token_id="token-b",
                agent_host="reference-host",
                endpoint_key="reference:endpoint:b",
                client_type="web",
                external_subject="user-b",
                conversation_ref="reference:conversation:b",
            )
            foreign_host, _ = service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id="token-a",
                agent_host="openclaw",
                endpoint_key="openclaw:endpoint:a",
                client_type="web",
                external_subject="user-a",
                conversation_ref="openclaw:conversation:a",
            )
            registration = {
                "agentHost": "reference-host",
                "hostInstanceId": "reference-host-test",
            }

            with self.assertRaises(TaskNotFound):
                service.validate_host_call_context(
                    user_subject="user-a",
                    registration=registration,
                    endpoint_id=foreign_user["endpoint_id"],
                )
            with self.assertRaises(TaskIntegrityError):
                service.validate_host_call_context(
                    user_subject="user-a",
                    registration=registration,
                    endpoint_id=foreign_host["endpoint_id"],
                )
            valid = service.validate_host_call_context(
                user_subject="user-a",
                registration=registration,
                task_id=owned["task"]["taskId"],
                endpoint_id=owned["endpoint"]["endpointId"],
            )
            self.assertEqual(
                owned["task"]["taskId"],
                valid["task"]["task_id"],
            )

    def test_unmapped_write_scope_policy_fails_closed(self):
        with self.assertRaisesRegex(KeyError, "no MCP scope policy"):
            capability_required_scopes("oa.future.unmapped_write")

    def test_planning_gate_blocks_a_second_source_or_sink_after_business_source(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, MagicMock())
            task_id = self._ensure_host_task(service)
            unrelated_task_id = service.ensure_host_task(
                user_subject="user-a",
                token_id="token-a",
                agent_host="test-host",
                host_task_key="unrelated-task",
                endpoint_key="batch-test-endpoint",
                client_type="web",
                external_subject="user-a",
                conversation_ref="batch-test-conversation",
                title="另一个任务",
            )["task"]["taskId"]
            spec = service.registry.get("oa.workflow.done.list")
            operation, _ = service.operations.create(
                user_subject="user-a",
                capability_name=spec.name,
                capability_version=spec.version,
                input_summary={},
                input_identity={},
                idempotency_key="source-for-planning-gate",
            )
            service.operations.mark_running(operation["operation_id"])
            operation = service.operations.mark_succeeded(
                operation["operation_id"], {"items": []}
            )
            service.tasks.link_operation(
                task_id=task_id,
                user_subject="user-a",
                operation=operation,
            )

            blocked = service.planning_gate_for_call(
                user_subject="user-a",
                task_id=task_id,
                capability_name="taihua.work_log.create.prepare",
                host_type="openclaw",
            )
            second_source = service.planning_gate_for_call(
                user_subject="user-a",
                task_id=task_id,
                capability_name="oa.workflow.sent.list",
                host_type="openclaw",
            )
            unrelated = service.planning_gate_for_call(
                user_subject="user-a",
                task_id=unrelated_task_id,
                capability_name="taihua.work_log.create.prepare",
                host_type="openclaw",
            )
            internal = service.planning_gate_for_call(
                user_subject="user-a",
                task_id=task_id,
                capability_name="taihua.work_log.create.prepare",
                host_type="task_plan",
            )

            self.assertEqual(blocked["error"]["code"], "PLAN_REQUIRED")
            self.assertIn("不向OA或其他业务系统写入", second_source["error"]["message"])
            self.assertIn("无写入步骤", second_source["error"]["message"])
            self.assertIn("业务写入仍须单独授权", blocked["error"]["message"])
            self.assertEqual(
                blocked["nextAction"],
                {
                    "type": "tool_sequence",
                    "tool": "agentbridge_task_plan_catalog",
                    "then": "agentbridge_task_plan_prepare",
                    "doNotRetryCapability": True,
                },
            )
            self.assertEqual(
                blocked["recovery"]["requiredProposalShape"],
                {
                    "schemaVersion": "agentbridge.task-plan.proposal.v2",
                    "readOnlyTerminal": "catalog_result_projection_transform",
                    "bindEveryBusinessSource": True,
                },
            )
            self.assertEqual(
                second_source["error"]["code"],
                "PLAN_REQUIRED",
            )
            self.assertIn("多个业务来源", second_source["error"]["message"])
            self.assertIsNone(unrelated)
            self.assertIsNone(internal)

    def test_host_preflight_checks_terminal_turn_before_task_or_lease_creation(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, MagicMock())
            task_id = self._ensure_host_task(service)
            spec = service.registry.get("oa.workflow.done.list")
            operation, _ = service.operations.create(
                user_subject="user-a", capability_name=spec.name,
                capability_version=spec.version, input_summary={}, input_identity={},
            )
            service.operations.mark_running(operation["operation_id"])
            operation = service.operations.mark_succeeded(operation["operation_id"], {"items": []})
            service.tasks.link_operation(task_id=task_id, user_subject="user-a", operation=operation)
            args = dict(user_subject="user-a", token_id="token-a", agent_host="test-host",
                host_task_key="batch-test-run", endpoint_key="batch-test-endpoint", client_type="web",
                external_subject="user-a", conversation_ref="batch-test-conversation", title="test")
            for tool, scope in (("oa_workflow_sent_list", "user_turn"),
                                ("taihua_work_log_create_prepare", "independent"),
                                ("agentbridge_task_plan_prepare", "user_turn")):
                with self.subTest(tool=tool):
                    request = dict(args, tool_name=tool, task_scope=scope, planning_task_key="batch-test-run")
                    if scope == "independent":
                        request["host_task_key"] = "independent-write"
                    with patch.object(service.tasks, "ensure_task", wraps=service.tasks.ensure_task) as ensure:
                        result = service.ensure_host_task(**request)
                    self.assertEqual(result["error"]["code"], "PLAN_REQUIRED")
                    self.assertEqual(result["taskId"], task_id)
                    ensure.assert_not_called()
            self.assertEqual(service.tasks.get_task(task_id, user_subject="user-a")["status"], "succeeded")
            repair = service.ensure_host_task(**dict(args, host_task_key="batch-test-run:plan-repair",
                task_scope="independent", tool_name="agentbridge_task_plan_prepare",
                planning_task_key="batch-test-run"))
            self.assertNotEqual(repair["task"]["taskId"], task_id)
            clean = service.ensure_host_task(**dict(args, host_task_key="next-turn",
                tool_name="taihua_work_log_create_prepare", planning_task_key="next-turn"))
            self.assertIn("task", clean)
            foreign = service.ensure_host_task(**dict(args, user_subject="user-b", token_id="token-b",
                endpoint_key="endpoint-b", external_subject="user-b", conversation_ref="conversation-b",
                tool_name="taihua_work_log_create_prepare"))
            self.assertIn("task", foreign)

    def test_parallel_business_sources_are_serialized_and_second_is_redirected(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, MagicMock())
            task_id = self._ensure_host_task(service)
            first_entered = threading.Event()
            release_first = threading.Event()

            def invoke_adapter(*, capability_name, **_kwargs):
                if capability_name == "oa.workflow.done.list":
                    first_entered.set()
                    self.assertTrue(release_first.wait(timeout=3))
                return {"items": [], "coverage": {"status": "complete"}}

            def invoke(name):
                return service.invoke(
                    user_subject="user-a",
                    capability_name=name,
                    arguments={},
                    task_id=task_id,
                    host_type="openclaw",
                )

            with patch.object(service, "_invoke_adapter", side_effect=invoke_adapter):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(invoke, "oa.workflow.done.list")
                    self.assertTrue(first_entered.wait(timeout=3))
                    second = executor.submit(invoke, "oa.workflow.sent.list")
                    time.sleep(0.05)
                    self.assertFalse(second.done())
                    release_first.set()
                    first_result = first.result(timeout=3)
                    second_result = second.result(timeout=3)

            self.assertEqual(first_result["status"], "succeeded")
            self.assertEqual(second_result["status"], "planning_control")
            self.assertEqual(
                second_result["error"]["code"],
                "PLAN_REQUIRED",
            )

    def test_smartlight_alarm_remark_uses_field_authorization_and_commit_chain(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = authorized_service(
                home=Path(tmp),
                base_url=BASE_URL,
                smartlight_base_url=(
                    "http://123.232.113.241:4101/smartlight"
                ),
                smartlight_allow_insecure_http=True,
            )
            service._worker_factories_by_system["smartlight"] = (  # noqa: SLF001
                lambda _session, _adapter: worker
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="smartlight",
                expected_principal_ref="无为",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="无为",
            )
            service.session_states.save(session["session_id"], {"cookies": []})

            started = service.invoke(
                user_subject="user-a",
                capability_name="smartlight.alarm.remark.update.prepare",
                arguments={"alarm_id": "alarm-1", "remark": "现场已复核"},
            )
            self.assertEqual(started["error"]["code"], "FIELD_INPUT_REQUIRED")
            submission_id = started["nextAction"]["inputSubmissionId"]
            submission = service.field_submissions.get(submission_id)
            self.assertEqual(
                submission["form_schema"]["fields"][0]["value"],
                "现场已复核",
            )
            self.assertEqual(
                submission["form_schema"]["_agentbridge_resume_arguments"],
                {"alarm_id": "alarm-1"},
            )
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=started["interaction"]["interactionId"],
                ),
                frozenset({"smartlight:write:alarm_remark"}),
            )

            csrf = service.field_submissions.issue_csrf(submission_id)
            service.field_submissions.submit(
                submission_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
                values={"remark": "现场已复核"},
            )
            prepared_payload = {
                "plan": {
                    "schema_version": (
                        "agentbridge.smartlight_alarm_remark_update_plan.v1"
                    ),
                    "business_intent": "update_alarm_remark",
                    "exact_input": {
                        "alarm_id": "alarm-1",
                        "remark": "现场已复核",
                    },
                },
                "summary": {
                    "title": "修改照明 RTU 告警备注",
                    "fields": [],
                },
            }
            with patch(
                "bscli.core.write_catalog.prepare_smartlight_alarm_remark_update",
                return_value=prepared_payload,
            ) as prepare:
                replaced_target = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.remark.update.prepare",
                    arguments={
                        "alarm_id": "alarm-2",
                        "input_submission_id": submission_id,
                    },
                )
                self.assertEqual(replaced_target["error"]["code"], "FIELD_INPUT_UNAVAILABLE")
                self.assertEqual(
                    service.field_submissions.get(submission_id)["state"], "submitted",
                )
                prepare.assert_not_called()
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.remark.update.prepare",
                    arguments={
                        "alarm_id": "alarm-1",
                        "input_submission_id": submission_id,
                    },
                )
            prepare.assert_called_once()
            self.assertEqual(
                prepare.call_args.args[2],
                {"alarm_id": "alarm-1", "remark": "现场已复核"},
            )
            self.assertEqual(
                prepared["error"]["code"],
                "WRITE_AUTHORIZATION_REQUIRED",
            )
            authorization_id = prepared["nextAction"]["authorizationId"]
            authorization = service.write_authorizations.get(
                authorization_id,
                include_plan=True,
            )
            self.assertEqual(
                authorization["summary"]["system"],
                "照明实验室测试系统",
            )
            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def commit(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                return {
                    "status": "updated",
                    "alarm": {"alarmId": "alarm-1", "remark": "现场已复核"},
                    "verification": {"matched": True},
                }

            with patch(
                "bscli.core.write_catalog.commit_smartlight_alarm_remark_update",
                side_effect=commit,
            ) as commit_handler:
                committed = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.remark.update",
                    arguments={"authorization_id": authorization_id},
                )
                repeated = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.remark.update",
                    arguments={"authorization_id": authorization_id},
                )
                self.assertEqual(repeated["error"]["code"], "WRITE_AUTHORIZATION_UNAVAILABLE")
                commit_handler.assert_called_once()
            self.assertEqual(committed["status"], "succeeded")
            self.assertEqual(
                service.write_authorizations.get(authorization_id)["state"],
                "consumed",
            )

    def test_smartlight_remark_readback_unknown_is_durable_and_never_replayed(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = authorized_service(
                home=Path(tmp),
                base_url=BASE_URL,
                smartlight_base_url="http://smartlight.example.test/smartlight",
                smartlight_allow_insecure_http=True,
            )
            service._worker_factories_by_system["smartlight"] = lambda *_: worker
            session = service.sessions.get_or_create(
                user_subject="user-a", system_id="smartlight", expected_principal_ref="无为",
            )
            session = service.sessions.activate(session["session_id"], observed_principal_ref="无为")
            service.session_states.save(session["session_id"], {"cookies": []})
            plan = {
                "schema_version": "agentbridge.smartlight_alarm_remark_update_plan.v1",
                "prepare_capability": "smartlight.alarm.remark.update.prepare",
                "user_subject": "user-a",
                "session_binding": {
                    name: session[name] for name in (
                        "session_id", "expected_principal_ref", "downstream_principal_ref", "last_verified_at",
                    )
                },
                "exact_input": {"alarm_id": "alarm-1", "remark": "new remark"},
                "exact_payload": {"hitchAlarmId": "alarm-1", "rtuId": "rtu-1", "remark": "new remark"},
                "target": {"alarmId": "alarm-1", "rtuId": "rtu-1"},
                "preconditions": {"previous_remark": "old remark"},
                "resume_arguments": {"alarm_id": "alarm-1"},
            }
            authorization = service.write_authorizations.create(
                user_subject="user-a", system_id="smartlight", session_id=session["session_id"],
                capability_name="smartlight.alarm.remark.update", capability_version="0.1.0",
                prepare_operation_id="prepare-alarm-1", plan=plan,
                summary={"title": "修改告警备注", "fields": []},
                card_base_url=service.trusted_card_base_url,
            )
            authorization_id = authorization["authorization_id"]
            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id, decision="approve", csrf_token=csrf, csrf_cookie=csrf,
            )
            adapter = service._adapters_by_system["smartlight"]
            with patch.object(adapter, "alarm_remark_snapshot", side_effect=[
                {"rtuId": "rtu-1", "remark": "old remark"},
                ConnectionError("simulated authoritative readback failure"),
            ]), patch.object(adapter, "save_alarm_remark", return_value={"code": 200}) as save:
                unknown = service.invoke(
                    user_subject="user-a", capability_name="smartlight.alarm.remark.update",
                    arguments={"authorization_id": authorization_id}, idempotency_key="remark-unknown",
                )
                same_request = service.invoke(
                    user_subject="user-a", capability_name="smartlight.alarm.remark.update",
                    arguments={"authorization_id": authorization_id}, idempotency_key="remark-unknown",
                )
                another_request = service.invoke(
                    user_subject="user-a", capability_name="smartlight.alarm.remark.update",
                    arguments={"authorization_id": authorization_id}, idempotency_key="remark-new-request",
                )
                save.assert_called_once()
            self.assertEqual(unknown["status"], "unknown")
            self.assertEqual(unknown["error"]["code"], "RESULT_UNKNOWN")
            self.assertEqual(same_request["operationId"], unknown["operationId"])
            self.assertEqual(same_request["status"], "unknown")
            self.assertEqual(another_request["error"]["code"], "WRITE_AUTHORIZATION_UNAVAILABLE")
            self.assertEqual(service.operations.get(unknown["operationId"])["status"], "unknown")
            consumed = service.write_authorizations.get(authorization_id)
            self.assertEqual(consumed["state"], "consumed")
            self.assertEqual(consumed["commit_operation_id"], unknown["operationId"])

    def test_smartlight_prepare_preserves_business_rule_rejection(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = authorized_service(
                home=Path(tmp),
                base_url=BASE_URL,
                smartlight_base_url=(
                    "http://123.232.113.241:4101/smartlight"
                ),
                smartlight_allow_insecure_http=True,
            )
            service._worker_factories_by_system["smartlight"] = (  # noqa: SLF001
                lambda _session, _adapter: worker
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="smartlight",
                expected_principal_ref="无为",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="无为",
            )
            service.session_states.save(session["session_id"], {"cookies": []})
            submission = service.field_submissions.create(
                user_subject="user-a",
                system_id="smartlight",
                session_id=session["session_id"],
                capability_name="smartlight.alarm.remark.update.prepare",
                capability_version="0.1.0",
                create_operation_id="prepare-1",
                supersession_key="alarm-1",
                form_schema={
                    **SMARTLIGHT_ALARM_REMARK_FIELD_CARD_SCHEMA,
                    "_agentbridge_resume_arguments": {"alarm_id": "alarm-1"},
                },
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=300,
            )
            csrf = service.field_submissions.issue_csrf(submission["submission_id"])
            service.field_submissions.submit(
                submission["submission_id"],
                csrf_token=csrf,
                csrf_cookie=csrf,
                values={"remark": "相同备注"},
            )
            with patch(
                "bscli.core.write_catalog.prepare_smartlight_alarm_remark_update",
                side_effect=SmartlightBusinessRuleRejected("备注没有变化。"),
            ):
                response = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.remark.update.prepare",
                    arguments={
                        "alarm_id": "alarm-1",
                        "input_submission_id": submission["submission_id"],
                    },
                )
            self.assertEqual(response["status"], "failed")
            self.assertEqual(
                response["error"]["code"],
                "SMARTLIGHT_BUSINESS_RULE_REJECTED",
            )
            self.assertEqual(response["error"]["message"], "备注没有变化。")

    def test_smartlight_rtu_action_skips_field_card_and_uses_authorization(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = authorized_service(
                home=Path(tmp),
                base_url=BASE_URL,
                smartlight_base_url="http://123.232.113.241:4101/smartlight",
                smartlight_allow_insecure_http=True,
            )
            service._worker_factories_by_system["smartlight"] = (  # noqa: SLF001
                lambda _session, _adapter: worker
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="smartlight",
                expected_principal_ref="Demo",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Demo",
            )
            service.session_states.save(session["session_id"], {"cookies": []})
            prepared_payload = {
                "plan": {
                    "schema_version": (
                        "agentbridge.smartlight_alarm_work_area_submit_plan.v1"
                    ),
                    "business_intent": "submit_work_area",
                    "target": {"alarmId": "alarm-1", "rtuId": "rtu-1"},
                    "exact_input": {"alarm_id": "alarm-1"},
                    "preconditions": {"alarmId": "alarm-1", "rtuId": "rtu-1"},
                    "expected_effect": {"work_area_submitted": True},
                },
                "summary": {
                    "title": "Submit RTU alarm to work area",
                    "system": "Smartlight",
                    "effect": "Submit one RTU alarm",
                    "fields": [],
                },
            }
            with patch(
                "bscli.core.write_catalog.prepare_smartlight_alarm_work_area_submit",
                return_value=prepared_payload,
            ) as prepare:
                started = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.work_area.submit.prepare",
                    arguments={"alarm_id": "alarm-1"},
                )

            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.args[2], {"alarm_id": "alarm-1"})
            self.assertEqual(
                started["error"]["code"],
                "WRITE_AUTHORIZATION_REQUIRED",
            )
            self.assertNotIn("inputSubmissionId", started["nextAction"])
            authorization_id = started["nextAction"]["authorizationId"]
            authorization = service.write_authorizations.get(
                authorization_id,
                include_plan=True,
            )
            self.assertEqual(
                authorization["plan"]["resume_arguments"],
                {"alarm_id": "alarm-1"},
            )
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=started["interaction"]["interactionId"],
                ),
                frozenset({"smartlight:write:alarm_work_area_submit"}),
            )

            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def commit(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                return {
                    "status": "succeeded",
                    "action": "submit_work_area",
                    "verification": {"matched": True},
                }

            with patch(
                "bscli.core.write_catalog.commit_smartlight_alarm_work_area_submit",
                side_effect=commit,
            ):
                committed = service.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.alarm.work_area.submit",
                    arguments={"authorization_id": authorization_id},
                )
            self.assertEqual(committed["status"], "succeeded")
            self.assertEqual(
                service.write_authorizations.get(authorization_id)["state"],
                "consumed",
            )

    def test_smartlight_actions_authorize_frozen_targets_once_without_field_cards(self):
        for capability, path, _, _ in SMARTLIGHT_ACTION_CASES:
            with self.subTest(capability=capability), TemporaryDirectory() as tmp:
                service, worker = self._smartlight_action_service(tmp, capability)
                prepared = service.invoke(
                    user_subject="user-a", capability_name=capability + ".prepare",
                    arguments={"alarm_id": "alarm-1"},
                )
                self.assertEqual(prepared["error"]["code"], "WRITE_AUTHORIZATION_REQUIRED")
                self.assertNotIn("inputSubmissionId", prepared["nextAction"])
                authorization_id = prepared["nextAction"]["authorizationId"]
                authorization = service.write_authorizations.get(
                    authorization_id, include_plan=True,
                )
                self.assertEqual(authorization["plan"]["exact_input"], {"alarm_id": "alarm-1"})
                self.assertEqual(authorization["plan"]["resume_arguments"], {"alarm_id": "alarm-1"})
                self.assertEqual(authorization["plan"]["target"]["alarmId"], "alarm-1")
                self.assertEqual(authorization["plan"]["target"]["rtuId"], "rtu-1")
                pending = service.invoke(
                    user_subject="user-a", capability_name=capability,
                    arguments={"authorization_id": authorization_id},
                )
                self.assertEqual(pending["error"]["code"], "WRITE_AUTHORIZATION_REQUIRED")
                self.assertEqual(self._smartlight_action_writes(worker, path), [])

                self._approve_smartlight_action(service, authorization_id)
                with self.assertRaisesRegex(ValueError, "unexpected capability input: alarm_id"):
                    service.invoke(
                        user_subject="user-a", capability_name=capability,
                        arguments={"authorization_id": authorization_id, "alarm_id": "alarm-2"},
                    )
                self.assertEqual(self._smartlight_action_writes(worker, path), [])
                committed = service.invoke(
                    user_subject="user-a", capability_name=capability,
                    arguments={"authorization_id": authorization_id},
                )
                repeated = service.invoke(
                    user_subject="user-a", capability_name=capability,
                    arguments={"authorization_id": authorization_id},
                )
                self.assertEqual(committed["status"], "succeeded")
                self.assertEqual(committed["result"]["alarm"]["alarmId"], "alarm-1")
                self.assertTrue(committed["result"]["verification"]["matched"])
                self.assertEqual(repeated["error"]["code"], "WRITE_AUTHORIZATION_UNAVAILABLE")
                self.assertEqual(len(self._smartlight_action_writes(worker, path)), 1)
                self.assertEqual(
                    service.write_authorizations.get(authorization_id)["state"], "consumed",
                )

    def test_smartlight_actions_already_completed_consume_authorization_without_writing(self):
        for capability, path, field, reached in SMARTLIGHT_ACTION_CASES:
            with self.subTest(capability=capability), TemporaryDirectory() as tmp:
                service, worker = self._smartlight_action_service(tmp, capability)
                prepared = service.invoke(
                    user_subject="user-a", capability_name=capability + ".prepare",
                    arguments={"alarm_id": "alarm-1"},
                )
                authorization_id = prepared["nextAction"]["authorizationId"]
                self._approve_smartlight_action(service, authorization_id)
                worker.alarm_record[field] = reached
                completed = service.invoke(
                    user_subject="user-a", capability_name=capability,
                    arguments={"authorization_id": authorization_id},
                )
                self.assertEqual(completed["status"], "succeeded")
                self.assertEqual(completed["result"]["status"], "already_completed")
                self.assertEqual(completed["result"]["downstream"]["affected"], 0)
                self.assertEqual(self._smartlight_action_writes(worker, path), [])
                self.assertEqual(
                    service.write_authorizations.get(authorization_id)["state"], "consumed",
                )

    def test_smartlight_action_unknown_results_are_durable_and_never_replayed(self):
        for capability, path, _, _ in SMARTLIGHT_ACTION_CASES:
            for phase in ("submit", "readback"):
                with self.subTest(capability=capability, phase=phase), TemporaryDirectory() as tmp:
                    service, worker = self._smartlight_action_service(tmp, capability)
                    prepared = service.invoke(
                        user_subject="user-a", capability_name=capability + ".prepare",
                        arguments={"alarm_id": "alarm-1"},
                    )
                    authorization_id = prepared["nextAction"]["authorizationId"]
                    self._approve_smartlight_action(service, authorization_id)
                    if phase == "submit":
                        worker.alarm_action_request_error = ConnectionError("simulated lost response")
                    else:
                        worker.fail_alarm_readback_after_action = True
                    arguments = {"authorization_id": authorization_id}
                    unknown = service.invoke(
                        user_subject="user-a", capability_name=capability,
                        arguments=arguments, idempotency_key="action-unknown",
                    )
                    same_request = service.invoke(
                        user_subject="user-a", capability_name=capability,
                        arguments=arguments, idempotency_key="action-unknown",
                    )
                    another_request = service.invoke(
                        user_subject="user-a", capability_name=capability,
                        arguments=arguments, idempotency_key="action-new-request",
                    )
                    self.assertEqual(unknown["status"], "unknown")
                    self.assertEqual(unknown["error"]["code"], "RESULT_UNKNOWN")
                    self.assertEqual(same_request["operationId"], unknown["operationId"])
                    self.assertEqual(same_request["status"], "unknown")
                    self.assertTrue(same_request["reused"])
                    self.assertEqual(another_request["error"]["code"], "WRITE_AUTHORIZATION_UNAVAILABLE")
                    self.assertEqual(len(self._smartlight_action_writes(worker, path)), 1)
                    persisted = OperationStore(service.operations.db_path).get(unknown["operationId"])
                    self.assertEqual(persisted["status"], "unknown")
                    consumed = service.write_authorizations.get(authorization_id)
                    self.assertEqual(consumed["state"], "consumed")
                    self.assertEqual(consumed["commit_operation_id"], unknown["operationId"])

    @staticmethod
    def _smartlight_action_service(tmp, capability):
        worker = FakeSmartlightWorker(authenticated=True)
        if capability == "smartlight.alarm.work_area.revoke":
            worker.alarm_record["isSubmitWorkArea"] = 1
        service = authorized_service(
            home=Path(tmp), base_url=BASE_URL,
            smartlight_base_url="http://smartlight.example.test/smartlight",
            smartlight_allow_insecure_http=True,
        )
        service._worker_factories_by_system["smartlight"] = lambda *_: worker
        session = service.sessions.get_or_create(
            user_subject="user-a", system_id="smartlight", expected_principal_ref="无为",
        )
        service.sessions.activate(session["session_id"], observed_principal_ref="无为")
        service.session_states.save(session["session_id"], worker.capture_session_state())
        return service, worker

    @staticmethod
    def _smartlight_action_writes(worker, path):
        return [request for request in worker.api_requests if request["path"].endswith(path)]

    @staticmethod
    def _approve_smartlight_action(service, authorization_id):
        csrf = service.write_authorizations.issue_csrf(authorization_id)
        service.write_authorizations.decide(
            authorization_id, decision="approve", csrf_token=csrf, csrf_cookie=csrf,
        )

    def test_submit_and_leave_capabilities_have_separate_scope_policies(self):
        self.assertEqual(
            capability_required_scopes("oa.business_trip.submit"),
            frozenset({"oa:write:submit"}),
        )
        self.assertEqual(
            capability_required_scopes("oa.business_trip.submit.prepare"),
            frozenset({"oa:write:submit"}),
        )
        self.assertEqual(
            capability_required_scopes("oa.leave.save_draft"),
            frozenset({"oa:write:draft"}),
        )
        self.assertEqual(
            capability_required_scopes("oa.leave.submit.prepare"),
            frozenset({"oa:write:submit"}),
        )
        self.assertEqual(
            capability_required_scopes("oa.leave.submit"),
            frozenset({"oa:write:submit"}),
        )
        self.assertEqual(
            capability_required_scopes("oa.workflow.revoke.prepare"),
            frozenset({"oa:write:revoke"}),
        )
        self.assertEqual(
            capability_required_scopes("oa.workflow.revoke"),
            frozenset({"oa:write:revoke"}),
        )
        for capability_name in (
            "oa.efficiency_data.approval.prepare",
            "oa.efficiency_data.approve",
            "oa.travel_expense.approval.prepare",
            "oa.travel_expense.approve",
            "oa.labor_contract_renewal.approval.prepare",
            "oa.labor_contract_renewal.approve",
            "oa.intellectual_property_declaration.approval.prepare",
            "oa.intellectual_property_declaration.approve",
            "oa.overtime.approval.prepare",
            "oa.overtime.approve",
            "oa.resignation.approval.prepare",
            "oa.work_handover.approval.prepare",
            "oa.resignation.approve",
            "oa.work_handover.approve",
            "oa.flight_application.approval.prepare",
            "oa.flight_application.approve",
            "oa.attendance_confirmation.prepare",
            "oa.attendance_confirmation.confirm",
            "oa.weekly_report.acknowledgement.prepare",
            "oa.weekly_report.acknowledge",
            "oa.standard_collaboration.approval.prepare",
            "oa.standard_collaboration.approve",
        ):
            self.assertEqual(
                capability_required_scopes(capability_name),
                frozenset({"oa:write:approval"}),
            )

    def test_pending_action_preflight_runs_before_field_card_creation(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            with patch(
                "bscli.core.write_catalog.preflight_pending_action",
                side_effect=PendingActionContractMismatch(
                    "The selected pending workflow is not a registered standard_collaboration item."
                ),
            ) as preflight:
                response = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.standard_collaboration.approval.prepare",
                    arguments={"affair_id": "attendance-affair"},
                )

            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "WORKFLOW_NOT_SUPPORTED")
            self.assertIsNone(response["nextAction"])
            preflight.assert_called_once()

    def test_attendance_preflight_opens_the_dedicated_prefilled_card(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            with patch(
                "bscli.core.write_catalog.preflight_pending_action",
                return_value={
                    "matched": True,
                    "review_fingerprint": "sha256:review-one",
                    "review": {
                        "title": "月度考勤确认单",
                        "effect": "确认当前考勤",
                        "fields": [{"label": "姓名", "value": "Alice"}],
                    },
                },
            ) as preflight:
                response = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.attendance_confirmation.prepare",
                    arguments={
                        "affair_id": "attendance-affair",
                        "opinion": "确认无异议",
                    },
                )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "FIELD_INPUT_REQUIRED")
            submission = service.field_submissions.get(
                response["nextAction"]["inputSubmissionId"]
            )
            self.assertEqual(
                submission["form_schema"]["title"],
                "填写月度考勤确认意见",
            )
            self.assertEqual(
                submission["form_schema"]["fields"][0]["value"],
                "确认无异议",
            )
            self.assertEqual(
                submission["form_schema"]["review"]["fields"],
                [{"label": "姓名", "value": "Alice"}],
            )
            self.assertEqual(
                submission["form_schema"]["_agentbridge_review_fingerprint"],
                "sha256:review-one",
            )
            preflight.assert_called_once()

    def test_changed_detail_is_called_out_on_authorization_card(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            with patch(
                "bscli.core.write_catalog.preflight_pending_action",
                return_value={
                    "matched": True,
                    "review_fingerprint": "sha256:before-opinion",
                    "review": {
                        "title": "月度考勤确认单",
                        "effect": "确认当前考勤",
                        "fields": [{"label": "姓名", "value": "Alice"}],
                    },
                },
            ):
                started = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.attendance_confirmation.prepare",
                    arguments={"affair_id": "attendance-affair"},
                )
            submission_id = started["nextAction"]["inputSubmissionId"]
            csrf = service.field_submissions.issue_csrf(submission_id)
            service.field_submissions.submit(
                submission_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
                values={"opinion": "确认无异议"},
            )
            prepared_payload = {
                "plan": {
                    "business_intent": "confirm_attendance",
                    "target": {"review_fingerprint": "sha256:after-opinion"},
                    "exact_input": {"opinion": "确认无异议"},
                },
                "summary": {
                    "title": "确认月度考勤",
                    "system": "致远 OA",
                    "effect": "确认当前考勤",
                    "fields": [{"label": "处理意见", "value": "确认无异议"}],
                },
            }

            with patch(
                "bscli.core.write_catalog.prepare_attendance_confirmation",
                return_value=prepared_payload,
            ):
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.attendance_confirmation.prepare",
                    arguments={
                        "affair_id": "attendance-affair",
                        "input_submission_id": submission_id,
                    },
                )

            authorization = service.write_authorizations.get(
                prepared["nextAction"]["authorizationId"]
            )
            self.assertEqual(
                authorization["summary"]["fields"][0],
                {
                    "label": "详情变化",
                    "value": "填写意见后单据内容发生变化，请按当前详情重新核对本次授权。",
                },
            )
            self.assertIn("最新详情", authorization["summary"]["authorization_notice"])

    def test_invoke_restores_session_and_persists_operation(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            session = self._activate(service)
            service.adapter.invoke_capability = MagicMock(
                return_value={"count": 1, "items": [{"title": "Pending"}]}
            )
            time.sleep(0.01)

            response = service.invoke(
                user_subject="user-a",
                capability_name="oa.workflow.pending.list",
                arguments={"limit": 5},
                idempotency_key="pending-1",
                request_id="request-1",
            )

            self.assertEqual(response["status"], "succeeded")
            self.assertEqual(response["requestId"], "request-1")
            self.assertEqual(response["result"]["count"], 1)
            self.assertEqual(worker.restored, {"cookies": []})
            operation = service.operations.get(response["operationId"])
            self.assertEqual(operation["user_subject"], "user-a")
            self.assertEqual(operation["status"], "succeeded")
            self.assertGreater(
                service.sessions.get(session["session_id"])["updated_at"],
                session["updated_at"],
            )

    def test_parallel_certificate_search_fails_fast_instead_of_queueing(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            self._activate(service)
            started = threading.Event()
            release = threading.Event()

            def invoke_capability(_name, _worker, _arguments):
                started.set()
                release.wait(timeout=30)
                return {"count": 0, "items": []}

            service.adapter.invoke_capability = invoke_capability
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    service.invoke,
                    user_subject="user-a",
                    capability_name="oa.document.certificate.search",
                    arguments={
                        "name": "系统甲",
                        "document_type": "software_copyright_certificate",
                    },
                )
                try:
                    self.assertTrue(started.wait(timeout=15))
                    second_future = pool.submit(
                        service.invoke,
                        user_subject="user-a",
                        capability_name="oa.document.certificate.search",
                        arguments={
                            "name": "系统乙",
                            "document_type": "software_copyright_certificate",
                        },
                    )
                    second = second_future.result(timeout=10)
                finally:
                    release.set()
                first_result = first.result(timeout=10)

            self.assertEqual(second["status"], "failed")
            self.assertEqual(second["error"]["code"], "SESSION_BUSY")
            self.assertEqual(first_result["status"], "succeeded")

    def test_login_expiry_is_shared_by_cli_and_future_mcp_callers(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            session = self._activate(service)
            service.adapter.invoke_capability = MagicMock(
                side_effect=SeeyonLoginRequired("OA expired")
            )

            response = service.invoke(
                user_subject="user-a",
                capability_name="oa.workflow.done.list",
                arguments={},
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "LOGIN_REQUIRED")
            self.assertEqual(response["nextAction"]["sessionState"], "expired")
            self.assertEqual(service.sessions.get(session["session_id"])["state"], "expired")
            self.assertIsNone(service.session_states.load(session["session_id"]))

    def test_temporary_session_check_failure_preserves_active_session(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            session = self._activate(service)
            service.adapter.invoke_capability = MagicMock(
                side_effect=SeeyonSessionCheckUnavailable(
                    "OA session check did not return JSON (HTTP 200, content_type=text/html)."
                )
            )

            response = service.invoke(
                user_subject="user-a",
                capability_name="oa.workflow.pending.list",
                arguments={},
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "SESSION_CHECK_UNAVAILABLE")
            self.assertTrue(response["nextAction"]["sessionPreserved"])
            self.assertIn("HTTP 200", response["error"]["message"])
            self.assertEqual(service.sessions.get(session["session_id"])["state"], "active")
            self.assertIsNotNone(service.session_states.load(session["session_id"]))

    def test_session_status_live_checks_an_active_session(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker, keepalive_lease_seconds=604_800)
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                return_value={
                    "authenticated": True,
                    "template_count": 118,
                    "transport": "central_http_session",
                }
            )
            time.sleep(0.01)

            response = service.session_status(user_subject="user-a")

            self.assertEqual(response["status"], "active")
            self.assertEqual(response["statusSource"], "live")
            self.assertIsNotNone(response["checkedAt"])
            self.assertEqual(response["lastVerifiedAt"], session["last_verified_at"])
            self.assertEqual(
                response["lastVerifiedAt"],
                service.sessions.get(session["session_id"])["last_verified_at"],
            )
            self.assertGreater(
                response["lastActivityAt"],
                session["last_user_activity_at"],
            )
            self.assertEqual(response["lastUserActivityAt"], response["lastActivityAt"])
            self.assertIsNone(response["lastKeepaliveAt"])
            self.assertEqual(response["keepaliveState"], "eligible")
            self.assertIsNotNone(response["keepaliveEligibleUntil"])
            self.assertEqual(worker.restored, {"cookies": []})
            service.adapter.probe_session.assert_called_once_with(worker)

    def test_outside_keepalive_lease_reports_last_confirmed_state(self):
        old_activity = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        response = session_response(
            {
                "session_id": "session-a",
                "system_id": "taihua",
                "user_subject": "user-a",
                "state": "active",
                "last_user_activity_at": old_activity,
                "last_keepalive_at": old_activity,
            },
            activity_lease_seconds=604_800,
        )

        self.assertEqual(response["keepaliveState"], "outside_lease")
        self.assertFalse(response["keepaliveActive"])
        self.assertEqual(response["sessionStateBasis"], "last_confirmed")
        self.assertIn("not a current live guarantee", response["keepaliveExplanation"])

    def test_session_status_records_adapter_session_recovery(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                return_value={
                    "authenticated": True,
                    "template_count": None,
                    "transport": "central_cas_cookie_jwt",
                    "session_recovery": "cas_sso",
                }
            )

            response = service.session_status(user_subject="user-a")
            latest_event = service.sessions.latest_event(session["session_id"])

            self.assertEqual(response["status"], "active")
            self.assertEqual(latest_event["event_type"], "session_recovered")
            self.assertEqual(latest_event["source"], "session_status")
            self.assertIn("cas_sso", latest_event["reason"])

    def test_session_status_reports_live_expiry_and_deletes_invalid_state(self):
        with TemporaryDirectory() as tmp:
            service = self._service(
                tmp,
                FakeWorker(),
                keepalive_lease_seconds=604_800,
            )
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                side_effect=SeeyonLoginRequired("OA expired")
            )

            response = service.session_status(user_subject="user-a")

            self.assertEqual(response["status"], "expired")
            self.assertEqual(response["statusSource"], "live")
            self.assertIsNotNone(response["checkedAt"])
            self.assertEqual(response["lastActivityAt"], session["last_user_activity_at"])
            self.assertIsNotNone(response["expiredAt"])
            self.assertEqual(response["keepaliveState"], "inactive")
            self.assertIsNotNone(response["keepaliveEligibleUntil"])
            self.assertEqual(service.sessions.get(session["session_id"])["state"], "expired")
            self.assertIsNone(service.session_states.load(session["session_id"]))

    def test_session_status_returns_check_unavailable_without_deleting_state(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                side_effect=SeeyonSessionCheckUnavailable(
                    "OA session check received a temporary response (HTTP 503)."
                )
            )

            response = service.session_status(user_subject="user-a")

            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "SESSION_CHECK_UNAVAILABLE")
            self.assertTrue(response["error"]["retryable"])
            self.assertEqual(response["statusSource"], "live")
            self.assertEqual(response["session"]["status"], "active")
            self.assertTrue(response["nextAction"]["sessionPreserved"])
            self.assertIsNotNone(service.session_states.load(session["session_id"]))

    def test_keepalive_cycle_refreshes_oa_without_renewing_its_own_lease(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker, keepalive_lease_seconds=604_800)
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                return_value={
                    "authenticated": True,
                    "template_count": 118,
                    "transport": "central_http_session",
                }
            )
            checked_at = (
                datetime.fromisoformat(session["last_user_activity_at"])
                + timedelta(minutes=1)
            )

            summary = service.run_session_keepalive_cycle(
                activity_lease_seconds=3_600,
                now=checked_at,
            )

            self.assertEqual(summary["activeSessions"], 1)
            self.assertEqual(summary["eligibleSessions"], 1)
            self.assertEqual(summary["keptAlive"], 1)
            persisted = service.sessions.get(session["session_id"])
            self.assertEqual(persisted["updated_at"], session["updated_at"])
            self.assertEqual(persisted["last_verified_at"], session["last_verified_at"])
            self.assertEqual(
                persisted["last_user_activity_at"],
                session["last_user_activity_at"],
            )
            self.assertIsNotNone(persisted["last_keepalive_at"])
            service.adapter.probe_session.assert_called_once_with(worker)

    def test_keepalive_cycle_prefers_adapter_keepalive_probe(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker, keepalive_lease_seconds=604_800)
            session = self._activate(service)
            service.adapter.probe_session = MagicMock()
            service.adapter.keepalive_session = MagicMock(
                return_value={
                    "authenticated": True,
                    "template_count": None,
                    "transport": "central_cas_cookie_jwt",
                }
            )

            summary = service.run_session_keepalive_cycle(
                activity_lease_seconds=3_600,
                now=(
                    datetime.fromisoformat(session["last_user_activity_at"])
                    + timedelta(minutes=1)
                ),
            )

            self.assertEqual(summary["keptAlive"], 1)
            service.adapter.keepalive_session.assert_called_once_with(worker)
            service.adapter.probe_session.assert_not_called()

    def test_keepalive_cycle_stops_after_activity_lease(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            service.adapter.probe_session = MagicMock()
            checked_at = (
                datetime.fromisoformat(session["last_user_activity_at"])
                + timedelta(hours=9)
            )

            summary = service.run_session_keepalive_cycle(
                activity_lease_seconds=28_800,
                now=checked_at,
            )

            self.assertEqual(summary["eligibleSessions"], 0)
            self.assertEqual(summary["outsideLease"], 1)
            service.adapter.probe_session.assert_not_called()

    def test_keepalive_cycle_expires_only_an_explicitly_logged_out_session(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                side_effect=SeeyonLoginRequired("OA expired")
            )

            summary = service.run_session_keepalive_cycle(
                activity_lease_seconds=28_800,
            )

            self.assertEqual(summary["expired"], 1)
            self.assertEqual(
                summary["issues"],
                [
                    {
                        "userSubject": "user-a",
                        "systemId": "oa",
                        "outcome": "expired",
                        "diagnostics": "OA expired",
                    }
                ],
            )
            self.assertEqual(service.sessions.get(session["session_id"])["state"], "expired")
            self.assertIsNone(service.session_states.load(session["session_id"]))

    def test_keepalive_cycle_defers_transient_failure_and_preserves_state(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                side_effect=SeeyonSessionCheckUnavailable("HTTP 503")
            )

            summary = service.run_session_keepalive_cycle(
                activity_lease_seconds=28_800,
            )

            self.assertEqual(summary["deferred"], 1)
            self.assertEqual(summary["issues"][0]["userSubject"], "user-a")
            self.assertEqual(summary["issues"][0]["systemId"], "oa")
            self.assertEqual(summary["issues"][0]["outcome"], "deferred")
            self.assertEqual(
                summary["issues"][0]["errorCode"],
                "SESSION_CHECK_UNAVAILABLE",
            )
            self.assertIn("HTTP 503", summary["issues"][0]["diagnostics"])
            self.assertEqual(service.sessions.get(session["session_id"])["state"], "active")
            self.assertIsNotNone(service.session_states.load(session["session_id"]))

    def test_keepalive_cycle_isolates_one_expired_user_from_another(self):
        with TemporaryDirectory() as tmp:
            def worker_factory(session, _adapter):
                worker = FakeWorker()
                worker.user_subject = session["user_subject"]
                return worker

            service = authorized_service(
                home=Path(tmp),
                base_url=BASE_URL,
                worker_factory=worker_factory,
            )
            sessions = {}
            for user_subject, principal in (("user-a", "Alice"), ("user-b", "Bob")):
                session = service.sessions.get_or_create(
                    user_subject=user_subject,
                    system_id="oa",
                    expected_principal_ref=principal,
                )
                session = service.sessions.activate(
                    session["session_id"],
                    observed_principal_ref=principal,
                )
                service.session_states.save(
                    session["session_id"],
                    {"cookies": [{"owner": user_subject}]},
                )
                sessions[user_subject] = session

            def probe(worker):
                if worker.user_subject == "user-a":
                    raise SeeyonLoginRequired("OA expired")
                return {
                    "authenticated": True,
                    "template_count": 118,
                    "transport": "central_http_session",
                }

            service.adapter.probe_session = MagicMock(side_effect=probe)
            checked_at = (
                datetime.fromisoformat(sessions["user-a"]["last_user_activity_at"])
                + timedelta(minutes=1)
            )

            summary = service.run_session_keepalive_cycle(
                activity_lease_seconds=3_600,
                now=checked_at,
            )

            self.assertEqual(summary["activeSessions"], 2)
            self.assertEqual(summary["eligibleSessions"], 2)
            self.assertEqual(summary["expired"], 1)
            self.assertEqual(summary["keptAlive"], 1)
            self.assertEqual(
                service.sessions.get(sessions["user-a"]["session_id"])["state"],
                "expired",
            )
            self.assertIsNone(
                service.session_states.load(sessions["user-a"]["session_id"])
            )
            self.assertEqual(
                service.sessions.get(sessions["user-b"]["session_id"])["state"],
                "active",
            )
            self.assertEqual(
                service.session_states.load(sessions["user-b"]["session_id"]),
                {"cookies": []},
            )
    def test_start_login_uses_server_bound_expected_principal(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            service.sessions.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Alice",
            )

            response = service.start_login(
                user_subject="user-a",
                expected_principal_ref=None,
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=300,
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["challenge"]["expectedPrincipalRef"], "Alice")
            self.assertTrue(response["nextAction"]["cardUrl"].startswith("http://127.0.0.1:8780/auth/"))
            self.assertEqual(response["interaction"]["type"], "credential")
            self.assertEqual(
                response["interaction"]["interactionId"],
                response["nextAction"]["interactionId"],
            )

    def test_start_login_reuses_matching_unexpired_card_and_interaction(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())

            first = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=300,
            )
            second = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=300,
            )

            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(
                second["challenge"]["challengeId"],
                first["challenge"]["challengeId"],
            )
            self.assertEqual(
                second["nextAction"]["cardUrl"],
                first["nextAction"]["cardUrl"],
            )
            self.assertEqual(
                second["interaction"]["interactionId"],
                first["interaction"]["interactionId"],
            )

    def test_credential_reader_accepts_task_scoped_reference_before_writer_upgrade(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            result = service.start_login(user_subject="user-a", expected_principal_ref="Alice",
                                         card_base_url="http://127.0.0.1:8780")
            legacy = service.interactions.get(result["interaction"]["interactionId"], user_subject="user-a")
            challenge_id = result["challenge"]["challengeId"]
            scoped = service.interactions.register(
                interaction_type="credential", user_subject="user-a", system_id="oa",
                session_id=legacy["session_id"], operation_id=None,
                resource_id=challenge_id+":fixture-task", title=legacy["title"], message=legacy["message"],
                display=legacy["display"], resume_spec={"kind":"session_ready","systemId":"oa","challengeId":challenge_id},
                created_at=legacy["created_at"], expires_at=legacy["expires_at"],
            )
            _, resource, envelope = service._load_interaction(user_subject="user-a", interaction_id=scoped["interaction_id"])
            self.assertEqual(resource["challenge_id"], challenge_id)
            self.assertEqual(envelope["presentation"]["url"], result["challenge"]["cardUrl"])

    def test_start_login_reuses_live_active_session_without_card(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                return_value={
                    "authenticated": True,
                    "template_count": 118,
                    "transport": "central_http_session",
                }
            )

            response = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertEqual(response["status"], "succeeded")
            self.assertTrue(response["reused"])
            self.assertNotIn("challenge", response)
            self.assertIsNone(response["nextAction"])
            self.assertEqual(response["result"]["templateCount"], 118)
            self.assertNotIn("browserBridgeUsed", response["result"])
            self.assertEqual(worker.restored, {"cookies": []})
            self.assertEqual(
                service.sessions.get(session["session_id"])["state"],
                "active",
            )

    def test_completed_credential_interaction_resumes_to_active_session(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            started = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
            )
            challenge_id = started["challenge"]["challengeId"]
            csrf = service.challenges.issue_csrf(challenge_id)
            service.challenges.claim(
                challenge_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            service.challenges.complete(
                challenge_id,
                result={"principal": "Alice"},
            )
            session = service.sessions.find(user_subject="user-a", system_id="oa")
            service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Alice",
            )

            response = service.resume_interaction(
                user_subject="user-a",
                interaction_id=started["interaction"]["interactionId"],
            )

            self.assertEqual(response["status"], "succeeded")
            self.assertEqual(response["interaction"]["state"], "completed")
            self.assertEqual(response["nextAction"]["type"], "retry_original_request")
            self.assertEqual(response["result"]["session"]["status"], "active")

    def test_failed_credential_interaction_cannot_be_reported_as_resumed(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            started = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
            )
            challenge_id = started["challenge"]["challengeId"]
            csrf = service.challenges.issue_csrf(challenge_id)
            service.challenges.claim(
                challenge_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            service.challenges.fail(
                challenge_id,
                code="LOGIN_REJECTED",
                message="OA rejected the login.",
            )

            response = service.resume_interaction(
                user_subject="user-a",
                interaction_id=started["interaction"]["interactionId"],
            )

            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "INTERACTION_FAILED")
            self.assertEqual(response["nextAction"]["type"], "start_again")

    def test_start_login_creates_card_only_after_live_probe_reports_expired(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                side_effect=SeeyonLoginRequired("OA expired")
            )

            response = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertFalse(response["reused"])
            self.assertEqual(
                response["nextAction"]["type"],
                "open_authentication_card",
            )
            self.assertEqual(
                service.sessions.get(session["session_id"])["state"],
                "expired",
            )
            self.assertIsNone(service.session_states.load(session["session_id"]))

    def test_start_login_probe_failure_does_not_prompt_for_credentials(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            service.adapter.probe_session = MagicMock(
                side_effect=TimeoutError("OA unavailable")
            )

            response = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "SESSION_CHECK_UNAVAILABLE")
            self.assertTrue(response["error"]["retryable"])
            self.assertEqual(
                response["nextAction"]["type"],
                "retry_session_check",
            )
            self.assertNotIn("challenge", response)
            self.assertEqual(
                service.sessions.get(session["session_id"])["state"],
                "active",
            )
            self.assertIsNotNone(service.session_states.load(session["session_id"]))

    def test_runtime_identity_mismatch_is_actionable_and_preserves_session(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            inaccessible = InaccessibleSessionStateStore()
            service.session_states = inaccessible

            response = service.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "SESSION_RUNTIME_MISMATCH")
            self.assertEqual(
                response["nextAction"]["type"],
                "retry_via_bound_central_runtime",
            )
            self.assertTrue(response["nextAction"]["sessionPreserved"])
            self.assertEqual(
                service.sessions.get(session["session_id"])["state"],
                "active",
            )
            self.assertFalse(inaccessible.deleted)

    def test_start_login_runtime_mismatch_does_not_replace_session_with_card(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            inaccessible = InaccessibleSessionStateStore()
            service.session_states = inaccessible

            response = service.start_login(
                user_subject="user-a",
                expected_principal_ref="Alice",
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "SESSION_RUNTIME_MISMATCH")
            self.assertNotIn("challenge", response)
            self.assertEqual(
                service.sessions.get(session["session_id"])["state"],
                "active",
            )
            self.assertFalse(inaccessible.deleted)

    def test_operation_lookup_does_not_cross_user_boundary(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            response = service.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
                idempotency_key="login-required-record",
            )

            with self.assertRaisesRegex(KeyError, "operation not found"):
                service.get_operation(
                    user_subject="user-b",
                    operation_id=response["operationId"],
                )

    def test_same_user_browser_work_is_serialized_inside_service(self):
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=Path(tmp),
                base_url=BASE_URL,
                worker_factory=lambda _session, _adapter: FakeWorker(),
            )
            self._activate(service)
            state_lock = threading.Lock()
            active = 0
            maximum_active = 0

            def invoke_adapter(_name, _worker, _arguments):
                nonlocal active, maximum_active
                with state_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.05)
                with state_lock:
                    active -= 1
                return {"count": 0, "items": []}

            service.adapter.invoke_capability = invoke_adapter
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        service.invoke,
                        user_subject="user-a",
                        capability_name="oa.workflow.pending.list",
                        arguments={},
                        idempotency_key=f"parallel-{index}",
                    )
                    for index in range(2)
                ]
                responses = [future.result() for future in futures]

        self.assertEqual(maximum_active, 1)
        self.assertTrue(all(response["status"] == "succeeded" for response in responses))

    def test_static_business_cards_prefill_all_supplied_fields(self):
        cases = [
            (
                "oa.business_trip.prepare",
                {
                    "start_time": "2026-07-21 09:00",
                    "end_time": "2026-07-21 18:00",
                    "travel_mode": "火车",
                    "origin": "济南",
                    "destination": "青岛",
                    "reason": "客户交流",
                    "has_direct_supervisor": False,
                    # Duration is calculated by OA and is not trusted-card input.
                    # The card only collects fields that the user can control.
                },
                {},
            ),
            (
                "oa.leave.submit.prepare",
                {
                    "leave_type": "年休",
                    "start_time": "2026-07-22 09:00",
                    "end_time": "2026-07-22 18:00",
                    "reason": "个人事务",
                    "has_direct_supervisor": True,
                },
                {},
            ),
            (
                "oa.missed_punch.prepare",
                {
                    "start_time": "2026-07-20 08:30",
                    "end_time": "2026-07-20 09:00",
                    "location": "公司园区",
                    "reason_type": "忘记打卡",
                    "explanation": "早间漏打卡",
                },
                {},
            ),
            (
                "oa.missed_punch.approval.prepare",
                {"affair_id": "affair-1", "opinion": "同意"},
                {"affair_id": "affair-1"},
            ),
        ]
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            for index, (capability, arguments, resume_arguments) in enumerate(cases):
                started = service.invoke(
                    user_subject="user-a",
                    capability_name=capability,
                    arguments=arguments,
                    idempotency_key=f"prefill-{index}",
                )
                self.assertEqual(started["error"]["code"], "FIELD_INPUT_REQUIRED")
                submission = service.field_submissions.get(
                    started["nextAction"]["inputSubmissionId"]
                )
                fields = {
                    item["name"]: item
                    for item in submission["form_schema"]["fields"]
                }
                for name, value in arguments.items():
                    if name != "affair_id":
                        self.assertEqual(fields[name]["value"], value)
                self.assertEqual(
                    submission["form_schema"]["_agentbridge_resume_arguments"],
                    resume_arguments,
                )
                for value in arguments.values():
                    if isinstance(value, str) and value not in resume_arguments.values():
                        self.assertNotIn(value, str(started["nextAction"]))

    def test_same_write_capability_keeps_distinct_target_cards_active(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            first = service.invoke(
                user_subject="user-a",
                capability_name="oa.missed_punch.approval.prepare",
                arguments={"affair_id": "affair-1"},
                idempotency_key="missed-punch-target-1",
            )
            second = service.invoke(
                user_subject="user-a",
                capability_name="oa.missed_punch.approval.prepare",
                arguments={"affair_id": "affair-2"},
                idempotency_key="missed-punch-target-2",
            )

            first_id = first["nextAction"]["inputSubmissionId"]
            second_id = second["nextAction"]["inputSubmissionId"]
            self.assertEqual(service.field_submissions.get(first_id)["state"], "pending")
            self.assertEqual(service.field_submissions.get(second_id)["state"], "pending")
            self.assertNotEqual(
                service.field_submissions.get(first_id)["supersession_key"],
                service.field_submissions.get(second_id)["supersession_key"],
            )

            replacement = service.invoke(
                user_subject="user-a",
                capability_name="oa.missed_punch.approval.prepare",
                arguments={"affair_id": "affair-1"},
                idempotency_key="missed-punch-target-1-replacement",
            )
            replacement_id = replacement["nextAction"]["inputSubmissionId"]
            self.assertEqual(
                service.field_submissions.get(first_id)["state"],
                "superseded",
            )
            self.assertEqual(service.field_submissions.get(second_id)["state"], "pending")
            self.assertEqual(
                service.field_submissions.get(replacement_id)["state"],
                "pending",
            )

    def test_business_trip_prepare_requires_trusted_card_then_consumes_it_once(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            self._activate(service)
            prepared_payload = {
                "plan": {
                    "business_intent": "save_business_trip_request_draft",
                    "target": {"template_id": "template-1"},
                    "form_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": {"reason": "Test"},
                },
                "summary": {
                    "title": "保存出差申请草稿",
                    "system": "致远 OA",
                    "fields": [{"label": "事由", "value": "Test"}],
                },
            }
            with patch(
                "bscli.core.write_catalog.prepare_business_trip_draft",
                return_value=prepared_payload,
            ) as prepare_draft:
                started = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.prepare",
                    arguments={},
                    idempotency_key="business-trip-fields-1",
                )
                self.assertEqual(started["status"], "requires_user_action")
                self.assertEqual(started["error"]["code"], "FIELD_INPUT_REQUIRED")
                submission_id = started["nextAction"]["inputSubmissionId"]
                submission = service.field_submissions.get(submission_id)
                self.assertEqual(
                    datetime.fromisoformat(submission["expires_at"])
                    - datetime.fromisoformat(submission["created_at"]),
                    timedelta(minutes=30),
                )
                _submit_business_trip_fields(service, submission_id)
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.prepare",
                    arguments={"input_submission_id": submission_id},
                    idempotency_key="business-trip-prepare-1",
                )

            self.assertEqual(prepared["status"], "requires_user_action")
            self.assertEqual(prepared["error"]["code"], "WRITE_AUTHORIZATION_REQUIRED")
            prepare_draft.assert_called_once()
            self.assertEqual(
                prepare_draft.call_args.args[2]["reason"],
                "Test",
            )
            self.assertNotIn("Test", str(prepared["nextAction"]))
            field_submission = service.field_submissions.get(submission_id)
            self.assertEqual(field_submission["state"], "consumed")
            authorization_id = prepared["nextAction"]["authorizationId"]
            authorization = service.write_authorizations.get(authorization_id)
            self.assertEqual(authorization["state"], "pending")
            self.assertEqual(
                datetime.fromisoformat(authorization["expires_at"])
                - datetime.fromisoformat(authorization["created_at"]),
                timedelta(minutes=30),
            )
            self.assertEqual(
                prepared["nextAction"]["then"]["capability"],
                "oa.business_trip.save_draft",
            )

            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def save(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                return {
                    "draft_saved": True,
                    "workflow_submitted": False,
                    "submitted_count": 0,
                    "verification": {"confirmed": True},
                }

            with patch(
                "bscli.core.write_catalog.save_business_trip_draft",
                side_effect=save,
            ):
                committed = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.save_draft",
                    arguments={"authorization_id": authorization_id},
                    idempotency_key="business-trip-save-1",
                )

            self.assertEqual(committed["status"], "succeeded")
            self.assertTrue(committed["result"]["draft_saved"])
            consumed = service.write_authorizations.get(authorization_id)
            self.assertEqual(consumed["state"], "consumed")
            self.assertEqual(consumed["commit_operation_id"], committed["operationId"])

    def test_interaction_resume_completes_business_trip_without_duplicate_effects(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            started = service.invoke(
                user_subject="user-a",
                capability_name="oa.business_trip.prepare",
                arguments={},
            )
            field_interaction = started["interaction"]
            self.assertEqual(field_interaction["type"], "business_input")
            self.assertEqual(field_interaction["state"], "pending")
            self.assertEqual(
                field_interaction["interactionId"],
                started["nextAction"]["interactionId"],
            )
            with self.assertRaises(InteractionNotFound):
                service.get_interaction(
                    user_subject="user-b",
                    interaction_id=field_interaction["interactionId"],
                )

            submission_id = started["nextAction"]["inputSubmissionId"]
            _submit_business_trip_fields(service, submission_id)
            completed_field = service.get_interaction(
                user_subject="user-a",
                interaction_id=field_interaction["interactionId"],
            )["interaction"]
            self.assertEqual(completed_field["state"], "completed")
            self.assertTrue(completed_field["resume"]["ready"])

            prepared_payload = {
                "plan": {
                    "business_intent": "save_business_trip_request_draft",
                    "target": {"template_id": "template-1"},
                    "form_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": {"reason": "Test"},
                },
                "summary": {
                    "title": "保存出差申请草稿",
                    "system": "致远 OA",
                    "effect": "保存待发草稿",
                    "fields": [{"label": "事由", "value": "Test"}],
                },
            }
            with patch(
                "bscli.core.write_catalog.prepare_business_trip_draft",
                return_value=prepared_payload,
            ) as prepare_draft:
                prepared = service.resume_interaction(
                    user_subject="user-a",
                    interaction_id=field_interaction["interactionId"],
                )
                repeated_prepare = service.resume_interaction(
                    user_subject="user-a",
                    interaction_id=field_interaction["interactionId"],
                )

            self.assertEqual(prepared["status"], "requires_user_action")
            self.assertEqual(
                prepared["resumedFromInteractionId"],
                field_interaction["interactionId"],
            )
            self.assertEqual(prepared["interaction"]["type"], "execution_authorization")
            self.assertTrue(
                prepared["interaction"]["presentation"][
                    "modelMustNotCollectValues"
                ]
            )
            self.assertEqual(repeated_prepare["status"], "already_resumed")
            prepare_draft.assert_called_once()

            authorization_id = prepared["nextAction"]["authorizationId"]
            authorization_interaction = prepared["interaction"]
            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def save(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                return {
                    "draft_saved": True,
                    "workflow_submitted": False,
                    "submitted_count": 0,
                    "verification": {"confirmed": True},
                }

            with patch(
                "bscli.core.write_catalog.save_business_trip_draft",
                side_effect=save,
            ) as save_draft:
                committed = service.resume_interaction(
                    user_subject="user-a",
                    interaction_id=authorization_interaction["interactionId"],
                )
                repeated_commit = service.resume_interaction(
                    user_subject="user-a",
                    interaction_id=authorization_interaction["interactionId"],
                )

            self.assertEqual(committed["status"], "succeeded")
            self.assertTrue(committed["result"]["draft_saved"])
            self.assertEqual(repeated_commit["status"], "already_resumed")
            save_draft.assert_called_once()

    def test_interaction_resume_retries_after_session_is_reauthenticated(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            started = service.invoke(
                user_subject="user-a",
                capability_name="oa.business_trip.prepare",
                arguments={},
            )
            submission_id = started["nextAction"]["inputSubmissionId"]
            interaction_id = started["interaction"]["interactionId"]
            _submit_business_trip_fields(service, submission_id)
            service.sessions.mark_expired(session["session_id"], "OA expired")

            blocked = service.resume_interaction(
                user_subject="user-a",
                interaction_id=interaction_id,
            )
            self.assertEqual(blocked["error"]["code"], "LOGIN_REQUIRED")

            service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Alice",
            )
            service.session_states.save(session["session_id"], {"cookies": []})
            prepared_payload = {
                "plan": {
                    "business_intent": "save_business_trip_request_draft",
                    "target": {"template_id": "template-1"},
                    "form_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": {"reason": "Test"},
                },
                "summary": {"title": "Draft", "fields": []},
            }
            with patch(
                "bscli.core.write_catalog.prepare_business_trip_draft",
                return_value=prepared_payload,
            ) as prepare_draft:
                resumed = service.resume_interaction(
                    user_subject="user-a",
                    interaction_id=interaction_id,
                )

            self.assertEqual(resumed["status"], "requires_user_action")
            self.assertEqual(
                resumed["error"]["code"],
                "WRITE_AUTHORIZATION_REQUIRED",
            )
            prepare_draft.assert_called_once()

    def test_business_trip_commit_before_approval_has_no_effect_and_unknown_is_durable(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            self._activate(service)
            prepared_payload = {
                "plan": {
                    "business_intent": "save_business_trip_request_draft",
                    "target": {"template_id": "template-1"},
                    "form_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": {"reason": "Test"},
                },
                "summary": {"title": "Draft", "fields": []},
            }
            with patch(
                "bscli.core.write_catalog.prepare_business_trip_draft",
                return_value=prepared_payload,
            ):
                started = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.prepare",
                    arguments={},
                )
                submission_id = started["nextAction"]["inputSubmissionId"]
                _submit_business_trip_fields(service, submission_id)
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.prepare",
                    arguments={"input_submission_id": submission_id},
                )
            authorization_id = prepared["nextAction"]["authorizationId"]

            with patch("bscli.core.write_catalog.save_business_trip_draft") as save:
                blocked = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.save_draft",
                    arguments={"authorization_id": authorization_id},
                )
            self.assertEqual(blocked["status"], "requires_user_action")
            save.assert_not_called()

            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def uncertain(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                raise BusinessTripOutcomeUnknown("readback failed")

            with patch(
                "bscli.core.write_catalog.save_business_trip_draft",
                side_effect=uncertain,
            ):
                unknown = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.save_draft",
                    arguments={"authorization_id": authorization_id},
                    idempotency_key="business-trip-unknown-1",
                )

            self.assertEqual(unknown["status"], "unknown")
            self.assertEqual(unknown["error"]["code"], "RESULT_UNKNOWN")
            self.assertEqual(
                service.operations.get(unknown["operationId"])["status"],
                "unknown",
            )

    def test_leave_commit_turns_continuable_oa_validation_into_second_authorization(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            spec = service.registry.get("oa.leave.submit")
            previous_validation = {
                "code": "PRE_SUBMIT_CONFIRMATION",
                "message": "提交前提示",
                "force_check": False,
                "can_continue": True,
                "fingerprint": "sha256:previous",
            }
            plan = {
                "business_intent": "submit_leave_request",
                "user_subject": "user-a",
                "session_binding": {
                    "session_id": session["session_id"],
                    "expected_principal_ref": session["expected_principal_ref"],
                    "downstream_principal_ref": session["downstream_principal_ref"],
                    "last_verified_at": session["last_verified_at"],
                },
                "business_validation_overrides": [previous_validation],
            }
            authorization = service.write_authorizations.create(
                user_subject="user-a",
                system_id="oa",
                session_id=session["session_id"],
                capability_name=spec.name,
                capability_version=spec.version,
                prepare_operation_id="prepare-leave",
                plan=plan,
                summary={
                    "title": "提交请假申请",
                    "system": "致远 OA",
                    "fields": [{"label": "请假事由", "value": "个人事务"}],
                },
                card_base_url=service.trusted_card_base_url,
            )
            csrf = service.write_authorizations.issue_csrf(
                authorization["authorization_id"]
            )
            service.write_authorizations.decide(
                authorization["authorization_id"],
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            validation = {
                "code": "3003",
                "message": "请假时长需要确认",
                "force_check": False,
                "can_continue": True,
                "fingerprint": "sha256:validation",
            }

            def needs_confirmation(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                raise LeaveBusinessValidationRequired(validation)

            with patch(
                "bscli.core.write_catalog.submit_leave_request",
                side_effect=needs_confirmation,
            ):
                response = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.leave.submit",
                    arguments={"authorization_id": authorization["authorization_id"]},
                )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(
                response["error"]["code"],
                "OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED",
            )
            continued_id = response["nextAction"]["authorizationId"]
            continued = service.write_authorizations.get(
                continued_id,
                include_plan=True,
            )
            self.assertEqual(
                continued["plan"]["business_validation_overrides"],
                [previous_validation, validation],
            )
            self.assertIn("请假时长需要确认", str(continued["summary"]))
            self.assertEqual(
                service.write_authorizations.get(
                    authorization["authorization_id"]
                )["state"],
                "consumed",
            )
            continued_csrf = service.write_authorizations.issue_csrf(continued_id)
            service.write_authorizations.decide(
                continued_id,
                decision="approve",
                csrf_token=continued_csrf,
                csrf_cookie=continued_csrf,
            )

            def completes_after_confirmation(
                _adapter,
                _worker,
                resumed_plan,
                *,
                enter_commit_boundary,
            ):
                self.assertEqual(
                    resumed_plan["business_validation_overrides"],
                    [previous_validation, validation],
                )
                enter_commit_boundary()
                return {"workflow_submitted": True}

            with patch(
                "bscli.core.write_catalog.submit_leave_request",
                side_effect=completes_after_confirmation,
            ):
                resumed = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.leave.submit",
                    arguments={"authorization_id": continued_id},
                )

            self.assertEqual(resumed["status"], "succeeded")
            self.assertTrue(resumed["result"]["workflow_submitted"])
            self.assertEqual(
                service.write_authorizations.get(continued_id)["state"],
                "consumed",
            )

    def test_business_trip_commit_turns_oa_confirmation_into_second_authorization(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            spec = service.registry.get("oa.business_trip.submit")
            plan = {
                "business_intent": "submit_business_trip_request",
                "user_subject": "user-a",
                "session_binding": {
                    "session_id": session["session_id"],
                    "expected_principal_ref": session["expected_principal_ref"],
                    "downstream_principal_ref": session["downstream_principal_ref"],
                    "last_verified_at": session["last_verified_at"],
                },
            }
            authorization = service.write_authorizations.create(
                user_subject="user-a",
                system_id="oa",
                session_id=session["session_id"],
                capability_name=spec.name,
                capability_version=spec.version,
                prepare_operation_id="prepare-business-trip",
                plan=plan,
                summary={
                    "title": "Submit business trip",
                    "system": "OA",
                    "fields": [{"label": "Destination", "value": "Qingdao"}],
                },
                card_base_url=service.trusted_card_base_url,
            )
            csrf = service.write_authorizations.issue_csrf(
                authorization["authorization_id"]
            )
            service.write_authorizations.decide(
                authorization["authorization_id"],
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            validation = {
                "code": "NATIVE_CONFIRMATION",
                "message": "Confirm business trip submission",
                "force_check": False,
                "can_continue": True,
                "fingerprint": "sha256:business-trip-confirmation",
            }

            def needs_confirmation(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                raise BusinessTripBusinessValidationRequired(validation)

            with patch(
                "bscli.core.write_catalog.submit_business_trip_request",
                side_effect=needs_confirmation,
            ):
                response = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.submit",
                    arguments={"authorization_id": authorization["authorization_id"]},
                )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(
                response["error"]["code"],
                "OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED",
            )
            continued_id = response["nextAction"]["authorizationId"]
            continued = service.write_authorizations.get(
                continued_id,
                include_plan=True,
            )
            self.assertEqual(
                continued["plan"]["business_validation_overrides"],
                [validation],
            )
            self.assertIn("Submit business trip", continued["summary"]["title"])
            self.assertIn(validation["message"], str(continued["summary"]))
            self.assertEqual(
                service.write_authorizations.get(
                    authorization["authorization_id"]
                )["state"],
                "consumed",
            )

    def test_business_trip_commit_surfaces_oa_business_rule_rejection(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            session = self._activate(service)
            spec = service.registry.get("oa.business_trip.submit")
            plan = {
                "business_intent": "submit_business_trip_request",
                "user_subject": "user-a",
                "session_binding": {
                    "session_id": session["session_id"],
                    "expected_principal_ref": session["expected_principal_ref"],
                    "downstream_principal_ref": session["downstream_principal_ref"],
                    "last_verified_at": session["last_verified_at"],
                },
            }
            authorization = service.write_authorizations.create(
                user_subject="user-a",
                system_id="oa",
                session_id=session["session_id"],
                capability_name=spec.name,
                capability_version=spec.version,
                prepare_operation_id="prepare-business-trip-rejected",
                plan=plan,
                summary={
                    "title": "Submit business trip",
                    "system": "OA",
                    "fields": [{"label": "Destination", "value": "Qingdao"}],
                },
                card_base_url=service.trusted_card_base_url,
            )
            csrf = service.write_authorizations.issue_csrf(
                authorization["authorization_id"]
            )
            service.write_authorizations.decide(
                authorization["authorization_id"],
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            reason = "The selected interval is not eligible for this request."

            def blocked(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                raise BusinessTripSubmissionBlocked(reason)

            with patch(
                "bscli.core.write_catalog.submit_business_trip_request",
                side_effect=blocked,
            ):
                response = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.business_trip.submit",
                    arguments={"authorization_id": authorization["authorization_id"]},
                )

            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "OA_BUSINESS_RULE_REJECTED")
            self.assertEqual(response["error"]["message"], reason)
            self.assertEqual(
                service.write_authorizations.get(
                    authorization["authorization_id"]
                )["state"],
                "consumed",
            )

    def test_business_trip_field_submission_cannot_cross_users(self):
        with TemporaryDirectory() as tmp:
            worker = FakeWorker()
            service = self._service(tmp, worker)
            self._activate(service)
            started = service.invoke(
                user_subject="user-a",
                capability_name="oa.business_trip.prepare",
                arguments={},
            )
            submission_id = started["nextAction"]["inputSubmissionId"]
            _submit_business_trip_fields(service, submission_id)
            session = service.sessions.get_or_create(
                user_subject="user-b",
                system_id="oa",
                expected_principal_ref="Bob",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Bob",
            )
            service.session_states.save(session["session_id"], {"cookies": []})

            response = service.invoke(
                user_subject="user-b",
                capability_name="oa.business_trip.prepare",
                arguments={"input_submission_id": submission_id},
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "FIELD_INPUT_UNAVAILABLE")
            self.assertEqual(
                service.field_submissions.get(submission_id)["state"],
                "submitted",
            )

    def test_missed_punch_approval_field_card_freezes_affair_context(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            started = service.invoke(
                user_subject="user-a",
                capability_name="oa.missed_punch.approval.prepare",
                arguments={"affair_id": "affair-1"},
            )
            submission_id = started["nextAction"]["inputSubmissionId"]
            interaction_id = started["interaction"]["interactionId"]
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=interaction_id,
                ),
                frozenset({"oa:write:approval"}),
            )
            submission = service.field_submissions.get(submission_id)
            self.assertEqual(
                submission["form_schema"]["_agentbridge_resume_arguments"],
                {"affair_id": "affair-1"},
            )
            csrf = service.field_submissions.issue_csrf(submission_id)
            service.field_submissions.submit(
                submission_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
                values={"opinion": "同意"},
            )

            mismatched = service.invoke(
                user_subject="user-a",
                capability_name="oa.missed_punch.approval.prepare",
                arguments={
                    "affair_id": "affair-2",
                    "input_submission_id": submission_id,
                },
            )
            self.assertEqual(mismatched["status"], "requires_user_action")
            self.assertEqual(mismatched["error"]["code"], "FIELD_INPUT_UNAVAILABLE")

            prepared_payload = {
                "plan": {
                    "business_intent": "approve_missed_punch_request",
                    "target": {"affair_id": "affair-1", "title": "补签申请"},
                    "action_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": {"opinion": "同意"},
                },
                "summary": {"title": "审批补签申请", "system": "致远 OA", "fields": []},
            }
            with patch(
                "bscli.core.write_catalog.prepare_missed_punch_approval",
                return_value=prepared_payload,
            ) as prepare:
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.missed_punch.approval.prepare",
                    arguments={
                        "affair_id": "affair-1",
                        "input_submission_id": submission_id,
                    },
                )

            self.assertEqual(prepared["status"], "requires_user_action")
            self.assertEqual(prepared["error"]["code"], "WRITE_AUTHORIZATION_REQUIRED")
            self.assertEqual(
                prepared["nextAction"]["then"]["capability"],
                "oa.missed_punch.approve",
            )
            self.assertEqual(
                prepare.call_args.args[2],
                {"affair_id": "affair-1", "opinion": "同意"},
            )

    def test_missed_punch_batch_advances_cards_until_every_item_succeeds(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            task_id = self._ensure_host_task(service)
            pending_items = [
                {
                    "affair_id": "affair-2",
                    "title": "【HR】补签申请单-Bob",
                    "sender": "Bob",
                    "date": "2026-08-28 09:00",
                    "category": "HR",
                },
                {
                    "affair_id": "affair-1",
                    "title": "【HR】补签申请单-Alice",
                    "sender": "Alice",
                    "date": "2026-08-27 09:00",
                    "category": "HR",
                },
                {
                    "affair_id": "ignored",
                    "title": "加班申请审核单",
                    "sender": "Carol",
                    "date": "2026-08-26 09:00",
                },
            ]
            prepared_targets = []
            committed_targets = []

            def prepare(_adapter, _worker, arguments):
                prepared_targets.append(arguments["affair_id"])
                return {
                    "plan": {
                        "business_intent": "approve_missed_punch_request",
                        "target": {
                            "affair_id": arguments["affair_id"],
                            "title": arguments.get("target_title") or "补签申请单",
                        },
                        "action_contract": {
                            "version": "v1",
                            "fingerprint": f"sha256:{arguments['affair_id']}",
                        },
                        "exact_input": {"opinion": arguments["opinion"]},
                    },
                    "summary": {
                        "title": "审批补签申请",
                        "system": "致远 OA",
                        "fields": [],
                    },
                }

            def commit(_adapter, _worker, plan, *, enter_commit_boundary):
                enter_commit_boundary()
                affair_id = plan["target"]["affair_id"]
                committed_targets.append(affair_id)
                return {
                    "workflow_approved": True,
                    "verification": {
                        "confirmed": True,
                        "method": "pending_disappearance",
                        "affair_id": affair_id,
                    },
                }

            with (
                patch.object(
                    service.adapter,
                    "list_workflows",
                    return_value={"count": 3, "items": pending_items},
                ) as list_pending,
                patch(
                    "bscli.core.write_catalog.prepare_missed_punch_approval",
                    side_effect=prepare,
                ),
                patch(
                    "bscli.core.write_catalog.approve_missed_punch_request",
                    side_effect=commit,
                ),
            ):
                first_field = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.missed_punch.approval.batch.prepare",
                    arguments={"limit": 10},
                    task_id=task_id,
                )
                self.assertEqual(first_field["error"]["code"], "FIELD_INPUT_REQUIRED")
                self.assertEqual(first_field["batch"]["totalCount"], 2)
                self.assertEqual(first_field["batch"]["currentOrdinal"], 1)
                first_submission = service.field_submissions.get(
                    first_field["nextAction"]["inputSubmissionId"]
                )
                self.assertEqual(
                    first_submission["form_schema"]["title"],
                    "填写补签审批意见（第 1/2 条）",
                )

                first_authorization = self._submit_and_resume_opinion(
                    service,
                    first_field,
                )
                second_field = self._approve_and_resume_authorization(
                    service,
                    first_authorization,
                )

                self.assertEqual(second_field["error"]["code"], "FIELD_INPUT_REQUIRED")
                self.assertEqual(second_field["completedBatchItemOrdinal"], 1)
                self.assertEqual(second_field["batch"]["currentOrdinal"], 2)
                self.assertEqual(second_field["batch"]["succeededCount"], 1)
                second_submission = service.field_submissions.get(
                    second_field["nextAction"]["inputSubmissionId"]
                )
                self.assertEqual(
                    second_submission["form_schema"]["title"],
                    "填写补签审批意见（第 2/2 条）",
                )

                second_authorization = self._submit_and_resume_opinion(
                    service,
                    second_field,
                )
                completed = self._approve_and_resume_authorization(
                    service,
                    second_authorization,
                )

            list_pending.assert_called_once()
            self.assertEqual(prepared_targets, ["affair-1", "affair-2"])
            self.assertEqual(committed_targets, ["affair-1", "affair-2"])
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["batch"]["state"], "succeeded")
            self.assertEqual(completed["batch"]["succeededCount"], 2)
            self.assertEqual(
                service.tasks.get_task(task_id, user_subject="user-a")["status"],
                "succeeded",
            )

    def test_workflow_revoke_card_prefills_comment_and_freezes_affair_context(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            started = service.invoke(
                user_subject="user-a",
                capability_name="oa.workflow.revoke.prepare",
                arguments={
                    "affair_id": "affair-1",
                    "repeal_comment": "自动化测试结束",
                },
            )
            submission_id = started["nextAction"]["inputSubmissionId"]
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=started["interaction"]["interactionId"],
                ),
                frozenset({"oa:write:revoke"}),
            )
            submission = service.field_submissions.get(submission_id)
            self.assertEqual(
                submission["form_schema"]["_agentbridge_resume_arguments"],
                {"affair_id": "affair-1"},
            )
            self.assertEqual(
                submission["form_schema"]["fields"][0]["value"],
                "自动化测试结束",
            )
            csrf = service.field_submissions.issue_csrf(submission_id)
            service.field_submissions.submit(
                submission_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
                values={"repeal_comment": "自动化测试结束"},
            )
            prepared_payload = {
                "plan": {
                    "business_intent": "revoke_sent_workflow",
                    "target": {"affair_id": "affair-1", "title": "请假申请"},
                    "action_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": {"repeal_comment": "自动化测试结束"},
                },
                "summary": {"title": "撤销已发流程", "system": "致远 OA", "fields": []},
            }
            with patch(
                "bscli.core.write_catalog.prepare_workflow_revoke",
                return_value=prepared_payload,
            ) as prepare:
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.workflow.revoke.prepare",
                    arguments={
                        "affair_id": "affair-1",
                        "input_submission_id": submission_id,
                    },
                )

            self.assertEqual(prepared["status"], "requires_user_action")
            self.assertEqual(prepared["error"]["code"], "WRITE_AUTHORIZATION_REQUIRED")
            self.assertEqual(
                prepared["nextAction"]["then"]["capability"],
                "oa.workflow.revoke",
            )
            self.assertEqual(
                prepare.call_args.args[2],
                {"affair_id": "affair-1", "repeal_comment": "自动化测试结束"},
            )
    def test_meeting_interaction_uses_meeting_scope_and_generic_commit(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            initial_arguments = {
                "subject": "智能体测试",
                "room": "三号会议室",
                "start_time": "2026-07-20 14:00",
                "end_time": "2026-07-20 16:00",
            }
            with patch(
                "bscli.core.write_catalog.build_meeting_field_card_schema",
                return_value=MEETING_FIELD_CARD_SCHEMA,
            ) as build_schema:
                started = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.meeting.create.prepare",
                    arguments=initial_arguments,
                )
            build_schema.assert_called_once()
            self.assertEqual(build_schema.call_args.args[2], initial_arguments)
            submission_id = started["nextAction"]["inputSubmissionId"]
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=started["interaction"]["interactionId"],
                ),
                frozenset({"oa:write:meeting"}),
            )
            fields = {
                "subject": "智能体测试",
                "room": "3号会议室",
                "start_time": "2026-07-20 14:00",
                "end_time": "2026-07-20 16:00",
            }
            csrf = service.field_submissions.issue_csrf(submission_id)
            service.field_submissions.submit(
                submission_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
                values=fields,
            )
            prepared_payload = {
                "plan": {
                    "business_intent": "create_meeting",
                    "target": {"room_id": "room-3", "room_name": "3号会议室"},
                    "action_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": fields,
                },
                "summary": {"title": "创建并发送会议", "system": "致远 OA", "fields": []},
            }
            with patch(
                "bscli.core.write_catalog.prepare_meeting_create",
                return_value=prepared_payload,
            ):
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.meeting.create.prepare",
                    arguments={"input_submission_id": submission_id},
                )
            authorization_id = prepared["nextAction"]["authorizationId"]
            authorization_interaction_id = prepared["interaction"]["interactionId"]
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=authorization_interaction_id,
                ),
                frozenset({"oa:write:meeting"}),
            )
            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def create(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                return {"meeting_created": True, "meeting_sent": True, "submitted_count": 1}

            with patch("bscli.core.write_catalog.create_meeting", side_effect=create):
                committed = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.meeting.create",
                    arguments={"authorization_id": authorization_id},
                )
            self.assertEqual(committed["status"], "succeeded")
            self.assertTrue(committed["result"]["meeting_created"])
            self.assertEqual(
                service.write_authorizations.get(authorization_id)["state"],
                "consumed",
            )

    def test_meeting_room_application_uses_dynamic_card_and_private_commit(self):
        with TemporaryDirectory() as tmp:
            service = self._service(tmp, FakeWorker())
            self._activate(service)
            initial_arguments = {
                "purpose": "项目讨论",
                "room": "三号会议室",
                "start_time": "2026-09-10 14:00",
                "end_time": "2026-09-10 15:00",
            }
            with patch(
                "bscli.core.write_catalog.build_meeting_room_application_field_card_schema",
                return_value=MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA,
            ) as build_schema:
                started = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.meeting_room.application.prepare",
                    arguments=initial_arguments,
                )
            build_schema.assert_called_once()
            submission_id = started["nextAction"]["inputSubmissionId"]
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="user-a",
                    interaction_id=started["interaction"]["interactionId"],
                ),
                frozenset({"oa:write:meeting"}),
            )

            fields = {
                "purpose": "项目讨论",
                "room": "3号会议室",
                "start_time": "2026-09-10 14:00",
                "end_time": "2026-09-10 15:00",
            }
            csrf = service.field_submissions.issue_csrf(submission_id)
            service.field_submissions.submit(
                submission_id,
                csrf_token=csrf,
                csrf_cookie=csrf,
                values=fields,
            )
            prepared_payload = {
                "plan": {
                    "business_intent": "apply_meeting_room",
                    "target": {"room_id": "room-3", "room_name": "3号会议室"},
                    "action_contract": {"version": "v1", "fingerprint": "sha256:test"},
                    "exact_input": fields,
                },
                "summary": {"title": "申请会议室", "system": "致远 OA", "fields": []},
            }
            with patch(
                "bscli.core.write_catalog.prepare_meeting_room_application",
                return_value=prepared_payload,
            ):
                prepared = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.meeting_room.application.prepare",
                    arguments={"input_submission_id": submission_id},
                )
            authorization_id = prepared["nextAction"]["authorizationId"]
            csrf = service.write_authorizations.issue_csrf(authorization_id)
            service.write_authorizations.decide(
                authorization_id,
                decision="approve",
                csrf_token=csrf,
                csrf_cookie=csrf,
            )

            def create(_adapter, _worker, _plan, *, enter_commit_boundary):
                enter_commit_boundary()
                return {
                    "meeting_room_application_created": True,
                    "meeting_created": False,
                    "submitted_count": 1,
                }

            with patch(
                "bscli.core.write_catalog.create_meeting_room_application",
                side_effect=create,
            ):
                committed = service.invoke(
                    user_subject="user-a",
                    capability_name="oa.meeting_room.application.create",
                    arguments={"authorization_id": authorization_id},
                )
            self.assertEqual(committed["status"], "succeeded")
            self.assertTrue(
                committed["result"]["meeting_room_application_created"]
            )
            self.assertEqual(
                service.write_authorizations.get(authorization_id)["state"],
                "consumed",
            )
    @staticmethod
    def _service(tmp, worker, *, keepalive_lease_seconds=None):
        return authorized_service(
            home=Path(tmp),
            base_url=BASE_URL,
            worker_factory=lambda _session, _adapter: worker,
            session_keepalive_lease_seconds=keepalive_lease_seconds,
        )

    @staticmethod
    def _activate(service):
        session = service.sessions.get_or_create(
            user_subject="user-a",
            system_id="oa",
            expected_principal_ref="Alice",
        )
        session = service.sessions.activate(
            session["session_id"],
            observed_principal_ref="Alice",
        )
        service.session_states.save(session["session_id"], {"cookies": []})
        return session

    @staticmethod
    def _ensure_host_task(service):
        response = service.ensure_host_task(
            user_subject="user-a",
            token_id="token-a",
            agent_host="test-host",
            host_task_key="batch-test-run",
            endpoint_key="batch-test-endpoint",
            client_type="web",
            external_subject="user-a",
            conversation_ref="batch-test-conversation",
            title="处理所有补签申请单",
        )
        return response["task"]["taskId"]

    @staticmethod
    def _submit_and_resume_opinion(service, field_response):
        submission_id = field_response["nextAction"]["inputSubmissionId"]
        csrf = service.field_submissions.issue_csrf(submission_id)
        service.field_submissions.submit(
            submission_id,
            csrf_token=csrf,
            csrf_cookie=csrf,
            values={"opinion": "同意"},
        )
        return service.resume_interaction(
            user_subject="user-a",
            interaction_id=field_response["interaction"]["interactionId"],
        )

    @staticmethod
    def _approve_and_resume_authorization(service, authorization_response):
        authorization_id = authorization_response["nextAction"]["authorizationId"]
        csrf = service.write_authorizations.issue_csrf(authorization_id)
        service.write_authorizations.decide(
            authorization_id,
            decision="approve",
            csrf_token=csrf,
            csrf_cookie=csrf,
        )
        return service.resume_interaction(
            user_subject="user-a",
            interaction_id=authorization_response["interaction"]["interactionId"],
        )


class FakeWorker:
    def __init__(self):
        self.restored = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def restore_session_state(self, state):
        self.restored = state

    def capture_session_state(self):
        return {"cookies": []}


class InaccessibleSessionStateStore:
    def __init__(self):
        self.deleted = False

    def load(self, _session_id):
        raise SessionStateAccessDenied("different Windows security principal")

    def delete(self, _session_id):
        self.deleted = True


def _business_trip_arguments():
    return {
        "start_time": "2026-07-13 09:00",
        "end_time": "2026-07-13 18:00",
        "travel_mode": "火车",
        "origin": "济南",
        "destination": "青岛",
        "reason": "Test",
        "has_direct_supervisor": False,
    }


def _submit_business_trip_fields(service, submission_id):
    csrf = service.field_submissions.issue_csrf(submission_id)
    service.field_submissions.submit(
        submission_id,
        csrf_token=csrf,
        csrf_cookie=csrf,
        values=_business_trip_arguments(),
    )


if __name__ == "__main__":
    unittest.main()
