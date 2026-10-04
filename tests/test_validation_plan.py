"""Offline evidence tests with real subprocesses and isolated Git candidates."""
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from scripts import agentbridge_artifact as artifact
from scripts import validation_plan as validation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tracked.txt").write_text("fixed candidate\n")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    artifact.git(root, "add", ".")
    artifact.git(root, "-c", "user.name=Validation Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", "fixture")
    commit = artifact.git(root, "rev-parse", "HEAD")
    receipt = root / "output/release-validation/full.json"
    # A real wheel build/install is covered by test_release_artifact. These
    # commands isolate orchestration and promotion failures without rebuilding it.
    wheel = root / "fixture.whl"
    wheel.write_bytes(b"fixture wheel")
    manifest = {"schema": "agentbridge.artifact.v1", "commit": commit,
                "wheel": str(wheel), "sha256": validation.digest(wheel)}
    stages = []
    for name in validation.CHECKS:
        report = None
        kind = "junit"
        code = "print('synthetic check passed')"
        if name in {"python-full", "workspace-node", "openclaw"}:
            report = "output/release-validation/" + ("pytest" if name == "python-full" else name) + ".xml"
            xml = '<testsuites><testcase name="ok"/><testcase name="optional"><skipped message="fixture condition"/></testcase></testsuites>'
            code = f"from pathlib import Path; Path({report!r}).write_text({xml!r})"
        if name == "installed-wheel":
            report = "output/release-validation/runs/{runId}/artifact/artifact.json"
            kind = "artifact"
            code = ("from pathlib import Path; p=Path(" + repr(report) + "); "
                    "p.parent.mkdir(parents=True); p.write_text(" + repr(json.dumps(manifest)) + ")")
        stages.append({"id": name, "command": [sys.executable, "-c", code], "cwd": ".",
                       "testFiles": ["fixture/test.py"] if report else [], "report": report,
                       "reportKind": kind, "timeoutSeconds": 5})
    plan = {"schema": "agentbridge.validation-plan.v1", "stages": stages, "groups": {}}
    with mock.patch.object(validation, "plan", return_value=plan), \
         mock.patch.object(artifact, "inputs", return_value={"commit": commit}):
        artifact.begin(root, receipt)
        yield root, receipt, plan


def execute(candidate):
    root, receipt, plan = candidate
    for stage in plan["stages"]:
        validation.execute_stage(root, receipt, stage)


def test_current_plan_includes_all_node_tests_and_explicit_browser_group():
    plan = validation.plan(ROOT)
    assert "tests/test_workspace_query_groups.mjs" in plan["groups"]["workspaceNode"]
    browser = plan["groups"]["browserAcceptance"]
    assert browser["mode"] == "opt-in"
    assert set(browser["manualFixtures"]).isdisjoint(plan["groups"]["workspaceNode"])
    assert [s["id"] for s in plan["stages"]] == validation.CHECKS
    assert plan["stages"][0]["command"][0] == str(Path(sys.executable).absolute())
    extended = validation.plan(ROOT, mcp_app=True)
    assert extended["options"]["mcpApp"] is True
    assert [s["id"] for s in extended["stages"]][-3:] == ["mcp-app-check", "mcp-app-build", "installed-wheel"]


@pytest.mark.parametrize("change", ["new", "missing"])
def test_unclassified_or_deleted_node_tests_reject_full_plan(tmp_path, change):
    for name in (*validation.WORKSPACE_NODE, *validation.BROWSER_FIXTURES):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.touch()
    if change == "new":
        (tmp_path / "tests/test_future.mjs").touch()
    else:
        (tmp_path / validation.WORKSPACE_NODE[0]).unlink()
    with pytest.raises(ValueError, match="classification mismatch"):
        validation.plan(tmp_path)


def test_real_stage_success_promotes_and_records_skips(candidate):
    root, receipt, plan = candidate
    execute(candidate)
    artifact.finish(root, receipt)
    data = validation.read(receipt)
    assert artifact.verify(root, receipt)["wheel"] == str(root / "fixture.whl")
    assert data["checks"] == validation.CHECKS
    assert data["pythonTests"]["passed"] == 1
    assert data["pythonTests"]["skipped"][0]["reason"] == "fixture condition"
    assert len(data["stageEvidence"]) == len(plan["stages"])
    first = validation.read(data["stageEvidence"][0]["path"])
    assert first["command"] == plan["stages"][0]["command"]
    assert first["exitCode"] == 0 and first["output"]["bytes"] > 0


def test_failed_stage_and_missing_stage_cannot_promote(candidate):
    root, receipt, plan = candidate
    stage = plan["stages"][0]
    stage["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
    # begin freezes the modified registered plan before executing it.
    artifact.begin(root, receipt)
    with pytest.raises(RuntimeError, match="exit 7"):
        validation.execute_stage(root, receipt, stage)
    data = validation.read(receipt)
    evidence = validation.read(validation.evidence_path(receipt, data["runId"], stage["id"]))
    assert evidence["exitCode"] == 7 and evidence["status"] == "failed"
    with pytest.raises(ValueError, match="Failed or stale"):
        artifact.finish(root, receipt)
    assert validation.read(receipt)["status"] == "running"
    artifact.begin(root, receipt)
    with pytest.raises(ValueError, match="Missing stage"):
        artifact.finish(root, receipt)


def test_successful_command_cannot_reuse_old_test_report(candidate):
    root, receipt, plan = candidate
    stage = next(s for s in plan["stages"] if s["id"] == "python-full")
    stage["command"] = [sys.executable, "-c", "print('no report produced')"]
    artifact.begin(root, receipt)
    report = root / stage["report"]
    report.write_text('<testsuites><testcase name="stale"/></testsuites>')
    with pytest.raises(FileNotFoundError):
        validation.execute_stage(root, receipt, stage)
    assert not report.exists()
    data = validation.read(receipt)
    assert validation.read(validation.evidence_path(receipt, data["runId"], stage["id"]))["status"] == "failed"


@pytest.mark.parametrize("result", ["skipped", "failure"])
def test_zero_exit_cannot_hide_failed_or_all_skipped_tests(candidate, result):
    root, receipt, plan = candidate
    stage = next(s for s in plan["stages"] if s["id"] == "python-full")
    xml = f'<testsuites><testcase name="sample"><{result} message="fixture"/></testcase></testsuites>'
    stage["command"] = [sys.executable, "-c", f"from pathlib import Path; Path({stage['report']!r}).write_text({xml!r})"]
    artifact.begin(root, receipt)
    with pytest.raises(ValueError, match="failures or no passing"):
        validation.execute_stage(root, receipt, stage)
    data = validation.read(receipt)
    record = validation.read(validation.evidence_path(receipt, data["runId"], stage["id"]))
    assert record["exitCode"] == 0 and record["status"] == "failed"
    assert record["report"]["summary"]["passed"] == 0


def test_replaced_run_cannot_execute_or_finish(candidate):
    root, receipt, plan = candidate
    old = validation.read(receipt)["runId"]
    artifact.begin(root, receipt)
    with pytest.raises(ValueError, match="replaced this run"):
        validation.execute_stage(root, receipt, plan["stages"][0], run_id=old)
    with pytest.raises(ValueError, match="replaced this run"):
        artifact.finish(root, receipt, run_id=old)


@pytest.mark.parametrize("tamper", ["record", "report", "run", "missing"])
def test_changed_or_old_evidence_rejected(candidate, tamper):
    root, receipt, _ = candidate
    execute(candidate)
    artifact.finish(root, receipt)
    data = validation.read(receipt)
    path = validation.evidence_path(receipt, data["runId"], "python-full")
    if tamper == "report":
        (receipt.parent / "pytest.xml").write_text('<testsuites><testcase name="other"/></testsuites>')
    elif tamper == "missing":
        path.unlink()
    else:
        record = validation.read(path)
        record["runId" if tamper == "run" else "elapsedSeconds"] = "old-run" if tamper == "run" else 123
        validation.save(path, record)
    with pytest.raises(ValueError):
        artifact.verify(root, receipt)


def test_legacy_static_checks_receipt_requires_full_rerun(candidate):
    root, receipt, _ = candidate
    validation.save(receipt, {"schema": "agentbridge.validation.v1", "status": "succeeded",
                              "checks": validation.CHECKS, "skipped": []})
    with pytest.raises(ValueError, match="Legacy/incomplete.*rerun Full"):
        artifact.verify(root, receipt)


def test_runner_stops_after_failure_without_finish(candidate):
    root, _, plan = candidate
    plan["stages"][1]["command"] = [sys.executable, "-c", "raise SystemExit(3)"]
    with mock.patch.object(artifact, "finish") as finish:
        with pytest.raises(RuntimeError, match="exit 3"):
            validation.run_full(root)
    finish.assert_not_called()
