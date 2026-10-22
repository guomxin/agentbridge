import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.core.write_authorizations import WriteAuthorizationStore


class TrustedActionCardTests(unittest.TestCase):
    def assert_inline_bridge(self, response, *, completed=False):
        html = response.body.decode("utf-8")
        self.assertIn(f"installTelegramBridge({{window, document, closeWhenComplete: {str(completed).lower()}}});", html)
        nonces = re.findall(r'<script\b[^>]*\bnonce="([^"]+)"', html)
        self.assertTrue(nonces)
        policy = response.headers["Content-Security-Policy"]
        script_policy = next(part.strip() for part in policy.split(";") if part.strip().startswith("script-src "))
        self.assertEqual(set(nonces), set(re.findall(r"'nonce-([^']+)'", script_policy)))
        self.assertNotIn("'unsafe-inline'", script_policy)
        self.assertNotRegex(html, r"<script\b[^>]*\bsrc\s*=")

    def test_reward_acknowledgement_card_displays_each_frozen_allocation_and_exact_action(self):
        from tests.test_seeyon_intellectual_property_reward import prepare, target

        _fixture, _snapshot, worker, adapter = target.__wrapped__()
        prepared = prepare(adapter, worker)
        with TemporaryDirectory() as tmp:
            store = WriteAuthorizationStore(Path(tmp) / "agentbridge.db")
            authorization = store.create(
                user_subject="user-a", system_id="oa", session_id="session-a",
                capability_name="oa.intellectual_property_reward.acknowledge",
                capability_version="0.2.0", prepare_operation_id="prepare-reward-1",
                plan=prepared["plan"], summary=prepared["summary"],
                card_base_url="http://127.0.0.1:8780",
            )
            app = TrustedActionApplication(authorization_store=store)
            response = app.get_card(authorization["authorization_id"], secure_cookie=False)
            html = response.body.decode("utf-8")

            self.assertEqual(response.status, 200)
            self.assertIn("阅办知识产权奖励单", html)
            self.assertIn("奖励金额</dt><dd>1000.00", html)
            for ordinal, name, amount in ((1, "合成人员甲", "600.00"), (2, "合成人员乙", "400.00")):
                with self.subTest(recipient=ordinal):
                    self.assertIn(f"奖励人 {ordinal}</dt><dd>{name}", html)
                    self.assertIn(f"奖励金额 {ordinal}（元）</dt><dd>{amount}", html)
            self.assertIn("本次操作</dt><dd>阅办知识产权奖励知会", html)
            self.assertIn("处理意见</dt><dd>已阅", html)
            self.assertIn("不调整奖励金额或人员", html)
            self.assertIn('value="approve" class="primary">授权阅办知识产权奖励', html)
            self.assertIn(authorization["plan_hash"], html)
            self.assertNotIn("审批通过", html)
            self.assertNotIn("workflow_approved", html)
            self.assertNotIn("<textarea", html)
            self.assertEqual(worker.page.commit_calls, [])

    def test_no_field_lighting_actions_show_precise_decision_and_original_alarm_target(self):
        from agentbridge.auth.action_card import _render_confirmation
        for capability, label in (
            ("smartlight.alarm.work_area.submit", "确认并提交工区"),
            ("smartlight.alarm.work_area.revoke", "确认并撤回工区提交"),
            ("smartlight.alarm.dispose", "确认并标记已处置"),
        ):
            with self.subTest(capability=capability):
                html = _render_confirmation({"authorization_id": "a" * 43, "capability_name": capability,
                    "plan_hash": "original-fingerprint", "summary": {"title": "照明告警操作",
                    "effect": "本次冻结操作", "authorize_label": "确认并执行", "authorization_notice": "原始后果提示",
                    "fields": [{"label": "目标告警", "value": "RTU-甲 / 告警 123"}]}},
                    csrf_token="session", nonce="nonce", presentation_id=None)
                self.assertIn(f'value="approve" class="primary">{label}', html)
                self.assertIn("RTU-甲 / 告警 123", html)
                self.assertIn("原始后果提示", html)
                self.assertIn("original-fingerprint", html)
                self.assertNotIn("<textarea", html)
                self.assertNotIn('style="', html)
                self.assertEqual('class="notice irreversible"' in html, capability.endswith(".dispose"))
                if capability.endswith(".dispose"):
                    self.assertIn("此操作不能由 AgentBridge 恢复", html)

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
            self.assert_inline_bridge(response)
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
            self.assert_inline_bridge(approved, completed=True)
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
            self.assert_inline_bridge(response, completed=True)
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
