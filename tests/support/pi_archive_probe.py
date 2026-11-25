"""Start a candidate's archived Pi worker exactly as native publication would, on loopback only."""
import argparse
from contextlib import ExitStack
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
from types import SimpleNamespace

import certifi
import httpx

from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_sdk_probe import model_server
from tests.support.pi_sdk_probe import Model, private
from tests.support.pi_workspace_acceptance import central_fixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/native"))
import nanobot_release
import pi_local
from pi_release import ADAPTER, file_digest


def stop_worker(child):
    child.terminate()
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def exercise(root, candidate, artifact, node, sdk, engine):
    """All business data, identities, service keys and model traffic are fixture-owned."""
    runtime = root / "pilot"
    runtime.mkdir(mode=0o700)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    secret = secrets.token_urlsafe(40)
    service_token = private(root / "service.token", secret)
    model = Model()
    with ExitStack() as stack:
        server = model_server(model)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(thread.join, 5)
        stack.callback(server.server_close)
        stack.callback(server.shutdown)
        auth = private(root / "model-auth.json", {})
        models = private(root / "models.json", {"providers":{"test-pi":{
            "api":"openai-completions","baseUrl":f"http://127.0.0.1:{server.server_port}/v1",
            "apiKey":"fixture","models":[{"id":"pi-fixture","input":["text"],"contextWindow":64000,"maxTokens":8000}]}}})
        model_config = private(root / "model.json", {"provider":"test-pi","id":"pi-fixture","authFile":auth,"modelsFile":models})
        mac = {"caCertificate":certifi.where(),"tunnel":{"knownHostsFile":str(root/"unused-hosts"),"host":"unused.invalid"}}
        central = []
        meta, accounts = stack.enter_context(central_fixture(root,host_url=url,token_file=service_token,configure=central.append))
        spec = {"runtimeDir":str(runtime),"enginePython":str(engine),"sdkDir":str(sdk),"nodePath":str(node),
                "adapterVersion":ADAPTER,"instanceId":"pi-office","listenPort":port,"remotePort":port,
                "serviceTokenFile":service_token,"centralMcpUrl":meta["endpoint"],"trustedCardOrigins":[],
                "requiredLevel":"L1","modelConfigFile":model_config,"tunnelIdentityFile":str(root/"unused-key"),
                "subjects":{b["user_subject"]:b["credential_ref"] for b in accounts.values()}}
        release = pi_local.archive(candidate,spec,artifact)
        archived_wheel = release / "agentbridge.whl"
        if file_digest(archived_wheel) != artifact["sha256"]:
            raise ValueError("Archived Pi worker wheel differs from the candidate")
        records = [{"binding":{"instance_id":spec["instanceId"],**b},
                    "personalCredential":Path(meta["tokenFiles"][label]).read_text().strip()} for label,b in accounts.items()]
        pi_local.configure(spec,mac,runtime,release,records)
        verified = pi_local.verify_subjects(spec,release)
        nanobot_release.set_current(runtime,release)
        job = nanobot_release.jobs(spec,mac,host_type="pi")["ai.agentbridge.pi.pi-office.worker"]
        child = subprocess.Popen(job["ProgramArguments"],cwd=job["WorkingDirectory"],stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,umask=job["Umask"])
        stack.callback(stop_worker,child)
        client = stack.enter_context(httpx.Client(base_url=url,headers={"Authorization":"Bearer "+secret},timeout=15,trust_env=False))
        def post(action,**body):
            response = client.post("/v1/"+action,json={"instance":spec["instanceId"],**body})
            response.raise_for_status()
            return response.json()
        deadline = time.monotonic()+18
        while time.monotonic()<deadline:
            if child.poll() is not None:
                raise RuntimeError("Archived Pi worker exited before readiness")
            try:
                ready = post("ready")
                if ready.get("available"):
                    break
            except httpx.TransportError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError("Archived Pi worker failed to become ready")
        if ready.get("acceptedLevel")!="L1" or (ready.get("configuredBindings"),ready.get("verifiedBindings"))!=(2,2):
            raise ValueError("Archived Pi worker did not authenticate both personal bindings")
        completed = []
        for label, binding in accounts.items():
            args = {"binding":binding,"requestKey":"archive-read-"+label}
            run = post("accept",**args,message="请查我的待办。")["runId"]
            deadline = time.monotonic()+30
            while time.monotonic()<deadline:
                row = post("lookup",**args)
                if row["state"]!="accepted":
                    break
                time.sleep(.1)
            expected = "采购申请" if label=="a" else "未获待办查询权限"
            if row["state"]!="completed" or row["runId"]!=run or expected not in row["answer"]:
                raise ValueError("Archived worker failed its account-scoped business read")
            completed.append(run)
        fixture, = central
        actual = [(call["owner"],call["capability"]) for call in fixture.calls]
        expected = [(accounts["a"]["user_subject"],"oa.workflow.pending.list")]
        with sqlite3.connect(fixture.service.db_path) as db:
            operations = db.execute("SELECT user_subject,capability_name,status FROM operations ORDER BY rowid").fetchall()
        if (actual!=expected or operations!=[(*expected[0],"succeeded")] or model.business_calls!=1
                or model.failures or len(set(completed))!=2):
            raise ValueError("Archive smoke expected one authorized Operation and zero other business effects")
        if pi_local.archive(candidate,spec,artifact)!=release:
            raise ValueError("Pi candidate archive changed during worker execution")
        denied_reads = sum(owner == accounts["b"]["user_subject"] for owner, _ in actual)
        other_operations = sum(capability != "oa.workflow.pending.list" for _, capability, _ in operations)
        return {"worker":"pi_agentbridge.server","launch":"native_worker_program_arguments",
                "runtime":"real_pi_sdk","central":"loopback_mcp","model":"synthetic_loopback","downstream":"synthetic_oa",
                "testedLevel":ready["acceptedLevel"],"personalIdentities":verified["authenticatedSubjects"],
                "completedRuns":len(completed),"authorizedReads":len(actual),"deniedSubjectReads":denied_reads,
                "succeededOperations":len(operations),"businessWrites":other_operations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit",required=True)
    parser.add_argument("--wheel-sha256",required=True)
    for key in ("wheel","node","sdk-dir","engine-python"):
        parser.add_argument("--"+key,type=Path,required=True)
    args = parser.parse_args()
    os.umask(0o077)
    candidate = SimpleNamespace(commit=args.commit,release=args.commit[:12])
    artifact = {"wheel":args.wheel,"sha256":args.wheel_sha256}
    with tempfile.TemporaryDirectory(prefix="pi-archive-smoke-") as tmp:
        root = Path(tmp).resolve()
        from unittest.mock import patch
        # This probe is also callable alone. Prevent inherited production configuration.
        environment = {key:os.environ[key] for key in ("PATH","LANG","LC_ALL") if key in os.environ}
        environment.update(HOME=str(root),TMPDIR=str(root),AGENTBRIDGE_HOME=str(root/"isolated-central"),
                           PYTHONNOUSERSITE="1",PYTHONDONTWRITEBYTECODE="1")
        with patch.dict(os.environ,environment,clear=True):
            print(json.dumps(exercise(root,candidate,artifact,args.node,args.sdk_dir,args.engine_python)))


if __name__=="__main__":
    main()
