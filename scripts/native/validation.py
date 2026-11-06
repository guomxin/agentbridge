"""Native macOS/Linux validation entry point."""
from __future__ import annotations
import sys
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from common import ROOT, run
from scripts import ci_validation, ci_validation_plan


def test_run(argv, **kwargs):
    # Fixture sessions use an isolated ephemeral key.
    # Never inherit a production session key into a test suite.
    with tempfile.TemporaryDirectory(prefix='agentbridge-native-tests-') as folder:
        directory = Path(folder).resolve()
        key = directory / 'session.key'
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        env = {**os.environ, 'AGENTBRIDGE_SESSION_KEY_FILE': str(key),
               'TMPDIR': str(directory)}
        log = Path.home() / '.local/state/agentbridge/maintenance' / ('native-test-' + uuid.uuid4().hex + '.json')
        return run(argv, env=env, diagnostics=log, **kwargs)


def _git_output(root, *args):
    metadata = root / ('.gitrepo' if (root / '.gitrepo').exists() else '.git')
    return subprocess.check_output(
        ['git', f'--git-dir={metadata}', f'--work-tree={root}', *args],
        cwd=root, stderr=subprocess.DEVNULL,
    ).decode('utf-8')


def _changed_files(root, ref):
    try:
        base = _git_output(root, 'rev-parse', '--verify', '--end-of-options', f'{ref}^{{commit}}').strip()
        head = _git_output(root, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    except subprocess.CalledProcessError:
        raise ValueError('The comparison base and HEAD must resolve to commits') from None

    try:
        _git_output(root, 'merge-base', '--is-ancestor', base, head)
    except subprocess.CalledProcessError:
        raise ValueError('The comparison base must be an ancestor of HEAD') from None

    # A staged change can be undone in the working tree. Inspect each layer so
    # the candidate being prepared in the index is never hidden by cancellation.
    diff = ('diff', '--no-ext-diff', '--no-renames', '--ignore-submodules=none', '--raw', '-z')
    try:
        if _git_output(root, 'ls-files', '--unmerged', '-z'):
            raise ValueError('Resolve unmerged paths before running --changed')
        committed = ci_validation_plan.parse_raw_diff(_git_output(root, *diff, base, head, '--'))
        staged = ci_validation_plan.parse_raw_diff(_git_output(root, *diff, '--cached', head, '--'))
        working = ci_validation_plan.parse_raw_diff(_git_output(root, *diff, '--'))
        untracked = _git_output(root, 'ls-files', '--others', '--exclude-standard', '-z')
        working += [(path, False) for path in untracked.split('\0') if path]
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        raise ValueError('Cannot read the complete Git changes for this checkout') from None
    changes = committed + staged + working
    paths = sorted({path for path, _ in changes})
    regular_edits = set(paths) - {path for path, regular in changes if not regular}
    unaligned = sorted({path for path, _ in staged} & {path for path, _ in working})
    return base, head, paths, regular_edits, bool(staged or working), unaligned


def _validate_changed(root, ref):
    base, head, paths, regular_edits, dirty, unaligned = _changed_files(root, ref)
    result = {'mode': 'changed', 'baseCommit': base, 'headCommit': head, 'changedPaths': paths,
              'stages': [], 'formalReceipt': False, 'businessWrites': 0}
    if not paths:
        return {**result, 'status': 'no_changes', 'reason': 'No changed files; no tests were run'}
    if unaligned:
        return {**result, 'status': 'needs_alignment', 'unalignedPaths': unaligned,
                'reason': 'Staged and working-tree versions differ for these paths; align them or '
                          'validate an isolated committed candidate'}

    profile = ci_validation_plan.classify(paths, regular_edits=regular_edits, root=root)
    result['profile'] = profile
    if profile == 'full':
        return {**result, 'status': 'full_required', 'reason': 'Changes exceed targeted coverage; '
                'run scripts/agentbridge-native validate --full on a clean, committed candidate'}

    output = f'output/development-validation/{uuid.uuid4().hex}'
    stages = ci_validation.ci_stages(profile, root, output=output)
    for stage in stages:
        if stage['id'] == 'public-content':
            stage['command'] = [*stage['command'], '--worktree']
    if dirty and any(stage['id'] == 'installed-wheel' for stage in stages):
        stages = [stage for stage in stages if stage['id'] != 'installed-wheel']
        result['deferredChecks'] = {'installed-wheel': 'Wheel validation requires a clean, committed candidate'}
    log_dir = Path.home() / '.local/state/agentbridge/maintenance'
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=log_dir, prefix='changed-', suffix='.log', delete=False) as log:
        try:
            ci_validation.run_stages(stages, root, stream=log)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            raise RuntimeError(f'Changed-file checks failed; inspect {log.name}') from None
    return {**result, 'status': 'succeeded', 'stages': [stage['id'] for stage in stages],
            'outputDirectory': output, 'diagnosticLog': log.name}


def validate(*, full=False, tests=(), plugin=False, changed=False, base=None):
    python = sys.executable
    if sys.version_info < (3, 12):
        raise ValueError('Python 3.12+ is required')
    if base is not None and not changed:
        raise ValueError('--base requires --changed')
    if changed and (full or tests or plugin):
        raise ValueError('--changed cannot be combined with --full, --test or --plugin')
    if changed:
        return _validate_changed(ROOT, base if base is not None else 'HEAD')
    if full:
        run([python, ROOT / 'scripts/validation_plan.py', 'full', '--root', ROOT], timeout=7200)
    elif tests:
        paths = [(ROOT / p).resolve() for p in tests]
        if any(not p.is_relative_to(ROOT) or not p.is_file() for p in paths):
            raise ValueError('Tests must be files inside the repository')
        test_run([python, '-m', 'pytest', '-q', *paths], timeout=3600)
    elif not plugin:
        raise ValueError('Targeted validation requires tests or --plugin')
    if plugin and not full:
        location = ROOT / 'integrations/openclaw-agentbridge'
        run(['npm', 'test'], cwd=location, timeout=1800)
        run(['npm', 'run', 'pack:check'], cwd=location)
    return {'status': 'succeeded', 'mode': 'full' if full else 'targeted',
            'formalReceipt': full, 'businessWrites': 0}
