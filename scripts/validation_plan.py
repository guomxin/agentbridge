"""Shared Full validation plan and per-run evidence for both maintenance entrypoints.

Only this runner executes the formal plan. Browser acceptance remains opt-in;
unclassified Node tests fail closed instead of silently disappearing from Full.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

WORKSPACE_NODE = (
    "tests/test_workspace_gateway_events.mjs",
    "tests/test_workspace_gateway_run_guard.mjs",
    "tests/test_workspace_gateway_client.mjs",
    "tests/test_workspace_card_messages.mjs",
    "tests/test_workspace_progress.mjs",
    "tests/test_workspace_query_groups.mjs",
)
BROWSER_FIXTURES = ("tests/test_workspace_markdown.browser.js",)
CHECKS = ["browser-runtime", "public-content", "current-facts", "python-full", "compileall",
          "pip-check", "workspace-node", "openclaw", "openclaw-pack", "installed-wheel"]
EVIDENCE_VERSION = 2


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def plan(root, *, mcp_app=False):
    root = Path(root).resolve()
    patterns = ("test_*.mjs", "test_*.js", "*.test.mjs", "*.test.js")
    discovered = {p.relative_to(root).as_posix() for pattern in patterns
                  for p in (root / "tests").rglob(pattern)}
    registered = set(WORKSPACE_NODE) | set(BROWSER_FIXTURES)
    if discovered != registered:
        raise ValueError("Validation test classification mismatch; unclassified="
                         + repr(sorted(discovered - registered)) + "; missing="
                         + repr(sorted(registered - discovered)))
    python_tests = sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))
    plugin_tests = sorted(p.relative_to(root).as_posix() for p in
                          (root / "integrations/openclaw-agentbridge/test").glob("*.test.js"))
    plugin_discovered = {p.relative_to(root).as_posix() for pattern in patterns
                         for p in (root / "integrations/openclaw-agentbridge/test").rglob(pattern)}
    if plugin_discovered != set(plugin_tests):
        raise ValueError("OpenClaw tests outside npm test inventory; classify them in the shared plan")
    if not python_tests or not plugin_tests:
        raise ValueError("Full validation requires Python and OpenClaw test inventories")
    # Resolving a venv's python symlink would silently leave that environment.
    python = str(Path(sys.executable).absolute())
    node = shutil.which("node") or "node"
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    stages = []

    def add(name, command, *, cwd=".", files=(), report=None, report_kind="junit", timeout=900):
        stages.append({"id": name, "command": command, "cwd": cwd, "testFiles": list(files),
                       "report": report, "reportKind": report_kind, "timeoutSeconds": timeout})

    add("browser-runtime", [python, "-m", "playwright", "install", "chromium"])
    add("public-content", [python, "scripts/check_public_content.py"])
    add("current-facts", [python, "scripts/current_facts.py", "--check"])
    add("python-full", [python, "-m", "pytest", "-q", "-n", "4", "--dist", "loadscope",
                        "--junitxml=output/release-validation/pytest.xml"],
        files=python_tests, report="output/release-validation/pytest.xml", timeout=3600)
    add("compileall", [python, "-m", "compileall", "-q", "bscli"])
    add("pip-check", [python, "-m", "pip", "check"])
    add("workspace-node", [node, "--test", "--test-reporter=junit",
                           "--test-reporter-destination=output/release-validation/workspace-node.xml", *WORKSPACE_NODE],
        files=WORKSPACE_NODE, report="output/release-validation/workspace-node.xml")
    # Run the same files as npm test with a machine-readable test report.
    add("openclaw", [node, "--test", "--test-reporter=junit",
                     "--test-reporter-destination=../../output/release-validation/openclaw.xml",
                     *[str(Path(p).relative_to("integrations/openclaw-agentbridge")) for p in plugin_tests]],
        cwd="integrations/openclaw-agentbridge", files=plugin_tests,
        report="output/release-validation/openclaw.xml", timeout=1800)
    # The runner's explicit Node invocation must retain npm test's exact meaning.
    package = read(root / "integrations/openclaw-agentbridge/package.json")
    if package.get("scripts", {}).get("test") != "node --test test/*.test.js":
        raise ValueError("OpenClaw npm test changed; update the shared validation plan")
    add("openclaw-pack", [npm, "run", "pack:check"], cwd="integrations/openclaw-agentbridge")
    if mcp_app:
        add("mcp-app-check", [npm, "run", "check"], cwd="integrations/mcp-app")
        add("mcp-app-build", [npm, "run", "build"], cwd="integrations/mcp-app")
    add("installed-wheel", [python, "scripts/agentbridge_artifact.py", "build", "--root", str(root),
                            "--output", str(root / "output/release-validation/runs/{runId}/artifact")],
        report="output/release-validation/runs/{runId}/artifact/artifact.json", report_kind="artifact")
    return {"schema": "agentbridge.validation-plan.v1", "options": {"mcpApp": bool(mcp_app)}, "stages": stages,
            "groups": {"workspaceNode": list(WORKSPACE_NODE), "python": python_tests,
                       "openclaw": plugin_tests,
                       "browserAcceptance": {"mode": "opt-in", "files": [*BROWSER_FIXTURES,
                            "tests/test_admin_frontend_browser.py"],
                            "pythonEnabled": os.environ.get("AGENTBRIDGE_BROWSER_TESTS") == "1",
                            "manualFixtures": list(BROWSER_FIXTURES)}}}


def test_report(path):
    cases = list(ET.parse(path).getroot().iter("testcase"))
    if not cases:
        raise ValueError("Test report contains no test cases")
    failed = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    skipped = [{"test": c.get("classname", "") + "." + c.get("name", ""),
                "reason": c.find("skipped").get("message", "")} for c in cases if c.find("skipped") is not None]
    return {"cases": len(cases), "passed": len(cases) - len(failed) - len(skipped),
            "failed": len(failed), "skipped": skipped}


def evidence_path(receipt, run_id, stage_id):
    # IDs originate only from the shared plan and a fresh UUID in begin().
    return Path(receipt).parent / "runs" / run_id / (stage_id + ".json")


def resolve(value, run_id):
    return value.replace("{runId}", run_id)


def execute_stage(root, receipt, stage, *, env=None, run_id=None):
    """Execute one registered stage; nonzero/timeout/invalid reports remain failed evidence."""
    root, receipt = Path(root).resolve(), Path(receipt)
    current = read(receipt)
    if run_id is not None and current.get("runId") != run_id:
        raise ValueError("Another Full validation replaced this run; restart Full validation")
    if current.get("status") != "running" or stage not in current.get("plan", {}).get("stages", []):
        raise ValueError("Stage requires this run's formal validation plan")
    path = evidence_path(receipt, current["runId"], stage["id"])
    if path.exists():
        raise ValueError("Stage already attempted; restart Full validation")
    # A stale report is removed before launching its producer, never after.
    report_name = resolve(stage["report"], current["runId"]) if stage["report"] else None
    report = root / report_name if report_name else None
    if report:
        report.unlink(missing_ok=True)
        if stage["reportKind"] != "artifact":
            report.parent.mkdir(parents=True, exist_ok=True)
    command = [resolve(value, current["runId"]) for value in stage["command"]]
    record = {"schema": "agentbridge.validation-stage.v1", "runId": current["runId"],
              "stage": stage, "command": command,
              "status": "running", "startedAt": stamp(), "exitCode": None}
    save(path, record)
    start = time.monotonic()
    output = b""
    try:
        process = subprocess.run(command, cwd=root / stage["cwd"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=stage["timeoutSeconds"], check=False)
        output = process.stdout
        record["exitCode"] = process.returncode
        if report and report.is_file():
            summary = test_report(report) if stage["reportKind"] == "junit" else {"schema": read(report)["schema"]}
            record["report"] = {"path": report_name, "sha256": digest(report), "summary": summary}
        if process.returncode:
            raise RuntimeError(f"Validation stage {stage['id']} failed (exit {process.returncode})")
        if report:
            if not report.is_file():
                raise FileNotFoundError("Validation stage did not produce its report: " + stage["id"])
            summary = record["report"]["summary"]
            if stage["reportKind"] == "junit" and (summary["failed"] or summary["passed"] == 0):
                raise ValueError("Test report has failures or no passing tests")
        record["status"] = "succeeded"
    except BaseException as exc:
        record["status"] = "failed"
        record["failureType"] = type(exc).__name__
        if isinstance(exc, subprocess.TimeoutExpired):
            output = exc.stdout or b""
        raise
    finally:
        # Reports contain synthetic fixtures. Child stdout may contain sensitive
        # diagnostics, so retain a hash/size rather than copying it into CI artifacts.
        record["output"] = {"sha256": hashlib.sha256(output).hexdigest(), "bytes": len(output)}
        if record["status"] == "failed" and output:
            # Keep useful failure diagnostics outside the directory uploaded by
            # CI. mkdtemp is private; never expose child output in exceptions.
            directory = Path(tempfile.mkdtemp(prefix="agentbridge-validation-failure-"))
            diagnostic = directory / (stage["id"] + ".log")
            with diagnostic.open("xb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(output)
            record["diagnosticLog"] = str(diagnostic)
        record["elapsedSeconds"] = round(time.monotonic() - start, 3)
        record["finishedAt"] = stamp()
        save(path, record)
        print(json.dumps({"stage": stage["id"], "status": record["status"],
                          "exitCode": record["exitCode"], "elapsedSeconds": record["elapsedSeconds"],
                          "tests": record.get("report", {}).get("summary"),
                          "diagnosticLog": record.get("diagnosticLog")}, ensure_ascii=True), flush=True)


def verify_stages(root, receipt, data):
    """Verify actual reports and immutable stage records, not merely their names."""
    if data.get("evidenceVersion") != EVIDENCE_VERSION or not data.get("runId"):
        raise ValueError("Legacy/incomplete validation receipt; rerun Full validation")
    if data.get("plan") != plan(root, mcp_app=data.get("plan", {}).get("options", {}).get("mcpApp", False)):
        raise ValueError("Validation plan changed; rerun Full validation")
    records = []
    for stage in data["plan"]["stages"]:
        path = evidence_path(receipt, data["runId"], stage["id"])
        try:
            record = read(path)
        except (OSError, ValueError) as exc:
            raise ValueError("Missing stage evidence: " + stage["id"] + "; rerun Full validation") from exc
        if (record.get("runId") != data["runId"] or record.get("stage") != stage
                or record.get("command") != [resolve(value, data["runId"]) for value in stage["command"]]
                or record.get("status") != "succeeded" or record.get("exitCode") != 0):
            raise ValueError("Failed or stale stage evidence: " + stage["id"])
        if stage["report"]:
            report_name = resolve(stage["report"], data["runId"])
            report = Path(root) / report_name
            evidence = record.get("report", {})
            if (not report.is_file() or evidence.get("path") != report_name
                    or evidence.get("sha256") != digest(report)
                    or evidence.get("summary") != (test_report(report) if stage["reportKind"] == "junit"
                                                     else {"schema": read(report)["schema"]})):
                raise ValueError("Test report changed or incomplete: " + stage["id"])
            if stage["reportKind"] == "junit" and (evidence["summary"]["failed"] or evidence["summary"]["passed"] == 0):
                raise ValueError("Test report has no passing tests: " + stage["id"])
        records.append({"id": stage["id"], "path": str(path), "sha256": digest(path)})
    if data.get("status") == "succeeded" and data.get("stageEvidence") != records:
        raise ValueError("Validation stage evidence changed; rerun Full validation")
    return records


@contextmanager
def test_environment():
    # Never pass a production session key into tests, including Windows CI.
    with tempfile.TemporaryDirectory(prefix="agentbridge-validation-") as folder:
        directory = Path(folder).resolve()
        key = directory / "session.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        yield {**os.environ, "AGENTBRIDGE_SESSION_KEY_FILE": str(key), "TMPDIR": str(directory)}


def run_full(root, *, mcp_app=False):
    try:
        import agentbridge_artifact as artifact
    except ModuleNotFoundError:
        from scripts import agentbridge_artifact as artifact
    root = Path(root).resolve()
    receipt = root / "output/release-validation/full.json"
    artifact.begin(root, receipt, mcp_app=mcp_app)
    current = read(receipt)
    stages = current["plan"]["stages"]
    with test_environment() as env:
        for stage in stages:
            execute_stage(root, receipt, stage, env=env, run_id=current["runId"])
    artifact.finish(root, receipt, run_id=current["runId"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["full", "show"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mcp-app", action="store_true")
    args = parser.parse_args()
    if args.action == "full":
        run_full(args.root, mcp_app=args.mcp_app)
    else:
        print(json.dumps(plan(args.root, mcp_app=args.mcp_app), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
