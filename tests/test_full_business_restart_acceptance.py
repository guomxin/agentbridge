"""Restart the real synthetic coordinator without inventing or replaying decisions."""
import json
from threading import Thread
import time
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results
from agentbridge.core.business_drafts import BusinessDraftService
from tests.support.card_content_acceptance import CardContentAcceptanceFixture
from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, OWNER
from tests.support.interaction_acceptance import write_metadata
from tests.test_full_business_acceptance import decide, drive


def ledger(fixture):
    with fixture.service.tasks._connect() as connection:
        return {
            "decisions": connection.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone()[0],
            "authorizations": dict(connection.execute("SELECT state,COUNT(*) FROM write_authorizations GROUP BY state")),
            "writes": len(fixture.oa.writes),
        }


def approve(fixture, case):
    drafts = BusinessDraftService(fixture.service)
    opened = drafts.open(case["input_id"])
    credentials = {"source_submission_id": case["input_id"], "presentation_id": None,
        "csrf_token": opened["csrfToken"], "csrf_cookie": opened["csrfToken"]}
    saved = drafts.save(**credentials, expected_revision=opened["state"]["revision"],
        values={"opinion": "合成重启验收：本项独立明确确认。"})
    preview = drafts.preview(**credentials, expected_revision=saved["revision"])
    return decide(drafts, credentials, preview)


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.03)
    raise AssertionError("Synthetic coordinator did not reach the expected durable state")


def forget_old_fixture_index(fixture, task_id):
    # Model the old fixture's missing JSON registration, leaving the entire
    # production task/input/authorization/operation ledger untouched.
    fixture.meta["draft_cases"] = [case for case in fixture.meta["draft_cases"] if case["task_id"] != task_id]
    write_metadata(fixture.meta_path, fixture.meta)


def test_dynamic_selection_persists_coordinator_membership_before_restart(tmp_path):
    first = CardContentAcceptanceFixture(tmp_path)
    thread = None
    try:
        source = first.meta["card_content_queries"][0]
        group = actionable_results.list_actionable_results(first.service, OWNER, source["task_id"])[0]
        queued = actionable_results.queue_preparation(first.service, first.account, source["task_id"],
            {"operationId": group["operationId"], "targetKeys": [group["targets"][0]["targetKey"]],
             "requestKey": uuid4().hex})
        thread = Thread(target=first._coordinate_preparations, daemon=True)
        thread.start()
        wait_until(lambda: any(case["task_id"] == queued["taskId"] for case in first.meta["draft_cases"]))
        first.stop_event.set()
        thread.join(timeout=2)
        persisted = json.loads(first.meta_path.read_text())
        assert sum(case["task_id"] == queued["taskId"] for case in persisted["draft_cases"]) == 1
        assert ledger(first) == {"decisions": 0, "authorizations": {}, "writes": 0}
        assert first.coordinator_errors == []
    finally:
        first.close()
        if thread:
            thread.join(timeout=2)
    second = CardContentAcceptanceFixture(tmp_path)
    try:
        assert sum(case["task_id"] == queued["taskId"] for case in second.meta["draft_cases"]) == 1
        assert ledger(second) == {"decisions": 0, "authorizations": {}, "writes": 0}
    finally:
        second.close()


@pytest.mark.parametrize("second_approved", [False, True])
def test_restart_recovers_second_batch_item_from_real_ledger_and_requires_its_decision(tmp_path, second_approved):
    first = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    try:
        case = first.add_pending_batch(("weekly_report", "attendance_confirmation", "efficiency_data"))
        approve(first, case)
        result = drive(first, case)
        current = {**case, "input_id": result["nextAction"]["inputSubmissionId"],
            "input_interaction": result["interaction"]["interactionId"]}
        if second_approved:
            approve(first, current)
        before = ledger(first)
        forget_old_fixture_index(first, case["task_id"])
    finally:
        first.close()
    second = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    thread = None
    try:
        assert ledger(second) == before  # Index recovery itself never executes.
        assert sum(item["task_id"] == case["task_id"] for item in second.meta["draft_cases"]) == 1
        thread = Thread(target=second.coordinate, daemon=True)
        thread.start()
        expected = 2 if second_approved else 1
        if second_approved:
            wait_until(lambda: second.service.tasks.get_batch_for_task(
                parent_task_id=case["task_id"], user_subject=OWNER)["succeeded_count"] == expected)
        else:
            time.sleep(.7)
        second.stop_event.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert ledger(second) == {"decisions": expected, "authorizations": {"consumed": expected}, "writes": expected}
        batch = second.service.tasks.get_batch_for_task(parent_task_id=case["task_id"], user_subject=OWNER)
        assert batch["current_ordinal"] == expected + 1 and batch["succeeded_count"] == expected
        assert second.oa.data["affairs"][case["affair_ids"][-1]]["processed"] is False
        assert second.coordinator_errors == []
    finally:
        second.close()
        if thread:
            thread.join(timeout=2)
    third = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    try:
        assert sum(item["task_id"] == case["task_id"] for item in third.meta["draft_cases"]) == 1
        assert ledger(third) == {"decisions": expected, "authorizations": {"consumed": expected}, "writes": expected}
    finally:
        third.close()


def test_restart_never_replays_unknown_or_advances_remaining_batch_items(tmp_path):
    first = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    try:
        case = first.add_pending_batch(("attendance_confirmation", "weekly_report"))
        decision = approve(first, case)
        first.oa.arm("approval_reply_lost")
        drive(first, case)
        authorization = first.service.write_authorizations.get(decision["receipt"]["authorizationId"])
        assert first.service.operations.get(authorization["commit_operation_id"])["status"] == "unknown"
        before = ledger(first)
        forget_old_fixture_index(first, case["task_id"])
        first.oa.arm(None)
    finally:
        first.close()
    second = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    thread = Thread(target=second.coordinate, daemon=True)
    try:
        thread.start()
        time.sleep(.7)
        second.stop_event.set()
        thread.join(timeout=2)
        assert ledger(second) == before == {"decisions": 1, "authorizations": {"consumed": 1}, "writes": 1}
        assert second.service.operations.get(authorization["commit_operation_id"])["status"] == "unknown"
        batch = second.service.tasks.get_batch_for_task(parent_task_id=case["task_id"], user_subject=OWNER)
        assert batch["current_ordinal"] == 1 and batch["succeeded_count"] == 0
        assert second.oa.data["affairs"][case["affair_ids"][1]]["processed"] is False
        assert second.coordinator_errors == []
    finally:
        second.close()
        thread.join(timeout=2)
