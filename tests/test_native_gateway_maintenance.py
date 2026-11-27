"""No real launchctl, network or production files: exercise the actual protocol."""
import argparse
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from unittest import mock

import pytest
pytest.importorskip('fcntl')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/native'))
import common
import gateway_maintenance as gm
import lifecycle
import publish
import acceptance
from tests.test_native_maintenance import host, private, registered_host

CANDIDATE = 'a' * 40


@pytest.fixture
def maintenance(host, private, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    authorization, _ = registered_host(host, private)
    common.save(host['configPath'], {'plugins': {'load': {'paths': ['/fixed/old/plugin']}}})
    replacement = private('replacement.json', {'plugins': {'load': {'paths': ['/fixed/new/plugin']}}})
    installed = tmp_path / 'Library/LaunchAgents' / (lifecycle.LABELS['gateway'] + '.plist')
    installed.parent.mkdir(parents=True)
    installed.write_bytes(plistlib.dumps(lifecycle.plists(host)['gateway']))
    installed.chmod(0o600)
    state = {'loaded': True, 'disabled': False, 'alive': True, 'unknown': False,
             'calls': [], 'runtime': {'pid': 123, 'started': 'old'}, 'listener': False}

    def command(argv):
        if argv[0] == '/bin/ps':
            return subprocess.CompletedProcess(argv, 0 if state['alive'] else 1,
                                               'old' if state['alive'] else '', '')
        if argv[1] == 'print-disabled':
            return subprocess.CompletedProcess(argv, 0,
                'disabled services = {\n "ai.agentbridge.gateway" => '
                + str(state['disabled']).lower() + '\n}', '')
        if state['unknown']:
            return subprocess.CompletedProcess(argv, 5, '', 'permission denied')
        return subprocess.CompletedProcess(argv, 0 if state['loaded'] else 113,
            'pid = 123' if state['loaded'] else '', '' if state['loaded'] else
            'Could not find service "ai.agentbridge.gateway" in domain for user gui: 501')

    def run(argv, **_):
        if argv[0] == 'git':
            return CANDIDATE
        state['calls'].append(argv[1])
        if argv[1] == 'disable':
            state['disabled'] = True
        elif argv[1] == 'bootout':
            state['loaded'] = state['alive'] = False
        elif argv[1] == 'enable':
            state['disabled'] = False
        elif argv[1] == 'bootstrap':
            if state.get('bootstrapFail'):
                raise RuntimeError('injected bootstrap failure')
            state['loaded'] = True
            state['runtime'] = {'pid': 124, 'started': 'new'}
        else:
            raise AssertionError('unexpected mutation')
        return ''

    def observe(_):
        if not state['loaded']:
            raise ValueError('not running')
        return state['runtime']

    monkeypatch.setattr(gm, 'command', command)
    monkeypatch.setattr(lifecycle, 'run', run)
    monkeypatch.setattr(lifecycle, 'observe', observe)
    probe = mock.MagicMock()
    probe.__enter__.return_value.connect_ex.side_effect = lambda *_: 0 if state['listener'] else 61
    monkeypatch.setattr(gm.socket, 'socket', lambda: probe)
    receipt = tmp_path / 'stop.json'

    def stop_and_replace():
        gm.stop(host, authorization, CANDIDATE, replacement, receipt)
        Path(host['configPath']).write_bytes(replacement.read_bytes())
        return receipt

    return argparse.Namespace(host=host, authorization=authorization, replacement=replacement,
                              installed=installed, receipt=receipt, state=state,
                              stop_and_replace=stop_and_replace)


def test_stop_switch_start_is_one_enable_with_original_plist(maintenance):
    m = maintenance
    original = Path(m.host['configPath']).read_bytes()
    assert gm.stop(m.host, m.authorization, CANDIDATE, m.replacement, m.receipt)['configurationChanged'] is False
    assert Path(m.host['configPath']).read_bytes() == original
    assert m.state['calls'] == ['disable', 'bootout']
    with pytest.raises(ValueError, match='does not match'):
        gm.check(m.host, m.authorization, CANDIDATE, m.receipt)
    Path(m.host['configPath']).write_bytes(m.replacement.read_bytes())
    assert gm.start(m.host, m.authorization, CANDIDATE, m.receipt)['status'] == 'started-awaiting-acceptance'
    assert m.state['calls'] == ['disable', 'bootout', 'enable', 'bootstrap']
    assert gm.start(m.host, m.authorization, CANDIDATE, m.receipt)['status'] == 'retained-maintenance-start'
    assert m.state['calls'].count('bootstrap') == 1
    assert (Path(m.host['runtimeDir']) / 'warmup.pending.json').exists()
    assert common.read(m.receipt)['startedRuntime'] == m.state['runtime']


@pytest.mark.parametrize('field,value', [('loaded', False), ('disabled', True), ('unknown', True)])
def test_original_disabled_absent_unknown_jobs_cannot_create_permission(maintenance, field, value):
    m = maintenance
    m.state[field] = value
    with pytest.raises(ValueError):
        gm.stop(m.host, m.authorization, CANDIDATE, m.replacement, m.receipt)
    assert not m.receipt.exists()
    assert not m.state['calls']


@pytest.mark.parametrize('change', ['bytes', 'mode', 'symlink', 'owner'])
def test_installed_plist_review_preserves_bytes_ownership_and_type(maintenance, change, monkeypatch):
    m = maintenance
    if change == 'bytes':
        m.installed.write_bytes(b'other job')
    elif change == 'mode':
        m.installed.chmod(0o644)
    elif change == 'symlink':
        target = m.installed.with_suffix('.saved')
        m.installed.rename(target)
        m.installed.symlink_to(target)
    else:
        original = Path.stat
        def stat(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == m.installed:
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=result.st_mode, st_uid=result.st_uid + 1)
            return result
        monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(ValueError):
        gm.stop(m.host, m.authorization, CANDIDATE, m.replacement, m.receipt)
    assert not m.state['calls']


@pytest.mark.parametrize('change', ['candidate', 'config', 'env', 'plist', 'authorization', 'old', 'unknown', 'listener', 'original_alive'])
def test_start_rechecks_every_binding_before_any_enable(maintenance, change):
    m = maintenance
    m.stop_and_replace()
    candidate = CANDIDATE
    if change == 'candidate':
        candidate = 'b' * 40
    elif change == 'config':
        common.save(m.host['configPath'], {})
    elif change == 'env':
        Path(m.host['envFile']).write_text('A=changed')
    elif change == 'plist':
        m.installed.write_bytes(b'changed')
    elif change == 'authorization':
        m.authorization.write_text('{}')
    elif change == 'old':
        receipt = common.read(m.receipt)
        receipt['createdAt'] = '2000-01-01T00:00:00+00:00'
        common.save(m.receipt, receipt)
    else:
        m.state[{'original_alive': 'alive'}.get(change, change)] = True
    with pytest.raises(ValueError):
        gm.start(m.host, m.authorization, candidate, m.receipt)
    assert m.state['calls'] == ['disable', 'bootout']


def test_bootstrap_unknown_is_not_retried_or_reenabled(maintenance):
    m = maintenance
    m.stop_and_replace()
    m.state['bootstrapFail'] = True
    with pytest.raises(RuntimeError):
        gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    assert common.read(m.receipt)['phase'] == 'starting'
    with pytest.raises(ValueError):
        gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    assert m.state['calls'].count('enable') == 1
    assert m.state['calls'].count('bootstrap') == 1


def test_operator_stop_after_success_cannot_be_undone_with_old_receipt(maintenance):
    m = maintenance
    m.stop_and_replace()
    gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    m.state.update(loaded=False, disabled=True)
    with pytest.raises(ValueError, match='no automatic re-enable'):
        gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    assert m.state['calls'].count('bootstrap') == 1


def test_replacement_may_only_change_one_load_path(maintenance):
    m = maintenance
    common.save(m.replacement, {'plugins': {'load': {'paths': ['/fixed/new/plugin']}}, 'gateway': {'auth': {'mode': 'none'}}})
    with pytest.raises(ValueError, match='outside'):
        gm.stop(m.host, m.authorization, CANDIDATE, m.replacement, m.receipt)
    assert not m.state['calls']


@pytest.mark.parametrize('diagnostic', ['permission denied', 'IPC error', '', 'No such process', 'Could not find service "other" in domain for user gui: 501'])
def test_unrecognized_launchctl_failures_are_not_absence(monkeypatch, diagnostic):
    monkeypatch.setattr(gm, 'command', lambda _: subprocess.CompletedProcess([], 113, '', diagnostic))
    with pytest.raises(ValueError, match='unknown'):
        gm.loaded()


@pytest.mark.parametrize('value,expected', [('true', True), ('false', False), ('disabled', True), ('enabled', False), (None, False)])
def test_print_disabled_accepts_native_formats_and_absent_default(monkeypatch, value, expected):
    text = 'disabled services = {\n' + ('' if value is None else f' "ai.agentbridge.gateway" => {value}\n') + '}\n'
    monkeypatch.setattr(gm, 'command', lambda _: subprocess.CompletedProcess([], 0, text, ''))
    assert gm.disabled() is expected


@pytest.mark.parametrize('text', ['disabled services = {', '', 'disabled services = {\n "ai.agentbridge.gateway" => unknown\n}', 'disabled services = {\n "ai.agentbridge.gateway" : disabled\n}'])
def test_print_disabled_unknown_format_rejects(monkeypatch, text):
    monkeypatch.setattr(gm, 'command', lambda _: subprocess.CompletedProcess([], 0, text, ''))
    with pytest.raises(ValueError):
        gm.disabled()


@pytest.mark.parametrize('mode', ['resume', 'rollback_nanobot', 'rollback_pi', 'restore_pi_from'])
def test_publish_does_not_turn_completion_or_rollback_into_enable(mode):
    publisher = object.__new__(publish.Publisher)
    publisher.args = argparse.Namespace(gateway_maintenance_receipt='unused', resume=False, **({} if mode == 'resume' else {mode: True}))
    setattr(publisher.args, mode, True)
    with pytest.raises(ValueError, match='cannot be combined'):
        publisher.gateway_maintenance()


def test_publish_starts_only_after_central_and_keeps_warmup_acceptance(maintenance, monkeypatch):
    m = maintenance
    m.stop_and_replace()
    publisher = object.__new__(publish.Publisher)
    publisher.args = argparse.Namespace(offline=False, reuse_validation=True, resume=False,
        host_profile='private', authorization=m.authorization, identity_label=[], expect_endpoint=[],
        profile='private', host='example.invalid', remote_root='/home/test', gateway_maintenance_receipt=m.receipt)
    publisher.commit = CANDIDATE
    publisher.units = {'unit': b'x'}
    publisher.nanobot = publisher.pi = None
    steps = []
    monkeypatch.setattr(publisher, 'plan', lambda: {'blockers': [], 'remoteUrl': 'https://github.com/guomxin/agentbridge.git'})
    monkeypatch.setattr(lifecycle, 'settings', lambda _: m.host)
    monkeypatch.setattr(publish, 'environment', lambda *_: publisher.units)
    monkeypatch.setattr(publish, 'git', lambda *_: CANDIDATE)
    monkeypatch.setattr(publish, 'candidate_artifact', lambda **_: ({}, 'reused'))
    monkeypatch.setattr(publish, 'test_run', lambda *_: None)
    monkeypatch.setattr(publish, 'run', lambda argv, **_: CANDIDATE if 'ls-remote' in argv else '{}')
    monkeypatch.setattr(publisher, 'deploy', lambda _: steps.append('central-confirmed'))
    original = gm.start
    def start(*args):
        assert steps == ['central-confirmed']
        steps.append('gateway')
        return original(*args)
    monkeypatch.setattr(gm, 'start', start)
    monkeypatch.setattr(acceptance, 'warmup', lambda _: steps.append('warmup'))
    monkeypatch.setattr(acceptance, 'acceptance', lambda *_: steps.append('acceptance') or {})
    assert publisher.execute()['status'] == 'succeeded'
    assert steps == ['central-confirmed', 'gateway', 'warmup', 'acceptance']


def test_no_explicit_receipt_preserves_old_conditional_restart():
    publisher = object.__new__(publish.Publisher)
    publisher.args = argparse.Namespace()
    assert publisher.gateway_maintenance() is None


def test_stop_interruption_never_authorizes_start(maintenance, monkeypatch):
    m = maintenance
    original_run = lifecycle.run
    def run(argv, **kwargs):
        if argv[:2] == ['/bin/launchctl', 'bootout']:
            raise RuntimeError('injected stop interruption')
        return original_run(argv, **kwargs)
    monkeypatch.setattr(lifecycle, 'run', run)
    with pytest.raises(RuntimeError):
        gm.stop(m.host, m.authorization, CANDIDATE, m.replacement, m.receipt)
    assert common.read(m.receipt)['phase'] == 'stopping'
    Path(m.host['configPath']).write_bytes(m.replacement.read_bytes())
    with pytest.raises(ValueError):
        gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    assert m.state['calls'] == ['disable']


def test_started_receipt_cannot_certify_reused_pid_or_other_process(maintenance):
    m = maintenance
    m.stop_and_replace()
    gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    m.state['runtime'] = {**m.state['runtime'], 'started': 'reused pid'}
    with pytest.raises(ValueError, match='no automatic re-enable'):
        gm.check(m.host, m.authorization, CANDIDATE, m.receipt)


def test_offline_receipt_plan_never_observes_launchd(maintenance, monkeypatch):
    m = maintenance
    m.stop_and_replace()
    monkeypatch.setattr(gm, 'command', lambda *_: pytest.fail('offline plan observed runtime'))
    assert gm.check(m.host, m.authorization, CANDIDATE, m.receipt, runtime=False)['phase'] == 'stopped'


def test_failed_receipt_directory_sync_prevents_enable(maintenance, monkeypatch):
    m = maintenance
    m.stop_and_replace()
    def fail(_):
        raise OSError('injected receipt directory sync failure')
    monkeypatch.setattr(gm, 'sync_directory', fail)
    with pytest.raises(OSError):
        gm.start(m.host, m.authorization, CANDIDATE, m.receipt)
    assert m.state['calls'] == ['disable', 'bootout']
    assert common.read(m.receipt)['phase'] == 'starting'
