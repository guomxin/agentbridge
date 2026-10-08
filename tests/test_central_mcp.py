from tests.authorization_fixtures import issue_authorized_token, grant_permissions
import asyncio
import json
import threading
import warnings
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, MagicMock, patch

from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
    category=StarletteDeprecationWarning,
)

from starlette.testclient import TestClient

from bscli.core.central_service import CentralCapabilityService
from bscli.core.mcp_identities import McpIdentityTokenStore
from bscli.auth.server import AuthServerConfig
from bscli.mcp.central import (
    CentralSessionKeepalive,
    _run_host_control,
    agent_facing_tools_for_scopes,
    create_central_mcp_server,
    serve_central_mcp,
    validate_central_mcp_server_config,
)
from bscli.mcp.presentation import (
    MCP_APP_MIME_TYPE,
    MCP_APP_RESOURCE_URI,
    MCP_PROFILE_RESOURCE_URI,
    PRIVATE_INTERACTION_META_KEY,
)


class CentralMcpTests(unittest.TestCase):
    def test_workflow_detail_optional_revision_matches_capability_contract(self):
        from bscli.adapters.seeyon_central import build_central_capability_registry
        from bscli.core.capability_runtime import _validate_json_object
        schema = build_central_capability_registry().get("oa.workflow.detail.get").input_schema
        with self._server() as (service, _store, token, client):
            def checked_invoke(**kwargs):
                _validate_json_object(kwargs["arguments"], schema)
                return {"protocolVersion": "0.1", "requestId": "detail", "operationId": "detail",
                        "status": "succeeded", "result": {}, "error": None,
                        "evidenceRefs": [], "nextAction": None, "reused": False}
            service.invoke.side_effect = checked_invoke
            for index, extra in enumerate(({}, {"expected_text_revision": None},
                                            {"expected_text_revision": "a" * 64, "text_offset": 6000})):
                with self.subTest(extra=extra):
                    response = self._request(client, "tools/call", request_id=4000 + index,
                        token=token, params={"name": "oa_workflow_detail_get",
                        "arguments": {"collection": "pending", "affair_id": "synthetic-affair", **extra}})
                    self.assertFalse(response.json()["result"]["isError"], response.json())
                    actual = service.invoke.call_args.kwargs["arguments"]
                    if extra.get("expected_text_revision") is None:
                        self.assertNotIn("expected_text_revision", actual)
                    else:
                        self.assertEqual(actual["expected_text_revision"], "a" * 64)
                        self.assertEqual(actual["text_offset"], 6000)

    def test_missing_user_grant_never_falls_back_to_historical_token_scopes(self):
        from contextlib import closing
        import sqlite3
        with self._server() as (service, store, token, client):
            with closing(sqlite3.connect(store.db_path)) as db, db:
                db.execute("DELETE FROM user_business_grants WHERE user_subject = ?", ("user-a",))
                db.execute("UPDATE mcp_identity_tokens SET scopes_json = ?", ('["oa:read", "oa:write:draft"]',))
            catalog = self._request(client, "tools/call", request_id=1, token=token,
                params={"name": "agentbridge_server_profile", "arguments": {}})
            names = catalog.json()["result"]["structuredContent"]["agentToolAccess"]["allowedToolNames"]
            self.assertNotIn("oa_workflow_pending_list", names)
            denied = self._request(client, "tools/call", request_id=2, token=token,
                params={"name": "oa_workflow_pending_list", "arguments": {}})
            self.assertTrue(denied.json()["result"]["isError"])
            service.invoke.assert_not_called()

    def test_slow_host_control_logs_non_sensitive_latency_context(self):
        with patch("bscli.mcp.central._LOGGER") as logger:
            result = asyncio.run(
                _run_host_control(
                    "agentbridge_host_timeline_append",
                    lambda **_kwargs: "ok",
                    user_subject="user-a",
                    warn_after_seconds=0,
                )
            )

        self.assertEqual(result, "ok")
        logger.warning.assert_called_once_with(
            "AgentBridge host control slow: tool=%s elapsed_ms=%d "
            "user_subject=%s",
            "agentbridge_host_timeline_append",
            ANY,
            "user-a",
        )

    def test_controlled_keepalive_worker_runs_and_stops(self):
        service = MagicMock()
        called = threading.Event()
        service.run_session_keepalive_cycle.side_effect = lambda **_kwargs: (
            called.set()
            or {
                "activeSessions": 1,
                "eligibleSessions": 1,
                "keptAlive": 1,
                "expired": 0,
                "deferred": 0,
                "outsideLease": 0,
            }
        )
        keepalive = CentralSessionKeepalive(
            service,
            interval_seconds=0.01,
            activity_lease_seconds=1,
            initial_delay_seconds=0,
        )

        keepalive.start()
        self.assertTrue(called.wait(timeout=1))
        keepalive.stop()

        service.run_session_keepalive_cycle.assert_called_with(
            activity_lease_seconds=1,
        )

    def test_non_loopback_server_requires_https_and_tls(self):
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            validate_central_mcp_server_config(
                host="0.0.0.0",
                port=8790,
                public_base_url="http://mcp.example.test",
                tls_cert=None,
                tls_key=None,
            )

    def test_explicit_private_ip_http_is_allowed_for_restricted_poc(self):
        config = validate_central_mcp_server_config(
            host="10.20.30.40",
            port=8790,
            public_base_url="http://10.20.30.40:8790",
            tls_cert=None,
            tls_key=None,
            allow_insecure_private_http=True,
        )

        self.assertEqual(config.mcp_url, "http://10.20.30.40:8790/mcp")
        self.assertTrue(config.insecure_private_http)

    def test_private_ip_http_mcp_accepts_its_configured_host(self):
        with TemporaryDirectory() as tmp:
            service = MagicMock()
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db")
            issued = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                ttl_seconds=3600,
            )
            config = validate_central_mcp_server_config(
                host="10.20.30.40",
                port=8790,
                public_base_url="http://10.20.30.40:8790",
                tls_cert=None,
                tls_key=None,
                allow_insecure_private_http=True,
            )
            server = create_central_mcp_server(
                service=service,
                identity_store=store,
                config=config,
                auth_card_base_url="http://10.20.30.40:8780",
            )
            with TestClient(
                server.streamable_http_app(),
                base_url="http://10.20.30.40:8790",
            ) as client:
                response = self._request(
                    client,
                    "tools/list",
                    request_id=1,
                    token=issued["token"],
                )

        self.assertEqual(response.status_code, 200)
        self.assertIn("tools", response.json()["result"])

    def test_unauthenticated_request_is_rejected(self):
        with self._server() as (_service, _store, _token, client):
            response = self._request(client, "tools/list", request_id=1, authenticated=False)

        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers.get("www-authenticate", ""))

    def test_tool_catalog_separates_reads_and_governed_writes_without_user_subject(self):
        with self._server() as (_service, _store, token, client):
            response = self._request(client, "tools/list", request_id=1, token=token)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        tools = payload["result"]["tools"]
        names = [tool["name"] for tool in tools]
        for plan_tool_name in (
            "agentbridge_task_plan_catalog",
            "agentbridge_task_plan_prepare",
            "agentbridge_task_plan_get",
            "agentbridge_task_plan_cancel",
        ):
            self.assertIn(plan_tool_name, names)
        self.assertIn("oa_workflow_pending_list", names)
        self.assertIn("oa_certificate_search", names)
        self.assertIn("oa_certificate_prepare_download", names)
        self.assertIn("oa_certificate_prepare_downloads", names)
        certificate_tool = next(
            tool for tool in tools if tool["name"] == "oa_certificate_search"
        )
        certificate_properties = certificate_tool["inputSchema"]["properties"]
        self.assertIn("names", certificate_properties)
        self.assertEqual(
            certificate_properties["names"]["anyOf"][0]["maxItems"],
            20,
        )
        self.assertIn(
            "do not launch parallel searches",
            certificate_tool["description"],
        )
        self.assertIn("oa_workflow_sent_list", names)
        self.assertIn("oa_workflow_detail_get", names)
        self.assertIn("oa_session_login", names)
        self.assertIn("taihua_work_log_my_list", names)
        self.assertIn("taihua_work_log_team_list", names)
        self.assertIn("taihua_project_search", names)
        self.assertIn("taihua_work_log_create_prepare", names)
        self.assertIn("taihua_work_log_create", names)
        self.assertIn("taihua_session_status", names)
        self.assertIn("taihua_session_login", names)
        self.assertIn("smartlight_system_overview", names)
        self.assertIn("smartlight_runtime_overview", names)
        self.assertIn("smartlight_rtu_status_list", names)
        self.assertIn("smartlight_lamp_status_list", names)
        self.assertIn("smartlight_lamp_alarm_list", names)
        self.assertIn("smartlight_lamp_alarm_analysis", names)
        self.assertIn("smartlight_rtu_survey_records", names)
        self.assertIn("smartlight_lamppost_list", names)
        self.assertIn("smartlight_alarm_list", names)
        self.assertIn("smartlight_alarm_remark_get", names)
        self.assertIn("smartlight_inspection_task_list", names)
        self.assertIn("smartlight_leakage_summary", names)
        self.assertIn("smartlight_asset_search", names)
        self.assertIn("smartlight_asset_detail", names)
        self.assertIn("smartlight_alarm_analysis", names)
        self.assertIn("smartlight_inspection_task_detail", names)
        self.assertIn("smartlight_leakage_analysis", names)
        self.assertIn("smartlight_alarm_work_area_submit_prepare", names)
        self.assertIn("smartlight_alarm_work_area_submit", names)
        self.assertIn("smartlight_alarm_work_area_revoke_prepare", names)
        self.assertIn("smartlight_alarm_work_area_revoke", names)
        self.assertIn("smartlight_rtu_alarm_dispose_prepare", names)
        self.assertIn("smartlight_rtu_alarm_dispose", names)
        self.assertIn("smartlight_session_status", names)
        self.assertIn("smartlight_session_login", names)
        smartlight_alarm_tool = next(
            tool for tool in tools if tool["name"] == "smartlight_alarm_list"
        )
        self.assertIn("sort_by=occurred_at", smartlight_alarm_tool["description"])
        self.assertIn("普通“最近”", smartlight_alarm_tool["description"])
        self.assertIn("latestGroup", smartlight_alarm_tool["description"])
        self.assertEqual(
            smartlight_alarm_tool["inputSchema"]["properties"]["sort_by"]["enum"],
            ["occurred_at", "last_activity"],
        )
        self.assertIn(
            "普通“最近/最新告警”",
            smartlight_alarm_tool["inputSchema"]["properties"]["sort_by"][
                "description"
            ],
        )
        smartlight_remark_tool = next(
            tool for tool in tools if tool["name"] == "smartlight_alarm_remark_get"
        )
        self.assertEqual(smartlight_remark_tool["annotations"]["readOnlyHint"], True)
        self.assertEqual(smartlight_remark_tool["inputSchema"]["required"], ["alarm_id"])
        smartlight_inspection_tool = next(
            tool
            for tool in tools
            if tool["name"] == "smartlight_inspection_task_list"
        )
        self.assertIn("状态码 1", smartlight_inspection_tool["description"])
        self.assertIn("不得自行组合", smartlight_inspection_tool["description"])
        smartlight_leakage_tool = next(
            tool for tool in tools if tool["name"] == "smartlight_leakage_summary"
        )
        self.assertIn(
            "last_days",
            smartlight_leakage_tool["inputSchema"]["properties"],
        )
        self.assertEqual(
            smartlight_leakage_tool["title"],
            "兼容旧入口：查询单灯告警",
        )
        self.assertIn("自然语言“漏电”不得选择本工具", smartlight_leakage_tool["description"])
        smartlight_survey_tool = next(
            tool for tool in tools if tool["name"] == "smartlight_rtu_survey_records"
        )
        self.assertIn("最长 7 天", smartlight_survey_tool["description"])
        smartlight_asset_tool = next(
            tool for tool in tools if tool["name"] == "smartlight_asset_search"
        )
        self.assertEqual(
            smartlight_asset_tool["inputSchema"]["properties"]["asset_type"]["enum"],
            ["cabinet", "rtu", "lamppost"],
        )
        smartlight_alarm_analysis_tool = next(
            tool for tool in tools if tool["name"] == "smartlight_alarm_analysis"
        )
        self.assertIn("最多分析 500 条", smartlight_alarm_analysis_tool["description"])
        smartlight_inspection_detail_tool = next(
            tool
            for tool in tools
            if tool["name"] == "smartlight_inspection_task_detail"
        )
        self.assertIn("不得根据数量差", smartlight_inspection_detail_tool["description"])
        self.assertIn("agentbridge_operation_list", names)
        self.assertIn("agentbridge_interaction_get", names)
        self.assertIn("agentbridge_interaction_resume", names)
        self.assertIn("agentbridge_host_task_ensure", names)
        self.assertIn("agentbridge_host_identity_profile", names)
        self.assertIn("agentbridge_host_task_observe", names)
        self.assertIn("agentbridge_host_task_finish", names)
        self.assertIn("agentbridge_host_task_recovery_list", names)
        self.assertIn("agentbridge_host_task_list", names)
        self.assertIn("agentbridge_host_task_continuation_resolve", names)
        self.assertIn("agentbridge_host_cross_endpoint_context", names)
        self.assertIn("agentbridge_host_interaction_present", names)
        self.assertIn("agentbridge_host_timeline_append", names)
        self.assertIn("agentbridge_host_notification_claim", names)
        self.assertIn("agentbridge_host_notification_ack", names)
        self.assertIn("agentbridge_host_artifact_delivery_report", names)
        self.assertIn("agentbridge_host_workspace_link_confirm", names)
        self.assertIn("agentbridge_host_workspace_session_bind", names)
        self.assertIn("agentbridge_host_workspace_session_resolve", names)
        self.assertIn("agentbridge_server_profile", names)
        self.assertIn("oa_business_trip_prepare", names)
        self.assertIn("oa_business_trip_save_draft", names)
        self.assertIn("oa_business_trip_submit_prepare", names)
        self.assertIn("oa_business_trip_submit", names)
        self.assertIn("oa_leave_prepare", names)
        self.assertIn("oa_leave_save_draft", names)
        self.assertIn("oa_leave_submit_prepare", names)
        self.assertIn("oa_leave_submit", names)
        self.assertIn("oa_workflow_revoke_prepare", names)
        self.assertIn("oa_workflow_revoke", names)
        self.assertIn("oa_missed_punch_prepare", names)
        self.assertIn("oa_missed_punch_save_draft", names)
        self.assertIn("oa_missed_punch_approval_prepare", names)
        self.assertIn("oa_missed_punch_approval_batch_prepare", names)
        self.assertIn("oa_workflow_pending_batch_prepare", names)
        self.assertIn("oa_missed_punch_approve", names)
        self.assertIn("oa_meeting_room_availability_list", names)
        self.assertIn("oa_meeting_room_my_applications_list", names)
        self.assertIn("oa_meeting_room_application_prepare", names)
        self.assertIn("oa_meeting_room_application_create", names)
        self.assertIn("oa_meeting_room_application_cancel_prepare", names)
        self.assertIn("oa_meeting_room_application_cancel", names)
        self.assertIn("oa_meeting_create_prepare", names)
        self.assertIn("oa_meeting_create", names)
        for tool_name in (
            "oa_efficiency_data_approval_prepare",
            "oa_efficiency_data_approve",
            "oa_travel_expense_approval_prepare",
            "oa_travel_expense_approve",
            "oa_business_trip_approval_prepare",
            "oa_business_trip_approve",
            "oa_labor_contract_renewal_approval_prepare",
            "oa_labor_contract_renewal_approve",
            "oa_intellectual_property_declaration_approval_prepare",
            "oa_intellectual_property_declaration_approve",
            "oa_overtime_approval_prepare",
            "oa_overtime_approve",
            "oa_leave_approval_prepare",
            "oa_leave_approve",
            "oa_resignation_approval_prepare",
            "oa_work_handover_approval_prepare",
            "oa_resignation_approve",
            "oa_work_handover_approve",
            "oa_flight_application_approval_prepare",
            "oa_flight_application_approve",
            "oa_attendance_confirmation_prepare",
            "oa_attendance_confirm",
            "oa_weekly_report_acknowledgement_prepare",
            "oa_weekly_report_acknowledge",
            "oa_standard_collaboration_approval_prepare",
            "oa_standard_collaboration_approve",
        ):
            self.assertIn(tool_name, names)
        pending = next(tool for tool in tools if tool["name"] == "oa_workflow_pending_list")
        sent = next(tool for tool in tools if tool["name"] == "oa_workflow_sent_list")
        done = next(tool for tool in tools if tool["name"] == "oa_workflow_done_list")
        plan_prepare = next(
            tool for tool in tools if tool["name"] == "agentbridge_task_plan_prepare"
        )
        team_logs = next(
            tool for tool in tools if tool["name"] == "taihua_work_log_team_list"
        )
        prepare = next(tool for tool in tools if tool["name"] == "oa_business_trip_prepare")
        save = next(tool for tool in tools if tool["name"] == "oa_business_trip_save_draft")
        submit = next(tool for tool in tools if tool["name"] == "oa_business_trip_submit")
        leave_save = next(tool for tool in tools if tool["name"] == "oa_leave_save_draft")
        leave_prepare = next(tool for tool in tools if tool["name"] == "oa_leave_prepare")
        leave_submit = next(tool for tool in tools if tool["name"] == "oa_leave_submit")
        revoke_prepare = next(tool for tool in tools if tool["name"] == "oa_workflow_revoke_prepare")
        revoke = next(tool for tool in tools if tool["name"] == "oa_workflow_revoke")
        self.assertTrue(pending["annotations"]["readOnlyHint"])
        self.assertTrue(sent["annotations"]["readOnlyHint"])
        self.assertIn("start_date", done["inputSchema"]["properties"])
        self.assertIn("end_date", done["inputSchema"]["properties"])
        self.assertIn("start_date", sent["inputSchema"]["properties"])
        self.assertIn("end_date", sent["inputSchema"]["properties"])
        self.assertFalse(plan_prepare["annotations"]["readOnlyHint"])
        self.assertFalse(plan_prepare["annotations"]["destructiveHint"])
        self.assertIn("inputSchema", plan_prepare["description"])
        self.assertIn("inventing arguments", plan_prepare["description"])
        self.assertEqual(
            plan_prepare["inputSchema"]["properties"]["steps"]["maxItems"],
            12,
        )
        self.assertIn(
            "agentbridge.task-plan.proposal.v2",
            plan_prepare["inputSchema"]["properties"]["schema_version"]["enum"],
        )
        self.assertIn("constraints", plan_prepare["inputSchema"]["properties"])
        step_items = plan_prepare["inputSchema"]["properties"]["steps"]["items"]
        self.assertEqual(step_items["discriminator"]["propertyName"], "kind")
        self.assertEqual(len(step_items["oneOf"]), 2)
        step_definitions = plan_prepare["inputSchema"]["$defs"]
        capability_step = step_definitions["TaskPlanCapabilityStepInput"]
        transform_step = step_definitions["TaskPlanTransformStepInput"]
        binding = step_definitions["TaskPlanBindingInput"]
        self.assertFalse(capability_step["additionalProperties"])
        self.assertFalse(transform_step["additionalProperties"])
        self.assertTrue(
            {"stepKey", "kind", "capabilityName"}.issubset(
                capability_step["required"]
            )
        )
        self.assertTrue(
            {"stepKey", "kind", "transformName"}.issubset(
                transform_step["required"]
            )
        )
        self.assertEqual(set(binding["required"]), {"step", "pointer"})
        self.assertIn("Sent page", sent["description"])
        self.assertIn("Done", sent["description"])
        self.assertFalse(prepare["annotations"]["readOnlyHint"])
        self.assertFalse(save["annotations"]["readOnlyHint"])
        self.assertFalse(save["annotations"]["destructiveHint"])
        self.assertTrue(submit["annotations"]["destructiveHint"])
        self.assertFalse(leave_save["annotations"]["destructiveHint"])
        self.assertTrue(leave_submit["annotations"]["destructiveHint"])
        self.assertFalse(revoke_prepare["annotations"]["destructiveHint"])
        self.assertTrue(revoke["annotations"]["destructiveHint"])
        approve = next(tool for tool in tools if tool["name"] == "oa_missed_punch_approve")
        batch_prepare = next(
            tool
            for tool in tools
            if tool["name"] == "oa_missed_punch_approval_batch_prepare"
        )
        prepare_meeting = next(
            tool for tool in tools if tool["name"] == "oa_meeting_create_prepare"
        )
        create_meeting = next(tool for tool in tools if tool["name"] == "oa_meeting_create")
        room_availability = next(
            tool for tool in tools if tool["name"] == "oa_meeting_room_availability_list"
        )
        room_applications = next(
            tool for tool in tools if tool["name"] == "oa_meeting_room_my_applications_list"
        )
        room_application_prepare = next(
            tool for tool in tools if tool["name"] == "oa_meeting_room_application_prepare"
        )
        room_application_create = next(
            tool for tool in tools if tool["name"] == "oa_meeting_room_application_create"
        )
        room_application_cancel_prepare = next(
            tool
            for tool in tools
            if tool["name"] == "oa_meeting_room_application_cancel_prepare"
        )
        room_application_cancel = next(
            tool for tool in tools if tool["name"] == "oa_meeting_room_application_cancel"
        )
        self.assertTrue(approve["annotations"]["destructiveHint"])
        self.assertFalse(batch_prepare["annotations"]["destructiveHint"])
        self.assertNotIn("affair_id", batch_prepare["inputSchema"]["properties"])
        generic_batch = next(tool for tool in tools if tool["name"] == "oa_workflow_pending_batch_prepare")
        self.assertFalse(generic_batch["annotations"]["destructiveHint"])
        properties = generic_batch["inputSchema"]["properties"]
        self.assertEqual(properties["max_items"]["maximum"], 100)
        for private in ("affair_id", "batch_id", "input_submission_id"):
            self.assertNotIn(private, properties)
        self.assertTrue(create_meeting["annotations"]["destructiveHint"])
        self.assertTrue(room_availability["annotations"]["readOnlyHint"])
        self.assertTrue(room_applications["annotations"]["readOnlyHint"])
        self.assertFalse(room_application_prepare["annotations"]["destructiveHint"])
        self.assertTrue(room_application_create["annotations"]["destructiveHint"])
        self.assertFalse(
            room_application_cancel_prepare["annotations"]["destructiveHint"]
        )
        self.assertTrue(room_application_cancel["annotations"]["destructiveHint"])
        self.assertIn("does not create or send", room_application_prepare["description"])
        self.assertIn("Default to this tool", room_application_prepare["description"])
        self.assertIn("Do not use this tool", prepare_meeting["description"])
        self.assertIn("only_available", room_availability["inputSchema"]["properties"])
        self.assertIn("audit_status", room_applications["inputSchema"]["properties"])
        self.assertNotIn("user_subject", json.dumps(tools))
        self.assertNotIn("expected_principal", json.dumps(tools))
        team_log_schema = team_logs["inputSchema"]["properties"]
        for field_name in (
            "keyword",
            "log_date",
            "start_date",
            "end_date",
            "member",
            "department",
            "watch_group",
            "dept_id",
            "member_id",
            "watch_group_id",
        ):
            self.assertIn(field_name, team_log_schema)
        view_mode_schema = team_log_schema["view_mode"]
        self.assertIn("submittedAt", json.dumps(view_mode_schema))
        self.assertIn("logDate", json.dumps(view_mode_schema))
        prepare_schema = prepare["inputSchema"]["properties"]
        for field_name in (
            "start_time",
            "end_time",
            "travel_mode",
            "origin",
            "destination",
            "reason",
            "has_direct_supervisor",
            "input_submission_id",
        ):
            self.assertIn(field_name, prepare_schema)
        self.assertNotIn("trip_days", prepare_schema)
        self.assertNotIn("trip_hours", prepare_schema)
        leave_prepare_schema = leave_prepare["inputSchema"]["properties"]
        for field_name in (
            "leave_type",
            "start_time",
            "end_time",
            "reason",
            "has_direct_supervisor",
            "input_submission_id",
        ):
            self.assertIn(field_name, leave_prepare_schema)
        revoke_prepare_schema = revoke_prepare["inputSchema"]["properties"]
        self.assertIn("affair_id", revoke_prepare_schema)
        self.assertIn("repeal_comment", revoke_prepare_schema)
        self.assertIn("input_submission_id", revoke_prepare_schema)
        meeting_prepare_schema = prepare_meeting["inputSchema"]["properties"]
        for field_name in (
            "subject",
            "room",
            "start_time",
            "end_time",
            "meeting_creation_intent",
            "input_submission_id",
        ):
            self.assertIn(field_name, meeting_prepare_schema)
        interaction_get = next(
            tool for tool in tools if tool["name"] == "agentbridge_interaction_get"
        )
        self.assertEqual(
            interaction_get["_meta"]["ui"]["resourceUri"],
            MCP_APP_RESOURCE_URI,
        )

    def test_certificate_batch_tool_omits_unused_null_name(self):
        with self._server() as (service, _store, token, client):
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "certificate-batch",
                "operationId": "certificate-operation",
                "status": "succeeded",
                "result": {"count": 0, "items": []},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            response = self._request(
                client,
                "tools/call",
                request_id=19,
                token=token,
                params={
                    "name": "oa_certificate_search",
                    "arguments": {
                        "names": ["系统甲V1.0", "系统乙V1.0"],
                        "document_type": "software_copyright_certificate",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        call = service.invoke.call_args.kwargs
        self.assertEqual(call["capability_name"], "oa.document.certificate.search")
        self.assertNotIn("name", call["arguments"])
        self.assertEqual(call["arguments"]["names"], ["系统甲V1.0", "系统乙V1.0"])

    def test_meeting_create_prepare_rejects_room_only_first_call(self):
        with self._server() as (service, _store, token, client):
            grant_permissions(_store.user_grants, "user-a", ["oa.meeting.create"])
            response = self._request(
                client,
                "tools/call",
                request_id=193,
                token=token,
                params={
                    "name": "oa_meeting_create_prepare",
                    "arguments": {
                        "room": "三号会议室",
                        "start_time": "2026-09-07 10:00",
                        "end_time": "2026-09-07 12:00",
                    },
                },
            )

        result = response.json()["result"]
        self.assertTrue(result["isError"])
        self.assertIn("oa_meeting_room_application_prepare", response.text)
        service.invoke.assert_not_called()

    def test_meeting_create_prepare_accepts_explicit_intent_without_forwarding_guard(self):
        with self._server() as (service, store, _token, client):
            token = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                label="meeting-client",
                scopes=["oa:read", "oa:write:meeting"],
                ttl_seconds=3600,
            )["token"]
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "meeting-prepare",
                "operationId": "meeting-operation",
                "status": "requires_user_action",
                "result": None,
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            response = self._request(
                client,
                "tools/call",
                request_id=194,
                token=token,
                params={
                    "name": "oa_meeting_create_prepare",
                    "arguments": {
                        "subject": "路由测试",
                        "room": "三号会议室",
                        "start_time": "2026-09-07 10:00",
                        "end_time": "2026-09-07 12:00",
                        "meeting_creation_intent": True,
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"], response.json())
        call = service.invoke.call_args.kwargs
        self.assertEqual(call["capability_name"], "oa.meeting.create.prepare")
        self.assertNotIn("meeting_creation_intent", call["arguments"])

    def test_meeting_create_prepare_continuation_does_not_repeat_intent_guard(self):
        with self._server() as (service, store, _token, client):
            token = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                label="meeting-client",
                scopes=["oa:read", "oa:write:meeting"],
                ttl_seconds=3600,
            )["token"]
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "meeting-continue",
                "operationId": "meeting-operation",
                "status": "requires_user_action",
                "result": None,
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            response = self._request(
                client,
                "tools/call",
                request_id=195,
                token=token,
                params={
                    "name": "oa_meeting_create_prepare",
                    "arguments": {"input_submission_id": "s" * 32},
                },
            )

        self.assertFalse(response.json()["result"]["isError"], response.json())
        self.assertEqual(
            service.invoke.call_args.kwargs["arguments"],
            {"input_submission_id": "s" * 32},
        )

    def test_certificate_tool_forwards_structured_documents(self):
        with self._server() as (service, _store, token, client):
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "certificate-structured",
                "operationId": "certificate-operation",
                "status": "succeeded",
                "result": {"count": 0, "items": []},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            documents = [
                {
                    "name": "泰华视图云大数据平台软件",
                    "version": "V2.0",
                    "aliases": ["视图云大数据平台"],
                }
            ]
            response = self._request(
                client,
                "tools/call",
                request_id=190,
                token=token,
                params={
                    "name": "oa_certificate_search",
                    "arguments": {
                        "documents": documents,
                        "document_type": "software_copyright_certificate",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        call = service.invoke.call_args.kwargs
        self.assertEqual(call["arguments"]["documents"], documents)
        self.assertNotIn("name", call["arguments"])
        self.assertNotIn("names", call["arguments"])
    def test_certificate_prepare_download_is_bound_to_authenticated_identity(self):
        with self._server() as (service, _store, token, client):
            service.prepare_document_download.return_value = {
                "protocolVersion": "0.1",
                "schemaVersion": "agentbridge.document_delivery.v1",
                "status": "succeeded",
                "file": {
                    "downloadId": "a" * 43,
                    "filename": "certificate.pdf",
                    "contentType": "application/pdf",
                    "size": 128,
                    "mediaUrl": f"https://10.10.50.213:8780/download/{'a' * 43}/file",
                    "expiresAt": "2026-07-28T08:00:00+00:00",
                },
            }
            response = self._request(
                client,
                "tools/call",
                request_id=191,
                token=token,
                params={
                    "name": "oa_certificate_prepare_download",
                    "arguments": {"download_id": "a" * 43},
                },
            )

        self.assertEqual(response.status_code, 200)
        call = service.prepare_document_download.call_args.kwargs
        self.assertEqual(call["download_id"], "a" * 43)
        self.assertNotIn("user_subject", response.text)
        self.assertEqual(
            response.json()["result"]["structuredContent"]["status"],
            "succeeded",
        )

    def test_certificate_batch_prepare_is_bound_to_authenticated_identity(self):
        with self._server() as (service, _store, token, client):
            service.prepare_document_downloads.return_value = {
                "protocolVersion": "0.1",
                "schemaVersion": "agentbridge.document_delivery_batch.v1",
                "status": "succeeded",
                "requestedCount": 2,
                "preparedCount": 2,
                "failedCount": 0,
                "files": [],
                "errors": [],
            }
            download_ids = ["a" * 43, "b" * 43]
            response = self._request(
                client,
                "tools/call",
                request_id=192,
                token=token,
                params={
                    "name": "oa_certificate_prepare_downloads",
                    "arguments": {"download_ids": download_ids},
                },
            )

        self.assertEqual(response.status_code, 200)
        call = service.prepare_document_downloads.call_args.kwargs
        self.assertEqual(call["download_ids"], download_ids)
        self.assertNotIn("user_subject", response.text)
        self.assertEqual(
            response.json()["result"]["structuredContent"]["status"],
            "succeeded",
        )

    def test_profile_resource_prompt_and_tool_are_discoverable(self):
        with self._server() as (_service, _store, token, client):
            resources = self._request(
                client,
                "resources/list",
                request_id=20,
                token=token,
            ).json()["result"]["resources"]
            prompts = self._request(
                client,
                "prompts/list",
                request_id=21,
                token=token,
            ).json()["result"]["prompts"]
            profile = self._request(
                client,
                "tools/call",
                request_id=22,
                token=token,
                params={"name": "agentbridge_server_profile", "arguments": {}},
            ).json()["result"]["structuredContent"]
            app_resource = self._request(
                client,
                "resources/read",
                request_id=23,
                token=token,
                params={"uri": MCP_APP_RESOURCE_URI},
            ).json()["result"]["contents"][0]
            operator_prompt = self._request(
                client,
                "prompts/get",
                request_id=24,
                token=token,
                params={"name": "agentbridge_oa_operator", "arguments": {}},
            ).json()["result"]

        resources_by_uri = {item["uri"]: item for item in resources}
        self.assertIn(MCP_PROFILE_RESOURCE_URI, resources_by_uri)
        self.assertEqual(
            resources_by_uri[MCP_APP_RESOURCE_URI]["mimeType"],
            MCP_APP_MIME_TYPE,
        )
        self.assertEqual(profile["mcp"]["endpoint"], "http://testserver/mcp")
        self.assertIn("agentbridge_oa_operator", [item["name"] for item in prompts])
        self.assertEqual(app_resource["mimeType"], MCP_APP_MIME_TYPE)
        self.assertIn("AGENTBRIDGE TRUSTED INTERACTION", app_resource["text"])
        self.assertIn(
            "prepare -> authorize -> commit -> verify",
            operator_prompt["messages"][0]["content"]["text"],
        )
        self.assertIn(
            "do not call agentbridge_interaction_get again in the same turn",
            operator_prompt["messages"][0]["content"]["text"],
        )

    def test_interaction_card_url_is_private_mcp_result_metadata(self):
        card_url = "https://cards.example.test/input/opaque-resource"
        with self._server() as (service, _store, token, client):
            service.get_interaction.return_value = {
                "protocolVersion": "0.1",
                "interaction": _trusted_interaction(card_url),
            }
            payload = self._request(
                client,
                "tools/call",
                request_id=25,
                token=token,
                params={
                    "name": "agentbridge_interaction_get",
                    "arguments": {"interaction_id": "interaction-1234567890"},
                },
            ).json()["result"]

        model_visible = json.dumps(
            {
                "content": payload["content"],
                "structuredContent": payload["structuredContent"],
            }
        )
        self.assertNotIn(card_url, model_visible)
        self.assertEqual(
            payload["_meta"][PRIVATE_INTERACTION_META_KEY]["presentation"]["url"],
            card_url,
        )

    def test_business_trip_prepare_requires_write_scope_and_uses_server_identity(self):
        with self._server() as (service, store, read_token, client):
            write_identity = issue_authorized_token(store,
                user_subject="write-identity",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:write:draft"],
                ttl_seconds=3600,
            )
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "mcp-write",
                "operationId": "prepare-1",
                "status": "requires_user_action",
                "result": None,
                "error": {"code": "WRITE_AUTHORIZATION_REQUIRED", "message": "confirm"},
                "evidenceRefs": [],
                "nextAction": {"cardUrl": "http://127.0.0.1:8780/authorize/card"},
                "reused": False,
            }
            arguments = {"idempotency_key": "mcp-business-trip-prepare"}
            denied = self._request(
                client,
                "tools/call",
                request_id=7,
                token=read_token,
                params={"name": "oa_business_trip_prepare", "arguments": arguments},
            )
            response = self._request(
                client,
                "tools/call",
                request_id=8,
                token=write_identity["token"],
                params={"name": "oa_business_trip_prepare", "arguments": arguments},
            )

        self.assertEqual(denied.status_code, 200)
        self.assertTrue(denied.json()["result"]["isError"])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"]["isError"])
        call = service.invoke.call_args.kwargs
        self.assertEqual(call["user_subject"], "write-identity")
        self.assertEqual(call["capability_name"], "oa.business_trip.prepare")
        self.assertEqual(call["idempotency_key"], "mcp-business-trip-prepare")
        self.assertEqual(call["arguments"], {})


    def test_generic_pending_batch_requires_workflow_permission(self):
        with self._server() as (service, store, read_token, client):
            grant_permissions(store.user_grants, "user-a", [])
            denied = self._request(client, "tools/call", request_id=73, token=read_token,
                params={"name": "oa_workflow_pending_batch_prepare", "arguments": {}})
            self.assertTrue(denied.json()["result"]["isError"])
            service.invoke.assert_not_called()

    def test_addressbook_tools_require_the_dedicated_read_scope(self):
        with self._server() as (service, store, read_token, client):
            addressbook_identity = issue_authorized_token(store,
                user_subject="addressbook-identity",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:read:addressbook"],
                ttl_seconds=3600,
            )
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "addressbook-scope",
                "operationId": "addressbook-op",
                "status": "succeeded",
                "result": {"count": 1, "items": [{"name": "Alice"}]},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "interaction": None,
                "reused": False,
            }
            parameters = {
                "name": "oa_addressbook_person_search",
                "arguments": {"query": "Alice"},
            }
            denied = self._request(
                client,
                "tools/call",
                request_id=70,
                token=read_token,
                params=parameters,
            )
            allowed = self._request(
                client,
                "tools/call",
                request_id=71,
                token=addressbook_identity["token"],
                params=parameters,
            )

        self.assertTrue(denied.json()["result"]["isError"])
        self.assertFalse(allowed.json()["result"]["isError"])
        self.assertEqual(
            service.invoke.call_args.kwargs["capability_name"],
            "oa.addressbook.person.search",
        )
        self.assertNotIn(
            "oa_addressbook_person_search",
            agent_facing_tools_for_scopes(["oa:read"]),
        )


    def test_configured_user_grant_overrides_old_token_scope_for_business_tools(self):
        from bscli.core.user_grants import UserGrants

        with self._server() as (service, store, read_token, client):
            UserGrants(store.db_path).save(
                "user-a", ["oa.addressbook.read"], expected_revision=1,
                actor="admin", reason="granular migration",
            )
            service.invoke.return_value = {
                "protocolVersion": "0.1", "operationId": "op-1", "requestId": "req-1",
                "status": "succeeded", "result": {"items": []}, "error": None,
                "evidenceRefs": [], "nextAction": None, "interaction": None,
                "reused": False,
            }
            addressbook = self._request(
                client, "tools/call", request_id=800, token=read_token,
                params={"name": "oa_addressbook_person_search", "arguments": {"query": "Alice"}},
            )
            workflow = self._request(
                client, "tools/call", request_id=801, token=read_token,
                params={"name": "oa_workflow_pending_list", "arguments": {}},
            )
        self.assertFalse(addressbook.json()["result"]["isError"])
        self.assertTrue(workflow.json()["result"]["isError"])

    def test_expired_token_renews_on_same_mcp_server_and_client(self):
        from datetime import datetime, timedelta, timezone

        with self._server() as (service, store, token, client):
            service.invoke.return_value = {
                "protocolVersion": "0.1", "operationId": "op-1", "requestId": "req-1",
                "status": "succeeded", "result": {"items": []}, "error": None,
                "evidenceRefs": [], "nextAction": None, "interaction": None,
                "reused": False,
            }
            now = datetime.now(timezone.utc)
            store.clock = lambda: now + timedelta(hours=2)
            params = {"name": "oa_workflow_pending_list", "arguments": {}}
            expired = self._request(client, "tools/call", request_id=810, token=token, params=params)
            self.assertEqual(expired.status_code, 401)
            old = store.list(user_subject="user-a")[0]
            store.renew(
                old["token_id"],
                new_expires_at=(now + timedelta(days=30)).isoformat(),
                expected_revision=0, request_id="renew-hot-1",
                actor="admin", reason="continue access",
            )
            resumed = self._request(client, "tools/call", request_id=811, token=token, params=params)
            self.assertFalse(resumed.json()["result"]["isError"])
        self.assertIn(
            "oa_addressbook_person_search",
            agent_facing_tools_for_scopes(["oa:read", "oa:read:addressbook"]),
        )

    def test_taihua_tools_enforce_read_and_worklog_scopes(self):
        with self._server() as (service, store, oa_read_token, client):
            taihua_reader = issue_authorized_token(store,
                user_subject="taihua-reader",
                expected_principal_ref="Alice",
                scopes=["taihua:read"],
                ttl_seconds=3600,
            )
            taihua_writer = issue_authorized_token(store,
                user_subject="taihua-writer",
                expected_principal_ref="Alice",
                scopes=["taihua:read", "taihua:write:worklog"],
                ttl_seconds=3600,
            )
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "taihua-scope",
                "operationId": "operation-1",
                "status": "succeeded",
                "result": {"count": 0, "items": []},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }

            oa_denied = self._request(
                client,
                "tools/call",
                request_id=21,
                token=oa_read_token,
                params={"name": "taihua_work_log_my_list", "arguments": {}},
            )
            read_allowed = self._request(
                client,
                "tools/call",
                request_id=22,
                token=taihua_reader["token"],
                params={"name": "taihua_work_log_my_list", "arguments": {}},
            )
            write_denied = self._request(
                client,
                "tools/call",
                request_id=23,
                token=taihua_reader["token"],
                params={"name": "taihua_work_log_create_prepare", "arguments": {}},
            )
            write_allowed = self._request(
                client,
                "tools/call",
                request_id=24,
                token=taihua_writer["token"],
                params={"name": "taihua_work_log_create_prepare", "arguments": {}},
            )

        self.assertTrue(oa_denied.json()["result"]["isError"])
        self.assertFalse(read_allowed.json()["result"]["isError"])
        self.assertTrue(write_denied.json()["result"]["isError"])
        self.assertFalse(write_allowed.json()["result"]["isError"])


    def test_smartlight_tools_separate_read_and_alarm_remark_write_scopes(self):
        with self._server() as (service, store, oa_read_token, client):
            smartlight_reader = issue_authorized_token(store,
                user_subject="smartlight-reader",
                expected_principal_ref="无为",
                scopes=["smartlight:read"],
                ttl_seconds=3600,
            )
            smartlight_writer = issue_authorized_token(store,
                user_subject="smartlight-writer",
                expected_principal_ref="无为",
                scopes=[
                    "smartlight:read",
                    "smartlight:write:alarm_remark",
                    "smartlight:write:alarm_work_area_submit",
                    "smartlight:write:alarm_work_area_revoke",
                    "smartlight:write:alarm_disposition",
                ],
                ttl_seconds=3600,
            )
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "smartlight-scope",
                "operationId": "operation-1",
                "status": "succeeded",
                "result": {"cabinetTotal": 0, "lampPostTotal": 0},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }

            denied = self._request(
                client,
                "tools/call",
                request_id=25,
                token=oa_read_token,
                params={"name": "smartlight_system_overview", "arguments": {}},
            )
            allowed = self._request(
                client,
                "tools/call",
                request_id=26,
                token=smartlight_reader["token"],
                params={"name": "smartlight_system_overview", "arguments": {}},
            )
            relative_range = self._request(
                client,
                "tools/call",
                request_id=27,
                token=smartlight_reader["token"],
                params={
                    "name": "smartlight_leakage_summary",
                    "arguments": {"last_days": 30},
                },
            )
            report = self._request(
                client,
                "tools/call",
                request_id=28,
                token=smartlight_reader["token"],
                params={
                    "name": "smartlight_report_export",
                    "arguments": {
                        "report_type": "asset_inventory",
                        "asset_type": "rtu",
                    },
                },
            )
            fourth_phase_read = self._request(
                client,
                "tools/call",
                request_id=34,
                token=smartlight_reader["token"],
                params={
                    "name": "smartlight_rtu_leakage_alarm_list",
                    "arguments": {"last_days": 30},
                },
            )
            write_denied = self._request(
                client,
                "tools/call",
                request_id=29,
                token=smartlight_reader["token"],
                params={
                    "name": "smartlight_alarm_remark_update_prepare",
                    "arguments": {"alarm_id": "alarm-1", "remark": "复核完成"},
                },
            )
            write_allowed = self._request(
                client,
                "tools/call",
                request_id=30,
                token=smartlight_writer["token"],
                params={
                    "name": "smartlight_alarm_remark_update_prepare",
                    "arguments": {"alarm_id": "alarm-1", "remark": "复核完成"},
                },
            )
            submit_allowed = self._request(
                client,
                "tools/call",
                request_id=31,
                token=smartlight_writer["token"],
                params={
                    "name": "smartlight_alarm_work_area_submit_prepare",
                    "arguments": {"alarm_id": "alarm-1"},
                },
            )
            revoke_allowed = self._request(
                client,
                "tools/call",
                request_id=32,
                token=smartlight_writer["token"],
                params={
                    "name": "smartlight_alarm_work_area_revoke_prepare",
                    "arguments": {"alarm_id": "alarm-1"},
                },
            )
            dispose_allowed = self._request(
                client,
                "tools/call",
                request_id=33,
                token=smartlight_writer["token"],
                params={
                    "name": "smartlight_rtu_alarm_dispose_prepare",
                    "arguments": {"alarm_id": "alarm-1"},
                },
            )

        self.assertTrue(denied.json()["result"]["isError"])
        self.assertFalse(allowed.json()["result"]["isError"])
        self.assertFalse(relative_range.json()["result"]["isError"])
        self.assertFalse(report.json()["result"]["isError"])
        self.assertFalse(fourth_phase_read.json()["result"]["isError"])
        self.assertTrue(write_denied.json()["result"]["isError"])
        self.assertFalse(write_allowed.json()["result"]["isError"])
        self.assertFalse(submit_allowed.json()["result"]["isError"])
        self.assertFalse(revoke_allowed.json()["result"]["isError"])
        self.assertFalse(dispose_allowed.json()["result"]["isError"])
        self.assertEqual(
            service.invoke.call_args.kwargs["capability_name"],
            "smartlight.alarm.dispose.prepare",
        )
        self.assertEqual(
            service.invoke.call_args.kwargs["arguments"],
            {"alarm_id": "alarm-1"},
        )


    def test_submit_approval_and_meeting_tools_enforce_separate_scopes(self):
        with self._server() as (service, store, read_token, client):
            approval_identity = issue_authorized_token(store,
                user_subject="approval-identity",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:write:approval"],
                ttl_seconds=3600,
            )
            meeting_identity = issue_authorized_token(store,
                user_subject="meeting-identity",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:write:meeting"],
                ttl_seconds=3600,
            )
            submit_identity = issue_authorized_token(store,
                user_subject="submit-identity",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:write:submit"],
                ttl_seconds=3600,
            )
            revoke_identity = issue_authorized_token(store,
                user_subject="revoke-identity",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:write:revoke"],
                ttl_seconds=3600,
            )
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "mcp-write",
                "operationId": "operation-1",
                "status": "succeeded",
                "result": {"submitted_count": 1},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            authorization_id = "a" * 32
            read_denied = self._request(
                client,
                "tools/call",
                request_id=31,
                token=read_token,
                params={
                    "name": "oa_missed_punch_approve",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            approval_allowed = self._request(
                client,
                "tools/call",
                request_id=32,
                token=approval_identity["token"],
                params={
                    "name": "oa_missed_punch_approve",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            approval_meeting_denied = self._request(
                client,
                "tools/call",
                request_id=33,
                token=approval_identity["token"],
                params={
                    "name": "oa_meeting_create",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            meeting_allowed = self._request(
                client,
                "tools/call",
                request_id=34,
                token=meeting_identity["token"],
                params={
                    "name": "oa_meeting_create",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            approval_submit_denied = self._request(
                client,
                "tools/call",
                request_id=35,
                token=approval_identity["token"],
                params={
                    "name": "oa_business_trip_submit",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            submit_allowed = self._request(
                client,
                "tools/call",
                request_id=36,
                token=submit_identity["token"],
                params={
                    "name": "oa_business_trip_submit",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            submit_revoke_denied = self._request(
                client,
                "tools/call",
                request_id=37,
                token=submit_identity["token"],
                params={
                    "name": "oa_workflow_revoke",
                    "arguments": {"authorization_id": authorization_id},
                },
            )
            revoke_allowed = self._request(
                client,
                "tools/call",
                request_id=38,
                token=revoke_identity["token"],
                params={
                    "name": "oa_workflow_revoke",
                    "arguments": {"authorization_id": authorization_id},
                },
            )

        self.assertTrue(read_denied.json()["result"]["isError"])
        self.assertFalse(approval_allowed.json()["result"]["isError"])
        self.assertTrue(approval_meeting_denied.json()["result"]["isError"])
        self.assertFalse(meeting_allowed.json()["result"]["isError"])
        self.assertTrue(approval_submit_denied.json()["result"]["isError"])
        self.assertFalse(submit_allowed.json()["result"]["isError"])
        self.assertTrue(submit_revoke_denied.json()["result"]["isError"])
        self.assertFalse(revoke_allowed.json()["result"]["isError"])
        self.assertEqual(
            [call.kwargs["capability_name"] for call in service.invoke.call_args_list],
            [
                "oa.missed_punch.approve",
                "oa.meeting.create",
                "oa.business_trip.submit",
                "oa.workflow.revoke",
            ],
        )


    def test_authenticated_tool_uses_server_bound_identity_and_shared_service(self):
        with self._server() as (service, _store, token, client):
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "mcp-request",
                "operationId": "operation-1",
                "status": "succeeded",
                "result": {"collection": "pending", "count": 0, "items": []},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            response = self._request(
                client,
                "tools/call",
                request_id=2,
                token=token,
                params={
                    "name": "oa_workflow_pending_list",
                    "arguments": {"limit": 5, "idempotency_key": "mcp-pending-1"},
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["result"]["isError"])
        self.assertEqual(payload["result"]["structuredContent"]["status"], "succeeded")
        call = service.invoke.call_args.kwargs
        self.assertEqual(call["user_subject"], "user-a")
        self.assertEqual(call["capability_name"], "oa.workflow.pending.list")
        self.assertEqual(call["arguments"], {"limit": 5})
        self.assertEqual(call["idempotency_key"], "mcp-pending-1")

    def test_private_task_metadata_is_validated_before_and_observed_after_call(self):
        with self._server() as (service, _store, token, client):
            service.observe_host_task.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "task": {"taskId": "task-1234567890-abcdef"},
            }
            service.invoke.return_value = {
                "protocolVersion": "0.1",
                "requestId": "mcp-request",
                "operationId": "operation-1",
                "status": "succeeded",
                "result": {"collection": "pending", "count": 0, "items": []},
                "error": None,
                "evidenceRefs": [],
                "nextAction": None,
                "reused": False,
            }
            response = self._request(
                client,
                "tools/call",
                request_id=21,
                token=token,
                params={
                    "name": "oa_workflow_pending_list",
                    "arguments": {"limit": 5},
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        },
                        "io.agentbridge/task": {
                            "taskId": "task-1234567890-abcdef",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual(service.observe_host_task.call_count, 2)
        self.assertEqual(
            service.observe_host_task.call_args_list[0].kwargs,
            {
                "user_subject": "user-a",
                "task_id": "task-1234567890-abcdef",
                "operation_ids": [],
                "interaction_ids": [],
            },
        )
        self.assertEqual(
            service.observe_host_task.call_args_list[1].kwargs,
            {
                "user_subject": "user-a",
                "task_id": "task-1234567890-abcdef",
                "operation_ids": ["operation-1"],
                "interaction_ids": [],
            },
        )

    def test_pre_operation_exception_closes_private_host_task_as_failed(self):
        with self._server() as (service, _store, token, client):
            service.observe_host_task.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "task": {"taskId": "task-1234567890-abcdef"},
            }
            service.fail_host_task.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "task": {
                    "taskId": "task-1234567890-abcdef",
                    "status": "failed",
                },
            }
            service.invoke.side_effect = ValueError("invalid capability input")

            response = self._request(
                client,
                "tools/call",
                request_id=22,
                token=token,
                params={
                    "name": "oa_workflow_pending_list",
                    "arguments": {"limit": 5},
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        },
                        "io.agentbridge/task": {
                            "taskId": "task-1234567890-abcdef",
                        }
                    },
                },
            )

        self.assertTrue(response.json()["result"]["isError"])
        service.fail_host_task.assert_called_once_with(
            user_subject="user-a",
            task_id="task-1234567890-abcdef",
            error_code="MCP_TOOL_EXECUTION_FAILED",
            message="invalid capability input",
            causation_ref=ANY,
        )

    def test_login_card_defers_principal_resolution_to_system_session(self):
        with self._server() as (service, _store, token, client):
            service.start_login.return_value = {
                "protocolVersion": "0.1",
                "status": "requires_user_action",
                "nextAction": {"cardUrl": "http://127.0.0.1:8780/auth/challenge"},
            }
            response = self._request(
                client,
                "tools/call",
                request_id=3,
                token=token,
                params={
                    "name": "oa_session_login",
                    "arguments": {"challenge_ttl_seconds": 600},
                },
            )

        self.assertEqual(response.status_code, 200)
        call = service.start_login.call_args.kwargs
        self.assertEqual(call["user_subject"], "user-a")
        self.assertIsNone(call["expected_principal_ref"])
        self.assertEqual(call["card_base_url"], "http://127.0.0.1:8780")
        self.assertEqual(call["ttl_seconds"], 600)

    def test_yuque_login_defaults_to_fifteen_minute_challenge(self):
        with self._server() as (service, store, _token, client):
            yuque_identity = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                scopes=["yuque:read"],
                ttl_seconds=3600,
            )
            service.start_login.return_value = {
                "protocolVersion": "0.1",
                "status": "requires_user_action",
                "nextAction": {"cardUrl": "http://127.0.0.1:8780/auth/challenge"},
            }
            response = self._request(
                client,
                "tools/call",
                request_id=31,
                token=yuque_identity["token"],
                params={
                    "name": "yuque_session_login",
                    "arguments": {},
                },
            )

        self.assertEqual(response.status_code, 200)
        call = service.start_login.call_args.kwargs
        self.assertEqual(call["user_subject"], "user-a")
        self.assertEqual(call["system_id"], "yuque")
        self.assertEqual(call["ttl_seconds"], 900)

    def test_interaction_resume_forwards_the_current_user_identity(self):
        with self._server() as (service, store, read_token, client):
            write_identity = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                scopes=["oa:read", "oa:write:draft"],
                ttl_seconds=3600,
            )
            service.interaction_required_scopes.return_value = frozenset(
                {"oa:write:draft"}
            )
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
                "status": "requires_user_action",
                "resumedFromInteractionId": "interaction-123456",
            }
            response = self._request(
                client,
                "tools/call",
                request_id=10,
                token=write_identity["token"],
                params={
                    "name": "agentbridge_interaction_resume",
                    "arguments": {
                        "interaction_id": "interaction-123456",
                        "idempotency_key": "resume-1",
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.resume_interaction.assert_called_once_with(
            user_subject="user-a",
            interaction_id="interaction-123456",
            idempotency_key="resume-1",
        )

    def test_revoked_token_is_rejected_without_calling_service(self):
        with self._server() as (service, store, token, client):
            record = store.verify(token)
            store.revoke(record["token_id"])
            response = self._request(
                client,
                "tools/call",
                request_id=4,
                token=token,
                params={"name": "oa_session_status", "arguments": {}},
            )

        self.assertEqual(response.status_code, 401)
        service.session_status.assert_not_called()

    def test_each_bearer_token_routes_to_its_own_server_bound_user(self):
        with self._server() as (service, store, _token, client):
            second = issue_authorized_token(store,
                user_subject="user-b",
                expected_principal_ref="Bob",
                ttl_seconds=3600,
            )
            service.session_status.return_value = {
                "protocolVersion": "0.1",
                "status": "not_found",
                "systemId": "oa",
                "userSubject": "user-b",
            }
            response = self._request(
                client,
                "tools/call",
                request_id=6,
                token=second["token"],
                params={"name": "oa_session_status", "arguments": {}},
            )

        self.assertEqual(response.status_code, 200)
        service.session_status.assert_called_once_with(
            user_subject="user-b",
            system_id="oa",
        )

    def test_host_task_tools_require_private_host_metadata(self):
        with self._server() as (service, _store, token, client):
            denied = self._request(
                client,
                "tools/call",
                request_id=61,
                token=token,
                params={
                    "name": "agentbridge_host_task_ensure",
                    "arguments": {
                        "agent_host": "openclaw",
                        "host_task_key": "session|run",
                        "endpoint_key": "telegram:*:1001",
                        "client_type": "telegram",
                        "external_subject": "1001",
                        "conversation_ref": "agent:main:telegram:direct:1001",
                        "title": "OA task",
                    },
                },
            )

        self.assertTrue(denied.json()["result"]["isError"])
        service.ensure_host_task.assert_not_called()

    def test_server_profile_negotiates_and_registers_declared_host(self):
        profile = json.loads(
            (
                Path(__file__).parents[1]
                / "schemas"
                / "agent-host"
                / "v1"
                / "test-vectors.json"
            ).read_text(encoding="utf-8")
        )["profiles"][0]["value"]
        with self._server() as (service, store, token, client):
            identity = store.verify(token)
            service.negotiate_host.return_value = {
                "schemaVersion": "agentbridge.host-negotiation.v1",
                "status": "succeeded",
                "acceptedLevel": "L3",
                "compatibilityStatus": "approved",
                "hostInstanceId": profile["hostInstanceId"],
                "implementation": profile["implementation"],
                "missingCapabilities": [],
                "mustReregisterOnVersionChange": True,
            }
            response = self._request(
                client,
                "tools/call",
                request_id=610,
                token=token,
                params={
                    "name": "agentbridge_server_profile",
                    "arguments": {},
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "reference-host",
                            "hostInstanceId": profile["hostInstanceId"],
                            "hostVersion": "0.1.0",
                        },
                        "io.agentbridge/host-profile": profile,
                    },
                },
            )

        result = response.json()["result"]["structuredContent"]
        self.assertEqual("L3", result["negotiation"]["acceptedLevel"])
        self.assertEqual(
            ["agentbridge_host_register", "agentbridge_host_runtime_snapshot", "agentbridge_host_identity_profile"],
            result["toolPlanes"]["hostControl"]["levels"]["L1"],
        )
        self.assertEqual(
            ["agentbridge_host_interaction_present"],
            result["toolPlanes"]["hostControl"]["levels"]["L2"],
        )
        service.negotiate_host.assert_called_once_with(
            user_subject="user-a",
            token_id=identity["token_id"],
            profile=profile,
        )

    def test_undeclared_host_profile_is_l1_only(self):
        with self._server() as (_service, _store, token, client):
            response = self._request(
                client,
                "tools/call",
                request_id=6101,
                token=token,
                params={"name": "agentbridge_server_profile", "arguments": {}},
            )

        result = response.json()["result"]["structuredContent"]
        self.assertEqual("L1", result["negotiation"]["acceptedLevel"])
        self.assertEqual("undeclared", result["negotiation"]["compatibilityStatus"])

    def test_host_runtime_lease_and_snapshot_controls_use_registered_identity(self):
        host_meta = {
            "io.agentbridge/host-context": {
                "version": "1",
                "agentHost": "openclaw",
                "hostInstanceId": "openclaw-gateway",
                "hostVersion": "0.4.65",
            }
        }
        snapshot = {
            "status": "healthy",
            "observedAt": "2026-08-29T10:00:00+00:00",
            "activeTaskCount": 1,
        }
        with self._server() as (service, _store, token, client):
            service.record_host_runtime_snapshot.return_value = {
                "status": "succeeded",
                "snapshotId": "snapshot-1234567890",
            }
            service.get_host_coordinator_lease.return_value = {
                "taskId": "task-1234567890-abcdef",
                "hostInstanceId": "openclaw-gateway",
                "version": 4,
            }
            service.get_host_task_snapshot.return_value = {
                "status": "succeeded",
                "task": {"taskId": "task-1234567890-abcdef"},
                "events": [],
                "artifacts": [],
            }
            runtime_response = self._request(
                client,
                "tools/call",
                request_id=6102,
                token=token,
                params={
                    "name": "agentbridge_host_runtime_snapshot",
                    "arguments": {"snapshot": snapshot},
                    "_meta": host_meta,
                },
            )
            lease_response = self._request(
                client,
                "tools/call",
                request_id=6103,
                token=token,
                params={
                    "name": "agentbridge_host_coordinator_lease_get",
                    "arguments": {"task_id": "task-1234567890-abcdef"},
                    "_meta": host_meta,
                },
            )
            task_response = self._request(
                client,
                "tools/call",
                request_id=6104,
                token=token,
                params={
                    "name": "agentbridge_host_task_snapshot",
                    "arguments": {
                        "agent_host": "openclaw",
                        "endpoint_key": "telegram:*:1001",
                        "task_id": "task-1234567890-abcdef",
                    },
                    "_meta": host_meta,
                },
            )

        self.assertFalse(runtime_response.json()["result"]["isError"])
        self.assertFalse(lease_response.json()["result"]["isError"])
        self.assertFalse(task_response.json()["result"]["isError"])
        service.record_host_runtime_snapshot.assert_called_once_with(
            user_subject="user-a",
            registration=service.require_host_registration.return_value,
            snapshot=snapshot,
        )
        service.get_host_coordinator_lease.assert_called_once_with(
            user_subject="user-a",
            task_id="task-1234567890-abcdef",
        )
        service.get_host_task_snapshot.assert_called_once_with(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            task_id="task-1234567890-abcdef",
            event_limit=100,
            artifact_limit=20,
        )

    def test_login_read_resume_requires_formal_host_and_current_coordinator_metadata(self):
        task_id = "task-login-read-1234567890"
        host_meta = {
            "io.agentbridge/host-context": {
                "version": "1", "agentHost": "openclaw",
                "hostInstanceId": "openclaw-gateway", "hostVersion": "0.4.79",
            },
            "io.agentbridge/task": {"taskId": task_id, "coordinatorLeaseVersion": "7"},
        }
        with self._server() as (service, _store, token, client):
            service.require_host_registration.return_value = {
                "hostInstanceId": "openclaw-gateway", "agentHost": "openclaw",
                "hostVersion": "0.4.79", "acceptedLevel": "L3",
            }
            service.interaction_required_scopes.return_value = frozenset({"oa:read"})
            service.assert_host_coordinator_lease = MagicMock()
            service.tasks.task_id_for_interaction.return_value = task_id
            service.resume_interaction.return_value = {
                "status": "succeeded", "taskStatus": "succeeded", "interaction": None,
                "nextAction": {"type": "original_request_completed"},
            }
            params = {"name": "agentbridge_interaction_resume", "arguments": {
                "interaction_id": "login-interaction-1234567890",
            }}
            rejected = self._request(client, "tools/call", request_id=6201, token=token,
                params={**params, "_meta": {"io.agentbridge/task": host_meta["io.agentbridge/task"]}})
            self.assertTrue(rejected.json()["result"]["isError"])
            service.resume_interaction.assert_not_called()
            accepted = self._request(client, "tools/call", request_id=6202, token=token,
                params={**params, "_meta": host_meta})
            self.assertFalse(accepted.json()["result"]["isError"], accepted.json())
            self.assertEqual(accepted.json()["result"]["structuredContent"]["nextAction"]["type"],
                "original_request_completed")
            service.assert_host_coordinator_lease.assert_called_once_with(
                user_subject="user-a", task_id=task_id,
                registration=service.require_host_registration.return_value, expected_version=7,
            )
            service.resume_interaction.assert_called_once()

    def test_non_coordinator_cannot_resume_task_interaction(self):
        host_meta = {
            "io.agentbridge/host-context": {
                "version": "1",
                "agentHost": "reference-host",
                "hostInstanceId": "reference-host-test",
                "hostVersion": "0.1.0",
            },
            "io.agentbridge/task": {
                "taskId": "task-1234567890-abcdef",
                "coordinatorLeaseVersion": "1",
            },
        }
        with self._server() as (service, _store, token, client):
            service.require_host_registration.return_value = {
                "hostInstanceId": "reference-host-test",
                "agentHost": "reference-host",
                "hostVersion": "0.1.0",
                "acceptedLevel": "L3",
            }
            service.interaction_required_scopes.return_value = []
            service.tasks.task_id_for_interaction.return_value = (
                "task-1234567890-abcdef"
            )
            service.assert_host_coordinator_lease = MagicMock(
                side_effect=PermissionError(
                    "Coordinator lease is held by another host"
                )
            )
            response = self._request(
                client,
                "tools/call",
                request_id=6105,
                token=token,
                params={
                    "name": "agentbridge_interaction_resume",
                    "arguments": {
                        "interaction_id": "interaction-1234567890-abcdef",
                        "idempotency_key": "reference-host:test",
                    },
                    "_meta": host_meta,
                },
            )

        self.assertTrue(response.json()["result"]["isError"])
        service.resume_interaction.assert_not_called()

    def test_terminal_task_does_not_receive_a_new_coordinator_lease(self):
        host_meta = {
            "io.agentbridge/host-context": {
                "version": "1",
                "agentHost": "openclaw",
                "hostInstanceId": "openclaw-gateway",
                "hostVersion": "0.4.72",
            }
        }
        with self._server() as (service, _store, token, client):
            service.require_host_registration.return_value = {
                "hostInstanceId": "openclaw-gateway",
                "agentHost": "openclaw",
                "hostVersion": "0.4.72",
                "acceptedLevel": "L3",
            }
            service.tasks.get_task.return_value = {
                "task_id": "task-terminal-1234567890",
                "status": "succeeded",
            }
            response = self._request(
                client,
                "tools/call",
                request_id=6106,
                token=token,
                params={
                    "name": "agentbridge_host_coordinator_lease_acquire",
                    "arguments": {
                        "task_id": "task-terminal-1234567890",
                        "lease_seconds": 60,
                    },
                    "_meta": host_meta,
                },
            )

        result = response.json()["result"]["structuredContent"]
        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual("ignored", result["status"])
        self.assertEqual("task_terminal", result["reason"])
        self.assertEqual("succeeded", result["taskStatus"])
        self.assertIsNone(result["coordinatorLease"])
        service.acquire_host_coordinator_lease.assert_not_called()

    def test_host_identity_profile_returns_scope_filtered_agent_tools(self):
        with self._server() as (service, store, _token, client):
            issued = issue_authorized_token(store,
                user_subject="user-b",
                expected_principal_ref="Bob",
                label="limited-client",
                scopes=["oa:read", "oa:write:submit"],
                ttl_seconds=3600,
            )
            service.require_host_registration.return_value = {
                "hostInstanceId": "openclaw-gateway",
                "agentHost": "openclaw",
                "hostVersion": "0.4.65",
                "acceptedLevel": "L3",
            }
            response = self._request(
                client,
                "tools/call",
                request_id=611,
                token=issued["token"],
                params={
                    "name": "agentbridge_host_identity_profile",
                    "arguments": {"agent_host": "openclaw"},
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

            expected_tools = store.user_grants.available_tools("user-b")

        result = response.json()["result"]["structuredContent"]
        self.assertFalse(response.json()["result"]["isError"])
        self.assertEqual(result["identity"]["userSubject"], "user-b")
        self.assertEqual(
            result["agentToolAccess"]["allowedToolNames"],
            expected_tools,
        )
        self.assertIn(
            "oa_business_trip_submit_prepare",
            result["agentToolAccess"]["allowedToolNames"],
        )
        for plan_tool_name in (
            "agentbridge_task_plan_catalog",
            "agentbridge_task_plan_prepare",
            "agentbridge_task_plan_get",
            "agentbridge_task_plan_cancel",
        ):
            self.assertIn(
                plan_tool_name,
                result["agentToolAccess"]["allowedToolNames"],
            )
        self.assertNotIn(
            "oa_business_trip_prepare",
            result["agentToolAccess"]["allowedToolNames"],
        )
        self.assertNotIn("yuque_document_search", str(result))
        self.assertNotIn("abmcp_", str(result))

    def test_registered_host_can_start_a_scope_checked_task_plan(self):
        host_meta = {
            "io.agentbridge/host-context": {
                "version": "1",
                "agentHost": "reference-host",
                "hostInstanceId": "reference-host-test",
                "hostVersion": "0.1.0",
            },
            "io.agentbridge/task": {
                "taskId": "task-1234567890-abcdef",
                "coordinatorLeaseVersion": "3",
            },
        }
        steps = [
            {
                "stepKey": "done",
                "kind": "capability",
                "capabilityName": "oa.workflow.done.list",
                "arguments": {
                    "start_date": "2026-08-30",
                    "end_date": "2026-08-30",
                },
            }
        ]
        with self._server() as (service, store, _token, client):
            issued = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                scopes=["oa:read"],
                ttl_seconds=3600,
            )
            service.require_host_registration.return_value = {
                "hostInstanceId": "reference-host-test",
                "agentHost": "reference-host",
                "hostVersion": "0.1.0",
                "acceptedLevel": "L3",
            }
            service.task_plan_required_scopes.return_value = frozenset(
                {"oa:read"}
            )
            service.prepare_task_plan.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "plan": {
                    "schemaVersion": "agentbridge.task-plan.v1",
                    "planId": "plan-1234567890-abcdef",
                    "taskId": "task-1234567890-abcdef",
                    "state": "succeeded",
                    "steps": [],
                },
            }
            response = self._request(
                client,
                "tools/call",
                request_id=612,
                token=issued["token"],
                params={
                    "name": "agentbridge_task_plan_prepare",
                    "arguments": {
                        "goal": "汇总今日已办",
                        "steps": steps,
                        "idempotency_key": "reference-plan-1",
                    },
                    "_meta": host_meta,
                },
            )

        result = response.json()["result"]
        self.assertFalse(result["isError"])
        service.task_plan_required_scopes.assert_called_once()
        service.validate_host_call_context.assert_called_once_with(
            user_subject="user-a",
            registration=service.require_host_registration.return_value,
            task_id="task-1234567890-abcdef",
            endpoint_id=None,
            expected_lease_version=3,
            require_coordinator_lease=True,
        )
        service.prepare_task_plan.assert_called_once_with(
            user_subject="user-a",
            task_id="task-1234567890-abcdef",
            proposal={
                "schemaVersion": "agentbridge.task-plan.proposal.v1",
                "goal": "汇总今日已办",
                "steps": steps,
            },
            granted_scopes=["agentbridge:connect", "oa:read"],
            authority_identity=ANY,
            idempotency_key="reference-plan-1",
            coordinator_lease_version=3,
        )
        self.assertEqual(
            service.prepare_task_plan.call_args.kwargs["authority_identity"]["user_subject"],
            "user-a",
        )

    def test_host_task_ensure_uses_token_identity_and_private_metadata(self):
        with self._server() as (service, store, token, client):
            identity = store.verify(token)
            service.require_host_registration.return_value = {
                "hostInstanceId": "openclaw-gateway",
                "agentHost": "openclaw",
                "hostVersion": "0.4.65",
                "acceptedLevel": "L3",
            }
            service.ensure_host_task.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "task": {"taskId": "task-1234567890-abcdef"},
                "endpoint": {"endpointId": "endpoint-123"},
            }
            response = self._request(
                client,
                "tools/call",
                request_id=62,
                token=token,
                params={
                    "name": "agentbridge_host_task_ensure",
                    "arguments": {
                        "agent_host": "openclaw",
                        "host_task_key": "session|run",
                        "endpoint_key": "telegram:*:1001",
                        "client_type": "telegram",
                        "external_subject": "1001",
                        "conversation_ref": "agent:main:telegram:direct:1001",
                        "title": "OA task",
                        "task_scope": "user_turn",
                        "route": {"channel": "telegram", "to": "1001"},
                    },
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.ensure_host_task.assert_called_once_with(
            user_subject="user-a",
            token_id=identity["token_id"],
            agent_host="openclaw",
            host_task_key="session|run",
            endpoint_key="telegram:*:1001",
            client_type="telegram",
            external_subject="1001",
            conversation_ref="agent:main:telegram:direct:1001",
            title="OA task",
            account_id=None,
            label=None,
            route={"channel": "telegram", "to": "1001"},
            capabilities=None,
            task_scope="user_turn",
            tool_name=None,
            planning_task_key=None,
            host_instance_id="openclaw-gateway",
            host_version="0.4.65",
        )

    def test_host_task_finish_uses_token_identity_and_private_metadata(self):
        with self._server() as (service, _store, token, client):
            service.finish_host_task.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "task": {
                    "taskId": "task-1234567890-abcdef",
                    "status": "failed",
                },
            }
            response = self._request(
                client,
                "tools/call",
                request_id=621,
                token=token,
                params={
                    "name": "agentbridge_host_task_finish",
                    "arguments": {
                        "agent_host": "openclaw",
                        "task_id": "task-1234567890-abcdef",
                        "outcome": "failed",
                        "error_code": "MCP_UNREACHABLE",
                        "message": "AgentBridge MCP is unreachable",
                        "causation_ref": "tool-call-1",
                    },
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.finish_host_task.assert_called_once_with(
            user_subject="user-a",
            task_id="task-1234567890-abcdef",
            outcome="failed",
            reason=None,
            error_code="MCP_UNREACHABLE",
            message="AgentBridge MCP is unreachable",
            causation_ref="tool-call-1",
        )

    def test_workspace_session_bind_persists_turn_reference(self):
        with self._server() as (service, _store, token, client):
            service.redeem_workspace_gateway_grant.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "binding": {
                    "endpointKey": "workspace:account-123",
                    "sessionKey": "agent:main:agentbridge-workspace:direct:account-123",
                    "turnRef": "request-123",
                },
            }
            response = self._request(
                client,
                "tools/call",
                request_id=621,
                token=token,
                params={
                    "name": "agentbridge_host_workspace_session_bind",
                    "arguments": {
                        "agent_host": "openclaw",
                        "endpoint_key": "workspace:account-123",
                        "session_key": (
                            "agent:main:agentbridge-workspace:direct:account-123"
                        ),
                        "grant": "abwg_1234567890123456789012345678901234567890",
                        "turn_ref": "request-123",
                    },
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.redeem_workspace_gateway_grant.assert_called_once_with(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="workspace:account-123",
            session_key="agent:main:agentbridge-workspace:direct:account-123",
            grant="abwg_1234567890123456789012345678901234567890",
            turn_ref="request-123",
        )

    def test_host_artifact_delivery_report_uses_token_identity(self):
        with self._server() as (service, _store, token, client):
            service.report_host_artifact_delivery.return_value = {
                "protocolVersion": "0.1",
                "schemaVersion": "agentbridge.host-artifact-delivery.v1",
                "status": "succeeded",
                "task": {"taskId": "task-1234567890-abcdef"},
                "delivery": {"attachmentSentCount": 1},
                "eventId": "event-1234567890-abcdef",
                "reused": False,
            }
            files = [
                {
                    "artifact_id": "artifact-1234567890-abcdef",
                    "state": "attachment_sent",
                    "attempt_count": 1,
                    "error_code": None,
                }
            ]
            response = self._request(
                client,
                "tools/call",
                request_id=621,
                token=token,
                params={
                    "name": "agentbridge_host_artifact_delivery_report",
                    "arguments": {
                        "agent_host": "openclaw",
                        "task_id": "task-1234567890-abcdef",
                        "delivery_ref": "tool-result:certificate-batch",
                        "channel": "telegram",
                        "files": files,
                    },
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.report_host_artifact_delivery.assert_called_once_with(
            user_subject="user-a",
            agent_host="openclaw",
            task_id="task-1234567890-abcdef",
            delivery_ref="tool-result:certificate-batch",
            channel="telegram",
            files=files,
        )

    def test_host_task_continuation_uses_token_identity_and_private_metadata(self):
        with self._server() as (service, _store, token, client):
            service.resolve_host_task_continuation.return_value = {
                "protocolVersion": "0.1",
                "status": "selected",
                "task": {"taskId": "task-1234567890-abcdef"},
                "continuation": {"executionMode": "observe_only"},
            }
            response = self._request(
                client,
                "tools/call",
                request_id=621,
                token=token,
                params={
                    "name": "agentbridge_host_task_continuation_resolve",
                    "arguments": {
                        "agent_host": "openclaw",
                        "endpoint_key": "telegram:*:1001",
                        "task_id": "task-1234567890-abcdef",
                    },
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.resolve_host_task_continuation.assert_called_once_with(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            task_id="task-1234567890-abcdef",
            ordinal=None,
            source_client_type=None,
            cross_endpoint_only=False,
            prefer_active=True,
            prefer_latest=False,
            reuse_selected=True,
            allow_follow_up=False,
            max_age_minutes=1_440,
            limit=8,
        )

    def test_host_interaction_presentation_uses_endpoint_identity(self):
        with self._server() as (service, _store, token, client):
            service.present_interaction.return_value = {
                "protocolVersion": "0.1",
                "status": "succeeded",
                "interaction": {
                    "interactionId": "interaction-1234567890",
                    "presentation": {
                        "url": "https://cards.example.test/endpoint-card"
                    },
                },
            }
            response = self._request(
                client,
                "tools/call",
                request_id=63,
                token=token,
                params={
                    "name": "agentbridge_host_interaction_present",
                    "arguments": {
                        "agent_host": "openclaw",
                        "endpoint_key": "telegram:*:1001",
                        "interaction_id": "interaction-1234567890",
                    },
                    "_meta": {
                        "io.agentbridge/host-context": {
                            "version": "1",
                            "agentHost": "openclaw",
                            "hostInstanceId": "openclaw-gateway",
                            "hostVersion": "0.4.65",
                        }
                    },
                },
            )

        self.assertFalse(response.json()["result"]["isError"])
        service.present_interaction.assert_called_once_with(
            user_subject="user-a",
            agent_host="openclaw",
            endpoint_key="telegram:*:1001",
            interaction_id="interaction-1234567890",
        )

    def test_mcp_and_direct_service_share_idempotent_operation(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            service = CentralCapabilityService(
                home=home,
                base_url="http://oa.example.test/seeyon/main.do?method=main",
            )
            store = McpIdentityTokenStore(home / "agentbridge.db")
            issued = issue_authorized_token(store,
                user_subject="user-a",
                expected_principal_ref="Alice",
                ttl_seconds=3600,
            )
            grant_permissions(store.user_grants, "user-a", ["oa.leave.draft"])
            config = validate_central_mcp_server_config(
                host="127.0.0.1",
                port=8790,
                public_base_url="http://testserver",
                tls_cert=None,
                tls_key=None,
            )
            server = create_central_mcp_server(
                service=service,
                identity_store=store,
                config=config,
                auth_card_base_url="http://127.0.0.1:8780",
            )
            with TestClient(server.streamable_http_app()) as client:
                mcp_response = self._request(
                    client,
                    "tools/call",
                    request_id=5,
                    token=issued["token"],
                    params={
                        "name": "oa_template_list",
                        "arguments": {"idempotency_key": "shared-operation"},
                    },
                ).json()["result"]["structuredContent"]

            direct_response = service.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
                idempotency_key="shared-operation",
            )

        self.assertEqual(mcp_response["status"], "requires_user_action")
        self.assertEqual(direct_response["operationId"], mcp_response["operationId"])
        self.assertTrue(direct_response["reused"])

    def test_runtime_starts_and_stops_auth_card_with_mcp_server(self):
        service = MagicMock()
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        service.home = Path(home.name)
        identity_store = MagicMock()
        mcp_config = validate_central_mcp_server_config(
            host="127.0.0.1",
            port=8790,
            public_base_url=None,
            tls_cert=None,
            tls_key=None,
        )
        auth_config = AuthServerConfig(
            host="127.0.0.1",
            port=8780,
            public_base_url="http://127.0.0.1:8780",
            tls_cert=None,
            tls_key=None,
        )
        auth_server = MagicMock()
        mcp = MagicMock()
        from starlette.applications import Starlette
        app = Starlette()
        mcp.streamable_http_app.return_value = app

        with (
            patch("bscli.mcp.central.create_auth_http_server", return_value=auth_server),
            patch("bscli.mcp.central.create_central_mcp_server", return_value=mcp),
            patch("bscli.mcp.central.CentralSessionKeepalive") as keepalive_class,
            patch("bscli.mcp.central.uvicorn.run") as run,
        ):
            serve_central_mcp(
                service=service,
                identity_store=identity_store,
                mcp_config=mcp_config,
                auth_config=auth_config,
            )

        auth_server.serve_forever.assert_called_once_with(poll_interval=0.25)
        keepalive_class.return_value.start.assert_called_once_with()
        keepalive_class.return_value.stop.assert_called_once_with()
        run.assert_called_once()
        self.assertIs(run.call_args.args[0], app)
        auth_server.shutdown.assert_called_once()
        auth_server.server_close.assert_called_once()

    def test_runtime_closes_auth_card_if_mcp_initialization_fails(self):
        service = MagicMock()
        home = TemporaryDirectory()
        self.addCleanup(home.cleanup)
        service.home = Path(home.name)
        mcp_config = validate_central_mcp_server_config(
            host="127.0.0.1",
            port=8790,
            public_base_url=None,
            tls_cert=None,
            tls_key=None,
        )
        auth_config = AuthServerConfig(
            host="127.0.0.1",
            port=8780,
            public_base_url="http://127.0.0.1:8780",
            tls_cert=None,
            tls_key=None,
        )
        auth_server = MagicMock()

        with (
            patch("bscli.mcp.central.create_auth_http_server", return_value=auth_server),
            patch(
                "bscli.mcp.central.create_central_mcp_server",
                side_effect=RuntimeError("MCP setup failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "MCP setup failed"),
        ):
            serve_central_mcp(
                service=service,
                identity_store=MagicMock(),
                mcp_config=mcp_config,
                auth_config=auth_config,
            )

        auth_server.shutdown.assert_called_once()
        auth_server.server_close.assert_called_once()

    def _server(self):
        return CentralMcpFixture()

    @staticmethod
    def _request(
        client,
        method,
        *,
        request_id,
        params=None,
        token=None,
        authenticated=True,
    ):
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        return client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            },
        )


class CentralMcpFixture:
    def __enter__(self):
        self.temp = TemporaryDirectory()
        self.service = MagicMock()
        self.service.require_host_registration.return_value = {
            "hostInstanceId": "openclaw-gateway",
            "agentHost": "openclaw",
            "hostVersion": "0.4.65",
            "acceptedLevel": "L3",
        }
        self.service.validate_host_call_context.return_value = {
            "task": None,
            "endpoint": None,
            "registration": self.service.require_host_registration.return_value,
        }
        self.store = McpIdentityTokenStore(Path(self.temp.name) / "agentbridge.db")
        issued = issue_authorized_token(self.store,
            user_subject="user-a",
            expected_principal_ref="Alice",
            label="test-client",
            ttl_seconds=3600,
        )
        self.token = issued["token"]
        config = validate_central_mcp_server_config(
            host="127.0.0.1",
            port=8790,
            public_base_url="http://testserver",
            tls_cert=None,
            tls_key=None,
        )
        self.server = create_central_mcp_server(
            service=self.service,
            identity_store=self.store,
            config=config,
            auth_card_base_url="http://127.0.0.1:8780",
        )
        self.client_context = TestClient(self.server.streamable_http_app())
        self.client = self.client_context.__enter__()
        return self.service, self.store, self.token, self.client

    def __exit__(self, exc_type, exc, traceback):
        self.client_context.__exit__(exc_type, exc, traceback)
        self.temp.cleanup()


def _trusted_interaction(card_url):
    return {
        "schemaVersion": "agentbridge.interaction.v1",
        "interactionId": "interaction-1234567890",
        "type": "business_input",
        "state": "pending",
        "title": "Business trip input",
        "message": "Enter the requested business fields.",
        "presentation": {
            "owner": "agentbridge",
            "preferred": "embedded_secure_web_app",
            "fallback": "url",
            "url": card_url,
            "modelMustNotCollectValues": True,
        },
        "display": {"systemName": "OA", "fieldCount": 6},
        "poll": {
            "tool": "agentbridge_interaction_get",
            "recommendedIntervalSeconds": 2,
        },
        "resume": {
            "tool": "agentbridge_interaction_resume",
            "ready": False,
            "completed": False,
        },
    }


if __name__ == "__main__":
    unittest.main()
