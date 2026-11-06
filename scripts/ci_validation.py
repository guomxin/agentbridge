"""Run independent Linux CI checks. The formal release receipt is produced only by local Full."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import uuid

try:
    from ci_validation_plan import HISTORY_PYTHON_TESTS, INDEPENDENT_PYTHON_TESTS, NODE_STAGES
    from validation_plan import CI_OUTPUT, CI_PYTEST_SCOPE, plan, resolve, test_environment, test_report
except ModuleNotFoundError:
    from scripts.ci_validation_plan import HISTORY_PYTHON_TESTS, INDEPENDENT_PYTHON_TESTS, NODE_STAGES
    from scripts.validation_plan import CI_OUTPUT, CI_PYTEST_SCOPE, plan, resolve, test_environment, test_report

RELEASE_TESTS = (
    "tests/test_release_policy.py",
    "tests/test_deployment_assets.py",
    "tests/test_release_transaction.py",
    "tests/test_release_artifact.py",
    "tests/test_native_maintenance.py",
)

def scoped_pytest(profile, output, tests):
    report = f"{output}/pytest.xml"
    stage_id = "python-tests" if profile == "python-tests" else f"python-{profile}"
    return {"id": stage_id, "report": report, "reportKind": "junit",
            "command": [sys.executable, "-m", "pytest", "-q", *CI_PYTEST_SCOPE,
                        f"--junitxml={report}", "tests/test_documentation.py", *tests]}


def ci_stages(profile, root, *, output=CI_OUTPUT):
    pytest_suites = {"docs": (), "release": RELEASE_TESTS, "python-tests": INDEPENDENT_PYTHON_TESTS}
    if profile in pytest_suites:
        return [
            scoped_pytest(profile, output, pytest_suites[profile]),
            {"id": "public-content", "command": [sys.executable, "scripts/check_public_content.py"]},
        ]
    if profile not in {"full", "node-tests", "node-tests-docs", "workspace-ui"}:
        raise ValueError("Unknown validation profile")
    stages = plan(root, for_ci=True, ci_output=output)["stages"]
    if profile == "full":
        return stages
    if profile in {"node-tests", "node-tests-docs"}:
        checks = [s for s in stages if s["id"] in NODE_STAGES or s["id"] == "public-content"]
        if profile == "node-tests-docs":
            checks.insert(0, scoped_pytest("docs", output, ()))
        return checks
    selected = {"public-content", "workspace-node", "installed-wheel"}
    return [scoped_pytest(profile, output, HISTORY_PYTHON_TESTS),
            *(s for s in stages if s["id"] in selected)]


def run_stages(stages, root, *, stream=None):
    root = Path(root).resolve()
    run_id = uuid.uuid4().hex
    with test_environment() as env:
        env.update(AGENTBRIDGE_BROWSER_TESTS="0", PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1")
        for stage in stages:
            print(f"CI: {stage['id']}", flush=True, file=stream)
            report = root / resolve(stage["report"], run_id) if stage.get("report") else None
            if report:
                if stage["reportKind"] == "junit":
                    report.parent.mkdir(parents=True, exist_ok=True)
                report.unlink(missing_ok=True)
            command = [resolve(value, run_id) for value in stage["command"]]
            subprocess.run(command, cwd=root / stage.get("cwd", "."), env=env, check=True,
                           timeout=stage.get("timeoutSeconds", 900), stdout=stream,
                           stderr=subprocess.STDOUT if stream is not None else None)
            if report and stage["reportKind"] == "junit":
                summary = test_report(report)
                if summary["failed"] or not summary["passed"]:
                    raise ValueError(f"CI stage {stage['id']} has failures or no passing tests")
            elif report and not report.is_file():
                raise FileNotFoundError(f"CI stage {stage['id']} did not produce {report}")


def run_profile(profile, root):
    run_stages(ci_stages(profile, root), root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=["docs", "release", "python-tests", "node-tests", "node-tests-docs", "workspace-ui", "full"])
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("CI validation requires macOS/Linux")
    run_profile(args.profile, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
