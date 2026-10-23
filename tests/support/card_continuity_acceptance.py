"""Real Central/Auth/Workspace card continuity; closed synthetic OA transport.

Each new home contains six query snapshots of the same unknown target.  Only
fixture seed code makes explicit synthetic decisions; browser acceptance starts
with one untouched recovery origin and its old authorization still consumed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.oa_pending_recovery import (
    ACK_VERSION, prepare_recovery_attempt, probe_unknown_pending, recovery_child_for_origin,
)
from agentbridge.core.quick_queries import execute_query, queue_query
from tests.support.business_view_acceptance import write_metadata
from tests.support.full_business_acceptance import OWNER
from tests.support.oa_pending_recovery_acceptance import PendingRecoveryAcceptanceFixture
from tests.test_full_business_acceptance import decide, drive
from tests.test_workspace import PASSWORD


CASES = {
    "live": "等待现场恢复",
    "complete": "已办核验",
    "conflict": "待办已办冲突",
    "child-succeeded": "后续办理成功",
    "child-failed": "后续提交前失败",
    "child-unknown": "后续结果未知",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class CardContinuityAcceptanceFixture(PendingRecoveryAcceptanceFixture):
    def __init__(self, home: Path, *, sources=6):
        if sources < 6:
            raise ValueError("Continuity acceptance requires at least six independent source queries")
        super().__init__(home, modes=())
        self.credentials_path = self.home / "browser-credentials.json"
        write_metadata(self.credentials_path, {"username": "cardreview", "password": PASSWORD,
            "otherUsername": "cardother", "otherPassword": PASSWORD})
        if self.meta.get("continuity_initialized"):
            return
        if self.meta["recovery_cases"]:
            raise ValueError("Use a fresh home for an independent continuity acceptance batch")
        original_add = self.oa.add_affair

        def add_named(affair_id, *, profile="leave"):
            original_add(affair_id, profile=profile)
            fixture = self.oa.data["affairs"][affair_id]
            label = next((label for key, label in CASES.items() if affair_id.endswith("continuity-" + key)), "普通待填写")
            fixture["source"]["title"] += "（合成验收：" + label + "）"
            fixture["detail"]["title"] = fixture["source"]["title"]
            self.oa.persist()

        self.oa.add_affair = add_named
        for mode in CASES:
            self._seed_unknown(mode)
        # Snapshot before creating recovery children, so all six sources retain
        # the same original items and receive their later states via projection.
        self.meta["continuity_sources"] = [self.add_query() for _ in range(sources)]
        for mode in ("child-succeeded", "child-failed", "child-unknown"):
            self._seed_child(mode)
        self.meta["continuity_waiting"] = self.add_pending_case("weekly_report", case_key="continuity-waiting")
        for case in self.meta["recovery_cases"]:
            item = self.oa.data["affairs"][case["arguments"]["affair_id"]]
            if case["recovery_case"] == "complete":
                item["processed"] = True
            elif case["recovery_case"] == "conflict":
                item["done_conflict"] = True
        self.oa.persist()
        self.meta["continuity_initialized"] = True
        write_metadata(self.meta_path, self.meta)
        write_metadata(self.home / "baseline-ledger.json", self.audit())

    def _decide(self, case, opinion):
        drafts = BusinessDraftService(self.service)
        opened = drafts.open(case["input_id"])
        credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
            "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
        saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"], values={"opinion": opinion})
        preview = drafts.preview(**credentials, expected_revision=saved["revision"])
        return decide(drafts, credentials, preview)

    def _seed_unknown(self, mode):
        case = self.add_pending_case("efficiency_data", case_key="continuity-" + mode)
        item = self.oa.data["affairs"][case["arguments"]["affair_id"]]
        item["readiness_case"] = "unknown"
        decision = self._decide(case, "原合成尝试意见；不得复用到新的办理决定。")
        drive(self, case)
        authorization = self.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        assert authorization["state"] == "consumed"
        assert self.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"] == "outcome_unknown"
        self.oa.data["affairs"][case["arguments"]["affair_id"]]["readiness_case"] = "late"
        self.meta["recovery_cases"].append({**case, "recovery_case": mode,
            "title": self.oa.data["affairs"][case["arguments"]["affair_id"]]["source"]["title"],
            "original_authorization_id": authorization["authorization_id"],
            "original_authorization_hash": _digest(authorization),
            "original_operation_id": authorization["commit_operation_id"]})
        self.oa.persist()

    def _seed_child(self, mode):
        origin = next(case for case in self.meta["recovery_cases"] if case["recovery_case"] == mode)
        assessment = probe_unknown_pending(self.service, subject=OWNER, task_id=origin["task_id"],
            request_key="continuity-probe-" + mode, endpoint_id=self.account["endpoint_id"])
        child = prepare_recovery_attempt(self.service, subject=OWNER, task_id=origin["task_id"],
            assessment_id=assessment["assessmentId"], assessment_hash=assessment["assessmentHash"],
            expected_revision=assessment["revision"], request_key="continuity-prepare-" + mode,
            acknowledgement=ACK_VERSION, endpoint_id=self.account["endpoint_id"])
        task = self.service.tasks.get_task(child["taskId"], user_subject=OWNER)
        with self.service.tasks._connect() as db:
            source = db.execute("SELECT f.submission_id FROM field_submissions f JOIN task_operations t "
                "ON t.operation_id=f.create_operation_id WHERE t.task_id=? AND f.state='pending' "
                "ORDER BY f.created_at DESC LIMIT 1", (child["taskId"],)).fetchone()[0]
        child_case = {"task_id": child["taskId"], "input_id": source,
            "input_interaction": task["current_interaction_id"]}
        assert not BusinessDraftService(self.service).open(source)["state"]["values"].get("opinion")
        decision = self._decide(child_case, "全新且明确的合成决定：" + CASES[mode])
        item = self.oa.data["affairs"][origin["arguments"]["affair_id"]]
        item["readiness_case"] = {"child-succeeded": "late", "child-failed": "never", "child-unknown": "unknown"}[mode]
        drive(self, child_case)
        status = self.service.tasks.get_task(child["taskId"], user_subject=OWNER)["status"]
        assert status == {"child-succeeded": "succeeded", "child-failed": "failed", "child-unknown": "outcome_unknown"}[mode], status
        origin["recovery_child"] = {**child_case, "expected_status": status,
            "authorization_id": decision["receipt"]["authorizationId"]}

    def add_query(self):
        queued = queue_query(self.service, self.account, {"requestKey": "continuity-query-" + uuid4().hex})
        result = execute_query(self.service, OWNER, queued["taskId"], queued["commandKey"],
            runtime_context={"endpointId": self.account["endpoint_id"], "hostType": "openclaw",
                "hostInstanceId": "synthetic-continuity-coordinator"})
        assert result["status"] == "succeeded", result
        return {"task_id": queued["taskId"], "operation_id": result["operationId"]}

    def audit(self):
        with self.service.tasks._connect() as db:
            count = lambda sql: db.execute(sql).fetchone()[0]
            states = lambda table: dict(db.execute("SELECT state,count(*) FROM " + table + " GROUP BY state"))
            result = {"observedAt": datetime.now(timezone.utc).isoformat(),
                "decisionCount": count("SELECT count(*) FROM business_draft_decisions"),
                "authorizationStates": states("write_authorizations"),
                "commitCount": count("SELECT count(*) FROM operations WHERE capability_name='oa.efficiency_data.approve'"),
                "modelDispatchCount": count("SELECT count(*) FROM agent_host_dispatches"),
                "recoveryChildCount": count("SELECT count(*) FROM task_events WHERE event_type='task.oa_recovery.child_created'"),
                "syntheticWriteCount": len(self.oa.writes), "originals": [],
                "coordinatorErrors": list(self.coordinator_errors)}
        for case in self.meta["recovery_cases"]:
            auth = self.service.write_authorizations.get(case["original_authorization_id"])
            child_id = recovery_child_for_origin(self.service, subject=OWNER, task_id=case["task_id"])
            result["originals"].append({"case": case["recovery_case"], "taskId": case["task_id"],
                "status": self.service.tasks.get_task(case["task_id"], user_subject=OWNER)["status"],
                "operationStatus": self.service.operations.get(case["original_operation_id"])["status"],
                "authorizationState": auth["state"],
                "authorizationUnchanged": _digest(auth) == case["original_authorization_hash"],
                "childTaskId": child_id,
                "childStatus": self.service.tasks.get_task(child_id, user_subject=OWNER)["status"] if child_id else None})
        return result

    def public_metadata(self):
        return {"fixture": "card-continuity-real-central-synthetic-oa-v1",
            "workspace_url": self.workspace_url, "card_origin": self.card_url,
            "credentials_file": str(self.credentials_path), "home": str(self.home),
            "sources": self.meta["continuity_sources"], "cases": self.meta["recovery_cases"],
            "waiting": self.meta["continuity_waiting"], "audit_file": str(self.home / "current-ledger.json"),
            "evidence_boundary": "Real Central/Auth/Workspace; network-free synthetic OA; no real business operations"}

    def coordinate(self):
        import threading
        thread = threading.Thread(target=super().coordinate, daemon=True)
        thread.start()
        try:
            while not self.stop_event.wait(.5):
                write_metadata(self.home / "current-ledger.json", self.audit())
        finally:
            thread.join(timeout=2)
            write_metadata(self.home / "current-ledger.json", self.audit())
