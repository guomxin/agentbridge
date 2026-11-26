"""Reviewed Pi candidate inputs and the optional native pilot transaction."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit

from common import ROOT, external, private_file, read, run, save
import nanobot_release as shared
from nanobot_tunnel import tunnel_settings
from nanobot_remote import transition_routes

SDK = "@earendil-works/pi-coding-agent"
SDK_VERSION = "1.6.0"
ADAPTER = "0.1.3"
LEVELS = ("L1", "L2", "L3")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sdk_tree_digest(modules):
    """Pin the installed executable dependency tree as well as its npm lock."""
    digest = hashlib.sha256()
    for path in sorted(modules.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            if not path.resolve(strict=True).is_relative_to(modules):
                raise ValueError("Pi SDK dependency link leaves the reviewed installation")
            entry = "link:" + target
        elif path.is_file():
            entry = "file:" + file_digest(path)
        elif path.is_dir():
            continue
        else:
            raise ValueError("Unexpected file type in the Pi SDK installation")
        digest.update(os.fsencode(path.relative_to(modules)) + b"\x00" + os.fsencode(entry) + b"\x00")
    return digest.hexdigest()


def verify_runtime(spec):
    sdk = external(spec["sdkDir"])
    modules = sdk.parents[1]
    if sdk != modules / SDK or modules.name != "node_modules":
        raise ValueError("Expected the reviewed installed Pi npm package")
    package = read(sdk / "package.json")
    lock = modules.parent / "package-lock.json"
    entry = read(lock)["packages"]["node_modules/" + SDK]
    if (package.get("name") != SDK or package.get("version") != SDK_VERSION
            or entry.get("version") != SDK_VERSION
            or not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", entry.get("integrity", ""))
            or file_digest(lock) != spec["sdkLockSha256"]
            or sdk_tree_digest(modules) != spec["sdkTreeSha256"]):
        raise ValueError("Pi SDK lock, distribution or installed files changed")
    node = external(spec["nodePath"])
    if file_digest(node) != spec["nodeSha256"] or run([node, "--version"]) != "v26.5.0":
        raise ValueError("Pi Node runtime differs from the reviewed binary")


def engine_identity(path):
    versions = json.loads(run([path, "-I", "-c",
        "import importlib.metadata as m,json,sys;print(json.dumps([sys.version]+[m.version(n) for n in ('mcp','uvicorn','Pillow','pypdf')]))"],
        timeout=15))
    if versions[1:] != ["1.23.2", "0.51.0", "13.0.0", "6.21.4"]:
        raise ValueError("Pi coordinator engine differs from reviewed dependencies")
    return {"executableSha256":file_digest(path),"versions":versions}


def checked_reference(reference):
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
        raise ValueError("A pinned private evidence file is required")
    path = private_file(reference["path"])
    if file_digest(path) != reference["sha256"]:
        raise ValueError("Private evidence digest mismatch")
    return path


def pinned(reference):
    return read(checked_reference(reference))


def verify_channel_case(case, proof, spec):
    if case in {"workspace_first", "workspace_second", "telegram_first"}:
        owner = proof.get("subjectSha256")
        subjects = {hashlib.sha256(s.encode()).hexdigest() for s in spec["subjects"]}
        if proof.get("instanceId") != spec["instanceId"] or owner not in subjects:
            raise ValueError("Pi observed run belongs to a different reviewed instance or subject")
        if spec.get("telegram"):
            first, = spec["telegram"]["allowedSenders"]
            first_hash = hashlib.sha256(first.encode()).hexdigest()
            if (case in {"workspace_first","telegram_first"}) != (owner == first_hash):
                raise ValueError("Pi client observation belongs to the wrong reviewed account")
        capture = checked_reference(proof.get("capture"))
        marker = proof.get("markerSha256")
        run_id = proof.get("runSha256")
        if (not isinstance(marker, str) or not DIGEST.fullmatch(marker)
                or not isinstance(run_id, str) or not DIGEST.fullmatch(run_id)
                or capture.stat().st_size > 256_000):
            raise ValueError("Expected a client capture correlated with the original Pi run")
        markers = re.findall(rb"\bPI-[a-f0-9]{32}\b", capture.read_bytes())
        if not any(hashlib.sha256(value).hexdigest() == marker for value in markers):
            raise ValueError("Client capture does not contain its unique Pi acceptance message")
    if case in {"workspace_first", "workspace_second"}:
        if proof.get("client") != "chrome_extension":
            raise ValueError("Workspace acceptance requires an actual Chrome extension observation")
    elif case == "telegram_first":
        if (proof.get("client") != "telegram_desktop" or proof.get("transport") != "telegram_bot_api"
                or not isinstance(proof.get("receiptPath"),str) or not DIGEST.fullmatch(str(proof.get("receiptSha256","")))):
            raise ValueError("First Pi account requires Telegram Desktop and an actual Bot API delivery receipt")
        receipt = pinned({"path":proof["receiptPath"],"sha256":proof["receiptSha256"]})
        bot = spec["telegram"]
        if (receipt.get("schema") != "agentbridge.pi-telegram-desktop-receipt.v1"
                or receipt.get("botId") != bot["botId"] or receipt.get("senderId") not in bot["allowedSenders"].values()
                or receipt.get("markerSha256") != marker):
            raise ValueError("Telegram desktop observation differs from the reviewed first user and bot")
        desktop = checked_reference(receipt.get("desktopEvidence"))
        inbox = checked_reference(receipt.get("inboxEvidence"))
        if not desktop.samefile(capture) or desktop.samefile(inbox) or file_digest(desktop) == file_digest(inbox):
            raise ValueError("The Telegram desktop observation and inbox must have independent evidence")
        incoming = read(inbox)
        if (incoming.get("markerSha256") != marker or incoming.get("botId") != bot["botId"]
                or incoming.get("senderId") != receipt["senderId"] or incoming.get("runSha256") != run_id
                or incoming.get("state") != "delivered"
                or any(type(incoming.get(key)) is not int or incoming[key] <= 0
                       for key in ("inboundMessageId", "replyMessageId"))
                or incoming["inboundMessageId"] == incoming["replyMessageId"]):
            raise ValueError("The recorded Telegram message does not match the desktop receipt")
    elif case == "telegram_second_denied":
        mode = proof.get("observationMode")
        if (mode not in {"real","injected"} or proof.get("denied") is not True
                or type(proof.get("dispatchCount")) is not int or proof["dispatchCount"] != 0
                or mode == "real" and proof.get("transport") != "telegram_bot_api"
                or mode == "injected" and proof.get("transport") not in {"fixture", "injected_update"}):
            raise ValueError("Pi requires the separately classified denied second sender with zero dispatches")
    elif case == "real_model":
        model = pinned({"path":spec["modelConfigFile"],"sha256":spec["modelConfigSha256"]})
        raw_origin = proof.get("modelServiceOrigin")
        origin = urlsplit(raw_origin) if isinstance(raw_origin, str) else None
        if (proof.get("syntheticModel") is not False or proof.get("modelConfigSha256") != spec["modelConfigSha256"]
                or proof.get("execution") != "tool_free_completion" or type(proof.get("toolCount")) is not int
                or proof["toolCount"] != 0 or proof.get("model") != model["provider"] + "/" + model["id"]
                or not DIGEST.fullmatch(str(proof.get("challengeSha256", "")))
                or not origin or origin.scheme != "https" or not origin.hostname or origin.username or origin.password
                or raw_origin != "https://" + origin.netloc):
            raise ValueError("Pi real_model observation requires its configured model and fresh tool-free challenge")


def check_observed_web_subjects(observed, spec):
    subjects = {hashlib.sha256(subject.encode()).hexdigest() for subject in spec["subjects"]}
    if set(observed.values()) != subjects or set(observed) != {"workspace_first","workspace_second"}:
        raise ValueError("The two Workspace observations must cover both reviewed Pi subjects")


def checked_profile(path, environment, host_profile, units, remote_root):
    path = private_file(path)
    reference = read(private_file(environment)).get("piPilot")
    if reference != {"path": str(path), "sha256": file_digest(path)}:
        raise ValueError("Environment must pin the private Pi profile")
    spec = read(path)
    fields = {"schema", "instanceId", "routeRef", "displayName", "adapterVersion", "enginePython", "runtimeDir",
              "listenPort", "remotePort", "subjects", "serviceTokenFile", "modelConfigFile", "modelConfigSha256",
              "macHostSha256", "centralMcpUrl", "trustedCardOrigins", "tunnelIdentityFile", "tunnelPublicKey",
              "nodePath", "nodeSha256", "sdkDir", "sdkTreeSha256", "sdkLockSha256", "contractReceipt", "requiredLevel"}
    if (set(spec) - {"telegram", "modelsFileSha256", "routeTransition"} != fields or spec["schema"] != "agentbridge.pi-mac-pilot.v1"
            or spec["adapterVersion"] != ADAPTER or spec["requiredLevel"] not in LEVELS):
        raise ValueError("Unexpected Pi pilot profile or version")
    for name in ("instanceId", "routeRef"):
        if not shared.ALIAS.fullmatch(spec[name]) or spec[name] == "openclaw-default":
            raise ValueError("Invalid Pi pilot identifier")
    label = spec["displayName"]
    if not isinstance(label, str) or label != label.strip() or not 0 < len(label) <= 100 or not label.isprintable():
        raise ValueError("Invalid Pi host display name")
    subjects = spec["subjects"]
    if (not isinstance(subjects, dict) or len(subjects) != 2
            or any(not s.strip() or not s.isprintable() or len(s) > 256 for s in subjects)
            or any(not isinstance(v, str) or not shared.ALIAS.fullmatch(v) for v in subjects.values())
            or len(set(subjects.values())) != 2):
        raise ValueError("Pi requires two web subjects with dedicated credentials")
    root = external(spec["runtimeDir"])
    if (root != Path(spec["runtimeDir"]) or not root.is_dir()
            or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise ValueError("Pi requires its private runtime directory")
    engine = Path(spec["enginePython"])
    if (not engine.is_absolute() or not engine.parent.parent.resolve().is_relative_to(root)
            or not (engine.parent.parent / "pyvenv.cfg").is_file() or not os.access(engine, os.X_OK)):
        raise ValueError("Expected a dedicated Pi Python environment")
    if not host_profile or file_digest(private_file(host_profile)) != spec["macHostSha256"]:
        raise ValueError("Pi host profile is not bound to this Mac")
    mac = read(private_file(host_profile))
    for key in ("listenPort", "remotePort"):
        if type(spec[key]) is not int or not 1024 <= spec[key] <= 65535:
            raise ValueError("Invalid Pi port")
    if spec["listenPort"] == mac["port"] or spec["remotePort"] == mac["tunnel"]["remotePort"]:
        raise ValueError("Pi and OpenClaw must own distinct listener ports")
    tunnel_settings(spec)
    key = private_file(spec["tunnelIdentityFile"])
    if (key.parent != root / "tunnel" or key.samefile(mac["tunnel"]["identityFile"])
            or run(["/usr/bin/ssh-keygen", "-y", "-P", "", "-f", key]).split()[:2] != spec["tunnelPublicKey"].split()):
        raise ValueError("Pi requires its dedicated SSH key")
    mcp = urlsplit(spec["centralMcpUrl"])
    if (mcp.scheme != "https" or mcp.hostname != mac["tunnel"]["host"] or mcp.path != "/mcp"
            or mcp.username or mcp.password or mcp.query or mcp.fragment):
        raise ValueError("Unreviewed Pi central MCP origin")
    origins = spec["trustedCardOrigins"]
    if not isinstance(origins, list) or not origins:
        raise ValueError("Expected reviewed Pi card origins")
    for raw in origins:
        origin = urlsplit(raw)
        if (origin.scheme != "https" or origin.hostname != mcp.hostname or origin.username or origin.password
                or raw != "https://" + origin.netloc):
            raise ValueError("Unreviewed Pi private card origin")
    service = private_file(spec["serviceTokenFile"])
    model_path = private_file(spec["modelConfigFile"])
    if (not service.is_relative_to(root) or not model_path.is_relative_to(root)
            or service.samefile(model_path) or file_digest(model_path) != spec["modelConfigSha256"]):
        raise ValueError("Pi service and model configuration must be separately pinned to this host")
    model = read(model_path)
    if (set(model) - {"modelsFile"} != {"provider", "id", "authFile"}
            or any(not isinstance(model[k], str) or not model[k].strip() for k in ["provider","id"])
            or ("modelsFile" in model) != ("modelsFileSha256" in spec)):
        raise ValueError("Unexpected Pi model authorization configuration")
    for name in ("authFile", "modelsFile"):
        if name in model:
            secret = private_file(model[name])
            if not secret.is_relative_to(root) or secret.samefile(service) or secret.samefile(model_path):
                raise ValueError("Pi model material must belong to its host")
    if "modelsFile" in model and file_digest(model["modelsFile"]) != spec["modelsFileSha256"]:
        raise ValueError("Pi model definitions changed")
    if "telegram" in spec:
        bot = spec["telegram"]
        ref = bot["botCredential"]
        allowed = bot["allowedSenders"]
        if (set(bot) != {"botId","username","botCredential","allowedSenders"} or type(bot["botId"]) is not int
                or not 0 < bot["botId"] < 2**53 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", bot["username"])
                or set(ref) != {"kind","service","account"} or ref["kind"] != "macos-keychain"
                or not all(isinstance(ref[k], str) and ref[k].strip() for k in ("service","account"))
                or not isinstance(allowed, dict) or len(allowed) != 1 or not set(allowed) <= subjects.keys()
                or any(not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", v) or int(v) >= 2**53
                       for v in allowed.values())):
            raise ValueError("Pi Telegram requires its own bot and exactly one reviewed private sender")
    expected = "Environment=AGENTBRIDGE_PI_ROUTES_FILE=" + remote_root + "/config/pi-" + spec["instanceId"] + "/routes.json"
    if [line for line in units["agentbridge.service"].decode().splitlines() if "AGENTBRIDGE_PI_ROUTES_FILE" in line] != [expected]:
        raise ValueError("The central unit must pin exactly this Pi route")
    if "routeTransition" in spec:
        transition = spec["routeTransition"]
        transition_routes(Path(remote_root)/"config"/("pi-"+spec["instanceId"]),
                          remote_metadata(spec),transition.get("toRelease") if isinstance(transition,dict) else None)
    verify_runtime(spec)
    return spec


def check_coexistence(spec, nano):
    if not nano:
        return
    for key in ("instanceId","routeRef","listenPort","remotePort","tunnelPublicKey"):
        if spec[key] == nano[key]:
            raise ValueError("Pi and NanoBot share a protected deployment resource: " + key)
    pi_root, nano_root = (external(s["runtimeDir"]) for s in (spec,nano))
    if pi_root.is_relative_to(nano_root) or nano_root.is_relative_to(pi_root):
        raise ValueError("Pi requires a separate runtime from NanoBot")
    pi_secrets = [private_file(spec["serviceTokenFile"]), private_file(read(private_file(spec["modelConfigFile"]))["authFile"])]
    nano_secrets = [private_file(nano["serviceTokenFile"]), private_file(nano["modelOAuthTokenFile"])]
    if any(a.samefile(b) or file_digest(a) == file_digest(b) for a in pi_secrets for b in nano_secrets):
        raise ValueError("Pi and NanoBot share private service or model credentials")
    if spec.get("telegram") and nano.get("telegram"):
        a, b = spec["telegram"], nano["telegram"]
        if a["botId"] == b["botId"] or a["username"].casefold() == b["username"].casefold() or a["botCredential"] == b["botCredential"]:
            raise ValueError("Pi requires its own independent Telegram bot")


def profile_for(pub):
    ref = read(external(pub.args.profile)).get("piPilot")
    requested = getattr(pub.args, "pi_profile", None)
    if not ref and not requested:
        return None
    if not isinstance(ref, dict) or requested and requested != ref.get("path"):
        raise ValueError("Pi profile differs from the reviewed environment")
    spec = checked_profile(ref["path"], pub.args.profile, pub.args.host_profile, pub.units, pub.args.remote_root)
    check_coexistence(spec, getattr(pub, "nanobot", None))
    return spec


def conformance_evidence(report):
    keys = ("implementation","adapterVersion","sdkVersion","sdkTreeSha256","sdkLockSha256","nodeSha256",
            "engine","adapterSourcesSha256","level","cases","probes")
    values = {key:report.get(key) for key in keys}
    return "sha256:"+hashlib.sha256(json.dumps(values,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def expected_archive_probe(report):
    """Candidate packaging has separate evidence from the reusable version-level contract."""
    keys = ("commit","wheelSha256","sdkVersion","adapterVersion","sdkTreeSha256","sdkLockSha256","nodeSha256","engine")
    return {"schema":"agentbridge.pi-archive-probe.v1", **{key:report[key] for key in keys},
            "probeSourceSha256":file_digest(ROOT/"tests/support/pi_archive_probe.py"),
            "measurements":{"worker":"pi_agentbridge.server","launch":"native_worker_program_arguments",
                            "runtime":"real_pi_sdk","central":"loopback_mcp","model":"synthetic_loopback",
                            "downstream":"synthetic_oa","testedLevel":"L1","personalIdentities":2,
                            "completedRuns":2,"authorizedReads":1,"deniedSubjectReads":0,
                            "succeededOperations":1,"businessWrites":0}}


def archive_probe_matches(report):
    expected = json.dumps(expected_archive_probe(report),sort_keys=True)
    return json.dumps(report.get("archiveProbe"),sort_keys=True) == expected


def reviewed_contract(pub, spec, artifact=None):
    from agentbridge.core.host_contract import HostContractStore
    from pi_qualification import CONTRACT, PRODUCT, checked_report, contract_measurement, product_measurement, required_cases

    if "routeTransition" in spec and spec["routeTransition"]["toRelease"] != pub.release:
        raise ValueError("The Pi route transition belongs to a different candidate")
    receipt = pinned(spec["contractReceipt"])
    expected = {"schema":"agentbridge.pi-qualification.v1","implementation":"pi","adapterVersion":ADAPTER,
                "sdkVersion":SDK_VERSION,"commit":pub.commit,"level":spec["requiredLevel"],
                "sdkTreeSha256":spec["sdkTreeSha256"],"sdkLockSha256":spec["sdkLockSha256"],
                "nodeSha256":spec["nodeSha256"],"modelConfigSha256":spec["modelConfigSha256"]}
    if not all(receipt.get(k) == v for k,v in expected.items()) or not DIGEST.fullmatch(str(receipt.get("wheelSha256", ""))) or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(receipt.get("contractEvidence"))):
        raise ValueError("Pi qualification differs from the candidate or reviewed runtimes")
    if artifact and receipt["wheelSha256"] != artifact["sha256"]:
        raise ValueError("Pi qualification tested a different central wheel")
    candidate = {"commit":pub.commit,"wheelSha256":receipt["wheelSha256"]}
    report = pinned(receipt.get("conformance"))
    checked_report(report,candidate,spec)
    if report["contractEvidence"] != receipt["contractEvidence"]:
        raise ValueError("Qualification refers to another Pi contract")
    cases = required_cases(spec)
    observations = receipt.get("observations", {})
    seen_markers, seen_runs, observed_web = set(), set(), {}
    for case in cases:
        proof = pinned(observations.get(case))
        if (proof.get("schema") != "agentbridge.pi-observation.v1" or proof.get("case") != case
                or proof.get("passed") is not True or proof.get("implementation") != "pi"
                or proof.get("commit") != pub.commit or proof.get("wheelSha256") != receipt["wheelSha256"]
                or proof.get("sdkVersion") != SDK_VERSION or proof.get("adapterVersion") != ADAPTER):
            raise ValueError("Pi qualification observation is missing or mismatched: " + case)
        verify_channel_case(case,proof,spec)
        if case in {"workspace_first","workspace_second"}:
            observed_web[case] = proof["subjectSha256"]
        if case in CONTRACT:
            measured = contract_measurement(case,report)
        elif case in PRODUCT or (case == "telegram_second_denied" and proof.get("observationMode") == "injected"):
            measured = product_measurement(case)
        else:
            measured = {}
        if any(proof.get(key)!=value for key,value in measured.items()):
            raise ValueError("Pi qualification measured evidence mismatch for "+case)
        if case in {"workspace_first","workspace_second","telegram_first"}:
            marker, run_id = proof["markerSha256"], proof["runSha256"]
            if marker in seen_markers or run_id in seen_runs:
                raise ValueError("Each client and account requires its own observed Pi run")
            seen_markers.add(marker)
            seen_runs.add(run_id)
    check_observed_web_subjects(observed_web,spec)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry.sqlite3"
        HostContractStore(path)
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT accepted_level,evidence_digest FROM agent_host_compatibility "
                             "WHERE implementation_name='pi' AND implementation_version=?", (ADAPTER,)).fetchone()
    level = spec["requiredLevel"]
    approved = level == "L1" or bool(row and row[0] == level and row[1] == receipt["contractEvidence"])
    if not approved and row and row[0] in LEVELS and LEVELS.index(row[0]) > LEVELS.index(level):
        higher = pinned(receipt.get("higherLevelConformance"))
        same_candidate = {"schema":"agentbridge.pi-conformance.v1","implementation":"pi","adapterVersion":ADAPTER,
                          "sdkVersion":SDK_VERSION,"commit":pub.commit,"wheelSha256":receipt["wheelSha256"],
                          "level":row[0],"engine":engine_identity(spec["enginePython"]),
                          **{k:spec[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256")}}
        approved = (all(higher.get(k) == v for k,v in same_candidate.items())
                    and all(higher.get(flag) is True for flag in ("passed","sourceStable","runtimeStable","formal"))
                    and archive_probe_matches(higher)
                    and higher.get("contractEvidence") == row[1] == conformance_evidence(higher))
    return {"approved":approved,"required":level,"evidence":spec["contractReceipt"]["sha256"]}


def verify_conformance(pub, spec, artifact):
    """Reproduce candidate qualification before any production mutation."""
    qualification = pinned(spec["contractReceipt"])
    expected = {"schema":"agentbridge.pi-conformance.v1","implementation":"pi","adapterVersion":ADAPTER,
                "sdkVersion":SDK_VERSION,"level":spec["requiredLevel"],"formal":True,"passed":True,"sourceStable":True,"runtimeStable":True,
                "commit":pub.commit,"wheelSha256":artifact["sha256"],"contractEvidence":qualification.get("contractEvidence"),
                "engine":engine_identity(spec["enginePython"]),
                **{k:spec[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256")}}
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", str(expected["contractEvidence"])):
        raise ValueError("Missing the reviewed Pi contract digest")
    receipt_dir = shared.owned_directory(Path(spec["runtimeDir"]) / "qualifications")
    receipt_file = receipt_dir / (pub.commit + "-" + spec["requiredLevel"] + ".json")
    def matches(report):
        if not all(report.get(k) == value for k,value in expected.items()):
            return False
        return archive_probe_matches(report) and conformance_evidence(report) == expected["contractEvidence"]
    if receipt_file.exists() and matches(read(private_file(receipt_file))):
        return
    with tempfile.TemporaryDirectory(prefix="pi-release-conformance-") as directory:
        output = Path(directory) / "pi.json"
        run([sys.executable, ROOT / "scripts/run_pi_conformance.py", "--formal", "--level", spec["requiredLevel"],
             "--node", spec["nodePath"], "--sdk-dir", spec["sdkDir"], "--engine-python", spec["enginePython"], "--output", output], timeout=1100)
        report = read(output)
    if not matches(report):
        raise ValueError("Fresh Pi conformance differs from reviewed candidate evidence")
    save(receipt_file, report)


def remote_metadata(spec):
    public = {**shared.metadata(spec), "hostType":"pi"}
    if spec.get("telegram"):
        public["telegram"]["allowedSenders"] = spec["telegram"]["allowedSenders"]
    if "routeTransition" in spec:
        public["routeTransition"] = spec["routeTransition"]
    return public


def remote(pub, spec, action, **extra):
    public = remote_metadata(spec)
    if "routeTransition" in spec:
        if spec["routeTransition"]["toRelease"] != pub.release:
            raise ValueError("The Pi route transition belongs to a different candidate")
        extra["candidate"] = pub.release
    return shared.remote_request(pub,public,"bind" if action=="provision" else action,**extra)


def observability(pub, spec, *, offline):
    try:
        compatibility = reviewed_contract(pub, spec)
    except (OSError, ValueError, KeyError, TypeError):
        compatibility = {"approved":False,"required":spec["requiredLevel"]}
    return {"instance":spec["instanceId"],"adapterVersion":ADAPTER,"subjects":len(spec["subjects"]),
            "compatibility":compatibility, "remote":None if offline else remote(pub, spec, "observe")}
