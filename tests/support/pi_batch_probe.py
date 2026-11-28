"""Two Pi-origin pending approvals, two distinct cards and authoritative batch verification."""
import argparse
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

from tests.support.business_draft_acceptance import OWNER
from tests.support.interaction_acceptance import free_port
from tests.support.pi_governed_probe import MatrixCentral, ORIGIN
from tests.support.pi_sdk_probe import ROOT, host_fixture, private
from tests.support.pi_workspace_acceptance import central_fixture
from tests.test_action_matrix_acceptance import preview_case
from tests.test_workspace import _create_account

AFFAIRS = ["pi-batch-fixture-attendance","pi-batch-fixture-weekly"]
TOOL = "oa_workflow_pending_batch_prepare"


class BatchModel:
    def __init__(self):
        self.failures = []
        self.calls = 0

    def reply(self, request):
        self.calls += 1
        assert ORIGIN not in json.dumps(request)
        assert TOOL in {tool["function"]["name"] for tool in request["tools"]}
        previous = [m for m in request["messages"] if m["role"] == "tool"]
        if previous:
            assert "已核验" in str(previous[-1]["content"])
            return "两个原始项目已核验。",None
        return None,{"id":"batch-prepare","type":"function","function":{"name":TOOL,
                               "arguments":json.dumps({"affair_ids":AFFAIRS})}}


class PlanModel:
    def __init__(self):
        self.failures, self.calls = [], 0

    def reply(self, request):
        self.calls += 1
        previous = [m for m in request["messages"] if m["role"] == "tool"]
        tool = "agentbridge_task_plan_prepare"
        names = {t["function"]["name"] for t in request["tools"]}
        assert tool in names and "oa_leave_save_draft" not in names
        if len(previous) == 2:
            assert "计划状态" in str(previous[-1]["content"]), str(previous[-1]["content"])[:100]
            return "中央已记录两个系统的查询计划。",None
        if previous:
            assert "尚未开放" in str(previous[-1]["content"])
        steps = [{"stepKey":"one","kind":"capability","capabilityName":"oa.workflow.pending.list","arguments":{"limit":2}},
                 {"stepKey":"two","kind":"capability","capabilityName":"smartlight.alarm.list","dependsOn":["one"],"arguments":{}}]
        if not previous:
            steps[1]["capabilityName"] = "oa.leave.save_draft"
        args = {"goal":"汇总本人 OA 待办和智慧照明告警","steps":steps}
        return None,{"id":"pi-composite-"+str(self.calls),"type":"function","function":{"name":tool,"arguments":json.dumps(args)}}


def composite(post, central, accounts, model):
    args = {"binding":accounts["a"],"requestKey":"composite-pi"}
    message = "汇总本人 OA 待办和智慧照明告警。"
    run = post("accept",**args,message=message).json()["runId"]
    for _ in range(220):
        result = post("lookup",**args).json()
        if result["state"] != "accepted":
            break
        time.sleep(.12)
    assert result["state"] == "completed" and model.calls == 3 and not model.failures, (result["state"],model.calls,model.failures)
    assert post("accept",**args,message=message).json()["runId"] == run
    with sqlite3.connect(central.service.db_path) as db:
        plans = db.execute("SELECT plan_id,parent_task_id,state FROM task_plans WHERE user_subject=?",(OWNER,)).fetchall()
        assert len(plans) == 1 and plans[0][2] == "succeeded", plans
        plan, task, _ = plans[0]
        rows = db.execute("SELECT capability_name,system_id,state,attempt_count,depends_on_json,operation_id "
                          "FROM task_plan_steps WHERE plan_id=? ORDER BY ordinal",(plan,)).fetchall()
        source = db.execute("SELECT agent_host,origin_endpoint_id FROM agent_tasks WHERE task_id=?",(task,)).fetchone()
    assert source == ("pi", accounts["a"]["endpoint_id"])
    assert [(r[0],r[1],r[2],r[3]) for r in rows] == [
        ("oa.workflow.pending.list","oa","succeeded",1),("smartlight.alarm.list","smartlight","succeeded",1)], rows
    assert json.loads(rows[1][4]) == ["one"]
    assert len({r[5] for r in rows if r[5]}) == 2
    assert not central.matrix.matrix["effects"] and not central.matrix.oa.writes and not central.matrix.smartlight.data["writes"]
    return {"sameOriginalRun":True,"plans":len(plans),"systems":[r[1] for r in rows],
            "distinctOperations":2,"hiddenCommitRejected":True,"sourceEndpointMatched":True,"writes":0}


def exercise(node,sdk,engine,case):
    with tempfile.TemporaryDirectory(prefix="pi-batch-") as tmp:
        directory = Path(tmp).resolve()
        token = private(directory/"route.token",secrets.token_urlsafe(40))
        program = directory/"coordinator"
        program.write_text("#!"+sys.executable+" -I\nimport sys\nsys.path[:0]="+repr([str(ROOT/"integrations/reference-host"),str(ROOT)])+
                           "\nsys.argv[1:]=['--host-type','pi']\nfrom reference_host.bound_coordinator import main\nmain()\n")
        program.chmod(0o700)
        captured = []
        def setup(central):
            captured.append(central)
            assert central.service.home == directory/"central"
            with sqlite3.connect(central.service.db_path) as db:
                db.execute("INSERT OR REPLACE INTO agent_host_compatibility VALUES ('pi','0.1.9','L3','fixture-only','batch','now')")
            for affair,kind in zip(AFFAIRS,("attendance_confirmation","weekly_report")):
                central.matrix.oa.add_affair(affair,profile=kind)
        def account(service,*,user_subject,**kwargs):
            if user_subject == OWNER:
                with sqlite3.connect(service.db_path) as db:
                    found = db.execute("SELECT account_id FROM workspace_accounts WHERE user_subject=?", (OWNER,)).fetchone()
                return service.workspace.get_account(found[0])
            return _create_account(service,user_subject=user_subject,**kwargs)
        port = free_port()
        model = BatchModel() if case == "batch" else PlanModel()
        with patch("tests.support.pi_workspace_acceptance.NanoBotMcpFixture",MatrixCentral), patch(
              "tests.support.pi_workspace_acceptance._create_account",account), central_fixture(
              directory,host_url=f"http://127.0.0.1:{port}",token_file=token,maximum_level="L3", configure=setup) as (meta,accounts), host_fixture(
              directory,node,sdk,meta,accounts,port=port,token_file=token,model=model,engine_python=engine,
              options={"trustedCardOrigins":[ORIGIN],"coordinatorProgram":str(program),"requiredLevel":"L3"}) as (post,_,_,_):
            central = captured[0]
            if case == "composite":
                return composite(post,central,accounts,model)
            args = {"binding":accounts["a"],"requestKey":"pi-batch-contract"}
            accepted = post("accept",**args,message="分别预览和办理这两条合成待办。")
            assert accepted.status_code == 200
            run = accepted.json()["runId"]
            decided, revisions = set(), set()
            for _ in range(500):
                row = post("lookup",**args).json()
                if row["state"] != "accepted":
                    break
                if row.get("hasPrivateCard") and row["cardRevision"] not in revisions:
                    card = post("card",**args,runId=run).json()
                    if card.get("available"):
                        assert card["kind"] == "business_input" and card["url"].startswith(ORIGIN+"/")
                        with sqlite3.connect(central.service.db_path) as db:
                            fid = db.execute("SELECT submission_id FROM field_submissions WHERE user_subject=? "
                                             "ORDER BY rowid DESC LIMIT 1",(OWNER,)).fetchone()[0]
                        if fid not in decided:
                            assert len(central.matrix.oa.writes) == len(decided)
                            drafts, creds, state = preview_case(central.matrix,{"input_id":fid})
                            drafts.approve(**creds,expected_revision=state["revision"],preview_id=state["preview"]["previewId"],
                                           request_key="pi-batch-synthetic-"+str(len(decided)))
                            decided.add(fid)
                            revisions.add(row["cardRevision"])
                time.sleep(.16)
            # The host publishes the coordinator's verified outcome, replacing the model's wording.
            assert (row["runId"],row["state"],row["answer"]) == (run,"completed","中央已核验原任务的业务操作成功。")
            with sqlite3.connect(central.service.db_path) as db:
                batch = db.execute("SELECT parent_task_id,total_count,succeeded_count,state FROM task_batches").fetchone()
                item_operations = db.execute("""SELECT i.operation_id,o.status FROM task_batch_items i
                    JOIN task_batches b ON b.batch_id=i.batch_id JOIN operations o ON o.operation_id=i.operation_id
                    WHERE b.parent_task_id=? AND i.state='succeeded' AND o.user_subject=?""", (batch[0], OWNER)).fetchall()
                auths = db.execute("SELECT COUNT(DISTINCT authorization_id) FROM write_authorizations WHERE user_subject=?",(OWNER,)).fetchone()[0]
            assert batch[1:] == (2,2,"succeeded")
            assert len({op for op,status in item_operations if status == "succeeded"}) == 2
            snapshot = central.service.get_host_task_snapshot(user_subject=OWNER,agent_host="pi",
                       endpoint_key=accounts["a"]["endpoint_key"],task_id=batch[0])
            assert snapshot["batchVerification"]["verifiedCount"] == 2 and snapshot["batchVerification"]["allVerified"]
            assert len(decided) == len(revisions) == len(central.matrix.oa.writes) == auths == 2
            assert not model.failures and model.calls == 2
            with central.service.runtime_governance._connect() as db:
                signals = [json.loads(row[0]) for row in db.execute(
                    "SELECT value_json FROM runtime_signals WHERE signal_type='host.runtime.snapshot' "
                    "AND source='agent_host_contract' AND user_subject=? AND host_type='pi'",(OWNER,))]
            assert any(s.get("hostInstanceId") == "pi-office" and s.get("hostVersion") == "0.1.9"
                       and s.get("snapshotId") for s in signals)
            return {"sameParent":True,"distinctCards":len(decided),"distinctApprovals":auths,
                    "verifiedOperations":snapshot["batchVerification"]["verifiedCount"],"effects":len(central.matrix.oa.writes),
                    "automaticRuntimeSignal":True}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for flag in ("--node","--sdk-dir","--engine-python"):
        parser.add_argument(flag,required=True,type=Path)
    parser.add_argument("--case",choices=("batch","composite"),default="batch")
    arg=parser.parse_args()
    print(json.dumps(exercise(arg.node.resolve(),arg.sdk_dir.resolve(),arg.engine_python,arg.case)))
