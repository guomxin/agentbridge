from __future__ import annotations

from tests.authorization_fixtures import authorized_service
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlparse

from agentbridge.adapters.taihua import (
    TAIHUA_MY_LOGS_CAPABILITY,
    TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA,
    TaihuaBusinessRuleRejected,
    TaihuaCentralAdapter,
    TaihuaLoginRequired,
    TaihuaSessionCheckUnavailable,
    TaihuaQueryFilterUnverified,
    TaihuaWorkLogOutcomeUnknown,
    commit_taihua_work_log_create,
    prepare_taihua_work_log_create,
)
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.core.central_service import (
    CentralCapabilityService,
    capability_required_scopes,
)
from agentbridge.core.field_submissions import FieldSubmissionStore


class TaihuaCentralAdapterTests(unittest.TestCase):
    def test_work_log_write_capabilities_have_taihua_scope(self):
        self.assertEqual(
            capability_required_scopes("taihua.work_log.create.prepare"),
            frozenset({"taihua:write:worklog"}),
        )
        self.assertEqual(
            capability_required_scopes("taihua.work_log.create"),
            frozenset({"taihua:write:worklog"}),
        )
    def test_authenticate_and_refresh_are_api_first(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/authenticates/basic",
                    response(
                        200,
                        {
                            "token": "access-1",
                            "refreshToken": "refresh-1",
                            "tokenExpired": "2099-07-26T14:00:00Z",
                            "refreshTokenExpired": "2099-08-26T14:00:00Z",
                        },
                    ),
                ),
                expected(
                    "GET",
                    "/api/users/principal",
                    response(
                        200,
                        {
                            "id": 7,
                            "username": "xingm",
                            "fullname": "辛国茂",
                            "roles": [{"name": "EMPLOYEE"}],
                        },
                    ),
                ),
            ]
        )

        authenticated = adapter.authenticate(
            worker,
            {"username": "xingm", "password": "secret"},
            timeout_seconds=15,
        )

        self.assertEqual(authenticated["observed_principal_ref"], "辛国茂")
        self.assertEqual(worker.get_http_state()["authorization"], "Bearer access-1")
        self.assertNotIn("secret", repr(worker.get_http_state()))
        self.assertEqual(
            worker.calls[0]["headers"]["X-Sisyphus-Client"],
            "pc-web",
        )

        worker.responses.extend(
            [
                expected(
                    "GET",
                    "/api/users/principal",
                    response(401, {"code": "A0230", "message": "expired"}),
                ),
                expected(
                    "POST",
                    "/api/authenticates/refresh",
                    response(
                        200,
                        {
                            "token": "access-2",
                            "refreshToken": "refresh-2",
                            "tokenExpired": "2099-07-26T15:00:00Z",
                            "refreshTokenExpired": "2099-08-26T15:00:00Z",
                        },
                    ),
                ),
                expected(
                    "GET",
                    "/api/users/principal",
                    response(
                        200,
                        {
                            "id": 7,
                            "username": "xingm",
                            "fullname": "辛国茂",
                        },
                    ),
                ),
            ]
        )

        probe = adapter.probe_session(worker)

        self.assertTrue(probe["authenticated"])
        self.assertEqual(worker.get_http_state()["authorization"], "Bearer access-2")
        self.assertNotIn("browser_bridge_used", probe)
        self.assertTrue(
            all(
                call["headers"]["X-Sisyphus-Client"] == "pc-web"
                for call in worker.calls
            )
        )

    def test_access_token_is_refreshed_before_the_protected_request(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/authenticates/refresh",
                    response(
                        200,
                        {
                            "token": "access-2",
                            "refreshToken": "refresh-2",
                            "tokenExpired": "2099-07-26T15:00:00Z",
                            "refreshTokenExpired": "2099-08-26T15:00:00Z",
                        },
                    ),
                ),
                expected(
                    "GET",
                    "/api/users/principal",
                    response(
                        200,
                        {
                            "id": 7,
                            "username": "xingm",
                            "fullname": "辛国茂",
                        },
                    ),
                ),
            ],
            state={
                "authorization": "Bearer access-1",
                "refresh_token": "refresh-1",
                "token_expired_at": (
                    datetime.now() + timedelta(minutes=5)
                ).isoformat(sep=" ", timespec="seconds"),
            },
        )

        probe = adapter.probe_session(worker)

        self.assertTrue(probe["authenticated"])
        self.assertEqual(
            [call["path"] for call in worker.calls],
            ["/api/authenticates/refresh", "/api/users/principal"],
        )
        self.assertEqual(
            worker.calls[1]["headers"]["Authorization"],
            "Bearer access-2",
        )
        self.assertEqual(worker.get_http_state()["refresh_token"], "refresh-2")

    def test_access_token_outside_refresh_window_is_reused(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "GET",
                    "/api/users/principal",
                    response(
                        200,
                        {
                            "id": 7,
                            "username": "xingm",
                            "fullname": "辛国茂",
                        },
                    ),
                )
            ],
            state={
                "authorization": "Bearer access-1",
                "refresh_token": "refresh-1",
                "token_expired_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=30)
                ).isoformat(),
            },
        )

        adapter.probe_session(worker)

        self.assertEqual(
            [call["path"] for call in worker.calls],
            ["/api/users/principal"],
        )
        self.assertEqual(
            worker.calls[0]["headers"]["Authorization"],
            "Bearer access-1",
        )

    def test_rejected_proactive_refresh_uses_still_valid_access_token(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/authenticates/refresh",
                    response(401, {"message": "refresh token rejected"}),
                ),
                expected(
                    "GET",
                    "/api/users/principal",
                    response(
                        200,
                        {
                            "id": 7,
                            "username": "xingm",
                            "fullname": "辛国茂",
                        },
                    ),
                ),
            ],
            state={
                "authorization": "Bearer access-1",
                "refresh_token": "refresh-1",
                "token_expired_at": (
                    datetime.now() + timedelta(minutes=5)
                ).isoformat(sep=" ", timespec="seconds"),
            },
        )

        probe = adapter.probe_session(worker)

        self.assertTrue(probe["authenticated"])
        self.assertEqual(
            worker.calls[1]["headers"]["Authorization"],
            "Bearer access-1",
        )
        self.assertEqual(worker.get_http_state()["authorization"], "Bearer access-1")

    def test_rejected_proactive_refresh_reports_detail_if_access_token_is_invalid(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/authenticates/refresh",
                    response(401, {"message": "refresh token rejected"}),
                ),
                expected(
                    "GET",
                    "/api/users/principal",
                    response(401, {"message": "access token rejected"}),
                ),
            ],
            state={
                "authorization": "Bearer access-1",
                "refresh_token": "refresh-1",
                "token_expired_at": (
                    datetime.now() + timedelta(minutes=5)
                ).isoformat(sep=" ", timespec="seconds"),
            },
        )

        with self.assertRaisesRegex(
            TaihuaLoginRequired,
            "HTTP 401.*refresh token rejected",
        ):
            adapter.probe_session(worker)

        self.assertEqual(worker.get_http_state()["authorization"], "Bearer access-1")
    def test_transient_refresh_failure_preserves_existing_session_state(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "GET",
                    "/api/users/principal",
                    response(401, {"code": "A0230", "message": "expired"}),
                ),
                expected(
                    "POST",
                    "/api/authenticates/refresh",
                    response(503, {"message": "temporarily unavailable"}),
                ),
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )

        with self.assertRaises(TaihuaSessionCheckUnavailable):
            adapter.probe_session(worker)

        self.assertEqual(worker.get_http_state()["authorization"], "Bearer access")
        self.assertEqual(worker.get_http_state()["refresh_token"], "refresh")

    def test_authenticate_reports_server_failure_as_temporarily_unavailable(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/authenticates/basic",
                    response(503, {"message": "temporarily unavailable"}),
                )
            ]
        )

        with self.assertRaisesRegex(
            TaihuaSessionCheckUnavailable,
            "temporarily unavailable",
        ):
            adapter.authenticate(
                worker,
                {"username": "xingm", "password": "secret"},
                timeout_seconds=15,
            )

    def test_read_capabilities_use_observed_api_contracts(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "GET",
                    "/api/work-logs/range",
                    response(
                        200,
                        [
                            {
                                "id": 11,
                                "logDate": "2026-07-25",
                                "hours": 8,
                                "content": "系统适配",
                                "projectName": "AgentBridge",
                            }
                        ],
                    ),
                ),
                expected(
                    "GET",
                    "/api/work-logs/team",
                    response(
                        200,
                        {
                            "content": [
                                {
                                    "id": 12,
                                    "logDate": "2026-07-25",
                                    "hours": 7.5,
                                    "content": "联调",
                                    "fullname": "李世玉",
                                }
                            ],
                            "totalElements": 1,
                        },
                    ),
                ),
                expected(
                    "GET",
                    "/api/projects",
                    response(
                        200,
                        [{"id": 9, "code": "AB-01", "name": "AgentBridge"}],
                    ),
                ),
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )

        mine = adapter.list_my_logs(
            worker,
            {"start_date": "2026-07-25", "end_date": "2026-07-25"},
        )
        team = adapter.list_team_logs(
            worker,
            {"page": 1, "size": 10, "view_mode": "submittedAt"},
        )
        projects = adapter.search_projects(worker, {"keyword": "Agent", "limit": 20})

        self.assertEqual(mine["items"][0]["content"], "系统适配")
        self.assertEqual(team["total"], 1)
        self.assertEqual(team["items"][0]["fullname"], "李世玉")
        self.assertEqual(projects["items"][0]["code"], "AB-01")
        team_query = worker.calls[1]["query"]
        self.assertEqual(team_query["page"], ["1"])
        self.assertEqual(team_query["viewMode"], ["submittedAt"])

    def test_team_logs_resolve_member_and_apply_date_range(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "GET",
                    "/api/work-logs/team/member-options",
                    response(
                        200,
                        [
                            {
                                "userId": 300000881,
                                "fullname": "刘大扬",
                                "username": "liudayang",
                                "deptId": 300000101,
                                "deptName": "山东泰华照明科技有限公司",
                            }
                        ],
                    ),
                ),
                expected(
                    "GET",
                    "/api/work-logs/team",
                    response(
                        200,
                        {
                            "content": [
                                {
                                    "id": 12,
                                    "logDate": "2026-07-24",
                                    "fullname": "刘大扬",
                                    "username": "liudayang",
                                    "content": "完成周工作。",
                                }
                            ],
                            "totalElements": 1,
                        },
                    ),
                ),
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )

        result = adapter.list_team_logs(
            worker,
            {
                "member": "刘大扬",
                "start_date": "2026-07-20",
                "end_date": "2026-07-26",
                "view_mode": "submittedAt",
                "size": 100,
            },
        )

        query = worker.calls[1]["query"]
        self.assertEqual(query["userId"], ["300000881"])
        self.assertEqual(query["deptId"], ["300000101"])
        self.assertEqual(query["startDate"], ["2026-07-20"])
        self.assertEqual(query["endDate"], ["2026-07-26"])
        self.assertEqual(query["viewMode"], ["logDate"])
        self.assertEqual(query["sort"], ["logDate,desc", "createdAt,desc"])
        self.assertEqual(result["viewMode"], "logDate")
        self.assertEqual(result["filters"]["member"]["username"], "liudayang")

    def test_team_logs_support_department_watch_group_and_single_date(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "GET",
                    "/api/work-logs/team/dept-options",
                    response(
                        200,
                        [
                            {
                                "value": 300000001,
                                "label": "泰华智慧产业集团",
                                "children": [
                                    {
                                        "value": 300000101,
                                        "label": "山东泰华照明科技有限公司",
                                    }
                                ],
                            }
                        ],
                    ),
                ),
                expected(
                    "GET",
                    "/api/watch-groups",
                    response(200, [{"id": 9, "name": "重点关注"}]),
                ),
                expected(
                    "GET",
                    "/api/work-logs/team",
                    response(
                        200,
                        {
                            "content": [
                                {
                                    "id": 13,
                                    "deptId": 300000101,
                                    "deptName": "山东泰华照明科技有限公司",
                                    "logDate": "2026-07-24",
                                    "content": "部门日志。",
                                }
                            ],
                            "totalElements": 1,
                        },
                    ),
                ),
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )

        result = adapter.list_team_logs(
            worker,
            {
                "department": "山东泰华照明科技有限公司",
                "watch_group": "重点关注",
                "log_date": "2026-07-24",
            },
        )

        query = worker.calls[2]["query"]
        self.assertEqual(query["deptId"], ["300000101"])
        self.assertEqual(query["watchGroupId"], ["9"])
        self.assertEqual(query["logDate"], ["2026-07-24"])
        self.assertEqual(
            result["filters"]["department"]["name"],
            "山东泰华照明科技有限公司",
        )
        self.assertEqual(result["filters"]["watchGroup"]["name"], "重点关注")

    def test_team_logs_reject_invalid_date_ranges_before_calling_backend(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )

        with self.assertRaisesRegex(ValueError, "provided together"):
            adapter.list_team_logs(worker, {"start_date": "2026-07-20"})
        with self.assertRaisesRegex(ValueError, "must not be earlier"):
            adapter.list_team_logs(
                worker,
                {"start_date": "2026-07-26", "end_date": "2026-07-20"},
            )
        self.assertEqual(worker.calls, [])

    def test_team_logs_stop_when_backend_ignores_member_filter(self):
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")
        worker = FakeHttpWorker(
            [
                expected(
                    "GET",
                    "/api/work-logs/team/member-options",
                    response(
                        200,
                        [
                            {
                                "userId": 300000881,
                                "fullname": "刘大扬",
                                "username": "liudayang",
                                "deptId": 300000101,
                            }
                        ],
                    ),
                ),
                expected(
                    "GET",
                    "/api/work-logs/team",
                    response(
                        200,
                        {
                            "content": [
                                {
                                    "id": 14,
                                    "userId": 999,
                                    "logDate": "2026-07-24",
                                    "content": "不属于所选成员。",
                                }
                            ],
                            "totalElements": 1,
                        },
                    ),
                ),
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )

        with self.assertRaisesRegex(
            TaihuaQueryFilterUnverified,
            "未按成员条件筛选",
        ):
            adapter.list_team_logs(worker, {"member": "刘大扬"})

    def test_unverified_date_and_department_filters_are_not_login_actions(self):
        from agentbridge.adapters.taihua import _verify_team_log_filters
        for filters, department in (({"logDate": "2026-09-06"}, None),
                                    ({"startDate": "2026-09-01", "endDate": "2026-09-06"}, None),
                                    ({}, {"id": 10, "name": "测试部门"})):
            with self.subTest(filters=filters, department=department):
                with self.assertRaises(TaihuaQueryFilterUnverified) as caught:
                    _verify_team_log_filters([{"logDate": "2026-08-01", "deptId": 20}],
                                             date_filters=filters, member=None, department=department)
                self.assertEqual(caught.exception.error_code, "QUERY_FILTER_UNVERIFIED")

    def test_write_errors_distinguish_business_rejection_and_unknown_outcome(self):
        business_worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/work-logs",
                    response(400, {"message": "该日期不允许重复填写。"}),
                )
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )
        unknown_worker = FakeHttpWorker(
            [
                expected(
                    "POST",
                    "/api/work-logs",
                    response(500, {"message": "internal error"}),
                )
            ],
            state={"authorization": "Bearer access", "refresh_token": "refresh"},
        )
        adapter = TaihuaCentralAdapter(base_url="http://10.10.50.101")

        with self.assertRaisesRegex(
            TaihuaBusinessRuleRejected,
            "该日期不允许重复填写",
        ):
            adapter.create_work_log(business_worker, {"content": "test"})
        with self.assertRaises(TaihuaWorkLogOutcomeUnknown):
            adapter.create_work_log(unknown_worker, {"content": "test"})
    def test_prepare_and_commit_freeze_fields_and_verify_readback(self):
        adapter = FakeWriteAdapter()
        prepared = prepare_taihua_work_log_create(
            adapter,
            object(),
            {
                "log_date": "2026-07-26",
                "hours": 8,
                "project": "AB-01",
                "content": "完成泰华日志系统适配测试。",
            },
        )
        boundary_calls: list[str] = []

        self.assertEqual(prepared["summary"]["system"], "日志系统")
        result = commit_taihua_work_log_create(
            adapter,
            object(),
            prepared["plan"],
            enter_commit_boundary=lambda: boundary_calls.append("commit"),
        )

        self.assertEqual(prepared["plan"]["exact_input"]["project_id"], 9)
        self.assertEqual(boundary_calls, ["commit"])
        self.assertEqual(adapter.created_payload["typeCode"], "DAILY")
        self.assertEqual(result["status"], "created")
        self.assertTrue(result["verification"]["matched"])

    def test_prepare_rejects_exact_duplicate_log(self):
        adapter = FakeWriteAdapter(existing_before_prepare=True)

        with self.assertRaises(TaihuaBusinessRuleRejected):
            prepare_taihua_work_log_create(
                adapter,
                object(),
                {
                    "log_date": "2026-07-26",
                    "hours": 8,
                    "content": "重复日志",
                },
            )

    def test_prepare_allows_a_different_log_on_the_same_date(self):
        adapter = FakeWriteAdapter(existing_before_prepare=True)

        prepared = prepare_taihua_work_log_create(
            adapter,
            object(),
            {
                "log_date": "2026-07-26",
                "hours": 4,
                "content": "同一天的另一项工作",
            },
        )

        self.assertEqual(prepared["plan"]["preconditions"]["same_date_log_count"], 1)

    def test_date_field_card_renders_prefill_and_normalizes_submission(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            schema = {
                **TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA,
                "fields": [
                    {**field, "value": _field_value(field["name"])}
                    for field in TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA["fields"]
                ],
            }
            submission = store.create(
                user_subject="user-a",
                system_id="taihua",
                session_id="session-a",
                capability_name="taihua.work_log.create.prepare",
                capability_version="0.1.0",
                create_operation_id="prepare-1",
                form_schema=schema,
                card_base_url="http://127.0.0.1:8780",
            )
            app = TrustedFieldApplication(submission_store=store)
            page = app.get_card(submission["submission_id"], secure_cookie=False)
            html = page.body.decode("utf-8")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
            cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

            accepted = app.submit_card(
                submission["submission_id"],
                body=urlencode(
                    {
                        "csrf_token": csrf,
                        "log_date": "2026-07-26",
                        "hours": "8",
                        "project": "AB-01",
                        "content": "完成泰华日志适配。",
                    }
                ).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            stored = store.get(submission["submission_id"], include_values=True)
            self.assertIn('type="date"', html)
            self.assertIn('value="2026-07-26"', html)
            self.assertEqual(accepted.status, 200)
            self.assertEqual(stored["values"]["log_date"], "2026-07-26")
            self.assertEqual(stored["values"]["hours"], 8)

    def test_central_service_keeps_oa_and_taihua_sessions_separate(self):
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://oa.example.test/seeyon/",
                taihua_base_url="http://10.10.50.101",
                trusted_card_base_url="http://127.0.0.1:8780",
            )
            oa_login = service.start_login(
                user_subject="user-a",
                expected_principal_ref="辛国茂",
                card_base_url="http://127.0.0.1:8780",
                system_id="oa",
            )
            taihua_login = service.start_login(
                user_subject="user-a",
                expected_principal_ref="辛国茂",
                card_base_url="http://127.0.0.1:8780",
                system_id="taihua",
            )

            self.assertNotEqual(
                oa_login["nextAction"]["challengeId"],
                taihua_login["nextAction"]["challengeId"],
            )
            self.assertEqual(
                service.sessions.find(user_subject="user-a", system_id="oa")[
                    "system_id"
                ],
                "oa",
            )
            self.assertEqual(
                service.sessions.find(user_subject="user-a", system_id="taihua")[
                    "system_id"
                ],
                "taihua",
            )

    def test_central_service_routes_taihua_read_to_http_runtime(self):
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://oa.example.test/seeyon/",
                taihua_base_url="http://10.10.50.101",
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="taihua",
                expected_principal_ref="辛国茂",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="辛国茂",
            )
            service.session_states.save(
                session["session_id"],
                {
                    "cookies": [],
                    "http": {
                        "authorization": "Bearer access",
                        "refresh_token": "refresh",
                    },
                },
            )
            worker = FakeHttpWorker(
                [
                    expected(
                        "GET",
                        "/api/work-logs/range",
                        response(
                            200,
                            [
                                {
                                    "id": 88,
                                    "logDate": "2026-07-26",
                                    "hours": 8,
                                    "content": "中心服务路由测试",
                                }
                            ],
                        ),
                    )
                ]
            )
            service._worker_factories_by_system["taihua"] = (
                lambda _session, _adapter: worker
            )

            result = service.invoke(
                user_subject="user-a",
                capability_name=TAIHUA_MY_LOGS_CAPABILITY,
                arguments={
                    "start_date": "2026-07-26",
                    "end_date": "2026-07-26",
                },
            )

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["items"][0]["id"], "88")
            self.assertIsNone(
                service.sessions.find(user_subject="user-a", system_id="oa")
            )


class FakeHttpWorker:
    def __init__(self, responses, *, state=None):
        self.responses = list(responses)
        self.state = {
            "cookies": [],
            "http": dict(state or {}),
        }
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def request(self, method, url, *, headers=None, body=None, timeout_seconds=30):
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        route = self.responses.pop(0)
        parsed = urlparse(url)
        self.calls.append(
            {
                "method": method,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(headers or {}),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        self.assert_route(route, method, parsed.path)
        return route["response"]

    @staticmethod
    def assert_route(route, method, path):
        if route["method"] != method or route["path"] != path:
            raise AssertionError(
                f"expected {route['method']} {route['path']}, got {method} {path}"
            )

    def capture_session_state(self):
        return {
            "cookies": [],
            "http": dict(self.state["http"]),
        }

    def restore_session_state(self, state):
        self.state = {
            "cookies": [],
            "http": dict(state["http"]),
        }

    def clear_session_state(self):
        self.state = {"cookies": [], "http": {}}

    def get_http_state(self):
        return dict(self.state["http"])

    def set_http_state(self, state):
        self.state = {"cookies": [], "http": dict(state)}


class FakeWriteAdapter:
    def __init__(self, *, existing_before_prepare=False):
        self.existing_before_prepare = existing_before_prepare
        self.read_count = 0
        self.created_payload = None

    def work_logs_for_date(self, _worker, log_date):
        self.read_count += 1
        if self.existing_before_prepare and self.read_count == 1:
            return [
                {
                    "id": "existing",
                    "logDate": log_date,
                    "hours": 8,
                    "content": "重复日志",
                    "projectId": None,
                }
            ]
        if self.created_payload is not None:
            return [
                {
                    "id": "created-1",
                    "logDate": log_date,
                    "hours": self.created_payload["hours"],
                    "content": self.created_payload["content"],
                    "projectId": self.created_payload.get("projectId"),
                }
            ]
        return []

    def project_candidates(self, _worker, _query, *, limit):
        self.project_limit = limit
        return [{"id": "9", "code": "AB-01", "name": "AgentBridge"}]

    def create_work_log(self, _worker, payload):
        self.created_payload = dict(payload)
        return {"id": "created-1"}


def expected(method, path, response_value):
    return {"method": method, "path": path, "response": response_value}


def response(status, payload):
    return {
        "status": status,
        "url": "http://10.10.50.101/",
        "json": payload,
        "text": "",
    }


def _field_value(name):
    return {
        "log_date": "2026-07-26",
        "hours": 8,
        "project": "AB-01",
        "content": "完成泰华日志适配。",
    }[name]


if __name__ == "__main__":
    unittest.main()
