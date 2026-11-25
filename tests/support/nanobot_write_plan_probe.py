"""Run a two-step Telegram plan through central authority, a real SDK, and isolated Workspace approval."""

import argparse
import json
import sqlite3
from functools import partial
from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from tests.support.nanobot_controlled_write_probe import ORIGIN, run
from tests.support.nanobot_telegram_write_probe import exercise as approve_original_task
from tests.test_action_matrix_acceptance import decide, effects
from tests.support.action_matrix_acceptance import OWNER


def configure_model(model, outcome):
    calls = []
    proposal = {
        "schema_version": "agentbridge.task-plan.proposal.v1",
        "goal": "先查待办，再准备请假草稿",
        "steps": [
            {
                "stepKey": "read",
                "kind": "capability",
                "capabilityName": "oa.workflow.pending.list",
                "arguments": {"limit": 2},
            },
            {
                "stepKey": "draft",
                "kind": "capability",
                "capabilityName": "oa.leave.prepare",
                "dependsOn": ["read"],
                "arguments": {},
            },
        ],
    }

    if outcome == "v2":
        from tests.test_task_plan_validation import TaskPlanValidationTests

        proposal = TaskPlanValidationTests().proposal_v2()
        proposal["schema_version"] = proposal.pop("schemaVersion")
        proposal["constraints"] = {
            "temporal": {
                "kind": "absolute_range",
                "start": "2026-08-30",
                "end": "2026-08-30",
            }
        }

    def answer(data):
        assert ORIGIN not in json.dumps(data) and "/flow/" not in json.dumps(data)
        if not calls:
            calls.append(1)
            return None, {
                "id": "bound_plan",
                "type": "function",
                "function": {
                    "name": "mcp_ab_agentbridge_task_plan_prepare",
                    "arguments": json.dumps(proposal),
                },
            }
        result = next(
            row["content"]
            for row in reversed(data["messages"])
            if row["role"] == "tool"
        )
        model.results.append(result)
        return result, None

    model.reply = answer


def exercise(outcome="approve", **ctx):
    configure_model(ctx["model"], outcome)
    if outcome == "v2":
        from tests.test_plan_write_boundaries import PlanWriteBoundaryTests

        synthetic_read = partial(
            PlanWriteBoundaryTests.read, SimpleNamespace(read_calls=0)
        )
        with patch.object(
            ctx["fixture"].service.adapter,
            "invoke_capability",
            side_effect=synthetic_read,
        ):
            receipt = approve_original_task(
                **ctx, capability="taihua.work_log.create.prepare"
            )
        with sqlite3.connect(ctx["fixture"].service.db_path) as db:
            assert db.execute("SELECT state FROM task_plans").fetchall() == [
                ("succeeded",)
            ]
            assert (
                db.execute(
                    "SELECT state FROM task_plan_steps ORDER BY ordinal"
                ).fetchall()
                == [("succeeded",)] * 5
            )
        return {**receipt, "v2Plan": "succeeded", "steps": 5}
    if outcome != "approve":
        return close_without_success(ctx, outcome)
    receipt = approve_original_task(**ctx)
    with sqlite3.connect(ctx["fixture"].service.db_path) as db:
        rows = db.execute("SELECT plan_id,state FROM task_plans").fetchall()
        assert len(rows) == 1 and rows[0][1] == "succeeded", rows
        assert db.execute(
            "SELECT state FROM task_plan_steps WHERE plan_id=? ORDER BY ordinal",
            (rows[0][0],),
        ).fetchall() == [("succeeded",), ("succeeded",)]
    return {**receipt, "twoStepPlan": "succeeded", "steps": 2}


def close_without_success(ctx, outcome):
    fixture = ctx["fixture"]
    gateway = ctx["gateway"]
    route = gateway.resolve_telegram_sender(8001, 501)
    req = {"idempotency_key": "synthetic-plan-" + outcome}
    prompt = "核对计划办理条件。"
    observed = None
    for event in gateway.send_dispatch(req, route, prompt, [], None):
        if event.get("type") == "accepted":
            original = event["runId"]
        if event.get("kind") != "private_interaction" or observed is not None:
            continue
        with sqlite3.connect(fixture.service.db_path) as db:
            observed = db.execute("SELECT plan_id FROM task_plans").fetchone()[0]
            source = db.execute(
                "SELECT resource_id FROM interactions WHERE interaction_type='business_input'"
            ).fetchone()[0]
        if outcome == "cancel":
            fixture.service.cancel_task_plan(user_subject=OWNER, plan_id=observed)
        elif outcome == "expire":
            old_clock = fixture.service.field_submissions.clock
            fixture.service.field_submissions.clock = lambda: (
                old_clock() + timedelta(days=2)
            )
            fixture.service.field_submissions.get(source)
        else:
            fixture.matrix["fault"] = "lost_reply"
            decide(fixture, {"input_id": source, "capability": "oa.leave.prepare"})
    assert observed
    fixture.service.recover_task_plans()
    before = effects(fixture)
    repeated = list(gateway.send_dispatch(req, route, prompt, [], None))
    assert repeated[0]["runId"] == original
    with sqlite3.connect(fixture.service.db_path) as db:
        state = db.execute(
            "SELECT state FROM task_plans WHERE plan_id=?", (observed,)
        ).fetchone()[0]
        commits = db.execute(
            "SELECT COUNT(*) FROM operations WHERE capability_name='oa.leave.save_draft'"
        ).fetchone()[0]
    assert (
        state
        == {"cancel": "canceled", "expire": "failed", "unknown": "outcome_unknown"}[
            outcome
        ]
    ), (outcome, state)
    assert before == effects(fixture) == commits == (1 if outcome == "unknown" else 0)
    assert not any("已核验" in result for result in ctx["model"].results)
    return {"plan": state, "writes": before, "sameRun": True, "replayedWrites": 0}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--nanobot-python", type=Path, required=True)
    p.add_argument(
        "--outcome",
        choices=("approve", "v2", "cancel", "expire", "unknown", "disconnect"),
        default="approve",
    )
    args = p.parse_args()
    print(
        json.dumps(
            run(
                args.nanobot_python,
                False,
                browser=True,
                exercise=partial(exercise, args.outcome),
                pending_restart=args.outcome == "disconnect",
                model_setup=(lambda model: configure_model(model, "approve"))
                if args.outcome == "disconnect"
                else None,
            )
        )
    )
