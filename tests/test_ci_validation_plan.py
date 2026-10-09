"""Prove that scope reduction cannot hide code changes or stale CI failures."""
import subprocess
import io
import json
from unittest.mock import patch

import pytest

from scripts.ci_validation_plan import classify, latest_trusted, plan
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


@pytest.mark.parametrize('paths,expected', [
    (['docs/a.md', 'README.md'], 'docs'),
    (['docs/a.md', 'deploy/release-policy.json'], 'release'),
    (['deploy/systemd/agentbridge.service'], 'full'),
    (['docs/a.md', 'agentbridge/core/tasks.py'], 'full'),
    (['docs/example.py'], 'full'),
    (['.github/workflows/validate.yml'], 'full'),
    (['scripts/ci_validation_plan.py'], 'full'),
    (['tests/test_ci_validation_plan.py'], 'full'),
    (['pyproject.toml'], 'full'),
    (['unknown.file'], 'full'), ([], 'full'),
])
def test_scope_is_an_allowlist(paths, expected):
    assert classify(paths) == expected


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
    with patch('scripts.ci_validation_plan.git', side_effect=[BASE, 'README.md\0']):
        result = plan(tmp_path, environment(), event, lambda *_: [run(BASE)])
    assert result['profile'] == 'docs'
    assert result['baseline'] == BASE
    assert plan(tmp_path, {**environment(), 'GITHUB_RUN_ATTEMPT': '2'}, event,
                lambda *_: [run(BASE)])['profile'] == 'full'
    assert plan(tmp_path, {**environment(), 'GITHUB_EVENT_NAME': 'workflow_dispatch'}, event,
                lambda *_: [run(BASE)])['profile'] == 'full'


def test_entire_baseline_diff_includes_earlier_code_commit(tmp_path):
    with patch('scripts.ci_validation_plan.git', side_effect=[BASE, 'docs/latest.md\0agentbridge/core/tasks.py\0']) as git:
        result = plan(tmp_path, environment(), {'before': BASE}, lambda *_: [run()])
    assert result['profile'] == 'full'
    assert git.call_args.args[1:] == ('diff', '--no-renames', '--name-only', '-z', BASE, SHA)


def test_scoped_check_requires_verified_ancestor(tmp_path):
    for files, expected in [('docs/latest.md\0', 'docs'), ('deploy/release-policy.json\0', 'release')]:
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
    with patch('scripts.ci_validation_plan.git', side_effect=[BASE, BASE, 'README.md\0']) as git:
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
    assert json.loads((tmp_path / 'output/release-validation/ci-plan.json').read_text(encoding='utf-8')) == result
    assert '验收记录' in (tmp_path / 'summary.md').read_text(encoding='utf-8')
