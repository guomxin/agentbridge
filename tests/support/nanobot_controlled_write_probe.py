"""Real NanoBot SDK -> isolated coordinator/central MCP -> synthetic durable OA ledger."""

import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import uvicorn
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import (
    create_central_mcp_server,
    validate_central_mcp_server_config,
)
from agentbridge.mcp.resource_budget import BoundedMcpProtocol, bounded_mcp_app
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from tests.support.action_matrix_acceptance import ActionMatrixAcceptanceFixture
from tests.support.business_draft_acceptance import OWNER
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_mcp_fixture import private_file
from tests.support.nanobot_sdk_probe import MODEL, model_server
from tests.test_action_matrix_acceptance import decide

ORIGIN = "https://synthetic-credentials.invalid"


class Model:
    def __init__(self):
        self.failures = []
        self.results = []
        self.before_final = None
        self.current_call = None

    def reply(self, data):
        raw = json.dumps(data, ensure_ascii=False)
        if ORIGIN in raw or "Bearer " in raw or "/flow/" in raw:
            raise ValueError("private_material_in_model_input")
        names = {v["function"]["name"] for v in data["tools"]}
        name = "mcp_ab_oa_leave_prepare"
        if name not in names or any(
            "commit" in n or "resume" in n or "save_draft" in n for n in names
        ):
            raise ValueError("unexpected_model_tool_projection")
        if not self.current_call:
            self.current_call="prepare-"+secrets.token_hex(10)
            return None, {"id":self.current_call,"type":"function","function":{"name":name,"arguments":"{}"}}
        results=[m for m in data["messages"] if m.get("role")=="tool" and m.get("tool_call_id")==self.current_call]
        if not results:
            # Some compatible providers omit the call ID on historical tool output. Require only the two
            # fixed public certificates returned by our tool, with no previous-turn inference.
            raise ValueError("current_fixture_tool_result_missing")
        msg=results[-1]["content"]
        self.current_call=None
        self.results.append(msg)
        if self.before_final:
            callback, self.before_final = self.before_final, None
            callback()
        return msg, None


class PlanModel:
    def __init__(self):
        self.failures=[]
        self.calls=0

    def reply(self,data):
        self.calls+=1
        name="mcp_ab_agentbridge_task_plan_prepare"
        assert name in {t["function"]["name"] for t in data["tools"]}
        seen=[m.get("content","") for m in data["messages"] if m.get("role")=="tool"]
        if seen and "计划状态" in seen[-1]:
            return seen[-1],None
        cap="oa.leave.save_draft" if not seen else "oa.workflow.pending.list"
        if seen:
            assert "尚未开放" in seen[-1]
        return None,{"id":"p"+str(self.calls),"type":"function","function":{"name":name,"arguments":json.dumps(
            {"goal":"查看我的待办","steps":[{"kind":"capability","stepKey":"list","capabilityName":cap,"arguments":{"limit":2}}]})}}


def run(python, lost, browser=False, restart=False, twice=False,plan=False,*,exercise=None,capability="oa.leave.prepare",pending_restart=False,skill_scope=False):
    with tempfile.TemporaryDirectory(prefix="nanobot-controlled-action-") as tmp:
        root = Path(tmp).resolve()
        fixture = ActionMatrixAcceptanceFixture(root / "matrix", seed=False)
        mcp_port = free_port()
        host_port = free_port()
        base = f"http://127.0.0.1:{mcp_port}"
        fixture.service.trusted_card_base_url = ORIGIN
        instance = "nanobot-l3-fixture"
        account = fixture.account["account_id"]
        endpoint_key = f"workspace:{instance}:{account}"
        identity = McpIdentityTokenStore(fixture.service.db_path)
        token = identity.issue(user_subject=OWNER, expected_principal_ref=OWNER)
        token_file = root / "personal.token"
        private_file(token_file, token["token"].encode())
        service_token = root / "service.token"
        private_file(service_token, secrets.token_urlsafe(36).encode())
        servers = []
        token_map = {OWNER: str(token_file)}
        if browser:
            fixture.service.tasks.ensure_endpoint(user_subject=OWNER,token_id=token["token_id"],agent_host="openclaw",
                endpoint_key="test-original-telegram-501",client_type="telegram",external_subject="501",
                conversation_ref="agent:main:telegram:direct:501",route={"channel":"telegram","to":"501","accountId":"default"})
            from tests.support.nanobot_write_workspace import configure_workspace

            other = "interaction-other"
            other_token = identity.issue(
                user_subject=other, expected_principal_ref=other
            )
            other_path = root / "other.token"
            private_file(other_path, other_token["token"].encode())
            token_map[other] = str(other_path)
            bindings, workspace = configure_workspace(
                root, fixture, instance, host_port, token_map
            )
        ep, _ = fixture.service.tasks.ensure_endpoint(
            user_subject=OWNER,
            token_id=token["token_id"],
            agent_host="nanobot",
            endpoint_key=endpoint_key,
            client_type="web",
            account_id=account,
            external_subject=account,
            conversation_ref=endpoint_key,
        )
        binding = {
            "user_subject": OWNER,
            "endpoint_id": ep["endpoint_id"],
            "endpoint_key": endpoint_key,
            "session_key": endpoint_key,
            "credential_ref": "synthetic-owner",
        }
        if browser:
            binding = next(b for b in bindings if b["user_subject"] == OWNER)
        else:
            bindings = [binding]
        # A candidate-only compatibility entry in this throw-away database permits exercising the gate.
        with sqlite3.connect(fixture.service.db_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO agent_host_compatibility VALUES('nanobot','0.3.0','L3','isolated-candidate-conformance','isolated-fixture',CURRENT_TIMESTAMP)"
            )
            assert db.execute(
                "SELECT accepted_level FROM agent_host_compatibility WHERE implementation_name=? AND implementation_version=?",
                ("nanobot","0.3.0"),
            ).fetchone()==("L3",)
        cfg = validate_central_mcp_server_config(
            host="127.0.0.1",
            port=mcp_port,
            public_base_url=base,
            tls_cert=None,
            tls_key=None,
        )
        mcp = create_central_mcp_server(
            service=fixture.service,
            identity_store=identity,
            config=cfg,
            auth_card_base_url=ORIGIN,
        )
        uv = uvicorn.Server(
            uvicorn.Config(
                bounded_mcp_app(mcp, identity),
                host="127.0.0.1",
                port=mcp_port,
                http=BoundedMcpProtocol,
                ws="none",
                access_log=False,
                log_level="error",
            )
        )
        uvthread = threading.Thread(target=uv.run, daemon=True)
        uvthread.start()
        model = PlanModel() if plan else Model()
        httpd = model_server(model)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        model_config = root / "model.json"
        private_file(
            model_config,
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": MODEL,
                            "provider": "custom",
                            "maxToolIterations": 6 if skill_scope else 3,
                        }
                    },
                    "providers": {
                        "custom": {
                            "apiBase": f"http://127.0.0.1:{httpd.server_port}/v1",
                            "apiKey": "synthetic",
                        }
                    },
                }
            ).encode(),
        )
        coordinator = root / "coordinator"
        coordinator.write_text(
            "#!"
            + sys.executable
            + " -I\nimport sys\nsys.path.insert(0,"
            + repr(str(ROOT / "integrations/reference-host"))
            + ")\nfrom reference_host.bound_coordinator import main\nmain()\n"
        )
        coordinator.chmod(0o700)
        spec = root / "host.json"
        private_file(
            spec,
            json.dumps(
                {
                    "instanceId": instance,
                    "mcpUrl": base + "/mcp",
                    "stateDir": str(root / "state"),
                    "listenPort": host_port,
                    "serviceTokenFile": str(service_token),
                    "coordinatorProgram": str(coordinator),
                    "enableSkills":skill_scope,
                    "trustedCardOrigins": [ORIGIN],
                    **({"telegramPilot":{"botId":"8001","username":"fixture_bot"}} if browser else {}),
                    "bindings": [
                        {
                            **b,
                            "tokenFile": token_map[b["user_subject"]],
                            "modelConfigFile": str(model_config),
                        }
                        for b in bindings
                    ],
                }
            ).encode(),
        )
        command = "import sys;sys.path.insert(0,sys.argv.pop(1));from nanobot_agentbridge.server import main;main()"

        def start_host():
            return subprocess.Popen(
                [
                    str(python),
                    "-I",
                    "-c",
                    command,
                    str(ROOT / "integrations/nanobot-agentbridge"),
                    "--config",
                    str(spec),
                ],
                env={"HOME": str(root), "PATH": "/usr/bin:/bin", "TMPDIR": str(root)},
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        proc = start_host()
        gateway = NanoBotGatewayClient(
            url=f"http://127.0.0.1:{host_port}",
            token_file=service_token,
            instance_id=instance,
        )
        dispatch = {"idempotency_key": "controlled-fixture"}
        try:
            for i in range(160):
                if gateway.readiness()["available"]:
                    break
                time.sleep(0.1)
            assert gateway.readiness()["available"], "host_not_ready"
            if pending_restart:
                import signal
                key={"idempotency_key":"late-claim-after-restart"}
                for event in gateway.send_dispatch(key,binding,"请帮我保存请假草稿，稍后确认。",[],None):
                    if event["type"]=="accepted": rid=event["runId"]
                    if event.get("kind")!="private_interaction" or event.get("status")!="waiting_private":continue
                    card=gateway.private_card(key,binding,rid)
                    assert card["kind"]=="business_input"
                    with sqlite3.connect(fixture.service.db_path) as db:
                        source=db.execute("SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",(OWNER,)).fetchone()[0]
                    os.killpg(proc.pid,signal.SIGKILL)
                    proc.wait(8)
                    decide(fixture,{"capability":"oa.leave.prepare","input_id":source})
                    assert not fixture.matrix["effects"]
                    proc=start_host()
                    break
                status={}
                for _ in range(260):
                    try: status=gateway.dispatch_evidence(key,binding,rid)
                    except GatewayRequestError: pass
                    if status.get("state")=="completed":break
                    time.sleep(.12)
                assert status.get("state")=="completed" and len(fixture.matrix["effects"])==1,str(status.get("state"))
                with sqlite3.connect(fixture.service.db_path) as db:
                    checked=db.execute("SELECT COUNT(*) FROM operations WHERE user_subject=? AND capability_name='oa.leave.save_draft'",(OWNER,)).fetchone()[0]
                    assert checked==1
                assert len(list(gateway.send_dispatch(key,binding,"请帮我保存请假草稿，稍后确认。",[],None)))==2
                return {"offline_approval":True,"late_original_lease_resume":True,"commit_operations":1,"reconciled_original":True}
            if exercise is not None:
                return exercise(fixture=fixture,binding=binding,instance=instance,gateway=gateway,model=model)
            if plan:
                if restart:
                    from functools import partial
                    from tests.support.nanobot_plan_restart import handoff
                    fixture.service.task_plan_runtime.start=partial(handoff,fixture)
                response=list(gateway.send_dispatch(dispatch,binding,"请使用持久只读计划查看待办。",[],None))
                assert response[-1]["state"]=="final" and "计划状态" in response[-1]["text"],("plan_result",response[-1].get("text"),model.failures)
                with sqlite3.connect(fixture.service.db_path) as db:
                    counts=db.execute("SELECT state,COUNT(*) FROM task_plans GROUP BY state").fetchall()
                assert counts==[("succeeded",1)] and not fixture.matrix["effects"] and model.calls==3,counts
                again=list(gateway.send_dispatch(dispatch,binding,"请使用持久只读计划查看待办。",[],None))
                assert model.calls==3 and again[0]["runId"]==response[0]["runId"]
                return {"durable_plans":1,"read_plan_state":"succeeded","hidden_commit_blocked":True,"no_side_effect":True,"same_run":True,"central_plan_process_restart":restart}
            if twice:
                from tests.test_action_matrix_acceptance import preview_case
                import signal
                outcomes=[]
                for lose in (False,True):
                    fixture.matrix["fault"]="lost_reply" if lose else None
                    key={"idempotency_key":"second" if lose else "first"}
                    old=len(fixture.matrix["effects"])
                    decided=False
                    def reboot():
                        nonlocal proc
                        os.killpg(proc.pid,signal.SIGKILL)
                        proc.wait(8)
                        proc=start_host()
                    model.before_final=reboot
                    try:
                        for event in gateway.send_dispatch(key,binding,"请帮我保存请假草稿，保留待发。",[],None):
                            if event["type"]=="accepted": rid=event["runId"]
                            if event.get("kind")!="private_interaction" or event.get("status")!="waiting_private" or decided:continue
                            decided=True
                            with sqlite3.connect(fixture.service.db_path) as db:
                                row=db.execute("SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",(OWNER,)).fetchone()
                            drafts,credentials,state=preview_case(fixture,{"capability":"oa.leave.prepare","input_id":row[0]})
                            assert len(fixture.matrix["effects"])==old
                            drafts.approve(**credentials,expected_revision=state["revision"],preview_id=state["preview"]["previewId"],
                                           request_key="fixture-decision-"+key["idempotency_key"])
                    except GatewayRequestError:
                        pass
                    target="unknown" if lose else "completed"
                    for _ in range(200):
                        try: status=gateway.dispatch_evidence(key,binding,rid).get("state")
                        except GatewayRequestError:status=None
                        if status==target:break
                        time.sleep(.1)
                    assert model.before_final is None and status==target and len(fixture.matrix["effects"])==old+1,("two_turn_failure",target,status,model.failures)
                    outcomes.append(status)
                return {"same_subject_conversation":True,"two_process_group_stops":True,"reconciled":outcomes,"downstream_effects":2}
            if browser:
                from tests.support.nanobot_write_workspace import start_workspace

                controls=None
                if restart:
                    from tests.support.nanobot_restart_controls import RestartControls
                    def restart_now():
                        nonlocal proc
                        proc=start_host()
                    controls=RestartControls(fixture,model,lambda:proc,restart_now,gateway)
                servers, links = start_workspace(fixture, workspace,controls=controls)
                print(
                    json.dumps(
                        {
                            "signin_and_native_form": links,
                            "isolated_home": str(root),
                            "direct_card_browser_tls": False,
                        }
                    ),
                    flush=True,
                )
                threading.Event().wait()
            if lost:
                fixture.matrix["fault"] = "lost_reply"
            if restart:

                def restart_host():
                    nonlocal proc
                    proc.kill()
                    proc.wait(8)
                    proc = start_host()

                model.before_final = restart_host
            authorization=None
            try:
                for event in gateway.send_dispatch(
                    dispatch, binding, "请帮我保存请假草稿，保留待发。", [], None
                ):
                    if event["type"] == "accepted":
                        run_id = event["runId"]
                    if event.get("kind") != "private_interaction" or event.get("status")!="waiting_private" or authorization:
                        continue
                    view = gateway.private_card(dispatch, binding, run_id)
                    assert view["kind"] == "business_input" and view["url"].startswith(
                        ORIGIN + "/"
                    )
                    with sqlite3.connect(fixture.service.db_path) as db:
                        row = db.execute(
                            "SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1",
                            (OWNER,),
                        ).fetchone()
                    assert row
                    authorization = decide(
                        fixture, {"capability": "oa.leave.prepare", "input_id": row[0]}
                    )
                    assert authorization
            except GatewayRequestError:
                if not restart:
                    raise
            if restart:
                observed = {}
                for attempt in range(220):
                    try:
                        observed = gateway.dispatch_evidence(dispatch, binding, run_id)
                    except GatewayRequestError:
                        pass
                    if observed.get("state") == ("unknown" if lost else "completed"):
                        break
                    time.sleep(0.12)
                assert observed.get("state") == ("unknown" if lost else "completed"), (
                    "restart_reconciliation_failed"
                )
                if not lost:
                    assert "重新核验" in observed["final_text"]
            assert model.results, "model_no_result:" + ",".join(model.failures)
            assert len(fixture.matrix["effects"]) == 1, (
                "effect_count:"
                + str(len(fixture.matrix["effects"]))
                + " result:"
                + str(model.results)
            )
            assert (
                ("有待核对" in model.results[-1])
                if lost
                else ("已核验" in model.results[-1])
            ), "unexpected_authority:" + str(model.results)
            try:
                duplicate = list(
                    gateway.send_dispatch(
                        dispatch, binding, "请帮我保存请假草稿，保留待发。", [], None
                    )
                )
            except GatewayRequestError:
                if not (restart and lost):
                    raise
                duplicate = [
                    {
                        "runId": gateway.dispatch_evidence(dispatch, binding, run_id)[
                            "observed_run_id"
                        ]
                    }
                ]
            assert (
                duplicate[0]["runId"] == run_id and len(fixture.matrix["effects"]) == 1
            )
            return {
                "outcome": "unknown" if lost else "verified",
                "same_run_dedup": True,
                "downstream_effects": 1,
                "central_task_lease": True,
                "durable_synthetic_readback": not lost,
                "restarted": restart,
                "real_business": False,
            }
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            if browser:
                workspace.close()
            proc.terminate()
            proc.wait(12)
            uv.should_exit = True
            uvthread.join(8)
            httpd.shutdown()
            httpd.server_close()
            fixture.close()


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("--nanobot-python", required=True, type=Path)
    parser.add_argument("--lost-reply", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--twice",action="store_true")
    parser.add_argument("--plan",action="store_true")
    parser.add_argument("--pending-restart",action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.nanobot_python, args.lost_reply, args.browser, args.restart,args.twice,args.plan,pending_restart=args.pending_restart)
        )
    )
