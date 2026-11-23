"""Rehearse the legacy first rollback and fresh-credential admission without any live services."""

import json
from types import SimpleNamespace

import pytest

from tests.test_nanobot_native_publish import (
    central,
    host,
    private,
    synthetic_admission,
)


@pytest.fixture
def rolled(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    spec, args, ws, ids, original, config = synthetic_admission(root, monkeypatch)

    def remote(pub, spec, action, **extra):
        return central.execute({**args, "action": action, **extra})

    monkeypatch.setattr(host, "remote", remote)
    issued = remote(None, spec, "bind")["bindings"]
    bindings = host.ensure_local_bindings(root, issued)
    first, second = "a" * 12, "b" * 12
    archive = root / "releases" / first
    archive.mkdir(parents=True)
    private(
        archive / "host.json",
        json.dumps({"instanceId": spec["instanceId"], "bindings": bindings}),
    )
    remote(None, spec, "rollback_scope")
    receipt = {
        "stage": "rolled_back",
        "previous": None,
        "previousScopePresent": False,
        "jobs": {},
        "previousTunnelAuthorized": False,
        "candidate": first,
    }
    pub = SimpleNamespace(
        release=second,
        policy={"compatibleFrom": [first]},
        args=SimpleNamespace(resume=False),
    )
    return SimpleNamespace(**locals())


def check(r, receipt=None):
    return host.retry_proof(
        r.pub, r.spec, r.root, None, receipt or r.receipt, {}, {"scopePresent": True}
    )


def tokens(records):
    return [r["personalCredential"] for r in records]


def test_retry_rotates_exact_two_tokens_persists_receipts_survives_another_rollback(
    rolled,
):
    r = rolled
    proof = check(r)
    assert proof["baseRevision"] == 2
    assert (
        next((r.root / "rollback-history").glob("*.json")).stat().st_mode & 0o077 == 0
    )
    admission = lambda action, proof: r.remote(None, r.spec, action, recovery=proof)
    records = admission("bind", proof)["bindings"]
    assert admission("bind", proof)["bindings"] == records
    assert len(set(tokens(records) + tokens(r.issued))) == 4
    assert all(r.ids.verify(token) is None for token in tokens(r.issued))
    assert all(r.ids.verify(token) for token in tokens(records))
    from pathlib import Path

    bindings = host.ensure_local_bindings(r.root, records)
    assert {Path(b["tokenFile"]).read_text() for b in bindings} == set(tokens(records))
    assert {Path(b["tokenFile"]).stat().st_mode & 0o777 for b in bindings} == {0o600}
    # A future normal upgrade gets the confirmed generation without any recovery privileges.
    again = r.remote(None, r.spec, "bind")["bindings"]
    assert set(tokens(again)) == set(tokens(records))
    assert all("retiredCredentialDigest" not in rec for rec in again)
    assert host.ensure_local_bindings(r.root, again) == bindings
    assert admission("rollback_scope", proof) == {"closed": True, "reverted": True}
    assert admission("rollback_scope", proof) == {"closed": True, "reverted": True}
    assert all(r.ids.verify(token) is None for token in tokens(records))
    assert all(r.ids.verify(token) for token in r.original.values())
    with pytest.raises(ValueError):
        admission("bind", proof)
    with pytest.raises(ValueError, match="cannot override"):
        r.remote(None, r.spec, "bind")
    failed = {**r.receipt, "candidate": r.second, "recovery": proof}
    r.pub.release, r.pub.policy = "c" * 12, {"compatibleFrom": [r.second]}
    third = check(r, failed)
    assert third["baseRevision"] == 4
    fresh = admission("bind", third)["bindings"]
    assert len(set(tokens(records) + tokens(r.issued) + tokens(fresh))) == 6
    assert all(r.ids.verify(token) for token in tokens(fresh))
    assert all(
        r.ws.hosts.choices(subject)["default_instance_id"] == "openclaw-default"
        for subject in r.original
    )


@pytest.mark.parametrize(
    "change",
    [
        "directory",
        "token-time",
        "admin-token",
        "binding",
        "local-material",
        "other-label-token",
        "linux-material",
        "unreviewed-ancestor",
    ],
)
def test_cannot_reopen_foreign_changes_or_replace_private_material(rolled, change):
    r = rolled
    subject = next(iter(r.original))
    ref = r.spec["subjects"][subject]
    instance = r.spec["instanceId"]
    if change == "local-material":
        private(r.root / "credentials" / (ref + ".token"), "unrelated secret")
    elif change == "linux-material":
        private(r.config / (ref + ".token"), "unrelated secret")
    elif change == "unreviewed-ancestor":
        r.pub.policy = {"compatibleFrom": []}
    elif change == "other-label-token":
        r.ids.issue(
            user_subject=subject,
            expected_principal_ref=subject,
            label=f"workspace-nanobot:{instance}:{ref}",
        )
    else:
        with r.ws._connect() as db:
            if change == "directory":
                db.execute(
                    "UPDATE workspace_host_directory_audit SET actor='administrator' WHERE revision=2 AND instance_id=?",
                    (instance,),
                )
            elif change == "binding":
                db.execute(
                    "UPDATE workspace_host_bindings SET credential_ref=? WHERE instance_id=? AND account_id=?",
                    ("other-ref", instance, subject),
                )
            else:
                row = db.execute(
                    "SELECT token_id FROM mcp_identity_tokens WHERE label=?",
                    (f"workspace-nanobot:{instance}:{ref}",),
                ).fetchone()
                if change == "token-time":
                    db.execute(
                        "UPDATE mcp_identity_tokens SET revoked_at='2000-01-01T00:00:00+00:00' WHERE token_id=?",
                        (row[0],),
                    )
                else:
                    db.execute(
                        "INSERT INTO admin_audit_events(event_id,actor_username,action,target_type,target_id,result,created_at) "
                        "VALUES('revoke-event','operator','mcp.token.revoke','mcp_token',?,'succeeded','t')",
                        (row[0],),
                    )
    with pytest.raises(ValueError):
        check(r)
    with r.ws._connect() as db:
        assert tuple(
            db.execute(
                "SELECT revision,state FROM workspace_host_instances WHERE instance_id=?",
                (instance,),
            ).fetchone()
        ) == (2, "revoked")
    assert all(r.ids.verify(token) for token in r.original.values())


def test_credential_write_failure_and_changed_local_file_are_never_overwritten(
    rolled, monkeypatch
):
    r = rolled
    proof = check(r)
    store = central.store_private
    count = 0

    def broken(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic disk full")
        store(*args)

    monkeypatch.setattr(central, "store_private", broken)
    with pytest.raises(OSError):
        r.remote(None, r.spec, "bind", recovery=proof)
    assert not list(r.config.glob("issued-*.token"))
    assert r.remote(None, r.spec, "rollback_scope", recovery=proof) == {
        "closed": True,
        "reverted": False,
    }
    assert check(r)["baseRevision"] == 2
    monkeypatch.setattr(central, "store_private", store)
    records = r.remote(None, r.spec, "bind", recovery=proof)["bindings"]
    replaced = (
        r.root / "credentials" / (records[0]["binding"]["credential_ref"] + ".token")
    )
    private(replaced, "another source")
    with pytest.raises(ValueError, match="differs"):
        host.ensure_local_bindings(r.root, records)
    assert replaced.read_text() == "another source"
    assert r.remote(None, r.spec, "rollback_scope", recovery=proof)["reverted"]
    assert all(r.ids.verify(token) is None for token in tokens(records))


def test_publish_retry_restores_after_lost_bind_response_and_normal_upgrade_reuses_new_generation(
    rolled, monkeypatch
):
    from tests.test_nanobot_native_publish import mac_for

    r = rolled
    root = r.root
    state_file = root / "native-release.json"
    host.save(state_file, r.receipt)
    tunnel = False
    fail_after_issue = True

    def ssh(pub, spec, action, **extra):
        nonlocal tunnel
        if action == "observe":
            return {"scopePresent": True, "tunnelAuthorized": tunnel}
        if action in {"tunnel_enable", "tunnel_disable"}:
            tunnel = action == "tunnel_enable"
            return {}
        result = r.remote(pub, spec, action, **extra)
        if action == "bind" and fail_after_issue:
            raise RuntimeError("synthetic receipt loss")
        return result

    def archive(pub, *_):
        path = root / "releases" / pub.release
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(host, "remote", ssh)
    monkeypatch.setattr(host, "managed_jobs", lambda _: {})
    monkeypatch.setattr(host, "archive_bridge", archive)
    monkeypatch.setattr(
        host, "verify_subjects", lambda *_: {"authenticatedSubjects": 2}
    )
    monkeypatch.setattr(host, "wait_for_route", lambda *_: {"ready": True})
    monkeypatch.setattr(host, "wait_for_running_jobs", lambda *_: None)
    r.spec["listenPort"] = 0
    with pytest.raises(RuntimeError, match="synthetic receipt loss"):
        host.admission(r.pub, r.spec, mac_for(), {})
    failed = host.read(state_file)
    assert failed["stage"] == "rolled_back" and failed["recovery"]["baseRevision"] == 2
    with r.ws._connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM mcp_identity_tokens WHERE state='active' AND label LIKE 'workspace-nanobot:%'"
            ).fetchone()[0]
            == 0
        )
    assert not (root / "current").exists() and not tunnel
    fail_after_issue = False
    r.pub.release = "c" * 12
    r.pub.policy = {"compatibleFrom": [r.second]}
    assert host.admission(r.pub, r.spec, mac_for(), {})["authenticatedSubjects"] == 2
    confirmed = host.read(state_file)
    assert (
        confirmed["stage"] == "confirmed" and confirmed["recovery"]["baseRevision"] == 4
    )
    assert tunnel and (root / "current").resolve().name == r.pub.release

    def active():
        return sorted(
            x["token_id"]
            for subject in r.original
            for x in r.ids.list(user_subject=subject)
            if (x["label"] or "").startswith("workspace-nanobot:")
            and x["state"] == "active"
        )

    current_tokens = active()
    assert len(current_tokens) == 2
    r.pub.release = "d" * 12
    assert host.admission(r.pub, r.spec, mac_for(), {})["ready"]
    assert "recovery" not in host.read(state_file) and active() == current_tokens
    assert len(list((root / "rollback-history").glob("*.json"))) == 2
