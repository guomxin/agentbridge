"""Restart the central planner on the persisted plan produced by the real NanoBot SDK."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.task_plans import task_plan_response
from tests.support.action_matrix_acceptance import ActionMatrixAcceptanceFixture


def handoff(fixture, plan_id, *, user_subject):
    entry = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--home",
        str(fixture.home),
        "--plan",
        plan_id,
        "--subject",
        user_subject,
    ]
    crashed = subprocess.run([*entry, "--interrupt"], capture_output=True, timeout=25)
    assert crashed.returncode == 86, (
        "first_central_worker_did_not_stop_after_durable_claim"
    )
    restarted = subprocess.run(entry, capture_output=True, timeout=30)
    assert restarted.returncode == 0, "fresh_central_planner_failed"
    proof = json.loads(restarted.stdout)
    assert proof["completed"] == proof["restarted"] == 1
    stored = fixture.service.task_plans.get(plan_id, user_subject=user_subject)
    assert stored["state"] == "succeeded" and stored["steps"][0]["attempt_count"] == 2
    return {"status": "succeeded", "plan": task_plan_response(stored)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--home", required=True, type=Path)
    p.add_argument("--plan", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--interrupt", action="store_true")
    args = p.parse_args()
    assert args.home.name == "matrix" and args.home.parent.name.startswith(
        "nanobot-controlled-action-"
    )
    fixture = ActionMatrixAcceptanceFixture(args.home, seed=False)
    try:
        service = fixture.service
        identity = McpIdentityTokenStore(service.db_path)
        identity.user_grants = service.user_grants
        service.set_task_plan_authority_resolver(
            lambda token, scopes: identity.resolve_client(
                token, required_scopes=set(scopes)
            )
        )
        plan = service.task_plans.get(args.plan, user_subject=args.subject)
        assert (
            service.tasks.get_task(plan["parent_task_id"], user_subject=args.subject)[
                "agent_host"
            ]
            == "nanobot"
        )
        if args.interrupt:
            row = service.task_plans.begin_next_step(
                args.plan, user_subject=args.subject
            )
            assert (
                row["state"] == "running"
                and row["capability_name"] == "oa.workflow.pending.list"
            )
            os._exit(86)
        result = service.recover_task_plans()
        assert not fixture.matrix["effects"]
        print(
            json.dumps(
                {"restarted": result["restarted"], "completed": result["completed"]}
            )
        )
    finally:
        fixture.close()
