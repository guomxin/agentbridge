"""Independent Workspace HTTP acceptance for immutable, task-bound feedback."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest

from agentbridge.core.business_skills import SkillRejected, skill_catalog
from tests.support.task_feedback_acceptance import TaskFeedbackAcceptanceFixture, OWNER
from tests.test_workspace import PASSWORD, _cookies, _request


class TaskFeedbackAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = TaskFeedbackAcceptanceFixture(Path(self.temp.name))
        self.addCleanup(lambda: self.fixture.close())
        self.servers, self.threads = [], []
        self.addCleanup(self.stop_servers)
        self.start_servers()
        self.cookies = self.login("cardreview")
        self.other_cookies = self.login("cardother")
        self.payload = {"rating": "useful", "comment": "这条助手方法有帮助；评价不是业务成功证据。", "requestKey": "feedback-confirmation-0001"}

    def start_servers(self):
        self.servers = self.fixture.servers()
        for server in self.servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.port = self.servers[1].server_address[1]
        self.origin = self.fixture.workspace_url

    def stop_servers(self):
        for server, thread in zip(self.servers, self.threads):
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.servers, self.threads = [], []

    def login(self, username):
        status, headers, body = _request(self.port, "POST", "/api/login", origin=self.origin,
            body={"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return _cookies(headers)

    def case(self, state="succeeded"):
        return next(case for case in self.fixture.meta["feedback_cases"] if case["state"] == state)

    def get(self, state="succeeded", cookies=None):
        return _request(self.port, "GET", f"/api/tasks/{self.case(state)['task_id']}",
                        cookies=self.cookies if cookies is None else cookies)

    def post(self, state="succeeded", payload=None, cookies=None, **overrides):
        cookies = self.cookies if cookies is None else cookies
        parameters = {"origin": self.origin, "cookies": cookies,
                      "csrf": cookies.get("agentbridge_workspace_csrf"), "body": self.payload if payload is None else payload}
        parameters.update(overrides)
        return _request(self.port, "POST", f"/api/tasks/{self.case(state)['task_id']}/feedback", **parameters)

    def rows(self, sql, params=()):
        with sqlite3.connect(self.fixture.service.db_path) as db:
            return db.execute(sql, params).fetchall()

    def assert_recorded(self, response):
        status, _, body = response
        self.assertEqual(status, 200, body)
        feedback = body["feedback"]
        self.assertFalse(feedback["available"])
        self.assertEqual(feedback["receipt"]["status"], "recorded")
        self.assertEqual(feedback["receipt"]["version"], "1.0.0")
        self.assertEqual(feedback["receipt"]["profile"], "use")
        return feedback["receipt"]

    def test_new_publication_does_not_move_feedback_off_the_original_task_version(self):
        case = self.case()
        self.assertEqual(self.fixture.service.skills.current(case["skill_id"])["manifest"]["version"], "1.0.1")
        status, _, detail = self.get()
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["skill"]["version"], "1.0.0")
        self.assertEqual(detail["feedback"], {"available": True, "receipt": None})
        receipt = self.assert_recorded(self.post())
        self.assertEqual(receipt["rating"], self.payload["rating"])
        self.assertEqual(receipt["comment"], self.payload["comment"])
        self.assertEqual(self.rows("SELECT skill_id,version,rating FROM skill_feedback"), [(case["skill_id"], "1.0.0", "useful")])
        context = json.loads(self.rows("SELECT source_json FROM skill_feedback_context")[0][0])
        self.assertEqual(context["binding_id"], case["binding_id"])
        self.assertEqual(context["task_id"], case["task_id"])
        self.assertEqual(self.get()[2]["feedback"]["receipt"], receipt)

    def test_same_or_different_key_replays_one_receipt_but_changed_payload_conflicts(self):
        receipt = self.assert_recorded(self.post())
        self.assertEqual(self.assert_recorded(self.post()), receipt)
        self.assertEqual(self.assert_recorded(self.post(payload={**self.payload, "requestKey": "feedback-new-key-0002"})), receipt)
        for change in ({"rating": "incorrect"}, {"comment": "不同评价"}):
            with self.subTest(change=change):
                status, _, body = self.post(payload={**self.payload, **change})
                self.assertEqual(status, 409, body)
                self.assertEqual(body["error"]["code"], "TASK_FEEDBACK_CONFLICT")
                self.assertEqual(body["feedback"]["receipt"], receipt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(1,)])

    def test_concurrent_http_posts_return_one_receipt_and_one_feedback_row(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.post(), range(2)))
        receipts = [self.assert_recorded(result) for result in results]
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(1,)])

    def test_discarded_response_recovers_on_refresh_and_safe_replay(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Origin": self.origin, "Content-Type": "application/json", "X-AgentBridge-CSRF": self.cookies["agentbridge_workspace_csrf"],
                   "Cookie": "; ".join(f"{key}={value}" for key, value in self.cookies.items())}
        connection.request("POST", f"/api/tasks/{self.case()['task_id']}/feedback", body=json.dumps(self.payload).encode(), headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        # Deliberately lose the entire response body after the server committed.
        # A new page has neither an in-memory receipt nor a completed fetch.
        connection.close()
        receipt = self.get()[2]["feedback"]["receipt"]
        self.assertIsNotNone(receipt)
        self.assertEqual(self.assert_recorded(self.post()), receipt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(1,)])

    def test_refresh_and_replay_survive_actual_service_restart(self):
        receipt = self.assert_recorded(self.post())
        self.stop_servers()
        self.fixture.close()
        self.fixture = TaskFeedbackAcceptanceFixture(Path(self.temp.name), seed=False)
        self.start_servers()
        self.assertEqual(self.get()[2]["feedback"]["receipt"], receipt)
        self.assertEqual(self.assert_recorded(self.post()), receipt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(1,)])

    def test_cross_user_cannot_read_rate_or_discover_private_task_binding(self):
        own_skill = self.case()["skill_id"]
        self.assertNotIn(own_skill, {item["skill_id"] for item in skill_catalog(self.fixture.service, "interaction-other")["items"]})
        for response in (self.get(cookies=self.other_cookies), self.post(cookies=self.other_cookies)):
            status, _, body = response
            self.assertEqual(status, 404, body)
            self.assertNotIn(own_skill, str(body))
            self.assertNotIn(self.case()["binding_id"], str(body))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(0,)])

    def test_csrf_origin_and_authentication_are_enforced_before_writes(self):
        for parameters, expected in (({"csrf": None}, 401), ({"csrf": "wrong"}, 401),
                                     ({"origin": "https://untrusted.example"}, 403), ({"cookies": {}, "csrf": None}, 401)):
            with self.subTest(parameters=parameters):
                status, _, body = self.post(**parameters)
                self.assertEqual(status, expected, body)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(0,)])

    def test_active_and_unbound_tasks_cannot_create_feedback(self):
        for state, expected in (("active", 409), ("unbound", 404)):
            with self.subTest(state=state):
                status, _, detail = self.get(state)
                self.assertEqual(status, 200, detail)
                self.assertFalse(detail["feedback"]["available"])
                status, _, body = self.post(state)
                self.assertEqual(status, expected, body)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(0,)])

    def test_feedback_after_revocation_does_not_restore_binding_or_assignment(self):
        case = self.case()
        self.fixture.revoke_current_skill()
        with self.assertRaises(SkillRejected):
            self.fixture.service.skills.binding(OWNER, case["binding_id"])
        grants = self.fixture.service.user_grants.get(OWNER)
        receipt = self.assert_recorded(self.post())
        with self.assertRaises(SkillRejected):
            self.fixture.service.skills.binding(OWNER, case["binding_id"])
        self.assertNotIn(case["skill_id"], self.fixture.service.skills.config(f"user:{OWNER}")["value"])
        self.assertEqual(self.fixture.service.user_grants.get(OWNER), grants)
        detail = self.get()[2]
        self.assertTrue(detail["skill"]["revoked"])
        self.assertEqual(detail["feedback"]["receipt"], receipt)

    def test_useful_feedback_does_not_turn_unknown_or_failed_into_business_success(self):
        before_operations = self.rows("SELECT COUNT(*) FROM operations")
        before_authorizations = self.rows("SELECT COUNT(*) FROM write_authorizations")
        for state in ("outcome_unknown", "failed"):
            with self.subTest(state=state):
                receipt = self.assert_recorded(self.post(state))
                detail = self.get(state)[2]
                self.assertEqual(detail["task"]["status"], state)
                self.assertEqual(detail["feedback"]["receipt"], receipt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM operations"), before_operations)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), before_authorizations)
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.fixture.service.skill_authoring.workbench.metrics(OWNER)["verification"], "user_feedback_not_independent_success_rate")

    def test_client_cannot_choose_attribution_or_send_invalid_feedback(self):
        updates = ({"version": "1.0.1"}, {"profile": "other"}, {"binding_id": "other"}, {"skill_id": "other"},
                   {"rating": "succeeded"}, {"comment": "x" * 1001}, {"requestKey": 12})
        for update in updates:
            with self.subTest(fields=list(update)):
                status, _, body = self.post(payload={**self.payload, **update})
                self.assertEqual(status, 400, body)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(0,)])

    def test_feedback_context_failure_rolls_back_and_same_key_can_retry(self):
        with sqlite3.connect(self.fixture.service.db_path) as db:
            db.execute("""CREATE TRIGGER acceptance_feedback_context_failure BEFORE INSERT ON skill_feedback_context
                BEGIN SELECT RAISE(ABORT, 'synthetic feedback context failure'); END""")
        status, _, _ = self.post()
        self.assertEqual(status, 500)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM skill_feedback"), [(0,)])
        self.assertIsNone(self.get()[2]["feedback"]["receipt"])
        with sqlite3.connect(self.fixture.service.db_path) as db:
            db.execute("DROP TRIGGER acceptance_feedback_context_failure")
        self.assert_recorded(self.post())

    def test_new_business_inputs_preserve_historical_tasks_feedback_and_input_ledgers(self):
        receipt = self.assert_recorded(self.post())
        first = self.fixture.add_pending_business_cases()
        before = {row[0]: row[1:] for row in self.rows("SELECT task_id,status,version FROM agent_tasks")}
        binding_before = self.rows("SELECT binding_id,version,revoked FROM skill_bindings ORDER BY binding_id")
        second = self.fixture.add_pending_business_cases()
        after = {row[0]: row[1:] for row in self.rows("SELECT task_id,status,version FROM agent_tasks")}
        self.assertTrue(all(after[task] == value for task, value in before.items()))
        self.assertEqual(binding_before, self.rows("SELECT binding_id,version,revoked FROM skill_bindings ORDER BY binding_id"))
        self.assertEqual({case["profile"] for case in second}, {"meeting_room", "leave", "legacy_overtime", "work_log"})
        self.assertEqual(len({case["input_id"] for case in first + second}), 8)
        for case in first + second:
            self.assertEqual(self.fixture.service.field_submissions.get(case["input_id"])["state"], "pending")
        self.assertEqual(self.get()[2]["feedback"]["receipt"], receipt)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(self.fixture.transport.write_calls, 0)

