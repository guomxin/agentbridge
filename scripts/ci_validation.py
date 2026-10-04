"""Run the selected CI profile using the POSIX validation entrypoints.

Scope selection remains in ci_validation_plan.py. This entry neither publishes
nor has production credentials, and scoped checks never create a Full receipt.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

try:
    from validation_plan import test_environment
except ModuleNotFoundError:
    from scripts.validation_plan import test_environment

RELEASE_TESTS = (
    "tests/test_release_policy.py",
    "tests/test_deployment_assets.py",
    "tests/test_release_transaction.py",
    "tests/test_release_artifact.py",
    "tests/test_native_maintenance.py",
)


def commands(profile, root, python):
    if profile not in {"docs", "release", "full"}:
        raise ValueError("Unknown validation profile")
    if profile == "full":
        return [[python, str(root / "scripts/validation_plan.py"), "full", "--root", str(root)]]
    tests = ["tests/test_documentation.py"]
    if profile == "release":
        tests.extend(RELEASE_TESTS)
    targeted = [python, str(root / "scripts/native/cli.py"), "validate"]
    for path in tests:
        targeted.extend(["--test", path])
    return [targeted, [python, str(root / "scripts/check_public_content.py")],
            [python, str(root / "scripts/current_facts.py"), "--check"]]


def run_profile(profile, root):
    root = Path(root).resolve()
    calls = commands(profile, root, sys.executable)
    # current_facts constructs temporary services even in docs-only checks. On
    # POSIX it needs an ephemeral key, just as the native test runner does.
    with test_environment() as environment:
        for command in calls:
            subprocess.run(command, cwd=root, env=environment, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=["docs", "release", "full"])
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("CI validation targets macOS/Linux; use the legacy Windows entrypoint separately")
    run_profile(args.profile, Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
