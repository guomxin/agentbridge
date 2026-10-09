"""Linux workflow contract and offline profile dispatch through real children."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest

from scripts import ci_validation

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_uses_linux_native_profiles_and_keeps_permissions_and_evidence():
    source = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    for required in (
        "name: repository-validation", "runs-on: ubuntu-24.04", "shell: bash",
        "contents: read", "actions: read", "persist-credentials: false", "fetch-depth: 0",
        "python scripts/ci_validation_plan.py", 'scripts/ci_validation.py --profile "$VALIDATION_PROFILE"',
        'python -m venv "$validation_venv"', "pytest pytest-xdist", "'setuptools>=77'",
        "--no-build-isolation -e '.[database-analysis]'", "playwright install --with-deps chromium",
        "if: steps.plan.outputs.profile == 'full'", "uses: actions/upload-artifact@v4", "if: always()",
        "path: output/release-validation/", "retention-days: 14",
    ):
        assert required in source
    for forbidden in ("shell: pwsh", "windows-latest", "Scripts/python.exe", "Invoke-AgentBridgeValidation.ps1",
                      "secrets.", "contents: write", "pull_request_target"):
        assert forbidden not in source
    assert source.count("GH_TOKEN:") == 1
    assert source.index("GH_TOKEN:") < source.index("- uses: actions/setup-node")
    assert "branches: [main]" in source and "pull_request:" in source and "workflow_dispatch:" in source


def test_workflow_bash_blocks_parse_without_running_installs():
    if not shutil.which("bash"):
        pytest.skip("Bash syntax checker is unavailable")
    lines = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8").splitlines()
    scripts = []
    for index, line in enumerate(lines):
        if line.strip() != "run: |":
            continue
        indent = len(line) - len(line.lstrip())
        block = []
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            block.append(following)
        scripts.append(textwrap.dedent("\n".join(block)))
    assert len(scripts) == 3
    for script in scripts:
        assert "set -euo pipefail" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


@pytest.fixture
def dispatch_root(tmp_path, monkeypatch):
    # These executables only record the dispatcher contract; no install, network,
    # production key, repository publication or Full artifact build is performed.
    root = tmp_path / "fixture"
    root.mkdir()
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", "/production/key/must-not-be-used")
    code = '''import json, os, sys
from pathlib import Path
key = Path(os.environ['AGENTBRIDGE_SESSION_KEY_FILE'])
row = {'script': Path(__file__).name, 'args': sys.argv[1:], 'key': str(key),
       'keyBytes': len(key.read_bytes()), 'keyMode': key.stat().st_mode & 0o777}
with Path('dispatch.jsonl').open('a') as out:
    out.write(json.dumps(row) + '\\n')
if os.environ.get('TEST_FAIL_STAGE') == row['script']:
    raise SystemExit(13)
'''
    for name in ("native/cli.py", "check_public_content.py", "current_facts.py", "validation_plan.py"):
        path = root / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
    return root


def records(root):
    return [json.loads(line) for line in (root / "dispatch.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("profile", ["docs", "release"])
def test_scoped_profile_uses_native_targeted_and_isolated_key(dispatch_root, profile):
    ci_validation.run_profile(profile, dispatch_root)
    calls = records(dispatch_root)
    assert [c["script"] for c in calls] == ["cli.py", "check_public_content.py", "current_facts.py"]
    assert calls[0]["args"][0] == "validate" and "--full" not in calls[0]["args"]
    tests = calls[0]["args"][2::2]
    expected = ["tests/test_documentation.py"]
    if profile == "release":
        expected += list(ci_validation.RELEASE_TESTS)
        assert "tests/test_release_transaction.py" in tests
        assert "tests/test_native_maintenance.py" in tests
    assert tests == expected
    assert calls[-1]["args"] == ["--check"]
    assert {c["keyBytes"] for c in calls} == {32}
    assert {c["keyMode"] for c in calls} == {stat.S_IRUSR | stat.S_IWUSR}
    assert len({c["key"] for c in calls}) == 1
    assert not Path(calls[0]["key"]).exists()
    assert not (dispatch_root / "output/release-validation/full.json").exists()


def test_full_profile_uses_shared_full_runner(dispatch_root):
    ci_validation.run_profile("full", dispatch_root)
    calls = records(dispatch_root)
    assert len(calls) == 1
    assert calls[0]["script"] == "validation_plan.py"
    assert calls[0]["args"] == ["full", "--root", str(dispatch_root.resolve())]


@pytest.mark.parametrize("failure", ["cli.py", "check_public_content.py", "current_facts.py"])
def test_scoped_failure_stops_remaining_checks_and_cleans_key(dispatch_root, monkeypatch, failure):
    monkeypatch.setenv("TEST_FAIL_STAGE", failure)
    with pytest.raises(subprocess.CalledProcessError) as error:
        ci_validation.run_profile("release", dispatch_root)
    assert error.value.returncode == 13
    calls = records(dispatch_root)
    assert calls[-1]["script"] == failure
    assert not Path(calls[0]["key"]).exists()


def test_unknown_profile_runs_nothing(dispatch_root):
    with pytest.raises(ValueError, match="Unknown validation profile"):
        ci_validation.run_profile("reuse", dispatch_root)
    assert not (dispatch_root / "dispatch.jsonl").exists()
