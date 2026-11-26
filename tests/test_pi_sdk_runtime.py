"""Pi capability ceilings and the original admission survive concurrency and process loss."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/pi-agentbridge"))
from pi_agentbridge.central import declared_profile
from pi_agentbridge.ledger import Conflict, Ledger


@pytest.mark.parametrize(("configured", "expected"), [(None,["L1"]),("L1",["L1"]),("L2",["L1","L2"]),("L3",["L1","L2","L3"])])
def test_requested_level_caps_declaration_even_when_every_implementation_is_installed(configured, expected):
    spec = {"instanceId":"pi-test", "trustedCardOrigins":["https://fixture.invalid"], "coordinatorProgram":"/fixture/program"}
    if configured is not None:
        spec["requiredLevel"] = configured
    profile = declared_profile(spec)
    assert profile["levels"] == expected
    assert profile["capabilities"]["privateResultMeta"] == ("L2" in expected)
    assert profile["capabilities"]["coordinatorLease"] == ("L3" in expected)


def test_invalid_configured_level_fails_closed():
    with pytest.raises(ValueError):
        declared_profile({"instanceId":"pi-test", "requiredLevel":"L4"})


def test_planning_control_is_bound_to_one_run_and_expires_without_replay(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "integrations/nanobot-agentbridge"))
    from pi_agentbridge import planning
    policy = planning.PlanningRepair(None)
    control = {"status":"planning_control","error":{"code":"PLAN_REQUIRED"},"taskId":"source-a"}
    with pytest.raises(PermissionError):
        policy.capture(control,"source-b","oa_workflow_done_list")
    assert not policy.source
    now = [10]
    monkeypatch.setattr(planning.time,"monotonic",lambda:now[0])
    assert "PLAN_REQUIRED" in policy.capture(control,"source-a","oa_workflow_done_list")
    assert policy.visible({planning.PLAN,planning.CATALOG,"oa_leave_prepare"}) == sorted({planning.PLAN,planning.CATALOG})
    now[0] += 301
    with pytest.raises(PermissionError):
        policy.capture(control,"source-a","oa_workflow_done_list")
    assert policy.visible({planning.PLAN,planning.CATALOG}) == []
    assert "尚未核验" in policy.final_answer("模型自称已提交")
    policy.used = True
    policy.observe({"plan":{"state":"failed","terminalReason":"PLAN_SOURCE_INCOMPLETE"}})
    assert "未提交业务写入" in policy.final_answer("模型自称已提交")


def test_ledger_survives_same_key_retry_listener_disconnect_and_lost_process(tmp_path):
    async def exercise():
        key = json.dumps(["owner","endpoint","web:key","session","credential"])
        other = json.dumps(["owner","other","web:other","another","credential"])
        ledger = Ledger(tmp_path, "pi-test")
        entered = asyncio.Event()
        calls = []
        async def run(run_id, history, emit):
            calls.append((run_id,history))
            emit("delta","first")
            entered.set()
            await asyncio.Event().wait()
        a = ledger.accept("owner",key,"original","same-payload","prompt",run)
        await entered.wait()
        b = ledger.accept("owner",key,"original","same-payload","prompt",run)
        assert a["id"] == b["id"]
        assert [event["kind"] for event in ledger.events(a["id"], 0)] == ["accepted","delta"]
        with pytest.raises(Conflict):
            ledger.accept("owner",key,"original","other-payload","prompt",run)
        with pytest.raises(Conflict):
            ledger.accept("owner",other,"original","same-payload","prompt",run)
        with pytest.raises(KeyError):
            ledger.snapshot(a["id"],subject="other",endpoint="endpoint",conversation="session")
        assert ledger.lookup(other,"original") is None
        with pytest.raises(BlockingIOError):
            Ledger(tmp_path,"pi-test")
        await ledger.close()
        recovered = Ledger(tmp_path,"pi-test")
        assert recovered.lookup(key,"original")["state"] == "unknown"
        original = recovered.accept("owner",key,"original","same-payload","prompt",run)
        assert original["id"] == a["id"] and original["state"] == "unknown" and len(calls) == 1
        assert [e["kind"] for e in recovered.events(a["id"],0)] == ["accepted","delta","unknown"]
        await recovered.close()
        with pytest.raises(ValueError):
            Ledger(tmp_path,"second-instance")
    asyncio.run(exercise())


def test_original_telegram_query_flag_survives_terminal_poll_and_is_scoped(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
    from nanobot_agentbridge.task_context import TaskContext
    from pi_agentbridge.server import Host, binding_key
    from starlette.exceptions import HTTPException

    owner = dict(user_subject="alpha", endpoint_id="original", endpoint_key="telegram:8801:501",
                 session_key="original-conversation", credential_ref="original-pair")
    other = {**owner, "endpoint_id":"second", "endpoint_key":"workspace:instance:second"}
    run = {"id":"private-run", "state":"completed", "answer":"成功"}
    host = object.__new__(Host)
    host.bindings = {binding_key(other): other}
    host.telegram = SimpleNamespace(verified=AsyncMock(side_effect=lambda key: owner if key == binding_key(owner) else None))
    host.ledger = SimpleNamespace(lookup=lambda scope, key: run if key == "query" and scope == json.dumps(binding_key(owner)) else None,
                                  events=lambda run_id, after: [])
    host.coordinator = SimpleNamespace(waiting=lambda *unused: None)
    host.cards = SimpleNamespace(waiting=lambda *unused: None)
    host.user_directory = lambda unused: tmp_path
    host.level = "L3"
    context = TaskContext(None, {"userDir":tmp_path, "runId":run["id"], "endpoint":owner["endpoint_id"],
                                 "endpointKey":owner["endpoint_key"]}, {}, host_type="pi")
    context.meta = {"io.agentbridge/task":{"taskId":"source-task"}}
    with sqlite3.connect(context.db) as db:
        db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (run["id"], owner["endpoint_id"], "source-task", 7))
    receipt = SimpleNamespace(isError=False, meta=None, structuredContent={"status":"succeeded",
                              "result":{"items":[{"affair_id":"synthetic"}], "coverage":{"status":"complete"}}})
    context.observe_read("oa_workflow_pending_list", receipt)

    async def exercise():
        body = {"binding":owner, "requestKey":"query", "runId":run["id"]}
        for state in ("accepted", "completed", "stopped", "unknown"):
            run["state"] = state
            expected = state in {"accepted", "completed"}
            for action in ("lookup", "events"):
                assert (await host.action(action, body))["hasResultView"] == expected
        run["state"] = "completed"
        assert await host.action("lookup", {**body, "binding":other}) == {"runId":None}
        with pytest.raises(HTTPException) as exc:
            await host.action("events", {**body, "binding":other})
        assert exc.value.status_code == 403
        host.level = "L2"
        assert not (await host.action("lookup", body))["hasResultView"]
        assert await host.action("result-view", body) == {"available":False}

    asyncio.run(exercise())
