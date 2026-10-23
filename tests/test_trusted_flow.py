from datetime import datetime, timedelta
import hashlib
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.field_submissions import FieldSubmissionIntegrityError
from agentbridge.core.session_secrets import AesGcmSessionStateProtector, SessionStateStore
from tests.authorization_fixtures import authorized_service, grant_permissions


class TrustedFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        secrets = SessionStateStore(Path(self.temp.name) / "secrets", protector=AesGcmSessionStateProtector(b"x" * 32))
        self.service = authorized_service(home=self.temp.name, base_url="http://oa.example.test/seeyon", session_state_store=secrets)
        self.field_app = TrustedFieldApplication(submission_store=self.service.field_submissions)
        self.action_app = TrustedActionApplication(authorization_store=self.service.write_authorizations)
        self.app = TrustedFlowApplication(service=self.service, field_application=self.field_app, action_application=self.action_app)
        self.task_id, self.endpoint_id = self.task("first")
        self.prepare = self.service.registry.get("taihua.work_log.create.prepare")
        self.commit = self.service.registry.get("taihua.work_log.create")
        self.source = self.source_for(self.task_id)

    def task(self, key):
        result = self.service.ensure_host_task(
            user_subject="user-a", token_id="token-a", agent_host="test-host", host_task_key=key,
            endpoint_key="endpoint-" + key, client_type="web", external_subject="user-a",
            conversation_ref="conversation-" + key, title="日志办理",
        )
        return result["task"]["taskId"], result["endpoint"]["endpointId"]

    def operation(self, spec, task_id, arguments=None, status="requires_user_action"):
        operation, _ = self.service.operations.create(
            user_subject="user-a", capability_name=spec.name, capability_version=spec.version,
            input_summary={}, input_identity=arguments or {},
        )
        if status == "requires_user_action":
            operation = self.service.operations.mark_requires_user_action(
                operation["operation_id"], code="TEST_ACTION", message="test", next_action={},
            )
        elif status == "running":
            operation = self.service.operations.mark_running(operation["operation_id"])
        self.service.tasks.link_operation(task_id=task_id, user_subject="user-a", operation=operation)
        return operation

    def link(self, envelope, task_id):
        record = self.service.interactions.get(envelope["interactionId"], user_subject="user-a")
        self.service.tasks.link_interaction(
            task_id=task_id, user_subject="user-a", interaction_record=record, interaction=envelope,
        )

    def source_for(self, task_id, arguments=None):
        operation = self.operation(self.prepare, task_id)
        source = self.service.field_submissions.create(
            user_subject="user-a", system_id="taihua", session_id="session-a",
            capability_name=self.prepare.name, capability_version=self.prepare.version,
            create_operation_id=operation["operation_id"], supersession_key=operation["operation_id"],
            form_schema={"title": "填写日志", "fields": [{"name": "content", "control": "textarea", "required": True}],
                         "_agentbridge_resume_arguments": arguments or {}},
            card_base_url="http://127.0.0.1:8780",
        )
        envelope = self.service._business_input_interaction(source)
        self.link(envelope, task_id)
        return source

    def open(self, kind="input", resource=None, presentation_id=None, cookie=""):
        resource = resource or self.source
        resource_id = resource["submission_id" if kind == "input" else "authorization_id"]
        response = self.app.get_card(kind, resource_id, presentation_id=presentation_id, secure_cookie=True, csrf_cookie=cookie)
        token = response.headers.get("Set-Cookie", "").split(";", 1)[0].removeprefix("agentbridge_csrf=") or cookie
        return response, token

    def submit_source(self, *, presentation_id=None):
        response, token = self.open(presentation_id=presentation_id)
        self.assertEqual(response.status, 200)
        response = self.app.submit_card(
            "input", self.source["submission_id"], presentation_id=presentation_id,
            body=urlencode({"csrf_token": token, "content": "sensitive work content"}).encode(),
            content_type="application/x-www-form-urlencoded", csrf_cookie=token,
        )
        self.assertEqual(response.status, 200)
        return response, token

    def status(self, kind="input", resource=None, token="", presentation_id=None, cookie=None):
        resource = resource or self.source
        resource_id = resource["submission_id" if kind == "input" else "authorization_id"]
        response = self.app.status(kind, resource_id, presentation_id=presentation_id, token=token,
                                   csrf_cookie=token if cookie is None else cookie)
        return response.status, json.loads(response.body)

    def successor(self, *, task_id=None, arguments=None, link=True, source=None, prepare_operation=None):
        source = source or self.source
        task_id = task_id or self.task_id
        resume = source["form_schema"]["_agentbridge_resume_arguments"]
        operation = prepare_operation or self.operation(
            self.prepare, task_id, {**resume, "input_submission_id": source["submission_id"]},
        )
        authorization = self.service.write_authorizations.create(
            user_subject="user-a", system_id="taihua", session_id="session-a",
            capability_name=self.commit.name, capability_version=self.commit.version,
            prepare_operation_id=operation["operation_id"], supersession_key=operation["operation_id"],
            plan={"prepare_capability": self.prepare.name, "resume_arguments": resume if arguments is None else arguments,
                  "exact_input": {"content": "secret"}},
            summary={"title": "提交日志", "fields": [{"label": "正文", "value": "sensitive work content"}]},
            card_base_url="http://127.0.0.1:8780",
        )
        envelope = self.service._execution_authorization_interaction(authorization)
        current = self.service.field_submissions.get(source["submission_id"])
        if current["state"] == "submitted":
            self.service.field_submissions.consume(
                source["submission_id"], user_subject="user-a", system_id="taihua", session_id="session-a",
                capability_name=self.prepare.name, capability_version=self.prepare.version,
                consume_operation_id=operation["operation_id"],
            )
        # Production prepare consumes the source before Central links the
        # returned interaction and publishes its committed timeline hint.
        if link:
            self.link(envelope, task_id)
        return authorization, envelope

    def approve(self, authorization, presentation_id=None):
        response, token = self.open("authorize", authorization, presentation_id)
        self.assertEqual(response.status, 200)
        response = self.app.submit_card(
            "authorize", authorization["authorization_id"], presentation_id=presentation_id,
            body=urlencode({"csrf_token": token, "decision": "approve"}).encode(),
            content_type="application/x-www-form-urlencoded", csrf_cookie=token,
        )
        self.assertEqual(response.status, 200)
        return token

    def consume(self, authorization, *, task_id=None):
        operation = self.operation(self.commit, task_id or self.task_id, status="running")
        self.service.write_authorizations.consume(
            authorization["authorization_id"], user_subject="user-a", system_id="taihua", session_id="session-a",
            capability_name=self.commit.name, capability_version=self.commit.version,
            commit_operation_id=operation["operation_id"],
        )
        return operation

    def test_continues_only_exact_coordinator_lineage_and_never_invokes_business(self):
        self.service.invoke = Mock(side_effect=AssertionError("page must not invoke"))
        self.service.resume_interaction = Mock(side_effect=AssertionError("page must not coordinate"))
        response, token = self.submit_source()
        self.assertIn(b"/status", response.body)
        self.assertNotIn("请返回智能体继续".encode(), response.body)
        self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(self.status(token=token)[1]["state"], "waiting_coordinator")
        authorization, _ = self.successor()
        response, view = self.status(token=token)
        self.assertEqual(response, 200)
        self.assertEqual(view["nextUrl"], "/flow/authorize/" + authorization["authorization_id"])
        self.assertNotIn("sensitive", json.dumps(view))
        auth_token = self.approve(authorization)
        self.assertEqual(self.status("authorize", authorization, auth_token)[1]["state"], "authorized")
        operation = self.consume(authorization)
        self.assertEqual(self.status("authorize", authorization, auth_token)[1]["state"], "processing")
        self.service.operations.mark_succeeded(operation["operation_id"], {"verification": {"confirmed": True}})
        view = self.status("authorize", authorization, auth_token)[1]
        self.assertEqual(view["state"], "completed")
        self.assertEqual(view["operationId"], operation["operation_id"])
        self.service.invoke.assert_not_called()
        self.service.resume_interaction.assert_not_called()

    def test_old_source_does_not_select_an_unrelated_latest_authorization(self):
        _, token = self.submit_source()
        other_task, _ = self.task("other")
        other = self.source_for(other_task)
        self.successor(task_id=other_task, source=other)
        self.assertEqual(self.status(token=token)[1]["state"], "waiting_coordinator")
        authorization, _ = self.successor()
        self.assertTrue(self.status(token=token)[1]["nextUrl"].endswith(authorization["authorization_id"]))

    def test_task_link_lag_waits_and_cross_task_link_fails_closed(self):
        _, token = self.submit_source()
        _, envelope = self.successor(link=False)
        self.assertEqual(self.status(token=token)[1]["state"], "waiting_coordinator")
        other_task, _ = self.task("other")
        self.link(envelope, other_task)
        self.assertEqual(self.status(token=token)[0], 403)

    def test_changed_business_context_is_not_followed(self):
        _, token = self.submit_source()
        self.successor(arguments={"affair_id": "another-target"})
        self.assertEqual(self.status(token=token)[0], 403)

    def test_presentation_is_carried_to_same_endpoint_and_session_cannot_cross_presentations(self):
        first = self.service.field_submissions.create_presentation(self.source["submission_id"], user_subject="user-a", endpoint_id=self.endpoint_id)
        second = self.service.field_submissions.create_presentation(self.source["submission_id"], user_subject="user-a", endpoint_id="another-endpoint")
        _, token = self.submit_source(presentation_id=first["presentation_id"])
        authorization, _ = self.successor()
        self.assertEqual(self.status(token=token, presentation_id=second["presentation_id"])[0], 403)
        _, state = self.status(token=token, presentation_id=first["presentation_id"])
        self.assertIn("/present/", state["nextUrl"])
        presentation = self.service.write_authorizations.get_presentation(authorization["authorization_id"], state["nextUrl"].split("/")[-1])
        self.assertEqual(presentation["endpoint_id"], self.endpoint_id)

    def test_cross_device_read_resume_cannot_approve_and_does_not_extend_expiry(self):
        self.submit_source()
        authorization, _ = self.successor()
        self.approve(authorization)
        response, read_token = self.open("authorize", authorization)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.status("authorize", authorization, read_token)[1]["state"], "authorized")
        with self.service.write_authorizations._connect() as connection:
            row = connection.execute("SELECT state, expires_at FROM write_authorization_card_sessions WHERE csrf_hash=?", (hashlib.sha256(read_token.encode()).hexdigest(),)).fetchone()
        self.assertEqual(row["state"], "consumed")
        self.assertEqual(row["expires_at"], authorization["expires_at"])
        self.service.write_authorizations.clock = lambda: datetime.fromisoformat(authorization["expires_at"]) + timedelta(seconds=1)
        self.assertEqual(self.status("authorize", authorization, read_token)[0], 403)
        self.assertEqual(self.open("authorize", authorization)[0].status, 403)

    def test_unknown_is_terminal_no_successor_retry_or_private_error(self):
        self.submit_source()
        authorization, _ = self.successor()
        token = self.approve(authorization)
        operation = self.consume(authorization)
        self.service.operations.mark_unknown(operation["operation_id"], code="RESULT_UNKNOWN", message='secret endpoint payload')
        _, state = self.status("authorize", authorization, token)
        self.assertEqual(state["state"], "unknown")
        self.assertTrue(state["terminal"])
        self.assertNotIn("nextUrl", state)
        self.assertNotIn("secret", json.dumps(state))

    def test_final_operation_from_another_task_is_rejected(self):
        self.submit_source()
        authorization, _ = self.successor()
        token = self.approve(authorization)
        other, _ = self.task("other")
        operation = self.consume(authorization, task_id=other)
        self.service.operations.mark_succeeded(operation["operation_id"], {})
        self.assertEqual(self.status("authorize", authorization, token)[0], 403)

    def test_preconsume_commit_failure_matches_only_exact_authorization_and_task(self):
        self.submit_source()
        authorization, _ = self.successor()
        token = self.approve(authorization)
        other = self.operation(self.commit, self.task_id, {"authorization_id": "unrelated"}, status="running")
        self.service.operations.mark_failed(other["operation_id"], code="WRITE_PAUSED", message="private detail")
        other_task, _ = self.task("other")
        foreign = self.operation(self.commit, other_task, {"authorization_id": authorization["authorization_id"]}, status="running")
        self.service.operations.mark_failed(foreign["operation_id"], code="WRITE_PAUSED", message="private detail")
        self.assertEqual(self.status("authorize", authorization, token)[1]["state"], "authorized")
        exact = self.operation(self.commit, self.task_id, {"authorization_id": authorization["authorization_id"]}, status="running")
        for state in ("failed", "unknown"):
            with self.subTest(state=state):
                getattr(self.service.operations, "mark_" + state)(exact["operation_id"], code="WRITE_PAUSED", message="private detail")
                response, view = self.status("authorize", authorization, token)
                self.assertEqual(response, 200)
                self.assertEqual(view["state"], state)
                self.assertEqual(view["operationId"], exact["operation_id"])
                self.assertTrue(view["terminal"])
                self.assertNotIn("private detail", json.dumps(view))
        self.assertIsNone(self.service.write_authorizations.get(authorization["authorization_id"])["commit_operation_id"])

    def test_missing_cookie_wrong_token_and_revoked_permissions_are_rejected(self):
        _, token = self.submit_source()
        self.assertEqual(self.status(token=token, cookie="")[0], 403)
        self.assertEqual(self.status(token="not-a-session")[0], 403)
        grant_permissions(self.service.user_grants, "user-a", [])
        self.assertEqual(self.status(token=token)[0], 403)

    def test_flow_form_retains_csrf_validation_and_legacy_card_is_unchanged(self):
        response, token = self.open()
        path = "/flow/input/" + self.source["submission_id"]
        self.assertIn(f'action="{path}"'.encode(), response.body)
        self.assertIn("Path=" + path, response.headers["Set-Cookie"])
        self.assertIn("Secure", response.headers["Set-Cookie"])
        bad = self.app.submit_card("input", self.source["submission_id"], presentation_id=None,
            body=urlencode({"csrf_token": "wrong", "content": "text"}).encode(),
            content_type="application/x-www-form-urlencoded", csrf_cookie=token)
        self.assertEqual(bad.status, 403)
        self.assertEqual(self.service.field_submissions.get(self.source["submission_id"])["state"], "pending")
        legacy = self.field_app.get_card(self.source["submission_id"], secure_cookie=True)
        self.assertIn(f'action="/input/{self.source["submission_id"]}"'.encode(), legacy.body)

    def test_extra_registered_confirmation_follows_exact_commit_without_replaying(self):
        self.submit_source()
        authorization, _ = self.successor()
        token = self.approve(authorization)
        operation = self.consume(authorization)
        continuation, _ = self.successor(prepare_operation=operation)
        self.service.operations.mark_requires_user_action(operation["operation_id"], code="OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED", message="warning", next_action={})
        _, view = self.status("authorize", authorization, token)
        self.assertEqual(view["nextUrl"], "/flow/authorize/" + continuation["authorization_id"])

    def test_preboundary_warning_reopens_readonly_and_follows_only_its_exact_successor(self):
        self.submit_source()
        authorization, _ = self.successor()
        token = self.approve(authorization)
        operation = self.operation(self.commit, self.task_id,
            {"authorization_id": authorization["authorization_id"]}, status="running")
        continuation, _ = self.successor(prepare_operation=operation)
        self.service.write_authorizations.supersede(authorization["authorization_id"], user_subject="user-a")
        self.service.operations.mark_requires_user_action(operation["operation_id"],
            code="OA_BUSINESS_VALIDATION_CONFIRMATION_REQUIRED", message="warning", next_action={})
        self.assertIsNone(self.service.write_authorizations.get(authorization["authorization_id"])["commit_operation_id"])
        self.assertEqual(self.status("authorize", authorization, token)[1]["nextUrl"],
            "/flow/authorize/" + continuation["authorization_id"])
        reopened, read_token = self.open("authorize", authorization)
        self.assertEqual(reopened.status, 200)
        self.assertEqual(self.status("authorize", authorization, read_token)[1]["nextUrl"],
            "/flow/authorize/" + continuation["authorization_id"])
        denied = self.app.submit_card("authorize", authorization["authorization_id"], presentation_id=None,
            body=urlencode({"csrf_token": read_token, "decision": "approve"}).encode(),
            content_type="application/x-www-form-urlencoded", csrf_cookie=read_token)
        self.assertEqual(denied.status, 410)
        self.assertEqual(self.service.write_authorizations.get(continuation["authorization_id"])["state"], "pending")
        self.service.operations.mark_requires_user_action(operation["operation_id"],
            code="UNRELATED_REASON", message="unrelated", next_action={})
        self.assertNotIn("nextUrl", self.status("authorize", authorization, read_token)[1])
        self.assertEqual(self.open("authorize", authorization)[0].status, 403)

    def test_batch_source_cannot_follow_another_frozen_item(self):
        batch, _ = self.service.tasks.create_batch(parent_task_id=self.task_id, user_subject="user-a", system_id="taihua",
            capability_name=self.prepare.name, selection_summary={}, failure_policy="stop_on_failure",
            items=[{"resource_ref": "affair-a"}, {"resource_ref": "affair-b"}])
        self.source = self.source_for(self.task_id, {"batch_id": batch["batch_id"], "affair_id": "affair-a"})
        _, token = self.submit_source()
        self.successor(arguments={"batch_id": batch["batch_id"], "affair_id": "affair-b"})
        self.assertEqual(self.status(token=token)[0], 403)

    def test_old_batch_item_cannot_render_or_submit_pending_card(self):
        batch, _ = self.service.tasks.create_batch(parent_task_id=self.task_id, user_subject="user-a", system_id="taihua",
            capability_name=self.prepare.name, selection_summary={}, failure_policy="stop_on_failure",
            items=[{"resource_ref": "affair-a"}, {"resource_ref": "affair-b"}])
        self.source = self.source_for(self.task_id, {"batch_id": batch["batch_id"], "affair_id": "affair-a"})
        _, token = self.open()
        operation = self.operation(self.commit, self.task_id, status="running")
        self.service.operations.mark_succeeded(operation["operation_id"], {})
        self.service.tasks.complete_current_batch_item(parent_task_id=self.task_id, user_subject="user-a",
            operation_id=operation["operation_id"], expected_ordinal=1)
        self.assertEqual(self.open()[0].status, 403)
        response = self.app.submit_card("input", self.source["submission_id"], presentation_id=None,
            body=urlencode({"csrf_token": token, "content": "stale"}).encode(),
            content_type="application/x-www-form-urlencoded", csrf_cookie=token)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.status(token=token)[1]["state"], "superseded")
        self.assertEqual(self.service.field_submissions.get(self.source["submission_id"])["state"], "pending")

    def completed_batch_item(self):
        batch, _ = self.service.tasks.create_batch(parent_task_id=self.task_id, user_subject="user-a", system_id="taihua",
            capability_name=self.prepare.name, selection_summary={}, failure_policy="stop_on_failure",
            items=[{"resource_ref": "affair-a", "display_summary": {"title": "第一项"}},
                   {"resource_ref": "affair-b", "display_summary": {"title": "第二项待独立核对"}}])
        self.source = self.source_for(self.task_id, {"batch_id": batch["batch_id"], "affair_id": "affair-a"})
        self.submit_source()
        authorization, _ = self.successor()
        presentation = self.service.write_authorizations.create_presentation(authorization["authorization_id"],
            user_subject="user-a", endpoint_id=self.endpoint_id)
        token = self.approve(authorization, presentation["presentation_id"])
        operation = self.consume(authorization)
        self.service.operations.mark_succeeded(operation["operation_id"], {"verification": {"confirmed": True}})
        self.service.tasks.complete_current_batch_item(parent_task_id=self.task_id, user_subject="user-a",
            operation_id=operation["operation_id"], expected_ordinal=1)
        following = self.source_for(self.task_id, {"batch_id": batch["batch_id"], "affair_id": "affair-b"})
        record = self.service.interactions.find_by_resource(interaction_type="business_input",
            resource_id=following["submission_id"], user_subject="user-a")
        self.service.tasks.record_batch_item_activity(parent_task_id=self.task_id, user_subject="user-a",
            operation_id=following["create_operation_id"], interaction_id=record["interaction_id"])
        return batch, authorization, presentation["presentation_id"], token, operation, following

    def test_completed_batch_item_offers_exact_current_item_without_resuming_or_authorizing(self):
        batch, authorization, presentation, token, operation, following = self.completed_batch_item()
        self.service.invoke = Mock(side_effect=AssertionError("reader cannot invoke"))
        self.service.resume_interaction = Mock(side_effect=AssertionError("reader cannot coordinate"))
        for _ in range(2):
            status, view = self.status("authorize", authorization, token, presentation_id=presentation)
            self.assertEqual(status, 200)
            self.assertEqual(view["state"], "completed")
            self.assertNotIn("nextUrl", view)
            self.assertTrue(view["continueUrl"].startswith(f'/flow/input/{following["submission_id"]}/present/'))
            self.assertEqual(view["batch"], {"completedCount": 1, "totalCount": 2,
                "nextOrdinal": 2, "nextTitle": "第二项待独立核对"})
            next_presentation = self.service.field_submissions.get_presentation(following["submission_id"],
                view["continueUrl"].split("/")[-1])
            self.assertEqual(next_presentation["endpoint_id"], self.endpoint_id)
        self.assertEqual(self.service.field_submissions.get(following["submission_id"])["state"], "pending")
        self.service.invoke.assert_not_called(); self.service.resume_interaction.assert_not_called()
        page, _ = self.open("authorize", authorization, presentation, token)
        self.assertIn(b'id="flow-continue" hidden', page.body)
        self.assertIn(b'continued.href = returnTo?.href || next.href', page.body)

    def test_unknown_failed_or_canceled_batch_cannot_offer_a_next_item(self):
        batch, authorization, presentation, token, operation, following = self.completed_batch_item()
        for state in ("unknown", "failed"):
            getattr(self.service.operations, "mark_" + state)(operation["operation_id"], code="TEST", message="test")
            view = self.status("authorize", authorization, token, presentation_id=presentation)[1]
            self.assertEqual(view["state"], state)
            self.assertNotIn("continueUrl", view)
        self.service.operations.mark_succeeded(operation["operation_id"], {})
        self.service.tasks.cancel_task(task_id=self.task_id, user_subject="user-a", reason="用户取消")
        self.assertNotIn("continueUrl", self.status("authorize", authorization, token, presentation_id=presentation)[1])

    def test_foreign_batch_target_cannot_be_selected_by_old_success_receipt(self):
        batch, authorization, presentation, token, operation, following = self.completed_batch_item()
        other_task, _ = self.task("foreign-next")
        other = self.source_for(other_task)
        original = self.service.interactions.get
        self.service.interactions.get = lambda interaction_id, **kw: {**original(interaction_id, **kw),
            "resource_id": other["submission_id"]}
        self.assertEqual(self.status("authorize", authorization, token, presentation_id=presentation)[0], 403)

    def test_resource_integrity_errors_return_safe_unavailable_response(self):
        _, token = self.open()
        self.service.field_submissions.get = Mock(side_effect=FieldSubmissionIntegrityError("sensitive diagnostic"))
        self.assertEqual(self.open()[0].status, 403)
        response, view = self.status(token=token)
        self.assertEqual(response, 403)
        self.assertNotIn("sensitive", json.dumps(view))

    def test_origin_interaction_operation_must_match_resource(self):
        _, token = self.open()
        original = self.service.interactions.find_by_resource
        self.service.interactions.find_by_resource = lambda **kwargs: {**original(**kwargs), "operation_id": "foreign"}
        self.assertEqual(self.status(token=token)[0], 403)

    def test_http_status_requires_same_origin_and_header_session(self):
        config = validate_auth_server_config(host="127.0.0.1", port=0, public_base_url="http://127.0.0.1:8780", tls_cert=None, tls_key=None)
        server = create_auth_http_server(config=config,
            application=TrustedAuthApplication(challenge_store=self.service.challenges, broker=Mock()),
            field_application=self.field_app, action_application=self.action_app, flow_application=self.app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection(*server.server_address)
        self.addCleanup(connection.close)
        path = "/flow/input/" + self.source["submission_id"]
        connection.request("GET", path)
        response = connection.getresponse()
        token = response.getheader("Set-Cookie").split(";", 1)[0].split("=", 1)[1]
        self.assertEqual(response.status, 200)
        response.read()
        headers = {"Cookie": "agentbridge_csrf=" + token, "X-AgentBridge-Flow-Token": token, "Sec-Fetch-Site": "cross-site"}
        connection.request("GET", path + "/status", headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        headers["Sec-Fetch-Site"] = "same-origin"
        connection.request("GET", path + "/status", headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["state"], "waiting_coordinator")
