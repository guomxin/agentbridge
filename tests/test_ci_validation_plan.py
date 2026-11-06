"""Prove that scope reduction cannot hide code changes or stale CI failures."""
import subprocess
import io
import json
from unittest.mock import patch

import pytest

from scripts.ci_validation_plan import HISTORY_UI, classify, latest_trusted, parse_raw_diff, plan
from scripts import ci_validation_plan

SHA = 'a' * 40
BASE = 'b' * 40
REPO = 'guomxin/agentbridge'


def run(sha=BASE, **overrides):
    return dict(dict(id=10, head_sha=sha, head_branch='main', event='push',
                     path='.github/workflows/validate.yml', head_repository={'full_name': REPO},
                     run_number=10, run_attempt=1, status='completed', conclusion='success',
                     html_url='https://example.test/run/10'), **overrides)


def environment(**overrides):
    return dict(dict(GITHUB_SHA=SHA, GITHUB_EVENT_NAME='push', GITHUB_REPOSITORY=REPO,
                     GITHUB_RUN_ID='20', GITHUB_REF='refs/heads/main'), **overrides)


def edits(*paths):
    return ''.join(':100644 100644 abc1234 def5678 M\0' + p + '\0' for p in paths)


@pytest.mark.parametrize('paths,expected', [
    (['docs/a.md', 'README.md'], 'docs'),
    (['AGENTS.md'], 'docs'),
    (['docs/a.md', 'deploy/release-policy.json'], 'release'),
    (['deploy/systemd/agentbridge.service'], 'full'),
    (['docs/a.md', 'agentbridge/core/tasks.py'], 'full'),
    (['docs/example.py'], 'full'),
    (['agentbridge/business_skills/log-review/SKILL.md'], 'full'),
    (['.github/workflows/validate.yml'], 'full'),
    (['scripts/ci_validation_plan.py'], 'full'),
    (['tests/test_ci_validation_plan.py'], 'full'),
    (['pyproject.toml'], 'full'),
    (['unknown.file'], 'full'), ([], 'full'),
])
def test_scope_is_an_allowlist(paths, expected):
    assert classify(paths) == expected


@pytest.mark.parametrize('paths,expected', [
    (['tests/test_workspace_gateway_history.mjs'], 'node-tests'),
    (['integrations/openclaw-agentbridge/test/database-errors.test.js', 'integrations/mcp-app/tests/result-state.test.mjs'], 'node-tests'),
    (['tests/test_workspace_gateway_history.mjs', 'docs/验收记录/示例.md'], 'node-tests-docs'),
    (['tests/test_workspace_gateway_history.mjs', 'AGENTS.md'], 'node-tests-docs'),
    (['tests/test_central_storage.py'], 'python-tests'),
    (['tests/test_task_feedback_acceptance.py', 'AGENTS.md'], 'python-tests'),
    (['tests/test_database_analysis_quality.py', 'docs/说明.md'], 'python-tests'),
    (['tests/test_central_storage.py', 'deploy/release-policy.json'], 'full'),
    (['tests/test_central_storage.py', 'tests/test_workspace.py'], 'full'),
    (['tests/test_auth_card.py', 'tests/test_field_card.py'], 'full'),
    ([HISTORY_UI], 'workspace-ui'),
    ([HISTORY_UI, 'tests/test_workspace_history_integration.mjs', 'docs/说明.md'], 'workspace-ui'),
    ([HISTORY_UI, 'tests/test_workspace_current.py'], 'full'),
    ([HISTORY_UI, 'integrations/mcp-app/tests/result-state.test.mjs'], 'full'),
    ([HISTORY_UI, 'deploy/release-policy.json'], 'full'),
    (['tests/test_workspace_gateway_history.mjs', 'deploy/release-policy.json'], 'full'),
    (['tests/test_workspace_markdown.browser.js'], 'full'),
    (['tests/unregistered.test.js'], 'full'),
    (['integrations/openclaw-agentbridge/test/fixtures.js'], 'full'),
    (['tests/test_workspace_gateway_history.mjs', 'agentbridge/core/planning_policy.py'], 'full'),
])
def test_small_scopes_require_only_registered_edits(paths, expected):
    assert classify(paths, regular_edits=paths) == expected
    assert classify(paths) == 'full'


def test_raw_git_metadata_preserves_path_bytes_and_only_grants_regular_edits():
    name = 'tests/含空格\t和换行\n.mjs'
    assert parse_raw_diff(edits(name)) == [(name, True)]
    for old, new, status in [('000000', '100644', 'A'), ('100644', '000000', 'D'),
                             ('100644', '120000', 'T'), ('120000', '120000', 'M'), ('100644', '100755', 'M')]:
        raw = f':{old} {new} 3ae875d a00673b {status}\0{HISTORY_UI}\0'
        assert parse_raw_diff(raw) == [(HISTORY_UI, False)]
    with pytest.raises(ValueError):
        parse_raw_diff('truncated\0')


@pytest.mark.parametrize('importer', [
    'from tests.test_central_storage import helper',
    'from tests import test_central_storage as shared',
    'import tests.test_central_storage',
    'import importlib; importlib.import_module("tests.test_central_storage")',
])
def test_previously_independent_test_reverts_to_full_if_consumed_elsewhere(tmp_path, importer):
    target = 'tests/test_central_storage.py'
    path = tmp_path / target
    path.parent.mkdir()
    path.write_text('def helper(): pass')
    assert classify([target], regular_edits=[target], root=tmp_path) == 'python-tests'
    consumer = tmp_path / 'tests/support/test_central_storage.py'
    consumer.parent.mkdir()
    consumer.write_text(importer)
    assert classify([target], regular_edits=[target], root=tmp_path) == 'full'


@pytest.mark.parametrize('override', [
    {'head_sha': SHA}, {'head_branch': 'feature'}, {'event': 'pull_request'},
    {'path': '.github/workflows/other.yml'}, {'head_repository': {'full_name': 'fork/repo'}},
    {'status': 'in_progress'}, {'conclusion': 'skipped'}, {'conclusion': 'failure'},
])
def test_reuse_rejects_untrusted_or_incomplete_evidence(override):
    assert latest_trusted([run(**override)], BASE, REPO) is None


def test_newer_failed_attempt_blocks_old_green():
    assert latest_trusted([run(), run(run_attempt=2, conclusion='failure')], BASE, REPO) is None
    assert latest_trusted([run(), run(id=11, run_number=11, status='in_progress')], BASE, REPO) is None


def test_main_uses_only_a_verified_previous_main_run(tmp_path):
    event = {'before': BASE}
    with patch('scripts.ci_validation_plan.git', side_effect=[BASE, edits('README.md')]):
        result = plan(tmp_path, environment(), event, lambda *_: [run(BASE)])
    assert result['profile'] == 'docs'
    assert result['baseline'] == BASE
    assert plan(tmp_path, {**environment(), 'GITHUB_RUN_ATTEMPT': '2'}, event,
                lambda *_: [run(BASE)])['profile'] == 'full'
    assert plan(tmp_path, {**environment(), 'GITHUB_EVENT_NAME': 'workflow_dispatch'}, event,
                lambda *_: [run(BASE)])['profile'] == 'full'


def test_entire_baseline_diff_includes_earlier_code_commit(tmp_path):
    with patch('scripts.ci_validation_plan.git', side_effect=[BASE, edits('docs/latest.md', 'agentbridge/core/tasks.py')]) as git:
        result = plan(tmp_path, environment(), {'before': BASE}, lambda *_: [run()])
    assert result['profile'] == 'full'
    assert git.call_args.args[1:] == ('diff', '--no-ext-diff', '--no-renames', '--ignore-submodules=none',
                                     '--raw', '-z', BASE, SHA, '--')


def test_scoped_check_requires_verified_ancestor(tmp_path):
    for files, expected in [(edits('docs/latest.md'), 'docs'), (edits('deploy/release-policy.json'), 'release')]:
        with patch('scripts.ci_validation_plan.git', side_effect=[BASE, files]):
            result = plan(tmp_path, environment(), {'before': BASE}, lambda *_: [run()])
        assert result['profile'] == expected
        assert result['baseline'] == BASE
    with patch('scripts.ci_validation_plan.git', return_value=SHA):
        assert plan(tmp_path, environment(), {'before': BASE}, lambda *_: [run()])['profile'] == 'full'
    with patch('scripts.ci_validation_plan.git', return_value=BASE):
        assert plan(tmp_path, environment(), {'before': BASE}, lambda *_: [])['profile'] == 'full'


def test_api_and_missing_history_fail_to_full(tmp_path):
    for error in [OSError('API unavailable'), subprocess.CalledProcessError(1, 'git')]:
        with patch('scripts.ci_validation_plan.git', side_effect=error):
            assert plan(tmp_path, environment(), {}, lambda *_: [run()])['profile'] == 'full'
    with patch('scripts.ci_validation_plan.github_runs', side_effect=OSError('API unavailable')) as fetch:
        assert plan(tmp_path, environment(GITHUB_REF='refs/heads/main'), {}, fetch)['profile'] == 'full'


def test_pull_request_uses_merge_base_not_latest_commit(tmp_path):
    with patch('scripts.ci_validation_plan.git', side_effect=[BASE, BASE, edits('README.md')]) as git:
        result = plan(tmp_path, environment(GITHUB_EVENT_NAME='pull_request'),
                      {'pull_request': {'base': {'sha': BASE}}}, lambda *_: [run()])
    assert result['profile'] == 'docs'
    assert git.call_args_list[0].args[1:] == ('merge-base', SHA, BASE)


def test_real_history_detects_code_before_document_and_rename(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.email', 'test@example.test')
    git('config', 'user.name', 'CI test')
    (tmp_path / 'runtime.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    (tmp_path / 'runtime.py').write_text('value = 2\n')
    git('commit', '-qam', 'runtime change')
    (tmp_path / 'README.md').write_text('docs\n')
    git('add', '.')
    git('commit', '-qm', 'later docs')
    sha = git('rev-parse', 'HEAD')
    result = plan(tmp_path, environment(GITHUB_SHA=sha), {'before': base}, lambda *_: [run(base)])
    assert result['profile'] == 'full'
    assert set(result['changedPaths']) == {'runtime.py', 'README.md'}
    git('update-ref', 'refs/remotes/origin/main', sha)
    git('mv', 'runtime.py', 'renamed.md')
    git('commit', '-qm', 'rename code')
    result = plan(
        tmp_path,
        environment(GITHUB_SHA=git('rev-parse', 'HEAD')),
        {'before': sha},
        lambda *_: [run(sha)],
    )
    assert result['profile'] == 'full'
    assert set(result['changedPaths']) == {'runtime.py', 'renamed.md'}


def test_git_modes_allow_content_edits_but_not_new_or_symlinked_workspace_resources(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()

    def commit_profile():
        base = git('rev-parse', 'HEAD')
        git('add', '-A')
        git('commit', '-qm', 'test resource')
        return plan(tmp_path, environment(GITHUB_SHA=git('rev-parse', 'HEAD')),
                    {'before': base}, lambda *_: [run(base)])['profile']

    git('init', '-q')
    git('config', 'user.email', 'test@example.test')
    git('config', 'user.name', 'CI test')
    git('commit', '--allow-empty', '-qm', 'initial')
    resource = tmp_path / HISTORY_UI
    resource.parent.mkdir(parents=True)
    resource.write_text('export const value = 1;')
    assert commit_profile() == 'full'
    resource.write_text('export const value = 2;')
    assert commit_profile() == 'workspace-ui'
    resource.unlink()
    resource.symlink_to('another.mjs')
    assert commit_profile() == 'full'


def test_entrypoint_handles_chinese_paths_with_legacy_encoding(tmp_path, monkeypatch):
    event_path = tmp_path / 'event.json'
    event_path.write_text('{}')
    monkeypatch.setenv('GITHUB_EVENT_PATH', str(event_path))
    monkeypatch.setenv('GITHUB_OUTPUT', str(tmp_path / 'output.txt'))
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(tmp_path / 'summary.md'))
    monkeypatch.setattr(ci_validation_plan, '__file__', str(tmp_path / 'scripts/ci_validation_plan.py'))
    result = {'profile': 'docs', 'changedPaths': ['docs/验收记录.md']}
    console = io.BytesIO()
    with io.TextIOWrapper(console, encoding='cp1252') as stdout:
        with patch.object(ci_validation_plan, 'plan', return_value=result), patch('sys.stdout', stdout):
            ci_validation_plan.main()
        stdout.flush()
        assert json.loads(console.getvalue().decode('cp1252')) == result
    assert json.loads((tmp_path / 'output/ci-validation/ci-plan.json').read_text(encoding='utf-8')) == result
    assert '验收记录' in (tmp_path / 'summary.md').read_text(encoding='utf-8')
