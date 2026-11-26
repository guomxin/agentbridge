"""Only temporary, synthetic release metadata, accounts and package installations."""
import json
from types import SimpleNamespace

import pytest

from tests.test_nanobot_native_publish import central, private, synthetic_admission
import pi_release as pi


def test_independent_instances_provision_and_restore_only_pi(tmp_path, monkeypatch):
    nano, args, ws, identities, originals, config = synthetic_admission(tmp_path, monkeypatch)
    monkeypatch.delenv("AGENTBRIDGE_PI_ROUTES_FILE", raising=False)
    nano_records = central.execute({**args, "action":"bind"})["bindings"]
    spec = {**args["spec"], "hostType":"pi", "instanceId":"pi-pilot", "routeRef":"pi-route",
            "adapterVersion":pi.ADAPTER, "remotePort":49382, "subjects":{"alice":"pi-a", "bob":"pi-b"},
            "requiredLevel":"L1", "telegram":{"botId":9912345,"username":"pi_test_bot","allowedSenders":{"alice":"501"}}}
    directory = tmp_path / "config" / "pi-pi-pilot"
    directory.mkdir(mode=0o700)
    data = {"routes":{"pi-route":{"url":"http://127.0.0.1:49382", "instanceId":"pi-pilot",
            "tokenFile":str(directory/"service.token"),"subjects":spec["subjects"],
            "adapterVersion":pi.ADAPTER,"telegram":spec["telegram"]}}}
    private(directory/"routes.json",json.dumps(data))
    request = {**args,"spec":spec}
    pi_records = central.execute({**request,"action":"bind"})["bindings"]
    assert central.execute({**request,"action":"bind"})["bindings"] == pi_records
    for item in pi_records:
        owner = item["binding"]["user_subject"]
        identity = identities.verify(item["personalCredential"])
        assert identity["user_subject"] == owner
        assert identity["label"] == f"workspace-pi:pi-pilot:{spec['subjects'][owner]}"
        assert ws.hosts.choices(owner)["default_instance_id"] == "openclaw-default"
    with pytest.raises(ValueError, match="conflicts"):
        central.execute({**request, "spec":{**spec,"instanceId":nano["instanceId"]}, "action":"bind"})
    # Simulate losing the SSH session after the scope closes, before the second personal token is revoked.
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    original_revoke = McpIdentityTokenStore.revoke
    revoked = []
    def once(store, token_id):
        if revoked:
            raise ConnectionError("synthetic disconnect")
        revoked.append(token_id)
        return original_revoke(store, token_id)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(McpIdentityTokenStore,"revoke",once)
        with pytest.raises(ConnectionError):
            central.execute({**request,"action":"rollback_scope"})
    assert len([item for item in pi_records if identities.verify(item["personalCredential"])]) == 1
    assert central.execute({**request,"action":"rollback_scope"})["closed"]
    assert all(identities.verify(item["personalCredential"]) is None for item in pi_records)
    assert all(identities.verify(item["personalCredential"]) for item in nano_records)
    assert all(identities.verify(secret) for secret in originals.values())


def test_candidate_qualification_requires_own_observations_and_verified_wheel(tmp_path, monkeypatch):
    import pi_qualification as qualify
    def pin(name, value):
        path = private(tmp_path / name, json.dumps(value))
        return {"path":str(path),"sha256":pi.file_digest(path)}
    pub = SimpleNamespace(commit="a"*40)
    model_file = pin("reviewed-model",{"provider":"test","id":"pi-fixture","authFile":"unit-test-only"})
    spec = {"requiredLevel":"L1","nodeSha256":"b"*64,"sdkTreeSha256":"c"*64,
            "sdkLockSha256":"d"*64,"modelConfigSha256":model_file["sha256"],"modelConfigFile":model_file["path"]}
    artifact = {"sha256":"e"*64}
    receipt = {"commit":pub.commit,"sdkVersion":pi.SDK_VERSION,"adapterVersion":pi.ADAPTER,
               "wheelSha256":artifact["sha256"],"implementation":"pi"}
    observations = {}
    for case in ("workspace_first","workspace_second","subject_isolation","real_model","telegram_first","telegram_second_denied"):
        observations[case] = pin(case,{**receipt,"schema":"agentbridge.pi-observation.v1","case":case,
                                      "passed":True,"syntheticModel":False,"modelConfigSha256":spec["modelConfigSha256"]})
    def extend(name, extra):
        observations[name] = pin(name, {**json.loads((tmp_path/name).read_text()), **extra})
    spec.update({"enginePython":"/synthetic-engine", "nodePath":"/synthetic-node", "sdkDir":"/synthetic-sdk"})
    spec.update({"instanceId":"pi-reviewed","subjects":{"one":"pi-a","two":"pi-b"}})
    monkeypatch.setattr(qualify, "engine_identity", lambda engine: {"fixture": True})
    vectors = json.loads((pi.ROOT/"schemas/agent-host/v1/test-vectors.json").read_text())["conformanceCases"]
    report = {"schema":"agentbridge.pi-conformance.v1", **receipt, "level":"L1",
              "engine":{"fixture":True},"adapterSourcesSha256":"1"*64,"clean":True,"formal":True,"sourceStable":True,
              "runtimeStable":True,"passed":True,"sourceSha256":"2"*64,
              **{k:spec[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256")},
              "cases":[{**v,"status":"passed" if v["level"]=="L1" else "outside_level"} for v in vectors],
              "probes":[{"probe":"sdk","passed":True,"cases":{"H02":"read","H18":"isolated"},"evidenceSha256":"3"*64},
                        {"probe":"fixture-central","passed":True,"cases":{"H01":"version","H03":"scope"},"evidenceSha256":"8"*64}]}
    report["contractEvidence"] = pi.conformance_evidence(report)
    report["archiveProbe"] = pi.expected_archive_probe(report)
    extend("subject_isolation", qualify.contract_measurement("subject_isolation",report))
    extend("real_model", {"execution":"tool_free_completion","model":"test/pi-fixture","toolCount":0,
                          "modelServiceOrigin":"https://fixture.invalid","challengeSha256":"6"*64})
    import hashlib
    markers, captures, run_ids = {}, {}, {}
    for index, case in enumerate(("workspace_first","workspace_second","telegram_first"), start=1):
        challenge = "PI-" + str(index) * 32
        markers[case] = hashlib.sha256(challenge.encode()).hexdigest()
        run_ids[case] = hashlib.sha256(("run-"+str(index)).encode()).hexdigest()
        captures[case] = pin(case+"-capture", {"text":challenge+" synthetic reply"})
        extend(case, {"client":"chrome_extension", "markerSha256":markers[case],
                      "runSha256":run_ids[case], "capture":captures[case],"instanceId":"pi-reviewed",
                      "subjectSha256":hashlib.sha256(("two" if case=="workspace_second" else "one").encode()).hexdigest()})
    marker = markers["telegram_first"]
    desktop = captures["telegram_first"]
    incoming_data = {"botId":88001,"senderId":"501","markerSha256":marker,"inboundMessageId":12,
                     "replyMessageId":13,"state":"delivered","runSha256":run_ids["telegram_first"]}
    incoming = pin("incoming",incoming_data)
    delivered = pin("desktop-and-inbox",{"schema":"agentbridge.pi-telegram-desktop-receipt.v1","botId":88001,
                                         "senderId":"501","markerSha256":marker,"desktopEvidence":desktop,"inboxEvidence":incoming})
    extend("telegram_first",{"client":"telegram_desktop","transport":"telegram_bot_api",
                             "receiptPath":delivered["path"],"receiptSha256":delivered["sha256"]})
    extend("telegram_second_denied",qualify.product_measurement("telegram_second_denied"))
    report_ref = pin("conformance.json",report)
    receipt.update({**spec, "schema":"agentbridge.pi-qualification.v1","level":"L1","observations":observations,
                    "contractEvidence":report["contractEvidence"],"conformance":report_ref})
    spec.update({"telegram":{"botId":88001,"allowedSenders":{"one":"501"}},"contractReceipt":pin("receipt",receipt)})
    assert pi.reviewed_contract(pub,spec,artifact)["approved"]
    for index, change in enumerate((
            None,
            {**report["archiveProbe"],"wheelSha256":"9"*64},
            {**report["archiveProbe"],"commit":"8"*40},
            {**report["archiveProbe"],"measurements":{**report["archiveProbe"]["measurements"],"authorizedReads":True}},
            {**report["archiveProbe"],"measurements":{**report["archiveProbe"]["measurements"],"completedRuns":1}},
            {**report["archiveProbe"],"measurements":{**report["archiveProbe"]["measurements"],"runtime":"source_tree"}})):
        other_report = {**report,"archiveProbe":change}
        assert pi.conformance_evidence(other_report) == report["contractEvidence"]
        copied = pin("archive-mismatch-"+str(index),other_report)
        bad = pin("archive-qualification-"+str(index),{**receipt,"conformance":copied})
        with pytest.raises(ValueError,match="archived worker"):
            pi.reviewed_contract(pub,{**spec,"contractReceipt":bad},artifact)
    def rejects(case, override, message):
        original = observations[case]
        payload = json.loads((tmp_path/case).read_text())
        observations[case] = pin("negative-case",{**payload,**override})
        spec["contractReceipt"] = pin("receipt",receipt)
        with pytest.raises(ValueError,match=message):
            pi.reviewed_contract(pub,spec,artifact)
        observations[case] = original
    rejects("workspace_first", {"client":"http_fixture"}, "Chrome extension")
    rejects("workspace_first", {"instanceId":"other"}, "reviewed instance")
    rejects("workspace_second", {"subjectSha256":hashlib.sha256(b"one").hexdigest()}, "wrong reviewed account")
    rejects("telegram_first", {"subjectSha256":hashlib.sha256(b"two").hexdigest()}, "wrong reviewed account")
    rejects("workspace_first", {"subjectSha256":hashlib.sha256(b"unbound").hexdigest()}, "reviewed instance")
    with pytest.raises(ValueError,match="cover both"):
        pi.check_observed_web_subjects(dict.fromkeys(("workspace_first","workspace_second"),hashlib.sha256(b"one").hexdigest()),spec)
    rejects("real_model", {"execution":"mock_reply"}, "real_model")
    rejects("real_model", {"model":"test/other"}, "real_model")
    rejects("real_model", {"toolCount":1}, "real_model")
    rejects("real_model", {"challengeSha256":""}, "real_model")
    rejects("real_model", {"modelServiceOrigin":"http://fixture.invalid"}, "real_model")
    rejects("subject_isolation", {"measurementSha256":"7"*64}, "measured evidence")
    rejects("telegram_second_denied", {"source":"assumed_blocked"}, "measured evidence")
    rejects("workspace_second", {"markerSha256":markers["workspace_first"]}, "acceptance message")
    rejects("workspace_second", {"runSha256":run_ids["workspace_first"]}, "own observed")
    rejects("telegram_first", {"transport":"fixture"}, "actual Bot API")
    rejects("telegram_first", {"receiptSha256":"0"*64}, "digest mismatch")
    rejects("telegram_second_denied", {"observationMode":"real"}, "zero dispatches")
    rejects("telegram_second_denied", {"transport":"telegram_bot_api"}, "zero dispatches")
    rejects("telegram_second_denied", {"dispatchCount":1}, "zero dispatches")
    combined = json.loads((tmp_path/"desktop-and-inbox").read_text())
    for index, (changes, message) in enumerate((
            ({"senderId":"502"},"reviewed first"),
            ({"markerSha256":"3"*64},"reviewed first"),
            ({"inboxEvidence":desktop},"independent evidence"),
            ({"inboxEvidence":pin("wrong-ledger",{**incoming_data,"runSha256":"2"*64})},"recorded Telegram"),
            ({"inboxEvidence":pin("pending",{**incoming_data,"state":"accepted"})},"recorded Telegram"),
            ({"inboxEvidence":pin("no-upload",{**incoming_data,"replyMessageId":None})},"recorded Telegram"))):
        edited = pin("wrong-receipt-"+str(index),{**combined,**changes})
        rejects("telegram_first",{"receiptPath":edited["path"],"receiptSha256":edited["sha256"]},message)
    spec["contractReceipt"] = pin("receipt",receipt)
    with pytest.raises(ValueError,match="different central wheel"):
        pi.reviewed_contract(pub,spec,{"sha256":"a"*64})
    before = observations.pop("telegram_second_denied")
    spec["contractReceipt"] = pin("receipt",receipt)
    with pytest.raises(ValueError,match="pinned private evidence"):
        pi.reviewed_contract(pub,spec,artifact)
    observations["telegram_second_denied"] = before
    observations["real_model"] = pin("model-mock", {**receipt,"schema":"agentbridge.pi-observation.v1","case":"real_model",
                                            "passed":True,"syntheticModel":True,"modelConfigSha256":spec["modelConfigSha256"]})
    spec["contractReceipt"] = pin("receipt",receipt)
    with pytest.raises(ValueError,match="real_model"):
        pi.reviewed_contract(pub,spec,artifact)
    monkeypatch.setattr(qualify,"fixed_candidate",lambda: {"commit":pub.commit,"wheelSha256":artifact["sha256"]})
    monkeypatch.setattr(qualify,"verify_runtime",lambda spec:None)
    monkeypatch.setattr(qualify,"snapshot",lambda:{"sourceSha256":"2"*64})
    output = tmp_path/"assembled.json"
    assert qualify.assemble(spec,report_ref["path"],[],output)["missing"] == ["real_model","telegram_first","workspace_first","workspace_second"]
    assert not output.exists()
    inputs = [tmp_path/v for v in ("real_model","telegram_first","workspace_first","workspace_second")]
    wrong = pin("wrong-client",{**json.loads((tmp_path/"workspace_first").read_text()),
                                "subjectSha256":hashlib.sha256(b"two").hexdigest()})
    with pytest.raises(ValueError,match="wrong reviewed account"):
        qualify.assemble(spec,report_ref["path"],[*inputs[:2],wrong["path"],inputs[-1]],output)
    web_only = {key:value for key,value in spec.items() if key != "telegram"}
    with pytest.raises(ValueError,match="cover both"):
        qualify.assemble(web_only,report_ref["path"],[inputs[0],wrong["path"],inputs[-1]],output)
    result = qualify.assemble(spec,report_ref["path"],inputs,output)
    assert result["complete"]
    assert pi.reviewed_contract(pub,{**spec,"contractReceipt":result["contractReceipt"]},artifact)["approved"]
    assert qualify.assemble(spec,report_ref["path"],list(reversed(inputs)),output) == result


@pytest.mark.parametrize("pillow,pdf,accepted",[("13.0.0","6.21.4",True),("12.0.0","6.21.4",False),("13.0.0","6.0.0",False)])
def test_engine_media_dependencies_are_pinned(tmp_path,monkeypatch,pillow,pdf,accepted):
    engine = private(tmp_path/"engine","synthetic-only")
    versions = ["3.12", "1.23.2","0.51.0",pillow,pdf]
    monkeypatch.setattr(pi,"run",lambda *args,**kwargs:json.dumps(versions))
    if accepted:
        assert pi.engine_identity(engine)["versions"] == versions
    else:
        with pytest.raises(ValueError,match="dependencies"):
            pi.engine_identity(engine)


def test_installed_pi_tree_is_bound_including_dependency_contents(tmp_path):
    root = tmp_path / "node_modules"
    root.mkdir()
    deps = root / "example"
    deps.mkdir()
    module = deps / "index.js"
    module.write_text("approved")
    digest = pi.sdk_tree_digest(root)
    module.write_text("changed")
    assert pi.sdk_tree_digest(root) != digest
    (deps/"outside").symlink_to(tmp_path)
    with pytest.raises(ValueError,match="leaves"):
        pi.sdk_tree_digest(root)


def test_private_profile_pins_candidate_sdk_model_bot_and_environment(tmp_path, monkeypatch):
    root = tmp_path / "pi"
    root.mkdir(mode=0o700)
    (root / "tunnel").mkdir()
    (root / "engine" / "bin").mkdir(parents=True)
    engine = root / "engine" / "bin" / "python"
    engine.write_text("synthetic interpreter")
    engine.chmod(0o700)
    (root / "engine" / "pyvenv.cfg").write_text("synthetic fixture")
    mac_key = private(tmp_path/"old.key", "openclaw fixture")
    key = private(root/"tunnel/key", "pi fixture")
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    mac = private(tmp_path/"mac.json", json.dumps({"port":40001, "tunnel":{
        "remotePort":40002, "host":"test.invalid", "identityFile":str(mac_key)}}))
    model = private(root/"model.json", json.dumps({"provider":"fixture", "id":"test",
                                                  "authFile":str(private(root/"auth.json","fixture"))}))
    sdk = tmp_path/"tools"/"node_modules"/pi.SDK
    sdk.mkdir(parents=True)
    (sdk/"package.json").write_text(json.dumps({"name":pi.SDK,"version":pi.SDK_VERSION}))
    lock = sdk.parents[2]/"package-lock.json"
    lock.write_text(json.dumps({"packages":{"node_modules/"+pi.SDK:{"version":pi.SDK_VERSION,
                                        "integrity":"sha512-"+"A"*86+"=="}}}))
    node = tmp_path/"node"
    node.write_text("fixture node")
    monkeypatch.setattr(pi,"run",lambda cmd: pubkey if str(cmd[0]).endswith("ssh-keygen") else "v26.5.0")
    spec = {"schema":"agentbridge.pi-mac-pilot.v1","instanceId":"pi-one","routeRef":"pi-route",
            "displayName":"Pi","adapterVersion":pi.ADAPTER,"enginePython":str(engine),"runtimeDir":str(root),
            "listenPort":41001,"remotePort":41002,"subjects":{"first":"pi-1","second":"pi-2"},
            "serviceTokenFile":str(private(root/"service.token","synthetic-only")),"modelConfigFile":str(model),
            "modelConfigSha256":pi.file_digest(model),"macHostSha256":pi.file_digest(mac),
            "centralMcpUrl":"https://test.invalid/mcp","trustedCardOrigins":["https://test.invalid"],
            "tunnelIdentityFile":str(key),"tunnelPublicKey":pubkey,"nodePath":str(node),"nodeSha256":pi.file_digest(node),
            "sdkDir":str(sdk),"sdkTreeSha256":pi.sdk_tree_digest(sdk.parents[1]),"sdkLockSha256":pi.file_digest(lock),
            "contractReceipt":{},"requiredLevel":"L1","telegram":{"botId":880001,"username":"pi_test_bot",
            "botCredential":{"kind":"macos-keychain","service":"fixture","account":"fixture"},"allowedSenders":{"first":"501"}}}
    def check():
        profile = private(root/"profile.json",json.dumps(spec))
        env = private(tmp_path/"env.json",json.dumps({"piPilot":{"path":str(profile),"sha256":pi.file_digest(profile)}}))
        units = {"agentbridge.service":b"Environment=AGENTBRIDGE_PI_ROUTES_FILE=/remote/config/pi-pi-one/routes.json"}
        return pi.checked_profile(profile,env,mac,units,"/remote")
    assert check() == spec
    spec["telegram"]["allowedSenders"]["second"] = "502"
    with pytest.raises(ValueError,match="exactly one"):
        check()
    del spec["telegram"]
    assert check() == spec
    (sdk/"installed.js").write_text("unreviewed dependency")
    with pytest.raises(ValueError,match="installed files changed"):
        check()


def test_publisher_refuses_before_staging_either_host_if_pi_conformance_changes(tmp_path, monkeypatch):
    import lifecycle
    import publish

    publisher = object.__new__(publish.Publisher)
    publisher.commit = "a"*40
    publisher.release = publisher.commit[:12]
    publisher.nanobot = {"instanceId":"existing-nano"}
    publisher.pi = {"instanceId":"candidate-pi"}
    publisher.units = {"agentbridge.service":b"reviewed"}
    publisher.args = SimpleNamespace(offline=False,resume=False,reuse_validation=True,identity_label=[],expect_endpoint=[],
                                    host_profile="fixture",authorization="fixture",profile="fixture",host="fixture",remote_root="fixture")
    mac = {"runtimeDir":str(tmp_path),"profileSha256":"original"}
    monkeypatch.setattr(publisher,"plan",lambda:{"blockers":[],"remoteUrl":"https://test.invalid/repo"})
    monkeypatch.setattr(lifecycle,"settings",lambda _:mac)
    monkeypatch.setattr(lifecycle,"authorize",lambda *_:None)
    monkeypatch.setattr(lifecycle,"fingerprint",lambda _:{"host":"verified"})
    monkeypatch.setattr(publish,"candidate_artifact",lambda **_:({"sha256":"b"*64},"reused"))
    monkeypatch.setattr(publish,"environment",lambda *_:publisher.units)
    monkeypatch.setattr(publish,"git",lambda *_:publisher.commit)
    monkeypatch.setattr(publish,"test_run",lambda *_:None)
    monkeypatch.setattr(pi,"profile_for",lambda _:publisher.pi)
    monkeypatch.setattr(pi,"reviewed_contract",lambda *_:{"approved":True})
    def fail(*args):
        raise ValueError("isolated conformance changed")
    monkeypatch.setattr(pi,"verify_conformance",fail)
    for obj, attr in ((publish.nanobot_release,"verify_bot"),(publish.nanobot_release,"remote"),
                      (pi,"remote"),(publisher,"deploy"),(publish,"run")):
        monkeypatch.setattr(obj,attr,lambda *args,**kw:pytest.fail("Reached an external action before Pi qualification"))
    with pytest.raises(ValueError,match="conformance changed"):
        publisher.execute()


def test_reviewed_transition_preserves_live_preimage_until_atomic_activation_then_restores(tmp_path, monkeypatch):
    import hashlib
    import os
    _, args, ws, _, _, _ = synthetic_admission(tmp_path,monkeypatch)
    monkeypatch.delenv("AGENTBRIDGE_PI_ROUTES_FILE",raising=False)
    before = {**args["spec"],"hostType":"pi","instanceId":"pi-next","routeRef":"pi-next","adapterVersion":pi.ADAPTER,
              "requiredLevel":"L1","subjects":{"alice":"pi-a","bob":"pi-b"}}
    before.pop("telegram",None)
    config = tmp_path/"config"/"pi-pi-next"
    config.mkdir(mode=0o700)
    live = private(config/"routes.json",central.route_document(config,before).decode())
    (config/"service.token").write_text("fixture-route-only")
    (config/"service.token").chmod(0o600)
    first = {**args,"spec":before,"action":"bind"}
    old_bindings = central.execute(first)
    after = {**before,"requiredLevel":"L3","telegram":{"botId":98001,"username":"pi_private_bot","allowedSenders":{"alice":"501"}}}
    old_bytes, new_bytes = live.read_bytes(), central.route_document(config,after)
    transition = {"fromRelease":"1"*12,"toRelease":"2"*12,"fromLevel":"L1","fromTelegram":None,
                  "beforeSha256":hashlib.sha256(old_bytes).hexdigest(),"afterSha256":hashlib.sha256(new_bytes).hexdigest()}
    spec = {**after,"routeTransition":transition}
    req = {**args,"spec":spec,"candidate":"2"*12}
    with monkeypatch.context() as privileged:
        privileged.setattr(os,"geteuid",lambda:0)
        with pytest.raises(FileNotFoundError):
            central.execute({**req,"action":"route_activate"})
        assert central.execute({**req,"action":"stage","serviceSecret":"fixture-route-only"})["routeState"] == "predecessor"
        assert live.read_bytes() == old_bytes
        assert central.execute({**req,"action":"route_restore"}) == {"changed":False,"routeState":"predecessor"}
        assert central.execute({**req,"action":"route_activate"}) == {"changed":True,"routeState":"candidate"}
        assert live.read_bytes() == new_bytes and live.stat().st_mode & 0o077 == 0
        assert central.execute({**req,"action":"route_activate"})["changed"] is False
        assert central.execute({**req,"action":"stage","serviceSecret":"fixture-route-only"})["routeState"] == "candidate"
        assert central.execute({**req,"action":"route_restore"}) == {"changed":True,"routeState":"predecessor"}
        assert live.read_bytes() == old_bytes
        with pytest.raises(ValueError,match="candidate"):
            central.execute({**req,"candidate":"3"*12,"action":"route_activate"})
        live.write_bytes(old_bytes+b" ")
        with pytest.raises(ValueError,match="outside"):
            central.execute({**req,"action":"route_activate"})
        assert live.read_bytes() == old_bytes+b" "
        live.write_bytes(old_bytes)
    assert central.execute(first) == old_bindings
    with ws._connect() as db:
        db.execute("DELETE FROM workspace_host_visibility WHERE instance_id='pi-next' AND user_subject='bob'")
    with pytest.raises(ValueError,match="administrator scope"):
        central.execute({**req,"action":"route_activate"})


def test_predeployment_cache_retests_a_matching_contract_without_archive_evidence(tmp_path, monkeypatch):
    runtime = tmp_path/"runtime"
    runtime.mkdir(mode=0o700)
    directory = pi.shared.owned_directory(runtime/"qualifications")
    commit, wheel = "a"*40, "b"*64
    report = {"schema":"agentbridge.pi-conformance.v1","implementation":"pi","adapterVersion":pi.ADAPTER,
              "sdkVersion":pi.SDK_VERSION,"level":"L1","formal":True,"passed":True,"sourceStable":True,
              "runtimeStable":True,"commit":commit,"wheelSha256":wheel,"engine":{"test":"engine"},
              "nodeSha256":"1"*64,"sdkTreeSha256":"2"*64,"sdkLockSha256":"3"*64,
              "adapterSourcesSha256":"4"*64,"cases":[],"probes":[]}
    report["contractEvidence"] = pi.conformance_evidence(report)
    qualification=tmp_path/"qualified.json"
    pi.save(qualification,{"contractEvidence":report["contractEvidence"]})
    spec = {"runtimeDir":str(runtime),"requiredLevel":"L1","enginePython":"/fixture/engine",
            "contractReceipt":{"path":str(qualification),"sha256":pi.file_digest(qualification)},
            "nodePath":"/fixture/node","sdkDir":"/fixture/sdk",
            **{k:report[k] for k in ("nodeSha256","sdkTreeSha256","sdkLockSha256")}}
    cache=directory/(commit+"-L1.json")
    pi.save(cache,report)
    monkeypatch.setattr(pi,"engine_identity",lambda path:report["engine"])
    calls=[]
    def run(argv, **kwargs):
        calls.append(argv)
        pi.save(argv[-1],{**report,"archiveProbe":pi.expected_archive_probe(report)})
    monkeypatch.setattr(pi,"run",run)
    for _ in range(2):
        pi.verify_conformance(SimpleNamespace(commit=commit),spec,{"sha256":wheel})
    assert len(calls)==1 and "--formal" in calls[0]
    assert pi.archive_probe_matches(pi.read(cache))


@pytest.mark.parametrize(("old_version", "new_version"), [("0.1.0", "0.1.1"), ("0.1.1", "0.1.2"), ("0.1.2", pi.ADAPTER)])
@pytest.mark.parametrize("first_closed", [False, True])
def test_pi_patch_upgrades_original_scope_and_restores_only_its_version(tmp_path, monkeypatch, first_closed, old_version, new_version):
    import hashlib
    import os

    _, args, ws, identities, _, _ = synthetic_admission(tmp_path, monkeypatch)
    old = {**args["spec"], "hostType":"pi", "instanceId":"pi-patch", "routeRef":"pi-patch",
           "adapterVersion":old_version, "requiredLevel":"L3", "subjects":{"alice":"pi-a","bob":"pi-b"},
           "telegram":{"botId":97001,"username":"pi_patch_bot","allowedSenders":{"alice":"501"}}}
    config = tmp_path/"config"/"pi-pi-patch"
    config.mkdir(mode=0o700)
    original = central.route_document(config,old)
    manifest = private(config/"routes.json", original.decode())
    private(config/"service.token","fixture-service")
    previous = {**args,"spec":old}
    initial = central.execute({**previous,"action":"bind"})["bindings"]
    if first_closed:
        central.execute({**previous,"action":"rollback_scope"})
    version = lambda: next(x for x in ws.hosts.admin_list() if x["instance_id"]==old["instanceId"])
    revision = version()["revision"]
    current = {**old,"adapterVersion":new_version}
    candidate = "2"*12
    updated = central.route_document(config,current)
    transition = {"fromRelease":"1"*12,"toRelease":candidate,"fromLevel":"L3","fromTelegram":old["telegram"],
                  "beforeSha256":hashlib.sha256(original).hexdigest(),"afterSha256":hashlib.sha256(updated).hexdigest(),
                  "fromAdapterVersion":old_version,"fromRevision":revision}
    request = {**args,"spec":{**current,"routeTransition":transition},"candidate":candidate}
    def invoke(action, **extra):
        return central.execute({**request, "action":action, **extra})
    def stage():
        with monkeypatch.context() as root:
            root.setattr(os,"geteuid",lambda:0)
            return invoke("stage",serviceSecret="fixture-service")

    skipped_version = {"0.1.0": "0.1.2", "0.1.1": "0.1.3"}.get(old_version)
    if skipped_version:
        skipped = {**current, "adapterVersion": skipped_version, "routeTransition": transition}
        with pytest.raises(ValueError, match="reviewed versions"):
            central.transition_routes(config, skipped, candidate)
    assert stage()["routeState"]=="predecessor"
    assert manifest.read_bytes()==original and version()["adapter_version"]==old_version
    if not first_closed:
        replace = central.replace_route
        def interrupt_after_rename(path, data, uid, gid):
            replace(path, data, uid, gid)
            if data == updated:
                raise OSError("simulated failure after manifest rename")
        with monkeypatch.context() as interrupted:
            interrupted.setattr(central, "replace_route", interrupt_after_rename)
            with pytest.raises(OSError, match="after manifest rename"):
                invoke("route_activate")
        assert manifest.read_bytes()==original and version()["revision"]==revision
        wrong = {**transition,"fromRevision":revision+1}
        with pytest.raises(ValueError,match="reviewed revision"):
            central.execute({**request,"spec":{**current,"routeTransition":wrong},"action":"route_activate"})
        assert manifest.read_bytes()==original
    recovery = {}
    if first_closed:
        proof = {"sourceCandidate":transition["fromRelease"],"candidate":candidate,"attempt":"a"*32,
                 "previousAttempt":None,"subjects":{}}
        for item in initial:
            binding = item["binding"]
            proof["subjects"][binding["user_subject"]] = {**binding,
                "fileDigest":hashlib.sha256(item["personalCredential"].encode()).hexdigest()}
        proof["baseRevision"] = invoke("recovery_check",recovery=proof)["baseRevision"]
        recovery = {"recovery":proof}
    assert invoke("route_activate")["changed"] is True
    assert invoke("route_activate")["changed"] is False
    assert manifest.read_bytes()==updated and version()["adapter_version"]==new_version
    issued = invoke("bind", **recovery)["bindings"]
    assert [r["binding"] for r in initial] == [r["binding"] for r in issued]
    if first_closed:
        assert all(identities.verify(r["personalCredential"]) is None for r in initial)
        assert all(identities.verify(r["personalCredential"]) for r in issued)
    else:
        assert issued==initial

    assert invoke("route_restore")["changed"] is True
    assert manifest.read_bytes()==original and version()["adapter_version"]==old_version
    if first_closed:
        assert invoke("rollback_scope",**recovery)["reverted"]
        assert version()["state"]=="revoked" and all(identities.verify(r["personalCredential"]) is None for r in issued)
    else:
        assert central.execute({**previous,"action":"bind"})["bindings"]==initial
    assert invoke("route_restore")["changed"] is False
    with ws._connect() as db:
        db.execute("UPDATE workspace_host_instances SET revision=revision+1 WHERE instance_id=?",(old["instanceId"],))
    with pytest.raises(ValueError, match="revision"):
        invoke("route_activate")
    assert manifest.read_bytes()==original
