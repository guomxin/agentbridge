"""Only the native display-name change may extend the audit without changing authority."""

import json

import pytest

from tests.test_nanobot_native_publish import central, host, synthetic_admission
from tests.test_nanobot_native_retry import check, rolled


def test_native_rename_cas_audit_repeat_compensation_and_new_attempt(tmp_path, monkeypatch):
    spec, args, ws, ids, _, _ = synthetic_admission(tmp_path, monkeypatch)
    records = central.execute({**args, "action": "bind"})["bindings"]
    before = [ws.hosts.choices(subject) for subject in spec["subjects"]]
    instance = spec["instanceId"]
    spec["displayNameChange"] = {"from":spec["displayName"], "fromRelease":"1"*12, "expectedRevision":1}
    spec["displayName"] = "NanoBot"
    args["spec"] = host.metadata(spec)
    proof = {"revision":1,"candidate":"2"*12,"attempt":"a"*32}
    def invoke(action, attempt=proof):
        return central.execute({**args,"action":action,"nameProof":attempt})
    assert invoke("name_check") == {"matches":False,"revision":1}
    assert invoke("name_restore") == {"changed":False}
    assert invoke("name_apply") == invoke("name_apply") == {"changed":True}
    assert invoke("name_check") == {"matches":True,"revision":2}
    assert invoke("name_restore") == invoke("name_restore") == {"changed":True}
    assert invoke("name_check") == {"matches":False,"revision":3}
    again = {**proof,"revision":3,"attempt":"b"*32}
    assert invoke("name_apply",again) == {"changed":True}
    assert invoke("name_check") == {"matches":True,"revision":4}
    assert sum(bool(ids.verify(row["personalCredential"])) for row in records) == 2
    assert all(before[i]["default_instance_id"] == ws.hosts.choices(subject)["default_instance_id"]
               for i,subject in enumerate(spec["subjects"]))
    with ws._connect() as db:
        rows = db.execute("SELECT before_json,after_json FROM admin_audit_events WHERE action='workspace.host.rename'").fetchall()
        assert len(rows)==3
        for row in rows:
            start, end = map(json.loads,row)
            assert all(start[key]==end[key] for key in central.NAME_SCOPE)
    # An administrator's subsequent edit is always respected, even if the retained name is the same.
    ws.hosts.save_instance(instance,host_type="nanobot",route_ref=spec["routeRef"],display_name="NanoBot",state="enabled",
                           subjects=["alice"],expected_revision=4,adapter_version=spec["adapterVersion"],actor="human",reason="review")
    with pytest.raises(ValueError):
        invoke("name_restore", again)
    assert ws.hosts.admin_list()[1]["subjects"]==["alice"]


def test_name_audit_preserves_first_automatic_rollback_proof(tmp_path, monkeypatch):
    spec, args, ws, _, _, _ = synthetic_admission(tmp_path, monkeypatch)
    central.execute({**args,"action":"bind"})
    central.execute({**args,"action":"rollback_scope"})
    with ws._connect() as db:
        row=ws.hosts._admin_row(db,spec["instanceId"])
        originals,_=central.recovery_history(db,spec,row)
        assert set(originals)=={1,2}
    # Neither an unauthorized active-state transition nor an old release proof can rename a revoked scope.
    spec["displayNameChange"]={"from":spec["displayName"],"fromRelease":"0"*12,"expectedRevision":1}
    spec["displayName"]="NanoBot"
    with pytest.raises(ValueError, match="no authority"):
        central.execute({**args,"spec":host.metadata(spec),"action":"name_apply",
                         "nameProof":{"candidate":"1"*12,"attempt":"e"*32,"revision":2}})

def test_display_compensation_keeps_previous_native_admission_eligible_for_rollback(rolled):
    r = rolled
    original_proof = check(r)
    active = r.remote(None, r.spec, "bind", recovery=original_proof)["bindings"]
    spec = {**r.spec, "displayName":"NanoBot", "displayNameChange":{
        "from":r.spec["displayName"], "fromRelease":r.second, "expectedRevision":3}}
    args = {**r.args, "spec":host.metadata(spec)}
    proof = {"revision":3,"candidate":"c"*12,"attempt":"d"*32}
    for action in ("name_apply", "name_restore"):
        assert central.execute({**args, "action":action, "nameProof":proof}) == {"changed":True}
    # Physical revisions 4/5 record the rename and compensation. The original security
    # revision 3 still owns the same two token generations and original endpoint bindings.
    assert all(r.ids.verify(entry["personalCredential"]) for entry in active)
    result = r.remote(None, r.spec, "rollback_scope", recovery=original_proof)
    assert result == {"closed":True,"reverted":True}
    assert all(r.ids.verify(entry["personalCredential"]) is None for entry in active)
    assert next(i for i in r.ws.hosts.admin_list() if i["instance_id"] == r.spec["instanceId"])["revision"] == 6


def test_lost_apply_reply_compensates_through_native_host_transaction(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tests.test_nanobot_native_publish import mac_for, private
    spec, args, ws, _, _, _ = synthetic_admission(tmp_path, monkeypatch)
    central.execute({**args,"action":"bind"})
    previous = tmp_path / "releases" / ("1"*12)
    candidate = tmp_path / "releases" / ("2"*12)
    for release in (previous,candidate):
        release.mkdir(mode=0o700,parents=True)
        private(release / "host.json","{}")
    host.set_current(tmp_path,previous)
    private(tmp_path / "native-release.json", json.dumps({"stage":"confirmed","candidate":previous.name}))
    spec["displayNameChange"] = {"from":spec["displayName"], "fromRelease":previous.name, "expectedRevision":1}
    spec["displayName"] = "NanoBot"
    args["spec"] = host.metadata(spec)
    events = []
    def remote(pub, config, action, **kwargs):
        events.append(action)
        if action=="observe":
            return {"scopePresent":True,"tunnelAuthorized":True}
        if action.startswith("name_"):
            result=central.execute({**args,"action":action,**kwargs})
            if action=="name_apply":
                raise RuntimeError("synthetic lost native response")
            return result
        return {}
    monkeypatch.setattr(host,"managed_jobs",lambda _: {})
    monkeypatch.setattr(host,"archive_bridge",lambda *_:candidate)
    monkeypatch.setattr(host,"configure_release",lambda *_:None)
    monkeypatch.setattr(host,"verify_subjects",lambda *_:{})
    monkeypatch.setattr(host,"wait_for_route",lambda *_:{"ready":True})
    monkeypatch.setattr(host,"wait_for_running_jobs",lambda *_:None)
    monkeypatch.setattr(host,"remote",remote)
    pub=SimpleNamespace(release=candidate.name,args=SimpleNamespace(resume=False))
    with pytest.raises(RuntimeError,match="synthetic lost native response"):
        host.admission(pub,spec,mac_for(),{})
    assert events.index("name_apply") < events.index("name_restore")
    assert (tmp_path/"current").resolve()==previous and json.loads((tmp_path/"native-release.json").read_text())["stage"]=="rolled_back"
    actual=next(i for i in ws.hosts.admin_list() if i["instance_id"]==spec["instanceId"])
    assert actual["state"]=="enabled" and actual["display_name"]==spec["displayNameChange"]["from"]
