"""Use the trusted command receipt, its original endpoint and authoritative read operation."""
import asyncio
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/reference-host"))
from reference_host.bound_commands import PENDING_LIST, PREPARE, QUERY, finish_query, query_result
from reference_host.bound_coordinator import BoundDriver, IMPLEMENTATION, PI_IMPLEMENTATION


def reply(data, card=None, *, error=False):
    return SimpleNamespace(structured=data, private_meta={"io.agentbridge/interaction": card} if card else {},
                           is_error=error, payload=lambda: data)


@pytest.fixture
def bound():
    command = {"kind": QUERY, "capabilityName": PENDING_LIST, "commandKey": "fixed",
               "state": "succeeded", "operationId": "read-op"}
    task = {"taskId":"original","userSubject":"person","agentHost":"nanobot","activeConversationRef":"original-web",
            "originEndpointId":"ep","summary":{"command":command},"status":"succeeded","currentOperationId":"read-op",
            "currentInteractionId":"login"}
    operation = {"operationId":"read-op","capability":PENDING_LIST,"userSubject":"person","taskId":"original",
                 "status":"succeeded"}
    journal = sqlite3.connect(":memory:")
    journal.execute("CREATE TABLE coordinated(run TEXT PRIMARY KEY,phase TEXT,interaction TEXT,kind TEXT)")
    journal.execute("INSERT INTO coordinated VALUES ('fixed','starting',NULL,NULL)")
    journal.execute("CREATE TABLE resumes(interaction TEXT PRIMARY KEY)")
    req = {"commandKey":"fixed","runId":"fixed","endpoint":"ep","subject":"person","task":{"taskId":"original"},
           "conversation":"original-web"}
    api = SimpleNamespace(call_tool=AsyncMock(return_value=reply({"operation":operation})))
    driver = SimpleNamespace(req=req, journal=journal, implementation=IMPLEMENTATION, client=lambda _:api, host_context=lambda:{},
                             state=SimpleNamespace(finish_task=Mock()),
                             refresh_task_snapshot=AsyncMock(return_value={"task":task,"endpoint":{"endpointId":"ep"}}))
    result = {"status":"succeeded","capability":PENDING_LIST,"commandKind":QUERY,"operationId":"read-op",
              "queryResult":{"kind":QUERY,"taskId":"original","endpointId":"ep","count":0,"complete":True}}
    yield SimpleNamespace(driver=driver,task=task,cmd=command,op=operation,api=api,data=result)
    journal.close()


def test_pi_and_nanobot_commands_cannot_share_origin(bound):
    from reference_host.bound_commands import command_snapshot
    bound.driver.implementation = PI_IMPLEMENTATION
    with pytest.raises(PermissionError):
        asyncio.run(command_snapshot(bound.driver, "local"))
    bound.task["agentHost"] = "pi"
    assert asyncio.run(command_snapshot(bound.driver, "local"))[0] is bound.task


@pytest.mark.parametrize("resumed", [False, True])
def test_only_actual_fixed_read_completes_local_query(bound, resumed):
    b=bound
    if resumed:
        b.data.update(resumedFromInteractionId="login",taskId="original",nextAction={"type":"original_request_completed"})
    assert asyncio.run(query_result(b.driver,"local",reply(b.data),"login" if resumed else None)) is False
    assert b.driver.journal.execute("SELECT phase FROM coordinated").fetchone()==("verified",)
    assert b.api.call_tool.await_args.args[:2]==("agentbridge_operation_get",{"operation_id":"read-op"})


@pytest.mark.parametrize("change",["subject","origin","session","command","cap","receipt","read-foreign","nonterminal"])
def test_other_origin_capability_or_receipt_cannot_finish(bound, change):
    b=bound
    if change=="subject": b.task["userSubject"]="other"
    if change=="origin": b.task["originEndpointId"]="other"
    if change=="session": b.task["activeConversationRef"]="other"
    if change=="command": b.cmd["commandKey"]="other"
    if change=="cap": b.data["capability"]="oa.workflow.manage"
    if change=="receipt": b.data["queryResult"]["taskId"]="other"
    if change=="read-foreign": b.op["taskId"]="other"
    if change=="nonterminal": b.task["status"]="running"
    with pytest.raises((PermissionError, RuntimeError)):
        asyncio.run(query_result(b.driver,"local",reply(b.data),None))
    b.driver.state.finish_task.assert_not_called()


def test_read_accepts_only_private_current_credential_and_never_reinvokes_prepare(bound):
    b=bound
    b.task["status"]="waiting_user";b.cmd["state"]="requires_user_action"
    data={"commandKind":QUERY,"capability":PENDING_LIST,"status":"requires_user_action","error":{"code":"LOGIN_REQUIRED"}}
    assert asyncio.run(query_result(b.driver,"local",reply(data,{"type":"credential","interactionId":"login"}),None))
    for card in ({"type":"business_input","interactionId":"login"},{"type":"execution_authorization","interactionId":"login"},
                 {"type":"credential","interactionId":"other"}, None):
        with pytest.raises(PermissionError):
            asyncio.run(query_result(b.driver,"local",reply(data,card),None))
    for result in ({"type":"retry_original_request"},{}):
        with pytest.raises(RuntimeError):
            asyncio.run(query_result(b.driver,"local",reply({**data,"nextAction":result}),"login"))
    b.api.call_tool.assert_not_called()


@pytest.mark.parametrize("status",["failed","expired","canceled"])
def test_authoritative_read_failure_or_expiry_is_preserved_without_new_call(bound, status):
    b=bound
    b.task["status"]=status
    assert asyncio.run(finish_query(b.driver,"local",b.task,b.cmd))
    assert b.driver.journal.execute("SELECT phase FROM coordinated").fetchone()[0]==status
    assert b.driver.state.finish_task.call_args.kwargs["result_summary"]["status"]==status
    b.api.call_tool.assert_not_called()


def test_failed_continuation_cannot_report_success(bound):
    b=bound
    b.task["status"]="failed";b.cmd["state"]="failed"
    data={"status":"failed","taskId":"original","resumedFromInteractionId":"login",
          "nextAction":{"type":"original_request_completed"}}
    assert not asyncio.run(query_result(b.driver,"local",reply(data),"login"))
    assert b.driver.journal.execute("SELECT phase FROM coordinated").fetchone()[0]=="failed"
    b.api.call_tool.assert_not_called()


def test_resume_guard_refuses_changed_card_before_any_origin_action(bound):
    b=bound
    b.driver.req["commandKind"]=QUERY
    b.task["status"]="waiting_user"
    b.driver.check_telegram=AsyncMock()
    b.driver.journal.execute("UPDATE coordinated SET interaction='login',kind='credential',phase='waiting'")
    with pytest.raises(PermissionError):
        asyncio.run(BoundDriver.resume_interaction(b.driver,"local","other"))
    assert not b.driver.journal.execute("SELECT 1 FROM resumes").fetchone()
    b.api.call_tool.assert_not_called()


def test_lost_query_resume_reconciles_by_read_only_snapshot(bound, monkeypatch):
    from reference_host.task_driver import ReferenceTaskDriver
    b=bound
    b.driver.req["commandKind"]=QUERY
    b.driver.check_telegram=AsyncMock()
    b.driver.journal.execute("UPDATE coordinated SET interaction='login',kind='credential',phase='waiting'")
    driver = object.__new__(BoundDriver)
    driver.__dict__.update(b.driver.__dict__)
    b.task["status"] = "waiting_user"
    async def lost(*_):
        b.task["status"] = "succeeded"
        raise OSError("synthetic response loss")
    resume=AsyncMock(side_effect=lost)
    monkeypatch.setattr(ReferenceTaskDriver, "resume_interaction", resume)
    asyncio.run(driver.resume_interaction("local","login"))
    resume.assert_awaited_once()
    assert b.driver.journal.execute("SELECT phase FROM coordinated").fetchone()[0]=="verified"
    assert b.api.call_tool.await_args.args[0]=="agentbridge_operation_get"


@pytest.mark.parametrize(("host", "command", "plan", "batch", "optimized"), [
    ("pi", PREPARE, False, False, True),
    ("pi", None, False, False, False),
    ("pi", QUERY, False, False, False),
    ("pi", PREPARE, True, False, False),
    ("pi", PREPARE, False, True, False),
    ("nanobot", PREPARE, False, False, False),
    ("nanobot", None, False, False, False),
])
def test_only_fixed_single_pi_resume_opts_into_observation(bound, monkeypatch, host, command, plan, batch, optimized):
    from reference_host import bound_coordinator
    from reference_host.task_driver import ReferenceTaskDriver

    b = bound
    b.driver.implementation = PI_IMPLEMENTATION if host == "pi" else IMPLEMENTATION
    b.task.update(agentHost=host, status="waiting_user")
    b.driver.req["commandKind"] = command
    if plan:
        b.driver.req["boundPlanId"] = "original-plan"
    b.driver.journal.execute("CREATE TABLE batch_scope(run TEXT PRIMARY KEY,batch TEXT NOT NULL)")
    if batch:
        b.driver.journal.execute("INSERT INTO batch_scope VALUES('fixed','original-batch')")
    b.driver.journal.execute("UPDATE coordinated SET interaction='login',kind='credential',phase='waiting'")
    b.driver.check_telegram = AsyncMock()
    skill = AsyncMock()
    monkeypatch.setattr(bound_coordinator, "check_skill", skill)
    driver = object.__new__(BoundDriver)
    driver.__dict__.update(b.driver.__dict__)

    def claimed_before_resume(local_task_id, interaction_id):
        assert (local_task_id, interaction_id) == ("local", "login")
        assert b.driver.journal.execute("SELECT interaction FROM resumes").fetchall() == [("login",)]
        assert b.driver.journal.execute("SELECT phase FROM coordinated").fetchone() == ("resuming",)
        driver.check_telegram.assert_awaited_once()
        skill.assert_awaited_once_with(driver)

    async def legacy_resume(local_task_id, interaction_id):
        claimed_before_resume(local_task_id, interaction_id)

    async def fixed_pi_resume(local_task_id, interaction_id, *, with_observation):
        assert with_observation is True
        claimed_before_resume(local_task_id, interaction_id)

    resume = AsyncMock(side_effect=fixed_pi_resume if optimized else legacy_resume)
    monkeypatch.setattr(ReferenceTaskDriver, "resume_interaction", resume)
    asyncio.run(driver.resume_interaction("local", "login"))
    if command == QUERY:
        with pytest.raises(PermissionError):
            asyncio.run(driver.resume_interaction("local", "login"))
    else:
        asyncio.run(driver.resume_interaction("local", "login"))
    if optimized:
        resume.assert_awaited_once_with("local", "login", with_observation=True)
    else:
        resume.assert_awaited_once_with("local", "login")


@pytest.fixture
def queue_type(monkeypatch):
    import importlib.util
    integration=Path(__file__).resolve().parents[1]/"integrations/nanobot-agentbridge"
    monkeypatch.syspath_prepend(str(integration))
    monkeypatch.setitem(sys.modules,"aiohttp",SimpleNamespace(web=SimpleNamespace(HTTPForbidden=PermissionError)))
    monkeypatch.setitem(sys.modules,"nanobot_agentbridge.worker",SimpleNamespace(authenticated_session=AsyncMock()))
    spec=importlib.util.spec_from_file_location("nanobot_agentbridge.test_bound_queue",integration/"nanobot_agentbridge/commands.py")
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.CommandQueue


def test_each_new_observer_watches_existing_card_without_replaying_fixed_command(tmp_path, queue_type):
    q=queue_type(SimpleNamespace(state=tmp_path))
    q.observe=AsyncMock(side_effect=["waiting","done"])
    with sqlite3.connect(q.path) as db:
        db.execute("INSERT INTO commands VALUES('cmd','person','ep','web','task','queued')")
    asyncio.run(q.execute({"commandKey":"cmd"}))
    assert q.observe.await_args_list[0].args[0].get("recoveryOnly") is None
    assert q.observe.await_args_list[1].args[0]["recoveryOnly"] is True
    with sqlite3.connect(q.path) as db:
        assert db.execute("SELECT phase FROM commands").fetchone()[0]=="done"


def test_restart_discovers_only_previously_authenticated_query_card(tmp_path, queue_type):
    q=queue_type(SimpleNamespace(state=tmp_path,worker_payload=lambda *_:{"userDir":str(tmp_path)}))
    record={"endpoint_id":"ep","user_subject":"person","session_key":"web"}
    with sqlite3.connect(q.path) as db:
        db.executemany("INSERT INTO commands VALUES(?,'person','ep','web',?,'unknown')",
                       [(name,"t-"+name) for name in ("read","write","legacy","foreign")])
    with sqlite3.connect(tmp_path/"coordinator-state.sqlite3") as db:
        db.executescript("CREATE TABLE reference_tasks(local_task_id,task_id,endpoint_id,conversation_ref,lease_version);")
        db.executemany("INSERT INTO reference_tasks VALUES(?,?,'ep','web',2)",[(x,"t-"+x) for x in ("read","write","legacy","foreign")])
    with sqlite3.connect(tmp_path/"coordinated-private.sqlite3") as db:
        db.executescript("CREATE TABLE coordinated(run,endpoint,central_task,local_task,kind,phase);"
                         "CREATE TABLE query_commands(run,local_task,endpoint);")
        db.executemany("INSERT INTO coordinated VALUES(?,?,?,?,?,?)",[
            ("read","ep","t-read","read","credential","query_observe"),
            ("write","ep","t-write","write","execution_authorization","waiting"),
            ("legacy","ep","t-legacy","legacy","credential","unknown"),
            ("foreign","other","t-foreign","foreign","credential","waiting")])
        db.executemany("INSERT INTO query_commands VALUES(?,?,?)",[("read","read","ep"),("foreign","foreign","other")])
    q.execute=AsyncMock()
    async def check():
        q.recover_queries(record)
        await asyncio.gather(*q.jobs.values())
        q.recover_queries(record)
    asyncio.run(check())
    q.execute.assert_awaited_once()
    work=q.execute.await_args.args[0]
    assert work["recoveryOnly"] is True and work["task"]["taskId"]=="t-read" and set(q.jobs)=={"read"}
