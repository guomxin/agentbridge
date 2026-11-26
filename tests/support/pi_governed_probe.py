"""Exercise the actual Pi SDK, its private coordinator and an isolated synthetic OA action."""
import argparse
from contextlib import nullcontext
from functools import partial
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.task_plans import task_plan_response
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.mcp.resource_budget import bounded_mcp_app
from tests.support.action_matrix_acceptance import ActionMatrixAcceptanceFixture
from tests.support.business_draft_acceptance import OWNER
from tests.support.interaction_acceptance import free_port
from tests.support.pi_sdk_probe import ROOT, host_fixture, private
from tests.support.pi_workspace_acceptance import central_fixture
from tests.test_action_matrix_acceptance import preview_case
from tests.test_workspace import _create_account

ORIGIN = "https://pi-action-fixture.invalid"


class MatrixCentral:
    def __init__(self, directory, base, port):
        self.matrix = ActionMatrixAcceptanceFixture(directory, seed=False)
        self.service = self.matrix.service
        self.service.trusted_card_base_url = ORIGIN
        self.identities = McpIdentityTokenStore(self.service.db_path)
        self.subjects = {"a": OWNER, "b": "pi-secondary-test-only"}
        self.meta = {"endpoint": base + "/mcp", "subjects": self.subjects}
        config = validate_central_mcp_server_config(host="127.0.0.1", port=port, public_base_url=base, tls_cert=None, tls_key=None)
        self.app = bounded_mcp_app(create_central_mcp_server(service=self.service, identity_store=self.identities,
                               config=config, auth_card_base_url=ORIGIN), self.identities)

    def close(self):
        self.matrix.close()


class Model:
    def __init__(self, plan):
        self.plan = plan
        self.failures = []
        self.calls = 0
        self.outcome = ""

    def reply(self, data):
        text = json.dumps(data, ensure_ascii=False)
        assert ORIGIN not in text and "/flow/" not in text and "Bearer " not in text
        names = {entry["function"]["name"] for entry in data["tools"]}
        assert not any(n.endswith(("_commit", "_save_draft", "_resume")) for n in names)
        self.calls += 1
        tools = [m for m in data["messages"] if m["role"] == "tool"]
        if self.plan:
            tool = "agentbridge_task_plan_prepare"
            if tools and "计划状态" in str(tools[-1]["content"]):
                self.outcome = "plan"
                return "中央已返回持久计划状态。", None
            if tools:
                assert "尚未开放" in str(tools[-1]["content"])
            cap = "oa.workflow.pending.list" if tools else "oa.leave.save_draft"
            params = {"goal": "查看本人的待办", "steps": [{"stepKey": "list", "kind": "capability",
                    "capabilityName": cap, "arguments": {"limit": 2}}]}
        else:
            tool, params = "oa_leave_prepare", {}
            if tools:
                public = str(tools[-1]["content"])
                self.outcome = "verified" if "已核验" in public else "unknown" if "待核对" in public else "unexpected"
                return "原任务已核验。" if self.outcome == "verified" else "原任务结果待核对。", None
        assert tool in names, "missing_projected_tool"
        return None, {"id": "pi-call-" + str(self.calls), "type": "function", "function": {
                "name": tool, "arguments": json.dumps(params, ensure_ascii=False)}}


class BrowserModel(Model):
    def __init__(self, central):
        super().__init__(False)
        self.version = central.service.skills.current("oa-work-log")["manifest"]["version"]

    def reply(self, data):
        if not data.get("tools"):
            system = next((m["content"] for m in data["messages"] if m["role"] == "system"), "")
            if "业务方法编辑器" in system and "kind" in system and "proposal" in system:
                proposal = {"name":"隔离验收周报助手", "description":"从提供的文字归纳完成事项、待办和风险。",
                            "selection":{"use_when":"用户要求按材料撰写周报", "not_for":"审批或查询业务系统", "output":"完成事项、待办与风险"},
                            "instructions":"只按照用户提供的文字整理完成事项、待办和风险。对缺少依据的项目写明尚未提供。"}
                return json.dumps({"kind":"method", "summary":"从文字整理周报的可复用方法。", "proposal":proposal}, ensure_ascii=False), None
            return "独立模型已完成固定修订样例。", None
        last_user = next(m for m in reversed(data["messages"]) if m["role"] == "user")
        if not any(word in str(last_user["content"]) for word in ("助手", "技能", "工作日志")):
            return super().reply(data)
        names = {t["function"]["name"] for t in data["tools"]}
        history = [m for m in data["messages"] if m["role"] == "tool"]
        catalog, load = "agentbridge_skill_catalog", "agentbridge_skill_get"
        if not history:
            name, params = catalog, {}
        elif len(history) == 1:
            if "oa-work-log" not in str(history[-1]["content"]):
                return "本人没有获配工作日志助手。", None
            name, params = load, {"skill_id":"oa-work-log", "profile":"preview", "expected_version":self.version}
        else:
            assert "oa-work-log" in str(history[-1]["content"]) and "oa_leave_prepare" not in names
            return "已加载本人工作日志助手的预览模式。", None
        assert name in names
        return None, {"id":"skill-" + str(len(history)), "type":"function", "function":{
                      "name":name,"arguments":json.dumps(params)}}


def restarted_plan(central, plan_id, *, user_subject):
    args = [sys.executable, "-m", "tests.support.pi_governed_probe", "--planner", str(central.matrix.home),
            "--plan-id", plan_id]
    assert user_subject == OWNER
    assert subprocess.run([*args, "--interrupt"], cwd=ROOT, capture_output=True, timeout=30).returncode == 86
    completed = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=30, check=True)
    evidence = json.loads(completed.stdout)
    assert evidence == {"completed": 1, "restarted": 1}
    stored = central.service.task_plans.get(plan_id, user_subject=OWNER)
    assert stored["state"] == "succeeded" and stored["steps"][0]["attempt_count"] == 2
    return {"status": "succeeded", "plan": task_plan_response(stored)}


def planner_child(directory, plan_id, interrupt):
    assert directory.name == "central" and directory.parent.name.startswith("pi-governed-")
    with sqlite3.connect(directory / "agentbridge.db") as db:
        assert db.execute("SELECT COUNT(*) FROM agent_host_compatibility WHERE implementation_name='pi' AND approved_by='isolated-fixture'").fetchone()[0] == 1
    fixture = ActionMatrixAcceptanceFixture(directory, seed=False)
    try:
        s = fixture.service
        ids = McpIdentityTokenStore(s.db_path)
        ids.user_grants = s.user_grants
        s.set_task_plan_authority_resolver(lambda token, scopes: ids.resolve_client(token, required_scopes=set(scopes)))
        plan = s.task_plans.get(plan_id, user_subject=OWNER)
        assert s.tasks.get_task(plan["parent_task_id"], user_subject=OWNER)["agent_host"] == "pi"
        if interrupt:
            current = s.task_plans.begin_next_step(plan_id, user_subject=OWNER)
            assert (current["capability_name"], current["state"]) == ("oa.workflow.pending.list", "running")
            os._exit(86)
        result = s.recover_task_plans()
        assert not fixture.matrix["effects"]
        print(json.dumps({k:result[k] for k in ("completed","restarted")}))
    finally:
        fixture.close()


def exercise(node, sdk, engine, case, *, serve=False, scenario=None, desktop=None):
    maximum_level = getattr(scenario, "level", "L3")
    with tempfile.TemporaryDirectory(prefix="pi-governed-") as tmp:
        directory = Path(tmp).resolve()
        token = private(directory / "route.token", secrets.token_urlsafe(40))
        program = directory / "coordinator"
        program.write_text("#!" + sys.executable + " -I\nimport sys\nsys.path[:0]=" +
                           repr([str(ROOT / "integrations/reference-host"), str(ROOT)]) +
                           "\nsys.argv[1:]=['--host-type','pi']\nfrom reference_host.bound_coordinator import main\nmain()\n")
        program.chmod(0o700)
        port = free_port()
        captured = []
        def setup(central):
            captured.append(central)
            assert central.service.home == directory / "central"
            with sqlite3.connect(central.service.db_path) as db:
                db.execute("""INSERT OR REPLACE INTO agent_host_compatibility VALUES
                    ('pi','0.1.5','L3','isolated-pi-proof','isolated-fixture',CURRENT_TIMESTAMP)""")
            if serve or scenario:
                settings = central.service.skills.config("user:" + OWNER)
                central.service.skills.save("user:" + OWNER, {"oa-work-log":{"profiles":["preview"]}},
                    expected_revision=settings["revision"], actor="fixture", reason="private Pi browser comparison")
            elif case == "verified":
                session = central.service.sessions.find(user_subject=OWNER, system_id="oa")
                central.service.sessions.mark_expired(session["session_id"])
            elif case == "unknown":
                central.matrix.matrix["fault"] = "lost_reply"
            elif case == "plan":
                central.service.task_plan_runtime.start = partial(restarted_plan, central)
        def account(service, *, user_subject, **kwargs):
            if user_subject == OWNER:
                with sqlite3.connect(service.db_path) as db:
                    account_id = db.execute("SELECT account_id FROM workspace_accounts WHERE user_subject=?", (OWNER,)).fetchone()[0]
                return service.workspace.get_account(account_id)
            return _create_account(service, user_subject=user_subject, **kwargs)
        with patch("tests.support.pi_workspace_acceptance.NanoBotMcpFixture", MatrixCentral), patch(
                "tests.support.pi_workspace_acceptance._create_account", account), central_fixture(
                directory, host_url=f"http://127.0.0.1:{port}", token_file=token, maximum_level=maximum_level, configure=setup,
                desk_bot=desktop.central_bot if desktop else None) as (meta, accounts):
            central = captured[0]
            model = scenario.model(central) if scenario else BrowserModel(central) if serve else Model(case == "plan")
            pilot = {"telegramPilot":{"botId":"8801", "username":"fixture_pi_bot", "allowedSenders":{OWNER:"501"}}} if (scenario or case == "verified") and not serve else {}
            if desktop:
                pilot = desktop.pilot(accounts)
            with host_fixture(directory, node, sdk, meta, accounts, port=port, token_file=token, model=model,
                    engine_python=engine, options={"trustedCardOrigins":[ORIGIN], "coordinatorProgram":str(program),
                                                 "requiredLevel":maximum_level, **pilot}) as (post, _, _, _), (
                    desktop.consumer(directory, port, token, accounts) if desktop else nullcontext()) as check_channel:
                if serve:
                    stopped = threading.Event()
                    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                    signal.signal(signal.SIGINT, lambda *_: stopped.set())
                    trigger = directory / "approve-synthetic-draft"
                    print(json.dumps({"signin":meta["signin"], "brokerTrigger":str(trigger)}), flush=True)
                    while not stopped.wait(.25):
                        if check_channel:
                            check_channel()
                        if not trigger.is_file():
                            continue
                        trigger.unlink()
                        with sqlite3.connect(central.service.db_path) as db:
                            field = db.execute("SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1", (OWNER,)).fetchone()
                        if field:
                            drafts, creds, state = preview_case(central.matrix, {"input_id":field[0]})
                            drafts.approve(**creds, expected_revision=state["revision"], preview_id=state["preview"]["previewId"],
                                           request_key="isolated-browser-decision")
                    return
                if scenario:
                    return scenario.check(central, post, model, accounts)
                key = {"binding": accounts["a"], "requestKey": "governed-" + case}
                if case == "verified":
                    from tests.support.pi_notification_failure import pair_before_run
                    pair_before_run(central, post, accounts["a"])
                original = "计划查询本人待办" if case == "plan" else "办理本人请假草稿"
                run = post("accept", **key, message=original).json()["runId"]
                seen, revisions, duplicate_denied = [], [], False
                deadline = time.monotonic() + 85
                while time.monotonic() < deadline:
                    row = post("lookup", **key).json()
                    if row["state"] != "accepted" and not (case in {"restart","stop-generation"} and row["state"] == "unknown"):
                        break
                    if not row.get("hasPrivateCard") or row["cardRevision"] in revisions:
                        time.sleep(.12)
                        continue
                    card = post("card", **key, runId=run).json()
                    if not card.get("available"):
                        continue
                    assert card["url"].startswith(ORIGIN + "/")
                    assert post("card", binding=accounts["b"], requestKey=key["requestKey"], runId=run).status_code == 403
                    seen.append(card["kind"])
                    revisions.append(row["cardRevision"])
                    service = central.service
                    with sqlite3.connect(service.db_path) as db:
                        if card["kind"] == "credential":
                            cid,sid = db.execute("SELECT challenge_id, session_id FROM auth_challenges WHERE state='pending' AND user_subject=?", (OWNER,)).fetchone()
                            peer = "pi-owned-peer"
                            service.tasks.ensure_endpoint(user_subject=OWNER, token_id="synthetic", agent_host="pi",
                                endpoint_key=peer,client_type="web",external_subject=OWNER,conversation_ref=peer)
                            private_id = card["interaction"]["interactionId"]
                            alt = service.present_interaction(user_subject=OWNER, agent_host="pi", endpoint_key=peer, interaction_id=private_id)
                            assert alt["interaction"]["interactionId"] == private_id and alt["endpoint"]["endpointId"] != accounts["a"]["endpoint_id"]
                            csrf = service.challenges.issue_csrf(cid)
                            service.challenges.claim(cid, csrf_token=csrf, csrf_cookie=csrf)
                            service.sessions.activate(sid, observed_principal_ref=OWNER)
                            service.challenges.complete(cid, result={"observed_principal_ref":OWNER})
                            try:
                                service.challenges.claim(cid, csrf_token=csrf, csrf_cookie=csrf)
                            except RuntimeError:
                                duplicate_denied = True
                            assert duplicate_denied and not central.matrix.matrix["effects"]
                        elif card["kind"] == "business_input":
                            fid = db.execute("SELECT submission_id FROM field_submissions WHERE user_subject=? ORDER BY rowid DESC LIMIT 1", (OWNER,)).fetchone()[0]
                            drafts, credentials, state = preview_case(central.matrix, {"input_id":fid})
                            assert not central.matrix.matrix["effects"]
                            if case == "restart":
                                post.crash_host()
                            elif case == "stop-generation":
                                stopped = post("cancel", **key, runId=run).json()
                                assert stopped["runId"] == run and stopped["state"] == "unknown"
                                assert not central.matrix.matrix["effects"]
                            drafts.approve(**credentials, expected_revision=state["revision"], preview_id=state["preview"]["previewId"], request_key="verified-separate-auth")
                            if case == "restart":
                                post.start_host()
                        else:
                            raise AssertionError("Unexpected private card type")
                assert row["state"] == ("unknown" if case == "unknown" else "completed") and not model.failures, (row["state"],case,seen,model.failures)
                if case in {"restart","stop-generation"}:
                    assert model.calls == 1 and "重新核验" in row["answer"]
                else:
                    assert model.outcome == case, (case, model.outcome)
                assert post("accept", **key, message=original).json()["runId"] == run
                assert post("accept", **key, message="更改材料").status_code == 409
                count = len(central.matrix.matrix["effects"])
                if case == "plan":
                    assert count == 0 and model.calls == 3 and not seen
                elif case == "verified":
                    assert count == 1 and duplicate_denied and seen == ["credential","business_input"] and revisions == [1,2]
                else:
                    assert count == 1 and seen == ["business_input"]
                proof = {"case":case,"sdk":"1.6.0","sameRun":True,"effects":count,"cards":seen,
                         "cardRevisions":revisions,"duplicateChallengeBlocked":duplicate_denied,"modelCalls": model.calls,
                         "gatewayState":row["state"]}
                if case == "restart":
                    proof.update(offlineDecision=True, restoredOriginal=True, modelReplay=False)
                if case == "stop-generation":
                    proof.update(generationStopped=True, originalBusinessVerified=True, modelReplay=False)
                if case == "verified":
                    with sqlite3.connect(central.service.db_path) as db:
                        evidence = db.execute("""SELECT r.value_json,s.snapshot_id FROM runtime_signals r
                            JOIN agent_host_runtime_snapshots s ON json_extract(r.value_json,'$.snapshotId')=s.snapshot_id
                            WHERE r.signal_type='host.runtime.snapshot' AND r.source='agent_host_contract'
                            AND r.user_subject=? AND r.host_type='pi' AND s.user_subject=r.user_subject
                            AND s.host_type='pi' AND s.host_instance_id='pi-office' AND s.host_version='0.1.5'""", (OWNER,)).fetchall()
                    assert evidence and all((json.loads(value)["hostInstanceId"], json.loads(value)["hostVersion"]) ==
                                            ("pi-office","0.1.5") for value, _ in evidence)
                    proof.update(automaticSnapshotSignals=len(evidence))
                    from tests.support.pi_notification_failure import check_failed_delivery
                    proof.update(check_failed_delivery(central, accounts["a"]))
                if case == "plan":
                    proof.update(centralPlannerRestarted=True, restoredStepAttempts=2, restoredPlanCount=1, hiddenCommitRefused=True)
                return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--sdk-dir", type=Path)
    parser.add_argument("--engine-python", type=Path)
    parser.add_argument("--case", choices=("verified","unknown","plan","restart","stop-generation"))
    parser.add_argument("--planner", type=Path)
    parser.add_argument("--plan-id")
    parser.add_argument("--interrupt", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    if args.planner:
        planner_child(args.planner, args.plan_id, args.interrupt)
    else:
        for case in [args.case or "verified"] if args.case or args.serve else ["verified","unknown","plan","restart"]:
            result = exercise(args.node.resolve(), args.sdk_dir.resolve(), args.engine_python, case, serve=args.serve)
            if not args.serve:
                print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
