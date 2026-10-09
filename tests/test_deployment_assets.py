from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentAssetTests(unittest.TestCase):

    def test_systemd_service_cannot_import_legacy_app_source(self) -> None:
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(encoding="utf-8")

        self.assertIn("WorkingDirectory=/home/agentbridge/service\n", unit)
        self.assertNotIn("WorkingDirectory=/home/agentbridge/service/app", unit)
        self.assertIn("venv/bin/python -P -m agentbridge.cli.main", unit)

    def test_admin_console_is_deployed_with_tls_and_release_metadata(self) -> None:
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )

        for marker in (
            "EnvironmentFile=-/home/agentbridge/service/config/release.env",
            "--admin-host 127.0.0.1",
            "--admin-port 8782",
            "--admin-public-base-url https://127.0.0.1:8782",
            "--admin-tls-cert /home/agentbridge/service/config/tls/server.crt",
            "--admin-tls-key /home/agentbridge/service/config/tls/server.key",
        ):
            self.assertIn(marker, unit)
        runner = (ROOT / "scripts/agentbridge_release.py").read_text(encoding="utf-8")
        self.assertIn("AGENTBRIDGE_RELEASE_ID", runner)
        self.assertIn("def atomic_write(path, data, mode=0o640)", runner)

    def test_smartlight_adapter_requires_explicit_plain_http_opt_in(self) -> None:
        unit = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "--smartlight-base-url https://lighting.example.test/smartlight",
            unit,
        )
        self.assertNotIn("--smartlight-allow-insecure-http", unit)


    def test_yuque_remote_login_uses_challenge_isolation_and_native_novnc(self) -> None:
        service = (ROOT / "deploy/systemd/agentbridge.service").read_text(
            encoding="utf-8"
        )
        deploy = (ROOT / "scripts/native/publish.py").read_text(
            encoding="utf-8"
        )
        broker = (ROOT / "agentbridge/broker/remote_browser.py").read_text(
            encoding="utf-8"
        )

        for retired in (
            "Requires=agentbridge-xvfb.service",
            "JoinsNamespaceOf=agentbridge-xvfb.service",
            "Environment=DISPLAY=:99",
            "Environment=XAUTHORITY=",
        ):
            self.assertNotIn(retired, service)
        self.assertFalse(
            (ROOT / "deploy/systemd/agentbridge-xvfb.service").exists()
        )
        for marker in (
            "Xvfb x11vnc websockify xauth",
            "test -d /usr/share/novnc",
        ):
            self.assertIn(marker, deploy)
        for marker in (
            "-nolisten",
            '"tcp"',
            '"-localhost"',
            '"--token-plugin=TokenFile"',
            '"--remote-debugging-address=127.0.0.1"',
            'f"--remote-debugging-port={self.allocation.cdp_port}"',
        ):
            self.assertIn(marker, broker)
        for retired in ("--enable-automation", "--remote-debugging-pipe"):
            self.assertNotIn(retired, broker)

    def test_release_smoke_requires_new_write_tools(self) -> None:
        smoke = (ROOT / "scripts/agentbridge-mcp-smoke.mjs").read_text(encoding="utf-8")

        for tool in (
            "oa_certificate_search",
            "oa_certificate_prepare_download",
            "oa_certificate_prepare_downloads",
            "oa_business_trip_prepare",
            "oa_business_trip_save_draft",
            "oa_business_trip_submit_prepare",
            "oa_business_trip_submit",
            "oa_leave_prepare",
            "oa_leave_save_draft",
            "oa_leave_submit_prepare",
            "oa_leave_submit",
            "oa_workflow_revoke_prepare",
            "oa_workflow_revoke",
            "oa_missed_punch_prepare",
            "oa_missed_punch_save_draft",
            "oa_missed_punch_approval_prepare",
            "oa_missed_punch_approval_batch_prepare",
            "oa_missed_punch_approve",
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
            "oa_meeting_room_availability_list",
            "oa_meeting_room_my_applications_list",
            "oa_meeting_room_application_prepare",
            "oa_meeting_room_application_create",
            "oa_meeting_room_application_cancel_prepare",
            "oa_meeting_room_application_cancel",
            "oa_meeting_create_prepare",
            "oa_meeting_create",
            "smartlight_alarm_remark_update_prepare",
            "smartlight_alarm_remark_update",
            "smartlight_alarm_work_area_submit_prepare",
            "smartlight_alarm_work_area_submit",
            "smartlight_alarm_work_area_revoke_prepare",
            "smartlight_alarm_work_area_revoke",
            "smartlight_rtu_alarm_dispose_prepare",
            "smartlight_rtu_alarm_dispose",
        ):
            self.assertIn(tool, smoke)

        for check in (
            "SmartlightSessionStatus",
            "SmartlightOverview",
            "SmartlightRuntimeOverview",
            "SmartlightRtuStatus",
            "SmartlightLampStatus",
            "SmartlightLampAlarms",
            "SmartlightLampAlarmAnalysis",
            "SmartlightAlarmRemark",
            "SmartlightRtuSurvey",
            "ToolCatalog",
        ):
            self.assertIn(check, smoke)
        for report_type in (
            "energy_records",
            "energy_analysis",
            "lamp_survey_records",
            "rtu_leakage_alarms",
            "rtu_leakage_analysis",
            "inspection_logs",
            "maintenance_records",
        ):
            self.assertIn(f'"{report_type}"', smoke)
        self.assertIn("result.downstreamTotal", smoke)
        self.assertIn('businessSessionCheck: false', smoke)


    def test_pending_action_preflight_is_read_only_by_construction(self) -> None:
        script = (
            ROOT / "scripts/validate_oa_pending_actions_preflight.py"
        ).read_text(encoding="utf-8")

        for prepare_function in (
            "prepare_missed_punch_approval",
            "prepare_efficiency_data_approval",
            "prepare_travel_expense_approval",
            "prepare_business_trip_approval",
            "prepare_labor_contract_renewal_approval",
            "prepare_intellectual_property_declaration_approval",
            "prepare_overtime_approval",
            "prepare_leave_approval",
            "prepare_resignation_approval",
            "prepare_work_handover_approval",
            "prepare_flight_application_approval",
            "prepare_attendance_confirmation",
            "prepare_weekly_report_acknowledgement",
            "prepare_standard_collaboration_approval",
        ):
            self.assertIn(prepare_function, script)
        for forbidden_function in (
            "approve_missed_punch_request",
            "approve_efficiency_data",
            "approve_travel_expense",
            "approve_business_trip_request",
            "approve_labor_contract_renewal",
            "approve_intellectual_property_declaration",
            "approve_overtime",
            "approve_leave_request",
            "approve_resignation",
            "approve_work_handover",
            "approve_flight_application",
            "confirm_attendance",
            "acknowledge_weekly_report",
            "approve_standard_collaboration",
        ):
            self.assertNotIn(forbidden_function, script)
        self.assertIn('"write_controls_clicked": 0', script)
        self.assertIn('"collaboration_write_requests": 0', script)
        self.assertIn('"authorizations_created": 0', script)
        self.assertNotIn("state_store.save", script)


    def test_unit_document_probe_is_read_only_by_construction(self) -> None:
        script = (
            ROOT / "scripts/inspect_oa_unit_documents.py"
        ).read_text(encoding="utf-8")

        for marker in (
            '"downloads_started": 0',
            '"write_controls_clicked": 0',
            '"response_bodies_included": False',
            'method in {"DELETE", "PATCH", "PUT"}',
            "_BLOCKED_WRITE_MARKERS",
        ):
            self.assertIn(marker, script)
        for forbidden_function in (
            "expect_download",
            "state_store.save",
            "set_input_files",
            "request.post(",
            "request.put(",
        ):
            self.assertNotIn(forbidden_function, script)

if __name__ == "__main__":
    unittest.main()
