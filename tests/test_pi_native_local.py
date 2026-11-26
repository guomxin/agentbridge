"""Pi deployment switch tests use a temporary release tree and a simulated launchd."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_nanobot_native_publish import private
import pi_local as local


def profile(tmp_path):
    root = tmp_path.resolve()
    model = private(root / "model.json", '{"provider":"synthetic","id":"sample","authFile":"/synthetic/auth"}')
    release = root / "releases" / ("a" * 12)
    release.mkdir(parents=True, mode=0o700)
    spec = {"subjects": {"one": "pi-one", "two": "pi-two"}, "modelConfigFile": str(model),
            "serviceTokenFile": str(root / "service.token"), "centralMcpUrl": "https://synthetic.invalid/mcp",
            "trustedCardOrigins": ["https://synthetic.invalid"], "requiredLevel": "L1", "runtimeDir": str(root),
            "instanceId": "pi-office", "listenPort": 49179, "nodePath": "/synthetic/node",
            "sdkDir": "/synthetic/deps", "adapterVersion": "0.1.0"}
    records = [{"binding": {"instance_id": "pi-office", "user_subject": subject, "credential_ref": ref,
                "endpoint_key": "workspace:pi-office:" + subject, "endpoint_id": subject, "session_key": "s-" + subject},
                "personalCredential": "synthetic-secret-for-" + ref} for subject, ref in spec["subjects"].items()]
    return root, release, spec, {"caCertificate": "/synthetic/ca"}, records


@pytest.mark.parametrize("level", ["L1", "L2", "L3"])
def test_preflight_checks_both_subjects_before_writing_and_web_only_support(tmp_path, level):
    root, release, spec, mac, records = profile(tmp_path)
    spec["requiredLevel"] = level
    records[1]["binding"]["instance_id"] = "other"
    with pytest.raises(ValueError):
        local.configure(spec, mac, root, release, records)
    assert not (root / "credentials").exists()
    records[1]["binding"]["instance_id"] = "pi-office"
    local.configure(spec, mac, root, release, records)
    config = json.loads((release / "host.json").read_text())
    assert ("coordinatorProgram" in config) == (level == "L3")
    assert bool(config["trustedCardOrigins"]) == (level in ("L2", "L3"))
    assert "telegramPilot" not in config and not (release / "telegram.json").exists()
    assert [Path(item["tokenFile"]).stat().st_mode & 0o777 for item in config["bindings"]] == [0o600] * 2


@pytest.mark.parametrize("fail_worker", [True, False])
def test_transaction_switch_resume_and_restore(tmp_path, monkeypatch, fail_worker):
    root, release, spec, mac, records = profile(tmp_path)
    pub = SimpleNamespace(release=release.name, args=SimpleNamespace(resume=False))
    jobs = {f"ai.agentbridge.pi.pi-office.{name}": root / (name + ".plist") for name in ("worker", "tunnel")}
    loaded = set()
    remote_state = {"scopePresent": False, "tunnelAuthorized": False}
    actions = []

    def remote(_pub, _spec, action):
        actions.append(action)
        if action == "observe":
            return dict(remote_state)
        if action == "provision":
            remote_state["scopePresent"] = True
            return {"bindings": records}
        if action == "tunnel_enable" or action == "tunnel_disable":
            remote_state["tunnelAuthorized"] = action == "tunnel_enable"
        elif action == "rollback_scope":
            remote_state["scopePresent"] = False
        elif action == "probe":
            assert len(loaded) == 2
            return {"ready": True}
        else:
            raise AssertionError(action)

    def ctl(action, domain, *paths):
        if action == "bootstrap":
            name = next(name for name, path in jobs.items() if str(path) == paths[0])
            if fail_worker and name.endswith(".worker"):
                raise RuntimeError("simulated worker launch failure")
            loaded.add(name)

    lifecycle = local.lifecycle
    monkeypatch.setattr(local, "archive", lambda *_: release)
    monkeypatch.setattr(local, "verify_subjects", lambda *_: {"authenticatedSubjects": 2})
    monkeypatch.setattr(lifecycle, "jobs", lambda *_, **kw: jobs if kw["host_type"] == "pi" else {})
    monkeypatch.setattr(lifecycle, "managed_jobs", lambda *_: {k: (p, b"synthetic", k in loaded) for k,p in jobs.items()})
    monkeypatch.setattr(lifecycle, "ctl", ctl)
    monkeypatch.setattr(lifecycle, "drain_jobs", lambda *_, **kw: loaded.clear())
    monkeypatch.setattr(lifecycle, "wait_for_running_jobs", lambda labels, _: set(labels) == loaded or pytest.fail("Pi jobs missing"))

    if fail_worker:
        with pytest.raises(RuntimeError, match="launch failure"):
            local.admission(pub, spec, mac, {}, remote=remote)
    else:
        assert local.admission(pub, spec, mac, {}, remote=remote)["authenticatedSubjects"] == 2
        assert (root / "current").resolve() == release and len(loaded) == 2
        pub.args.resume = True
        assert local.admission(pub, spec, mac, {}, remote=remote)["ready"]
        assert actions.count("provision") == 1
        local.restore(pub, spec, mac, remote=remote)
    assert not (root / "current").is_symlink() and not any(p.exists() for p in jobs.values())
    assert not any(remote_state.values()) and not loaded
    assert json.loads((root / "native-release.json").read_text())["stage"] == "rolled_back"


@pytest.mark.parametrize("enable_telegram", [True, False, None])
@pytest.mark.parametrize("fail_auth", [False, True])
def test_reviewed_route_switch_and_rollback_recover_original_jobs(tmp_path, monkeypatch, enable_telegram, fail_auth):
    root, candidate, spec, mac, records = profile(tmp_path)
    predecessor = candidate.with_name("b" * 12)
    predecessor.mkdir(mode=0o700)
    bot = {"botId": 8801, "username": "pi_test_bot", "allowedSenders": {"one": "501"}, "botCredential": {"kind": "test"}}
    original = dict(spec)
    version_patch = enable_telegram is None
    if enable_telegram or version_patch:
        spec["telegram"] = bot
    if not enable_telegram:
        original["telegram"] = bot
    local.configure(original, mac, root, predecessor, records)
    old_channel = json.loads((predecessor / "host.json").read_text()).get("telegramPilot")
    spec["routeTransition"] = {"fromRelease": predecessor.name, "toRelease": candidate.name,
        "beforeSha256": "c" * 64, "afterSha256": "d" * 64, "fromLevel": "L1", "fromTelegram": old_channel}
    if version_patch:
        spec["routeTransition"].update(fromAdapterVersion="0.1.0", fromRevision=1)
        monkeypatch.setattr(local, "run", lambda cmd: '[project]\nversion="0.1.0"')
    local.lifecycle.set_current(root, predecessor)
    local.save(root / "native-release.json", {"stage": "confirmed", "candidate": predecessor.name})
    pub = SimpleNamespace(release=candidate.name, args=SimpleNamespace(resume=False))
    pid_prefix = "ai.agentbridge.pi.pi-office."

    def jobs(config, _, *, host_type):
        assert host_type == "pi"
        scopes = ["worker", "tunnel"] + (["telegram"] if config.get("telegram") else [])
        return {pid_prefix + k: root / (k + ".plist") for k in scopes}
    old_jobs = jobs(original, mac, host_type="pi")
    for p in old_jobs.values():
        p.write_bytes(b"fixture")
    loaded = set(old_jobs)
    route = ["predecessor"]
    actions = []

    def ctl(op, domain, *args):
        if op == "bootstrap":
            loaded.add(pid_prefix + Path(args[0]).stem)

    def remote(_pub, _spec, action):
        actions.append(action)
        if action == "observe":
            return {"scopePresent": True, "tunnelAuthorized": True}
        if action == "provision":
            return {"bindings": records}
        if action in {"route_activate", "route_restore"}:
            assert not loaded
            route[0] = "candidate" if action == "route_activate" else "predecessor"
            return {"routeState": route[0], "changed": True}
        if action in {"probe", "probe_predecessor"}:
            assert route[0] == ("candidate" if action == "probe" else "predecessor")
            return {"ready": True}
        assert action == "tunnel_enable"

    def authenticate(_spec, release):
        assert route[0] == "candidate" and release == candidate
        if fail_auth:
            raise RuntimeError("simulated account changed")
        return {"authenticatedSubjects": 2}
    lifecycle = local.lifecycle
    monkeypatch.setattr(local, "archive", lambda *_: candidate)
    monkeypatch.setattr(local, "verify_subjects", authenticate)
    monkeypatch.setattr(lifecycle, "jobs", jobs)
    monkeypatch.setattr(lifecycle, "managed_jobs", lambda jobs: {k: (p, b"fixture", k in loaded) for k,p in jobs.items()})
    monkeypatch.setattr(lifecycle, "ctl", ctl)
    monkeypatch.setattr(lifecycle, "drain_jobs", lambda *_, **kw: loaded.clear())
    monkeypatch.setattr(lifecycle, "wait_for_running_jobs", lambda labels, _: set(labels) == loaded or pytest.fail("wrong jobs"))
    if fail_auth:
        with pytest.raises(RuntimeError, match="account changed"):
            local.admission(pub, spec, mac, {}, remote=remote)
    else:
        assert local.admission(pub, spec, mac, {}, remote=remote)["ready"]
        assert (pid_prefix + "telegram" in loaded) == bool(enable_telegram or version_patch)
        local.restore(pub, spec, mac, remote=remote)
    assert route == ["predecessor"] and loaded == set(old_jobs)
    assert (root / "current").resolve() == predecessor
    assert actions.index("route_activate") < actions.index("route_restore") < actions.index("probe_predecessor")

    if version_patch:
        assert actions.index("route_activate") < actions.index("provision")


def test_version_patch_accepts_verified_archive_after_first_native_rollback(tmp_path, monkeypatch):
    root, candidate, spec, mac, records = profile(tmp_path)
    predecessor = candidate.with_name("b" * 12)
    predecessor.mkdir(mode=0o700)
    local.configure(spec, mac, root, predecessor, records)
    spec["routeTransition"] = {"fromRelease":predecessor.name,"toRelease":candidate.name,
        "fromAdapterVersion":"0.1.0","fromRevision":2,"fromLevel":spec["requiredLevel"],"fromTelegram":None}
    last = {"stage":"rolled_back","previous":None,"candidate":predecessor.name,"previousScopePresent":False}
    pub = SimpleNamespace(release=candidate.name)
    monkeypatch.setattr(local,"run",lambda cmd: '[project]\nversion="0.1.0"')
    local.check_predecessor(pub,spec,None,last)
    with pytest.raises(ValueError,match="reviewed predecessor"):
        local.check_predecessor(pub,spec,None,{**last,"previousScopePresent":True})
    monkeypatch.setattr(local,"run",lambda cmd: '[project]\nversion="0.1.9"')
    with pytest.raises(ValueError,match="adapter version"):
        local.check_predecessor(pub,spec,None,last)
