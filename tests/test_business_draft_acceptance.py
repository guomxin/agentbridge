"""Independent end-to-end tests: real draft, coordinator and Taihua adapter.

The sole downstream substitute is a network-free HTTP worker; assertions use
the real immutable ledgers and the persisted HTTP write count.
"""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from tests.authorization_fixtures import grant_permissions
from tests.support.business_draft_acceptance import BusinessDraftAcceptanceFixture, OWNER


class BusinessDraftAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = BusinessDraftAcceptanceFixture(Path(self.temp.name))
        self.addCleanup(lambda: self.fixture.close())
        self.case = self.fixture.meta["draft_cases"][-1]
        self.drafts = BusinessDraftService(self.fixture.service)
        self.opened = self.drafts.open(source_submission_id=self.case["input_id"])
        token = self.opened["csrfToken"]
        self.credentials = dict(presentation_id=None, csrf_token=token, csrf_cookie=token)
        self.values = {"log_date": "2026-10-13", "hours": "2", "project": "SYNTHETIC",
                       "content": "独立端到端验收，不连接真实日志系统。"}

    def rows(self, sql, values=()):
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            return connection.execute(sql, values).fetchall()

    def prepare_preview(self):
        saved = self.drafts.save(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=self.opened["state"]["revision"], values=self.values)
        return self.drafts.preview(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=saved["revision"])

    def approve(self, preview, *, request_key="independent-final-1"):
        return self.drafts.approve(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=preview["revision"], preview_id=preview["preview"]["previewId"],
            request_key=request_key)

    def drive_original(self):
        interaction_id = self.case["input_interaction"]
        for _ in range(4):
            result = self.fixture.resume(interaction_id)
            interaction = result.get("interaction")
            if interaction and interaction["resume"]["ready"]:
                interaction_id = interaction["interactionId"]
                continue
            return result
        self.fail("Original coordinator failed to reach a stable result")

    def authorization(self, receipt):
        return self.fixture.service.write_authorizations.get(receipt["authorizationId"], include_plan=True)

    def test_preview_final_original_coordinator_and_replay_write_once(self):
        immutable_before = self.rows("SELECT resume_json FROM interactions WHERE interaction_id=?",
                                     (self.case["input_interaction"],))
        preview = self.prepare_preview()
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.service.field_submissions.get(self.case["input_id"])["state"], "pending")
        self.assertIn("合成验收项目", str(preview["preview"]["summary"]))

        final = self.approve(preview)
        receipt = final["receipt"]
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.authorization(receipt)["state"], "approved")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.approve(preview)["receipt"], receipt)
        self.assertEqual(self.rows("SELECT resume_json FROM interactions WHERE interaction_id=?",
                                   (self.case["input_interaction"],)), immutable_before)
        preparation_reads = sum(c["path"] == "/api/projects" for c in self.fixture.transport.data["calls"])

        self.drive_original()
        authorization = self.authorization(receipt)
        operation = self.fixture.service.operations.get(authorization["commit_operation_id"])
        self.assertEqual(operation["status"], "succeeded", operation)
        self.assertTrue(operation["result"]["verification"]["matched"])
        self.assertEqual(authorization["plan"]["exact_input"]["project_id"], 901)
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(self.fixture.transport.data["records"][0]["content"], self.values["content"])
        self.drive_original()
        self.fixture.resume(receipt["interactionId"])
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(sum(c["path"] == "/api/projects" for c in self.fixture.transport.data["calls"]), preparation_reads,
                         "Resume must reuse the immutable preview, never prepare again")

    def test_concurrent_identical_final_decision_has_one_receipt(self):
        preview = self.prepare_preview()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.approve(preview), range(2)))
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.drive_original()
        self.assertEqual(self.fixture.transport.write_calls, 1)

    def test_final_link_failure_rolls_back_all_ledgers_and_keeps_preview_usable(self):
        preview = self.prepare_preview()
        baseline = {table: self.rows(f"SELECT COUNT(*) FROM {table}")[0][0]
                    for table in ("operations", "interactions", "task_interactions", "task_operations")}
        task_before = self.rows("SELECT status, version, current_operation_id, current_interaction_id FROM agent_tasks WHERE task_id=?",
                                (self.case["task_id"],))
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("""CREATE TRIGGER acceptance_fail_final_link
                BEFORE INSERT ON task_interactions
                BEGIN SELECT RAISE(ABORT, 'acceptance final association fault'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.approve(preview)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.service.field_submissions.get(self.case["input_id"])["state"], "pending")
        self.assertEqual({table: self.rows(f"SELECT COUNT(*) FROM {table}")[0][0] for table in baseline}, baseline)
        self.assertEqual(self.rows("SELECT status, version, current_operation_id, current_interaction_id FROM agent_tasks WHERE task_id=?",
                                   (self.case["task_id"],)), task_before)
        self.assertEqual(self.fixture.transport.write_calls, 0)
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("DROP TRIGGER acceptance_fail_final_link")
        self.assertIsNotNone(self.approve(preview)["receipt"])

    def test_final_receipt_and_original_resume_survive_service_restart(self):
        preview = self.prepare_preview()
        receipt = self.approve(preview)["receipt"]
        self.fixture.close()
        self.fixture = BusinessDraftAcceptanceFixture(Path(self.temp.name))
        self.drafts = BusinessDraftService(self.fixture.service)
        self.assertEqual(self.approve(preview)["receipt"], receipt)
        self.drive_original()
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.fixture.close()
        self.fixture = BusinessDraftAcceptanceFixture(Path(self.temp.name))
        self.drive_original()
        self.assertEqual(self.fixture.transport.write_calls, 1)

    def _assert_unknown_is_not_replayed(self, fault):
        preview = self.prepare_preview()
        receipt = self.approve(preview)["receipt"]
        self.fixture.transport.arm(fault)
        self.drive_original()
        authorization = self.authorization(receipt)
        operation = self.fixture.service.operations.get(authorization["commit_operation_id"])
        self.assertEqual(operation["status"], "unknown", operation)
        self.assertEqual(authorization["state"], "consumed")
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(len(self.fixture.transport.data["records"]), 1)
        self.fixture.transport.arm(None)
        self.drive_original()
        self.fixture.resume(receipt["interactionId"])
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(self.fixture.service.operations.get(operation["operation_id"])["status"], "unknown")

    def test_lost_post_response_keeps_unknown_and_does_not_repeat_write(self):
        self._assert_unknown_is_not_replayed("lost_write_reply")

    def test_readback_failure_keeps_unknown_and_does_not_repeat_write(self):
        self._assert_unknown_is_not_replayed("readback_unavailable")

    def test_explicit_downstream_rejection_is_failed_and_not_replayed(self):
        receipt = self.approve(self.prepare_preview())["receipt"]
        self.fixture.transport.arm("write_rejected")
        self.drive_original()
        authorization = self.authorization(receipt)
        operation = self.fixture.service.operations.get(authorization["commit_operation_id"])
        self.assertEqual(operation["status"], "failed", operation)
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(self.fixture.transport.data["records"], [])
        self.drive_original()
        self.assertEqual(self.fixture.transport.write_calls, 1)

    def test_http_transport_cannot_reach_other_routes_or_origins(self):
        for method, url in (("GET", "https://example.com/api/projects"),
                            ("POST", "http://127.0.0.1:1/api/authenticates/basic")):
            with self.subTest(url=url), self.assertRaises(AssertionError):
                self.fixture.transport.request(method, url)
        with self.assertRaises(RuntimeError):
            self.fixture._deny_worker()

    def test_real_http_page_revision_conflict_and_one_final_confirmation(self):
        servers = self.fixture.servers()
        threads = []
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            threads.append(thread)

        def close_servers():
            for server, thread in zip(servers, threads):
                server.shutdown()
                server.server_close()
                thread.join(2)
        self.addCleanup(close_servers)
        connection = HTTPConnection(*servers[0].server_address, timeout=5)
        self.addCleanup(connection.close)
        path = f"/flow/input/{self.case['input_id']}"
        connection.request("GET", path)
        response = connection.getresponse()
        page = response.read().decode()
        self.assertEqual(response.status, 200, page)
        self.assertIn("确认并提交日志", page)
        self.assertIn("mountBusinessDraft(", page)
        self.assertNotIn("提交字段</button>", page)
        config = json.loads(re.search(r"mountBusinessDraft\((\{[^\n]+\}), document, window\);", page)[1])
        cookies = SimpleCookie(response.getheader("Set-Cookie"))
        token = cookies["agentbridge_csrf"].value
        self.assertEqual(config["csrfToken"], token)
        headers = {"Origin": self.fixture.card_url, "Sec-Fetch-Site": "same-origin",
                   "Content-Type": "application/json", "Cookie": f"agentbridge_csrf={token}",
                   "X-AgentBridge-Draft-Token": token}

        def post(action, payload):
            connection.request("POST", f"{path}/draft/{action}", body=json.dumps(payload).encode(), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        revision = config["state"]["revision"]
        status, saved = post("values", {"expectedRevision": revision, "values": self.values})
        self.assertEqual(status, 200, saved)
        status, stale = post("values", {"expectedRevision": revision, "values": {**self.values, "hours": "3"}})
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale["error"]["code"], "DRAFT_REVISION_CONFLICT")
        self.assertEqual(stale["current"]["revision"], saved["revision"])
        self.assertEqual(self.fixture.transport.write_calls, 0)
        status, preview = post("preview", {"expectedRevision": saved["revision"]})
        self.assertEqual(status, 200, preview)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        decision = {"expectedRevision": preview["revision"], "previewId": preview["preview"]["previewId"],
                    "requestKey": "http-confirmation-1"}
        status, final = post("approve", decision)
        self.assertEqual(status, 200, final)
        status, repeated = post("approve", decision)
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated["receipt"], final["receipt"])
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.drive_original()
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(1,)])

    def test_edit_invalidates_old_preview_before_final(self):
        preview = self.prepare_preview()
        saved = self.drafts.save(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=preview["revision"], values={**self.values, "hours": "3"})
        with self.assertRaises(BusinessDraftError):
            self.approve(preview)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        current = self.drafts.preview(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=saved["revision"])
        receipt = self.approve(current, request_key="updated-finalization-1")["receipt"]
        self.assertEqual(self.authorization(receipt)["plan"]["exact_input"]["hours"], 3)
        self.drive_original()
        self.assertEqual(self.fixture.transport.data["records"][0]["hours"], 3)

    def test_revoked_write_permission_after_final_stops_original_resume(self):
        receipt = self.approve(self.prepare_preview())["receipt"]
        grant_permissions(self.fixture.service.user_grants, OWNER, ["oa.workflow.read"])
        try:
            self.drive_original()
        except PermissionError:
            pass  # Either an explicit denial or a recorded failure is acceptable.
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.authorization(receipt)["state"], "approved")

    def prepare_plan_case(self):
        service = self.fixture.service
        tokens = McpIdentityTokenStore(service.db_path)
        issued = tokens.issue(user_subject=OWNER, expected_principal_ref=OWNER)
        identity = tokens.resolve_client(issued["token_id"])
        service.set_task_plan_authority_resolver(lambda token, scopes: tokens.resolve_client(token, required_scopes=scopes))
        task_id = service.ensure_host_task(user_subject=OWNER, token_id=identity["token_id"],
            agent_host="acceptance-plan", host_task_key="draft-plan", endpoint_key="draft-plan-endpoint",
            client_type="web", external_subject=OWNER, conversation_ref="draft-plan", title="合成计划日志验收")["task"]["taskId"]
        session = service.sessions.get_or_create(user_subject=OWNER, system_id="oa", expected_principal_ref=OWNER)
        service.sessions.activate(session["session_id"], observed_principal_ref=OWNER)
        service.session_states.save(session["session_id"], {"cookies": []})
        worker = service._worker_factories_by_system["taihua"](None, None)

        def read(capability, _worker, _arguments):
            collection = "done" if ".done." in capability else "sent"
            return {"collection": collection, "items": [{"affair_id": collection, "title": "合成事项",
                "date": "2026-10-13", "category": "审批"}], "coverage": {
                "status": "complete", "queryApplied": True,
                "dateBasis": "processed_at" if collection == "done" else "initiated_at",
                "requestedRange": {"start": "2026-10-13", "end": "2026-10-13"},
                "scannedCount": 1, "matchedCount": 1, "hasMore": False, "completionReason": "test",
                "observedAt": "2026-10-13T09:00:00+08:00", "queryHash": "sha256:" + collection}}

        proposal = {"schemaVersion": "agentbridge.task-plan.proposal.v2", "goal": "汇总合成事项并写日志",
            "constraints": {"temporal": {"kind": "absolute_range", "start": "2026-10-13", "end": "2026-10-13"}},
            "steps": [
                {"stepKey": "done", "kind": "capability", "capabilityName": "oa.workflow.done.list"},
                {"stepKey": "sent", "kind": "capability", "capabilityName": "oa.workflow.sent.list"},
                {"stepKey": "merge", "kind": "transform", "transformName": "merge_work_items.v1",
                 "dependsOn": ["done", "sent"], "bindings": {"sources": {"mode": "many", "items": [
                     {"step": "done", "pointer": ""}, {"step": "sent", "pointer": ""}]}}},
                {"stepKey": "draft", "kind": "transform", "transformName": "work_items_to_log_draft.v2",
                 "dependsOn": ["merge"], "bindings": {"bundle": {"mode": "single", "step": "merge", "pointer": ""}}},
                {"stepKey": "write", "kind": "capability", "capabilityName": "taihua.work_log.create.prepare",
                 "dependsOn": ["draft"], "arguments": {"log_date": "2026-10-13", "hours": 2},
                 "bindings": {"content": {"mode": "single", "step": "draft", "pointer": "/draft"}}}]}
        with patch.dict(service._worker_factories_by_system, {"oa": lambda *_: worker}), \
                patch.object(service.adapter, "invoke_capability", side_effect=read):
            waiting = service.prepare_task_plan(user_subject=OWNER, task_id=task_id, proposal=proposal,
                granted_scopes=identity["scopes"], authority_identity=identity, idempotency_key="acceptance-plan")
        self.assertEqual(waiting["status"], "requires_user_action", waiting)
        self.case = {"task_id": task_id, "input_id": waiting["nextAction"]["inputSubmissionId"],
                     "input_interaction": waiting["interaction"]["interactionId"]}
        self.opened = self.drafts.open(source_submission_id=self.case["input_id"])
        token = self.opened["csrfToken"]
        self.credentials = dict(presentation_id=None, csrf_token=token, csrf_cookie=token)
        return waiting["plan"]["planId"]

    def test_real_durable_plan_rebinds_to_approved_authorization_and_finishes(self):
        plan_id = self.prepare_plan_case()
        service, task_id = self.fixture.service, self.case["task_id"]
        receipt = self.approve(self.prepare_preview())["receipt"]
        plan = service.task_plans.get(plan_id, user_subject=OWNER)
        write_step = next(step for step in plan["steps"] if step["step_key"] == "write")
        self.assertEqual(write_step["interaction_id"], receipt["interactionId"])
        self.drive_original()
        plan = service.task_plans.get(plan_id, user_subject=OWNER)
        self.assertEqual(plan["state"], "succeeded", plan)
        self.assertEqual(self.fixture.transport.write_calls, 1)
        self.assertEqual(service.tasks.task_id_for_interaction(receipt["interactionId"], user_subject=OWNER), task_id)

    def test_missing_original_plan_after_final_cannot_downgrade_to_standalone_write(self):
        plan_id = self.prepare_plan_case()
        receipt = self.approve(self.prepare_preview())["receipt"]
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("DELETE FROM task_plans WHERE plan_id=?", (plan_id,))
        result = self.drive_original()
        self.assertNotEqual(result.get("status"), "succeeded", result)
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.authorization(receipt)["state"], "approved")
        self.assertEqual(self.fixture.service.field_submissions.get(self.case["input_id"])["state"], "submitted")

    def test_missing_plan_step_authorization_link_cannot_bypass_commit_guard(self):
        plan_id = self.prepare_plan_case()
        receipt = self.approve(self.prepare_preview())["receipt"]
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            connection.execute("UPDATE task_plan_steps SET interaction_id=NULL WHERE plan_id=? AND step_key='write'", (plan_id,))
        result = self.drive_original()
        self.assertNotEqual(result.get("status"), "succeeded", result)
        self.assertEqual(self.fixture.transport.write_calls, 0)
        self.assertEqual(self.authorization(receipt)["state"], "approved")


if __name__ == "__main__":
    unittest.main()
