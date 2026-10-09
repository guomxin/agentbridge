import unittest
from unittest.mock import Mock, patch

from agentbridge.adapters.seeyon_business_trip import (
    BUSINESS_TRIP_FORM_APP_ID,
    BUSINESS_TRIP_TEMPLATE_ID,
    BUSINESS_TRIP_TEMPLATE_TITLE,
    BusinessTripContractMismatch,
    BusinessTripOutcomeUnknown,
    normalize_business_trip_inputs,
)
from agentbridge.adapters.seeyon_business_trip_submit import (
    BUSINESS_TRIP_SUBMIT_CONTRACT_VERSION,
    BusinessTripBusinessValidationRequired,
    _wait_for_sent_readback,
    business_trip_submit_contract_fingerprint,
    prepare_business_trip_submission,
    submit_business_trip_request,
)


class SeeyonBusinessTripSubmitTests(unittest.TestCase):
    def test_prepare_freezes_subject_and_sent_baseline_without_clicking(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_business_trip_inputs(_inputs())
        readback = {**expected, "note": "", "subject": "【HR】出差申请单-Alice-青岛"}
        adapter = FakeAdapter(sent_items=[{"affair_id": "sent-old", "title": "Old"}])
        with (
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_business_trip_submit._validate_optional_inputs"),
            patch("agentbridge.adapters.seeyon_business_trip_submit._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._read_business_trip_form",
                return_value=readback,
            ),
        ):
            prepared = prepare_business_trip_submission(adapter, object(), _inputs())

        plan = prepared["plan"]
        self.assertEqual(plan["target"]["expected_subject"], readback["subject"])
        self.assertEqual(plan["sent_baseline_affair_ids"], ["sent-old"])
        self.assertTrue(plan["expected_effect"]["workflow_submitted"])
        self.assertEqual(prepared["summary"]["authorize_label"], "授权提交审批")
        self.assertIn("正式提交", prepared["summary"]["authorization_notice"])
        self.assertEqual(page.click_count, 0)

    def test_commit_consumes_authorization_before_send_and_verifies_sent_readback(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_business_trip_inputs(_inputs())
        subject = "【HR】出差申请单-Alice-青岛"
        readback = {**expected, "note": "", "subject": subject}
        boundary = []
        with (
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_business_trip_submit._validate_optional_inputs"),
            patch("agentbridge.adapters.seeyon_business_trip_submit._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._read_business_trip_form",
                return_value=readback,
            ),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._wait_for_sent_readback",
                return_value={
                    "affair_id": "sent-new",
                    "title": subject,
                    "state": "sent",
                    "detail_readable": True,
                    "field_count": 8,
                },
            ),
        ):
            result = submit_business_trip_request(
                FakeAdapter(),
                object(),
                _plan(expected, subject),
                enter_commit_boundary=lambda: boundary.append(page.click_count),
                timeout_seconds=5,
            )

        self.assertEqual(boundary, [0])
        self.assertEqual(page.click_count, 1)
        self.assertTrue(result["workflow_submitted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["submitted"]["affair_id"], "sent-new")
        self.assertEqual(result["request_evidence"][0]["method"], "POST")

    def test_post_send_readback_failure_is_outcome_unknown(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_business_trip_inputs(_inputs())
        subject = "【HR】出差申请单-Alice-青岛"
        readback = {**expected, "note": "", "subject": subject}
        boundary = []
        with (
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_business_trip_submit._validate_optional_inputs"),
            patch("agentbridge.adapters.seeyon_business_trip_submit._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._read_business_trip_form",
                return_value=readback,
            ),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._wait_for_sent_readback",
                side_effect=BusinessTripOutcomeUnknown("not confirmed"),
            ),
        ):
            with self.assertRaises(BusinessTripOutcomeUnknown):
                submit_business_trip_request(
                    FakeAdapter(),
                    object(),
                    _plan(expected, subject),
                    enter_commit_boundary=lambda: boundary.append("consumed"),
                    timeout_seconds=5,
                )

        self.assertEqual(boundary, ["consumed"])
        self.assertEqual(page.click_count, 1)

    def test_native_confirmation_requires_separate_matching_authorization(self):
        expected = normalize_business_trip_inputs(_inputs())
        subject = "Business trip subject"
        readback = {**expected, "note": "", "subject": subject}
        boundary = []
        first_dialog = FakeDialog("Confirm business trip submission")
        first_page = DialogPage(first_dialog)
        patches = (
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._open_and_validate_form",
                return_value=(first_page, FakeFrame()),
            ),
            patch("agentbridge.adapters.seeyon_business_trip_submit._validate_optional_inputs"),
            patch("agentbridge.adapters.seeyon_business_trip_submit._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._read_business_trip_form",
                return_value=readback,
            ),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(
                BusinessTripBusinessValidationRequired
            ) as captured:
                submit_business_trip_request(
                    FakeAdapter(),
                    object(),
                    _plan(expected, subject),
                    enter_commit_boundary=lambda: boundary.append("first"),
                    timeout_seconds=5,
                )

        self.assertEqual(boundary, ["first"])
        self.assertTrue(first_dialog.dismissed)
        self.assertFalse(first_dialog.accepted)
        validation = captured.exception.validation
        self.assertEqual(validation["code"], "NATIVE_CONFIRMATION")
        self.assertTrue(validation["can_continue"])

        resumed_plan = _plan(expected, subject)
        resumed_plan["business_validation_overrides"] = [validation]
        second_dialog = FakeDialog("Confirm business trip submission")
        second_page = DialogPage(second_dialog)
        with (
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._open_and_validate_form",
                return_value=(second_page, FakeFrame()),
            ),
            patch("agentbridge.adapters.seeyon_business_trip_submit._validate_optional_inputs"),
            patch("agentbridge.adapters.seeyon_business_trip_submit._fill_business_trip_form"),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._read_business_trip_form",
                return_value=readback,
            ),
            patch(
                "agentbridge.adapters.seeyon_business_trip_submit._wait_for_sent_readback",
                return_value={
                    "affair_id": "sent-new",
                    "title": subject,
                    "state": "sent",
                    "detail_readable": True,
                    "field_count": 8,
                },
            ),
        ):
            result = submit_business_trip_request(
                FakeAdapter(),
                object(),
                resumed_plan,
                enter_commit_boundary=lambda: boundary.append("second"),
                timeout_seconds=5,
            )

        self.assertEqual(boundary, ["first", "second"])
        self.assertTrue(second_dialog.accepted)
        self.assertFalse(second_dialog.dismissed)
        self.assertTrue(result["workflow_submitted"])

    def test_stale_submit_plan_is_rejected_before_authorization_consumption(self):
        expected = normalize_business_trip_inputs(_inputs())
        plan = _plan(expected, "Subject")
        plan["form_contract"]["fingerprint"] = "sha256:stale"
        boundary = []

        with self.assertRaises(BusinessTripContractMismatch):
            submit_business_trip_request(
                FakeAdapter(),
                object(),
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )

        self.assertEqual(boundary, [])

    def test_authoritative_readback_matches_expanded_business_trip_subject(self):
        inputs = normalize_business_trip_inputs(_inputs())
        title = "-".join(
            (
                BUSINESS_TRIP_TEMPLATE_TITLE,
                "Alice",
                inputs["start_time"],
                inputs["end_time"],
            )
        )
        adapter = FakeAdapter(
            sent_items=[
                {
                    "affair_id": "sent-old",
                    "template_id": BUSINESS_TRIP_TEMPLATE_ID,
                    "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
                    "title": "Old business trip",
                },
                {
                    "affair_id": "sent-new",
                    "template_id": BUSINESS_TRIP_TEMPLATE_ID,
                    "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
                    "title": title,
                },
            ]
        )
        tracker = Mock()
        tracker.pending_business_validation = None

        result = _wait_for_sent_readback(
            adapter,
            object(),
            page=object(),
            baseline_affair_ids={"sent-old"},
            expected_template_id=BUSINESS_TRIP_TEMPLATE_ID,
            expected_form_app_id=BUSINESS_TRIP_FORM_APP_ID,
            title_markers=(
                BUSINESS_TRIP_TEMPLATE_TITLE,
                inputs["start_time"],
                inputs["end_time"],
            ),
            timeout_seconds=5,
            phase_tracker=tracker,
            validation_overrides=[],
        )

        self.assertEqual(result["affair_id"], "sent-new")
        self.assertEqual(result["title"], title)
        self.assertTrue(result["detail_readable"])


class FakeAdapter:
    def __init__(self, sent_items=None):
        self.sent_items = list(sent_items or [])

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

    def load_sent_workflow_rows(self, _worker):
        return list(self.sent_items), object()

    def resolve_sent_workflow_row_detail(self, _worker, *, source_item):
        return source_item, {
            "title": source_item["title"],
            "fields": [{"name": "Applicant", "value": "Alice"}],
        }

    def list_workflows(self, _worker, *, collection, arguments):
        raise AssertionError("the stale home sent projection must not be used")


class FakePage:
    def __init__(self):
        self.click_count = 0
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def locator(self, selector):
        return FakeLocator(self, selector)


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def click(self, **_kwargs):
        if self.selector != "#sendId_a":
            raise AssertionError(f"unexpected click: {self.selector}")
        self.page.click_count += 1
        response_handler = self.page.handlers.get("response")
        if response_handler:
            response_handler(FakeResponse())


class FakeRequest:
    method = "POST"


class FakeResponse:
    url = "http://oa.example.test/seeyon/collaboration/collaboration.do?method=send"
    status = 200
    request = FakeRequest()


class FakeDialog:
    type = "confirm"

    def __init__(self, message):
        self.message = message
        self.accepted = False
        self.dismissed = False

    def accept(self):
        self.accepted = True

    def dismiss(self):
        self.dismissed = True


class DialogPage(FakePage):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog

    def locator(self, selector):
        return DialogLocator(self, selector)


class DialogLocator(FakeLocator):
    def click(self, **_kwargs):
        if self.selector != "#sendId_a":
            raise AssertionError(f"unexpected click: {self.selector}")
        self.page.click_count += 1
        self.page.handlers["dialog"](self.page.dialog)


class FakeFrame:
    pass


def _inputs():
    return {
        "start_time": "2026-07-21 09:00",
        "end_time": "2026-07-21 18:00",
        "travel_mode": "火车",
        "origin": "济南",
        "destination": "青岛",
        "reason": "客户交流",
        "has_direct_supervisor": False,
    }


def _plan(inputs, subject):
    return {
        "business_intent": "submit_business_trip_request",
        "target": {
            "template_title": BUSINESS_TRIP_TEMPLATE_TITLE,
            "template_id": BUSINESS_TRIP_TEMPLATE_ID,
            "form_app_id": BUSINESS_TRIP_FORM_APP_ID,
            "expected_subject": subject,
        },
        "form_contract": {
            "version": BUSINESS_TRIP_SUBMIT_CONTRACT_VERSION,
            "fingerprint": business_trip_submit_contract_fingerprint(),
        },
        "exact_input": inputs,
        "sent_baseline_affair_ids": ["sent-old"],
    }


if __name__ == "__main__":
    unittest.main()
