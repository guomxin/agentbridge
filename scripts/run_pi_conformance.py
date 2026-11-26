"""Run Pi's own isolated probes and derive level coverage from executed assertions."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/native"))
from common import save
from pi_release import ADAPTER, LEVELS, SDK, SDK_VERSION, archive_probe_matches, conformance_evidence, engine_identity, expected_archive_probe, file_digest, sdk_tree_digest, verify_runtime


# Each item names the concrete, independently checked behavior within this run.
# Missing cases stay unverified even when all implemented probes succeed.
SDK_CASES = {"H02":"personal read with fixture OA through Pi SDK", "H12":"distinct responses for differently granted accounts",
             "H18":"other subject cannot read the original Pi run"}
CREDENTIAL_CASES = {"H04":"only the bound private endpoint receives the credential card",
                    "H06":"two repeat requests yield the same completed interaction and no additional original read",
                    "H07":"expired card leaves zero verified resumes and zero original reads",
                    "H16":"private URL absent from model inputs, public events and host state",
                    "H20":"second listener cannot claim the original run; repeated completed receipts trigger no new execution",
                    "H17":"model catalog excludes host controls, resume and commit",
                    "H26":"verified login resumes the immediately preceding read exactly once"}
AUTHORITY_CASES = {"H13":"renew an expired Pi task entry using retained bytes and reject the other web account",
                   "H15":"two uniquely keyed original-task messages remain ordered despite repeated append",
                   "H21":"valid second Pi registration cannot acquire the first Pi task",
                   "H22":"another registered Pi cannot reach central resume for the original interaction",
                   "H24":"Pi coordinator retries one transient read and stops after one unsafe transport failure"}
GOVERNED_CASES = {"H05":"two synthetic same-owner endpoints offer the same credential and its second claim fails",
                  "H08":"expired session completes and the original controlled preparation continues",
                  "H09":"field preview has no effects before separate approval", "H10":"unknown outcome has one downstream effect",
                  "H14":"final Telegram delivery failure leaves the original task and operation successful with one business effect",
                  "H11":"host is terminated while waiting and recovers the original decision with one effect",
                  "H19":"decision completed offline is picked up on the original interaction after restart",
                  "H29":"another Pi coordinator process recovers the persisted plan after process exit"}


def check_authority(lines):
    return lines == [{"timelineTexts":2,"foreignLeaseRejected":True,"foreignResumes":0,"reissuedCsv":1,"crossOwnerFileReads":0,"readAttempts":2,
                      "unsafeAttempts":1,"runtimeSnapshots":1,"syntheticBusinessEffects":0}]


def check_governed(lines):
    if len(lines) != 4:
        return False
    expected = (("verified",1,["credential","business_input"],[1,2],True,2),
                ("unknown",1,["business_input"],[1],False,2), ("plan",0,[],[],False,3),
                ("restart",1,["business_input"],[1],False,1))
    for entry, (case, effects, cards, revisions, duplicate, calls) in zip(lines, expected):
        if (entry.get("case"),entry.get("effects"),entry.get("cards"),entry.get("cardRevisions"),
                entry.get("duplicateChallengeBlocked"),entry.get("modelCalls"),entry.get("sameRun"),entry.get("sdk")) != (
                case,effects,cards,revisions,duplicate,calls,True,SDK_VERSION):
            return False
    return (lines[0].get("notificationOutcome") == "failed" and lines[0].get("notificationAttempts") == 5
            and lines[0].get("originalTaskStillSucceeded") is True and lines[0].get("originalOperationStillSucceeded") is True
            and lines[0].get("businessExecutionCount") == 1 and type(lines[0].get("automaticSnapshotSignals")) is int and lines[0]["automaticSnapshotSignals"] >= 1
            and lines[2].get("centralPlannerRestarted") is True and lines[2].get("restoredStepAttempts") == 2
            and lines[2].get("restoredPlanCount") == 1 and lines[2].get("hiddenCommitRefused") is True
            and lines[3].get("offlineDecision") is True and lines[3].get("restoredOriginal") is True
            and lines[3].get("modelReplay") is False)


def check_composite(lines):
    return lines == [{"sameOriginalRun":True, "plans":1, "systems":["oa","smartlight"], "distinctOperations":2,
                      "hiddenCommitRejected":True,"sourceEndpointMatched":True,"writes":0}]


def check_batch(lines):
    return lines == [{"sameParent":True,"distinctCards":2,"distinctApprovals":2,
                      "verifiedOperations":2,"effects":2,"automaticRuntimeSignal":True}]

def check_result_views(lines):
    return lines == [{"completePrivateCards": 1, "refreshedPrivateCards": 1, "emptyCards": 0,
                      "partialActionCards": 0, "permissionLossRejected": True, "sameRunReconciled": True,
                      "otherEndpointsRejected": True, "modelCalls": 6, "writes": 0}]


AUTHORITY_TESTS = {
    "test_exact_pi_review_is_needed_for_elevated_registration_and_downgrade":{"H01":"unreviewed version downgrades to L1"},
    "test_pi_personal_registration_and_tool_scope_through_mcp":{"H03":"L1 cannot call a credential interaction"},
    "test_coordinator_requires_original_pi_endpoint_and_current_pair":{"H21":"only original valid Pi host and endpoint can own the lease"},
}
JOURNAL_TESTS = {
    "test_interrupted_accepted_run_is_reconciled_without_starting_it_again":{"H27":"after a process exits an accepted run becomes unknown, with no repeat execution"},
}
SOURCES = ["agentbridge", "integrations/pi-agentbridge", "integrations/reference-host", "integrations/nanobot-agentbridge",
           "tests", "schemas/agent-host", "scripts/native", "scripts/run_pi_conformance.py"]


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",",":")).encode()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def snapshot():
    tracked = git("ls-files", "-co", "--exclude-standard", "-z", "--", *SOURCES).split(b"\0")
    files = {}
    for item in sorted(filter(None, tracked)):
        path = ROOT / os.fsdecode(item)
        files[os.fsdecode(item)] = file_digest(path) if path.is_file() else None
    adapter = {name: value for name,value in files.items() if name.startswith((
        "integrations/pi-agentbridge/","integrations/reference-host/reference_host/","integrations/nanobot-agentbridge/nanobot_agentbridge/"))}
    return {"commit":git("rev-parse","HEAD").decode().strip(), "sourceSha256":digest(files),"adapterSourcesSha256":digest(adapter),
            "clean":not bool(git("status","--porcelain","--untracked-files=normal").strip())}


def execute(argv, env, **kwargs):
    return subprocess.run([sys.executable, *map(str,argv)], cwd=ROOT,env=env,stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=360, check=True, **kwargs)


def run_archive_probe(args, artifact, pins):
    try:
        with tempfile.TemporaryDirectory(prefix="pi-formal-archive-") as directory:
            env = {k:os.environ[k] for k in ("PATH","LANG","LC_ALL") if k in os.environ}
            env.update(HOME=directory,TMPDIR=directory,AGENTBRIDGE_HOME=str(Path(directory)/"fixture"),
                       PYTHONNOUSERSITE="1",PYTHONDONTWRITEBYTECODE="1")
            result = execute(["-m","tests.support.pi_archive_probe","--commit",pins["commit"],
                              "--wheel",artifact["wheel"],"--wheel-sha256",artifact["sha256"],
                              "--node",args.node,"--sdk-dir",args.sdk_dir,"--engine-python",args.engine_python],env)
        proof = {**expected_archive_probe(pins),"measurements":json.loads(result.stdout)}
        if archive_probe_matches({**pins,"archiveProbe":proof}):
            return proof
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def check_sdk(payloads):
    return payloads == [{"passed":True,"sdk":SDK_VERSION,"adapter":ADAPTER,"subjects":2,"businessReads":1,
                         "syntheticModel":True,"syntheticDownstream":True,"oneRevokedAvailable":True,
                         "bothRevokedUnavailable":True,"restoredBindings":2}]


def check_credential(payloads):
    return payloads == [{"level":"L2","expired":expired,"resumes":count,"downstreamReads":count,
                         "crossSubjectDenied":True,"modelCalls":2,"duplicateReceiptChecks":0 if expired else 2,"competingListenerRefused":True} for expired, count in [(False,1),(True,0)]]


def run_probe(plan, args, *, extra_flags=()):
    name, cases, kind, source, verify = plan
    try:
        with tempfile.TemporaryDirectory(prefix="pi-conformance-") as tmp:
            root = Path(tmp)
            key = root/"session.key"
            key.write_bytes(os.urandom(32))
            key.chmod(0o600)
            env = {k:os.environ[k] for k in ("PATH","LANG","LC_ALL","SYSTEMROOT") if k in os.environ}
            env.update(HOME=tmp,TMPDIR=tmp,AGENTBRIDGE_HOME=str(root/"central"),AGENTBRIDGE_SESSION_KEY_FILE=str(key),
                       PYTHONNOUSERSITE="1",PYTHONDONTWRITEBYTECODE="1",PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
            if kind in {"sdk","central","governed","composite"}:
                flags = [] if kind == "central" else ["--node",args.node,"--sdk-dir",args.sdk_dir]
                if kind in {"governed","composite"}:
                    if not args.engine_python:
                        raise ValueError("Pi governed probe requires its pinned Python executable")
                    flags.extend(("--engine-python",args.engine_python))
                if kind == "composite":
                    flags.extend(("--case","composite"))
                output = execute(["-m",source,*flags,*extra_flags], env)
                measured = [json.loads(line) for line in output.stdout.splitlines() if line.strip()]
                if not verify(measured):
                    raise ValueError("Unexpected observation")
                if source == "tests.support.pi_governed_probe":
                    # Collector cadence can produce additional healthy snapshots on a slower run.
                    measured[0]["automaticSnapshotSignals"] = "observed"
                measured = {"measurements":measured,"sourceSha256":file_digest(ROOT / (source.replace(".", "/") + ".py"))}
            else:
                xml = root/"test-results.xml"
                test, identity = source.split("::")
                execute(["-m","pytest","-q","-p","no:cacheprovider","--junitxml",xml,source],env)
                results = ET.parse(xml).getroot().findall(".//testcase")
                if len(results) != 1 or results[0].get("name") != identity or any(
                    results[0].find(tag) is not None for tag in ("failure","skipped","error")):
                    raise ValueError("Exact assertion case did not run")
                measured = {"test":identity,"sourceSha256":file_digest(ROOT/test)}
        return {"probe":name,"passed":True,"evidenceSha256":digest(measured),"cases":cases}
    except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError):
        return {"probe":name,"passed":False,"cases":cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir",type=Path,required=True)
    parser.add_argument("--node",type=Path,required=True)
    parser.add_argument("--engine-python",type=Path,help="Reviewed Pi coordinator Python")
    parser.add_argument("--level",choices=LEVELS,default="L3")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--formal",action="store_true",help="Require a clean exact candidate and its formally verified central wheel")
    args = parser.parse_args()
    args.node, args.sdk_dir = args.node.resolve(strict=True), args.sdk_dir.resolve(strict=True)
    modules = args.sdk_dir.parents[1]
    runtime = {"sdkDir":str(args.sdk_dir),"nodePath":str(args.node),"sdkTreeSha256":sdk_tree_digest(modules),
               "sdkLockSha256":file_digest(modules.parent/"package-lock.json"),"nodeSha256":file_digest(args.node)}
    verify_runtime(runtime)
    engine = engine_identity(args.engine_python) if args.engine_python else None
    if args.formal and not engine:
        raise ValueError("Formal Pi conformance requires its reviewed Python engine")
    start = snapshot()
    artifact = None
    if args.formal:
        if not start["clean"]:
            raise ValueError("Formal Pi conformance requires a clean fixed candidate")
        artifact = json.loads(subprocess.check_output(
            [sys.executable,ROOT/"scripts/agentbridge_artifact.py","verify","--root",ROOT],cwd=ROOT))
        if file_digest(artifact["wheel"]) != artifact["sha256"]:
            raise ValueError("Pi conformance wheel hash differs")
    vectors = json.loads((ROOT/"schemas/agent-host/v1/test-vectors.json").read_bytes())["conformanceCases"]
    required = {case["id"] for case in vectors if LEVELS.index(case["level"]) <= LEVELS.index(args.level)}
    plans = [("sdk",SDK_CASES,"sdk","tests.support.pi_sdk_probe",check_sdk),
             ("credential",CREDENTIAL_CASES,"sdk","tests.support.pi_credential_probe",check_credential),
             ("authority",AUTHORITY_CASES,"central","tests.support.pi_authority_probe",check_authority),
             ("governed",GOVERNED_CASES,"governed","tests.support.pi_governed_probe",check_governed),
             ("result_views", {"H18": "only the original private endpoint can receive the completed OA lookup and refresh cards"},
              "governed", "tests.support.pi_result_view_probe", check_result_views),
             ("composite",{"H28":"scope-checked two-system plan executes two original read operations on one durable task"},
              "composite","tests.support.pi_batch_probe",check_composite),
             ("batch",{"H23":"two items independently approved then authoritatively verified on one parent",
                       "H25":"the normal Pi coordinator emits a runtime signal for its exact identity"},
              "governed","tests.support.pi_batch_probe",check_batch)]
    workspace_views = {"test_companion_uses_original_web_read_and_current_pair": {
        "H18": "the paired first Telegram account can open only the original eligible Workspace result; re-pairing invalidates the card"}}
    for path, cases in (("tests/test_pi_central.py",AUTHORITY_TESTS),("tests/test_pi_journal_conformance.py",JOURNAL_TESTS),
                        ("tests/test_pi_workspace_result_views.py",workspace_views)):
        plans.extend((name, claims, "pytest", path+"::"+name, None) for name, claims in cases.items())
    selected = [p for p in plans if set(p[1]) & required]
    pins = {"commit":start["commit"],"wheelSha256":artifact["sha256"] if artifact else None,
            "sdkVersion":SDK_VERSION,"adapterVersion":ADAPTER,"engine":engine,
            **{k:runtime[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256")}}
    with ThreadPoolExecutor(max_workers=3) as pool:
        archive_job = pool.submit(run_archive_probe,args,artifact,pins) if args.formal else None
        checks = list(pool.map(lambda plan:run_probe(plan,args),selected))
        archived = archive_job.result() if archive_job else None
    cases = []
    for item in vectors:
        matches = [p for p in checks if item["id"] in p["cases"]]
        if item["id"] not in required:
            status = "outside_level"
        elif not matches:
            status = "unverified"
        else:
            status = "passed" if all(p["passed"] for p in matches) else "failed"
        cases.append({**item,"status":status})
    finish = snapshot()
    runtime_stable = (sdk_tree_digest(modules) == runtime["sdkTreeSha256"]
                      and file_digest(modules.parent/"package-lock.json") == runtime["sdkLockSha256"]
                      and file_digest(args.node) == runtime["nodeSha256"]
                      and (not args.engine_python or engine == engine_identity(args.engine_python)))
    complete = (start == finish and runtime_stable and (not args.formal or archived is not None)
                and all(c["status"] in {"passed","outside_level"} for c in cases))
    report = {"schema":"agentbridge.pi-conformance.v1","implementation":"pi","adapterVersion":ADAPTER,
              "sdkPackage":SDK,"sdkVersion":SDK_VERSION,"level":args.level, **start,
              "sourceStable":start == finish,"runtimeStable":runtime_stable,"engine":engine,"formal":args.formal,"wheelSha256":artifact["sha256"] if artifact else None,
              **{k:runtime[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256")},
              "cases":cases,"probes":checks,"passed":complete}
    if args.formal:
        report["archiveProbe"] = archived
    report["contractEvidence"] = conformance_evidence(report) if complete else None
    save(args.output,report)
    print(json.dumps({"passed":complete,"level":args.level,"sourceStable":start==finish,
                      "covered":[c["id"] for c in cases if c["status"]=="passed"],
                      "missing":[c["id"] for c in cases if c["status"]=="unverified"],
                      "failed":[p["probe"] for p in checks if not p["passed"]]
                              + (["archive_worker"] if args.formal and archived is None else [])}))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
