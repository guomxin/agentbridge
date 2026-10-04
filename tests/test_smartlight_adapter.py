from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlparse
import unittest

from bscli.adapters.smartlight import (
    SMARTLIGHT_ALARM_ANALYSIS_CAPABILITY,
    SMARTLIGHT_ALARM_LIST_CAPABILITY,
    SMARTLIGHT_ALARM_REMARK_GET_CAPABILITY,
    SMARTLIGHT_ALARM_REMARK_UPDATE_CAPABILITY,
    SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_REVOKE_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_CAPABILITY,
    SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY,
    SMARTLIGHT_ASSET_DETAIL_CAPABILITY,
    SMARTLIGHT_ASSET_SEARCH_CAPABILITY,
    SMARTLIGHT_ENERGY_ANALYSIS_CAPABILITY,
    SMARTLIGHT_ENERGY_RECORD_LIST_CAPABILITY,
    SMARTLIGHT_INSPECTION_LOG_LIST_CAPABILITY,
    SMARTLIGHT_INSPECTION_TASK_DETAIL_CAPABILITY,
    SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY,
    SMARTLIGHT_LAMPPOST_LIST_CAPABILITY,
    SMARTLIGHT_LAMP_ALARM_ANALYSIS_CAPABILITY,
    SMARTLIGHT_LAMP_ALARM_LIST_CAPABILITY,
    SMARTLIGHT_LAMP_STATUS_LIST_CAPABILITY,
    SMARTLIGHT_LAMP_SURVEY_RECORDS_CAPABILITY,
    SMARTLIGHT_LEAKAGE_ANALYSIS_CAPABILITY,
    SMARTLIGHT_LEAKAGE_SUMMARY_CAPABILITY,
    SMARTLIGHT_OVERVIEW_CAPABILITY,
    SMARTLIGHT_OFF_HOURS_CURRENT_LIST_CAPABILITY,
    SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
    SMARTLIGHT_RTU_STATUS_LIST_CAPABILITY,
    SMARTLIGHT_RTU_LEAKAGE_ALARM_LIST_CAPABILITY,
    SMARTLIGHT_RTU_LEAKAGE_ANALYSIS_CAPABILITY,
    SMARTLIGHT_RTU_SURVEY_RECORDS_CAPABILITY,
    SMARTLIGHT_RUNTIME_OVERVIEW_CAPABILITY,
    SMARTLIGHT_MAINTENANCE_RECORD_LIST_CAPABILITY,
    SMARTLIGHT_RTU_ALARM_DISPOSE_CAPABILITY,
    SMARTLIGHT_RTU_ALARM_DISPOSE_PREPARE_CAPABILITY,
    SmartlightAlarmActionOutcomeUnknown,
    SmartlightBusinessRuleRejected,
    SmartlightAuthenticationRejected,
    SmartlightCentralAdapter,
    SmartlightLoginRequired,
    SmartlightSessionCheckUnavailable,
    _normalize_alarm,
    _resolve_date_range,
    _resolve_survey_time_range,
    build_smartlight_capability_registry,
    commit_smartlight_alarm_remark_update,
    commit_smartlight_alarm_work_area_revoke,
    commit_smartlight_alarm_work_area_submit,
    commit_smartlight_rtu_alarm_dispose,
    prepare_smartlight_alarm_work_area_revoke,
    prepare_smartlight_alarm_work_area_submit,
    prepare_smartlight_alarm_remark_update,
    prepare_smartlight_rtu_alarm_dispose,
)
from bscli.core.central_service import capability_required_scopes


class SmartlightAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = SmartlightCentralAdapter(
            base_url="http://123.232.113.241:4101/smartlight",
            allow_insecure_http=True,
        )

    def test_plain_http_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "explicit"):
            SmartlightCentralAdapter(
                base_url="http://123.232.113.241:4101/smartlight"
            )

    def test_alarm_state_codes_use_the_authoritative_labels(self):
        expected = {
            0: "当前告警",
            1: "非当前告警",
            2: "已解除报警",
            3: "已处置",
        }

        for code, label in expected.items():
            with self.subTest(code=code):
                alarm = _normalize_alarm(
                    {"hitchAlarmId": "alarm-1", "alarmState": code}
                )
                self.assertEqual(alarm["state"], label)
                self.assertEqual(alarm["stateLabel"], label)

        unknown = _normalize_alarm(
            {"hitchAlarmId": "alarm-unknown", "alarmState": 99}
        )
        self.assertEqual(unknown["stateLabel"], "未知状态（代码 99）")

    def test_authentication_contract_uses_trusted_captcha_card(self):
        contract = self.adapter.authentication_contract()

        self.assertEqual(contract["system_id"], "smartlight")
        self.assertNotIn("authentication_mode", contract)
        self.assertEqual(
            [field["name"] for field in contract["fields"]],
            ["username", "password", "authcode"],
        )
        self.assertEqual(
            contract["prepared_authentication"]["kind"],
            "image_captcha",
        )

    def test_prepares_captcha_then_authenticates_and_captures_jwt(self):
        worker = FakeSmartlightWorker()

        prepared = self.adapter.prepare_authentication(worker, timeout_seconds=20)
        recovered = self.adapter.recover_prepared_authentication(worker)
        result = self.adapter.authenticate(
            worker,
            {"username": "yanshi", "password": "secret", "authcode": "1234"},
            timeout_seconds=20,
        )

        self.assertEqual(prepared["captcha"]["content_type"], "image/jpeg")
        self.assertEqual(recovered, prepared)
        self.assertEqual(result["observed_principal_ref"], "无为")
        self.assertEqual(result["principal"]["account"], "yanshi")
        self.assertNotIn("password_digest", result["principal"])
        self.assertEqual(worker.state["access_token"], "jwt-access")
        submitted = parse_qs(worker.cas_submission)
        self.assertEqual(submitted["username"], ["eWFuc2hp"])
        self.assertEqual(submitted["authcode"], ["1234"])
        self.assertEqual(
            submitted["password"],
            ["5EBE2294ECD0E0F08EAB7690D2A6EE69"],
        )
        self.assertEqual(worker.cas_headers["Origin"], self.adapter.origin)
        self.assertIn("/cas/login", worker.cas_headers["Referer"])
        self.assertIn("Mozilla/5.0", worker.cas_headers["User-Agent"])
        self.assertIn("/cas/login", worker.captcha_headers["Referer"])

    def test_probe_session_prefers_refresh_token_without_requiring_cas(self):
        worker = FakeSmartlightWorker(authenticated=True)

        result = self.adapter.probe_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(result["observed_principal_ref"], "无为")
        self.assertEqual(worker.state["access_token"], "jwt-access-refreshed")
        self.assertEqual(worker.refresh_requests, 1)
        self.assertEqual(worker.principal_requests, 0)
        self.assertEqual(worker.token_exchange_requests, 0)

    def test_probe_session_persists_rotated_refresh_token(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.rotated_refresh_token = "jwt-refresh-rotated"

        result = self.adapter.probe_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(worker.state["refresh_token"], "jwt-refresh-rotated")

    def test_keepalive_renews_complete_token_pair_through_cas(self):
        worker = FakeSmartlightWorker(authenticated=True)

        result = self.adapter.keepalive_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(worker.state["access_token"], "jwt-access")
        self.assertEqual(worker.state["refresh_token"], "jwt-refresh")
        self.assertEqual(worker.principal_requests, 1)
        self.assertEqual(worker.token_exchange_requests, 1)
        self.assertEqual(worker.refresh_requests, 0)

    def test_keepalive_falls_back_to_refresh_when_cas_is_logged_out(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.empty_principal = True

        result = self.adapter.keepalive_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(worker.principal_requests, 1)
        self.assertEqual(worker.token_exchange_requests, 0)
        self.assertEqual(worker.refresh_requests, 1)

    def test_keepalive_recovers_application_session_through_cas_sso(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.principal_requires_cas_renewal = True

        result = self.adapter.keepalive_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(worker.cas_renewal_requests, 1)
        self.assertEqual(worker.principal_requests, 2)
        self.assertEqual(worker.token_exchange_requests, 1)
        self.assertEqual(worker.refresh_requests, 0)
        self.assertEqual(result["session_recovery"], "cas_sso")

    def test_probe_session_falls_back_to_cas_when_refresh_is_rejected(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.reject_refresh_token = True

        result = self.adapter.probe_session(worker)

        self.assertTrue(result["authenticated"])
        self.assertEqual(worker.state["access_token"], "jwt-access")
        self.assertEqual(worker.refresh_requests, 1)
        self.assertEqual(worker.principal_requests, 1)
        self.assertEqual(worker.token_exchange_requests, 1)

    def test_probe_session_expires_when_refresh_and_cas_are_rejected(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.reject_refresh_token = True
        worker.empty_principal = True

        with self.assertRaises(SmartlightLoginRequired) as raised:
            self.adapter.probe_session(worker)

        self.assertIn("refresh token and CAS session", str(raised.exception))
        self.assertIn("SmartlightLoginRequired", str(raised.exception))

    def test_probe_session_expires_for_http_200_refresh_expiry_response(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.refresh_expired_payload = True
        worker.empty_principal = True

        with self.assertRaises(SmartlightLoginRequired) as raised:
            self.adapter.probe_session(worker)

        self.assertIn("refresh token and CAS session", str(raised.exception))
        self.assertEqual(worker.refresh_requests, 1)

    def test_probe_session_rejects_expired_jwt_before_network_refresh(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.state["refresh_token"] = _unsigned_jwt(exp=1)
        worker.empty_principal = True

        with self.assertRaises(SmartlightLoginRequired):
            self.adapter.probe_session(worker)

        self.assertEqual(worker.refresh_requests, 0)

    def test_probe_session_preserves_session_when_refresh_is_temporarily_unavailable(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.refresh_temporarily_unavailable = True
        worker.empty_principal = True

        with self.assertRaises(SmartlightSessionCheckUnavailable) as raised:
            self.adapter.probe_session(worker)

        self.assertIn("preserved for retry", str(raised.exception))
        self.assertIn("SmartlightSessionCheckUnavailable", str(raised.exception))
        self.assertIn("SmartlightLoginRequired", str(raised.exception))

    def test_probe_session_classifies_empty_principal_as_logged_out(self):
        worker = FakeSmartlightWorker()
        worker.empty_principal = True

        with self.assertRaises(SmartlightLoginRequired):
            self.adapter.probe_session(worker)

        self.assertEqual(worker.principal_requests, 1)
        self.assertEqual(worker.token_exchange_requests, 0)

    def test_classifies_captcha_rejection_from_cas_error_page(self):
        worker = FakeSmartlightWorker(login_rejection="验证码错误，请重新输入")
        self.adapter.prepare_authentication(worker, timeout_seconds=20)

        with self.assertRaises(SmartlightAuthenticationRejected) as raised:
            self.adapter.authenticate(
                worker,
                {"username": "yanshi", "password": "secret", "authcode": "1234"},
                timeout_seconds=20,
            )

        self.assertEqual(raised.exception.error_code, "CAPTCHA_REJECTED")

    def test_classifies_account_rejection_from_cas_error_page(self):
        worker = FakeSmartlightWorker(login_rejection="用户名或密码错误")
        self.adapter.prepare_authentication(worker, timeout_seconds=20)

        with self.assertRaises(SmartlightAuthenticationRejected) as raised:
            self.adapter.authenticate(
                worker,
                {"username": "yanshi", "password": "secret", "authcode": "1234"},
                timeout_seconds=20,
            )

        self.assertEqual(raised.exception.error_code, "CREDENTIALS_REJECTED")

    def test_read_capabilities_use_observed_api_contracts(self):
        worker = FakeSmartlightWorker(authenticated=True)

        overview = self.adapter.invoke_capability(
            SMARTLIGHT_OVERVIEW_CAPABILITY, worker, {}
        )
        lamp_posts = self.adapter.invoke_capability(
            SMARTLIGHT_LAMPPOST_LIST_CAPABILITY,
            worker,
            {"keyword": "LP-", "page": 1, "size": 10},
        )
        alarms = self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_LIST_CAPABILITY,
            worker,
            {"page": 1, "size": 10},
        )
        tasks = self.adapter.invoke_capability(
            SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY,
            worker,
            {"task_name": "夜巡", "state": 2, "page": 1, "size": 10},
        )
        compatibility_alarm_list = self.adapter.invoke_capability(
            SMARTLIGHT_LEAKAGE_SUMMARY_CAPABILITY,
            worker,
            {"start_date": "2026-08-01", "end_date": "2026-08-11"},
        )

        self.assertEqual(overview["cabinetTotal"], 1)
        self.assertEqual(overview["lampPostTotal"], 131)
        self.assertEqual(
            overview["lampPostCounts"],
            {"searchable": 131, "mapDetail": 116},
        )
        self.assertEqual(lamp_posts["items"][0]["code"], "LP-001")
        self.assertEqual(alarms["summary"]["untreated"], 2)
        self.assertEqual(alarms["alarmSource"], "rtu")
        self.assertEqual(alarms["sort"]["field"], "occurredAt")
        self.assertEqual(alarms["sort"]["scope"], "downstream_global")
        self.assertTrue(alarms["sort"]["verified"])
        self.assertEqual(alarms["summaryScope"]["type"], "current_system_snapshot")
        self.assertEqual(alarms["items"][0]["id"], "alarm-1")
        self.assertEqual(alarms["items"][0]["occurredAt"], "2026-08-10 10:05:38")
        self.assertEqual(
            alarms["items"][0]["lastActivityAt"],
            "2026-08-12 10:30:02",
        )
        self.assertEqual(alarms["items"][0]["type"], "电源缺相")
        self.assertEqual(alarms["items"][0]["message"], "电源缺相(B,C)")
        self.assertEqual(alarms["items"][0]["alarmWeight"], 3)
        self.assertEqual(alarms["items"][0]["workAreaId"], "work-area-1")
        self.assertEqual(alarms["items"][0]["workArea"], "实验室工区")
        self.assertFalse(alarms["items"][0]["workAreaSubmitted"])
        self.assertEqual(alarms["items"][0]["workAreaSubmitState"], 0)
        self.assertEqual(
            alarms["items"][0]["workAreaSubmitStateLabel"],
            "未提交",
        )
        self.assertEqual(tasks["items"][0]["taskName"], "夜巡一组")
        self.assertEqual(tasks["items"][0]["inspectionGroup"], "一号巡检组")
        self.assertEqual(tasks["items"][0]["startTime"], "2026-08-01")
        self.assertEqual(tasks["items"][0]["endTime"], "2026-08-31")
        self.assertEqual(tasks["items"][0]["progress"], "25.00%")
        self.assertEqual(tasks["items"][0]["stateCode"], 2)
        self.assertEqual(tasks["items"][0]["stateLabel"], "执行中")
        self.assertEqual(
            tasks["items"][0]["deviceCounts"],
            {"confirmed": 4, "lampPosts": 12, "rtus": 2},
        )
        self.assertEqual(
            tasks["items"][0]["progressScope"],
            "downstream_reported_independent_metric",
        )
        self.assertTrue(compatibility_alarm_list["deprecated"])
        self.assertEqual(
            compatibility_alarm_list["canonicalTool"],
            "smartlight_lamp_alarm_list",
        )
        self.assertEqual(compatibility_alarm_list["alarmSource"], "single_lamp")
        self.assertEqual(
            compatibility_alarm_list["items"][0]["alarmType"],
            "异常亮灯",
        )
        self.assertNotIn("value", compatibility_alarm_list["items"][0])
        self.assertTrue(
            all("x-Authentication-Token" in headers for headers in worker.api_headers)
        )
        task_request = next(
            item
            for item in worker.api_requests
            if item["path"].endswith("/inspectionTask/getDataByCondition")
        )
        task_query = json.loads(parse_qs(task_request["body"])["json"][0])
        self.assertEqual(task_query["_taskState"], 2)

    def test_runtime_lamp_alarm_and_survey_capabilities_use_observed_contracts(self):
        worker = FakeSmartlightWorker(authenticated=True)

        runtime = self.adapter.invoke_capability(
            SMARTLIGHT_RUNTIME_OVERVIEW_CAPABILITY, worker, {}
        )
        rtus = self.adapter.invoke_capability(
            SMARTLIGHT_RTU_STATUS_LIST_CAPABILITY,
            worker,
            {"state": "offline", "page": 1, "size": 20},
        )
        lamps = self.adapter.invoke_capability(
            SMARTLIGHT_LAMP_STATUS_LIST_CAPABILITY,
            worker,
            {"controller_state": "offline", "page": 1, "size": 20},
        )
        lamp_alarms = self.adapter.invoke_capability(
            SMARTLIGHT_LAMP_ALARM_LIST_CAPABILITY,
            worker,
            {"last_days": 30, "page": 1, "size": 20},
        )
        lamp_analysis = self.adapter.invoke_capability(
            SMARTLIGHT_LAMP_ALARM_ANALYSIS_CAPABILITY,
            worker,
            {"last_days": 30},
        )
        survey = self.adapter.invoke_capability(
            SMARTLIGHT_RTU_SURVEY_RECORDS_CAPABILITY,
            worker,
            {
                "rtu_id": "rtu-1",
                "start_time": "2026-08-10 00:00",
                "end_time": "2026-08-10 23:59",
            },
        )

        self.assertEqual(runtime["scope"], "authenticated_user_runtime_pages")
        self.assertEqual(runtime["rtu"]["total"], 29)
        self.assertEqual(runtime["singleLamp"]["controllerTotal"], 118)
        self.assertEqual(rtus["items"][0]["state"], "离线")
        self.assertEqual(rtus["items"][0]["telemetry"]["phaseVoltage"]["a"], 220.1)
        self.assertFalse(lamps["items"][0]["controllerOnline"])
        self.assertEqual(lamps["items"][0]["lamps"][0]["effect"], "主道灯")
        self.assertEqual(lamp_alarms["alarmSource"], "single_lamp")
        self.assertEqual(lamp_alarms["items"][0]["alarmType"], "异常亮灯")
        self.assertEqual(lamp_analysis["topAlarmTypes"][0]["value"], "异常亮灯")
        self.assertTrue(survey["resolved"])
        self.assertEqual(survey["items"][0]["phaseVoltage"]["a"], 220.1)
        self.assertEqual(survey["items"][0]["leakCurrents"], [0.02, 0.01])
        self.assertEqual(survey["items"][0]["phaseCurrentRatio"]["a"], "1.1/20")
        self.assertEqual(survey["items"][0]["circuitCurrents"], {"1": "1.1/3"})
        self.assertEqual(survey["items"][0]["phaseCircuits"]["a"], "1,4")
        self.assertEqual(survey["items"][0]["state"], "正常")

        request_by_path = {item["path"]: item for item in worker.api_requests}
        rtu_query = json.loads(
            parse_qs(request_by_path["/smartlight/rRtu/getRtusByConditionNew"]["body"])[
                "json"
            ][0]
        )
        self.assertEqual(rtu_query["filterParam"], "NoOnlineWithNoHandle")
        survey_query = json.loads(
            parse_qs(
                request_by_path[
                    "/smartlight/rHisCoplogPhase/getDataByCondition"
                ]["body"]
            )["json"][0]
        )
        self.assertEqual(survey_query["rtuId"], "rtu-1")
        self.assertEqual(survey_query["_timebegin_addDateTime"], "2026-08-10 00:00")

    def test_alarm_list_uses_downstream_time_sort_and_reports_latest_ties(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_records = [
            {
                **deepcopy(worker.alarm_record),
                "hitchAlarmId": "alarm-a",
                "occurDate": "2026-08-15 10:00:00",
                "lastDate": "2026-08-15 10:01:00",
            },
            {
                **deepcopy(worker.alarm_record),
                "hitchAlarmId": "alarm-b",
                "occurDate": "2026-08-15 10:00:00",
                "lastDate": "2026-08-15 10:02:00",
            },
            {
                **deepcopy(worker.alarm_record),
                "hitchAlarmId": "alarm-c",
                "occurDate": "2026-08-14 09:00:00",
                "lastDate": "2026-08-15 11:00:00",
            },
        ]

        latest = self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_LIST_CAPABILITY,
            worker,
            {"page": 1, "size": 1},
        )
        active = self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_LIST_CAPABILITY,
            worker,
            {"sort_by": "last_activity", "page": 1, "size": 1},
        )

        self.assertEqual(latest["items"][0]["id"], "alarm-b")
        self.assertEqual(latest["latestGroup"]["exactCount"], 2)
        self.assertTrue(latest["latestGroup"]["complete"])
        self.assertEqual(
            [item["id"] for item in latest["latestGroup"]["candidates"]],
            ["alarm-b", "alarm-a"],
        )
        self.assertEqual(active["items"][0]["id"], "alarm-c")
        self.assertEqual(active["sort"]["field"], "lastActivityAt")
        alarm_queries = [
            json.loads(parse_qs(request["body"])["json"][0])
            for request in worker.api_requests
            if request["path"].endswith("/rHisHitchAlarm/getDataByRtuAlarm")
        ]
        self.assertEqual(
            [query["dateType"] for query in alarm_queries],
            ["0", "0", "1", "1"],
        )
        self.assertTrue(all("codeOrName" in query for query in alarm_queries))
        self.assertTrue(all("_like_params" not in query for query in alarm_queries))

    def test_alarm_list_sends_keyword_using_the_downstream_device_filter(self):
        worker = FakeSmartlightWorker(authenticated=True)

        self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_LIST_CAPABILITY,
            worker,
            {"keyword": "五一路东", "page": 1, "size": 20},
        )

        request = next(
            item
            for item in worker.api_requests
            if item["path"].endswith("/rHisHitchAlarm/getDataByRtuAlarm")
        )
        query = json.loads(parse_qs(request["body"])["json"][0])
        self.assertEqual(query["codeOrName"], "五一路东")
        self.assertNotIn("_like_params", query)

    def test_runtime_name_filters_are_applied_without_sending_names_as_ids(self):
        worker = FakeSmartlightWorker(authenticated=True)

        rtus = self.adapter.invoke_capability(
            SMARTLIGHT_RTU_STATUS_LIST_CAPABILITY,
            worker,
            {"state": "offline", "work_area": "实验室", "size": 20},
        )
        lamps = self.adapter.invoke_capability(
            SMARTLIGHT_LAMP_STATUS_LIST_CAPABILITY,
            worker,
            {"controller_state": "offline", "street": "测试路", "size": 20},
        )
        alarms = self.adapter.invoke_capability(
            SMARTLIGHT_LAMP_ALARM_LIST_CAPABILITY,
            worker,
            {"last_days": 30, "cabinet": "一号箱变", "size": 20},
        )

        self.assertEqual(rtus["count"], 1)
        self.assertEqual(lamps["count"], 1)
        self.assertEqual(alarms["count"], 1)
        request_queries = [
            json.loads(parse_qs(request["body"])["json"][0])
            for request in worker.api_requests
            if "json=" in str(request.get("body") or "")
        ]
        self.assertTrue(
            all(
                "实验室" not in json.dumps(query, ensure_ascii=False)
                and "测试路" not in json.dumps(query, ensure_ascii=False)
                and "一号箱变" not in json.dumps(query, ensure_ascii=False)
                for query in request_queries
            )
        )

    def test_alarm_remark_get_is_read_only_and_authoritative(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_remark = {
            "remark": "现场已复核",
            "createUser": "无为",
            "createTime": "2026-08-15 11:00:00",
        }

        result = self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_REMARK_GET_CAPABILITY,
            worker,
            {"alarm_id": "alarm-1"},
        )

        self.assertEqual(result["alarmId"], "alarm-1")
        self.assertEqual(result["remark"], "现场已复核")
        self.assertTrue(result["hasRemark"])
        self.assertEqual(result["createUser"], "无为")
        self.assertEqual(worker.saved_alarm_payloads, [])

    def test_alarm_list_normalizes_blank_work_area_state_as_not_submitted(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_record["isSubmitWorkArea"] = ""

        alarms = self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_LIST_CAPABILITY,
            worker,
            {"page": 1, "size": 10},
        )

        self.assertFalse(alarms["items"][0]["workAreaSubmitted"])
        self.assertEqual(alarms["items"][0]["workAreaSubmitState"], 0)

    def test_relative_date_range_is_computed_in_business_timezone(self):
        start, end, source, last_days = _resolve_date_range(
            {"last_days": 30},
            now=datetime(
                2026,
                8,
                12,
                1,
                30,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )

        self.assertEqual(start, "2026-07-14")
        self.assertEqual(end, "2026-08-12")
        self.assertEqual(source, "last_days")
        self.assertEqual(last_days, 30)

    def test_survey_range_defaults_to_24_hours_and_rejects_more_than_7_days(self):
        start, end, source = _resolve_survey_time_range(
            {},
            now=datetime(
                2026,
                8,
                20,
                12,
                30,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )

        self.assertEqual(start, "2026-08-19 12:30")
        self.assertEqual(end, "2026-08-20 12:30")
        self.assertEqual(source, "default_last_24_hours")
        with self.assertRaisesRegex(ValueError, "cannot exceed 7 days"):
            _resolve_survey_time_range(
                {
                    "start_time": "2026-08-01 00:00",
                    "end_time": "2026-08-09 00:00",
                }
            )

    def test_inspection_state_string_is_normalized_before_downstream_request(self):
        worker = FakeSmartlightWorker(authenticated=True)

        result = self.adapter.invoke_capability(
            SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY,
            worker,
            {"state": "2", "page": 1, "size": 3},
        )

        task_request = next(
            item
            for item in worker.api_requests
            if item["path"].endswith("/inspectionTask/getDataByCondition")
        )
        task_query = json.loads(parse_qs(task_request["body"])["json"][0])
        self.assertEqual(task_query["_taskState"], 2)
        self.assertEqual(result["filters"]["state"], 2)

    def test_phase_two_asset_detail_and_bounded_analysis_contracts(self):
        worker = FakeSmartlightWorker(authenticated=True)

        cabinets = self.adapter.invoke_capability(
            SMARTLIGHT_ASSET_SEARCH_CAPABILITY,
            worker,
            {"asset_type": "cabinet", "keyword": "一号", "page": 1, "size": 20},
        )
        rtu = self.adapter.invoke_capability(
            SMARTLIGHT_ASSET_DETAIL_CAPABILITY,
            worker,
            {"asset_type": "rtu", "asset_id": "rtu-1"},
        )
        lamp_post = self.adapter.invoke_capability(
            SMARTLIGHT_ASSET_DETAIL_CAPABILITY,
            worker,
            {"asset_type": "lamppost", "asset_id": "lp-1"},
        )
        alarms = self.adapter.invoke_capability(
            SMARTLIGHT_ALARM_ANALYSIS_CAPABILITY,
            worker,
            {"alarm_state": "current", "top_n": 5},
        )
        inspection = self.adapter.invoke_capability(
            SMARTLIGHT_INSPECTION_TASK_DETAIL_CAPABILITY,
            worker,
            {"task_id": "task-1", "detail_date": "2026-08-12"},
        )
        leakage = self.adapter.invoke_capability(
            SMARTLIGHT_LEAKAGE_ANALYSIS_CAPABILITY,
            worker,
            {"last_days": 30, "top_n": 5},
        )

        self.assertEqual(cabinets["items"][0]["code"], "CAB-001")
        self.assertTrue(rtu["found"])
        self.assertEqual(rtu["detail"]["code"], "RTU-001")
        self.assertEqual(rtu["relayTotal"], 1)
        self.assertEqual(rtu["relays"][0]["circuitCount"], 3)
        self.assertTrue(lamp_post["found"])
        self.assertEqual(lamp_post["detail"]["height"], 12)
        self.assertEqual(alarms["dateRange"]["source"], "default_last_days")
        self.assertEqual(alarms["analyzedCount"], 1)
        self.assertFalse(alarms["truncated"])
        self.assertEqual(
            alarms["stateCounts"],
            [{"value": "当前告警", "count": 1}],
        )
        self.assertEqual(inspection["dailyCount"], 1)
        self.assertEqual(inspection["days"][0]["plannedDeviceCount"], 10)
        self.assertTrue(inspection["detailDateFound"])
        self.assertEqual(inspection["clockins"][0]["deviceCode"], "LP-001")
        self.assertEqual(leakage["analyzedCount"], 1)
        self.assertEqual(
            leakage["topLampPosts"],
            [{"value": "LP-001", "count": 1}],
        )
        alarm_request = next(
            item
            for item in worker.api_requests
            if item["path"].endswith("/rHisHitchAlarm/getDataByRtuAlarm")
            and "_timebegin_begin" in item["body"]
        )
        alarm_query = json.loads(parse_qs(alarm_request["body"])["json"][0])
        self.assertEqual(alarm_query["dateType"], "1")
        self.assertEqual(alarm_query["_include_conductStatue"], ["131"])

    def test_relative_and_explicit_date_ranges_cannot_be_mixed(self):
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            _resolve_date_range(
                {"last_days": 30, "start_date": "2026-08-01"}
            )

    def test_fourth_phase_reads_use_dedicated_sources_and_bounded_semantics(self):
        worker = FakeSmartlightWorker(authenticated=True)

        energy = self.adapter.invoke_capability(
            SMARTLIGHT_ENERGY_RECORD_LIST_CAPABILITY,
            worker,
            {"start_date": "2026-08-01", "end_date": "2026-08-07"},
        )
        energy_analysis = self.adapter.invoke_capability(
            SMARTLIGHT_ENERGY_ANALYSIS_CAPABILITY,
            worker,
            {"last_days": 30},
        )
        lamp_survey = self.adapter.invoke_capability(
            SMARTLIGHT_LAMP_SURVEY_RECORDS_CAPABILITY,
            worker,
            {
                "start_time": "2026-08-20 08:00",
                "end_time": "2026-08-21 08:00",
            },
        )
        leakage = self.adapter.invoke_capability(
            SMARTLIGHT_RTU_LEAKAGE_ALARM_LIST_CAPABILITY,
            worker,
            {"last_days": 30},
        )
        leakage_analysis = self.adapter.invoke_capability(
            SMARTLIGHT_RTU_LEAKAGE_ANALYSIS_CAPABILITY,
            worker,
            {"last_days": 30},
        )
        off_hours = self.adapter.invoke_capability(
            SMARTLIGHT_OFF_HOURS_CURRENT_LIST_CAPABILITY,
            worker,
            {
                "start_time": "2026-08-20 08:00",
                "end_time": "2026-08-21 08:00",
            },
        )
        inspection_logs = self.adapter.invoke_capability(
            SMARTLIGHT_INSPECTION_LOG_LIST_CAPABILITY,
            worker,
            {"last_days": 30},
        )
        maintenance = self.adapter.invoke_capability(
            SMARTLIGHT_MAINTENANCE_RECORD_LIST_CAPABILITY,
            worker,
            {
                "start_date": "2024-08-01",
                "end_date": "2024-09-30",
            },
        )

        self.assertEqual(energy["sourceKind"], "downstream_daily_energy_matrix")
        self.assertEqual(energy["items"][0]["periodValues"][0]["value"], "1.25")
        self.assertIn("未明确返回单位", energy["warnings"][0])
        self.assertEqual(energy_analysis["totalValue"], 3.5)
        self.assertEqual(energy_analysis["topDevices"][0]["deviceId"], "rtu-1")
        self.assertEqual(lamp_survey["sourceKind"], "single_lamp_telemetry_history")
        self.assertEqual(lamp_survey["items"][0]["voltage"], 220.1)
        self.assertEqual(leakage["sourceKind"], "rtu_branch_leakage_alarm")
        self.assertEqual(leakage["items"][0]["branch"], "支路1")
        self.assertEqual(leakage_analysis["currentUnclearedCount"], 1)
        self.assertEqual(off_hours["sourceKind"], "off_hours_current_observation")
        self.assertIn("不自动判定漏电", off_hours["warnings"][0])
        self.assertEqual(inspection_logs["scope"], "inspection_log_group_summary")
        self.assertEqual(inspection_logs["items"][0]["shouldChecked"], 2)
        self.assertEqual(maintenance["items"][0]["deviceType"], "rtu")
        self.assertFalse(maintenance["items"][0]["stableDetailAvailable"])

        paths = [request["path"] for request in worker.api_requests]
        self.assertIn("/smartlight/rEnergyReport/getDateByEnergy", paths)
        self.assertIn("/smartlight/lHisCoplog/getDataByCondition", paths)
        self.assertIn("/smartlight/rHisHitchAlarm/getDataByRtuLeakageAlarm", paths)
        self.assertNotIn("/smartlight/lHisCoplog/getDataByLampElectricLeakage", paths)

        leakage_request = next(
            request
            for request in worker.api_requests
            if request["path"].endswith("/rHisHitchAlarm/getDataByRtuLeakageAlarm")
        )
        leakage_query = json.loads(parse_qs(leakage_request["body"])["json"][0])
        self.assertEqual(leakage_query["_include_hitchDicIds"], ["htch047"])

    def test_fourth_phase_rejects_unbounded_date_and_time_ranges(self):
        worker = FakeSmartlightWorker(authenticated=True)

        with self.assertRaisesRegex(ValueError, "cannot exceed 92 days"):
            self.adapter.invoke_capability(
                SMARTLIGHT_ENERGY_RECORD_LIST_CAPABILITY,
                worker,
                {"start_date": "2026-01-01", "end_date": "2026-06-01"},
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed 7 days"):
            self.adapter.invoke_capability(
                SMARTLIGHT_LAMP_SURVEY_RECORDS_CAPABILITY,
                worker,
                {
                    "start_time": "2026-08-01 00:00",
                    "end_time": "2026-08-09 00:01",
                },
            )

    def test_report_export_returns_bounded_rows_and_csv_contract_metadata(self):
        worker = FakeSmartlightWorker(authenticated=True)

        alarm_report = self.adapter.invoke_capability(
            SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
            worker,
            {"report_type": "alarm_analysis", "last_days": 30},
        )
        asset_report = self.adapter.invoke_capability(
            SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
            worker,
            {"report_type": "asset_inventory", "asset_type": "rtu"},
        )
        inspection_report = self.adapter.invoke_capability(
            SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
            worker,
            {
                "report_type": "inspection_progress",
                "task_id": "task-1",
                "detail_date": "2026-08-12",
            },
        )
        energy_report = self.adapter.invoke_capability(
            SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
            worker,
            {
                "report_type": "energy_records",
                "start_date": "2026-08-01",
                "end_date": "2026-08-07",
            },
        )
        maintenance_report = self.adapter.invoke_capability(
            SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
            worker,
            {
                "report_type": "maintenance_records",
                "start_date": "2024-08-01",
                "end_date": "2024-09-30",
            },
        )

        self.assertEqual(alarm_report["reportType"], "alarm_analysis")
        self.assertEqual(alarm_report["metadata"]["exportedCount"], 1)
        self.assertEqual(alarm_report["rows"][0]["id"], "alarm-1")
        self.assertEqual(asset_report["reportTitle"], "照明RTU清单")
        self.assertEqual(asset_report["rows"][0]["code"], "RTU-001")
        self.assertEqual(inspection_report["metadata"]["exportedCount"], 1)
        self.assertEqual(inspection_report["rows"][0]["deviceCode"], "LP-001")
        self.assertEqual(energy_report["reportTitle"], "照明RTU用电记录")
        self.assertEqual(energy_report["rows"][0]["date"], "2026-08-01")
        self.assertEqual(maintenance_report["reportTitle"], "照明检修记录")
        self.assertEqual(maintenance_report["rows"][0]["recordId"], "overhaul-1")

    def test_registry_contains_read_and_reversible_write_capabilities(self):
        capabilities = build_smartlight_capability_registry().list()

        self.assertEqual(len(capabilities), 34)
        self.assertEqual(
            sum(spec.effect == "read" for spec in capabilities),
            26,
        )
        self.assertEqual(
            {
                spec.name
                for spec in capabilities
                if spec.effect == "reversible_write"
            },
            {
                SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY,
                SMARTLIGHT_ALARM_REMARK_UPDATE_CAPABILITY,
                SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY,
                SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_CAPABILITY,
                SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY,
                SMARTLIGHT_ALARM_WORK_AREA_REVOKE_CAPABILITY,
            },
        )
        self.assertEqual(
            {
                spec.name
                for spec in capabilities
                if spec.effect == "controlled_write"
            },
            {
                SMARTLIGHT_RTU_ALARM_DISPOSE_PREPARE_CAPABILITY,
                SMARTLIGHT_RTU_ALARM_DISPOSE_CAPABILITY,
            },
        )
        inspection = next(
            spec
            for spec in capabilities
            if spec.name == SMARTLIGHT_INSPECTION_TASK_LIST_CAPABILITY
        )
        self.assertEqual(inspection.version, "0.2.1")
        self.assertEqual(
            inspection.input_schema["properties"]["state"]["type"],
            ["integer", "string", "null"],
        )

    def test_alarm_remark_update_freezes_commits_verifies_and_exposes_rollback(self):
        worker = FakeSmartlightWorker(authenticated=True)
        entered = []

        prepared = prepare_smartlight_alarm_remark_update(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1", "remark": "现场已复核"},
        )
        self.assertEqual(prepared["summary"]["system"], "照明实验室测试系统")
        result = commit_smartlight_alarm_remark_update(
            self.adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: entered.append(True),
        )

        self.assertEqual(entered, [True])
        self.assertEqual(worker.alarm_remark["remark"], "现场已复核")
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["verification"]["matched"])
        self.assertEqual(
            result["rollback"]["arguments"],
            {"alarm_id": "alarm-1", "remark": ""},
        )
        prepared_clear = prepare_smartlight_alarm_remark_update(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1", "remark": ""},
        )
        cleared = commit_smartlight_alarm_remark_update(
            self.adapter,
            worker,
            prepared_clear["plan"],
            enter_commit_boundary=lambda: entered.append("clear"),
        )
        self.assertEqual(cleared["alarm"]["remark"], "")
        self.assertIn("已清除", cleared["effect"])
        self.assertTrue(cleared["verification"]["matched"])
        self.assertEqual(
            capability_required_scopes(
                SMARTLIGHT_ALARM_REMARK_UPDATE_PREPARE_CAPABILITY
            ),
            frozenset({"smartlight:write:alarm_remark"}),
        )
        self.assertEqual(
            capability_required_scopes(SMARTLIGHT_ALARM_REMARK_UPDATE_CAPABILITY),
            frozenset({"smartlight:write:alarm_remark"}),
        )

    def test_alarm_remark_update_stops_when_remark_changed_after_prepare(self):
        worker = FakeSmartlightWorker(authenticated=True)
        prepared = prepare_smartlight_alarm_remark_update(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1", "remark": "拟写入备注"},
        )
        worker.alarm_remark = {
            "hitchAlarmId": "alarm-1",
            "rtuId": "rtu-1",
            "remark": "其他用户已修改",
        }

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "其他操作修改",
        ):
            commit_smartlight_alarm_remark_update(
                self.adapter,
                worker,
                prepared["plan"],
                enter_commit_boundary=lambda: self.fail(
                    "commit boundary must not be entered"
                ),
            )

    def test_work_area_submit_and_revoke_are_frozen_verified_action_pair(self):
        worker = FakeSmartlightWorker(authenticated=True)
        entered = []

        prepared_submit = prepare_smartlight_alarm_work_area_submit(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        submitted = commit_smartlight_alarm_work_area_submit(
            self.adapter,
            worker,
            prepared_submit["plan"],
            enter_commit_boundary=lambda: entered.append("submit"),
        )
        prepared_revoke = prepare_smartlight_alarm_work_area_revoke(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        revoked = commit_smartlight_alarm_work_area_revoke(
            self.adapter,
            worker,
            prepared_revoke["plan"],
            enter_commit_boundary=lambda: entered.append("revoke"),
        )

        self.assertEqual(entered, ["submit", "revoke"])
        self.assertTrue(submitted["verification"]["matched"])
        self.assertEqual(
            submitted["rollback"]["capability"],
            SMARTLIGHT_ALARM_WORK_AREA_REVOKE_PREPARE_CAPABILITY,
        )
        self.assertFalse(revoked["alarm"]["workAreaSubmitted"])
        self.assertEqual(worker.alarm_record["isSubmitWorkArea"], 0)
        self.assertEqual(revoked["rollback"], {
            "available": True,
            "capability": SMARTLIGHT_ALARM_WORK_AREA_SUBMIT_PREPARE_CAPABILITY,
            "arguments": {"alarm_id": "alarm-1"},
        })
        self.assertIn(
            "必须重新生成并确认授权卡", prepared_revoke["summary"]["authorization_notice"],
        )

        action_reads = [
            request
            for request in worker.api_requests
            if request["path"].endswith("/rHisHitchAlarm/getDataByRtuAlarm")
        ]
        self.assertGreaterEqual(len(action_reads), 6)
        for request in action_reads:
            query = parse_qs(request["body"])
            filters = json.loads(query["json"][0])
            self.assertEqual(filters["usageType"], 1)
            self.assertEqual(filters["_include_conductStatue"], ["131", "132"])
            self.assertEqual(filters["_include_isSubmitWorkArea"], [])
            self.assertEqual(filters["weightFacto"], [1, 2, 3, 4, 5, 6])
            self.assertTrue(filters["showData"])
            self.assertEqual(filters["userId"], "user-1")

    def test_work_area_prepare_rejects_alarm_outside_actionable_view(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.hide_alarm_from_actionable_view = True

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "actionable RTU alarm view",
        ):
            prepare_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                {"alarm_id": "alarm-1"},
            )

    def test_work_area_submit_rejects_non_work_area_alarm(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_record["weightFacto"] = 2

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "等级不是工区接收范围",
        ):
            prepare_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                {"alarm_id": "alarm-1"},
            )

    def test_work_area_submit_rejects_blank_work_area_name(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_record["workAreaName"] = ""

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "没有有效所属工区",
        ):
            prepare_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                {"alarm_id": "alarm-1"},
            )

    def test_work_area_revoke_allows_blank_work_area_name_for_recovery(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_record["isSubmitWorkArea"] = 1
        worker.alarm_record["workAreaName"] = ""

        prepared = prepare_smartlight_alarm_work_area_revoke(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )

        self.assertEqual(prepared["plan"]["business_intent"], "revoke_work_area")
        self.assertIsNone(prepared["plan"]["preconditions"]["workAreaName"])

    def test_work_area_submit_stops_when_snapshot_changes_after_prepare(self):
        worker = FakeSmartlightWorker(authenticated=True)
        prepared = prepare_smartlight_alarm_work_area_submit(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        worker.alarm_record["workAreaId"] = "work-area-2"

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "已经变化",
        ):
            commit_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                prepared["plan"],
                enter_commit_boundary=lambda: self.fail(
                    "commit boundary must not be entered"
                ),
            )

    def test_work_area_submit_rejects_unknown_submission_state(self):
        worker = FakeSmartlightWorker(authenticated=True)
        worker.alarm_record["isSubmitWorkArea"] = 9

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "工区提交状态无法识别",
        ):
            prepare_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                {"alarm_id": "alarm-1"},
            )

    def test_work_area_submit_stops_when_rtu_binding_changes_after_prepare(self):
        worker = FakeSmartlightWorker(authenticated=True)
        prepared = prepare_smartlight_alarm_work_area_submit(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        worker.alarm_record["rtuId"] = "rtu-2"
        worker.alarm_record["isSubmitWorkArea"] = 1

        with self.assertRaisesRegex(
            SmartlightBusinessRuleRejected,
            "RTU 已变化",
        ):
            commit_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                prepared["plan"],
                enter_commit_boundary=lambda: self.fail(
                    "commit boundary must not be entered"
                ),
            )

    def test_rtu_alarm_dispose_is_irreversible_and_verified(self):
        worker = FakeSmartlightWorker(authenticated=True)
        entered = []

        prepared = prepare_smartlight_rtu_alarm_dispose(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        result = commit_smartlight_rtu_alarm_dispose(
            self.adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: entered.append(True),
        )

        self.assertEqual(entered, [True])
        self.assertEqual(result["alarm"]["alarmState"], 3)
        self.assertFalse(result["rollback"]["available"])
        self.assertIn(
            "确认后不能由 AgentBridge 恢复", prepared["summary"]["authorization_notice"],
        )
        self.assertEqual(worker.alarm_record["conductStatue"], 3)
        self.assertEqual(
            capability_required_scopes(SMARTLIGHT_RTU_ALARM_DISPOSE_CAPABILITY),
            frozenset({"smartlight:write:alarm_disposition"}),
        )

    def test_alarm_actions_reject_changed_targets_before_commit_boundary(self):
        cases = (
            ("submit", prepare_smartlight_alarm_work_area_submit,
             commit_smartlight_alarm_work_area_submit, "conductStatue", 1),
            ("revoke", prepare_smartlight_alarm_work_area_revoke,
             commit_smartlight_alarm_work_area_revoke, "rtuId", "rtu-2"),
            ("revoke", prepare_smartlight_alarm_work_area_revoke,
             commit_smartlight_alarm_work_area_revoke, "workAreaId", "work-area-2"),
            ("revoke", prepare_smartlight_alarm_work_area_revoke,
             commit_smartlight_alarm_work_area_revoke, "conductStatue", 1),
            ("dispose", prepare_smartlight_rtu_alarm_dispose,
             commit_smartlight_rtu_alarm_dispose, "rtuId", "rtu-2"),
            ("dispose", prepare_smartlight_rtu_alarm_dispose,
             commit_smartlight_rtu_alarm_dispose, "workAreaId", "work-area-2"),
            ("dispose", prepare_smartlight_rtu_alarm_dispose,
             commit_smartlight_rtu_alarm_dispose, "conductStatue", 1),
        )
        for action, prepare, commit, field, value in cases:
            with self.subTest(action=action, changed=field):
                worker = FakeSmartlightWorker(authenticated=True)
                if action == "revoke":
                    worker.alarm_record["isSubmitWorkArea"] = 1
                prepared = prepare(self.adapter, worker, {"alarm_id": "alarm-1"})
                worker.alarm_record[field] = value
                if field == "rtuId":
                    # Reaching the desired state cannot override a changed RTU binding.
                    if action == "revoke":
                        worker.alarm_record["isSubmitWorkArea"] = 0
                    else:
                        worker.alarm_record["conductStatue"] = 3
                expected = "RTU 已变化" if field == "rtuId" else "已经变化"
                with self.assertRaisesRegex(SmartlightBusinessRuleRejected, expected):
                    commit(
                        self.adapter, worker, prepared["plan"],
                        enter_commit_boundary=lambda: self.fail(
                            "changed targets must not enter the commit boundary"
                        ),
                    )
                self.assertFalse(worker.alarm_action_performed)
                self.assertFalse(any(
                    request["path"].endswith((
                        "/rHisHitchAlarm/updateIsSubmitWorkArea",
                        "/rHisHitchAlarm/cancleSubmitWorkArea",
                        "/rHisHitchAlarm/setRtuConductStatusDisposed",
                    )) for request in worker.api_requests
                ))

    def test_alarm_action_timeout_after_commit_boundary_is_outcome_unknown(self):
        worker = FakeSmartlightWorker(authenticated=True)
        prepared = prepare_smartlight_alarm_work_area_submit(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        worker.alarm_action_request_error = ConnectionError("timed out")
        entered = []

        with self.assertRaisesRegex(
            SmartlightAlarmActionOutcomeUnknown,
            "最终结果无法确认",
        ):
            commit_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                prepared["plan"],
                enter_commit_boundary=lambda: entered.append(True),
            )
        self.assertEqual(entered, [True])

    def test_alarm_action_readback_failure_is_outcome_unknown(self):
        worker = FakeSmartlightWorker(authenticated=True)
        prepared = prepare_smartlight_alarm_work_area_submit(
            self.adapter,
            worker,
            {"alarm_id": "alarm-1"},
        )
        worker.fail_alarm_readback_after_action = True
        entered = []

        with self.assertRaisesRegex(
            SmartlightAlarmActionOutcomeUnknown,
            "权威回读失败",
        ):
            commit_smartlight_alarm_work_area_submit(
                self.adapter,
                worker,
                prepared["plan"],
                enter_commit_boundary=lambda: entered.append(True),
            )
        self.assertEqual(entered, [True])


class FakePage:
    def content(self) -> str:
        return """
        <form id="auth-form" action="/cas/login;jsessionid=test?service=x">
          <input type="hidden" name="lt" value="LT-test">
          <input type="hidden" name="execution" value="exec-test">
          <input type="hidden" name="_eventId" value="submit">
          <img id="captchaImage" src="captcha.jpg">
        </form>
        """


def _unsigned_jwt(*, exp: int) -> str:
    def encode(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': exp})}.signature"


class FakeSmartlightWorker:
    def __init__(
        self,
        *,
        authenticated: bool = False,
        login_rejection: str | None = None,
    ) -> None:
        self.state = {}
        self.page_url = ""
        self.cas_submission = ""
        self.cas_headers: dict = {}
        self.captcha_headers: dict = {}
        self.api_headers: list[dict] = []
        self.api_requests: list[dict] = []
        self.login_rejection = login_rejection
        self.login_completed = authenticated
        self.reject_refresh_token = False
        self.refresh_expired_payload = False
        self.refresh_temporarily_unavailable = False
        self.rotated_refresh_token = ""
        self.empty_principal = False
        self.principal_requires_cas_renewal = False
        self.cas_renewal_requests = 0
        self.refresh_requests = 0
        self.principal_requests = 0
        self.token_exchange_requests = 0
        self.alarm_remark: dict | None = None
        self.saved_alarm_payloads: list[dict] = []
        self.alarm_action_request_error: Exception | None = None
        self.fail_alarm_readback_after_action = False
        self.alarm_action_performed = False
        self.hide_alarm_from_actionable_view = False
        self.alarm_records: list[dict] | None = None
        self.alarm_record = {
            "hitchAlarmId": "alarm-1",
            "hitchName": "电源缺相",
            "hitchIntro": "电源缺相(B,C)",
            "occurDate": "2026-08-10 10:05:38",
            "lastDate": "2026-08-12 10:30:02",
            "rtuCode": "05312222",
            "rtuId": "rtu-1",
            "rtuName": "实验室控制柜",
            "weightFacto": 3,
            "conductStatue": 0,
            "workAreaId": "work-area-1",
            "workAreaName": "实验室工区",
            "isSubmitWorkArea": 0,
            "groupName": "RTU分组测试",
        }
        if authenticated:
            self.state = {
                "access_token": "jwt-access",
                "refresh_token": "jwt-refresh",
                "principal": _safe_principal(),
            }

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def capture_session_state(self) -> dict:
        return {"cookies": [], "http": deepcopy(self.state)}

    def restore_session_state(self, state: dict) -> None:
        self.state = deepcopy(state["http"])

    def goto(self, url: str, *, timeout_seconds: float = 30):
        del url, timeout_seconds
        self.page_url = "http://123.232.113.241:4101/cas/login?service=test"
        return FakePage()

    def request_bytes(self, method: str, url: str, **kwargs) -> dict:
        path = urlparse(url).path
        if method == "GET" and path == "/smartlight/" and (
            not self.login_completed or self.empty_principal
        ):
            return {
                "status": 302,
                "url": url,
                "content_type": "text/html",
                "body": b"",
                "location": "/cas/login?service=http%3A%2F%2Ftest%2Fsmartlight%2F",
            }
        if method == "GET" and path == "/cas/login":
            return {
                "status": 200,
                "url": url,
                "content_type": "text/html",
                "body": FakePage().content().encode("utf-8"),
                "location": None,
            }
        if method == "GET" and path.endswith("/cas/captcha.jpg"):
            self.captcha_headers = dict(kwargs.get("headers") or {})
            return {
                "status": 200,
                "url": url,
                "content_type": "image/jpeg",
                "body": b"jpeg-captcha",
                "location": None,
            }
        if method == "POST" and path.startswith("/cas/login"):
            self.cas_submission = kwargs["body"]
            self.cas_headers = dict(kwargs.get("headers") or {})
            if self.login_rejection is not None:
                body = f"""
                <form id="auth-form">
                  <div class="controls login-error-info">
                    {self.login_rejection}
                  </div>
                </form>
                """.encode("utf-8")
                return {
                    "status": 200,
                    "url": url,
                    "content_type": "text/html; charset=UTF-8",
                    "body": body,
                    "location": None,
                }
            self.login_completed = True
            return {
                "status": 302,
                "url": url,
                "content_type": "text/html",
                "body": b"",
                "location": "/smartlight/?ticket=ST-test",
            }
        if method == "GET" and path == "/smartlight/":
            self.cas_renewal_requests += 1
            self.principal_requires_cas_renewal = False
            return {
                "status": 200,
                "url": url,
                "content_type": "text/html",
                "body": b"app",
                "location": None,
            }
        raise AssertionError(f"unexpected bytes request: {method} {url}")

    def request(self, method: str, url: str, **kwargs) -> dict:
        self.assert_post(method)
        path = urlparse(url).path
        if path == "/smartlight/userInfo/getCasLoginUser":
            self.principal_requests += 1
            if self.empty_principal or self.principal_requires_cas_renewal:
                return _response(
                    {
                        "dlzh": None,
                        "userName": None,
                        "dlmm": None,
                        "organroleId": None,
                        "yhid": None,
                    }
                )
            return _response(
                {
                    "dlzh": "yanshi",
                    "userName": "无为",
                    "dlmm": "D" * 96,
                    "organId": "org-1",
                    "organroleId": "role-1",
                    "organroleName": "wuwei",
                    "ryid": "person-1",
                    "yhid": "user-1",
                }
            )
        if path == "/jwtcenter//JWTInfoController/getToken":
            self.token_exchange_requests += 1
            return _response(
                {
                    "resp_code": 0,
                    "resp_data": {
                        "access_token": "jwt-access",
                        "refresh_token": "jwt-refresh",
                        "access_token_duration": 3600,
                    },
                }
            )
        if path == "/jwtcenter//JWTInfoController/refreshToken":
            self.refresh_requests += 1
            if self.reject_refresh_token:
                response = _response({"resp_code": 1001, "resp_data": None})
                response["status"] = 401
                return response
            if self.refresh_expired_payload:
                return _response(
                    {
                        "resp_code": 1009,
                        "resp_msg": "refresh_token已失效,请重新登录以取得新的Token.",
                    }
                )
            if self.refresh_temporarily_unavailable:
                response = _response({"message": "temporarily unavailable"})
                response["status"] = 503
                return response
            return _response(
                {
                    "resp_code": 1000,
                    "resp_data": {
                        "access_token": "jwt-access-refreshed",
                        "refresh_token": self.rotated_refresh_token or None,
                        "access_token_duration": 1800,
                    },
                }
            )
        self.api_headers.append(dict(kwargs.get("headers") or {}))
        self.api_requests.append(
            {"path": path, "body": str(kwargs.get("body") or "")}
        )
        if path.endswith("/map/getIntegratedRControlCabinetDataByCondition"):
            return _response(
                [
                    {
                        "controlCabinetId": "cab-1",
                        "controlCabinetCode": "CAB-001",
                        "controlCabinetName": "一号箱变",
                        "onlineState": "online",
                    }
                ]
            )
        if path.endswith("/rRtu/getRtuStateCountDataByConditionNew"):
            return _response(
                {
                    "rAllCount": "29",
                    "rOnlineCount": "0",
                    "rNoOnlineWithNoHandleCount": "27",
                    "rPowerOutageCount": "1",
                    "rDisableCount": "1",
                    "rOnlineWithEleCount": "0",
                    "rOnlineWithOutEleCount": "0",
                    "rOnlineNoCalCount": "0",
                }
            )
        if path.endswith("/lLamppost/newGetTotalStatus"):
            return _response(
                {
                    "AllControlCount": 118,
                    "OnlineCount": 0,
                    "OfflineCount": 118,
                    "AlonelampCount": 178,
                    "OpenLampCount": 0,
                    "CloseLampCount": 0,
                    "LampPostCount": 116,
                    "SingleLampPostCount": 56,
                    "DoubleLampPostCount": 57,
                    "TribleLampPostCount": 2,
                    "OtherLampPostCount": 3,
                    "alarmLampPostCount": 0,
                }
            )
        if path.endswith("/lLamppost/getLampDetialList"):
            return _response(
                {
                    "list": [
                        {
                            "LampPostID": "lp-runtime-1",
                            "LampPostCode": "LP-RUNTIME-001",
                            "LampPostType": "单臂灯",
                            "StreetName": "测试路",
                            "StreetSideName": "北侧",
                            "WorkAreaName": "实验室工区",
                            "controlCabinetCode": "CAB-001",
                            "controlCabinetName": "一号箱变",
                            "rtuCode": "RTU-001",
                            "rtuName": "一号 RTU",
                            "AloneLamps": [
                                {
                                    "AloneLampId": "lamp-1",
                                    "aloneLampControlId": "controller-1",
                                    "LampNumber": 1,
                                    "LampCode": "1",
                                    "Effect": "主道灯",
                                    "IsOnline": False,
                                    "IsSwitchOn": False,
                                    "copDate": "2026-08-10 10:00:00",
                                    "U": 0,
                                    "I": 0,
                                    "Pf": 0,
                                    "Ap": 0,
                                    "hitchAlarms": None,
                                }
                            ],
                        }
                    ],
                    "totalCount": 116,
                }
            )
        if path.endswith("/lLamppost/getDataByConditionForFacilityEx"):
            return _response(
                {
                    "list": [
                        {
                            "lampPostID": "lp-1",
                            "LampPostCode": "LP-001",
                            "StreetName": "测试路",
                        }
                    ],
                    "totalCount": 131,
                }
            )
        if path.endswith("/lLamppost/getLampPostDetail"):
            return _response(
                {
                    "code": "200",
                    "result": {
                        "lampPostId": "lp-1",
                        "lampPostCode": "LP-001",
                        "lampPostTypeName": "十二米杆",
                        "lampPostHeight": 12,
                        "streetName": "测试路",
                        "controlCabinetName": "一号箱变",
                        "rtuName": "一号 RTU",
                        "workAreaName": "实验室工区",
                    },
                }
            )
        if path.endswith("/rControlCabinet/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "controlCabinetId": "cab-1",
                            "controlCabinetCode": "CAB-001",
                            "controlCabinetName": "一号箱变",
                            "capacityStr": "100kVA",
                            "workAreaName": "实验室工区",
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rRtu/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "rtuId": "rtu-1",
                            "rtuCode": "RTU-001",
                            "rtuName": "一号 RTU",
                            "productModel": "RTU-X",
                            "controlCabinetName": "一号箱变",
                            "groupName": "测试组",
                            "runningStateName": "在线",
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rRtu/getRtusByConditionNew"):
            return _response(
                {
                    "list": [
                        {
                            "rtuId": "rtu-1",
                            "rtuCode": "RTU-001",
                            "rtuName": "一号 RTU",
                            "rtuProductModelId": "model-1",
                            "rtuProductModelName": "RTU-X",
                            "controlCabinetId": "cab-1",
                            "controlCabinetName": "一号箱变",
                            "workAreaId": "work-area-1",
                            "workAreaName": "实验室工区",
                            "rtuGroupId": "group-1",
                            "rtuGroupName": "测试组",
                            "rtuRunningState": 4,
                            "lastOnlineTime": "2026-08-10 09:59:00",
                            "coplogTime": "2026-08-10 10:00:00",
                            "isAlarm": 1,
                            "isOpen": 0,
                            "isEnabled": 1,
                            "workModels": ["全夜灯"],
                            "coplogPhase": {
                                "strRtuScaleU1": 220.1,
                                "strRtuScaleU2": 220.2,
                                "strRtuScaleU3": 220.3,
                                "strRtuScaleIsp1": 1.1,
                                "strRtuScaleIsp2": 1.2,
                                "strRtuScaleIsp3": 1.3,
                                "temperature": 25.5,
                                "humidity": 48,
                                "relayLeakCurrents": [0.02, 0.01],
                            },
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rRturelay/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "rturelayId": "relay-1",
                            "rturelayNumber": 1,
                            "rturelayName": "1",
                            "workModelName": "全夜灯",
                            "isEnabled": 1,
                            "roadSwitchList": [
                                {
                                    "roadSwitchId": "switch-1",
                                    "roadSwitchNumber": 1,
                                    "rRturoadList": [
                                        {"rturoadId": "road-a", "rturoadNumber": 1, "powerType": "A"},
                                        {"rturoadId": "road-b", "rturoadNumber": 2, "powerType": "B"},
                                        {"rturoadId": "road-c", "rturoadNumber": 3, "powerType": "C"},
                                    ],
                                }
                            ],
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rHisHitchAlarm/getDataByRtuAlarm"):
            if self.fail_alarm_readback_after_action and self.alarm_action_performed:
                raise ConnectionError("readback failed")
            query = parse_qs(str(kwargs.get("body") or ""))
            filters_json = query.get("json", ["{}"])[0]
            filters = json.loads(filters_json)
            if (
                self.hide_alarm_from_actionable_view
                and filters.get("showData") is True
            ):
                return _response(
                    {
                        "RtuHisHitchAlarm": {"list": [], "totalCount": 0},
                        "todayAlarm": 0,
                        "untreated": 0,
                        "yesterdayAlarm": 0,
                    }
                )
            records = deepcopy(
                self.alarm_records
                if self.alarm_records is not None
                else [self.alarm_record]
            )
            sort_field = "lastDate" if filters.get("dateType") == "1" else "occurDate"
            records.sort(
                key=lambda item: str(item.get(sort_field) or ""),
                reverse=True,
            )
            page_num = int(query.get("pageNum", [1])[0])
            page_size = int(query.get("pageSize", [20])[0])
            start = (page_num - 1) * page_size
            page_records = records[start : start + page_size]
            return _response(
                {
                    "RtuHisHitchAlarm": {
                        "list": page_records,
                        "totalCount": len(records),
                    },
                    "todayAlarm": 3,
                    "untreated": 2,
                    "yesterdayAlarm": 1,
                }
            )
        if path.endswith("/rHisHitchAlarm/getRtuAlarmRemark"):
            return _response(deepcopy(self.alarm_remark))
        if path.endswith("/rHisHitchAlarm/saveRtuAlarmRemark"):
            payload = json.loads(parse_qs(str(kwargs.get("body") or ""))["json"][0])
            self.saved_alarm_payloads.append(deepcopy(payload))
            self.alarm_remark = deepcopy(payload)
            return _response({"code": 200, "message": "保存成功"})
        if path.endswith("/rHisHitchAlarm/updateIsSubmitWorkArea"):
            if self.alarm_action_request_error is not None:
                raise self.alarm_action_request_error
            self.alarm_record["isSubmitWorkArea"] = 1
            self.alarm_action_performed = True
            return _response(1)
        if path.endswith("/rHisHitchAlarm/cancleSubmitWorkArea"):
            if self.alarm_action_request_error is not None:
                raise self.alarm_action_request_error
            self.alarm_record["isSubmitWorkArea"] = 0
            self.alarm_action_performed = True
            return _response(1)
        if path.endswith("/rHisHitchAlarm/setRtuConductStatusDisposed"):
            if self.alarm_action_request_error is not None:
                raise self.alarm_action_request_error
            self.alarm_record["conductStatue"] = 3
            self.alarm_action_performed = True
            return _response(1)
        if path.endswith("/inspectionTask/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "taskId": "task-1",
                            "taskName": "夜巡一组",
                            "planName": "夜巡计划",
                            "groupName": "一号巡检组",
                            "taskStartDate": "2026-08-01",
                            "taskDeadline": "2026-08-31",
                            "taskState": 2,
                            "taskProgress": "25.00%",
                            "confirmDeviceNum": 4,
                            "lampostQty": 12,
                            "rtuQty": 2,
                        }
                    ],
                    "totalCount": 1,
                }
            )
        if path.endswith("/InspectionDeviceGroup/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "taskId": "task-1",
                            "groupId": "group-day-1",
                            "dateTimeStr": "2026-08-12",
                            "deviceNum": 10,
                            "realityNum": 4,
                            "rate": "40.00%",
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/inspectionTask/getClockinDataByTaskId"):
            return _response(
                {
                    "list": [
                        {
                            "clockinId": "clock-1",
                            "clockinTime": "2026-08-12 20:00:00",
                            "clockinUserName": "巡检员",
                            "deviceCode": "LP-001",
                            "deviceTypeName": "灯杆",
                            "hasIssues": 0,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rHisCoplogPhase/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "hisCoplogPhaseId": "survey-1",
                            "rtuId": "rtu-1",
                            "rtuCode": "RTU-001",
                            "rtuName": "一号 RTU",
                            "addDateTime": "2026-08-10 10:00:00",
                            "rtuTime": "2026-08-10 09:59:58",
                            "rtuScaleU1": 220.1,
                            "rtuScaleU2": 220.2,
                            "rtuScaleU3": 220.3,
                            "strRtuScaleIsp1": 1.1,
                            "strRtuScaleIsp2": 1.2,
                            "strRtuScaleIsp3": 1.3,
                            "iaIan": "1.1/20",
                            "ibIbn": "1.2/20",
                            "icIcn": "1.3/20",
                            "APowerFactor": 0.91,
                            "BPowerFactor": 0.92,
                            "CPowerFactor": 0.93,
                            "temperature": 25.5,
                            "humidity": 48,
                            "relayLeakCurrents": [0.02, 0.01],
                            "jsonRoadIsp": {"1": "1.1/3"},
                            "roadInA": "1,4",
                            "roadInB": "2,5",
                            "roadInC": "3,6",
                            "onRelayIds": [1],
                            "offRelayIds": [2],
                            "isSucceeded": 1,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rEnergyReport/getDateByEnergy"):
            return _response(
                {
                    "list": [
                        {
                            "ID": "rtu-1",
                            "name": "一号 RTU",
                            "productModel": "RTU-X",
                            "item": {
                                "2026-08-01": "1.25",
                                "2026-08-02": "2.25",
                                "2026-08-03": "--",
                            },
                            "total": "3.50",
                            "avg": "1.75",
                            "rtuEmpty": False,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/lHisCoplog/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "hisCoplogId": "lamp-survey-1",
                            "lampPostId": "lp-1",
                            "lampPostCode": "LP-001",
                            "lampEffectName": "主道灯",
                            "streetName": "测试路",
                            "controlCabinetName": "一号箱变",
                            "copDate": "2026-08-20 20:00:00",
                            "deviceTime": "2026-08-20 19:59:58",
                            "IsOnline": True,
                            "IsSwitchOn": True,
                            "U": 220.1,
                            "I": 1.2,
                            "Ap": 260.0,
                            "Pf": 0.95,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/rHisHitchAlarm/getDataByRtuLeakageAlarm"):
            return _response(
                {
                    "RtuHisHitchAlarm": {
                        "list": [
                            {
                                "hitchAlarmId": "leak-1",
                                "rtuId": "rtu-1",
                                "rtuCode": "RTU-001",
                                "rtuName": "一号 RTU",
                                "controlCabinetName": "一号箱变",
                                "relayId": "relay-1",
                                "relayName": "支路1",
                                "leakageCurrent": "0.15",
                                "occurDate": "2026-08-20 20:00:00",
                                "lastDate": "2026-08-20 20:05:00",
                                "conductStatue": 131,
                                "hitchName": "支路漏电报警",
                                "isSubmitWorkArea": 0,
                            }
                        ],
                        "total": 1,
                    },
                    "todayAlarm": 1,
                    "yesterdayAlarm": 0,
                    "untreated": 1,
                }
            )
        if path.endswith("/rOnoffTime/getOpenCloseTime"):
            return _response({"openLightTime": "18:30", "closeLightTime": "06:00"})
        if path.endswith("/rHisCoplogPhase/getOffRelayLeakCurrent"):
            return _response(
                {
                    "list": [
                        {
                            "hisCoplogPhaseId": "off-current-1",
                            "rtuId": "rtu-1",
                            "rtuCode": "RTU-001",
                            "rtuName": "一号 RTU",
                            "relayId": "relay-1",
                            "relayName": "支路1",
                            "addDateTime": "2026-08-20 08:30:00",
                            "avgCurrent": "0.12",
                            "maxCurrent": "0.18",
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/inspectionLog/getInspectionLog"):
            return _response(
                [
                    {
                        "groupId": "group-1",
                        "groupName": "巡检一组",
                        "shouldChecked": 2,
                        "reality": 1,
                        "normal": 1,
                        "abnormal": 0,
                        "oneLevel": 0,
                        "twoLevel": 0,
                        "threeLevel": 0,
                    }
                ]
            )
        if path.endswith("/inspectionOverhaul/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "id": "overhaul-1",
                            "deviceId": "rtu-1",
                            "deviceCode": "RTU-001",
                            "deviceType": 1,
                            "overhaulTimeStr": "2024-09-02 16:51:53",
                            "overhaulUserId": "admin",
                            "overhaulUserName": "管理员",
                            "streetName": "测试路",
                            "wgs84xTude": 117.0,
                            "wgs84yTude": 36.6,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/lHisHitchAlarm/getDataByCondition"):
            return _response(
                {
                    "list": [
                        {
                            "hisHitchAlarmId": "lamp-alarm-1",
                            "alarmAddDate": "2026-08-10 10:00:00",
                            "lastDate": "2026-08-10 10:05:00",
                            "lampPostCode": "LP-001",
                            "lampEffectName": "主道灯",
                            "streetName": "测试路",
                            "controlCabinetName": "一号箱变",
                            "workAreaName": "实验室工区",
                            "hitchDicName": "异常亮灯",
                            "alarmState": 1,
                        }
                    ],
                    "total": 1,
                }
            )
        if path.endswith("/lHisHitchAlarm/getCountDataByCondition"):
            return _response({"todayAlarm": 1, "untreated": 1, "yesterdayAlarm": 0})
        raise AssertionError(f"unexpected API request: {method} {url}")

    def get_http_state(self) -> dict:
        return deepcopy(self.state)

    def set_http_state(self, value: dict) -> None:
        self.state = deepcopy(value)

    @staticmethod
    def assert_post(method: str) -> None:
        if method != "POST":
            raise AssertionError(f"unexpected method: {method}")


def _safe_principal() -> dict:
    return {
        "account": "yanshi",
        "name": "无为",
        "organId": "org-1",
        "organroleId": "role-1",
        "organroleName": "wuwei",
        "personId": "person-1",
        "userId": "user-1",
    }


def _response(payload) -> dict:
    return {
        "status": 200,
        "url": "http://123.232.113.241:4101/smartlight/api",
        "content_type": "application/json",
        "json": payload,
        "text": "",
        "elapsed_ms": 1,
    }


if __name__ == "__main__":
    unittest.main()
