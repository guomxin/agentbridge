"""Assemble a Pi release qualification from formal conformance and observed client receipts."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from common import ROOT, external, private_file, read, save
from nanobot_release import owned_directory
from pi_release import ADAPTER, DIGEST, LEVELS, SDK_VERSION, check_observed_web_subjects, conformance_evidence, engine_identity, file_digest, verify_channel_case, verify_runtime
from pi_model_observation import fixed_candidate

sys.path.insert(0,str(ROOT / "scripts"))
from run_pi_conformance import digest, run_probe, snapshot


# A probe's checked measurements and actual execution are hashed in the formal report.
CONTRACT = {
    "subject_isolation": ("sdk", ("H02","H18")),
    "private_envelope": ("credential", ("H04","H16")),
    "private_resume": ("credential", ("H06","H07","H26")),
    "original_lease": ("authority", ("H21","H22")),
    "restart_recovery": ("governed", ("H11","H19","H29")),
    "unknown_commit_no_replay": ("governed", ("H10",)),
}
DECLINE = "tests/test_pi_private_telegram.py::test_private_pi_rejects_before_download_pairing_or_dispatch"
PRODUCT = {
    "two_endpoints": ("tests.support.pi_workspace_images_probe",
        {"passed":True,"sdk":SDK_VERSION,"acceptedLevel":"L3","modelReceived":2,"originalUploads":2,
         "privateReceipts":2,"syntheticModel":True,"syntheticTelegram":True}),
    "artifact_delivery": ("tests.support.pi_artifact_delivery_probe",
        {"agentHost":"pi","originalTask":True,"privateEndpoint":True,"types":["application/pdf","text/csv"],
         "originalBytesMatched":2,"acknowledged":2,"deliveryAttempts":[1,1],"syntheticTelegram":True}),
    "scheduled_delivery": ("tests.support.pi_scheduled_delivery_probe",
        {"host":"pi","digestScheduled":1,"repeatScheduleClaims":0,"digestAcknowledged":1,"watchAcknowledged":1,
         "postRevocationWatchSends":0,"otherSubjectNotifications":0,"syntheticTelegram":True}),
    "file_delivery_failure": ("tests.support.pi_delivery_failure_probe",
        {"agentHost":"pi","partial":{"sent":1,"http429":1,"unknown":1,"reclaims":0},
         "postRevocationUploads":0,"postRevocationFileReadsAllowed":False,"syntheticTelegram":True}),
}


def system_reads(level):
    rows = []
    for system, expired in (("taihua",False),("yuque",False),("smartlight",False),("oa",False),("taihua",True)):
        count = 0 if expired else 1
        rows.append({"system":system,"expired":expired,"samePiRun":True,"sameTask":level == "L3" and not expired,
                     "businessReads":count,"successfulOperations":count,"modelCalls":2})
    return {"level":level,"cases":rows,"businessWrites":0,"syntheticBroker":True,
            "syntheticBusinessData":True,"privateTelegramRoute":True}


for _level in ("L2","L3"):
    PRODUCT["four_system_reads_"+_level.lower()] = ("tests.support.pi_system_read_probe",system_reads(_level))


def required_cases(spec):
    cases = {"workspace_first","workspace_second","subject_isolation","real_model"}
    if spec.get("telegram"):
        cases.update(("telegram_first","telegram_second_denied"))
    if spec["requiredLevel"] in ("L2","L3"):
        cases.update(("private_envelope","private_resume","four_system_reads_"+spec["requiredLevel"].lower()))
    if spec["requiredLevel"] == "L3":
        cases.update(("original_lease","restart_recovery","unknown_commit_no_replay","artifact_delivery","two_endpoints"))
        if spec.get("telegram"):
            cases.update(("scheduled_delivery","file_delivery_failure"))
    return cases


def checked_report(report, candidate, spec):
    exact = {"schema":"agentbridge.pi-conformance.v1","implementation":"pi","adapterVersion":ADAPTER,
             "sdkVersion":SDK_VERSION,"level":spec["requiredLevel"],"engine":engine_identity(spec["enginePython"]),
             **candidate, **{key:spec[key] for key in ("nodeSha256","sdkTreeSha256","sdkLockSha256")}}
    if (not all(report.get(k)==v for k,v in exact.items())
            or not all(report.get(flag) is True for flag in ("clean","formal","passed","sourceStable","runtimeStable"))
            or report.get("contractEvidence") != conformance_evidence(report)):
        raise ValueError("Pi formal conformance does not match the approved candidate and runtime")
    vectors = read(ROOT / "schemas/agent-host/v1/test-vectors.json")["conformanceCases"]
    required = {v["id"] for v in vectors if LEVELS.index(v["level"]) <= LEVELS.index(spec["requiredLevel"])}
    results = report["cases"]
    if (len(results) != len(vectors) or {v["id"] for v in results} != {v["id"] for v in vectors}
            or any(v["status"] != ("passed" if v["id"] in required else "outside_level") for v in results)):
        raise ValueError("Incomplete Pi contract vectors")
    probes = report["probes"]
    coverage = {claim for probe in probes for claim in probe["cases"]}
    if (len(probes) != len({p["probe"] for p in probes}) or not required <= coverage
            or any(p.get("passed") is not True or not DIGEST.fullmatch(str(p.get("evidenceSha256","")))
                   for p in probes)):
        raise ValueError("Pi formal conformance lacks measured passing assertions")


def contract_measurement(case, report):
    name, claims = CONTRACT[case]
    matches = [p for p in report["probes"] if p["probe"]==name]
    if (len(matches)!=1 or matches[0].get("passed") is not True
            or not DIGEST.fullmatch(str(matches[0].get("evidenceSha256","")))
            or not set(claims) <= set(matches[0]["cases"])):
        raise ValueError("Missing measured Pi conformance assertions for "+case)
    return {"source":"formal_conformance","contractEvidence":report["contractEvidence"],"probe":name,
            "cases":list(claims),"measurementSha256":matches[0]["evidenceSha256"]}


def product_measurement(case):
    if case == "telegram_second_denied":
        path, name = DECLINE.split("::")
        measured = {"test":name,"sourceSha256":file_digest(ROOT/path)}
        return {"source":"pi_injected_update","probe":DECLINE,"measurementSha256":digest(measured),
                "observationMode":"injected","transport":"injected_update","denied":True,"dispatchCount":0}
    source, expected = PRODUCT[case]
    measured = {"measurements":[expected],"sourceSha256":file_digest(ROOT/(source.replace(".","/")+".py"))}
    return {"source":"pi_product_probe","probe":source,"measurementSha256":digest(measured),"measurements":expected}


def run_product(case, spec):
    expected = product_measurement(case)
    args = SimpleNamespace(node=spec["nodePath"],sdk_dir=spec["sdkDir"],engine_python=spec["enginePython"])
    if case == "telegram_second_denied":
        probe = (case,{}, "pytest", DECLINE,None)
    else:
        source, payload = PRODUCT[case]
        probe = (case,{}, "governed", source, lambda lines:lines == [payload])
    flags = ("--level",spec["requiredLevel"]) if case.startswith("four_system_reads_") else ()
    checked = run_probe(probe,args,extra_flags=flags)
    if checked["passed"] is not True or checked["evidenceSha256"] != expected["measurementSha256"]:
        raise ValueError("Pi qualification product probe failed: "+case)
    return expected


def pointer(path):
    path = private_file(path)
    return {"path":str(path),"sha256":file_digest(path)}


def save_immutable(path, payload):
    with tempfile.TemporaryDirectory(prefix=".pi-qualification-", dir=path.parent) as tmp:
        incoming = Path(tmp)/"receipt"
        save(incoming,payload)
        try:
            os.link(incoming,path)
        except FileExistsError:
            if private_file(path).read_bytes() != incoming.read_bytes():
                raise ValueError("Existing Pi qualification evidence differs from this observation") from None
    return pointer(path)


def assemble(spec, report_path, provided, output, *, higher=None):
    candidate = fixed_candidate()
    verify_runtime(spec)
    report_ref = pointer(report_path)
    report = read(report_ref["path"])
    checked_report(report,candidate,spec)
    if report.get("sourceSha256") != snapshot()["sourceSha256"]:
        raise ValueError("Pi source or probe code changed since the formal run")
    header = {"schema":"agentbridge.pi-observation.v1","implementation":"pi","sdkVersion":SDK_VERSION,
              "adapterVersion":ADAPTER,"passed":True,**candidate}
    cases = required_cases(spec)
    required_clients = cases - CONTRACT.keys() - PRODUCT.keys() - {"telegram_second_denied"}
    refs, marker_ids, run_ids, observed_web = {}, set(), set(), {}
    for path in provided:
        ref = pointer(path)
        value = read(ref["path"])
        case = value.get("case")
        if (case not in required_clients | (cases & {"telegram_second_denied"}) or case in refs
                or any(value.get(k)!=v for k,v in header.items()) or value.get("passed") is not True
                or case == "telegram_second_denied" and value.get("observationMode") != "real"):
            raise ValueError("Provided observation has an unexpected case, origin or candidate")
        verify_channel_case(case,value,spec)
        if case in {"workspace_first","workspace_second"}:
            observed_web[case] = value["subjectSha256"]
        if case in {"workspace_first","workspace_second","telegram_first"}:
            if value["markerSha256"] in marker_ids or value["runSha256"] in run_ids:
                raise ValueError("Required client observations reused one original run")
            marker_ids.add(value["markerSha256"])
            run_ids.add(value["runSha256"])
        refs[case] = ref
    missing = sorted(required_clients - refs.keys())
    if missing:
        return {"complete":False,"missing":missing}
    check_observed_web_subjects(observed_web,spec)
    output = external(output)
    evidence_dir = output.with_name(output.stem+"-observations")
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Publish pointers only after all independent checks pass.
    measured = {}
    for case in sorted(cases - refs.keys()):
        measured[case] = contract_measurement(case,report) if case in CONTRACT else run_product(case,spec)
    verify_runtime(spec)
    if (fixed_candidate()!=candidate or snapshot()["sourceSha256"]!=report["sourceSha256"]
            or pointer(report_path) != report_ref or any(pointer(ref["path"]) != ref for ref in refs.values())):
        raise ValueError("Pi candidate changed during qualification")
    extra = {}
    if higher:
        high_ref = pointer(higher)
        reviewed = read(high_ref["path"])
        if reviewed.get("level") not in LEVELS or LEVELS.index(reviewed["level"]) <= LEVELS.index(spec["requiredLevel"]):
            raise ValueError("Expected a higher approved Pi contract level")
        checked_report(reviewed,candidate,{**spec,"requiredLevel":reviewed["level"]})
        extra["higherLevelConformance"] = high_ref
    evidence_dir = owned_directory(evidence_dir)
    for case, values in measured.items():
        refs[case] = save_immutable(evidence_dir/(case+".json"),{**header,"case":case,**values})
    result = save_immutable(output,{"schema":"agentbridge.pi-qualification.v1","implementation":"pi","adapterVersion":ADAPTER,
                 "sdkVersion":SDK_VERSION,**candidate,"level":spec["requiredLevel"],
                 **{k:spec[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256","modelConfigSha256")},
                 "contractEvidence":report["contractEvidence"],"conformance":report_ref,
                 "observations":dict(sorted(refs.items())),**extra})
    return {"complete":True,"cases":sorted(refs),"contractReceipt":result}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("runtime-pins","conformance","output"):
        parser.add_argument("--"+key,required=True)
    parser.add_argument("--observation",action="append",default=[],help="Actual candidate-bound client or model observation")
    parser.add_argument("--higher-conformance",help="Same candidate's higher-level receipt when registering a lower deployment")
    args=parser.parse_args()
    result=assemble(read(private_file(args.runtime_pins)),args.conformance,args.observation,args.output,
                    higher=args.higher_conformance)
    print(json.dumps(result))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
