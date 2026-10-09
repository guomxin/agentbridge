from copy import deepcopy
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from agentbridge.adapters.seeyon_business_trip import BUSINESS_TRIP_FIELD_CARD_SCHEMA
from agentbridge.adapters.seeyon_meeting import MEETING_FIELD_CARD_SCHEMA
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.core.field_submissions import FieldSubmissionStore


class TrustedFieldCardTests(unittest.TestCase):
    def test_card_renders_business_fields_and_strict_headers(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            submission = _submission(store)
            app = TrustedFieldApplication(submission_store=store)

            response = app.get_card(submission["submission_id"], secure_cookie=False)

            html = response.body.decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("填写出差申请", html)
            self.assertIn('name="start_time"', html)
            self.assertIn('name="reason"', html)
            self.assertIn('name="has_direct_supervisor"', html)
            self.assertIn("只保存为待发草稿", html)
            self.assertNotIn("form.addEventListener('submit'", html)
            self.assertIn("width:1px; height:1px", html)
            self.assertIn("HttpOnly", response.headers["Set-Cookie"])
            self.assertIn("SameSite=Strict", response.headers["Set-Cookie"])
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertIn("script-src 'nonce-", response.headers["Content-Security-Policy"])
            self.assertIn('postEvent("web_app_ready")', html)
            self.assertIn('postEvent("web_app_expand")', html)
            self.assertIn("if (false && (platform || canPost || canNotify))", html)
            self.assertNotIn("telegram.org", html)

    def test_card_renders_read_only_approval_context_before_input(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            schema = deepcopy(BUSINESS_TRIP_FIELD_CARD_SCHEMA)
            schema["review"] = {
                "title": "【综合】乘坐飞机申请单",
                "effect": "审批通过后该事项将离开待办列表",
                "fields": [
                    {"label": "申请人", "value": "郑其荣"},
                    {"label": "航段1 · 行程", "value": "济南 → 榆林"},
                ],
            }
            submission = store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                capability_name="oa.flight_application.approval.prepare",
                capability_version="0.1.0",
                create_operation_id="prepare-flight-1",
                form_schema=schema,
                card_base_url="http://127.0.0.1:8780",
            )
            app = TrustedFieldApplication(submission_store=store)

            response = app.get_card(submission["submission_id"], secure_cookie=False)
            html = response.body.decode("utf-8")

            self.assertIn("审批依据", html)
            self.assertIn("【综合】乘坐飞机申请单", html)
            self.assertIn("郑其荣", html)
            self.assertIn("济南 → 榆林", html)
            self.assertLess(html.index("审批依据"), html.index('name="start_time"'))

    def test_card_renders_prefilled_meeting_values_and_real_room_options(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            schema = deepcopy(MEETING_FIELD_CARD_SCHEMA)
            fields = {item["name"]: item for item in schema["fields"]}
            fields["subject"]["value"] = "智能体测试"
            fields["room"] = {
                "name": "room",
                "label": "会议室",
                "control": "select",
                "required": True,
                "options": [
                    {"value": "4层2#会议室", "label": "4层2#会议室"},
                    {"value": "4层3#会议室", "label": "4层3#会议室"},
                ],
                "value": "4层3#会议室",
            }
            fields["start_time"]["value"] = "2026-07-18 17:00"
            fields["end_time"]["value"] = "2026-07-18 18:00"
            schema["fields"] = [fields[item["name"]] for item in schema["fields"]]
            submission = store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                capability_name="oa.meeting.create.prepare",
                capability_version="0.1.0",
                create_operation_id="prepare-meeting-1",
                form_schema=schema,
                card_base_url="http://127.0.0.1:8780",
            )
            app = TrustedFieldApplication(submission_store=store)

            response = app.get_card(submission["submission_id"], secure_cookie=False)

            html = response.body.decode("utf-8")
            self.assertIn('value="智能体测试"', html)
            self.assertIn('value="2026-07-18T17:00"', html)
            self.assertIn('value="2026-07-18T18:00"', html)
            self.assertIn(
                '<option value="4层3#会议室" selected>4层3#会议室</option>',
                html,
            )
            self.assertIn('<option value="4层2#会议室">4层2#会议室</option>', html)

    def test_invalid_time_range_is_shown_without_consuming_card(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            submission = _submission(store)
            app = TrustedFieldApplication(submission_store=store)
            csrf, cookie = _csrf(app, submission["submission_id"])
            values = _valid_values()
            values["end_time"] = "2026-07-14T08:00"

            response = app.submit_card(
                submission["submission_id"],
                body=urlencode({"csrf_token": csrf, **values}).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            html = response.body.decode("utf-8")
            self.assertEqual(response.status, 400)
            self.assertIn("结束时间必须晚于开始时间", html)
            self.assertIn('value="济南"', html)
            self.assertEqual(store.get(submission["submission_id"])["state"], "pending")

    def test_submission_is_normalized_csrf_bound_and_single_use(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            submission = _submission(store)
            app = TrustedFieldApplication(submission_store=store)
            csrf, cookie = _csrf(app, submission["submission_id"])
            body = urlencode({"csrf_token": csrf, **_valid_values()}).encode()

            denied = app.submit_card(
                submission["submission_id"],
                body=body,
                content_type="application/x-www-form-urlencoded",
                csrf_cookie="wrong",
            )
            accepted = app.submit_card(
                submission["submission_id"],
                body=body,
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )
            replay = app.submit_card(
                submission["submission_id"],
                body=body,
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            stored = store.get(submission["submission_id"], include_values=True)
            self.assertEqual(denied.status, 403)
            self.assertEqual(accepted.status, 200)
            accepted_body = accepted.body.decode("utf-8")
            self.assertIn("字段已提交", accepted_body)
            self.assertIn("请返回智能体继续", accepted_body)
            self.assertIn("if (true && (platform || canPost || canNotify))", accepted_body)
            self.assertEqual(stored["values"]["start_time"], "2026-07-14 09:00")
            self.assertFalse(stored["values"]["has_direct_supervisor"])
            self.assertNotIn("trip_days", stored["values"])
            self.assertNotIn("trip_hours", stored["values"])
            self.assertEqual(replay.status, 409)

    def test_unknown_posted_field_is_rejected(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            submission = _submission(store)
            app = TrustedFieldApplication(submission_store=store)
            csrf, cookie = _csrf(app, submission["submission_id"])

            response = app.submit_card(
                submission["submission_id"],
                body=urlencode(
                    {"csrf_token": csrf, **_valid_values(), "unexpected": "value"}
                ).encode(),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            self.assertEqual(response.status, 400)
            self.assertIn("填写字段不匹配", response.body.decode("utf-8"))
            self.assertEqual(store.get(submission["submission_id"])["state"], "pending")


    def test_endpoint_cards_do_not_invalidate_each_other(self):
        with TemporaryDirectory() as tmp:
            store = FieldSubmissionStore(Path(tmp) / "agentbridge.db")
            submission = _submission(store)
            telegram = store.create_presentation(
                submission["submission_id"],
                user_subject="user-a",
                endpoint_id="endpoint-telegram",
            )
            workspace = store.create_presentation(
                submission["submission_id"],
                user_subject="user-a",
                endpoint_id="endpoint-workspace",
            )
            app = TrustedFieldApplication(submission_store=store)
            telegram_csrf, telegram_cookie = _csrf(
                app,
                submission["submission_id"],
                telegram["presentation_id"],
            )
            workspace_csrf, workspace_cookie = _csrf(
                app,
                submission["submission_id"],
                workspace["presentation_id"],
            )
            body = urlencode(
                {"csrf_token": telegram_csrf, **_valid_values()}
            ).encode()

            accepted = app.submit_card(
                submission["submission_id"],
                body=body,
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=telegram_cookie,
                presentation_id=telegram["presentation_id"],
            )
            other_end = app.get_card(
                submission["submission_id"],
                secure_cookie=False,
                presentation_id=workspace["presentation_id"],
            )

            self.assertEqual(accepted.status, 200)
            self.assertEqual(other_end.status, 200)
            self.assertNotIn("<form", other_end.body.decode("utf-8"))
            self.assertNotEqual(telegram_csrf, workspace_csrf)
            self.assertTrue(workspace_cookie)


def _submission(store):
    return store.create(
        user_subject="user-a",
        system_id="oa",
        session_id="session-a",
        capability_name="oa.business_trip.prepare",
        capability_version="0.2.0",
        create_operation_id="prepare-1",
        form_schema=BUSINESS_TRIP_FIELD_CARD_SCHEMA,
        card_base_url="http://127.0.0.1:8780",
    )


def _csrf(app, submission_id, presentation_id=None):
    page = app.get_card(
        submission_id,
        secure_cookie=False,
        presentation_id=presentation_id,
    )
    html = page.body.decode("utf-8")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    return csrf, cookie


def _valid_values():
    return {
        "start_time": "2026-07-14T09:00",
        "end_time": "2026-07-14T18:00",
        "travel_mode": "火车",
        "origin": "济南",
        "destination": "青岛",
        "reason": "客户交流",
        "has_direct_supervisor": "false",
    }


if __name__ == "__main__":
    unittest.main()
