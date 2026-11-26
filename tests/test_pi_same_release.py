"""Pi-only recovery guards. All checkout, wheel and service data are isolated."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

NATIVE = Path(__file__).resolve().parents[1] / "scripts/native"
sys.path.insert(0, str(NATIVE))

import nanobot_release
import pi_local
import pi_release
import pi_same_release as restore
import publish
import lifecycle


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def tree(directory):
    directory.mkdir()
    git(directory, "init", "-q")
    git(directory, "config", "user.name", "fixture")
    git(directory, "config", "user.email", "fixture@example.invalid")
    (directory / "module.py").write_text("value = 7\n")
    (directory / ".gitignore").write_text("ignored.py\n__pycache__/\n")
    git(directory, "add", ".")
    git(directory, "commit", "-qm", "initial")
    return git(directory, "rev-parse", "HEAD")


def test_verifies_files_against_trusted_tree_not_index(tmp_path):
    trusted, source = tmp_path / "trusted", tmp_path / "original"
    commit = tree(trusted)
    subprocess.run(["git", "clone", "--quiet", str(trusted), str(source)], check=True)
    assert restore.exact_source(trusted, source) == commit
    (source / "ignored.py").write_text("raise RuntimeError\n")
    with pytest.raises(ValueError, match="Unexpected executable"):
        restore.exact_source(trusted, source)
    (source / "ignored.py").unlink()
    git(source, "update-index", "--assume-unchanged", "module.py")
    (source / "module.py").write_text("value = 8\n")
    assert git(source, "status", "--porcelain") == ""
    with pytest.raises(ValueError, match="trusted Git tree"):
        restore.exact_source(trusted, source)
    other = tmp_path / "other"
    tree(other)
    (other / "foreign.py").write_text("value = 9\n")
    git(other, "add", ".")
    git(other, "commit", "-qm", "foreign")
    with pytest.raises(ValueError):
        restore.exact_source(trusted, other)


def test_only_documentation_may_follow_published_commit(tmp_path):
    repo = tmp_path / "repo"
    original = tree(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "read.md").write_text("notes\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "notes")
    docs = git(repo, "rev-parse", "HEAD")
    restore.published_ancestor(repo, original, docs)
    restore.published_ancestor(repo, original, original)
    (repo / "module.py").write_text("value = 2")
    git(repo, "commit", "-qam", "code")
    git(repo, "revert", "--no-edit", "HEAD")
    with pytest.raises(ValueError, match="non-documentation"):
        restore.published_ancestor(repo, original, git(repo, "rev-parse", "HEAD"))
    with pytest.raises(ValueError):
        restore.published_ancestor(repo, docs, original)


def wheel(path, day=10, *, record=b"file proof", mode=0o100644, extra=False):
    with zipfile.ZipFile(path, "w") as output:
        for name, value in [("package.py", b"value=1"), ("test.dist-info/RECORD", record)]:
            info = zipfile.ZipInfo(name, (2026, 11, day, 1, 30, 0))
            info.external_attr = mode << 16
            output.writestr(info, value)
        if extra:
            output.writestr("another.py", b"1")
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_original_archive_uses_original_bytes_and_enforces_member_integrity(tmp_path):
    old = tmp_path / "releases" / "aabbccddeeff" / "agentbridge.whl"
    old.parent.mkdir(parents=True)
    original_sha = wheel(old)
    rebuilt = tmp_path / "rebuilt.whl"
    rebuilt_sha = wheel(rebuilt, day=12)
    assert rebuilt_sha != original_sha
    rebuilt_artifact = {"sha256": rebuilt_sha, "wheel": str(rebuilt)}
    spec = {"runtimeDir": str(tmp_path)}
    pub = SimpleNamespace(release=old.parent.name)
    assert restore.original_artifact(pub, spec, rebuilt_artifact) == {"wheel": str(old), "sha256": original_sha}
    for kw in [{"extra": True}, {"record": b"changed"}, {"mode": 0o120777}]:
        rebuilt_artifact["sha256"] = wheel(rebuilt, **kw)
        with pytest.raises(ValueError):
            restore.original_artifact(pub, spec, rebuilt_artifact)


def test_central_active_requires_actual_wheel_and_commit(tmp_path):
    release = "abcdef012345"
    depot = tmp_path / "releases" / release
    depot.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/release.env").write_text("AGENTBRIDGE_RELEASE_ID=" + release + "\n")
    (tmp_path / "current").symlink_to(depot)
    digest = wheel(depot / "published.whl")
    commit = release + "1" * 28
    (depot / "artifact.json").write_text(json.dumps({"commit": commit, "sha256": digest}))
    (depot / "deployment.json").write_text(json.dumps({"status": "confirmed", "releaseId": release}))

    def probe():
        return subprocess.run(
            [sys.executable, "-I", "-c", restore.CENTRAL_VERIFY, str(tmp_path), release], capture_output=True
        )

    assert json.loads(probe().stdout) == [commit, digest]
    (depot / "published.whl").write_bytes(b"bad")
    assert probe().returncode != 0
    pub = SimpleNamespace(
        commit=commit,
        release=release,
        args=SimpleNamespace(remote_root="/tmp/test"),
        ssh=lambda command: json.dumps(["0" * 40, digest]),
    )
    with pytest.raises(ValueError, match="Active central"):
        restore.central_active(pub, {"sha256": digest})


@pytest.mark.parametrize("flag", ["offline", "resume", "rollback_pi", "rollback_nanobot"])
def test_mixed_modes_rejected_before_any_io(flag):
    args = SimpleNamespace(offline=False, resume=False, rollback_pi=False, rollback_nanobot=False)
    setattr(args, flag, True)
    with pytest.raises(ValueError, match="standalone"):
        restore.restore_published(args)


def test_exact_stage_binding_and_idempotent_already_confirmed(tmp_path):
    pub = SimpleNamespace(release="a" * 12)
    archive = tmp_path / "releases" / pub.release
    archive.mkdir(parents=True)
    (archive / "host.json").write_text("{}")
    spec = {"runtimeDir": str(tmp_path)}
    record = {
        "stage": "rolled_back",
        "candidate": pub.release,
        "previous": None,
        "previousScopePresent": False,
        "previousTunnelAuthorized": False,
    }
    observed = {"scopePresent": True, "portOccupied": False, "tunnelAuthorized": False}
    path = tmp_path / "native-release.json"

    def set_record(**values):
        path.write_text(json.dumps({**record, **values}))
        path.chmod(0o600)

    set_record()
    assert restore.source_state(pub, spec, observed) == record
    for changed in [{"portOccupied": True}, {"scopePresent": False}, {"tunnelAuthorized": True}]:
        with pytest.raises(ValueError):
            restore.source_state(pub, spec, {**observed, **changed})
    for changed in [
        {"candidate": "b" * 12},
        {"previous": "b" * 12},
        {"stage": "switching"},
        {"stage": "confirmed"},
    ]:
        set_record(**changed)
        with pytest.raises(ValueError):
            restore.source_state(pub, spec, observed)
    set_record(stage="confirmed")
    (tmp_path / "current").symlink_to(archive)
    assert restore.source_state(pub, spec, observed)["stage"] == "confirmed"


@pytest.mark.parametrize("apply,stage", [(False, "rolled_back"), (True, "rolled_back"), (True, "confirmed")])
def test_transaction_uses_old_admission_only_and_never_publishes(monkeypatch, apply, stage):
    commit = "c" * 40
    args = SimpleNamespace(
        verifiedCommit=commit, verifiedMain=commit, host_profile="reviewed", authorization="accepted", apply=apply
    )
    spec = {"runtimeDir": "/unused"}
    pub = SimpleNamespace(commit=commit, release=commit[:12], pi=spec)
    monkeypatch.setattr(publish, "Publisher", lambda args: pub)
    monkeypatch.setattr(lifecycle, "settings", lambda path: {})
    monkeypatch.setattr(lifecycle, "authorize", lambda host, auth: None)
    monkeypatch.setattr(publish, "candidate_artifact", lambda **kw: ({"sha256": "rebuilt"}, "reused"))
    monkeypatch.setattr(restore, "original_artifact", lambda *a: {"sha256": "original"})
    monkeypatch.setattr(pi_release, "reviewed_contract", lambda *a: {"approved": True})
    monkeypatch.setattr(pi_release, "verify_conformance", lambda *a: None)
    monkeypatch.setattr(restore, "archive_probe", lambda *a: {"completedRuns": 2})
    monkeypatch.setattr(nanobot_release, "verify_bot", lambda spec: None)
    monkeypatch.setattr(restore, "central_active", lambda *a: None)
    monkeypatch.setattr(restore, "validate_closed", lambda *a: None)
    monkeypatch.setattr(pi_release, "profile_for", lambda pub: spec)
    record = {"stage": stage}
    monkeypatch.setattr(restore, "source_state", lambda *a: record.copy())
    actions = []

    def remote(pub, spec, action, **kw):
        actions.append(action)
        return {}

    monkeypatch.setattr(pi_release, "remote", remote)

    def admission(pub, spec, mac, artifact, *, remote):
        assert artifact["sha256"] == "original"
        remote(pub, spec, "observe")
        if stage == "rolled_back":
            with pytest.raises(ValueError, match="original recovery chain"):
                remote(pub, spec, "provision")
            remote(pub, spec, "provision", recovery={"baseRevision": 2})
            record["stage"] = "switching"
            remote(pub, spec, "observe")
        return {"ready": True, "authenticatedSubjects": 2}

    monkeypatch.setattr(pi_local, "admission", admission)
    result = restore.old_transaction(args)
    assert result["status"] == ("pi_host_restored" if apply else "planned")
    assert not result["pushed"] and not result["centralDeployed"]
    assert actions.count("provision") == int(apply and stage == "rolled_back")


def test_closed_plan_reads_authoritative_rollback_and_leaves_history_untouched(tmp_path, monkeypatch):
    spec = {"runtimeDir": str(tmp_path)}
    pub = SimpleNamespace(release="0" * 12)
    actions = []
    monkeypatch.setattr(pi_local, "job_sets", lambda *args: ({}, {}))
    monkeypatch.setattr(pi_local, "check_predecessor", lambda *args: None)

    def checked(pub, spec, action, **kw):
        actions.append(action)
        return {"baseRevision": 4}

    monkeypatch.setattr(pi_release, "remote", checked)

    def original_contract(*args, recovery_remote):
        recovery_remote(pub, spec, "recovery_check", recovery={"attempt": "test"})
        pytest.fail("plan must finish before writing historical receipt")

    monkeypatch.setattr(nanobot_release, "retry_proof", original_contract)
    restore.validate_closed(pub, spec, {}, {}, {})
    assert actions == ["recovery_check"] and not list(tmp_path.iterdir())
    monkeypatch.setattr(nanobot_release, "retry_proof", lambda *a, **kw: None)
    with pytest.raises(ValueError, match="no reviewed identity recovery chain"):
        restore.validate_closed(pub, spec, {}, {}, {})
    monkeypatch.setattr(nanobot_release, "retry_proof", original_contract)
    monkeypatch.setattr(pi_release, "remote", lambda *a, **kw: {"baseRevision": 3})
    with pytest.raises(ValueError, match="revoked Pi revision"):
        restore.validate_closed(pub, spec, {}, {}, {})
