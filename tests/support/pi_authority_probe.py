"""Exercise task coordination through two independently authenticated Pi MCP registrations."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

import httpx

from agentbridge.core.user_grants import PERMISSIONS
from agentbridge.workspace.host_provision import WorkspaceHostProvision
from agentbridge.admin.stores import AdminAuditStore
from tests.authorization_fixtures import grant_permissions
from tests.support.nanobot_credential_probe import ORIGIN, credential_service
from tests.support.pi_sdk_probe import private
from tests.support.pi_workspace_acceptance import central_fixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"integrations/reference-host"))
from reference_host.host_profile import CoordinatorImplementation, host_context_meta, registration_meta, task_call_meta
from reference_host.mcp_client import AgentBridgeMcpClient

PI = CoordinatorImplementation("pi", "0.1.7")


async def exercise(meta, binding, principal, fixture):
    owner_id, other_id = "pi-office", "pi-other"
    subject = binding["user_subject"]
    service = fixture.service
    def client(secret):
        return AgentBridgeMcpClient(mcp_url=meta["endpoint"],token_provider=lambda:secret,private_transport=True)
    owner = client(Path(meta["tokenFiles"]["a"]).read_text())
    other = client(principal)
    for instance, caller in ((owner_id,owner),(other_id,other)):
        reg = await caller.call_tool("agentbridge_host_register",{},meta=registration_meta(instance_id=instance,implementation=PI))
        assert not reg.is_error and reg.payload()["acceptedLevel"] == "L3"
    owner_meta = host_context_meta(instance_id=owner_id,implementation=PI)
    other_meta = host_context_meta(instance_id=other_id,implementation=PI)
    scope = {"agent_host":"pi","endpoint_key":binding["endpoint_key"],"client_type":"web",
             "external_subject":fixture.account_id,"account_id":fixture.account_id,"conversation_ref":binding["session_key"]}
    reply = await owner.call_tool("agentbridge_host_task_ensure", {**scope,"host_task_key":"pi-authority","title":"Pi synthetic contract"},
                                  meta=owner_meta)
    assert not reply.is_error
    original = reply.payload()
    task, lease = original["task"]["taskId"], original["coordinatorLease"]
    def task_context(instance):
        return task_call_meta(task_id=task,lease_version=lease["version"],endpoint_id=binding["endpoint_id"],
                              conversation_ref=binding["session_key"],instance_id=instance,implementation=PI)
    foreign = await other.call_tool("agentbridge_host_coordinator_lease_acquire",
                         {"task_id":task,"takeover":False,"expected_version":lease["version"]},meta=other_meta)
    assert foreign.is_error
    owned = await owner.call_tool("agentbridge_host_coordinator_lease_get",{"task_id":task},meta=owner_meta)
    assert not owned.is_error and owned.payload()["coordinatorLease"]["hostInstanceId"] == owner_id
    args = {k:v for k,v in scope.items() if k != "account_id"}
    texts = ("第一条独立时间线","第二条独立时间线")
    for index in (0,0,1):
        item = {**args,"task_id":task,"message_key":f"pi-contract-{index}","role":"assistant","text":texts[index]}
        result = await owner.call_tool("agentbridge_host_timeline_append",item,meta=task_context(owner_id))
        assert not result.is_error
    with service.workspace._connect() as db:
        rows = db.execute("SELECT text FROM user_timeline WHERE task_id=? AND text IN (?,?) ORDER BY rowid",
                          (task,*texts)).fetchall()
    assert [r[0] for r in rows] == list(texts)
    snapshot = await owner.call_tool("agentbridge_host_task_snapshot",{"agent_host":"pi","endpoint_key":binding["endpoint_key"],
                                  "task_id":task},meta=owner_meta)
    order = [e["sequence"] for e in snapshot.payload()["events"]]
    assert order and order == sorted(set(order))
    session = service.sessions.find(user_subject=subject, system_id="oa")
    service.sessions.mark_expired(session["session_id"])
    prepared = await owner.call_tool("oa_session_login",{},meta={**task_context(owner_id),"io.agentbridge/continue-after-login":True})
    card = prepared.private_meta.get("io.agentbridge/interaction", {})
    assert not prepared.is_error and card.get("interactionId")
    assert service.tasks.task_id_for_interaction(card["interactionId"],user_subject=subject) == task
    resume_calls = []
    original_resume = service.resume_interaction
    def count(**kwargs):
        resume_calls.append(True)
        return original_resume(**kwargs)
    service.resume_interaction = count
    denied = await other.call_tool("agentbridge_interaction_resume",{"interaction_id":card["interactionId"],
                                  "idempotency_key":"wrong-pi-owner"},meta=task_context(other_id))
    assert denied.is_error and not resume_calls
    async def transmit_failure_then_read():
        original_call = owner._call_tool_once
        count = []
        async def intermittent(*args, **kwargs):
            count.append(True)
            if len(count) == 1:
                raise httpx.ConnectError("isolated loopback disconnect")
            return await original_call(*args, **kwargs)
        owner._call_tool_once = intermittent
        try:
            retried = await owner.call_tool("agentbridge_server_profile",{},meta=owner_meta,recovery_class="read")
            assert not retried.is_error and len(count) == 2
            count.clear()
            try:
                await owner.call_tool("agentbridge_interaction_resume",{"interaction_id":card["interactionId"]},
                                      meta=task_context(owner_id),recovery_class="unsafe")
            except httpx.ConnectError:
                pass
            assert len(count) == 1 and not resume_calls
        finally:
            owner._call_tool_once = original_call
    await transmit_failure_then_read()
    # Expire the short entry while retaining the already exported bytes.
    from tests.support.file_retention_acceptance import seed_retained_csv
    from tests.support.nanobot_b4_files import login
    retained = seed_retained_csv(service,user_subject=subject,task_id=task)
    path = f"/api/tasks/{task}/artifacts/{retained['artifact_id']}/reissue"
    with login(meta["signin"]["a"],"piusera") as first, login(meta["signin"]["b"],"piuserb") as second:
        denied = second.post(path,json={})
        assert denied.status_code in {403,404,409}
        renewed = first.post(path,json={})
        assert renewed.status_code == 200, renewed.status_code
        entry = next(file for file in renewed.json()["artifacts"] if file["artifact_id"] == retained["artifact_id"])
        raw = first.get(entry["download_url"])
        assert entry["state"] == "ready" and raw.status_code == 200 and "合成历史报告" in raw.content.decode("utf-8-sig")
        assert second.get(entry["download_url"]).status_code in {403,404,409}
    report = await owner.call_tool("agentbridge_host_runtime_snapshot",{"snapshot":{"status":"healthy",
                    "observedAt":datetime.now(timezone.utc).isoformat(),"activeTaskCount":1}},meta=owner_meta)
    assert not report.is_error and report.payload()["status"] == "succeeded"
    assert any(r["hostInstanceId"] == owner_id and r.get("agentHost") == "pi"
               for r in service.host_contract.runtime_overview()["registrations"])
    with service.runtime_governance._connect() as db:
        signals = db.execute("SELECT value_json FROM runtime_signals WHERE signal_type='host.runtime.snapshot' "
                             "AND host_type='pi' AND user_subject=? AND status='healthy'", (subject,)).fetchall()
    assert any(json.loads(row[0]).get("snapshotId") == report.payload()["snapshotId"] for row in signals)
    assert not fixture.calls and not resume_calls
    return {"timelineTexts":len(rows),"foreignLeaseRejected":True,"foreignResumes":len(resume_calls),
            "reissuedCsv":1,"crossOwnerFileReads":0,"readAttempts":2,"unsafeAttempts":1,"runtimeSnapshots":1,"syntheticBusinessEffects":len(fixture.calls)}


def main():
    with tempfile.TemporaryDirectory(prefix="pi-authority-") as tmp:
        directory = Path(tmp).resolve()
        token = private(directory/"service.token", "synthetic-route-credential")
        state = {}
        def configure(fixture):
            service = fixture.service
            assert service.home.resolve().is_relative_to(directory)
            with service.workspace._connect() as db:
                db.execute("INSERT OR REPLACE INTO agent_host_compatibility VALUES ('pi','0.1.7','L3','fixture-only','probe','now')")
                fixture.account_id = db.execute("SELECT account_id FROM workspace_accounts WHERE user_subject=?",
                                                (fixture.subjects["a"],)).fetchone()[0]
            grant_permissions(service.user_grants,fixture.subjects["a"],PERMISSIONS)
            routes = Path(os.environ["AGENTBRIDGE_PI_ROUTES_FILE"])
            manifest = json.loads(routes.read_text())
            manifest["routes"]["pi-other"] = {**manifest["routes"]["pi-office"],"instanceId":"pi-other",
                                              "subjects":{fixture.subjects["a"]:"other-personal"}}
            del manifest["routes"]["pi-other"]["telegram"]
            routes.write_text(json.dumps(manifest))
            provision = WorkspaceHostProvision(service,AdminAuditStore(service.db_path),None)
            actor = {"username":"temporary-authority","role":"admin"}
            provision.save(actor,"loopback",{"instance_id":"pi-other","route_ref":"pi-other","display_name":"Second Pi",
                       "state":"enabled","subjects":[fixture.subjects["a"]],"expected_revision":0,"reason":"isolated probe"})
            provision.bind(actor,"loopback",{"instance_id":"pi-other","account_id":fixture.account_id,
                                             "expected_revision":1,"reason":"isolated probe"})
            state["secret"] = fixture.identities.issue(user_subject=fixture.subjects["a"],expected_principal_ref=fixture.subjects["a"],
                                                    label="workspace-pi:pi-other:other-personal")["token"]
            state["fixture"] = fixture
        with central_fixture(directory,host_url="http://127.0.0.1:1",token_file=token,maximum_level="L3", configure=configure,
                             central_options={"trusted_card_origin":ORIGIN,"service_class":credential_service()}) as (meta,accounts):
            print(json.dumps(asyncio.run(exercise(meta,accounts["a"],state["secret"],state["fixture"]))))


if __name__ == "__main__":
    main()
