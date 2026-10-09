import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.core.write_authorizations import WriteAuthorizationStore


class TrustedActionCardTests(unittest.TestCase):
    def test_card_displays_frozen_business_summary_and_strict_headers(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = _authorization(store)
            app = TrustedActionApplication(authorization_store=store)

            response = app.get_card(
                authorization["authorization_id"],
                secure_cookie=False,
            )

            html = response.body.decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("保存出差申请草稿", html)
            self.assertIn("济南", html)
            self.assertIn("青岛", html)
            self.assertIn("只保存为待发草稿", html)
            self.assertIn("执行效果", html)
            self.assertIn("授权保存草稿", html)
            self.assertIn(authorization["plan_hash"], html)
            self.assertNotIn("internal-selector", html)
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("script-src 'nonce-", response.headers["Content-Security-Policy"])
            self.assertIn('postEvent("web_app_ready")', html)
            self.assertIn('postEvent("web_app_expand")', html)
            self.assertIn("if (false && (platform || canPost || canNotify))", html)
            self.assertNotIn("telegram.org", html)

    def test_meeting_card_discloses_immediate_create_and_send_effect(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = _meeting_authorization(store)
            app = TrustedActionApplication(authorization_store=store)

            response = app.get_card(
                authorization["authorization_id"],
                secure_cookie=False,
            )

            html = response.body.decode("utf-8")
            self.assertIn("创建并发送会议", html)
            self.assertIn("预订会议室并向当前用户发送会议", html)
            self.assertIn("不会仅保存为草稿", html)
            self.assertIn("授权创建并发送", html)
            self.assertNotIn("授权保存草稿", html)

    def test_card_never_mislabels_missing_system_as_oa(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = store.create(
                user_subject="user-a",
                system_id="smartlight",
                session_id="session-a",
                capability_name="smartlight.alarm.remark.update",
                capability_version="0.1.0",
                prepare_operation_id="prepare-smartlight-1",
                plan={"business_intent": "update_alarm_remark"},
                summary={"title": "修改照明 RTU 告警备注", "fields": []},
                card_base_url="http://127.0.0.1:8780",
            )
            app = TrustedActionApplication(authorization_store=store)

            response = app.get_card(
                authorization["authorization_id"],
                secure_cookie=False,
            )

            html = response.body.decode("utf-8")
            self.assertIn("未标明", html)
            self.assertNotIn("致远 OA", html)

    def test_card_does_not_disable_submitter_before_form_serialization(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = _authorization(store)
            app = TrustedActionApplication(authorization_store=store)

            response = app.get_card(
                authorization["authorization_id"],
                secure_cookie=False,
            )

            html = response.body.decode("utf-8")
            self.assertIn('name="decision" value="approve"', html)
            self.assertIn('name="decision" value="reject"', html)
            self.assertNotIn("form.addEventListener('submit'", html)

    def test_card_approval_is_csrf_bound_and_single_use(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = _authorization(store)
            app = TrustedActionApplication(authorization_store=store)
            page = app.get_card(authorization["authorization_id"], secure_cookie=False)
            html = page.body.decode("utf-8")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
            cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

            denied = app.submit_card(
                authorization["authorization_id"],
                body=urlencode({"csrf_token": csrf, "decision": "approve"}).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie="wrong",
            )
            approved = app.submit_card(
                authorization["authorization_id"],
                body=urlencode({"csrf_token": csrf, "decision": "approve"}).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )
            replay = app.submit_card(
                authorization["authorization_id"],
                body=urlencode({"csrf_token": csrf, "decision": "approve"}).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            self.assertEqual(denied.status, 403)
            self.assertEqual(approved.status, 200)
            self.assertIn("操作已授权", approved.body.decode("utf-8"))
            self.assertIn(
                "if (true && (platform || canPost || canNotify))",
                approved.body.decode("utf-8"),
            )
            self.assertEqual(store.get(authorization["authorization_id"])["state"], "approved")
            self.assertEqual(replay.status, 200)
            self.assertIn("已在其他可信端确认", replay.body.decode("utf-8"))

    def test_each_endpoint_gets_an_independent_card_session(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = _authorization(store)
            app = TrustedActionApplication(authorization_store=store)
            endpoint_a = store.create_presentation(
                authorization["authorization_id"],
                user_subject="user-a",
                endpoint_id="telegram-1",
            )
            endpoint_b = store.create_presentation(
                authorization["authorization_id"],
                user_subject="user-a",
                endpoint_id="workspace-1",
            )
            page_a = app.get_card(
                authorization["authorization_id"],
                presentation_id=endpoint_a["presentation_id"],
                secure_cookie=False,
            )
            page_b = app.get_card(
                authorization["authorization_id"],
                presentation_id=endpoint_b["presentation_id"],
                secure_cookie=False,
            )
            csrf_a = re.search(
                r'name="csrf_token" value="([^"]+)"',
                page_a.body.decode("utf-8"),
            ).group(1)
            csrf_b = re.search(
                r'name="csrf_token" value="([^"]+)"',
                page_b.body.decode("utf-8"),
            ).group(1)
            cookie_a = page_a.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
            cookie_b = page_b.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

            approved = app.submit_card(
                authorization["authorization_id"],
                presentation_id=endpoint_b["presentation_id"],
                body=urlencode(
                    {"csrf_token": csrf_b, "decision": "approve"}
                ).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie_b,
            )
            already_done = app.submit_card(
                authorization["authorization_id"],
                presentation_id=endpoint_a["presentation_id"],
                body=urlencode(
                    {"csrf_token": csrf_a, "decision": "approve"}
                ).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie_a,
            )

            self.assertEqual(approved.status, 200)
            self.assertEqual(already_done.status, 200)
            self.assertIn(
                "已在其他可信端确认",
                already_done.body.decode("utf-8"),
            )
            self.assertIn(
                f"Path=/authorize/{authorization['authorization_id']}/present/",
                page_a.headers["Set-Cookie"],
            )

    def test_rejection_never_creates_an_executable_authorization(self):
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = _authorization(store)
            app = TrustedActionApplication(authorization_store=store)
            page = app.get_card(authorization["authorization_id"], secure_cookie=True)
            html = page.body.decode("utf-8")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
            cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

            response = app.submit_card(
                authorization["authorization_id"],
                body=urlencode({"csrf_token": csrf, "decision": "reject"}).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            self.assertEqual(response.status, 200)
            self.assertIn("操作已取消", response.body.decode("utf-8"))
            self.assertIn(
                "if (true && (platform || canPost || canNotify))",
                response.body.decode("utf-8"),
            )
            self.assertEqual(store.get(authorization["authorization_id"])["state"], "rejected")
            self.assertIn("Secure", page.headers["Set-Cookie"])


def _authorization(store):
    return store.create(
        user_subject="user-a",
        system_id="oa",
        session_id="session-a",
        capability_name="oa.business_trip.save_draft",
        capability_version="0.1.0",
        prepare_operation_id="prepare-1",
        plan={
            "exact_input": {"reason": "Test"},
            "internal": "internal-selector",
        },
        summary={
            "title": "保存出差申请草稿",
            "principal": "Alice",
            "system": "致远 OA",
            "effect": "仅保存待发草稿",
            "authorization_notice": "授权后只保存为待发草稿，不会发送、提交或进入审批流程。",
            "authorize_label": "授权保存草稿",
            "fields": [
                {"label": "出差始发地", "value": "济南"},
                {"label": "出差目的地", "value": "青岛"},
            ],
        },
        card_base_url="http://127.0.0.1:8780",
    )


def _meeting_authorization(store):
    return store.create(
        user_subject="user-a",
        system_id="oa",
        session_id="session-a",
        capability_name="oa.meeting.create",
        capability_version="0.1.0",
        prepare_operation_id="prepare-meeting-1",
        plan={"business_intent": "create_meeting"},
        summary={
            "title": "创建并发送会议",
            "principal": "Alice",
            "system": "致远 OA",
            "effect": "预订会议室并向当前用户发送会议",
            "authorization_notice": "授权后将立即预订上述会议室并发送会议，不会仅保存为草稿。",
            "authorize_label": "授权创建并发送",
            "fields": [
                {"label": "会议主题", "value": "智能体测试"},
                {"label": "会议室", "value": "4层3#会议室"},
            ],
        },
        card_base_url="http://127.0.0.1:8780",
    )


if __name__ == "__main__":
    unittest.main()
