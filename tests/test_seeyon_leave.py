import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_leave import (
    LEAVE_CONTRACT_VERSION,
    LEAVE_FORM_APP_ID,
    LEAVE_TEMPLATE_ID,
    LEAVE_TEMPLATE_TITLE,
    LeaveContractMismatch,
    LeaveOutcomeUnknown,
    _read_leave_form,
    _supervisor_choice_to_bool,
    leave_contract_fingerprint,
    normalize_leave_inputs,
    prepare_leave_draft,
    save_leave_draft,
)


class SeeyonLeaveTests(unittest.TestCase):
    def test_supervisor_readback_requires_an_explicit_choice(self):
        self.assertTrue(_supervisor_choice_to_bool("是"))
        self.assertFalse(_supervisor_choice_to_bool("否"))
        self.assertIsNone(_supervisor_choice_to_bool(""))

    def test_normalization_supports_only_first_phase_attachment_free_types(self):
        normalized = normalize_leave_inputs(_inputs())

        self.assertEqual(normalized["leave_type"], "年休")
        self.assertEqual(normalized["start_time"], "2026-07-22 09:00")
        with self.assertRaisesRegex(ValueError, "attachment-free"):
            normalize_leave_inputs(_inputs(leave_type="病假"))
        with self.assertRaisesRegex(ValueError, "later than"):
            normalize_leave_inputs(
                _inputs(start_time="2026-07-22 18:00", end_time="2026-07-22 09:00")
            )

    def test_readback_uses_cap4_browse_node_for_calculated_duration(self):
        frame = ReadbackFrame()

        readback = _read_leave_form(ReadbackPage(), frame)

        self.assertIn(".cap4-number__browse", frame.script)
        self.assertEqual(readback["leave_days"], "0.43")
        self.assertEqual(readback["leave_hours"], "3.00")

    def test_prepare_freezes_live_contract_without_clicking_save_or_send(self):
        page = FakePage()
        frame = FakeFrame()
        with (
            patch(
                "agentbridge.adapters.seeyon_leave._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave._read_leave_form",
                return_value={
                    **normalize_leave_inputs(_inputs()),
                    "subject": "【HR】请假申请单-Alice",
                    "leave_days": "1",
                    "leave_hours": "",
                },
            ),
        ):
            prepared = prepare_leave_draft(FakeAdapter(), object(), _inputs())

        plan = prepared["plan"]
        self.assertEqual(plan["target"]["template_id"], LEAVE_TEMPLATE_ID)
        self.assertEqual(plan["form_contract"]["fingerprint"], leave_contract_fingerprint())
        self.assertFalse(plan["expected_effect"]["workflow_submitted"])
        self.assertEqual(prepared["summary"]["effect"], "仅保存待发草稿")
        self.assertIn("不会发送", prepared["summary"]["authorization_notice"])
        self.assertEqual(page.click_count, 0)

    def test_commit_consumes_authorization_before_save_and_verifies_reload(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_leave_inputs(_inputs())
        readback = {
            **expected,
            "subject": "【HR】请假申请单-Alice",
            "leave_days": "1",
            "leave_hours": "",
        }
        boundary = []
        with (
            patch(
                "agentbridge.adapters.seeyon_leave._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave._read_leave_form",
                side_effect=[readback, readback],
            ),
            patch("agentbridge.adapters.seeyon_leave._wait_for_cap4_frame", return_value=frame),
            patch("agentbridge.adapters.seeyon_leave._validate_form_controls"),
        ):
            result = save_leave_draft(
                FakeAdapter(),
                object(),
                _plan(expected),
                enter_commit_boundary=lambda: boundary.append(page.click_count),
                timeout_seconds=5,
            )

        self.assertEqual(boundary, [0])
        self.assertEqual(page.clicked_selector, "#saveDraft_a")
        self.assertEqual(page.click_count, 1)
        self.assertTrue(result["draft_saved"])
        self.assertFalse(result["workflow_submitted"])
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["draft"]["state"], "wait_send")
        self.assertEqual(result["request_evidence"][0]["method"], "POST")

    def test_verification_mismatch_after_save_is_outcome_unknown(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_leave_inputs(_inputs())
        precommit = {
            **expected,
            "subject": "Leave",
            "leave_days": "1",
            "leave_hours": "",
        }
        mismatched = {**precommit, "reason": "Wrong"}
        with (
            patch(
                "agentbridge.adapters.seeyon_leave._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave._read_leave_form",
                side_effect=[precommit, mismatched],
            ),
            patch("agentbridge.adapters.seeyon_leave._wait_for_cap4_frame", return_value=frame),
            patch("agentbridge.adapters.seeyon_leave._validate_form_controls"),
        ):
            with self.assertRaises(LeaveOutcomeUnknown):
                save_leave_draft(
                    FakeAdapter(),
                    object(),
                    _plan(expected),
                    enter_commit_boundary=lambda: None,
                    timeout_seconds=5,
                )

        self.assertEqual(page.clicked_selector, "#saveDraft_a")

    def test_missing_oa_computed_duration_is_advisory_after_stable_draft_readback(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_leave_inputs(_inputs())
        readback = {
            **expected,
            "subject": "Leave",
            "leave_days": "",
            "leave_hours": "",
        }
        with (
            patch(
                "agentbridge.adapters.seeyon_leave._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave._read_leave_form",
                side_effect=[readback, readback],
            ),
            patch("agentbridge.adapters.seeyon_leave._wait_for_cap4_frame", return_value=frame),
            patch("agentbridge.adapters.seeyon_leave._validate_form_controls"),
        ):
            result = save_leave_draft(
                FakeAdapter(),
                object(),
                _plan(expected),
                enter_commit_boundary=lambda: None,
                timeout_seconds=5,
            )

        self.assertEqual(page.clicked_selector, "#saveDraft_a")
        self.assertTrue(result["draft_saved"])
        self.assertFalse(result["verification"]["duration"]["reported"])
        self.assertEqual(result["draft"]["summary_id"], "leave-summary")
        self.assertEqual(result["draft"]["affair_id"], "leave-affair")

    def test_stale_plan_is_rejected_before_authorization_consumption(self):
        plan = _plan(normalize_leave_inputs(_inputs()))
        plan["form_contract"]["fingerprint"] = "sha256:stale"
        boundary = []

        with self.assertRaises(LeaveContractMismatch):
            save_leave_draft(
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
                    "title": LEAVE_TEMPLATE_TITLE,
                    "template_id": LEAVE_TEMPLATE_ID,
                    "form_app_id": LEAVE_FORM_APP_ID,
                    "href": "http://oa.example.test/seeyon/collaboration/new",
                }
            ]
        }


class FakePage:
    def __init__(self):
        self.url = "http://oa.example.test/seeyon/collaboration/new"
        self.click_count = 0
        self.clicked_selector = None
        self.handlers = {}

    def title(self):
        return "新建请假申请"

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

    def click(self, **_kwargs):
        if self.selector != "#saveDraft_a":
            raise AssertionError(f"unexpected click: {self.selector}")
        self.page.clicked_selector = self.selector
        self.page.click_count += 1
        self.page.url = (
            "http://oa.example.test/seeyon/collaboration/collaboration.do?"
            "method=newColl&summaryId=leave-summary&affairId=leave-affair&from=waitSend"
        )
        response_handler = self.page.handlers.get("response")
        if response_handler:
            response_handler(FakeResponse())


class FakeResponse:
    url = "http://oa.example.test/seeyon/collaboration/collaboration.do?method=saveDraft"
    status = 200
    request = type("FakeRequest", (), {"method": "POST"})()


class FakeFrame:
    url = (
        "http://oa.example.test/seeyon/common/cap4/index.html?"
        f"moduleId={LEAVE_TEMPLATE_ID}"
    )


class ReadbackFrame:
    def __init__(self):
        self.script = ""

    def evaluate(self, script):
        self.script = script
        return {
            "leave_type": "年休",
            "start_time": "2026-07-22 09:00",
            "end_time": "2026-07-22 12:00",
            "leave_days": "0.43",
            "leave_hours": "3.00",
            "reason": "个人事务",
            "supervisor_selection": "否",
        }


class ReadbackPage:
    def locator(self, selector):
        if selector != "#subject":
            raise AssertionError(f"unexpected selector: {selector}")
        return ReadbackSubject()


class ReadbackSubject:
    def input_value(self):
        return "【HR】请假申请单"


def _inputs(**updates):
    value = {
        "leave_type": "年休",
        "start_time": "2026-07-22 09:00",
        "end_time": "2026-07-22 18:00",
        "reason": "个人事务",
        "has_direct_supervisor": False,
    }
    value.update(updates)
    return value


def _plan(inputs):
    return {
        "business_intent": "save_leave_request_draft",
        "target": {
            "template_title": LEAVE_TEMPLATE_TITLE,
            "template_id": LEAVE_TEMPLATE_ID,
            "form_app_id": LEAVE_FORM_APP_ID,
        },
        "form_contract": {
            "version": LEAVE_CONTRACT_VERSION,
            "fingerprint": leave_contract_fingerprint(),
        },
        "exact_input": inputs,
    }


if __name__ == "__main__":
    unittest.main()
