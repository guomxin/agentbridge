"""Conservative main/PR CI scope selection backed by successful GitHub runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

WORKFLOW = '.github/workflows/validate.yml'
RELEASE_CONFIG = {'deploy/release-policy.json'}


def classify(paths):
    if not paths:
        return 'full'  # An empty/unavailable diff is not evidence of documentation-only work.
    profiles = set()
    for path in paths:
        if (path.startswith('docs/') and path.endswith('.md')) or path == 'README.md':
            profiles.add('docs')
        elif path in RELEASE_CONFIG:
            profiles.add('release')
        else:
            return 'full'
    return 'release' if 'release' in profiles else 'docs'


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
        # No rename detection: moves/deletions must expose the original path as well.
        paths = [p for p in git(root, 'diff', '--no-renames', '--name-only', '-z', base, sha).split('\0') if p]
        return {**result, 'baseline': base, 'profile': classify(paths), 'changedPaths': paths,
                'reason': 'verified_baseline_diff', 'evidenceRun': run['html_url']}
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError):
        # API, history or diff uncertainty costs a full run; it can never grant a pass.
        return {**result, 'reason': 'evidence_unavailable_full_fallback'}


def main():
    root = Path(__file__).resolve().parents[1]
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    result = plan(root, os.environ, event)
    destination = root / 'output/release-validation/ci-plan.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
        stream.write(f"profile={result['profile']}\n")
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
        stream.write('### Validation plan\n```json\n' + json.dumps(result, ensure_ascii=False, indent=2) + '\n```\n')
    # Caller stdout may use a legacy encoding; artifacts and summaries stay UTF-8.
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
