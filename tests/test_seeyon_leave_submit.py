import unittest
from unittest.mock import Mock, patch

from agentbridge.adapters.seeyon_leave import (
    LEAVE_FORM_APP_ID,
    LEAVE_TEMPLATE_ID,
    LEAVE_TEMPLATE_TITLE,
    LeaveContractMismatch,
    LeaveOutcomeUnknown,
    normalize_leave_inputs,
)
from agentbridge.adapters.seeyon_leave_submit import (
    LEAVE_SUBMIT_CONTRACT_VERSION,
    LeaveBusinessValidationRequired,
    LeaveSubmissionBlocked,
    _handle_business_validation,
    _wait_for_sent_readback,
    leave_submit_contract_fingerprint,
    prepare_leave_submission,
    submit_leave_request,
)


class SeeyonLeaveSubmitTests(unittest.TestCase):
    def test_prepare_freezes_subject_and_sent_baseline_without_clicking(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_leave_inputs(_inputs())
        readback = {
            **expected,
            "subject": "【HR】请假申请单-{申请人}-{请假类型}",
            "leave_days": "",
            "leave_hours": "",
        }
        adapter = FakeAdapter(sent_items=[{"affair_id": "sent-old", "title": "Old"}])
        with (
            patch(
                "agentbridge.adapters.seeyon_leave_submit._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave_submit._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave_submit._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave_submit._read_leave_form",
                return_value=readback,
            ),
        ):
            prepared = prepare_leave_submission(adapter, object(), _inputs())

        plan = prepared["plan"]
        self.assertEqual(plan["target"]["expected_subject"], readback["subject"])
        self.assertEqual(plan["target"]["template_id"], LEAVE_TEMPLATE_ID)
        self.assertEqual(plan["target"]["form_app_id"], LEAVE_FORM_APP_ID)
        self.assertEqual(plan["sent_baseline_affair_ids"], ["sent-old"])
        self.assertTrue(plan["expected_effect"]["workflow_submitted"])
        self.assertEqual(prepared["summary"]["authorize_label"], "授权提交审批")
        self.assertIn("正式提交", prepared["summary"]["authorization_notice"])
        self.assertEqual(page.click_count, 0)

    def test_commit_consumes_authorization_before_send_and_verifies_sent_readback(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_leave_inputs(_inputs())
        subject = "【HR】请假申请单-{申请人}-{请假类型}"
        readback = {
            **expected,
            "subject": subject,
            "leave_days": "",
            "leave_hours": "",
        }
        boundary = []
        worker = FakeForkingWorker()
        with (
            patch(
                "agentbridge.adapters.seeyon_leave_submit._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave_submit._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave_submit._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave_submit._read_leave_form",
                return_value=readback,
            ),
            patch(
                "agentbridge.adapters.seeyon_leave_submit._wait_for_sent_readback",
                return_value={
                    "affair_id": "sent-new",
                    "title": "【HR】请假申请单-Alice-年休",
                    "state": "sent",
                    "detail_readable": True,
                    "field_count": 7,
                },
            ) as wait_for_sent,
        ):
            result = submit_leave_request(
                FakeAdapter(),
                worker,
                _plan(expected, subject),
                enter_commit_boundary=lambda: boundary.append(page.click_count),
                timeout_seconds=5,
            )

        self.assertEqual(boundary, [0])
        self.assertEqual(page.clicked_selector, "#sendId_a")
        self.assertEqual(page.click_count, 1)
        self.assertTrue(result["workflow_submitted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["submitted"]["affair_id"], "sent-new")
        self.assertEqual(result["request_evidence"][0]["method"], "POST")
        self.assertIs(wait_for_sent.call_args.args[1], worker.readback_worker)
        self.assertTrue(worker.readback_closed)

    def test_post_send_readback_failure_is_outcome_unknown(self):
        page = FakePage()
        frame = FakeFrame()
        expected = normalize_leave_inputs(_inputs())
        subject = "【HR】请假申请单-{申请人}-{请假类型}"
        readback = {
            **expected,
            "subject": subject,
            "leave_days": "",
            "leave_hours": "",
        }
        boundary = []
        with (
            patch(
                "agentbridge.adapters.seeyon_leave_submit._open_and_validate_form",
                return_value=(page, frame),
            ),
            patch("agentbridge.adapters.seeyon_leave_submit._validate_supported_option"),
            patch("agentbridge.adapters.seeyon_leave_submit._fill_leave_form"),
            patch(
                "agentbridge.adapters.seeyon_leave_submit._read_leave_form",
                return_value=readback,
            ),
            patch(
                "agentbridge.adapters.seeyon_leave_submit._wait_for_sent_readback",
                side_effect=LeaveOutcomeUnknown("not confirmed"),
            ),
        ):
            with self.assertRaises(LeaveOutcomeUnknown):
                submit_leave_request(
                    FakeAdapter(),
                    object(),
                    _plan(expected, subject),
                    enter_commit_boundary=lambda: boundary.append("consumed"),
                    timeout_seconds=5,
                )

        self.assertEqual(boundary, ["consumed"])
        self.assertEqual(page.click_count, 1)

    def test_stale_submit_plan_is_rejected_before_authorization_consumption(self):
        expected = normalize_leave_inputs(_inputs())
        plan = _plan(expected, "Subject")
        plan["form_contract"]["fingerprint"] = "sha256:stale"
        boundary = []

        with self.assertRaises(LeaveContractMismatch):
            submit_leave_request(
                FakeAdapter(),
                object(),
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )

        self.assertEqual(boundary, [])

    def test_continuable_validation_requires_matching_explicit_override(self):
        validation = {
            "code": "3003",
            "message": "请假时长需要确认",
            "force_check": False,
            "can_continue": True,
            "fingerprint": "sha256:validation",
            "control_selector": "#verifySure",
        }
        page = ValidationPage()
        tracker = ValidationTracker(validation)
        with self.assertRaises(LeaveBusinessValidationRequired):
            _handle_business_validation(page, tracker, validation_overrides=[])
        self.assertEqual(page.continue_clicks, 0)

        with self.assertRaises(LeaveBusinessValidationRequired):
            _handle_business_validation(
                page,
                ValidationTracker(validation),
                validation_overrides=[{"fingerprint": "sha256:changed"}],
            )
        self.assertEqual(page.continue_clicks, 0)

        tracker = ValidationTracker(validation)
        _handle_business_validation(
            page,
            tracker,
            validation_overrides=[{"fingerprint": validation["fingerprint"]}],
        )
        self.assertEqual(page.continue_clicks, 1)
        self.assertIsNone(tracker.pending_business_validation)

        frame = ValidationFrame()
        framed_validation = {
            **validation,
            "control_frame_url": frame.url,
        }
        framed_tracker = ValidationTracker(framed_validation)
        _handle_business_validation(
            ValidationFramedPage(frame),
            framed_tracker,
            validation_overrides=[{"fingerprint": validation["fingerprint"]}],
        )
        self.assertEqual(frame.continue_clicks, 1)
        self.assertIsNone(framed_tracker.pending_business_validation)

        activated_validation = {
            **validation,
            "control_already_activated": True,
        }
        activated_tracker = ValidationTracker(activated_validation)
        _handle_business_validation(
            object(),
            activated_tracker,
            validation_overrides=[{"fingerprint": validation["fingerprint"]}],
        )
        self.assertIsNone(activated_tracker.pending_business_validation)

        blocked = {**validation, "can_continue": False}
        with self.assertRaises(LeaveSubmissionBlocked):
            _handle_business_validation(
                page,
                ValidationTracker(blocked),
                validation_overrides=[],
            )

    def test_authoritative_readback_matches_expanded_leave_subject(self):
        inputs = normalize_leave_inputs(_inputs())
        title = "-".join((LEAVE_TEMPLATE_TITLE, "Alice", inputs["leave_type"]))
        adapter = FakeAdapter(
            sent_items=[
                {
                    "affair_id": "sent-old",
                    "template_id": LEAVE_TEMPLATE_ID,
                    "form_app_id": LEAVE_FORM_APP_ID,
                    "title": "Old leave",
                },
                {
                    "affair_id": "sent-new",
                    "template_id": LEAVE_TEMPLATE_ID,
                    "form_app_id": LEAVE_FORM_APP_ID,
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
            expected_template_id=LEAVE_TEMPLATE_ID,
            expected_form_app_id=LEAVE_FORM_APP_ID,
            title_markers=(LEAVE_TEMPLATE_TITLE, inputs["leave_type"]),
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
                    "title": LEAVE_TEMPLATE_TITLE,
                    "template_id": LEAVE_TEMPLATE_ID,
                    "form_app_id": LEAVE_FORM_APP_ID,
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


class FakeForkingWorker:
    def __init__(self):
        self.readback_worker = object()
        self.readback_closed = False

    def fork_page(self):
        return FakeForkedWorkerContext(self)


class FakeForkedWorkerContext:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self.owner.readback_worker

    def __exit__(self, _exc_type, _exc, _traceback):
        self.owner.readback_closed = True


class FakePage:
    def __init__(self):
        self.click_count = 0
        self.clicked_selector = None
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
        self.page.clicked_selector = self.selector
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


class FakeFrame:
    pass


class ValidationTracker:
    def __init__(self, validation):
        self.pending_business_validation = dict(validation)
        self.continued = set()

    def mark_business_validation_continued(self):
        self.continued.add(self.pending_business_validation["fingerprint"])
        self.pending_business_validation = None

    def business_validation_was_continued(self, fingerprint):
        return fingerprint in self.continued


class ValidationPage:
    def __init__(self):
        self.continue_clicks = 0

    def get_by_text(self, text, *, exact):
        if text != "继续" or not exact:
            raise AssertionError("unexpected validation control lookup")
        return ValidationLocatorCollection(self)

    def locator(self, selector):
        if selector != "#verifySure:visible":
            raise AssertionError("unexpected validation selector")
        return ValidationLocatorCollection(self)


class ValidationFrame(ValidationPage):
    url = "http://oa.example.test/cap4/form"


class ValidationFramedPage:
    def __init__(self, frame):
        self.frames = [frame]


class ValidationLocatorCollection:
    def __init__(self, page):
        self.page = page

    def count(self):
        return 1

    @property
    def last(self):
        return self.nth(0)

    def nth(self, index):
        if index != 0:
            raise AssertionError("unexpected validation control index")
        return ValidationLocator(self.page)


class ValidationLocator:
    def __init__(self, page):
        self.page = page

    def is_visible(self):
        return True

    def wait_for(self, **_kwargs):
        return None

    def click(self, **_kwargs):
        self.page.continue_clicks += 1


def _inputs():
    return {
        "leave_type": "年休",
        "start_time": "2026-07-22 09:00",
        "end_time": "2026-07-22 18:00",
        "reason": "个人事务",
        "has_direct_supervisor": False,
    }


def _plan(inputs, subject):
    return {
        "business_intent": "submit_leave_request",
        "target": {
            "template_title": LEAVE_TEMPLATE_TITLE,
            "template_id": LEAVE_TEMPLATE_ID,
            "form_app_id": LEAVE_FORM_APP_ID,
            "expected_subject": subject,
        },
        "form_contract": {
            "version": LEAVE_SUBMIT_CONTRACT_VERSION,
            "fingerprint": leave_submit_contract_fingerprint(),
        },
        "exact_input": inputs,
        "sent_baseline_affair_ids": ["sent-old"],
    }


if __name__ == "__main__":
    unittest.main()