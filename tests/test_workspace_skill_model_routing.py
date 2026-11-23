"""A delayed skill job keeps its creator and original host through a default change."""

import json

import pytest

from agentbridge.workspace.host_instances import LEGACY_INSTANCE_ID
from tests.test_workspace_host_dispatch_routing import workspace


def test_generation_replays_original_snapshot_and_revocation_stops_model(workspace):
    app, account = workspace
    owner, account_id = account["user_subject"], account["account_id"]
    workbench = app.service.skill_authoring.workbench
    app.hosts.set_default(account_id, "office", expected_revision=1)
    options = {"request_key": "draft-generation", "material": "请提炼每周总结方法"}
    job = workbench.generate(owner, **options)
    with app.store._connect() as db:
        data = json.loads(
            db.execute(
                "SELECT payload_json FROM skill_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0]
        )
    recorded = data["model_route"]
    assert (recorded["instance_id"], recorded["user_subject"]) == ("office", owner)
    app.hosts.set_default(account_id, LEGACY_INSTANCE_ID, expected_revision=2)
    assert workbench.generate(owner, **options)["job_id"] == job["job_id"]
    calls = []

    class TextOnly:
        def skill_completion(self, binding, system, prompt):
            assert binding == recorded and system and options["material"] in prompt
            calls.append(binding["user_subject"])
            return {
                "text": json.dumps(
                    {"kind": "fact", "summary": "合成事实", "proposal": None}
                ),
                "model": "synthetic",
            }

    app.host_routes[recorded["route_ref"]] = TextOnly()
    app.gateway = None
    with pytest.raises(PermissionError):
        app._bound_skill_completion("another-user", recorded, "系统", "材料")
    assert workbench._job_executor().run_once(
        None, bound_completion=app._bound_skill_completion
    )
    assert workbench.jobs(owner, job["job_id"])["state"] == "succeeded"
    assert calls == [owner]
    app.hosts.save_instance(
        "office",
        host_type="nanobot",
        display_name="办公实例",
        route_ref="route-office",
        adapter_version="0.2.0",
        state="revoked",
        subjects=[owner],
        expected_revision=1,
        actor="admin",
        reason="fixture",
    )
    with pytest.raises(PermissionError):
        app._bound_skill_completion(owner, recorded, "系统", "材料")
    assert calls == [owner]


def test_evaluation_and_preupgrade_receipts_keep_their_own_hosts(workspace):
    app, account = workspace
    owner, account_id = account["user_subject"], account["account_id"]
    workbench = app.service.skill_authoring.workbench
    draft = app.service.skill_authoring.save(
        owner,
        proposal={
            "name": "周报方法",
            "description": "整理本周内容",
            "selection": {
                "use_when": "编排周报",
                "not_for": "业务操作",
                "output": "完成和计划",
            },
            "instructions": "输入用户文字。区分计划和完成，检查所有来源。材料不足时追问。",
        },
        request_key="test-draft",
    )
    app.hosts.set_default(account_id, "office", expected_revision=1)
    j = workbench.evaluate(
        owner,
        draft_id=draft["draft_id"],
        expected_revision=1,
        cases=[{"prompt": "计划修复", "contains": ["计划"]}],
        request_key="eval",
    )
    with app.store._connect() as db:
        route = json.loads(
            db.execute(
                "SELECT payload_json FROM skill_jobs WHERE job_id=?", (j["job_id"],)
            ).fetchone()[0]
        )["model_route"]
    assert route["instance_id"] == "office"

    class Legacy:
        def call(self, method, payload, *, timeout_seconds):
            assert method == "agentbridge.skills.complete" and timeout_seconds == 100
            return {"text": "旧任务保留 OpenClaw"}

    app.gateway = Legacy()
    assert app._bound_skill_completion(owner, None, "系统", "材料") == {
        "text": "旧任务保留 OpenClaw"
    }


def test_original_dispatch_material_stays_on_original_host(workspace):
    from agentbridge.workspace.skill_model_route import snapshot

    app, account = workspace
    owner, account_id = account["user_subject"], account["account_id"]
    app.send_chat_stream(
        account,
        message="原工作记录",
        idempotency_key="original",
        instance_id=LEGACY_INSTANCE_ID,
        expected_revision=1,
    )
    app.hosts.set_default(account_id, "office", expected_revision=1)
    with app.store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        original = db.execute(
            "SELECT dispatch_id FROM agent_host_dispatches WHERE user_subject=? AND idempotency_key=?",
            (owner, "original"),
        ).fetchone()[0]
        assert (
            snapshot(app.store, db, owner, source_dispatch=original)["instance_id"]
            == LEGACY_INSTANCE_ID
        )
        assert snapshot(app.store, db, owner)["instance_id"] == "office"
        with pytest.raises(PermissionError):
            snapshot(app.store, db, owner, source_dispatch="absent")
