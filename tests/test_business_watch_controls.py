"""Local observation controls use isolated ledgers, never production services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import pwd
import sqlite3
import threading

import pytest

from agentbridge.admin.stores import GovernancePolicyStore
from agentbridge.cli.main import build_parser, main
from agentbridge.core.business_watch_controls import BusinessWatchControlConflict, BusinessWatchControls
from agentbridge.core.business_watches import BusinessWatchStore


@pytest.fixture
def context(tmp_path):
    store = BusinessWatchStore(tmp_path / "agentbridge.db")
    return store, BusinessWatchControls(store.db_path)


def watch(store, owner="user-a", number=1, *, consent=True):
    return store.create(user_subject=owner, principal_ref="synthetic-principal-" + owner,
        system_instance="synthetic-oa", source={"collection": "done", "affair_id": str(number),
            "summary_id": str(number + 100), "process_id": str(number + 200), "case_id": str(number + 300)},
        title="PRIVATE_WATCH_TITLE", allow_mark_read=consent, include_private=True)[0]


def set_control(controls, **changes):
    return controls.set(**{**dict(scope="global", target="*", paused=True, expected_version=0,
        reason="synthetic maintenance", actor="synthetic-operator"), **changes})


def rows(store, table):
    with sqlite3.connect(store.db_path) as db:
        return db.execute("SELECT * FROM " + table).fetchall()


def test_cli_control_uses_os_operator_cas_and_does_not_construct_service(context, capsys, monkeypatch):
    store, controls = context
    initial = watch(store)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("local control must not initialize a service or load session keys")

    monkeypatch.setattr("agentbridge.cli.main.CentralCapabilityService", forbidden)
    prefix = ["--home", str(store.db_path.parent), "watch", "control", "--scope", "global", "--target", "*",
              "--reason", "synthetic CLI maintenance"]
    assert main(prefix + ["--paused", "--expected-version", "0"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded" and result["control"]["paused"] is True
    assert result["control"]["actor"] == pwd.getpwuid(os.geteuid()).pw_name
    assert controls.is_paused("user-a")
    assert store.get(initial["watch_id"], user_subject="user-a", include_private=True)["next_check_at"] is None
    assert main(prefix + ["--resume", "--expected-version", "0"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "WATCH_CONTROL_CONFLICT"
    assert main(prefix + ["--resume", "--expected-version", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["control"]["version"] == 2
    assert not controls.is_paused("user-a")
    events = rows(store, "business_watch_control_events")
    assert len(events) == 2 and {event[6] for event in events} == {pwd.getpwuid(os.geteuid()).pw_name}


@pytest.mark.parametrize("scope,target", [("global", "*"), ("system", "oa"), ("user", "user-a")])
def test_cli_parser_supports_explicit_pause_resume_and_scopes(scope, target):
    parser = build_parser()
    args = parser.parse_args(["watch", "control", "--scope", scope, "--target", target,
        "--expected-version", "0", "--pause", "--reason", "synthetic maintenance"])
    assert args.paused and args.scope == scope and args.target == target


@pytest.mark.parametrize("extra", [[], ["--paused", "--resume"], ["--paused", "--actor", "forged-user"]])
def test_control_parser_requires_one_mode_and_rejects_actor_override(extra):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["watch", "control", "--scope", "global", "--target", "*",
            "--expected-version", "0", "--reason", "synthetic maintenance", *extra])
    assert error.value.code == 2


def test_runtime_reads_existing_ledger_without_schema_or_watch_mutation(tmp_path, capsys, monkeypatch):
    store = BusinessWatchStore(tmp_path / "agentbridge.db")
    watch(store, number=1)
    watch(store, number=2, consent=False)
    expired = watch(store, number=3)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE business_watches SET expires_at=? WHERE watch_id=?",
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), expired["watch_id"]))
        before_schema = db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    before = rows(store, "business_watches")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime inspection must not run startup maintenance")

    monkeypatch.setattr("agentbridge.cli.main.CentralCapabilityService", forbidden)
    assert main(["--home", str(tmp_path), "watch", "runtime"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runtime"] == {"states": {"active": 2, "expired": 1}, "due": 1, "checks": {}, "controls": []}
    assert "PRIVATE_WATCH_TITLE" not in json.dumps(result)
    assert rows(store, "business_watches") == before
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before_schema
    with BusinessWatchControls(store.db_path, initialize=False)._connect(read_only=True) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE business_watches SET version=version+1")


def test_runtime_missing_home_does_not_create_state(tmp_path, capsys):
    absent = tmp_path / "absent-home"
    assert main(["--home", str(absent), "watch", "runtime"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "WATCH_RUNTIME_UNAVAILABLE"
    assert not absent.exists()


def test_scope_layers_resume_only_eligible_active_watches(context):
    store, controls = context
    a, b = watch(store), watch(store, "user-b")
    paused = watch(store, number=2)
    paused = store.update(paused["watch_id"], user_subject="user-a", expected_version=paused["version"], state="paused", include_private=True)
    set_control(controls, scope="user", target="user-a")
    assert controls.is_paused("user-a") and not controls.is_paused("user-b")
    assert store.get(b["watch_id"], user_subject="user-b", include_private=True) == b
    set_control(controls)
    set_control(controls, paused=False, expected_version=1)
    assert controls.is_paused("user-a") and not controls.is_paused("user-b")
    assert store.get(a["watch_id"], user_subject="user-a", include_private=True)["next_check_at"] is None
    set_control(controls, scope="user", target="user-a", paused=False, expected_version=1)
    assert not controls.is_paused("user-a")
    assert store.get(a["watch_id"], user_subject="user-a", include_private=True)["next_check_at"] is not None
    assert store.get(paused["watch_id"], user_subject="user-a", include_private=True) == paused
    set_control(controls, scope="system", target="oa")
    assert controls.is_paused("user-a") and controls.is_paused("user-b") and controls.summary()["due"] == 0


def test_pause_invalidates_inflight_check_and_resume_allows_new_lease(context):
    store, controls = context
    initial = watch(store)
    job = store.claim_due()[0]
    set_control(controls)
    before = store.get(initial["watch_id"], user_subject="user-a", include_private=True)
    result = store.finish_check(job["check_id"], job["lease_token"], error_code="WATCH_SESSION_BUSY")
    assert result["status"] == "discarded"
    assert store.get(initial["watch_id"], user_subject="user-a", include_private=True) == before
    assert rows(store, "business_watch_observations") == [] and store.claim_due() == []
    set_control(controls, paused=False, expected_version=1)
    assert len(store.claim_due()) == 1


def test_stale_cas_and_writer_failure_leave_audit_and_schedule_atomic(context):
    store, controls = context
    initial = watch(store)
    with sqlite3.connect(store.db_path) as db:
        db.execute("CREATE TRIGGER synthetic_control_failure BEFORE UPDATE ON business_watches "
                   "BEGIN SELECT RAISE(ABORT,'synthetic schedule failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        set_control(controls)
    assert rows(store, "business_watch_controls") == rows(store, "business_watch_control_events") == []
    assert store.get(initial["watch_id"], user_subject="user-a", include_private=True) == initial
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TRIGGER synthetic_control_failure")
    set_control(controls)
    before = rows(store, "business_watch_control_events")
    with pytest.raises(BusinessWatchControlConflict):
        set_control(controls, paused=False)
    assert rows(store, "business_watch_control_events") == before and controls.is_paused("user-a")


def test_real_threads_share_one_expected_control_version(context):
    store, _ = context
    watch(store)
    controls = [BusinessWatchControls(store.db_path) for _ in range(8)]
    barrier = threading.Barrier(8)

    def changing(index):
        barrier.wait(timeout=10)
        try:
            return set_control(controls[index])
        except BusinessWatchControlConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(changing, range(8)))
    assert sum(value is not None for value in result) == 1
    assert len(rows(store, "business_watch_control_events")) == 1


@pytest.mark.parametrize("changes", [{"scope": []}, {"target": " user-a "}, {"target": "oa", "scope": "global"},
    {"target": "other", "scope": "system"}, {"expected_version": True}, {"paused": "false"},
    {"reason": " "}, {"actor": "operator\nforged"}])
def test_invalid_control_does_not_append_audit(context, changes):
    store, controls = context
    with pytest.raises(ValueError):
        set_control(controls, **changes)
    assert rows(store, "business_watch_controls") == rows(store, "business_watch_control_events") == []


def test_observation_controls_are_independent_of_governed_write_pauses(context):
    store, controls = context
    policies = GovernancePolicyStore(store.db_path)
    policies.pause(scope_type="global", scope_value="*", reason="synthetic write pause", actor="synthetic-admin")
    before = policies.list()
    assert not controls.is_paused("user-a")
    set_control(controls)
    set_control(controls, paused=False, expected_version=1)
    assert policies.list() == before
