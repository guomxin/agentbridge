"""Isolated release rehearsal. Every supervisor/SSH action in these tests is synthetic."""

import fcntl
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/native"))
import nanobot_release as host
import nanobot_remote as central
import nanobot_tunnel as forwarding

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.tasks import TaskHubStore
from agentbridge.workspace.stores import WorkspaceStore


def spec_for(root):
    return {
        "runtimeDir": str(root),
        "instanceId": "pilot-test",
        "routeRef": "pilot-route",
        "displayName": "办公试点",
        "adapterVersion": "0.2.0",
        "listenPort": 49421,
        "remotePort": 49422,
        "centralMcpUrl": "https://central.example:8790/mcp",
        "trustedCardOrigins": ["https://central.example:8780"],
        "serviceTokenFile": str(root / "service.token"),
        "modelConfigFile": str(root / "model.json"),
        "modelOAuthTokenFile": str(root / "model.oauth"),
        "enginePython": str(root / "engine/bin/python"),
        "tunnelIdentityFile": str(root / "tunnel/forward-only"),
        "tunnelPublicKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "subjects": {"alice": "pilot-alice", "bob": "pilot-bob"},
        "telegram": {
            "botId": 1088,
            "username": "pilot_bot",
            "botCredential": {
                "kind": "macos-keychain",
                "service": "fixture",
                "account": "test",
            },
        },
    }


def mac_for():
    return {
        "port": 18789,
        "caCertificate": "/tmp/synthetic-ca",
        "tunnel": {
            "host": "central.example",
            "remotePort": 18789,
            "knownHostsFile": "/tmp/synthetic-known",
            "identityFile": "/tmp/synthetic-key",
            "user": "pilot",
        },
    }


def private(path, data):
    path.write_text(data)
    path.chmod(0o600)
    return path


def synthetic_admission(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    home = root / "data"
    home.mkdir()
    db_path = home / "agentbridge.db"
    user = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(central.pwd, "getpwnam", lambda _: user)
    # The only paths and identities seen by the helper are in this pytest directory.
    monkeypatch.setenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", "")
    tasks = TaskHubStore(db_path, maintain_on_startup=False)
    ws = WorkspaceStore(db_path)
    ids = McpIdentityTokenStore(db_path)
    spec = spec_for(root)
    actor_tokens = {}
    for subject in spec["subjects"]:
        endpoint, _ = tasks.ensure_endpoint(
            user_subject=subject,
            agent_host="openclaw",
            endpoint_key=subject,
            account_id=subject,
            external_subject=subject,
            token_id=subject,
            conversation_ref=subject,
            client_type="web",
        )
        with ws._connect() as db:
            db.execute(
                "INSERT INTO workspace_accounts (account_id,username,password_hash,user_subject,endpoint_id,"
                "endpoint_key,openclaw_session_key,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'active','t','t')",
                (
                    subject,
                    subject,
                    "unused",
                    subject,
                    endpoint["endpoint_id"],
                    subject,
                    subject,
                ),
            )
        actor_tokens[subject] = ids.issue(
            user_subject=subject, expected_principal_ref=subject
        )["token"]
    config = root / "config" / "nanobot-pilot-test"
    config.mkdir(parents=True, mode=0o700)
    private(
        config / "routes.json",
        json.dumps(
            {
                "routes": {
                    "pilot-route": {
                        "url": "http://127.0.0.1:49422",
                        "instanceId": "pilot-test",
                        "tokenFile": str(config / "service.token"),
                        "adapterVersion": "0.2.0",
                        "subjects": spec["subjects"],
                    }
                }
            }
        ),
    )
    args = {"root": str(root), "spec": host.metadata(spec)}
    return spec, args, ws, ids, actor_tokens, config


def test_remote_provision_issues_two_distinct_identities_preserves_defaults_and_rolls_back(
    tmp_path, monkeypatch
):
    spec, args, ws, ids, actor_tokens, config = synthetic_admission(tmp_path, monkeypatch)
    result = central.execute({**args, "action": "bind"})
    assert len(result["bindings"]) == 2
    tokens = [r["personalCredential"] for r in result["bindings"]]
    assert len(set(tokens)) == 2 and not set(tokens).intersection(actor_tokens.values())
    assert {ids.verify(t)["user_subject"] for t in tokens} == set(spec["subjects"])
    assert all(
        ws.hosts.choices(subject)["default_instance_id"] == "openclaw-default"
        for subject in spec["subjects"]
    )
    again = central.execute({**args, "action": "bind"})
    assert again == result
    assert all(
        (config / (ref + ".token")).stat().st_mode & 0o077 == 0
        for ref in spec["subjects"].values()
    )
    with ws._connect() as db:
        db.execute(
            "UPDATE workspace_host_directory_audit SET actor='another-admin' WHERE instance_id=?",
            (spec["instanceId"],),
        )
    with pytest.raises(RuntimeError, match="administrator changed"):
        central.execute({**args, "action": "rollback_scope"})
    assert all(ids.verify(t) is not None for t in tokens)
    with ws._connect() as db:
        db.execute(
            "UPDATE workspace_host_directory_audit SET actor='native-publish' WHERE instance_id=?",
            (spec["instanceId"],),
        )
    assert central.execute({**args, "action": "rollback_scope"}) == {"closed": True}
    assert all(ids.verify(t) is None for t in tokens)
    assert all(ids.verify(t) is not None for t in actor_tokens.values())
    with pytest.raises(ValueError, match="cannot override"):
        central.execute({**args, "action": "bind"})


def test_host_jobs_are_independent_and_only_forward_loopback(tmp_path):
    spec = spec_for(tmp_path)
    jobs = host.jobs(spec, mac_for())
    assert set(jobs) == {
        "ai.agentbridge.nanobot.pilot-test." + name
        for name in ("worker", "tunnel", "telegram")
    }
    tunnel = jobs["ai.agentbridge.nanobot.pilot-test.tunnel"]["ProgramArguments"]
    assert tunnel[tunnel.index("-i") + 1] == spec["tunnelIdentityFile"]
    assert tunnel[-1] == host.tunnel_user(spec["instanceId"]) + "@central.example"
    assert "127.0.0.1:49422:127.0.0.1:49421" in tunnel
    assert (
        "StrictHostKeyChecking=yes" in tunnel and "ExitOnForwardFailure=yes" in tunnel
    )
    worker = jobs["ai.agentbridge.nanobot.pilot-test.worker"]["ProgramArguments"]
    assert worker[0] == spec["enginePython"] and "nanobot_agentbridge.server" in worker
    assert (
        "nanobot_agentbridge.telegram"
        in jobs["ai.agentbridge.nanobot.pilot-test.telegram"]["ProgramArguments"]
    )
    assert "botCredential" not in str(host.metadata(spec))
    assert set(host.metadata(spec)["telegram"]) == {"botId", "username"}


def test_dedicated_ssh_identity_allows_only_the_reviewed_reverse_port(tmp_path):
    spec = spec_for(tmp_path)
    user, home, policy, body, authorized = forwarding.tunnel_settings(spec)
    assert (
        user.startswith("abnb-") and user != "root" and home == Path("/var/lib") / user
    )
    assert f"Match User {user}" in body
    assert (
        policy["PermitListen"] == "127.0.0.1:49422"
        and policy["AllowTcpForwarding"] == "remote"
    )
    assert (
        policy["PermitOpen"] == "none" and policy["AllowStreamLocalForwarding"] == "no"
    )
    assert policy["MaxSessions"] == "0" and policy["GatewayPorts"] == "no"
    assert policy["AuthorizedKeysFile"] == str(home / "authorized_keys")
    assert (
        'command="/usr/bin/false"' in authorized
        and 'permitlisten="127.0.0.1:49422"' in authorized
    )
    with pytest.raises(ValueError, match="public identity"):
        forwarding.tunnel_settings({**spec, "tunnelPublicKey": "ssh-ed25519 Zm9v"})


def test_forwarding_installs_public_key_after_effective_policy_and_revokes_only_it(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    spec = spec_for(tmp_path)
    user, _, options, body, key = forwarding.tunnel_settings(spec)
    home = tmp_path / "controlled"
    dropins = tmp_path / "dropins"
    dropins.mkdir()
    monkeypatch.setattr(
        forwarding, "tunnel_settings", lambda _: (user, home, options, body, key)
    )
    original_path = Path
    monkeypatch.setattr(
        forwarding,
        "Path",
        lambda path: (
            dropins if path == "/etc/ssh/sshd_config.d" else original_path(path)
        ),
    )
    monkeypatch.setattr(forwarding.os, "geteuid", lambda: 0)
    monkeypatch.setattr(forwarding.pwd, "getpwnam", Mock(side_effect=KeyError(user)))
    monkeypatch.setattr(
        forwarding,
        "effective",
        lambda subject: (
            {k.lower(): v for k, v in options.items()}
            if subject == user
            else {"permanent": "root"}
        ),
    )
    calls = Mock()
    monkeypatch.setattr(forwarding, "run_system", calls)
    assert forwarding.restricted_tunnel(spec, "enable")["tunnelAuthorized"] is True
    assert (home / "authorized_keys").read_text() == key
    assert [c.args[:2] for c in calls.call_args_list] == [
        ("/usr/sbin/useradd", "--system"),
        ("/usr/sbin/sshd", "-t"),
        ("/usr/bin/systemctl", "reload"),
    ]
    # Root-owned paths on the real Linux control plane; the isolated test has the local user's UID.
    monkeypatch.setattr(
        forwarding,
        "secure_regular",
        lambda p, expected: not p.is_symlink() and p.read_text() == expected,
    )
    real_home_stat = Path.stat

    def fake_owner(path, *args, **kwargs):
        info = real_home_stat(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0) if path == home else info

    monkeypatch.setattr(Path, "stat", fake_owner)
    assert forwarding.restricted_tunnel(spec, "disable")["tunnelAuthorized"] is False
    assert not (home / "authorized_keys").exists()
    assert (dropins / (user + ".conf")).read_text() == body


def test_synthetic_launch_verification_observes_stable_pids_and_channel_lock(
    tmp_path, monkeypatch
):
    spec = spec_for(tmp_path.resolve())
    labels = ("worker", "telegram")
    (tmp_path / "telegram").mkdir()
    lock = tmp_path / "telegram/poll.lock"
    lock.touch()
    monkeypatch.setattr(host.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(host, "launchd_info", lambda label: "state = running\npid = 5050")
    monkeypatch.setattr(host, "owned_resource", lambda args, pid, name: pid == 5050)
    with pytest.raises(RuntimeError, match="channel ownership"):
        host.wait_for_running_jobs(labels, spec)
    with lock.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        host.wait_for_running_jobs(labels, spec)
        monkeypatch.setattr(
            host, "launchd_info", lambda label: "state = spawn scheduled\nlast exit code = 1"
        )
        with pytest.raises(RuntimeError, match="stable processes"):
            host.wait_for_running_jobs(labels, spec)


def test_archive_reuses_exact_reviewed_sources_wheel_and_coordinator(
    tmp_path, monkeypatch
):
    tree = tmp_path / "synthetic repo"
    runtime = tmp_path / "managed Mac"
    tree.mkdir()
    for pkg in (host.BRIDGE, host.COORDINATOR):
        (tree / pkg).mkdir(parents=True)
        (tree / pkg / "__init__.py").touch()
    (tree / host.BRIDGE / "server.py").write_text("STATUS='fixture'\n")
    (tree / host.COORDINATOR / "bound_coordinator.py").write_text(
        "def main(): print('coordinator-ready')\n"
    )
    (tree / host.BRIDGE.parent / "pyproject.toml").write_text(
        '[project]\nversion="0.3.0"\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
    subprocess.run(["git", "add", "integrations"], cwd=tree, check=True)
    commit = subprocess.check_output(["git", "write-tree"], cwd=tree, text=True).strip()
    pkg = tmp_path / "fixture.whl"
    pkg.write_bytes(b"reviewed central client")
    spec = {
        **spec_for(runtime),
        "adapterVersion": "0.3.0",
        "coordinatorPython": sys.executable,
    }
    pub = SimpleNamespace(commit=commit, release=commit[:12])
    artifact = {"wheel": str(pkg), "sha256": host.sha(pkg.read_bytes())}
    monkeypatch.setattr(host, "ROOT", tree)
    monkeypatch.setattr(
        host,
        "run",
        lambda argv: subprocess.check_output(argv, cwd=tree, text=True).strip(),
    )
    archive = host.archive_bridge(pub, spec, artifact)
    assert (
        subprocess.check_output([archive / "coordinator"], text=True).strip()
        == "coordinator-ready"
    )
    assert host.archive_bridge(pub, spec, artifact) == archive
    (archive / "reference_host/unexpected.so").write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unexpected entry"):
        host.archive_bridge(pub, spec, artifact)


def test_mid_switch_failure_restores_previous_host_and_keeps_user_data(
    tmp_path, monkeypatch
):
    root = tmp_path.resolve()
    root.chmod(0o700)
    spec = spec_for(root)
    previous = root / "releases" / ("a" * 12)
    candidate = root / "releases" / ("b" * 12)
    for rel in (previous, candidate):
        rel.mkdir(mode=0o700, parents=True)
        private(rel / "host.json", "{}")
    host.set_current(root, previous)
    state_db = root / "user-state"
    state_db.write_text("retained")
    definitions = host.jobs(spec, mac_for())
    paths = {
        label: private(root / (label + ".plist"), "fixture") for label in definitions
    }
    loaded = set(definitions)
    history = []

    def supervised(ignored):
        return {
            label: (paths[label], b"fixture", label in loaded) for label in definitions
        }

    def control(action, *args):
        label = args[-1].rsplit("/", 1)[-1].removesuffix(".plist")
        history.append((action, label))
        if action == "bootout":
            loaded.discard(label)
        if action == "bootstrap":
            loaded.add(label)

    def probe(pub, spec):
        if (root / "current").resolve() == candidate:
            raise RuntimeError("synthetic route loss")
        return {"ready": True}

    monkeypatch.setattr(host, "managed_jobs", supervised)
    monkeypatch.setattr(host, "ctl", control)
    monkeypatch.setattr(host, "archive_bridge", lambda *_: candidate)
    monkeypatch.setattr(host, "configure_release", lambda *_: None)
    monkeypatch.setattr(
        host, "verify_subjects", lambda *_: {"authenticatedSubjects": 2}
    )
    monkeypatch.setattr(host, "remote", lambda *args, **kwargs: {"scopePresent": True, "portOccupied": False})
    monkeypatch.setattr(host, "launchd_info", lambda label: f"path = {paths[label]}" if label in loaded else None)
    monkeypatch.setattr(host, "local_port_free", lambda _: True)
    monkeypatch.setattr(host, "wait_for_running_jobs", lambda labels, _: set(labels) == loaded or pytest.fail())
    monkeypatch.setattr(host, "wait_for_route", probe)
    monkeypatch.setattr(host, "job_state", lambda label: label in loaded)
    pub = SimpleNamespace(release=candidate.name, args=SimpleNamespace(resume=False))
    with pytest.raises(RuntimeError, match="synthetic route loss"):
        host.admission(pub, spec, mac_for(), {})
    assert (root / "current").resolve() == previous
    assert set(loaded) == set(definitions)
    assert (
        json.loads((root / "native-release.json").read_text())["stage"] == "rolled_back"
    )
    assert state_db.read_text() == "retained"
    assert all(
        label.startswith("ai.agentbridge.nanobot.pilot-test.") for _, label in history
    )


def test_full_receipt_and_stable_scope_required_before_resuming(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    root.chmod(0o700)
    spec = spec_for(root)
    monkeypatch.setattr(host, "managed_jobs", lambda _: {})
    with pytest.raises(ValueError, match="confirmed"):
        host.admission(
            SimpleNamespace(args=SimpleNamespace(resume=True), release="f" * 12),
            spec,
            mac_for(),
            {},
        )
    private(
        root / "native-release.json",
        json.dumps({"stage": "switching", "candidate": "f" * 12}),
    )
    with pytest.raises(ValueError, match="interrupted"):
        host.admission(
            SimpleNamespace(args=SimpleNamespace(resume=False), release="f" * 12),
            spec,
            mac_for(),
            {},
        )
