from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from agentbridge.adapters.taihua import TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA
from agentbridge.auth.business_draft_card import BusinessDraftApplication, business_draft_target
from agentbridge.auth.card import AuthCardResponse
from agentbridge.auth.flow import TrustedFlowApplication


SOURCE_ID = "s" * 43
PRESENTATION_ID = "p" * 43


def draft_state():
    return {"draftId": "draft-id", "revision": 0, "version": 0, "state": "editable",
        "expiresAt": "2099-01-01T00:00:00+00:00", "allowedActions": ["save", "preview"],
        "values": {"log_date": "2026-10-13", "hours": "8", "project": "P001", "content": "日志正文"},
        "preview": None, "receipt": None}


class BusinessDraftCardTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.state = draft_state()
        self.backend.open.return_value = {"state": self.state, "csrfToken": "session-token", "maxAge": 500}
        self.backend.state.return_value = self.state
        self.app = BusinessDraftApplication(draft_service=self.backend)

    def context(self, marker=True):
        schema = deepcopy(TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA)
        if marker:
            schema["_agentbridge_business_draft"] = "v1"
        return SimpleNamespace(kind="input", task_id="task-id", actionable=True,
            resource={"capability_name": "taihua.work_log.create.prepare", "form_schema": schema, "state": "pending"})

    def test_page_uses_four_existing_controls_and_preserves_trusted_security_headers(self):
        response = self.app.get_card(SOURCE_ID, presentation_id=PRESENTATION_ID,
            csrf_cookie="existing-cookie", secure_cookie=True)
        body = response.body.decode()
        self.assertEqual(response.status, 200)
        for name in ("log_date", "hours", "project", "content"):
            self.assertIn(f'id="{name}"', body)
        self.assertIn('id="draft-submit" type="submit" disabled>确认并提交日志', body)
        self.assertIn('method="post"', body)
        self.assertIn("项目编码", body)
        self.assertIn("重复检查", body)
        self.assertIn("执行后果", body)
        self.assertNotIn("localStorage", body)
        self.assertNotIn("sessionStorage", body)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
        self.assertIn(f"Path=/flow/input/{SOURCE_ID}/present/{PRESENTATION_ID};", response.headers["Set-Cookie"])
        self.assertIn("HttpOnly; SameSite=Strict", response.headers["Set-Cookie"])
        self.assertIn("; Secure", response.headers["Set-Cookie"])
        self.backend.open.assert_called_once_with(source_submission_id=SOURCE_ID,
            presentation_id=PRESENTATION_ID, csrf_cookie="existing-cookie")

    def test_business_values_cannot_escape_control_or_bootstrap_script(self):
        self.state["values"]["content"] = '</textarea></script><script>alert("private")</script>'
        response = self.app.get_card(SOURCE_ID, presentation_id=None, csrf_cookie="", secure_cookie=False)
        body = response.body.decode()
        self.assertNotIn(self.state["values"]["content"], body)
        self.assertIn("&lt;/textarea&gt;", body)
        self.assertIn("\\u003c/script>", body)
        self.assertEqual(body.count("</script>"), 2)

    def test_only_new_marked_bound_taihua_sources_upgrade_while_legacy_keeps_form(self):
        self.assertTrue(self.app.supports(self.context()))
        for key, value in (("task_id", None), ("kind", "authorize")):
            context = self.context()
            setattr(context, key, value)
            self.assertFalse(self.app.supports(context))
        context = self.context(False)
        self.assertFalse(self.app.supports(context))
        fields = Mock()
        fields.get_card.return_value = AuthCardResponse(200, {}, f'<form action="/input/{SOURCE_ID}"></form>'.encode())
        flow = TrustedFlowApplication(service=Mock(), field_application=fields,
            action_application=Mock(), draft_application=self.app)
        flow.reader.context = Mock(return_value=context)
        response = flow.get_card("input", SOURCE_ID, presentation_id=None, secure_cookie=True, csrf_cookie="")
        self.assertIn(f'action="/flow/input/{SOURCE_ID}"'.encode(), response.body)
        self.backend.open.assert_not_called()
        flow.reader.context.return_value = self.context()
        upgraded = flow.get_card("input", SOURCE_ID, presentation_id=None, secure_cookie=True, csrf_cookie="")
        self.assertIn(b'id="draft-form"', upgraded.body)
        self.backend.open.assert_called_once()

    def test_route_parser_is_exact_and_keeps_presentation_scope(self):
        path = f"/flow/input/{SOURCE_ID}/present/{PRESENTATION_ID}/draft/preview"
        self.assertEqual(business_draft_target(path), (SOURCE_ID, PRESENTATION_ID, "preview"))
        self.assertIsNone(business_draft_target(path + "/other"))
        self.assertIsNone(business_draft_target(path.replace("/input/", "/authorize/")))
        self.assertIsNone(business_draft_target("/flow/input/short/draft/state"))

    def test_marked_source_submitted_through_legacy_form_observes_original_flow(self):
        self.backend.has_decision.return_value = False
        flow = TrustedFlowApplication(service=Mock(), field_application=Mock(),
            action_application=Mock(), draft_application=self.app)
        flow.reader.authenticate = Mock()
        # This also covers an editing draft opened before the legacy form was used:
        # draft existence alone must not route a submitted source back into editing.
        for state in ("submitted", "consumed"):
            with self.subTest(state=state):
                context = self.context()
                context.resource["state"] = state
                flow.reader.context = Mock(return_value=context)
                response = flow.get_card("input", SOURCE_ID, presentation_id=PRESENTATION_ID,
                    secure_cookie=True, csrf_cookie="source-session")
                self.assertEqual(response.status, 200)
                self.assertIn(f"/flow/input/{SOURCE_ID}/present/{PRESENTATION_ID}/status".encode(), response.body)
                self.assertNotIn(b'id="draft-form"', response.body)
        self.backend.open.assert_not_called()
        self.backend.has_decision.assert_called_with(source_submission_id=SOURCE_ID)

    def test_finalized_draft_receipt_remains_recoverable_after_source_submission(self):
        self.backend.has_decision.return_value = True
        self.state.update(state="finalized", allowedActions=[],
            receipt={"requestKey": "previous-decision", "flowUrl": "/flow/authorize/" + "a" * 43})
        context = self.context()
        context.resource["state"] = "consumed"
        flow = TrustedFlowApplication(service=Mock(), field_application=Mock(),
            action_application=Mock(), draft_application=self.app)
        flow.reader.context = Mock(return_value=context)
        response = flow.get_card("input", SOURCE_ID, presentation_id=None,
            secure_cookie=True, csrf_cookie="source-session")
        self.assertEqual(response.status, 200)
        self.assertIn(b'"state": "finalized"', response.body)
        self.assertIn(b'"flowUrl": "/flow/authorize/', response.body)
        self.backend.open.assert_called_once_with(source_submission_id=SOURCE_ID,
            presentation_id=None, csrf_cookie="source-session")

    def test_state_and_decision_forward_csrf_and_exact_revision_without_plan(self):
        response = self.app.state(SOURCE_ID, presentation_id=PRESENTATION_ID, token="token", csrf_cookie="cookie")
        self.assertEqual(response.status, 200)
        self.backend.state.assert_called_once_with(source_submission_id=SOURCE_ID,
            presentation_id=PRESENTATION_ID, csrf_token="token", csrf_cookie="cookie")
        self.backend.approve.return_value = self.state
        response = self.app.mutate(SOURCE_ID, presentation_id=PRESENTATION_ID, action="approve",
            token="token", csrf_cookie="cookie", content_type="application/json",
            body=json.dumps({"expectedRevision": 4, "previewId": "preview-4", "requestKey": "decision-key"}).encode())
        self.assertEqual(response.status, 200)
        self.backend.approve.assert_called_once_with(source_submission_id=SOURCE_ID,
            presentation_id=PRESENTATION_ID, csrf_token="token", csrf_cookie="cookie",
            expected_revision=4, preview_id="preview-4", request_key="decision-key")

    def test_decision_rejects_extra_plan_fields_wrong_types_and_oversized_payload(self):
        for payload in (
            {"expectedRevision": 1, "previewId": "p", "requestKey": "r", "plan": {}},
            {"expectedRevision": True, "previewId": "p", "requestKey": "r"},
            {"expectedRevision": 1, "previewId": {}, "requestKey": "r"},
        ):
            with self.subTest(payload=payload):
                response = self.app.mutate(SOURCE_ID, presentation_id=None, action="approve", token="t",
                    csrf_cookie="t", content_type="application/json", body=json.dumps(payload).encode())
                self.assertEqual(response.status, 400)
        self.backend.approve.assert_not_called()

    def test_explicit_conflict_returns_safe_error_and_current_trusted_values(self):
        from agentbridge.core.business_drafts import BusinessDraftError
        self.backend.preview.side_effect = BusinessDraftError("DRAFT_REVISION_CONFLICT", "另一页已更新", status=409,
            current={**self.state, "revision": 2, "version": 4})
        response = self.app.mutate(SOURCE_ID, presentation_id=None, action="preview", token="t",
            csrf_cookie="t", content_type="application/json", body=b'{"expectedRevision":0}')
        result = json.loads(response.body)
        self.assertEqual(response.status, 409)
        self.assertEqual(result["error"]["code"], "DRAFT_REVISION_CONFLICT")
        self.assertEqual(result["current"]["revision"], 2)

    def test_unexpected_backend_errors_do_not_expose_raw_business_payload(self):
        self.backend.state.side_effect = RuntimeError("private cookie=secret raw downstream response")
        response = self.app.state(SOURCE_ID, presentation_id=None, token="t", csrf_cookie="t")
        self.assertEqual(response.status, 503)
        self.assertNotIn(b"private", response.body)
        self.assertNotIn(b"cookie", response.body)


if __name__ == "__main__":
    unittest.main()
