"""Real unknown authorizations and recovery; all OA pages are closed synthetic IO."""
from contextlib import contextmanager
from copy import deepcopy
import threading

from agentbridge.core.business_drafts import BusinessDraftService
from tests.support.pending_readiness_acceptance import PendingReadinessAcceptanceFixture, ReadinessPage, ReadinessWorker
from tests.support.full_business_acceptance import OWNER
from tests.support.business_view_acceptance import write_metadata
from tests.test_full_business_acceptance import decide, drive


class RecoveryPage(ReadinessPage):
    def _done_rows(self):
        rows = super()._done_rows()
        for item in self.worker.transport.data["affairs"].values():
            if item.get("done_conflict") and not item["processed"]:
                rows.append({**deepcopy(item["source"]), "date_basis": "processed_at"})
        return rows


class RecoveryWorker(ReadinessWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = RecoveryPage(self)

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield RecoveryWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class PendingRecoveryAcceptanceFixture(PendingReadinessAcceptanceFixture):
    def __init__(self, home, *, modes=("unresolved", "complete", "conflict")):
        super().__init__(home, modes=())
        self.service._worker_factories_by_system["oa"] = lambda *_: RecoveryWorker(self.oa)
        self.meta.setdefault("recovery_cases", [])
        if not self.meta["recovery_cases"]:
            for mode in modes:
                case = self.add_pending_case("efficiency_data", case_key="recovery-" + mode)
                item = self.oa.data["affairs"][case["arguments"]["affair_id"]]
                item["readiness_case"] = "unknown"
                drafts = BusinessDraftService(self.service)
                opened = drafts.open(case["input_id"])
                credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
                               "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
                saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"],
                                    values={"opinion": "原合成尝试意见；恢复表单不得复制。"})
                preview = drafts.preview(**credentials, expected_revision=saved["revision"])
                decision = decide(drafts, credentials, preview)
                drive(self, case)
                authorization = self.service.write_authorizations.get(decision["receipt"]["authorizationId"])
                assert authorization["state"] == "consumed"
                assert self.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
                item = self.oa.data["affairs"][case["arguments"]["affair_id"]]
                item["readiness_case"] = "late"
                item["processed"] = mode == "complete"
                item["done_conflict"] = mode == "conflict"
                self.meta["recovery_cases"].append({**case, "recovery_case": mode,
                    "original_authorization_id": authorization["authorization_id"],
                    "original_operation_id": authorization["commit_operation_id"]})
                self.oa.persist()
        write_metadata(self.meta_path, self.meta)

    def coordinate(self):
        # Recovery children use the same original Central coordinator; discover
        # only server-attested links, never fabricate a decision or approval.
        thread = threading.Thread(target=super().coordinate, daemon=True)
        thread.start()
        try:
            while not self.stop_event.wait(.1):
                with self.service.tasks._connect() as db:
                    rows = db.execute("SELECT json_extract(payload_json,'$.taskId') FROM task_events WHERE user_subject=? AND event_type='task.oa_recovery.child_created'", (OWNER,)).fetchall()
                with self.lock:
                    known = {item["task_id"] for item in self.meta["draft_cases"]}
                    self.meta["draft_cases"].extend({"task_id": row[0]} for row in rows if row[0] not in known)
        finally:
            thread.join(timeout=2)

    def public_metadata(self):
        return {"fixture": "oa-unknown-recovery-real-central-synthetic-oa-v1",
                "workspace_url": self.workspace_url, "card_origin": self.card_url,
                "username": "cardreview", "cases": self.meta["recovery_cases"],
                "oa_write_count": len(self.oa.writes),
                "evidence_boundary": "Real Central/Auth/draft/Workspace and consumed unknown; network-free synthetic OA only"}
