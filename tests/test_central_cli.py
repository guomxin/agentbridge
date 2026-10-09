import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from agentbridge.cli.main import main
from agentbridge.core.tasks import TaskHubStore


class CentralCliTests(unittest.TestCase):
    def test_omnichannel_diagnostics_enforces_expected_active_endpoints(self):
        with TemporaryDirectory() as tmp:
            tasks = TaskHubStore(Path(tmp) / "agentbridge.db")
            tasks.ensure_endpoint(
                user_subject="user-a",
                token_id="token-a",
                agent_host="openclaw",
                endpoint_key="telegram:*:1001",
                client_type="telegram",
                external_subject="1001",
                conversation_ref="agent:main:telegram:direct:1001",
            )
            with redirect_stdout(io.StringIO()) as stdout:
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "diagnostics",
                        "omnichannel",
                        "--expect-endpoint",
                        "user-a=telegram",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "succeeded")
        self.assertTrue(payload["report"]["isolation"]["passed"])
        self.assertTrue(payload["expectations"][0]["present"])

    def test_capability_list_exposes_reads_and_governed_write_workflows(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(["--home", tmp, "capability", "list"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["protocolVersion"], "0.1")
        self.assertEqual(len(payload["capabilities"]), 110)
        capabilities = {item["name"]: item for item in payload["capabilities"]}
        self.assertIn("oa.template.list", capabilities)
        self.assertIn("oa.workflow.pending.list", capabilities)
        self.assertIn("oa.workflow.sent.list", capabilities)
        self.assertIn("oa.document.certificate.search", capabilities)
        self.assertIn("taihua.work_log.my.list", capabilities)
        self.assertIn("taihua.work_log.team.list", capabilities)
        self.assertIn("taihua.project.search", capabilities)
        self.assertIn("oa.missed_punch.approval.batch.prepare", capabilities)
        for capability_name in (
            "oa.meeting_room.application.prepare",
            "oa.meeting_room.application.create",
            "oa.meeting_room.application.cancel.prepare",
            "oa.meeting_room.application.cancel",
        ):
            self.assertEqual(capabilities[capability_name]["effect"], "controlled_write")
        for capability_name in (
            "smartlight.system.overview",
            "smartlight.lamppost.list",
            "smartlight.alarm.list",
            "smartlight.alarm.remark.get",
            "smartlight.inspection_task.list",
            "smartlight.leakage.summary",
            "smartlight.asset.search",
            "smartlight.asset.detail",
            "smartlight.alarm.analysis",
            "smartlight.inspection_task.detail",
            "smartlight.leakage.analysis",
            "smartlight.energy_record.list",
            "smartlight.energy.analysis",
            "smartlight.lamp.survey.records",
            "smartlight.rtu.leakage_alarm.list",
            "smartlight.rtu.leakage_alarm.analysis",
            "smartlight.rtu.off_hours_current.list",
            "smartlight.inspection_log.list",
            "smartlight.maintenance_record.list",
            "smartlight.report.export",
        ):
            self.assertEqual(capabilities[capability_name]["effect"], "read")
        for capability_name in (
            "smartlight.alarm.remark.update.prepare",
            "smartlight.alarm.remark.update",
            "smartlight.alarm.work_area.submit.prepare",
            "smartlight.alarm.work_area.submit",
            "smartlight.alarm.work_area.revoke.prepare",
            "smartlight.alarm.work_area.revoke",
        ):
            self.assertEqual(
                capabilities[capability_name]["effect"],
                "reversible_write",
            )
        for capability_name in (
            "smartlight.alarm.dispose.prepare",
            "smartlight.alarm.dispose",
        ):
            self.assertEqual(
                capabilities[capability_name]["effect"],
                "controlled_write",
            )
        for capability_name in (
            "yuque.public_books.list",
            "yuque.document.catalog",
            "yuque.document.search",
            "yuque.document.read",
        ):
            self.assertEqual(capabilities[capability_name]["effect"], "read")
        self.assertEqual(
            capabilities["taihua.work_log.create"]["effect"],
            "controlled_write",
        )
        self.assertEqual(
            capabilities["oa.business_trip.prepare"]["effect"],
            "reversible_write",
        )
        self.assertEqual(
            capabilities["oa.business_trip.save_draft"]["effect"],
            "reversible_write",
        )
        self.assertEqual(
            capabilities["oa.business_trip.submit"]["effect"],
            "controlled_write",
        )
        self.assertEqual(
            capabilities["oa.leave.save_draft"]["effect"],
            "reversible_write",
        )
        self.assertEqual(
            capabilities["oa.leave.submit"]["effect"],
            "controlled_write",
        )
        self.assertEqual(
            capabilities["oa.missed_punch.save_draft"]["effect"],
            "reversible_write",
        )
        self.assertEqual(
            capabilities["oa.missed_punch.approve"]["effect"],
            "controlled_write",
        )
        self.assertEqual(
            capabilities["oa.meeting.create"]["effect"],
            "controlled_write",
        )
        self.assertEqual(
            capabilities["oa.workflow.revoke.prepare"]["effect"],
            "controlled_write",
        )
        self.assertEqual(
            capabilities["oa.workflow.revoke"]["effect"],
            "controlled_write",
        )
        for capability_name in (
            "oa.efficiency_data.approve",
            "oa.business_trip.approve",
            "oa.labor_contract_renewal.approve",
            "oa.intellectual_property_declaration.approve",
            "oa.leave.approve",
            "oa.overtime.approve",
            "oa.resignation.approve",
            "oa.work_handover.approve",
            "oa.flight_application.approve",
            "oa.attendance_confirmation.confirm",
            "oa.travel_expense.approve",
            "oa.weekly_report.acknowledge",
            "oa.standard_collaboration.approve",
        ):
            self.assertEqual(
                capabilities[capability_name]["effect"],
                "controlled_write",
            )
        prepare_schema = capabilities["oa.business_trip.prepare"]["input_schema"]
        self.assertEqual(
            set(prepare_schema["properties"]),
            {
                "start_time",
                "end_time",
                "travel_mode",
                "origin",
                "destination",
                "reason",
                "has_direct_supervisor",
                "input_submission_id",
            },
        )

    def test_session_status_returns_not_found_without_opening_browser(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(
                [
                    "--home",
                    tmp,
                    "session",
                    "status",
                    "--system",
                    "oa",
                    "--user-subject",
                    "user-a",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "not_found")
        self.assertEqual(payload["systemId"], "oa")
        self.assertEqual(payload["statusSource"], "registry")
        self.assertIsNone(payload["checkedAt"])
        self.assertFalse((Path(tmp) / "profiles").exists())

    def test_unconfigured_user_cannot_invoke_business_capability(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            code = main(["--home", tmp, "capability", "invoke", "oa.template.list", "--user-subject", "new-user"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "PERMISSION_DENIED")

    def test_capability_invoke_without_session_returns_login_action_and_operation(self):
        with TemporaryDirectory() as tmp:
            from agentbridge.core.user_grants import UserGrants
            UserGrants(Path(tmp) / "agentbridge.db").save("user-a", ["oa.leave.draft"],
                expected_revision=0, actor="test-admin", reason="Authorize form catalog test")
            with redirect_stdout(io.StringIO()) as stdout:
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "capability",
                        "invoke",
                        "oa.template.list",
                        "--user-subject",
                        "user-a",
                        "--idempotency-key",
                        "first-list",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "requires_user_action")
            self.assertEqual(payload["error"]["code"], "LOGIN_REQUIRED")
            self.assertEqual(payload["nextAction"]["type"], "session_login")
            self.assertFalse((Path(tmp) / "profiles").exists())

            with redirect_stdout(io.StringIO()) as operation_stdout:
                operation_exit = main(
                    ["--home", tmp, "operation", "get", payload["operationId"]]
                )
            operation = json.loads(operation_stdout.getvalue())["operation"]
            self.assertEqual(operation_exit, 0)
            self.assertEqual(operation["status"], "requires_user_action")

    def test_workflow_capability_uses_generic_central_session_handler(self):
        with TemporaryDirectory() as tmp:
            service = MagicMock()
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "request-1",
                "operationId": "operation-1",
                "status": "succeeded",
                "result": {"collection": "pending", "count": 0, "items": []},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }

            with (
                patch("agentbridge.cli.main.CentralCapabilityService", return_value=service),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "capability",
                        "invoke",
                        "oa.workflow.pending.list",
                        "--user-subject",
                        "user-a",
                        "--json",
                        '{"limit": 5}',
                        "--idempotency-key",
                        "pending-generic-handler",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "succeeded")
        service.invoke.assert_called_once_with(
            user_subject="user-a",
            capability_name="oa.workflow.pending.list",
            arguments={"limit": 5},
            idempotency_key="pending-generic-handler",
            request_id=None,
        )

    def test_expired_workflow_session_returns_login_action_and_deletes_secret(self):
        with TemporaryDirectory() as tmp:
            service = MagicMock()
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "request-1",
                "operationId": "operation-1",
                "status": "requires_user_action",
                "result": None,
                "error": {"code": "LOGIN_REQUIRED", "message": "not active"},
                "evidenceRefs": [],
                "nextAction": {
                    "type": "session_login",
                    "system": "oa",
                    "userSubject": "user-a",
                    "sessionState": "expired",
                },
                "reused": False,
            }

            with (
                patch("agentbridge.cli.main.CentralCapabilityService", return_value=service),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "capability",
                        "invoke",
                        "oa.workflow.done.list",
                        "--user-subject",
                        "user-a",
                        "--idempotency-key",
                        "expired-workflow-session",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "requires_user_action")
        self.assertEqual(payload["error"]["code"], "LOGIN_REQUIRED")
        self.assertEqual(payload["nextAction"]["sessionState"], "expired")

    def test_session_login_creates_authentication_card_challenge_without_browser(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(
                [
                    "--home",
                    tmp,
                    "session",
                    "login",
                    "--system",
                    "oa",
                    "--user-subject",
                    "user-a",
                    "--expected-principal",
                    "Alice",
                    "--card-base-url",
                    "http://127.0.0.1:8780",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "requires_user_action")
        self.assertEqual(payload["challenge"]["type"], "legacy_form_login")
        self.assertEqual(payload["challenge"]["state"], "pending")
        self.assertEqual(payload["challenge"]["systemName"], "致远 OA")
        self.assertTrue(payload["challenge"]["cardUrl"].startswith("http://127.0.0.1:8780/auth/"))
        self.assertEqual(payload["interaction"]["type"], "credential")
        self.assertNotIn("password", json.dumps(payload).lower())

    def test_session_login_prints_reused_active_session_without_card(self):
        with TemporaryDirectory() as tmp:
            service = MagicMock()
            service.start_login.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "sessionId": "session-1",
                "result": {
                    "authenticated": True,
                    "templateCount": 118,
                    "transport": "central_http_session",
                },
                "nextAction": None,
                "reused": True,
            }
            with (
                patch("agentbridge.cli.main.CentralCapabilityService", return_value=service),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "session",
                        "login",
                        "--system",
                        "oa",
                        "--user-subject",
                        "user-a",
                        "--expected-principal",
                        "Alice",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "succeeded")
        self.assertTrue(payload["reused"])
        self.assertNotIn("challenge", payload)
        service.start_login.assert_called_once()

    def test_interaction_get_and_resume_use_host_independent_service_methods(self):
        with TemporaryDirectory() as tmp:
            service = MagicMock()
            service.get_interaction.return_value = {
                "protocolVersion": "0.1",
                "interaction": {
                    "interactionId": "interaction-123456",
                    "type": "business_input",
                    "state": "completed",
                    "resume": {"ready": True, "completed": False},
                },
            }
            service.resume_interaction.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "resumedFromInteractionId": "interaction-123456",
            }
            with (
                patch("agentbridge.cli.main.CentralCapabilityService", return_value=service),
                redirect_stdout(io.StringIO()) as get_stdout,
            ):
                get_exit = main(
                    [
                        "--home",
                        tmp,
                        "interaction",
                        "get",
                        "interaction-123456",
                        "--user-subject",
                        "user-a",
                    ]
                )
            with (
                patch("agentbridge.cli.main.CentralCapabilityService", return_value=service),
                redirect_stdout(io.StringIO()) as resume_stdout,
            ):
                resume_exit = main(
                    [
                        "--home",
                        tmp,
                        "interaction",
                        "resume",
                        "interaction-123456",
                        "--user-subject",
                        "user-a",
                        "--idempotency-key",
                        "resume-1",
                    ]
                )

        self.assertEqual(get_exit, 0)
        self.assertEqual(resume_exit, 0)
        self.assertEqual(
            json.loads(get_stdout.getvalue())["interaction"]["state"],
            "completed",
        )
        self.assertEqual(json.loads(resume_stdout.getvalue())["status"], "succeeded")
        service.get_interaction.assert_called_once_with(
            user_subject="user-a",
            interaction_id="interaction-123456",
        )
        service.resume_interaction.assert_called_once_with(
            user_subject="user-a",
            interaction_id="interaction-123456",
            idempotency_key="resume-1",
        )


if __name__ == "__main__":
    unittest.main()
