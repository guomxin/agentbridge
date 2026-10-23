from tests.support.pending_execution_clock import single_readback_attempt
"""Independent OA integration tests with actual drafts, coordinator and adapters."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.core.business_drafts import BusinessDraftError, BusinessDraftService
from tests.support.business_view_acceptance import (
    BusinessViewAcceptanceFixture, HTTP_ORIGIN, OWNER, ROOM_VALUES, SyntheticOaWorker, _ms,
)


class BusinessViewAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = BusinessViewAcceptanceFixture(Path(self.temp.name))
        self.addCleanup(lambda: self.fixture.close())
        self.drafts = BusinessDraftService(self.fixture.service)

    def open(self, profile):
        self.case = next(case for case in self.fixture.meta["view_cases"] if case["profile"] == profile)
        return self.open_case(self.case)

    def open_case(self, case):
        self.case = case
        self.opened = self.drafts.open(source_submission_id=self.case["input_id"])
        token = self.opened["csrfToken"]
        self.credentials = {"presentation_id": None, "csrf_token": token, "csrf_cookie": token}
        return self.opened["state"]

    def save(self, state, values):
        return self.drafts.save(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=state["revision"], values=values)

    def options(self, state):
        return self.drafts.options(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=state["revision"])

    def preview(self, state):
        return self.drafts.preview(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=state["revision"])

    def approve(self, state, request_key="view-confirmation-1"):
        return self.drafts.approve(source_submission_id=self.case["input_id"], **self.credentials,
            expected_revision=state["revision"], preview_id=state["preview"]["previewId"], request_key=request_key)

    def room_preview(self):
        state = self.save(self.open("meeting_room"), ROOM_VALUES)
        state = self.options(state)
        self.assertEqual(state["ui"]["viewKind"], "meeting_room")
        choice = next(item for item in state["options"]["items"] if item["id"] == "room-a")
        self.assertNotEqual(choice["value"], choice["label"])
        state = self.save(state, {**state["values"], "room": choice["value"]})
        # Saving a selection may conservatively require the same-time options
        # to be reloaded; no candidate selection is synthesized by the fixture.
        state = self.options(state)
        return self.preview(state)

    def leave_preview(self):
        state = self.open("leave")
        self.assertEqual(state["ui"]["viewKind"], "approval")
        return self.preview(self.save(state, {"opinion": "同意，已核对合成申请材料。"}))

    def drive(self, interaction_id=None):
        interaction_id = interaction_id or self.case["input_interaction"]
        for _ in range(4):
            result = self.fixture.resume(interaction_id)
            interaction = result.get("interaction")
            if interaction and interaction["resume"]["ready"]:
                interaction_id = interaction["interactionId"]
                continue
            return result
        self.fail("Original coordinator did not settle")

    def authorization(self, receipt):
        return self.fixture.service.write_authorizations.get(receipt["authorizationId"], include_plan=True)

    def operation(self, receipt):
        auth = self.authorization(receipt)
        return self.fixture.service.operations.get(auth["commit_operation_id"])

    def rows(self, sql, values=()):
        with sqlite3.connect(self.fixture.service.db_path) as connection:
            return connection.execute(sql, values).fetchall()

    def test_room_one_final_one_real_apply_and_authoritative_readback(self):
        preview = self.room_preview()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        receipt = self.approve(preview)["receipt"]
        self.assertEqual(self.approve(preview)["receipt"], receipt)
        self.assertEqual(self.fixture.oa.writes, [])
        self.drive()
        operation = self.operation(receipt)
        self.assertEqual(operation["status"], "succeeded", operation)
        self.assertEqual(operation["result"]["verification"], {"confirmed": True, "method": "new_my_application_readback"})
        self.assertFalse(operation["result"]["meeting_created"])
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.assertEqual(self.fixture.oa.data["applications"][0]["roomId"], "room-a")
        self.drive()
        self.drive(receipt["interactionId"])
        self.assertEqual(len(self.fixture.oa.writes), 1)

    def test_time_edit_refreshes_candidates_without_replacing_old_selection(self):
        preview = self.room_preview()
        selected = preview["values"]["room"]
        changed = self.save(preview, {**preview["values"], "start_time": "2026-10-20 14:00", "end_time": "2026-10-20 15:00"})
        self.assertIsNone(changed["preview"])
        with self.assertRaises(BusinessDraftError):
            self.approve(preview)
        changed = self.options(changed)
        self.assertEqual(changed["values"]["room"], selected)
        self.assertFalse(changed["options"]["selected"]["available"])
        self.assertTrue(any(item["id"] == "room-b" and item["available"] for item in changed["options"]["items"]))
        with self.assertRaises(BusinessDraftError):
            self.preview(changed)
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])

    def test_late_candidate_response_cannot_replace_a_newer_time_revision(self):
        state = self.save(self.open("meeting_room"), ROOM_VALUES)
        entered, release = threading.Event(), threading.Event()
        original = self.fixture.oa.request

        def delayed(method, url, **kwargs):
            result = original(method, url, **kwargs)
            if "roomListInfo" in str(kwargs.get("body", "")):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Synthetic read barrier timed out")
            return result
        with patch.object(self.fixture.oa, "request", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.options, state)
            self.assertTrue(entered.wait(5))
            try:
                newer = self.save(state, {**state["values"], "start_time": "2026-10-20 14:00", "end_time": "2026-10-20 15:00"})
            finally:
                release.set()
            with self.assertRaises(BusinessDraftError):
                future.result()
        current = self.drafts.state(source_submission_id=self.case["input_id"], **self.credentials)
        self.assertEqual(current["revision"], newer["revision"])
        self.assertEqual(current["values"]["start_time"], "2026-10-20 14:00")
        self.assertEqual(self.fixture.oa.writes, [])

    def test_same_room_id_renamed_requires_explicit_new_choice(self):
        preview = self.room_preview()
        selected = preview["values"]["room"]
        self.fixture.oa.data["rooms"][0]["roomName"] = "已经改名的合成会议室"
        current = self.options(preview)
        self.assertEqual(current["values"]["room"], selected)
        self.assertFalse(current["options"]["selected"]["available"])
        new = next(item for item in current["options"]["items"] if item["id"] == "room-a")
        self.assertNotEqual(new["value"], selected)
        with self.assertRaises(BusinessDraftError):
            self.approve(preview)
        self.assertEqual(self.fixture.oa.writes, [])

    def test_same_room_name_with_new_id_does_not_reuse_old_authorization_preview(self):
        preview = self.room_preview()
        self.fixture.oa.data["rooms"][0]["roomId"] = "replacement-room-a"
        with self.assertRaises(BusinessDraftError):
            self.approve(preview)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        current = self.options(preview)
        self.assertFalse(current["options"]["selected"]["available"])
        self.assertEqual(current["values"]["room"], preview["values"]["room"])

    def test_initially_occupied_schedule_still_opens_and_new_time_recovers_candidates(self):
        self.fixture.oa.data["bookings"] = [{"roomId": room["roomId"],
            "appBeginDate": _ms(ROOM_VALUES["start_time"]), "appEndDate": _ms(ROOM_VALUES["end_time"])}
            for room in self.fixture.oa.data["rooms"]]
        state = self.open_case(self.fixture.add_view_case("meeting_room"))
        state = self.options(self.save(state, ROOM_VALUES))
        self.assertEqual(state["options"]["items"], [])
        self.assertEqual(state["values"]["room"], "")
        state = self.save(state, {**state["values"], "start_time": "2026-10-20 12:00", "end_time": "2026-10-20 13:00"})
        state = self.options(state)
        self.assertEqual({item["id"] for item in state["options"]["items"]}, {"room-a", "room-b"})
        self.assertEqual(state["values"]["room"], "")
        self.assertEqual(self.fixture.oa.writes, [])

    def test_room_becoming_occupied_after_preview_creates_no_authorization(self):
        preview = self.room_preview()
        self.fixture.oa.data["bookings"].append({"roomId": "room-a", "appBeginDate": 0, "appEndDate": 9999999999999})
        with self.assertRaises(BusinessDraftError):
            self.approve(preview)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_room_commit_rechecks_new_conflict_before_write_boundary(self):
        receipt = self.approve(self.room_preview())["receipt"]
        self.fixture.oa.data["bookings"].append({"roomId": "room-a", "appBeginDate": 0, "appEndDate": 9999999999999})
        self.drive()
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(self.authorization(receipt)["state"], "approved")
        task = self.fixture.service.tasks.get_task(self.case["task_id"], user_subject=OWNER)
        self.assertNotEqual(task["status"], "succeeded")

    def test_leave_one_final_shared_executor_independent_reader_and_one_effect(self):
        preview = self.leave_preview()
        receipt = self.approve(preview)["receipt"]
        self.drive()
        operation = self.operation(receipt)
        self.assertEqual(operation["status"], "succeeded", operation)
        verification = operation["result"]["verification"]
        self.assertEqual(verification["method"], "complete_pending_absence_and_done_identity")
        self.assertTrue(verification["pending_absence_confirmed"])
        self.assertTrue(verification["done_identity_confirmed"])
        submitters = [worker for worker in self.fixture.oa.workers if worker.readback_opened]
        self.assertEqual(len(submitters), 1)
        self.assertTrue(submitters[0].readback_closed)
        self.assertEqual(submitters[0].page.pumped, [800])
        self.assertFalse(submitters[0].page.handlers)
        self.drive()
        self.drive(receipt["interactionId"])
        self.assertEqual(len(self.fixture.oa.writes), 1)

    def test_changed_leave_material_requires_new_preview_and_new_explicit_decision(self):
        preview = self.leave_preview()
        subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/serve-business-view-acceptance.py"),
            "--home", str(self.fixture.home), "--change-material", self.case["key"]], check=True, capture_output=True, text=True)
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve(preview)
        self.assertEqual(caught.exception.code, "DRAFT_REVIEW_CHANGED")
        current = caught.exception.current
        self.assertIsNotNone(current)
        self.assertNotEqual(current["preview"]["previewId"], preview["preview"]["previewId"])
        self.assertIn("材料已经变更", str(current))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM business_draft_decisions"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])
        with self.assertRaises(BusinessDraftError):
            self.approve(preview)
        receipt = self.approve(current, request_key="review-changed-confirmation-2")["receipt"]
        self.drive()
        self.assertEqual(self.operation(receipt)["status"], "succeeded")
        self.assertEqual(len(self.fixture.oa.writes), 1)

    def test_local_material_control_is_consumed_once_by_concurrent_dom_reads(self):
        self.open("leave")
        affair_id = self.case["arguments"]["affair_id"]
        control = {"id": "concurrent-dom-drift", "action": "change_material", "affair_id": affair_id}
        (self.fixture.home / "business-view-control.json").write_text(json.dumps(control))
        workers = [SyntheticOaWorker(self.fixture.oa) for _ in range(2)]
        for worker in workers:
            worker.affair_id = affair_id
        boundary = threading.Barrier(2)

        def read(worker):
            boundary.wait(5)
            return worker.page.frames[0].evaluate("synthetic read")

        with patch.object(self.fixture.oa, "change_material", wraps=self.fixture.oa.change_material) as change, \
             ThreadPoolExecutor(max_workers=2) as pool:
            snapshots = list(pool.map(read, workers))
        self.assertEqual(change.call_count, 1)
        self.assertEqual(self.fixture.oa.data["last_control"], control["id"])
        self.assertTrue(all(snapshot["reason"] == "材料已经变更，需要重新核对" for snapshot in snapshots))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM business_draft_decisions"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def test_material_change_after_final_is_rejected_before_any_submission(self):
        receipt = self.approve(self.leave_preview())["receipt"]
        self.fixture.oa.change_material(self.case["arguments"]["affair_id"])
        self.drive()
        self.assertEqual(self.fixture.oa.writes, [])
        self.assertEqual(self.authorization(receipt)["state"], "approved")

    def test_actual_oa_home_principal_change_blocks_final_authorization(self):
        preview = self.leave_preview()
        self.fixture.oa.data["principal"] = "different-synthetic-account"
        with self.assertRaises(BusinessDraftError) as caught:
            self.approve(preview)
        self.assertEqual(caught.exception.code, "DRAFT_SESSION_CHANGED")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(0,)])
        self.assertEqual(self.fixture.oa.writes, [])

    def _unknown(self, profile, fault, *, bounded_clock=False):
        preview = self.room_preview() if profile == "meeting_room" else self.leave_preview()
        receipt = self.approve(preview)["receipt"]
        self.fixture.oa.arm(fault)
        if bounded_clock:
            # Advance at the readback boundary, independent of how many lower
            # level stages inspect the shared deadline.
            with single_readback_attempt():
                self.drive()
        else:
            self.drive()
        operation = self.operation(receipt)
        self.assertEqual(operation["status"], "unknown", operation)
        self.assertEqual(self.authorization(receipt)["state"], "consumed")
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.fixture.oa.arm(None)
        self.drive()
        self.drive(receipt["interactionId"])
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.assertEqual(self.operation(receipt)["status"], "unknown")
        return operation

    def test_room_missing_readback_is_unknown_without_replaying_apply(self):
        self._unknown("meeting_room", "room_readback_missing")

    def test_lost_approval_reply_is_unknown_without_replaying_dom_submit(self):
        self._unknown("leave", "approval_reply_lost")

    def test_partial_pending_list_cannot_prove_approval_success(self):
        operation = self._unknown("leave", "pending_partial", bounded_clock=True)
        self.assertIn("pending_coverage_incomplete", str(operation))

    def test_done_identity_mismatch_cannot_prove_approval_success(self):
        operation = self._unknown("leave", "done_identity_changed", bounded_clock=True)
        self.assertIn("done_identity_mismatch", str(operation))

    def concurrent_final(self, preview):
        boundary = threading.Barrier(2)
        original = self.drafts._prepare

        def concurrent_read(*args, **kwargs):
            result = original(*args, **kwargs)
            # Pause after the real downstream read releases its session lock,
            # before either final request can enter the decision transaction.
            boundary.wait(5)
            return result
        with patch.object(self.drafts, "_prepare", side_effect=concurrent_read), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.approve(preview), range(2)))
        self.assertEqual(results[0]["receipt"], results[1]["receipt"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM write_authorizations"), [(1,)])
        self.drive()
        self.assertEqual(len(self.fixture.oa.writes), 1)

    def test_concurrent_leave_final_rechecks_both_requests_and_returns_one_decision(self):
        self.concurrent_final(self.leave_preview())

    def test_concurrent_room_final_rechecks_both_requests_and_returns_one_decision(self):
        self.concurrent_final(self.room_preview())

    def test_nonwhitelist_legacy_input_still_requires_separate_authorization(self):
        self.case = next(case for case in self.fixture.meta["view_cases"] if case["profile"] == "legacy_overtime")
        service = self.fixture.service
        fields = TrustedFieldApplication(submission_store=service.field_submissions)
        flow = TrustedFlowApplication(service=service, field_application=fields,
                                      action_application=TrustedActionApplication(authorization_store=service.write_authorizations))
        page = flow.get_card("input", self.case["input_id"], secure_cookie=False, presentation_id=None, csrf_cookie="")
        self.assertEqual(page.status, 200)
        self.assertNotIn("mountBusinessDraft(", page.body.decode())
        token = service.field_submissions.issue_csrf(self.case["input_id"])
        service.field_submissions.submit(self.case["input_id"], csrf_token=token, csrf_cookie=token, values={"opinion": "同意合成加班材料"})
        result = self.drive()
        self.assertEqual(result["interaction"]["type"], "execution_authorization")
        authorization_id = result["nextAction"]["authorizationId"]
        self.assertEqual(service.write_authorizations.get(authorization_id)["state"], "pending")
        self.assertEqual(self.fixture.oa.writes, [])
        token = service.write_authorizations.issue_csrf(authorization_id)
        service.write_authorizations.decide(authorization_id, decision="approve", csrf_token=token, csrf_cookie=token)
        self.drive(result["interaction"]["interactionId"])
        self.assertEqual(len(self.fixture.oa.writes), 1)
        operation = service.operations.get(service.write_authorizations.get(authorization_id)["commit_operation_id"])
        self.assertEqual(operation["status"], "succeeded", operation)

    def test_synthetic_worker_blocks_external_io_and_unknown_routes(self):
        worker = SyntheticOaWorker(self.fixture.oa)
        for url in ("https://oa.example.test/seeyon/", f"{HTTP_ORIGIN}/seeyon/login"):
            with self.subTest(url=url), self.assertRaises(AssertionError):
                worker.goto(url)
        with self.assertRaises(AssertionError):
            worker.request("POST", f"{HTTP_ORIGIN}/seeyon/ajax.do?managerName=unknown", body="")
        with self.assertRaises(RuntimeError):
            self.fixture._deny_worker()

    def test_trusted_http_profiles_options_and_explicit_final_use_actual_backend(self):
        servers = self.fixture.servers()
        threads = []
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            threads.append(thread)

        def stop():
            for server, thread in zip(servers, threads):
                server.shutdown()
                server.server_close()
                thread.join(2)
        self.addCleanup(stop)
        connection = HTTPConnection(*servers[0].server_address, timeout=5)
        self.addCleanup(connection.close)
        for profile in ("meeting_room", "leave"):
            with self.subTest(profile=profile):
                self.case = next(case for case in self.fixture.meta["view_cases"] if case["profile"] == profile)
                path = f"/flow/input/{self.case['input_id']}"
                connection.request("GET", path)
                response = connection.getresponse()
                page = response.read().decode()
                self.assertEqual(response.status, 200, page)
                config = json.loads(re.search(r"mountBusinessDraft\((\{[^\n]+\}), document, window\);", page)[1])
                cookies = SimpleCookie(response.getheader("Set-Cookie"))
                token = cookies["agentbridge_csrf"].value
                self.assertEqual(token, config["csrfToken"])
                headers = {"Origin": self.fixture.card_url, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json",
                    "Cookie": f"agentbridge_csrf={token}", "X-AgentBridge-Draft-Token": token}

                def post(action, payload):
                    connection.request("POST", f"{path}/draft/{action}", body=json.dumps(payload).encode(), headers=headers)
                    result = connection.getresponse()
                    body = json.loads(result.read())
                    self.assertEqual(result.status, 200, body)
                    return body

                state = config["state"]
                values = ROOM_VALUES if profile == "meeting_room" else {"opinion": "已核对 HTTP 合成材料，同意。"}
                state = post("values", {"expectedRevision": state["revision"], "values": values})
                if profile == "meeting_room":
                    state = post("options", {"expectedRevision": state["revision"]})
                    selected = next(item for item in state["options"]["items"] if item["id"] == "room-a")
                    state = post("values", {"expectedRevision": state["revision"], "values": {**state["values"], "room": selected["value"]}})
                    state = post("options", {"expectedRevision": state["revision"]})
                preview = post("preview", {"expectedRevision": state["revision"]})
                before = len(self.fixture.oa.writes)
                decision = {"expectedRevision": preview["revision"], "previewId": preview["preview"]["previewId"],
                            "requestKey": f"http-final-{profile}-1"}
                final = post("approve", decision)
                self.assertEqual(post("approve", decision)["receipt"], final["receipt"])
                self.assertEqual(len(self.fixture.oa.writes), before)
                self.drive()
                self.assertEqual(self.operation(final["receipt"])["status"], "succeeded")
                self.assertEqual(len(self.fixture.oa.writes), before + 1)

    def test_finalized_leave_receipt_resumes_after_service_restart_without_new_prepare(self):
        preview = self.leave_preview()
        receipt = self.approve(preview)["receipt"]
        self.fixture.close()
        self.fixture = BusinessViewAcceptanceFixture(Path(self.temp.name))
        self.drafts = BusinessDraftService(self.fixture.service)
        self.assertEqual(self.approve(preview)["receipt"], receipt)
        self.drive()
        self.assertEqual(self.operation(receipt)["status"], "succeeded")
        self.assertEqual(len(self.fixture.oa.writes), 1)
        self.fixture.close()
        self.fixture = BusinessViewAcceptanceFixture(Path(self.temp.name))
        self.drive()
        self.assertEqual(len(self.fixture.oa.writes), 1)

