import json
import unittest

from agentbridge.adapters.seeyon_pending_submission import (
    PendingSubmissionTracker,
    confirmation_fingerprint,
)


class PendingSubmissionTrackerTests(unittest.TestCase):
    def test_attaches_before_submission_and_removes_only_owned_listeners(self):
        page = FakePage()
        unrelated = lambda _: None
        page.on("response", unrelated)
        tracker = PendingSubmissionTracker()
        tracker.attach(page)
        tracker.attach(page)
        self.assertEqual(page.evaluate_count, 0)
        self.assertEqual(len(page.listeners["response"]), 2)
        page.emit("pageerror", RuntimeError("delayed submission failed"))
        self.assertEqual(tracker.snapshot()["runtime_errors"], ["delayed submission failed"])

        tracker.detach()
        tracker.detach()
        self.assertEqual(page.listeners["response"], [unrelated])
        self.assertTrue(all(not values for key, values in page.listeners.items() if key != "response"))

    def test_partial_attach_failure_cleans_up_and_propagates_before_submit(self):
        page = FakePage(fail_event="dialog")
        tracker = PendingSubmissionTracker()
        with self.assertRaisesRegex(RuntimeError, "observer failed"):
            tracker.attach(page)
        self.assertTrue(all(not values for values in page.listeners.values()))
        tracker.attach(FakePage())
        tracker.detach()

    def test_page_without_observer_cleanup_is_rejected_before_attach(self):
        page = FakePage()
        page.remove_listener = None
        tracker = PendingSubmissionTracker()
        with self.assertRaisesRegex(RuntimeError, "observer cleanup"):
            tracker.attach(page)
        self.assertEqual(page.listeners, {})

    def test_approval_responses_are_diagnostic_not_success_or_rejection(self):
        tracker = PendingSubmissionTracker()
        tracker.observe_response(FakeResponse(
            "/collaboration/collaboration.do?method=finishWorkItem&token=hidden",
            payload={"success": True, "private": "not-returned"},
        ))
        tracker.observe_response(FakeResponse(
            "/collaboration/collaboration.do?method=unknownSecretAction", status=500,
        ))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["response_evidence"][0], {
            "phase": "approval_endpoint_response",
            "endpoint": "/seeyon/collaboration/collaboration.do",
            "operation": "finishWorkItem",
            "status": 200,
        })
        self.assertEqual(snapshot["response_evidence"][1]["phase"], "collaboration_response")
        self.assertIsNone(tracker.definitive_rejection)
        self.assertNotIn("hidden", str(snapshot))
        self.assertNotIn("not-returned", str(snapshot))
        self.assertNotIn("unknownSecretAction", str(snapshot))
        self.assertIn("does not establish", tracker.unknown_outcome_detail())

    def test_ignores_detail_page_get_static_resources_and_unrelated_responses(self):
        tracker = PendingSubmissionTracker()
        for response in [
            FakeResponse("/collaboration/collaboration.do?method=finishWorkItem", method="GET"),
            FakeResponse("/rest/tracking/collect"),
            FakeResponse("/assets/collaboration.js", method="GET"),
        ]:
            tracker.observe_response(response)
        self.assertEqual(tracker.snapshot()["response_evidence"], [])

    def test_failed_approval_request_remains_unknown(self):
        tracker = PendingSubmissionTracker()
        request = FakeRequest("/collaboration/collaboration.do?method=finishWorkItem&cookie=secret")
        tracker.observe_request_failed(request)
        item = tracker.snapshot()["response_evidence"][0]
        self.assertEqual(item["transport_status"], "request_failed")
        self.assertIsNone(tracker.definitive_rejection)
        self.assertNotIn("cookie", str(item))

    def test_recognizes_cap4_validation_without_claiming_no_commit(self):
        tracker = PendingSubmissionTracker()
        tracker.observe_response(FakeResponse("/rest/cap4/form/saveOrUpdate", payload={
            "data": {"success": 0, "code": "3003", "data": {
                "validateResult": json.dumps({
                    "ruleError": "<b>时长不满足规则</b> token=private-session",
                    "forceCheck": 1,
                    "fields": [{"private": "private-form-value"}],
                }),
            }},
        }))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["response_evidence"][0]["business_status"], "form_save_validation")
        self.assertIn("时长不满足规则", snapshot["validations"][0]["message"])
        self.assertNotIn("private-session", str(snapshot))
        self.assertNotIn("private-form-value", str(snapshot))
        self.assertIsNone(tracker.definitive_rejection)

    def test_cap4_save_acceptance_does_not_mean_approval_success(self):
        tracker = PendingSubmissionTracker()
        tracker.observe_response(FakeResponse("/rest/cap4/form/saveOrUpdate", payload={
            "success": 1, "code": "0", "data": {"opinion": "private opinion"},
        }))
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["response_evidence"][0]["business_status"], "form_save_accepted")
        self.assertNotIn("private opinion", str(snapshot))
        self.assertNotIn("confirmed", snapshot)
        self.assertIsNone(tracker.definitive_rejection)

    def test_large_cap4_response_is_not_parsed(self):
        tracker = PendingSubmissionTracker()
        tracker.observe_response(FakeResponse("/rest/cap4/form/saveOrUpdate", payload={
            "success": 0, "code": "3003", "private": "x" * 70000,
        }))
        self.assertEqual(tracker.snapshot()["response_evidence"][0]["business_status"], "unparsed")
        self.assertEqual(tracker.snapshot()["validations"], [])

    def test_unavailable_response_body_preserves_transport_evidence(self):
        tracker = PendingSubmissionTracker()
        response = FakeResponse("/rest/cap4/form/saveOrUpdate")
        response.text = lambda: (_ for _ in ()).throw(Exception("page closed"))
        tracker.observe_response(response)
        self.assertEqual(tracker.snapshot()["response_evidence"][0]["status"], 200)
        self.assertEqual(tracker.snapshot()["response_evidence"][0]["business_status"], "unparsed")

    def test_accepts_only_short_standard_submission_confirmation(self):
        for message in ["是否提交？", "确定提交吗？", "确认提交", "是否确认提交？", "确认提交吗？", "确定要提交吗？"]:
            tracker = PendingSubmissionTracker()
            dialog = FakeDialog("confirm", message)
            tracker.observe_dialog(dialog)
            self.assertEqual(dialog.accept_count, 1, message)
            self.assertEqual(dialog.dismiss_count, 0, message)
            self.assertFalse(tracker.snapshot()["unhandled_confirmation"])

    def test_additional_business_confirmation_is_dismissed_and_recorded(self):
        tracker = PendingSubmissionTracker()
        dialog = FakeDialog("confirm", "提交将跳过经理审批，是否继续？")
        tracker.observe_dialog(dialog)
        self.assertEqual(dialog.accept_count, 0)
        self.assertEqual(dialog.dismiss_count, 1)
        self.assertTrue(tracker.snapshot()["unhandled_confirmation"])
        self.assertIn("not authorized", tracker.unknown_outcome_detail())
        self.assertIsNone(tracker.definitive_rejection)

    def test_only_full_exact_fingerprint_authorizes_nonstandard_confirmation(self):
        message = "需要特殊确认 token=authorized-value"
        tracker = PendingSubmissionTracker({confirmation_fingerprint(message)})
        permitted = FakeDialog("confirm", message)
        tracker.observe_dialog(permitted)
        changed = FakeDialog("confirm", "需要特殊确认 token=different-value")
        tracker.observe_dialog(changed)
        self.assertEqual(permitted.accept_count, 1)
        self.assertEqual(changed.dismiss_count, 1)
        self.assertNotIn("authorized-value", str(tracker.snapshot()))
        self.assertNotIn("different-value", str(tracker.snapshot()))

    def test_truncated_message_cannot_masquerade_as_standard_confirmation(self):
        tracker = PendingSubmissionTracker()
        dialog = FakeDialog("confirm", "是否提交" + " " * 500 + "并跳过审批")
        tracker.observe_dialog(dialog)
        self.assertEqual(dialog.dismiss_count, 1)
        self.assertEqual(dialog.accept_count, 0)

    def test_alerts_are_closed_but_never_interpreted_as_rejection(self):
        tracker = PendingSubmissionTracker()
        for message in ["处理成功", "处理失败", "页面提示"]:
            dialog = FakeDialog("alert", message)
            tracker.observe_dialog(dialog)
            self.assertEqual(dialog.accept_count, 1)
            self.assertIsNone(tracker.definitive_rejection)
        beforeunload = FakeDialog("beforeunload", "Leave?")
        tracker.observe_dialog(beforeunload)
        self.assertEqual(beforeunload.accept_count, 1)

    def test_polls_only_bounded_visible_dom_messages_without_activating_controls(self):
        page = FakePage(messages=[{"kind": "alert", "message": "请填写必填项"}])
        frame = FakePage(messages=[{"kind": "confirmation", "message": "异常金额是否继续"}])
        page.frames = [page, frame]
        page.main_frame = page
        tracker = PendingSubmissionTracker()
        tracker.attach(page)
        tracker.poll()
        tracker.poll()
        snapshot = tracker.snapshot()
        self.assertEqual(len(snapshot["validations"]), 2)
        self.assertTrue(snapshot["unhandled_confirmation"])
        self.assertEqual(page.evaluate_count, 2)
        self.assertEqual(frame.evaluate_count, 2)
        self.assertTrue(all("agentbridge_pending_submission_prompts" in script for script in page.scripts))
        tracker.detach()
        tracker.poll()
        self.assertEqual(page.evaluate_count, 2)

    def test_failure_of_diagnostics_does_not_throw_after_submission(self):
        tracker = PendingSubmissionTracker()
        page = FakePage(evaluate_error=RuntimeError("frame closed token=private-key"))
        tracker.attach(page)
        tracker.poll()
        bad_dialog = FakeDialog("confirm", "是否额外执行操作")
        bad_dialog.dismiss = lambda: (_ for _ in ()).throw(RuntimeError("closed"))
        tracker.observe_dialog(bad_dialog)
        self.assertEqual(len(tracker.snapshot()["observation_errors"]), 2)
        self.assertNotIn("private-key", str(tracker.snapshot()))

    def test_evidence_is_bounded_redacted_and_snapshot_is_independent(self):
        tracker = PendingSubmissionTracker()
        for index in range(40):
            tracker.observe_response(FakeResponse("/ajax.do?method=ajaxAction&token=private"))
            tracker.observe_page_error(
                f"error {index}: https://oa.test/path?token=secret Bearer bearer-secret "
                'password="password secret" sessionid=private-session ' + "x" * 1000
            )
        snapshot = tracker.snapshot()
        self.assertEqual(len(snapshot["response_evidence"]), 20)
        self.assertEqual(len(snapshot["runtime_errors"]), 5)
        self.assertTrue(all(len(item) <= 300 for item in snapshot["runtime_errors"]))
        for secret in ("bearer-secret", "password secret", "private-session", "https://oa.test", "token=private"):
            self.assertNotIn(secret, str(snapshot))
        snapshot["response_evidence"][0]["status"] = 999
        self.assertEqual(tracker.snapshot()["response_evidence"][0]["status"], 200)


class FakeRequest:
    def __init__(self, path, method="POST"):
        self.url = "https://oa.example.test/seeyon" + path
        self.method = method
        self.post_data = "opinion=private-opinion&token=private-token"
        self.failure = "untrusted failure token=private-token"


class FakeResponse:
    def __init__(self, path, method="POST", status=200, payload=None):
        self.request = FakeRequest(path, method)
        self.url = self.request.url
        self.status = status
        self.payload = payload

    def text(self):
        return json.dumps(self.payload)


class FakeDialog:
    def __init__(self, kind, message):
        self.type = kind
        self.message = message
        self.accept_count = 0
        self.dismiss_count = 0

    def accept(self):
        self.accept_count += 1

    def dismiss(self):
        self.dismiss_count += 1


class FakePage:
    def __init__(self, messages=None, fail_event=None, evaluate_error=None):
        self.messages = messages or []
        self.fail_event = fail_event
        self.evaluate_error = evaluate_error
        self.frames = []
        self.main_frame = self
        self.listeners = {}
        self.evaluate_count = 0
        self.scripts = []

    def on(self, event, handler):
        if event == self.fail_event:
            raise RuntimeError("observer failed")
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.listeners[event].remove(handler)

    def emit(self, event, payload):
        for handler in self.listeners.get(event, []):
            handler(payload)

    def evaluate(self, script):
        self.evaluate_count += 1
        self.scripts.append(script)
        if self.evaluate_error:
            raise self.evaluate_error
        return self.messages


if __name__ == "__main__":
    unittest.main()
