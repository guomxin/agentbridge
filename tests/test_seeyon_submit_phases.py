import json
import unittest

from agentbridge.adapters.seeyon_submit_phases import (
    SubmissionPhaseTracker,
    pump_browser_events,
)


class SubmissionPhaseTrackerTests(unittest.TestCase):
    def test_tracks_only_sanitized_known_submission_phases(self):
        tracker = SubmissionPhaseTracker()
        tracker.observe_response(
            FakeResponse(
                "http://oa.example.test/seeyon/ajax.do?method=ajaxAction",
                post_data=(
                    "managerName=colManager&"
                    "managerMethod=checkAffairAndLock4NewColJson&secret=form-value"
                ),
            )
        )
        tracker.observe_response(
            FakeResponse(
                "http://oa.example.test/seeyon/ajax.do?method=ajaxAction",
                post_data="managerName=colManager&managerMethod=checkTemplate",
            )
        )
        tracker.observe_response(
            FakeResponse(
                "http://oa.example.test/seeyon/rest/cap4/form/saveOrUpdate",
                post_data="reason=private-value",
            )
        )
        tracker.observe_response(
            FakeResponse(
                "http://oa.example.test/seeyon/collaboration/collaboration.do?method=send",
                post_data="subject=private-value",
            )
        )

        self.assertEqual(
            [item["phase"] for item in tracker.evidence],
            [
                "affair_lock_check",
                "template_check",
                "cap4_form_save",
                "workflow_send",
            ],
        )
        self.assertEqual(tracker.evidence[-1]["operation"], "send")
        self.assertNotIn("private-value", str(tracker.evidence))
        self.assertIn("final workflow send was observed", tracker.unknown_outcome_detail())

    def test_pumps_playwright_events_during_server_readback(self):
        page = FakeWaitPage()

        pump_browser_events(page)

        self.assertEqual(page.waits, [250])

    def test_ignores_unrelated_requests_and_reports_last_safe_phase(self):
        tracker = SubmissionPhaseTracker()
        tracker.observe_response(FakeResponse("http://oa.example.test/seeyon/rest/track/log"))
        tracker.observe_response(
            FakeResponse("http://oa.example.test/seeyon/rest/cap4/form/saveOrUpdate")
        )

        self.assertEqual(len(tracker.evidence), 1)
        self.assertEqual(tracker.evidence[0]["phase"], "cap4_form_save")
        self.assertIn("final workflow send was not observed", tracker.unknown_outcome_detail())

    def test_extracts_only_sanitized_continuable_cap4_validation(self):
        tracker = SubmissionPhaseTracker()
        tracker.observe_response(
            FakeResponse(
                "http://oa.example.test/seeyon/rest/cap4/form/saveOrUpdate",
                payload={
                    "data": {
                        "success": 0,
                        "code": "3003",
                        "data": {
                            "validateResult": json.dumps(
                                {
                                    "ruleError": "<b>请假时长</b>\n  需要确认",
                                    "forceCheck": 0,
                                    "fields": [{"private": "must-not-leak"}],
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                },
            )
        )

        validation = tracker.pending_business_validation
        self.assertEqual(validation["code"], "3003")
        self.assertEqual(validation["message"], "请假时长 需要确认")
        self.assertTrue(validation["can_continue"])
        self.assertTrue(validation["fingerprint"].startswith("sha256:"))
        self.assertNotIn("must-not-leak", str(tracker.evidence))
        self.assertEqual(
            tracker.evidence[-1]["businessStatus"],
            "validation_required",
        )

    def test_detects_page_continue_prompt_as_a_separate_confirmation(self):
        tracker = SubmissionPhaseTracker()
        page = FakeConfirmationPage()

        tracker.observe_page_confirmation(page)
        tracker.observe_page_confirmation(page)

        validation = tracker.pending_business_validation
        self.assertEqual(validation["code"], "PRE_SUBMIT_CONFIRMATION")
        self.assertEqual(validation["message"], "请确认 自动汇总提示")
        self.assertEqual(validation["control_selector"], "#verifySure")
        self.assertEqual(len(tracker.evidence), 1)
        self.assertEqual(tracker.evidence[0]["phase"], "pre_submit_confirmation")
        self.assertNotIn(validation["message"], str(tracker.evidence))

    def test_detects_confirmation_inside_child_frame(self):
        tracker = SubmissionPhaseTracker()
        frame = FakeConfirmationFrame()
        page = FakeFramedConfirmationPage(frame)

        tracker.observe_page_confirmation(page)

        validation = tracker.pending_business_validation
        self.assertEqual(validation["control_frame_url"], frame.url)
        self.assertEqual(validation["message"], "请确认 自动汇总提示")


    def test_captures_hooked_strong_blocker(self):
        tracker = SubmissionPhaseTracker()
        page = FakeHookedPromptPage(
            {
                "kind": "alert",
                "message": "\u6d41\u7a0b\u542f\u52a8\u6761\u4ef6\u4e0d\u6ee1\u8db3",
                "buttons": [],
            }
        )

        tracker.observe_page_confirmation(page)

        validation = tracker.pending_business_validation
        self.assertEqual(validation["code"], "OA_PAGE_BLOCKER")
        self.assertFalse(validation["can_continue"])
        self.assertEqual(
            validation["message"],
            "\u6d41\u7a0b\u542f\u52a8\u6761\u4ef6\u4e0d\u6ee1\u8db3",
        )
        self.assertNotIn(validation["message"], str(tracker.evidence))

    def test_native_confirmation_is_dismissed_until_its_fingerprint_is_authorized(self):
        tracker = SubmissionPhaseTracker()
        first_dialog = FakeDialog(
            "confirm",
            "\u662f\u5426\u7ee7\u7eed\u542f\u52a8\u6d41\u7a0b",
        )

        tracker.observe_dialog(first_dialog)

        validation = tracker.pending_business_validation
        self.assertEqual(first_dialog.dismiss_count, 1)
        self.assertEqual(first_dialog.accept_count, 0)
        self.assertTrue(validation["can_continue"])

        authorized = SubmissionPhaseTracker({validation["fingerprint"]})
        replay_dialog = FakeDialog(
            "confirm",
            "\u662f\u5426\u7ee7\u7eed\u542f\u52a8\u6d41\u7a0b",
        )
        authorized.observe_dialog(replay_dialog)

        replay_validation = authorized.pending_business_validation
        self.assertEqual(replay_dialog.accept_count, 1)
        self.assertEqual(replay_dialog.dismiss_count, 0)
        self.assertTrue(replay_validation["control_already_activated"])

    def test_parses_cap4_result_wrapped_in_json_text(self):
        tracker = SubmissionPhaseTracker()
        tracker.observe_response(
            FakeResponse(
                "http://oa.example.test/seeyon/rest/cap4/form/saveOrUpdate",
                payload={
                    "payload": json.dumps(
                        {"result": {"success": "1", "code": "0"}}
                    )
                },
            )
        )

        evidence = tracker.evidence[-1]
        self.assertEqual(evidence["businessStatus"], "accepted")
        self.assertEqual(evidence["businessCode"], "0")
        self.assertIn("CAP4 save count: 1", tracker.unknown_outcome_detail())


class FakeWaitPage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class FakeConfirmationPage:
    def evaluate(self, _script):
        return {
            "confirmText": "\u7ee7\u7eed",
            "cancelText": "\u53d6\u6d88",
            "message": "\u8bf7\u786e\u8ba4\n  \u81ea\u52a8\u6c47\u603b\u63d0\u793a",
        }


class FakeConfirmationFrame(FakeConfirmationPage):
    url = "http://oa.example.test/cap4/form"


class FakeFramedConfirmationPage:
    def __init__(self, frame):
        self.frames = [frame]
        self.main_frame = None

    def evaluate(self, _script):
        return None


class FakeHookedPromptPage:
    def __init__(self, event):
        self.event = dict(event)
        self.frames = []
        self.main_frame = None

    def evaluate(self, script):
        if "queue.shift()" in script:
            event = self.event
            self.event = None
            return event
        return None


class FakeDialog:
    def __init__(self, dialog_type, message):
        self.type = dialog_type
        self.message = message
        self.accept_count = 0
        self.dismiss_count = 0

    def accept(self):
        self.accept_count += 1

    def dismiss(self):
        self.dismiss_count += 1


class FakeRequest:
    def __init__(self, *, post_data=""):
        self.method = "POST"
        self.post_data = post_data


class FakeResponse:
    def __init__(self, url, *, status=200, post_data="", payload=None):
        self.url = url
        self.status = status
        self.request = FakeRequest(post_data=post_data)
        self.payload = payload

    def json(self):
        return self.payload


if __name__ == "__main__":
    unittest.main()
