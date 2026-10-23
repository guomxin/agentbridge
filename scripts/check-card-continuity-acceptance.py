#!/usr/bin/env python3
"""Assert the independently recorded baseline/live-completion fixture ledger."""
import argparse
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--live-completed", action="store_true")
    args = parser.parse_args()
    home = args.home.resolve()
    meta = json.loads((home / "acceptance.json").read_text())
    before = json.loads((home / "baseline-ledger.json").read_text())
    after = json.loads((home / "current-ledger.json").read_text())
    delta = int(args.live_completed)
    for key in ("decisionCount", "commitCount", "recoveryChildCount", "syntheticWriteCount"):
        assert after[key] == before[key] + delta, (key, before[key], after[key])
    assert after["authorizationStates"].get("consumed", 0) == before["authorizationStates"].get("consumed", 0) + delta
    assert after["authorizationStates"].get("approved", 0) == before["authorizationStates"].get("approved", 0)
    assert before["modelDispatchCount"] == after["modelDispatchCount"] == 0
    assert after["coordinatorErrors"] == []
    assert len(meta["continuity_sources"]) >= 6
    assert len(after["originals"]) == 6
    for original in after["originals"]:
        assert original["status"] == "outcome_unknown" and original["operationStatus"] == "unknown"
        assert original["authorizationState"] == "consumed" and original["authorizationUnchanged"]
    live = next(case for case in meta["recovery_cases"] if case["recovery_case"] == "live")
    with sqlite3.connect((home / "agentbridge.db").as_uri() + "?mode=ro", uri=True) as db:
        links = db.execute("SELECT payload_json FROM task_events WHERE task_id=? AND event_type='task.oa_recovery.child_created'", (live["task_id"],)).fetchall()
        assert len(links) == delta
        if args.live_completed:
            child_id = json.loads(links[0][0])["taskId"]
            task = db.execute("SELECT status,current_operation_id FROM agent_tasks WHERE task_id=?", (child_id,)).fetchone()
            assert task[0] == "succeeded"
            operation = db.execute("SELECT status,result_json FROM operations WHERE operation_id=?", (task[1],)).fetchone()
            evidence = json.loads(operation[1])["verification"]
            assert operation[0] == "succeeded" and evidence["confirmed"] is True
            assert evidence["pending_absence_confirmed"] is True and evidence["done_identity_confirmed"] is True
            assert db.execute("SELECT count(*) FROM write_authorizations WHERE commit_operation_id=? AND state='consumed'", (task[1],)).fetchone()[0] == 1
    print(json.dumps({"passed": True, "phase": "live_completed" if delta else "baseline", "home": str(home),
        "decisions": after["decisionCount"], "consumed": after["authorizationStates"].get("consumed", 0),
        "syntheticWrites": after["syntheticWriteCount"], "oldUnknownsUnchanged": len(after["originals"]),
        "modelDispatchCount": after["modelDispatchCount"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
