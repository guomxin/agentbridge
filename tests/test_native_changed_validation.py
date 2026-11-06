"""Exercise the development-only entrypoint in disposable Git worktrees."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import validation_plan


ROOT = Path(__file__).resolve().parents[1]
HISTORY = 'agentbridge/workspace/static/workspace_history_autoload.mjs'


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'fixture')
    git(root, 'config', 'user.email', 'fixture@example.test')
    for script in ('native/cli.py', 'native/common.py', 'native/validation.py',
                   'ci_validation.py', 'ci_validation_plan.py', 'validation_plan.py'):
        destination = root / 'scripts' / script
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / 'scripts' / script, destination)
    write(root, 'README.md', '# Fixture\n')
    write(root, '.gitignore', 'output/\n__pycache__/\n.pytest_cache/\n')
    write(root, 'pytest.ini', '[pytest]\nmarkers = browser: uses a real browser (local validation only)\n')
    write(root, HISTORY, 'export const value = 1;\n')
    write(root, 'prod.py', 'value = 1\n')
    write(root, 'tests/test_documentation.py',
          'from pathlib import Path\ndef test_readme():\n    assert "FAIL" not in Path("README.md").read_text()\n')
    write(root, 'scripts/check_public_content.py',
          'import sys\nassert sys.argv[1:] == ["--worktree"]\n')
    write(root, 'scripts/current_facts.py', '')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'base')
    return root


def changed(root, *args, expected_code=0):
    command = [sys.executable, root / 'scripts/native/cli.py', '--report', root.parent / 'report.json',
               'validate', *args]
    response = subprocess.run(command, cwd=root.parent, capture_output=True, text=True)
    assert response.returncode == expected_code, (response.stdout, response.stderr)
    result = json.loads(response.stdout)
    assert result == json.loads((root.parent / 'report.json').read_text())
    assert not (root / 'output/release-validation/full.json').exists()
    return result


def test_no_changes_then_untracked_and_deleted_docs_are_checked(repo):
    clean = changed(repo, '--changed')
    assert clean['status'] == 'no_changes' and not clean['stages'] and not clean['formalReceipt']
    write(repo, 'docs/新 文档.md', '# Fixture\n')
    (repo / 'README.md').unlink()
    # The synthetic documentation test needs README.md and must actually fail.
    failed = changed(repo, '--changed', expected_code=2)
    assert failed['status'] == 'failed' and 'Changed-file checks failed' in failed['reason']
    write(repo, 'README.md', '# Fixture\n')
    result = changed(repo, '--changed')
    assert result['profile'] == 'docs' and result['changedPaths'] == ['docs/新 文档.md']
    assert 'python-docs' in result['stages'] and result['formalReceipt'] is False
    assert (repo / result['outputDirectory'] / 'pytest.xml').is_file()


def test_staged_failure_restored_on_disk_is_not_reported_as_validated(repo):
    write(repo, 'README.md', 'FAIL\n')
    git(repo, 'add', 'README.md')
    write(repo, 'README.md', '# Fixture\n')
    write(repo, 'docs/new.md', 'draft\n')
    result = changed(repo, '--changed', expected_code=2)
    assert result['changedPaths'] == ['README.md', 'docs/new.md']
    assert result['unalignedPaths'] == ['README.md'] and result['status'] == 'needs_alignment'
    assert result['stages'] == [] and result['formalReceipt'] is False


def test_staged_code_and_untracked_docs_require_full(repo):
    write(repo, 'prod.py', 'value = 2\n')
    git(repo, 'add', 'prod.py')
    write(repo, 'docs/new.md', 'draft\n')
    result = changed(repo, '--changed', expected_code=2)
    assert result['changedPaths'] == ['docs/new.md', 'prod.py']
    assert result['status'] == 'full_required' and result['profile'] == 'full'
    assert result['stages'] == [] and result['formalReceipt'] is False


def test_commit_range_rename_and_base_validation(repo):
    (repo / 'docs').mkdir()
    git(repo, 'mv', 'prod.py', 'docs/moved.md')
    git(repo, 'commit', '-qm', 'rename code into docs')
    assert changed(repo, '--changed')['status'] == 'no_changes'
    result = changed(repo, '--changed', '--base', 'HEAD^', expected_code=2)
    assert result['changedPaths'] == ['docs/moved.md', 'prod.py']
    assert result['status'] == 'full_required'
    failed = changed(repo, '--changed', '--base', 'unknown-ref', expected_code=2)
    assert 'resolve to commits' in failed['reason']
    git(repo, 'switch', '-qc', 'diverged', 'HEAD^')
    git(repo, 'commit', '--allow-empty', '-qm', 'other branch')
    git(repo, 'switch', '-q', 'main')
    failed = changed(repo, '--changed', '--base', 'diverged', expected_code=2)
    assert 'ancestor' in failed['reason']


def test_ui_checks_defer_the_wheel_until_the_same_source_is_committed(repo):
    # The planner sees the normal Node inventory; fixture commands remain bounded.
    for path in (*validation_plan.WORKSPACE_NODE, *validation_plan.BROWSER_FIXTURES):
        write(repo, path, '')
    write(repo, 'integrations/openclaw-agentbridge/package.json', '{"scripts":{"test":"node --test test/*.test.js"}}')
    write(repo, 'integrations/openclaw-agentbridge/test/fixture.test.js', '')
    write(repo, 'integrations/mcp-app/package.json', '{"scripts":{"test":"node --test tests/*.test.mjs"}}')
    # The real artifact builder has its own fixed-commit contract tests. This
    # substitute records the Git input it was asked to validate.
    write(repo, 'scripts/agentbridge_artifact.py', '''
from pathlib import Path
import subprocess, sys
out = Path(sys.argv[sys.argv.index("--output") + 1])
out.mkdir(parents=True)
head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
(out / "artifact.json").write_text(head)
''')
    from scripts.ci_validation_plan import HISTORY_PYTHON_TESTS
    for path in HISTORY_PYTHON_TESTS:
        write(repo, path, 'def test_history():\n    assert True\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'register fixture tests')
    write(repo, HISTORY, 'export const value = 2;\n')
    result = changed(repo, '--changed')
    assert result['profile'] == 'workspace-ui' and result['changedPaths'] == [HISTORY]
    assert {'python-workspace-ui', 'workspace-node'} <= set(result['stages'])
    assert 'installed-wheel' not in result['stages'] and 'installed-wheel' in result['deferredChecks']
    assert result['formalReceipt'] is False
    git(repo, 'add', HISTORY)
    git(repo, 'commit', '-qm', 'change only history')
    committed = changed(repo, '--changed', '--base', 'HEAD^')
    assert 'installed-wheel' in committed['stages'] and 'deferredChecks' not in committed
    artifact, = (repo / committed['outputDirectory']).rglob('artifact.json')
    assert artifact.read_text() == git(repo, 'rev-parse', 'HEAD')
    assert committed['formalReceipt'] is False


@pytest.mark.parametrize('options', [
    ['--base', 'HEAD'], ['--changed', '--test', 'tests/test_documentation.py'], ['--changed', '--plugin'],
])
def test_changed_rejects_conflicting_options(repo, options):
    result = changed(repo, *options, expected_code=2)
    assert result['status'] == 'failed'
