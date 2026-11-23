"""Run a centrally pinned write-capable skill through its original controlled action contract."""

import argparse
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from agentbridge.core.business_drafts import BusinessDraftError
from tests.support.business_draft_acceptance import OWNER
from tests.support.nanobot_controlled_write_probe import run
from tests.test_action_matrix_acceptance import decide, effects


def exercise(
    *, fixture, binding, instance, gateway, model, revoked=False, single=False
):
    skill, profile = ("oa-pending", "single") if single else ("oa-work-log", "fill")
    expected_version = fixture.service.skills.current(skill)["manifest"]["version"]
    args = {}
    if single:
        args = {"affair_id": "synthetic-skill-attendance"}
        fixture.oa.add_affair(args["affair_id"], profile="attendance_confirmation")
    current = fixture.service.skills.config("user:" + OWNER)
    fixture.service.skills.save(
        "user:" + OWNER,
        {skill: {"profiles": [profile]}},
        expected_revision=current["revision"],
        actor="fixture",
        reason="controlled workspace sample",
    )
    cap = (
        "oa.attendance_confirmation.prepare"
        if single
        else "taihua.work_log.create.prepare"
    )
    tool = "mcp_ab_" + cap.replace(".", "_")
    seal = "mcp_ab_agentbridge_skill_get"

    observed = []

    def reply(data):
        msgs = data["messages"]
        tools = [m for m in msgs if m.get("role") == "tool"]
        names = {item["function"]["name"] for item in data["tools"]}
        if not tools:
            name, arguments = (
                seal,
                {
                    "skill_id": skill,
                    "profile": profile,
                    "expected_version": expected_version,
                },
            )
        elif len(tools) == 1:
            assert "SKILL.md" in str(tools[-1]["content"])
            assert tool in names and "mcp_ab_oa_leave_prepare" not in names
            name, arguments = tool, args
        else:
            observed.append(str(tools[-1]["content"]))
            return observed[-1], None
        return None, {
            "id": uuid4().hex,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    model.reply = reply
    key = {"idempotency_key": "skill-write-" + uuid4().hex}
    decided = False
    for event in gateway.send_dispatch(
        key, binding, "用工作日志助手核对并填写合成日志，在本人确认后提交。", [], None
    ):
        if event["type"] == "accepted":
            rid = event["runId"]
        if event.get("kind") != "private_interaction" or decided:
            continue
        assert gateway.private_card(key, binding, rid)["available"]
        with sqlite3.connect(fixture.service.db_path) as db:
            row = db.execute(
                "SELECT submission_id FROM field_submissions WHERE user_subject=? AND capability_name=? ORDER BY rowid DESC LIMIT 1",
                (OWNER, cap),
            ).fetchone()
            assert row
            assert db.execute(
                "SELECT COUNT(*) FROM skill_task_bindings WHERE user_subject=?",
                (OWNER,),
            ).fetchone() == (1,)
        assert effects(fixture) == 0
        if revoked:
            current = fixture.service.skills.config("user:" + OWNER)
            fixture.service.skills.save(
                "user:" + OWNER,
                {},
                expected_revision=current["revision"],
                actor="fixture",
                reason="revoke before human action",
            )
        try:
            result = decide(fixture, {"capability": cap, "input_id": row[0]})
        except BusinessDraftError as exc:
            assert revoked and exc.status == 403
        else:
            assert not revoked and result
        decided = True
        if revoked:
            break
    if revoked:
        assert decided and effects(fixture) == 0
    else:
        assert decided and effects(fixture) == 1, (decided, effects(fixture))
        with sqlite3.connect(fixture.service.db_path) as db:
            count = db.execute(
                """SELECT COUNT(*) FROM skill_load_events e JOIN skill_task_bindings b ON b.binding_id=e.dedupe_key
                      JOIN agent_tasks t ON t.task_id=b.task_id WHERE t.host_task_key=? AND t.user_subject=?""",
                (rid, OWNER),
            ).fetchone()[0]
        assert count == 1 and len(observed) == 1 and "已核验" in observed[0]
    return {
        "boundOriginalTask": True,
        "currentScope": "revoked" if revoked else profile,
        "centrallyAuthorizedEffects": effects(fixture),
        "otherPrepareHidden": True,
    }


def preview_plan(*, fixture, binding, instance, gateway, model):
    rev = fixture.service.skills.config("user:" + OWNER)["revision"]
    fixture.service.skills.save(
        "user:" + OWNER,
        {"oa-work-log": {"profiles": ["preview"]}},
        expected_revision=rev,
        actor="fixture",
        reason="plan scope",
    )
    ready = []

    def reply(data):
        tools = [m for m in data["messages"] if m.get("role") == "tool"]
        if not tools:
            name, args = (
                "agentbridge_skill_get",
                {
                    "skill_id": "oa-work-log",
                    "profile": "preview",
                    "expected_version": "1.1.2",
                },
            )
        elif len(tools) <= 2:
            if len(tools) == 2:
                assert "不在原业务助手" in str(tools[-1]["content"])
            cap = (
                "oa.leave.save_draft" if len(tools) == 1 else "oa.workflow.pending.list"
            )
            name, args = (
                "agentbridge_task_plan_prepare",
                {
                    "goal": "核对合成待办",
                    "steps": [
                        {
                            "kind": "capability",
                            "stepKey": "list",
                            "capabilityName": cap,
                            "arguments": {} if len(tools) == 1 else {"limit": 2},
                        }
                    ],
                },
            )
        else:
            assert "计划状态" in str(tools[-1]["content"])
            ready.append(True)
            return "核对计划已登记。", None
        return None, {
            "id": uuid4().hex,
            "type": "function",
            "function": {"name": "mcp_ab_" + name, "arguments": json.dumps(args)},
        }

    model.reply = reply
    for _ in gateway.send_dispatch(
        {"idempotency_key": "skill-plan-" + uuid4().hex},
        binding,
        "用工作日志预览助手拟合成待办计划。",
        [],
        None,
    ):
        pass
    assert ready == [True] and effects(fixture) == 0
    return {
        "skillMode": "preview",
        "unauthorizedStepDenied": True,
        "authorizedPlan": True,
        "controlledEffects": 0,
    }


if __name__ == "__main__":
    from functools import partial

    parser = argparse.ArgumentParser()
    parser.add_argument("--nanobot-python", required=True, type=Path)
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--single", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.nanobot_python,
                False,
                skill_scope=True,
                exercise=preview_plan
                if args.plan
                else partial(exercise, revoked=args.revoke, single=args.single),
            )
        )
    )
