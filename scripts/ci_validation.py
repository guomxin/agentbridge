"""Run independent Linux CI checks. The formal release receipt is produced only by local Full."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import uuid

try:
    from validation_plan import CI_OUTPUT, CI_PYTEST_SCOPE, plan, resolve, test_environment, test_report
except ModuleNotFoundError:
    from scripts.validation_plan import CI_OUTPUT, CI_PYTEST_SCOPE, plan, resolve, test_environment, test_report

RELEASE_TESTS = (
    "tests/test_release_policy.py",
    "tests/test_deployment_assets.py",
    "tests/test_release_transaction.py",
    "tests/test_release_artifact.py",
    "tests/test_native_maintenance.py",
)

def ci_stages(profile, root):
    if profile == "full":
        return plan(root, for_ci=True)["stages"]
    if profile not in {"docs", "release"}:
        raise ValueError("Unknown validation profile")
    tests = ["tests/test_documentation.py"]
    if profile == "release":
        tests.extend(RELEASE_TESTS)
    report = f"{CI_OUTPUT}/pytest.xml"
    return [
        {"id": f"python-{profile}", "report": report, "reportKind": "junit",
         "command": [sys.executable, "-m", "pytest", "-q", *CI_PYTEST_SCOPE,
                     f"--junitxml={report}", *tests]},
        {"id": "public-content", "command": [sys.executable, "scripts/check_public_content.py"]},
        {"id": "current-facts", "command": [sys.executable, "scripts/current_facts.py", "--check"]},
    ]


def run_profile(profile, root):
    root = Path(root).resolve()
    stages = ci_stages(profile, root)
    (root / CI_OUTPUT).mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    with test_environment() as env:
        env.update(AGENTBRIDGE_BROWSER_TESTS="0", PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1")
        for stage in stages:
            print(f"CI: {stage['id']}", flush=True)
            report = root / resolve(stage["report"], run_id) if stage.get("report") else None
            if report:
                report.unlink(missing_ok=True)
            command = [resolve(value, run_id) for value in stage["command"]]
            subprocess.run(command, cwd=root / stage.get("cwd", "."), env=env, check=True,
                           timeout=stage.get("timeoutSeconds", 900))
            if report and stage["reportKind"] == "junit":
                summary = test_report(report)
                if summary["failed"] or not summary["passed"]:
                    raise ValueError(f"CI stage {stage['id']} has failures or no passing tests")
            elif report and not report.is_file():
                raise FileNotFoundError(f"CI stage {stage['id']} did not produce {report}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=["docs", "release", "full"])
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("CI validation requires macOS/Linux")
    run_profile(args.profile, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
