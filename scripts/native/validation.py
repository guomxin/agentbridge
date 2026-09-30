"""Native equivalent of Invoke-AgentBridgeValidation.ps1."""
from __future__ import annotations
import sys
import os
import tempfile
import uuid
from pathlib import Path
from common import ROOT, run


def test_run(argv, **kwargs):
    # Windows uses DPAPI; POSIX fixture sessions need an isolated ephemeral key.
    # Never inherit a production session key into a test suite.
    with tempfile.TemporaryDirectory(prefix='agentbridge-native-tests-') as folder:
        directory = Path(folder).resolve()
        key = directory / 'session.key'
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        env = {**os.environ, 'AGENTBRIDGE_SESSION_KEY_FILE': str(key),
               'TMPDIR': str(directory)}
        log = Path.home() / 'AgentBridgeMigration' / ('native-test-' + uuid.uuid4().hex + '.json')
        return run(argv, env=env, diagnostics=log, **kwargs)


def validate(*, full=False, tests=(), plugin=False):
    python = sys.executable
    if sys.version_info < (3, 12):
        raise ValueError('Python 3.12+ is required')
    artifact = [python, ROOT / 'scripts/agentbridge_artifact.py']
    if full:
        run([python, '-m', 'playwright', 'install', 'chromium'])
        run([python, 'scripts/check_public_content.py'])
        test_run([python, 'scripts/current_facts.py', '--check'])
        run([*artifact, 'begin', '--root', ROOT])
        test_run([python, '-m', 'pytest', '-q', '-n', '4', '--dist', 'loadscope',
             '--junitxml=output/release-validation/pytest.xml'], timeout=3600)
        run([python, '-m', 'compileall', '-q', 'bscli'])
        run([python, '-m', 'pip', 'check'])
        run(['node', '--test', *['tests/test_workspace_' + n + '.mjs' for n in
            ('gateway_events', 'gateway_run_guard', 'gateway_client', 'card_messages', 'progress')]])
    elif tests:
        paths = [(ROOT / p).resolve() for p in tests]
        if any(not p.is_relative_to(ROOT) or not p.is_file() for p in paths):
            raise ValueError('Tests must be files inside the repository')
        test_run([python, '-m', 'pytest', '-q', *paths], timeout=3600)
    elif not plugin:
        raise ValueError('Targeted validation requires tests or --plugin')
    if full or plugin:
        location = ROOT / 'integrations/openclaw-agentbridge'
        run(['npm', 'test'], cwd=location, timeout=1800)
        run(['npm', 'run', 'pack:check'], cwd=location)
    if full:
        run([*artifact, 'finish', '--root', ROOT])
    return {'status': 'succeeded', 'mode': 'full' if full else 'targeted',
            'formalReceipt': full, 'businessWrites': 0}
