"""Exercise both central PLAN_REQUIRED signals through the real Pi SDK and private scope."""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import threading

from agentbridge.core.transforms import OA_HISTORY_REPORT
from tests.authorization_fixtures import grant_permissions
from tests.support.pi_catalog_history_probe import NEXT, QUERY, packet, tool
from tests.support.pi_governed_probe import exercise
from tests.support.pi_skills_probe import chat
from tests.test_oa_history_query_service import row

PLAN, CATALOG, SECOND = "agentbridge_task_plan_prepare", "agentbridge_task_plan_catalog", "oa_workflow_done_list"


class Model:
    def __init__(self, central):
        self.failures = []
        self.stages = []
        self.rows = []
        self.cursor = None

    def reply(self, body):
        last = body["messages"][-1]
        names = {t["function"]["name"] for t in body.get("tools") or []}
        stage = len(self.stages)
        self.stages.append(stage)
        if stage == 0:
            return tool(QUERY, {"collection":"done","temporal":{"kind":"all"},"limit":1})
        if stage == 1:
            assert packet(last["content"])["result"]["has_more"] is True
            return tool(SECOND, {"limit":2})
        if stage == 2:
            assert "PLAN_REQUIRED" in str(last["content"]) and names == {PLAN,CATALOG}, ("control", names)
            return tool(CATALOG, {"view":"detail", "kind":"transform", "name":OA_HISTORY_REPORT})
        if stage == 3:
            assert packet(last["content"])["name"] == OA_HISTORY_REPORT
            if self.mode == "forbidden-sink":
                return tool(PLAN, {"goal":"额外写入草稿", "steps":[{"stepKey":"hidden","kind":"capability",
                    "capabilityName":"oa.leave.prepare","arguments":{}}]})
            return tool(PLAN, {"schema_version":"agentbridge.task-plan.proposal.v2","goal":"只读个人历史报告",
                "constraints":{"temporal":{"kind":"all"}}, "steps":[
                    {"stepKey":"source","kind":"capability","capabilityName":"oa.workflow.history.query",
                     "arguments":{"collection":"done","temporal":{"kind":"all"},"limit":1}},
                    {"stepKey":"report","kind":"transform","transformName":OA_HISTORY_REPORT,"dependsOn":["source"],
                     "bindings":{"report":{"step":"source","pointer":"","mode":"single"}}}]})
        if stage == 4:
            if self.mode == "forbidden-sink":
                assert not names and "尚未开放" in str(last["content"])
                return "原任务未授权额外写入。", None
            data = packet(last["content"])
            assert names == {NEXT} and data["计划状态"] == "succeeded", ("plan state", data.get("计划状态"), names)
            self.cursor = data["nextAction"]["arguments"]["cursor"]
            return tool(NEXT, {"cursor":"another-unverified-cursor"})
        if stage == 5:
            assert "原计划签发" in str(last["content"])
            return tool(NEXT, {"cursor":self.cursor})
        page = packet(last["content"])
        self.rows.extend(r["title"] for r in page["items"])
        if page["has_more"]:
            assert names == {NEXT}
            return tool(NEXT, {"cursor":page["next_cursor"]})
        assert not names
        return "原计划已按原游标读完三项。", None


class Scenario:
    model = Model

    def __init__(self, mode):
        self.mode = mode

    def check(self, central, post, model, accounts):
        model.mode = self.mode
        svc = central.service
        owner = central.subjects["a"]
        grant_permissions(svc.user_grants, owner, set(svc.user_grants.get(owner)["permissions"]) | {"oa.workflow.read"})
        rows = [row(i, title="合成事项"+str(i)) for i in (1,2,3)]
        svc.oa_history_query_service.read_page = lambda *_a, **_k: {"page":1,"size":50,"pages":1,"total":3,"items":rows}
        # Simulate a source becoming visible after preflight. The business call
        # itself still uses the real central gate, plan, journal and synthetic data.
        inside = threading.local()
        ensure, gate = svc.ensure_host_task, svc.planning_gate_for_call
        def observe(**args):
            inside.preflight = self.mode == "business" and args.get("tool_name") == SECOND
            try:
                return ensure(**args)
            finally:
                inside.preflight = False
        svc.ensure_host_task = observe
        svc.planning_gate_for_call = lambda **kwargs: None if getattr(inside,"preflight",False) else gate(**kwargs)
        try:
            reply = chat(post,accounts["a"],"请查本人历史，只读取并汇总所有已办事项。")
            if self.mode == "forbidden-sink":
                assert "尚未核验" in reply and not central.matrix.matrix["effects"] and not central.matrix.oa.writes
                with sqlite3.connect(svc.db_path) as db:
                    assert db.execute("SELECT COUNT(*) FROM task_plans").fetchone()[0] == 0
                return {"signal":self.mode,"plans":0,"writes":0,"repairAttemptConsumed":True}
            assert reply.endswith("三项。")
        except (ValueError, AssertionError):
            with sqlite3.connect(svc.db_path) as db:
                states = list(db.execute("SELECT capability_name,status,error_code FROM operations"))
                plans = list(db.execute("SELECT state,terminal_reason FROM task_plans"))
            raise AssertionError((self.mode,len(model.stages),model.failures,states,plans)) from None
        assert model.rows == [r["title"] for r in rows] and not model.failures
        with sqlite3.connect(svc.db_path) as db:
            plans = db.execute("SELECT plan_id,parent_task_id,state FROM task_plans WHERE user_subject=?",(owner,)).fetchall()
            assert len(plans) == 1 and plans[0][2] == "succeeded"
            tasks = [(t,json.loads(s)) for t,s in db.execute("SELECT task_id, summary_json FROM agent_tasks WHERE user_subject=? AND agent_host='pi'",(owner,))]
            linked = dict(tasks)[plans[0][1]]["planRepair"]["sourceTaskId"]
            assert linked != plans[0][1] and any(s.get("piReportPlan")==plans[0][0] for _,s in tasks)
            denied = db.execute("SELECT COUNT(*) FROM operations WHERE error_code='PLAN_REQUIRED'").fetchone()[0]
            assert denied == int(self.mode == "business")
        async def deny_foreign_or_stale():
            from tests.support.pi_authority_probe import AgentBridgeMcpClient, PI, host_context_meta
            context = host_context_meta(instance_id="pi-office",implementation=PI)
            directory = svc.home.parent
            original = svc.tasks.get_task(linked, user_subject=owner)
            async def ensure(label, plan_id, run_key):
                web = accounts[label]
                caller = AgentBridgeMcpClient(mcp_url=central.meta["endpoint"],
                     token_provider=lambda:(directory / ("pi-"+label+".token")).read_text(),private_transport=True)
                return await caller.call_tool("agentbridge_host_task_ensure",{
                    "agent_host":"pi", "host_task_key":run_key + ":plan-report", "task_scope":"independent",
                    "external_subject":web["endpoint_key"].split(":")[-1],"endpoint_key":web["endpoint_key"],
                    "client_type":"web","conversation_ref":web["session_key"],"tool_name":NEXT,
                    "planning_report_plan_id":plan_id},meta=context)
            key, pid = original["host_task_key"], plans[0][0]
            for label, selected, claimed in [("b",pid,key),("a","unregistered-private-report",key),("a",pid,"different-run")]:
                refused = await ensure(label, selected, claimed)
                assert refused.is_error or not refused.payload().get("task")
            old_grants = svc.user_grants.get(owner)["permissions"]
            grant_permissions(svc.user_grants,owner,set(old_grants) - {"oa.workflow.read"})
            revoked = await ensure("a",pid,key)
            assert revoked.is_error or not revoked.payload().get("task")
        asyncio.run(deny_foreign_or_stale())
        assert not central.matrix.matrix["effects"] and not central.matrix.oa.writes
        return {"signal":self.mode,"plans":1,"sourceTaskLinked":True,"repairedReadRows":3,"wrongCursorDenied":True,
                "rejectedBusinessOperations":denied,"foreignPlanDenied":True,"revokedReportDenied":True,"writes":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("node","sdk-dir","engine-python"):
        parser.add_argument("--"+key,type=Path,required=True)
    parser.add_argument("--signal",choices=("preflight","business","forbidden-sink"))
    args = parser.parse_args()
    for mode in [args.signal] if args.signal else ["preflight","business","forbidden-sink"]:
        print(json.dumps(exercise(args.node,args.sdk_dir,args.engine_python,"repair",scenario=Scenario(mode))), flush=True)
