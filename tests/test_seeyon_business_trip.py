import unittest
from unittest.mock import MagicMock, patch

from agentbridge.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_CONTRACT_VERSION,
    BUSINESS_TRIP_FIELD_CARD_SCHEMA,
    BUSINESS_TRIP_FORM_APP_ID,
    BUSINESS_TRIP_TEMPLATE_ID,
    BUSINESS_TRIP_TEMPLATE_TITLE,
    _fill_decimal,
    BusinessTripContractMismatch,
    BusinessTripOutcomeUnknown,
    _supervisor_choice_to_bool,
    business_trip_contract_fingerprint,
    normalize_business_trip_inputs,
    prepare_business_trip_draft,
    save_business_trip_draft,
)


class BusinessTripDecimalFieldTests(unittest.TestCase):
    def test_decimal_fill_skips_oa_calculated_read_only_field(self):
        frame = MagicMock()
        wrapper = frame.locator.return_value
        active = MagicMock()
        editable = MagicMock()
        wrapper.locator.side_effect = [active, editable]
        active.count.return_value = 0
        editable.count.return_value = 0

        self.assertFalse(_fill_decimal(frame, "field0029", 0))
        active.first.fill.assert_not_called()
        editable.first.fill.assert_not_called()

    def test_decimal_fill_uses_visible_editable_field_when_available(self):
        frame = MagicMock()
        wrapper = frame.locator.return_value
        active = MagicMock()
        editable = MagicMock()
        wrapper.locator.side_effect = [active, editable]
        active.count.return_value = 0
        editable.count.return_value = 1

        self.assertTrue(_fill_decimal(frame, "field0022", 4))
        editable.first.fill.assert_called_once_with("4")

class SeeyonBusinessTripTests(unittest.TestCase):
    def test_supervisor_readback_requires_an_explicit_choice(self):
        self.assertTrue(_supervisor_choice_to_bool("是"))
        self.assertFalse(_supervisor_choice_to_bool("否"))
        self.assertIsNone(_supervisor_choice_to_bool(""))

    def test_normalization_exposes_business_fields_and_rejects_invalid_ranges(self):
        normalized = normalize_business_trip_inputs(
            _inputs(trip_days=0, trip_hours=4)
        )

        self.assertEqual(normalized["start_time"], "2026-07-13 09:00")
        self.assertEqual(normalized["travel_mode"], "火车")
        self.assertFalse(normalized["has_direct_supervisor"])
        self.assertNotIn("trip_days", normalized)
        self.assertNotIn("trip_hours", normalized)
        field_names = {
            field["name"] for field in BUSINESS_TRIP_FIELD_CARD_SCHEMA["fields"]
        }
        self.assertNotIn("trip_days", field_names)
        self.assertNotIn("trip_hours", field_names)

        with self.assertRaisesRegex(ValueError, "later than"):
            normalize_business_trip_inputs(
                _inputs(start_time="2026-07-13 18:00", end_time="2026-07-13 09:00")
            )
        with self.assertRaisesRegex(ValueError, "travel_mode"):
            normalize_business_trip_inputs(_inputs(travel_mode="火箭"))

    def test_prepare_freezes_template_contract_without_mutating_page(self):
        page = FakePage()
        page.title_value = "新建页面"
        frame = FakeFrame()
        with patch(
            "agentbridge.adapters.seeyon_business_trip._open_and_validate_form",
            return_value=(page, frame),
        ):
            prepared = prepare_business_trip_draft(FakeAdapter(), object(), _inputs())

        plan = prepared["plan"]
        self.assertEqual(plan["target"]["template_id"], BUSINESS_TRIP_TEMPLATE_ID)
        self.assertEqual(
            plan["form_contract"]["fingerprint"],
            business_trip_contract_fingerprint(),
        )
        self.assertEqual(plan["expected_effect"]["submitted_count"], 0)
        self.assertEqual(prepared["summary"]["effect"], "仅保存待发草稿")
        self.assertEqual(prepared["summary"]["authorize_label"], "授权保存草稿")
        self.assertIn("不会发送", prepared["summary"]["authorization_notice"])
        self.assertEqual(page.click_count, 0)

    def test_prepare_rejects_hidden_optional_note_before_authorization(self):
        page = FakePage()
        page.content_coll_visible = False
        frame = FakeFrame()
        with patch(
            "agentbridge.adapters.seeyon_business_trip._open_and_validate_form",
            return_value=(page, frame),
        ):
            with self.assertRaisesRegex(BusinessTripContractMismatch, "note field"):
                prepare_business_trip_draft(
                    FakeAdapter(),
                    object(),
                    _inputs(note="Not available on this CAP4 template"),
                )

        self.assertEqual(page.click_count, 0)

    def test_commit_consumes_authorization_once_before_click_and_verifies_reload(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_business_trip_inputs(_inputs())
        readback = {**expected, "note": "", "subject": "【HR】出差申请单-Alice"}
        boundary = []
        with (
            patch(
                "agentbridge.adapters.seeyon_business_trip._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_business_trip._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip._read_business_trip_form",
                side_effect=[readback, readback],
            ),
            patch(
                "agentbridge.adapters.seeyon_business_trip._wait_for_cap4_frame",
                return_value=frame,
            ),
            patch("agentbridge.adapters.seeyon_business_trip._validate_form_controls"),
        ):
            result = save_business_trip_draft(
                FakeAdapter(),
                object(),
                _plan(expected),
                enter_commit_boundary=lambda: boundary.append("consumed"),
                timeout_seconds=5,
            )

        self.assertEqual(boundary, ["consumed"])
        self.assertEqual(page.click_count, 1)
        self.assertTrue(result["draft_saved"])
        self.assertFalse(result["workflow_submitted"])
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["draft"]["state"], "wait_send")
        self.assertEqual(result["draft"]["summary_id"], "summary-1")
        self.assertTrue(result["verification"]["confirmed"])

    def test_post_click_verification_mismatch_is_unknown(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_business_trip_inputs(_inputs())
        precommit = {**expected, "note": "", "subject": "Draft"}
        mismatched = {**precommit, "destination": "Wrong"}
        boundary = []
        with (
            patch(
                "agentbridge.adapters.seeyon_business_trip._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_business_trip._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip._read_business_trip_form",
                side_effect=[precommit, mismatched],
            ),
            patch(
                "agentbridge.adapters.seeyon_business_trip._wait_for_cap4_frame",
                return_value=frame,
            ),
            patch("agentbridge.adapters.seeyon_business_trip._validate_form_controls"),
        ):
            with self.assertRaises(BusinessTripOutcomeUnknown):
                save_business_trip_draft(
                    FakeAdapter(),
                    object(),
                    _plan(expected),
                    enter_commit_boundary=lambda: boundary.append("consumed"),
                    timeout_seconds=5,
                )

        self.assertEqual(boundary, ["consumed"])
        self.assertEqual(page.click_count, 1)

    def test_stale_plan_is_rejected_before_authorization_is_consumed(self):
        plan = _plan(normalize_business_trip_inputs(_inputs()))
        plan["form_contract"]["fingerprint"] = "sha256:stale"
        boundary = []

        with self.assertRaises(BusinessTripContractMismatch):
            save_business_trip_draft(
                FakeAdapter(),
                object(),
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )

        self.assertEqual(boundary, [])


class FakeAdapter:
    def list_templates(self, _worker):
        return {
            "items": [
                {
                    "title": BUSINESS_TRIP_TEMPLATE_TITLE,
                    "template_id": BUSINESS_TRIP_TEMPLATE_ID,
                    "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
                    "href": "http://oa.example.test/seeyon/collaboration/new",
                }
            ]
        }


class FakePage:
    def __init__(self):
        self.url = "http://oa.example.test/seeyon/collaboration/new"
        self.title_value = ""
        self.click_count = 0
        self.handlers = {}
        self.content_coll_visible = True

    def title(self):
        return self.title_value

    def on(self, event, handler):
        self.handlers[event] = handler

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_timeout(self, _milliseconds):
        return None

    def wait_for_load_state(self, *_args, **_kwargs):
        return None


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        return 1

    def is_visible(self):
        return self.selector == "#content_coll" and self.page.content_coll_visible

    def click(self, **_kwargs):
        if self.selector != "#saveDraft_a":
            raise AssertionError(f"unexpected click: {self.selector}")
        self.page.click_count += 1
        self.page.url = (
            "http://oa.example.test/seeyon/collaboration/collaboration.do?"
            "method=newColl&summaryId=summary-1&affairId=affair-1&from=waitSend"
        )
        response_handler = self.page.handlers.get("response")
        if response_handler:
            response_handler(FakeResponse())


class FakeResponse:
    url = "http://oa.example.test/seeyon/collaboration/collaboration.do?method=saveDraft"
    status = 200


class FakeFrame:
    url = (
        "http://oa.example.test/seeyon/common/cap4/index.html?"
        f"moduleId={BUSINESS_TRIP_TEMPLATE_ID}"
    )


def _inputs(**updates):
    value = {
        "start_time": "2026-07-13 09:00",
        "end_time": "2026-07-13 18:00",
        "travel_mode": "火车",
        "origin": "济南",
        "destination": "青岛",
        "reason": "智能体中心端草稿测试",
        "has_direct_supervisor": False,
    }
    value.update(updates)
    return value


def _plan(inputs):
    return {
        "business_intent": "save_business_trip_request_draft",
        "target": {
            "template_title": BUSINESS_TRIP_TEMPLATE_TITLE,
            "template_id": BUSINESS_TRIP_TEMPLATE_ID,
            "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
        },
        "form_contract": {
            "version": BUSINESS_TRIP_CONTRACT_VERSION,
            "fingerprint": business_trip_contract_fingerprint(),
        },
        "exact_input": inputs,
    }


if __name__ == "__main__":
    unittest.main()
