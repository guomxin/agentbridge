"""Closed shortcut reads retain durable intent, endpoint and host fencing."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core import actionable_results as actions, quick_queries as queries
from agentbridge.core.tasks import TaskIntegrityError
from tests.authorization_fixtures import grant_permissions
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, JsonHttpClient, OWNER
from tests.test_workspace import PASSWORD


class QuickQueryTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = LatencyAcceptanceFixture(Path(self.temp.name), seed=False,
            latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
        self.addCleanup(self.fixture.close)
        self.service, self.account = self.fixture.service, self.fixture.account
        grant_permissions(self.service.user_grants, OWNER, ["oa.workflow.read"])
        self.context = {"endpointId": self.account["endpoint_id"], "hostType": "openclaw", "hostInstanceId": "synthetic-host"}

    def queue(self, body=None):
        return queries.queue_query(self.service, self.account, body or {"requestKey": uuid4().hex})

    def execute(self, queued):
        return actions.execute_preparation(self.service, OWNER, queued["taskId"], queued["commandKey"], runtime_context=self.context)

    def count(self, table):
        with self.service.tasks._connect() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def user_messages(self):
        return [entry for entry in self.service.tasks.list_timeline(user_subject=OWNER, entry_type="chat_message")
            if entry["role"] == "user"]

    def test_queue_only_persists_intent_without_model_skill_or_operation(self):
        before = {table: self.count(table) for table in ("operations", "skill_bindings", "write_authorizations", "business_drafts")}
        with patch.object(self.service, "invoke", side_effect=AssertionError("HTTP cannot invoke")):
            queued = self.queue()
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(before, {table: self.count(table) for table in before})
        self.assertIsNone(self.service.skills.for_task(OWNER, queued["taskId"]))
        self.assertEqual(actions.pending_preparations(self.service, OWNER)[0]["taskId"], queued["taskId"])
        self.assertEqual(actions.pending_preparations(self.service, "other-user"), [])

    def test_fixed_read_completes_task_without_any_write_and_reuses_lost_response(self):
        queued = self.queue()
        response = self.execute(queued)
        self.assertEqual(response["status"], "succeeded")
        self.assertTrue(response["queryResult"]["complete"])
        self.assertEqual(response["queryResult"]["count"], 1)
        operation = self.service.operations.get(response["operationId"])
        self.assertEqual(operation["capability_name"], queries.CAPABILITY)
        self.assertEqual(operation["input_hash"], actions._hash({}))
        task = self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])
        with patch.object(self.service, "invoke", side_effect=AssertionError("No query replay")):
            repeated = self.execute(queued)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["operationId"], response["operationId"])
        self.assertEqual(self.fixture.audit()["downstreamWriteCount"], 0)
        self.assertEqual(self.fixture.audit()["decisionCount"], 0)
        self.assertEqual(self.fixture.audit()["authorizationStates"], {})

    def test_same_request_key_recovers_before_and_after_completion(self):
        body = {"requestKey": uuid4().hex}
        first = self.queue(body)
        self.assertEqual(self.queue(body)["taskId"], first["taskId"])
        self.execute(first)
        self.assertEqual(self.queue(body)["taskId"], first["taskId"])
        self.assertNotEqual(self.queue()["taskId"], first["taskId"])

    def test_typed_query_persists_original_message_before_task_and_recovers_once_without_model(self):
        body = {"requestKey": uuid4().hex, "userMessage": "查询 我的 ＯＡ 待办！"}
        with patch.object(self.service, "invoke", side_effect=AssertionError("HTTP cannot invoke")):
            first = self.queue(body)
            repeated = self.queue(body)
        self.assertEqual(first["taskId"], repeated["taskId"])
        self.assertEqual(first["userMessageKey"], repeated["userMessageKey"])
        messages = self.user_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["text"], body["userMessage"])
        self.assertEqual(messages[0]["source_endpoint_id"], self.account["endpoint_id"])
        timeline = self.service.tasks.list_timeline(user_subject=OWNER)
        self.assertEqual(timeline[0]["entry_type"], "chat_message")
        self.assertLess(messages[0]["sequence"], next(item["sequence"] for item in timeline if item["entry_type"] == "task_event"))
        task = self.service.tasks.get_task(first["taskId"], user_subject=OWNER)
        command = task["summary"]["command"]
        self.assertEqual(command["requestIdentity"]["messageHash"], actions._hash({"text": body["userMessage"]}))
        self.assertNotIn(body["userMessage"], json.dumps(command, ensure_ascii=False))
        self.assertEqual(command["arguments"], {})
        self.assertEqual(self.execute(first)["status"], "succeeded")
        self.assertEqual(self.queue(body)["taskId"], first["taskId"])
        self.assertEqual(len(self.user_messages()), 1)
        self.assertEqual(self.count("agent_host_dispatches"), 0)
        self.assertEqual(self.fixture.audit()["downstreamWriteCount"], 0)

    def test_typed_query_message_and_intent_roll_back_together(self):
        tables = ("agent_tasks", "task_events", "user_timeline", "notification_outbox")
        before = {table: self.count(table) for table in tables}
        ensure_task = self.service.tasks.ensure_task
        def failed_after_creation(**kwargs):
            ensure_task(**kwargs)
            raise RuntimeError("isolated acceptance failed after task creation")
        with patch.object(self.service.tasks, "ensure_task", side_effect=failed_after_creation), \
                self.assertRaisesRegex(RuntimeError, "isolated acceptance"):
            self.queue({"requestKey": uuid4().hex, "userMessage": "查看OA待办"})
        self.assertEqual(before, {table: self.count(table) for table in tables})

    def test_concurrent_typed_query_retry_creates_one_message_and_one_query(self):
        body = {"requestKey": uuid4().hex, "userMessage": "查询 OA 待办"}
        barrier, results, errors = Barrier(2), [], []
        def queue_once():
            try:
                barrier.wait(timeout=5)
                results.append(self.queue(body))
            except BaseException as exc:
                errors.append(exc)
        threads = [Thread(target=queue_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({result["taskId"] for result in results}), 1)
        self.assertEqual(len(self.user_messages()), 1)
        self.assertEqual(self.count("agent_tasks"), 1)
        self.assertEqual(self.count("agent_host_dispatches"), 0)

    def test_same_key_cannot_change_original_text_or_switch_between_button_and_typed_query(self):
        body = {"requestKey": uuid4().hex, "userMessage": "查看 OA 待办"}
        self.queue(body)
        for retry in ({**body, "userMessage": "查询OA待办"}, {**body, "userMessage": "查看OA待办"},
                      {"requestKey": body["requestKey"]}):
            with self.subTest(retry=retry), self.assertRaises(actions.ActionableResultError) as error:
                self.queue(retry)
            self.assertEqual(error.exception.code, "ACTION_REQUEST_CONFLICT")
        button = {"requestKey": uuid4().hex}
        self.queue(button)
        with self.assertRaises(actions.ActionableResultError) as error:
            self.queue({**button, "userMessage": "查看OA待办"})
        self.assertEqual(error.exception.code, "ACTION_REQUEST_CONFLICT")
        self.assertEqual([entry["text"] for entry in self.user_messages()], [body["userMessage"]])
        self.assertEqual(self.count("agent_tasks"), 2)

    def test_server_accepts_only_closed_personal_query_text_and_telegram_never_accepts_user_message(self):
        for text in queries.CURRENT_OWN_OA_PENDING:
            self.assertTrue(queries._is_current_own_pending_query(text))
        for text in ("查看OA待办", "查询 OA 待办。", "查看我当前的OA待办事项", "查询本人当前ＯＡ待办？"):
            self.assertTrue(queries._is_current_own_pending_query(text), text)
        for text in ("查询昨天OA待办", "查询张三的OA待办", "查看OA待办并审批", "不要查看OA待办",
                     "翻译：查看OA待办", "“查看OA待办”", "查看OA待办\n并提交", "查\n看OA待办",
                     "查看OA待办（仅会议）", "查看OA待办??", "", None, {}, 3):
            body = {"requestKey": uuid4().hex, "userMessage": text}
            with self.subTest(text=text), self.assertRaises(actions.ActionableResultError):
                self.queue(body)
        with self.assertRaises(actions.ActionableResultError):
            queries.queue_endpoint_query(self.service, OWNER, "not-used", {
                "requestKey": uuid4().hex, "userMessage": "查看OA待办"},
                binding_hash="not-used", source_task_id="not-used", view_hash="0" * 64)
        self.assertEqual(self.count("agent_tasks"), 0)
        self.assertEqual(self.user_messages(), [])

    def test_typed_query_failed_read_keeps_the_original_message_on_recovery(self):
        body = {"requestKey": uuid4().hex, "userMessage": "查看我的OA待办"}
        queued = self.queue(body)
        grant_permissions(self.service.user_grants, OWNER, [])
        self.assertEqual(self.execute(queued)["error"]["code"], "ACTION_PERMISSION_DENIED")
        self.assertEqual([entry["text"] for entry in self.user_messages()], [body["userMessage"]])
        grant_permissions(self.service.user_grants, OWNER, ["oa.workflow.read"])
        self.assertEqual(self.queue(body)["taskId"], queued["taskId"])
        self.assertEqual(len(self.user_messages()), 1)
        self.assertEqual(self.count("operations"), 0)

    def test_workspace_query_response_returns_normal_public_message_and_button_does_not_invent_one(self):
        body = {"requestKey": uuid4().hex, "userMessage": "查一下 OA 待办"}
        # Isolate the receipt projection from the asynchronous host wakeup.
        self.fixture.app.gateway = None
        first = self.fixture.app.query_oa_pending(self.account, body)
        message = first["userMessage"]
        self.assertEqual(message["text"], body["userMessage"])
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["source"]["client_type"], "web")
        self.assertTrue(message["source"]["is_origin"])
        self.assertNotIn("userMessageKey", first)
        self.assertNotIn("source_endpoint_id", message)
        self.assertNotIn("dedupe_key", message)
        self.assertEqual(message, self.fixture.app.list_timeline(self.account, entry_type="chat_message")[0])
        self.assertEqual(self.fixture.app.current(self.account)["timeline"]["items"], [message])
        self.assertEqual(self.fixture.app.query_oa_pending(self.account, body)["userMessage"], message)
        button = self.fixture.app.query_oa_pending(self.account, {"requestKey": uuid4().hex})
        self.assertNotIn("userMessage", button)
        self.assertEqual(len(self.user_messages()), 1)
        self.assertEqual(self.count("agent_host_dispatches"), 0)

    def expire_session(self):
        session = self.service.sessions.find(user_subject=OWNER, system_id="oa")
        self.service.sessions.mark_expired(session["session_id"], "test expired login")
        return session

    def complete_login(self, interaction_id):
        record = self.service.interactions.get(interaction_id, user_subject=OWNER)
        challenge_id = self.service._credential_challenge_id(record)
        csrf = self.service.challenges.issue_csrf(challenge_id)
        self.service.challenges.claim(challenge_id, csrf_token=csrf, csrf_cookie=csrf)
        self.service.challenges.complete(challenge_id, result={"principal": OWNER})
        self.service.sessions.activate(record["session_id"], observed_principal_ref=OWNER)

    def test_expired_login_attaches_one_credential_to_the_same_query_and_continues_without_another_command(self):
        self.expire_session(); queued = self.queue(); first = self.execute(queued)
        self.assertEqual(first["error"]["code"], "LOGIN_REQUIRED")
        interaction_id = first["interaction"]["interactionId"]
        task = self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)
        self.assertEqual(task["status"], "waiting_user")
        self.assertEqual(task["current_interaction_id"], interaction_id)
        self.assertEqual(task["current_operation_id"], first["operationId"])
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])
        with patch.object(self.service, "invoke", side_effect=AssertionError("waiting login cannot replay read")), \
                patch.object(self.service, "start_login", side_effect=AssertionError("reuse the original credential")):
            repeated = self.execute(queued)
        self.assertEqual(repeated["interaction"]["interactionId"], interaction_id)
        self.complete_login(interaction_id)
        resumed = self.service.resume_interaction(user_subject=OWNER, interaction_id=interaction_id)
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(resumed["taskId"], queued["taskId"])
        self.assertEqual(resumed["nextAction"]["type"], "original_request_completed")
        self.assertEqual(resumed["taskStatus"], "succeeded")
        with patch.object(self.service, "invoke", side_effect=AssertionError("completed query cannot replay")), \
                patch.object(self.service, "start_login", side_effect=AssertionError("completed query needs no login")):
            final = self.execute(queued)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(final["operationId"], resumed["operationId"])
        self.assertEqual(final["queryResult"]["count"], 1)
        self.assertEqual(self.count("operations"), 2)
        self.assertEqual(self.count("agent_tasks"), 1)
        self.assertEqual(self.fixture.audit()["downstreamWriteCount"], 0)
        self.assertEqual(self.fixture.audit()["decisionCount"], 0)
        self.assertEqual(self.fixture.audit()["authorizationStates"], {})

    def test_login_card_lost_reply_recovery_uses_central_continuation_and_deduplicates_read(self):
        self.expire_session(); queued = self.queue(); first = self.execute(queued)
        interaction_id = first["interaction"]["interactionId"]
        self.complete_login(interaction_id)
        recovered = self.execute(queued)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["taskId"], queued["taskId"])
        self.assertEqual(self.service.resume_interaction(user_subject=OWNER, interaction_id=interaction_id)["status"], "ignored")
        self.assertEqual(self.count("operations"), 2)

    def test_query_canceled_during_login_does_not_create_another_card_or_resume_read(self):
        self.expire_session(); queued = self.queue(); first = self.execute(queued)
        canceled = self.service.cancel_unsubmitted_task(user_subject=OWNER, task_id=queued["taskId"])
        self.assertEqual(canceled["task"]["status"], "canceled")
        with patch.object(self.service, "start_login", side_effect=AssertionError("canceled query cannot login")), \
                patch.object(self.service, "invoke", side_effect=AssertionError("canceled query cannot execute")):
            recovered = self.execute(queued)
        self.assertEqual(recovered["error"]["code"], "ACTION_COMMAND_NOT_PENDING")
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "canceled")
        self.assertIsNone(recovered.get("interaction"))
        self.assertEqual(self.count("operations"), 1)

    def test_session_recovered_elsewhere_before_card_creation_retries_only_the_same_fixed_read(self):
        session = self.expire_session(); queued = self.queue()
        def recovered_elsewhere(**kwargs):
            self.assertEqual(kwargs["task_id"], queued["taskId"])
            self.assertEqual(kwargs["system_id"], "oa")
            self.assertIsNone(kwargs["expected_principal_ref"])
            self.service.sessions.activate(session["session_id"], observed_principal_ref=OWNER)
            return {"status": "succeeded"}
        with patch.object(self.service, "start_login", side_effect=recovered_elsewhere):
            response = self.execute(queued)
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(self.count("operations"), 2)
        with patch.object(self.service, "invoke", side_effect=AssertionError("read recovery must be durable")):
            repeated = self.execute(queued)
        self.assertEqual(repeated["operationId"], response["operationId"])
        self.assertEqual(self.count("agent_tasks"), 1)

    def test_missing_login_configuration_finishes_query_instead_of_waiting_without_a_card(self):
        self.expire_session(); queued = self.queue()
        with patch.object(self.service, "start_login", side_effect=ValueError("principal missing")):
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "ACTION_LOGIN_UNAVAILABLE")
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "failed")
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_query_recovery_rechecks_permission_after_login_before_any_read_resume(self):
        self.expire_session(); queued = self.queue(); first = self.execute(queued)
        self.complete_login(first["interaction"]["interactionId"])
        grant_permissions(self.service.user_grants, OWNER, [])
        with patch.object(self.service, "invoke", side_effect=AssertionError("revoked read cannot resume")):
            response = self.execute(queued)
        self.assertEqual(response["error"]["code"], "ACTION_PERMISSION_DENIED")
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "failed")

    def test_expired_original_login_card_ends_query_without_another_login_or_read(self):
        self.expire_session(); queued = self.queue(); first = self.execute(queued)
        with patch.object(self.service.challenges, "clock", return_value=actions._now() + timedelta(minutes=10)), \
                patch.object(self.service, "start_login", side_effect=AssertionError("do not replace expired login")), \
                patch.object(self.service, "invoke", side_effect=AssertionError("expired login cannot query")):
            recovered = self.execute(queued)
        self.assertEqual(recovered["interaction"]["interactionId"], first["interaction"]["interactionId"])
        self.assertEqual(recovered["interaction"]["state"], "expired")
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "expired")
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_closed_request_rejects_arbitrary_capabilities_selections_and_arguments(self):
        valid = {"requestKey": uuid4().hex}
        for body in ({}, [], {"requestKey": "short"}, {**valid, "capabilityName": "oa.leave.approve"},
                     {**valid, "arguments": {}}, {**valid, "targetKeys": []}, {**valid, "taskId": str(uuid4())}):
            with self.subTest(body=body), self.assertRaises(actions.ActionableResultError):
                queries.queue_query(self.service, self.account, body)
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])

    def test_endpoint_scope_rechecked_on_queue_and_execution(self):
        for key in ("endpoint_id", "endpoint_key", "openclaw_session_key", "user_subject"):
            with self.subTest(key=key), self.assertRaises(actions.ActionableResultError):
                queries.queue_query(self.service, {**self.account, key: "foreign"}, {"requestKey": uuid4().hex})
        queued = self.queue()
        self.context["endpointId"] = str(uuid4())
        with self.assertRaises(actions.ActionableResultError):
            self.execute(queued)
        self.assertEqual(self.count("operations"), 0)

    def test_rotated_workspace_session_does_not_execute_old_intent(self):
        queued = self.queue()
        with self.service.tasks._connect() as connection:
            connection.execute("UPDATE workspace_accounts SET openclaw_session_key=? WHERE account_id=?",
                ("agent:main:agentbridge-workspace:direct:changed", self.account["account_id"]))
        self.assertEqual(actions.pending_preparations(self.service, OWNER), [])
        with self.assertRaises(actions.ActionableResultError):
            self.execute(queued)
        self.assertEqual(self.count("operations"), 0)

    def test_query_permission_is_live_on_queue_execution_and_recovery(self):
        body = {"requestKey": uuid4().hex}
        queued = self.queue(body)
        grant_permissions(self.service.user_grants, OWNER, [])
        with self.assertRaises(PermissionError):
            self.queue(body)
        result = self.execute(queued)
        self.assertEqual(result["error"]["code"], "ACTION_PERMISSION_DENIED")
        self.assertEqual(self.count("operations"), 0)
        self.assertEqual(self.service.tasks.get_task(queued["taskId"], user_subject=OWNER)["status"], "failed")

    def test_frozen_command_and_closed_capability_cannot_be_changed(self):
        queued = self.queue()
        with self.service.tasks._connect() as connection:
            row = connection.execute("SELECT summary_json FROM agent_tasks WHERE task_id=?", (queued["taskId"],)).fetchone()
            summary = json.loads(row[0]); summary["command"]["capabilityName"] = "oa.leave.approve"
            connection.execute("UPDATE agent_tasks SET summary_json=? WHERE task_id=?", (actions._json(summary), queued["taskId"]))
        with self.assertRaises(actions.ActionableResultError):
            self.execute(queued)
        self.assertEqual(self.count("operations"), 0)

    def test_expired_queued_query_is_not_silently_run(self):
        queued = self.queue()
        with patch.object(actions, "_now", return_value=actions._now() + timedelta(minutes=11)):
            result = self.execute(queued)
        self.assertEqual(result["error"]["code"], "ACTION_SOURCE_EXPIRED")
        self.assertEqual(self.count("operations"), 0)

    def test_queue_capacity_is_bounded_and_same_key_still_recovers(self):
        body = {"requestKey": uuid4().hex}; first = self.queue(body)
        for _ in range(3): self.queue()
        with self.assertRaises(actions.ActionableResultError) as error: self.queue()
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(self.queue(body)["taskId"], first["taskId"])

    def test_registered_host_entry_keeps_l3_lease_validation(self):
        queued = self.queue()
        with self.assertRaisesRegex(TaskIntegrityError, "coordinator lease"):
            self.service.prepare_host_task(user_subject=OWNER, task_id=queued["taskId"], command_key=queued["commandKey"],
                session_key=self.account["openclaw_session_key"], endpoint_key=self.account["endpoint_key"],
                registration={"agentHost": "openclaw", "hostInstanceId": "synthetic-host"}, runtime_context=self.context)
        self.assertEqual(self.count("operations"), 0)

    def test_workspace_http_requires_csrf_and_never_invokes_from_post(self):
        servers = self.fixture.servers()
        threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads: thread.start()
        try:
            client = JsonHttpClient(self.fixture.workspace_url)
            code, _, _ = client.json("POST", "/api/actions/oa-pending-query", {"requestKey": uuid4().hex})
            self.assertEqual(code, 401)
            code, _, _ = client.json("POST", "/api/login", {"username": "cardreview", "password": PASSWORD})
            self.assertEqual(code, 200)
            code, _, _ = client.json("POST", "/api/actions/oa-pending-query", {"requestKey": uuid4().hex})
            self.assertIn(code, (401, 403))
            client.json("POST", "/api/login", {"username": "cardreview", "password": PASSWORD})
            with patch.object(self.service, "invoke", side_effect=AssertionError("POST cannot query")):
                code, value, _ = client.json("POST", "/api/actions/oa-pending-query", {"requestKey": uuid4().hex}, csrf=True)
            self.assertEqual(code, 202, value)
            self.assertEqual(value["detail"]["task"]["title"], "查询 OA 待办")
            summary = value["detail"]["task"]["summary"]
            self.assertEqual(summary["preparation"], {
                "kind": "oa_pending_query", "state": "pending", "selectedCount": 0})
            self.assertNotIn("command", summary)
            self.assertNotIn(value["commandKey"], json.dumps(summary))
            self.assertEqual(self.count("operations"), 0)
            self.assertEqual(self.execute(value)["status"], "succeeded")
            typed_body = {"requestKey": uuid4().hex, "userMessage": "查询我的 OA 待办。"}
            with patch.object(self.service, "invoke", side_effect=AssertionError("POST cannot query")):
                code, typed, _ = client.json("POST", "/api/actions/oa-pending-query", typed_body, csrf=True)
                repeat_code, repeated, _ = client.json("POST", "/api/actions/oa-pending-query", typed_body, csrf=True)
            self.assertEqual(code, 202, typed)
            self.assertEqual(repeat_code, 202, repeated)
            self.assertEqual(typed["userMessage"]["text"], typed_body["userMessage"])
            self.assertEqual(typed["userMessage"], repeated["userMessage"])
            self.assertEqual(typed["taskId"], repeated["taskId"])
            self.assertNotIn("userMessageKey", typed)
            self.assertEqual(len(self.user_messages()), 1)
        finally:
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=5)
