"""Stage and switch one independently owned Pi host within the native publication transaction."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import tomllib

from common import ROOT, private_file, read, run, save, sha
import nanobot_release as lifecycle


def archive(pub, spec, artifact):
    root = lifecycle.owned_directory(spec["runtimeDir"])
    releases = lifecycle.owned_directory(root / "releases")
    if not re.fullmatch(r"[a-f0-9]{40}", pub.commit) or pub.release != pub.commit[:12]:
        raise ValueError("Exact Pi release commit required")
    target = releases / pub.release
    sources = ["integrations/pi-agentbridge/pi_agentbridge", "integrations/nanobot-agentbridge/nanobot_agentbridge",
               "integrations/reference-host/reference_host"]
    version = tomllib.loads(run(["git", "show", pub.commit + ":integrations/pi-agentbridge/pyproject.toml"]))
    if version["project"]["version"] != spec["adapterVersion"]:
        raise ValueError("Pi adapter package differs from its reviewed version")
    files = {}
    for name in sources:
        for tracked in run(["git", "ls-tree", "-r", "--name-only", pub.commit, "--", name]).splitlines():
            rel = Path(tracked).relative_to(name)
            if name == sources[2] and rel.parts[0] == "static":
                continue
            if len(rel.parts) != 1 or (rel.suffix != ".py" and not (name == sources[0] and rel.name == "worker.mjs")):
                raise ValueError("Unexpected host archive entry")
            files[Path(name).name + "/" + rel.name] = subprocess.check_output(["git", "show", pub.commit + ":" + tracked], cwd=ROOT)
    wheel = Path(artifact["wheel"]).read_bytes()
    if sha(wheel) != artifact["sha256"] or "pi_agentbridge/worker.mjs" not in files:
        raise ValueError("Missing fixed Pi SDK wrapper or incorrect central wheel")
    files["agentbridge.whl"] = wheel
    program = ("import sys;sys.path[:0]=sys.argv[1:3];sys.argv=[sys.argv[0],'--host-type','pi'];"
               "from reference_host.bound_coordinator import main;main()")
    files["coordinator"] = ("#!/bin/sh\nexec " + shlex.join([spec["enginePython"], "-I", "-B", "-c", program,
                              str(target), str(target / "agentbridge.whl")]) + "\n").encode()
    if target.is_symlink():
        raise ValueError("Unexpected Pi archive symlink")
    if target.exists():
        allowed = set(files) | {"host.json", "telegram.json"}
        for entry in target.rglob("*"):
            rel = entry.relative_to(target).as_posix()
            if entry.is_symlink() or (entry.is_dir() and rel not in {Path(s).name for s in sources}
                                      or not entry.is_dir() and rel not in allowed):
                raise ValueError("Unexpected existing Pi archive member")
        for name, expected in files.items():
            if (target / name).read_bytes() != expected:
                raise ValueError("Existing Pi archive differs from the selected candidate")
        return target
    with tempfile.TemporaryDirectory(dir=releases) as directory:
        stage = Path(directory) / "release"
        stage.mkdir(mode=0o700)
        for name, contents in files.items():
            path = stage / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(contents)
            path.chmod(0o700 if name == "coordinator" else 0o600)
        stage.rename(target)
    return target


def configure(spec, mac, root, release, records):
    if not isinstance(records, list) or len(records) != 2:
        raise ValueError("Pi requires exactly two issued personal identities")
    approved = spec["subjects"]
    observed = {}
    for entry in records:
        identity = entry["binding"]
        subject, ref = identity["user_subject"], identity["credential_ref"]
        if (subject in observed or approved.get(subject) != ref or identity["instance_id"] != spec["instanceId"]
                or not identity["endpoint_key"].startswith("workspace:" + spec["instanceId"] + ":")):
            raise ValueError("Unexpected issued Pi principal or route")
        observed[subject] = ref
    if observed != approved:
        raise ValueError("Pi issuance differs from the approved two subjects")
    bindings = lifecycle.ensure_local_bindings(root, records)
    for name, payload in configuration_files(spec, mac, root, release, bindings).items():
        path = release / name
        if os.path.lexists(path):
            if read(private_file(path)) != payload:
                raise ValueError("Existing Pi release configuration differs")
        else:
            save(path, payload)


def configuration_files(spec, mac, root, release, bindings):
    bot = spec.get("telegram")
    config = {"instanceId": spec["instanceId"], "listenPort": spec["listenPort"], "serviceTokenFile": spec["serviceTokenFile"],
              "stateDir": str(root / "state"), "sdkDir": spec["sdkDir"], "nodePath": spec["nodePath"],
              "model": read(private_file(spec["modelConfigFile"])), "bindings": bindings, "mcpUrl": spec["centralMcpUrl"],
              "caFile": mac["caCertificate"], "requiredLevel": spec["requiredLevel"],
              "trustedCardOrigins": spec["trustedCardOrigins"] if spec["requiredLevel"] in ("L2", "L3") else []}
    if spec["requiredLevel"] == "L3":
        config["coordinatorProgram"] = str(release / "coordinator")
    files = {"host.json": config}
    if bot:
        config["telegramPilot"] = {k: bot[k] for k in ("botId", "username", "allowedSenders")}
        files["telegram.json"] = {
            "hostType": "pi", "enabled": True, "preparationOnly": False, "adapterVersion": spec["adapterVersion"],
            "botId": bot["botId"], "botUsername": bot["username"], "allowedSenders": bot["allowedSenders"],
            "botCredential": bot["botCredential"], "hostUrl": f"http://127.0.0.1:{spec['listenPort']}",
            "instanceId": spec["instanceId"], "hostTokenFile": spec["serviceTokenFile"],
            "dataDir": str(root / "telegram"), "allowWorkspacePairing": True, "bindings": {}}
    elif os.path.lexists(release / "telegram.json"):
        raise ValueError("A Telegram-enabled Pi archive cannot be reused for a web-only release")
    return files


def verify_active_configuration(spec, mac, root, release):
    bindings = read(private_file(release / "host.json"))["bindings"]
    if len(bindings) != 2 or {r["user_subject"]: r["credential_ref"] for r in bindings} != spec["subjects"]:
        raise ValueError("Active Pi bindings differ from the reviewed subjects")
    for item in bindings:
        expected = root / "credentials" / (item["credential_ref"] + ".token")
        if private_file(item["tokenFile"]) != expected:
            raise ValueError("Active Pi personal credential path differs")
    for name, expected in configuration_files(spec, mac, root, release, bindings).items():
        path = release / name
        if not path.is_file() or read(private_file(path)) != expected:
            raise ValueError("Active Pi configuration differs from the confirmed release")


def verify_subjects(spec, release):
    receipt = json.loads(run([spec["enginePython"], "-I", "-B", ROOT / "scripts/native/pi_identity_probe.py",
                              release, spec["requiredLevel"]], timeout=120))
    if receipt != {"authenticatedSubjects": 2, "acceptedLevel": spec["requiredLevel"]}:
        raise ValueError("Pi central identity verification incomplete")
    return receipt


def ready(pub, spec, remote, *, action="probe"):
    for attempt in range(15):
        try:
            return remote(pub, spec, action)
        except RuntimeError:
            if attempt == 14:
                raise
            time.sleep(1)


def current_target(root):
    pointer = root / "current"
    if not os.path.lexists(pointer):
        return None
    if not pointer.is_symlink():
        raise ValueError("Unexpected Pi release pointer")
    target = pointer.resolve(strict=True)
    if target.parent != root / "releases" or not re.fullmatch(r"[a-f0-9]{12}", target.name):
        raise ValueError("Pi pointer outside owned releases")
    return target


def job_sets(spec, mac):
    desired = lifecycle.jobs(spec, mac, host_type="pi")
    transition = spec.get("routeTransition")
    if transition:
        predecessor = lifecycle.jobs({**spec, "telegram": transition["fromTelegram"]}, mac, host_type="pi")
        definitions = {**predecessor, **desired}
    else:
        definitions = desired
    return desired, lifecycle.managed_jobs(definitions)


def check_predecessor(pub, spec, old, last):
    change = spec.get("routeTransition")
    if not change:
        return
    if not old or old.name != change["fromRelease"] or pub.release != change["toRelease"]:
        raise ValueError("Pi route transition requires its reviewed predecessor and candidate")
    confirmed = last.get("stage") == "confirmed" and last.get("candidate") == old.name
    reverted = (last.get("stage") == "rolled_back" and last.get("candidate") == pub.release
                and last.get("previous") == old.name and last.get("routeTransition") == change)
    if not confirmed and not reverted:
        raise ValueError("Pi predecessor has no matching confirmed or restored transaction")
    host = read(private_file(old / "host.json"))
    if host["requiredLevel"] != change["fromLevel"] or host.get("telegramPilot") != change["fromTelegram"]:
        raise ValueError("Previous Pi level or private channel differs from the approved transition")


def drain(pub, spec, definitions, remote):
    change = spec.get("routeTransition")
    if change and change["fromTelegram"] and not spec.get("telegram"):
        # Include the old receiver's lock while keeping the remote observation
        # on the reviewed incoming profile.
        watch = {**spec, "telegram": change["fromTelegram"]}
        lifecycle.drain_jobs(pub, watch, definitions, observe_remote=lambda p, _, action: remote(p, spec, action))
    else:
        lifecycle.drain_jobs(pub, spec, definitions, observe_remote=remote)


def admission(pub, spec, mac, artifact, *, remote):
    with lifecycle.pilot_lock(spec):
        root = Path(spec["runtimeDir"])
        state = root / "native-release.json"
        last = read(private_file(state)) if state.exists() else {}
        release = archive(pub, spec, artifact)
        old = current_target(root)
        desired, definitions = job_sets(spec, mac)
        if last.get("stage") in {"switching", "restoring"}:
            raise ValueError("Interrupted Pi switch requires recorded recovery")
        if old == release or pub.args.resume:
            if (old != release or last.get("stage") != "confirmed" or last.get("candidate") != pub.release
                    or set(desired) != {label for label, item in definitions.items() if item[2]}):
                raise ValueError("Pi resume requires the confirmed candidate and original jobs")
            verify_active_configuration(spec, mac, root, release)
            lifecycle.wait_for_running_jobs(desired, spec)
            prior = remote(pub, spec, "observe")
            if not prior["scopePresent"] or not prior["tunnelAuthorized"]:
                raise ValueError("Active Pi scope or forwarding identity differs from the confirmed release")
            return {**ready(pub, spec, remote), **verify_subjects(spec, release)}
        check_predecessor(pub, spec, old, last)
        prior = remote(pub, spec, "observe")
        recovery = lifecycle.retry_proof(pub, spec, root, old, last, definitions, prior, recovery_remote=remote)
        if (not recovery and bool(old) != bool(prior["scopePresent"])
                or not old and any(v[2] for v in definitions.values())):
            raise ValueError("Pi local release and central ownership disagree")
        record = {"candidate": pub.release, "previous": old.name if old else None, "stage": "switching",
                  "previousScopePresent": False if recovery else prior["scopePresent"], "previousTunnelAuthorized": prior["tunnelAuthorized"],
                  "jobs": {label: {"existed": path.exists(), "loaded": loaded} for label, (path, _, loaded) in definitions.items()}}
        if spec.get("routeTransition"):
            record["routeTransition"] = spec["routeTransition"]
        if recovery:
            record["recovery"] = recovery
        save(state, record)
        domain = f"gui/{os.getuid()}"
        try:
            issued = remote(pub, spec, "provision", **({"recovery": recovery} if recovery else {}))
            configure(spec, mac, root, release, issued["bindings"])
            remote(pub, spec, "tunnel_enable")
            drain(pub, spec, definitions, remote)
            if record.get("routeTransition"):
                remote(pub, spec, "route_activate")
            identities = verify_subjects(spec, release)
            lifecycle.set_current(root, release)
            for label, (path, body, _) in definitions.items():
                if label not in desired:
                    lifecycle.ctl("disable", domain + "/" + label)
                    continue
                if label.endswith(".telegram"):
                    ready(pub, spec, remote)
                if not path.exists():
                    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
                        f.write(body)
                lifecycle.ctl("enable", domain + "/" + label)
                lifecycle.ctl("bootstrap", domain, str(path))
            result = ready(pub, spec, remote)
            lifecycle.wait_for_running_jobs(desired, spec)
            record["stage"] = "confirmed"
            save(state, record)
            return {**result, **identities}
        except Exception:
            _restore(pub, spec, mac, remote, record)
            raise


def restore(pub, spec, mac, *, remote, record=None):
    with lifecycle.pilot_lock(spec):
        return _restore(pub, spec, mac, remote, record)


def _restore(pub, spec, mac, remote, record):
    root = Path(spec["runtimeDir"])
    state = root / "native-release.json"
    record = record or read(private_file(state))
    if record.get("candidate") != pub.release or record.get("stage") not in {"switching", "confirmed", "restoring"}:
        raise ValueError("Pi rollback does not match the recorded candidate")
    old = record["previous"]
    if old and not re.fullmatch(r"[a-f0-9]{12}", old):
        raise ValueError("Invalid previous Pi release")
    prev = root / "releases" / old if old else None
    if prev and (prev.resolve() != prev or not (prev / "host.json").is_file()):
        raise ValueError("Previous Pi archive unavailable")
    if current_target(root) not in {prev, root / "releases" / pub.release}:
        raise ValueError("Pi pointer changed outside the release transaction")
    if record.get("routeTransition") != spec.get("routeTransition"):
        raise ValueError("Pi route restoration differs from its recorded configuration")
    _, definitions = job_sets(spec, mac)
    if set(definitions) != set(record["jobs"]):
        raise ValueError("Pi job set differs from the recorded transaction")
    record["stage"] = "restoring"
    save(state, record)
    drain(pub, spec, definitions, remote)
    change = record.get("routeTransition")
    if change:
        remote(pub, spec, "route_restore")
    probe = "probe_predecessor" if change else "probe"
    domain = f"gui/{os.getuid()}"
    for label, (path, _, _) in reversed(list(definitions.items())):
        if not record["jobs"][label]["existed"] and path.exists():
            lifecycle.ctl("disable", domain + "/" + label)
            path.unlink()
    lifecycle.set_current(root, prev)
    for label, (path, _, _) in definitions.items():
        if record["jobs"][label]["loaded"]:
            if label.endswith(".telegram"):
                ready(pub, spec, remote, action=probe)
            lifecycle.ctl("enable", domain + "/" + label)
            lifecycle.ctl("bootstrap", domain, str(path))
    if not record["previousTunnelAuthorized"]:
        remote(pub, spec, "tunnel_disable")
    if record.get("recovery"):
        result = remote(pub, spec, "rollback_scope", recovery=record["recovery"])
        if not result["reverted"]:
            record["unmodifiedSource"] = record["recovery"]["sourceCandidate"]
    elif not record["previousScopePresent"]:
        remote(pub, spec, "rollback_scope")
    else:
        ready(pub, spec, remote, action=probe)
    loaded = [label for label in definitions if record["jobs"][label]["loaded"]]
    if loaded:
        lifecycle.wait_for_running_jobs(loaded, spec)
    record["stage"] = "rolled_back"
    save(state, record)
    return {"status": "pi_host_rolled_back", "previous": old}
