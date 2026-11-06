"""Linux workflow contract and offline profile dispatch through real children."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import textwrap

import pytest

from scripts import ci_validation, validation_plan

ROOT = Path(__file__).resolve().parents[1]


def test_workflow_uses_linux_native_profiles_and_keeps_permissions_and_evidence():
    source = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
    for required in (
        "name: repository-validation", "runs-on: ubuntu-24.04", "shell: bash",
        "contents: read", "actions: read", "persist-credentials: false", "fetch-depth: 0",
        "python scripts/ci_validation_plan.py", 'scripts/ci_validation.py --profile "$VALIDATION_PROFILE"',
        "- uses: actions/setup-node@v4\n        if: steps.plan.outputs.profile != 'docs' && steps.plan.outputs.profile != 'python-tests'",
        "- name: Prepare Python validation environment\n        if: steps.plan.outputs.profile != 'node-tests'",
        'python -m venv "$validation_venv"', '"${AGENTBRIDGE_TEST_PYTHON:-python}" scripts/ci_validation.py',
        "Run independent repository checks without a browser",
        "uses: actions/upload-artifact@v4", "if: always()", "output/ci-validation/ci-plan.json",
        "output/ci-validation/*.xml", "output/ci-validation/runs/*/artifact/artifact.json", "retention-days: 14",
    ):
        assert required in source
    for forbidden in ("shell: pwsh", "windows-latest", "Scripts/python.exe", "Invoke-AgentBridgeValidation.ps1",
                      "secrets.", "contents: write", "pull_request_target", "playwright install", "chromium",
                      "output/release-validation", "scripts/validation_plan.py full"):
        assert forbidden not in source
    assert source.count("GH_TOKEN:") == 1
    assert source.index("GH_TOKEN:") < source.index("- uses: actions/setup-node")
    assert "branches: [main]" in source and "pull_request:" in source and "workflow_dispatch:" in source


def test_workflow_bash_blocks_parse_without_running_installs(tmp_path, monkeypatch):
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
    assert len(scripts) == 2
    for script in scripts:
        assert "set -euo pipefail" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    # Record the pip arguments selected by the real workflow block without installing anything.
    fake_python = tmp_path / 'python'
    fake_python.write_text("""#!/bin/bash
set -eu
if [[ "$1 $2" == "-m venv" ]]; then
  mkdir -p "$3/bin"
  cp "$0" "$3/bin/python"
else
  printf '%s\\n' "$*" >> "$INSTALL_LOG"
fi
""")
    fake_python.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path), prepend=':')
    monkeypatch.setenv('RUNNER_TEMP', str(tmp_path))
    monkeypatch.setenv('GITHUB_ENV', str(tmp_path / 'github-env'))
    log = tmp_path / 'pip.log'
    monkeypatch.setenv('INSTALL_LOG', str(log))
    for profile in ('docs', 'release', 'python-tests', 'node-tests-docs', 'workspace-ui', 'full'):
        log.write_text('')
        monkeypatch.setenv('VALIDATION_PROFILE', profile)
        subprocess.run(['bash', '-c', scripts[0]], check=True)
        tools, project = (line.split() for line in log.read_text().splitlines())
        assert 'setuptools>=77' in tools and 'pytest' in tools
        assert ('pytest-xdist' in tools) == (profile == 'full')
        assert project[-2:] == ['-e', '.[database-analysis]' if profile == 'full' else '.']


@pytest.fixture
def dispatch_root(tmp_path, monkeypatch):
    # These executables only record the dispatcher contract; no install, network,
    # production key, repository publication or Full artifact build is performed.
    root = tmp_path / "fixture"
    root.mkdir()
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", "/production/key/must-not-be-used")
    (root / "tests").mkdir()
    (root / "pytest.ini").write_text("[pytest]\nmarkers = browser: local Chromium only\n")
    record = r'''import json, os, sys
from pathlib import Path

def record():
    key = Path(os.environ['AGENTBRIDGE_SESSION_KEY_FILE'])
    row = {'script': Path(__file__).name, 'args': sys.argv[1:], 'key': str(key),
           'keyBytes': len(key.read_bytes()), 'keyMode': key.stat().st_mode & 0o777}
    with Path('dispatch.jsonl').open('a') as out:
        out.write(json.dumps(row) + '\n')
    return os.environ.get('TEST_FAIL_STAGE') == row['script']
'''
    path = root / "scripts/check_public_content.py"
    path.parent.mkdir()
    path.write_text(record + "\nif record(): raise SystemExit(13)\n")
    for name in ["tests/test_documentation.py", *ci_validation.RELEASE_TESTS, *ci_validation.INDEPENDENT_PYTHON_TESTS]:
        (root / name).write_text(record + """
import pytest

def test_selected():
    assert not record()

@pytest.mark.browser
def test_local_only():
    pytest.fail("CI must never execute a browser case, including one in a mixed module")
""")
    return root


def records(root):
    return [json.loads(line) for line in (root / "dispatch.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("profile", ["docs", "release", "python-tests"])
def test_scoped_profile_runs_selected_nonbrowser_tests_with_isolated_key(dispatch_root, profile):
    ci_validation.run_profile(profile, dispatch_root)
    calls = records(dispatch_root)
    expected = ["test_documentation.py"]
    if profile == "release":
        expected.extend(Path(path).name for path in ci_validation.RELEASE_TESTS)
    if profile == "python-tests":
        expected.extend(Path(path).name for path in ci_validation.INDEPENDENT_PYTHON_TESTS)
    assert [c["script"] for c in calls] == [*expected, "check_public_content.py"]
    assert {c["keyBytes"] for c in calls} == {32}
    assert {c["keyMode"] for c in calls} == {stat.S_IRUSR | stat.S_IWUSR}
    assert len({c["key"] for c in calls}) == 1
    assert not Path(calls[0]["key"]).exists()
    assert not (dispatch_root / "output/release-validation/full.json").exists()
    assert validation_plan.test_report(dispatch_root / "output/ci-validation/pytest.xml")["passed"] == len(expected)


def test_full_ci_has_all_nonbrowser_checks_and_no_formal_release_receipt():
    local = validation_plan.plan(ROOT)
    stages = {s["id"]: s for s in ci_validation.ci_stages("full", ROOT)}
    local_ids = {s["id"] for s in local["stages"]}
    assert set(stages) == local_ids - {"browser-runtime", "python-full"} | {"python-non-browser", "mcp-app-test"}
    command = stages["python-non-browser"]["command"]
    assert command[command.index("-m", 3) + 1] == "not browser"
    assert "--strict-markers" in command
    assert stages["python-non-browser"]["testFiles"] == local["groups"]["python"]
    for group, stage in (("workspaceNode", "workspace-node"), ("openclaw", "openclaw")):
        assert stages[stage]["testFiles"] == local["groups"][group]
    assert stages["installed-wheel"]["command"][2] == "build"
    assert "runs/{runId}/artifact" in stages["installed-wheel"]["report"]
    assert all(not stage["report"] or stage["report"].startswith("output/ci-validation/") for stage in stages.values())
    assert all("playwright" not in s["command"] and "chromium" not in s["command"] for s in stages.values())


def test_small_profiles_run_whole_node_families_and_workspace_ui_packages_the_change():
    local_root = "output/development-validation/example"
    node = ci_validation.ci_stages("node-tests", ROOT, output=local_root)
    node_docs = ci_validation.ci_stages("node-tests-docs", ROOT, output=local_root)
    ui = ci_validation.ci_stages("workspace-ui", ROOT, output=local_root)
    ids = lambda stages: [s["id"] for s in stages]
    assert set(ids(node)) == {"public-content", "workspace-node", "openclaw", "mcp-app-test"}
    assert ids(node_docs) == ["python-docs", *ids(node)]
    assert ids(ui) == ["python-workspace-ui", "public-content", "workspace-node", "installed-wheel"]
    for stage in node + node_docs + ui:
        if stage.get("report"):
            assert stage["report"].startswith(local_root + "/")
            assert not any(validation_plan.CI_OUTPUT in arg for arg in stage["command"])
    assert all(path in ui[0]["command"] for path in ("tests/test_documentation.py", *ci_validation.HISTORY_PYTHON_TESTS))
    assert node[1]["testFiles"] == list(validation_plan.WORKSPACE_NODE)


def test_repeated_ci_runs_do_not_collide_with_an_existing_wheel_probe(dispatch_root, monkeypatch):
    stage = next(s for s in ci_validation.ci_stages("full", ROOT) if s["id"] == "installed-wheel")
    builder = dispatch_root / "scripts/agentbridge_artifact.py"
    builder.write_text("""import sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index('--output') + 1])
out.mkdir(parents=True, exist_ok=False)
(out / 'artifact.json').write_text('{}')
""")
    stage["command"] = [arg.replace(str(ROOT), str(dispatch_root)) for arg in stage["command"]]
    monkeypatch.setattr(ci_validation, "ci_stages", lambda *args: [stage])
    for _ in range(2):
        ci_validation.run_profile("full", dispatch_root)
    assert len(list((dispatch_root / "output/ci-validation/runs").glob("*/artifact/artifact.json"))) == 2
    assert not (dispatch_root / "output/release-validation/full.json").exists()


@pytest.mark.parametrize("failure", ["test_documentation.py", "check_public_content.py"])
def test_scoped_failure_stops_remaining_checks_and_cleans_key(dispatch_root, monkeypatch, failure):
    monkeypatch.setenv("TEST_FAIL_STAGE", failure)
    with pytest.raises(subprocess.CalledProcessError) as error:
        ci_validation.run_profile("docs", dispatch_root)
    assert error.value.returncode == (1 if failure == "test_documentation.py" else 13)
    calls = records(dispatch_root)
    assert calls[-1]["script"] == failure
    assert not Path(calls[0]["key"]).exists()


def test_unknown_profile_runs_nothing(dispatch_root):
    with pytest.raises(ValueError, match="Unknown validation profile"):
        ci_validation.run_profile("reuse", dispatch_root)
    assert not (dispatch_root / "dispatch.jsonl").exists()
