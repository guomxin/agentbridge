"""Drive two independently authorized items through the real NanoBot run and central coordinator."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tests.support.nanobot_controlled_write_probe import run
from tests.test_action_matrix_acceptance import preview_case


def exercise(*,fixture,binding,instance,gateway,model,browser=False,skill=False):
    affairs=["synthetic-h23-attendance","synthetic-h23-report"]
    for aid,profile in zip(affairs,["attendance_confirmation","weekly_report"]):
        fixture.oa.add_affair(aid,profile=profile)
    identifier="h23-source"
    name="mcp_ab_oa_workflow_pending_batch_prepare"

    if skill:
        subject=binding["user_subject"]
        rev=fixture.service.skills.config("user:"+subject)["revision"]
        fixture.service.skills.save("user:"+subject,{"oa-pending":{"profiles":["batch"]}},expected_revision=rev,
            actor="fixture",reason="test original multi-item skill")
    def reply(data):
        if skill and not any(m.get("role")=="tool" and m.get("tool_call_id")=="load-batch-skill" for m in data["messages"]):
            return None,{"id":"load-batch-skill","type":"function","function":{"name":"mcp_ab_agentbridge_skill_get",
                "arguments":json.dumps({"skill_id":"oa-pending","expected_version":"1.5.1","profile":"batch"})}}
        assert name in {t["function"]["name"] for t in data["tools"]}
        for row in data["messages"]:
            if row.get("role")=="tool" and row.get("tool_call_id")==identifier:
                return row["content"],None
        return None,{"id":identifier,"type":"function","function":{"name":name,"arguments":json.dumps({"affair_ids":affairs})}}
    model.reply=reply
    if browser:
        from agentbridge.workspace.application import WorkspaceApplication
        from tests.support.nanobot_write_workspace import start_workspace
        app=WorkspaceApplication(service=fixture.service,host_routes={instance:gateway})
        servers,links=start_workspace(fixture,app,form_capability="oa.workflow.pending.batch.prepare")
        try:
            print(json.dumps({"signin":links["a"]["signin"],"native_form_mirror":links["a"]["form"]}),flush=True)
            while True:
                time.sleep(30)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            app.close()
    dispatch={"idempotency_key":"h23-repeatable"}
    stream=gateway.send_dispatch(dispatch,binding,"请逐项预览这两条合成待办。",[],None)
    rid=next(stream)["runId"]
    decided=set()
    for _ in range(150):
        if len(decided)==2:
            break
        with sqlite3.connect(fixture.service.db_path) as db:
            row=db.execute("SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",(binding["user_subject"],)).fetchone()
        if row and row[0] not in decided:
            card=gateway.private_card(dispatch,binding,rid)
            if card.get("available"):
                drafts,creds,state=preview_case(fixture,{"input_id":row[0]})
                expected=len(decided)
                assert len(fixture.oa.writes)==expected
                drafts.approve(**creds,expected_revision=state["revision"],preview_id=state["preview"]["previewId"],request_key="nanobot-h23-owner-decision-"+str(expected))
                decided.add(row[0])
        time.sleep(.15)
    assert len(decided)==2,("not_all_original_cards_presented",len(decided))
    events=list(stream)
    with sqlite3.connect(fixture.service.db_path) as db:
        source=db.execute("SELECT parent_task_id,total_count,succeeded_count,state FROM task_batches").fetchone()
        auths=db.execute("SELECT COUNT(DISTINCT authorization_id) FROM write_authorizations WHERE user_subject=?",(binding["user_subject"],)).fetchone()[0]
    assert source and source[1:]==(2,2,"succeeded"),("batch",source and source[1:],len(decided),events[-1].get("text","")[:100])
    snapshot=fixture.service.get_host_task_snapshot(user_subject=binding["user_subject"],agent_host="nanobot",endpoint_key=binding["endpoint_key"],task_id=source[0])
    proof=snapshot["batchVerification"]
    assert proof["verifiedCount"]==2 and proof["allVerified"] and len(fixture.oa.writes)==auths==len(decided)==2
    assert "已核验" in events[-1]["text"],"host_not_authoritative"
    return {"same_parent_task":True,"distinct_card_decisions":2,"distinct_authorizations":2,"verified_operations":2,"synthetic_writes":2}


if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--nanobot-python",required=True,type=Path)
    from functools import partial
    p.add_argument("--browser",action="store_true")
    p.add_argument("--skill",action="store_true")
    args=p.parse_args()
    print(json.dumps(run(args.nanobot_python,False,browser=args.browser,skill_scope=args.skill,exercise=partial(exercise,browser=args.browser,skill=args.skill))))
