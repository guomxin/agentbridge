"""Conservative main/PR CI scope selection backed by successful GitHub runs."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

try:
    from validation_plan import plan as validation_plan
except ModuleNotFoundError:
    from scripts.validation_plan import plan as validation_plan

WORKFLOW = '.github/workflows/validate.yml'
RELEASE_CONFIG = {'deploy/release-policy.json'}
HISTORY_UI = 'agentbridge/workspace/static/workspace_history_autoload.mjs'
HISTORY_PYTHON_TESTS = (
    'tests/test_workspace_history_page.py',
    'tests/test_workspace_history_boundaries.py',
    'tests/test_workspace_current.py',
    'tests/test_frontend_modules.py',
)
HISTORY_COMPANIONS = {
    'tests/test_workspace_history_autoload.mjs',
    'tests/test_workspace_history_integration.mjs',
}
NODE_STAGES = {'workspace-node', 'openclaw', 'mcp-app-test'}
INDEPENDENT_PYTHON_TESTS = (
    'tests/test_task_feedback_acceptance.py',
    'tests/test_central_storage.py',
    'tests/test_database_analysis_quality.py',
)


def parse_raw_diff(raw):
    """Read NUL-delimited --raw --no-renames output without interpreting path characters."""
    fields = raw.split('\0')
    if fields[-1] or len(fields) % 2 != 1:
        raise ValueError('Incomplete raw Git diff')
    changes = []
    for header, path in zip(fields[:-1:2], fields[1::2]):
        match = re.fullmatch(r':([0-7]{6}) ([0-7]{6}) [0-9a-f]+ [0-9a-f]+ ([AMDTUXB])', header)
        if not match or not path:
            raise ValueError('Unexpected raw Git diff entry')
        old_mode, new_mode, status = match.groups()
        regular_edit = status == 'M' and old_mode == new_mode and old_mode in {'100644', '100755'}
        changes.append((path, regular_edit))
    return changes


def tests_have_consumers(root, paths):
    """A test module used as a fixture can affect tests outside the short suite."""
    names = {root / path: Path(path).stem for path in paths}
    for source in (root / 'tests').rglob('*.py'):
        if source.is_symlink():
            return True
        text = source.read_text(encoding='utf-8')
        candidates = {name for path, name in names.items() if source != path and name in text}
        if not candidates:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                targets = [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module:
                    targets.append(node.module)
            elif isinstance(node, ast.Call) and (getattr(node.func, 'attr', '') or getattr(node.func, 'id', '')) in {
                'import_module', '__import__', 'spec_from_file_location', 'run_module', 'run_path',
            }:
                targets = [v.value for v in ast.walk(node) if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            else:
                continue
            if any(candidates.intersection(re.findall(r'\w+', target)) for target in targets):
                return True
    return False


def classify(paths, *, regular_edits=(), root=None):
    paths = set(paths)
    if not paths:
        return 'full'  # An empty/unavailable diff is not evidence of documentation-only work.
    docs = {p for p in paths if p in {'README.md', 'AGENTS.md'} or (p.startswith('docs/') and p.endswith('.md'))}
    other = paths - docs
    if not other:
        return 'docs'
    if other <= RELEASE_CONFIG:
        return 'release'
    if not other <= set(regular_edits):
        return 'full'
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    if other <= set(INDEPENDENT_PYTHON_TESTS):
        try:
            if all((root / p).is_file() for p in other) and not tests_have_consumers(root, other):
                return 'python-tests'
        except (OSError, UnicodeError, SyntaxError):
            pass
        return 'full'
    if HISTORY_UI in other:
        return 'workspace-ui' if other <= HISTORY_COMPANIONS | {HISTORY_UI} else 'full'
    if not all(p.endswith(('.mjs', '.js')) for p in other):
        return 'full'
    try:
        stages = validation_plan(root, for_ci=True)['stages']
    except (OSError, ValueError):
        return 'full'
    node_tests = {p for stage in stages if stage['id'] in NODE_STAGES for p in stage['testFiles']}
    if not other <= node_tests:
        return 'full'
    return 'node-tests-docs' if docs else 'node-tests'


def latest_trusted(runs, sha, repository, exclude_id=None):
    matches = [r for r in runs
               if r.get('id') != exclude_id and r.get('head_sha') == sha
               and r.get('event') == 'push' and r.get('path') == WORKFLOW
               and r.get('head_repository', {}).get('full_name') == repository
               and r.get('head_branch') == 'main']
    # Never search backwards past a newer failed, cancelled or pending attempt.
    run = max(matches, key=lambda r: (r['run_number'], r.get('run_attempt', 1)), default=None)
    return run if run and run.get('status') == 'completed' and run.get('conclusion') == 'success' else None


def github_runs(repository, sha):
    url = f'https://api.github.com/repos/{repository}/actions/runs?' + urllib.parse.urlencode(
        {'head_sha': sha, 'event': 'push', 'per_page': 100})
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'AgentBridge-CI'}
    if os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    runs = []
    for page in range(1, 11):
        with urllib.request.urlopen(urllib.request.Request(url + f'&page={page}', headers=headers), timeout=30) as response:
            batch = json.load(response)['workflow_runs']
        runs.extend(batch)
        if len(batch) < 100:
            return runs
    raise RuntimeError('Incomplete run history')


def git(root, *args):
    metadata = root / ('.gitrepo' if (root / '.gitrepo').exists() else '.git')
    return subprocess.check_output(['git', f'--git-dir={metadata}', f'--work-tree={root}', *args],
                                   cwd=root).decode('utf-8').strip()


def plan(root, env, event, fetch=github_runs):
    sha = env['GITHUB_SHA']
    result = {'commit': sha, 'profile': 'full', 'reason': 'unproven_baseline', 'baseline': None,
              'changedPaths': [], 'evidenceRun': None}
    if env['GITHUB_EVENT_NAME'] == 'workflow_dispatch' or int(env.get('GITHUB_RUN_ATTEMPT', '1')) > 1:
        return {**result, 'reason': 'explicit_revalidation'}
    repository = env['GITHUB_REPOSITORY']
    current_id = int(env['GITHUB_RUN_ID'])
    try:
        if env['GITHUB_EVENT_NAME'] == 'push' and env.get('GITHUB_REF') == 'refs/heads/main':
            base = event.get('before', '')
        elif env['GITHUB_EVENT_NAME'] == 'pull_request':
            base = git(root, 'merge-base', sha, event['pull_request']['base']['sha'])
        else:
            base = git(root, 'merge-base', sha, 'refs/remotes/origin/main')
        if not re.fullmatch('[0-9a-f]{40}', base) or base == '0' * 40 or base == sha:
            return result
        if git(root, 'merge-base', sha, base) != base:
            return result
        run = latest_trusted(fetch(repository, base), base, repository, current_id)
        if not run:
            return result
        changes = parse_raw_diff(git(root, 'diff', '--no-ext-diff', '--no-renames', '--ignore-submodules=none',
                                     '--raw', '-z', base, sha, '--'))
        paths = [p for p, _ in changes]
        regular = set(paths) - {p for p, regular_edit in changes if not regular_edit}
        return {**result, 'baseline': base, 'profile': classify(paths, regular_edits=regular, root=root), 'changedPaths': paths,
                'reason': 'verified_baseline_diff', 'evidenceRun': run['html_url']}
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError):
        # API, history or diff uncertainty costs a full run; it can never grant a pass.
        return {**result, 'reason': 'evidence_unavailable_full_fallback'}


def main():
    root = Path(__file__).resolve().parents[1]
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    result = plan(root, os.environ, event)
    destination = root / 'output/ci-validation/ci-plan.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
        stream.write(f"profile={result['profile']}\n")
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
        stream.write('### Independent CI checks (no browser)\n```json\n' + json.dumps(result, ensure_ascii=False, indent=2) + '\n```\n')
    # Caller stdout may use a legacy encoding; artifacts and summaries stay UTF-8.
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
