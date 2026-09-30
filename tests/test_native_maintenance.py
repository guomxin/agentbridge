"""Offline native maintenance boundary tests: never contact/start production."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import sys
import tempfile
from unittest import mock

import pytest
pytest.importorskip('fcntl', reason='Native macOS/POSIX maintenance uses fcntl lifecycle leases')

NATIVE = Path(__file__).resolve().parents[1] / 'scripts/native'
sys.path.insert(0, str(NATIVE))
import common
import lifecycle
import publish
import validation
import acceptance


@pytest.fixture
def private(tmp_path):
    def write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data) if isinstance(data, (dict, list)) else data)
        path.chmod(0o600)
        return path
    return write


@pytest.fixture
def host(tmp_path, private):
    return {'node': '/usr/bin/true', 'openclaw': '/usr/bin/true', 'port': 18789,
            'runtimeDir': str(tmp_path / 'runtime'), 'stateDir': str(tmp_path),
            'configPath': str(private('openclaw.json', {})),
            'envFile': str(private('host.env', 'A=literal$(id)\n')),
            'caCertificate': str(private('ca.crt', 'test-only')),
            'environment': {'HOME': str(tmp_path), 'OPENCLAW_CONFIG_PATH': str(tmp_path / 'openclaw.json')},
            'tunnel': {'host': 'example.invalid', 'user': 'test', 'remotePort': 18789,
                       'identityFile': str(private('key', 'test-only')),
                       'knownHostsFile': str(private('known_hosts', 'test-only'))}}


def test_plist_safety_and_private_render(host, tmp_path):
    destination = tmp_path / 'new'
    with mock.patch.object(lifecycle, 'run') as execute:
        lifecycle.render(host, destination)
    assert execute.call_count == 2
    for path in destination.glob('*.plist'):
        assert path.stat().st_mode & 0o077 == 0
        value = plistlib.loads(path.read_bytes())
        assert value['KeepAlive'] is True
    tunnel = lifecycle.plists(host)['tunnel']['ProgramArguments']
    assert '127.0.0.1:18789:127.0.0.1:18789' in tunnel
    for flag in ('StrictHostKeyChecking=yes', 'ExitOnForwardFailure=yes', 'BatchMode=yes', 'ServerAliveCountMax=2'):
        assert flag in tunnel
    gateway = lifecycle.plists(host)['gateway']['ProgramArguments']
    assert gateway[-4:] == ['--bind', 'loopback', '--port', '18789']
    assert '--force' not in gateway
    with pytest.raises(FileExistsError):
        lifecycle.render(host, destination)


def test_dotenv_is_literal_and_rejects_insecure_tls(private):
    assert common.environment_file(private('literal.env', 'A=$(touch nope)\n'))['A'] == '$(touch nope)'
    with pytest.raises(ValueError):
        common.environment_file(private('bad.env', 'NODE_TLS_REJECT_UNAUTHORIZED=0'))


def test_private_files_and_public_profile_refused(tmp_path, private):
    p = private('secret', 'x')
    p.chmod(0o644)
    with pytest.raises(ValueError):
        common.private_file(p)
    with pytest.raises(ValueError):
        common.external(common.ROOT / 'secrets.json')


@pytest.mark.parametrize('current,candidate,resume,expected', [
    ('a' * 12, 'b' * 12, False, 'compatible'),
    ('b' * 12, 'b' * 12, True, 'already_current'),
])
def test_release_compatibility(current, candidate, resume, expected):
    policy = {'schemaVersion': 'agentbridge.release-policy.v1', 'compatibleFrom': ['a' * 12], 'dataCompatibility': 'no-migration'}
    assert publish.compatibility(current, candidate, policy, resume) == expected


@pytest.mark.parametrize('current,policy,resume', [
    ('bad', {}, False), ('c' * 12, {}, False),
    ('a' * 12, {'schemaVersion': 'agentbridge.release-policy.v1', 'compatibleFrom': ['a' * 12], 'dataCompatibility': 'reviewed-schema-transition'}, False),
    ('a' * 12, {}, True),
])
def test_release_preflight_fails_closed(current, policy, resume):
    with pytest.raises(ValueError):
        publish.compatibility(current, 'b' * 12, policy, resume)


def test_unit_hash_and_external_path_gate(private):
    unit = private('unit', 'approved bytes')
    units = {name: {'path': str(unit), 'sha256': common.sha(unit.read_bytes())}
             for name in ('agentbridge.service', 'agentbridge-backup.service', 'agentbridge-backup.timer')}
    profile = private('profile.json', {'schema': 'agentbridge.environment.v1', 'host': 'host', 'root': '/home/test', 'units': units})
    assert len(publish.environment(profile, 'host', '/home/test')) == 3
    unit.write_text('changed')
    with pytest.raises(ValueError):
        publish.environment(profile, 'host', '/home/test')


def test_conditional_restart_pid_start_and_content():
    process = {'pid': 10, 'started': 'original'}
    baseline = {'fingerprint': 'abc', 'runtime': process}
    assert not lifecycle.decision('abc', baseline, process)['required']
    assert lifecycle.decision('def', baseline, process)['reason'] == 'inputs_changed'
    assert lifecycle.decision('abc', baseline, {'pid': 10, 'started': 'reused-pid'})['required']
    assert lifecycle.decision('abc', baseline, None)['required']


def test_content_fingerprint_ignores_docs_but_tracks_host_env(host, tmp_path):
    plugin = tmp_path / 'integrations/openclaw-agentbridge'
    (plugin / 'lib').mkdir(parents=True)
    (plugin / 'index.js').write_bytes(b'hello\r\n')
    with mock.patch.object(lifecycle, 'ROOT', tmp_path):
        original = lifecycle.fingerprint(host)
        (plugin / 'index.js').write_bytes(b'hello\n')
        (plugin / 'README.md').write_text('documentation change')
        assert lifecycle.fingerprint(host) == original
        Path(host['envFile']).write_text('A=changed')
        assert lifecycle.fingerprint(host) != original


def test_snapshot_cannot_activate(private, host):
    receipt = private('receipt.json', {'kind': 'snapshot'})
    manifest = private('manifest.json', {'kind': 'snapshot'})
    evidence = private('cutover.json', {'schema': 'agentbridge.mac-cutover.v1',
        **{n: True for n in ('windowsGatewayStopped', 'windowsGuardDisabled', 'windowsTunnelStopped', 'serverForwardReleased', 'm2Accepted')},
        'stagedManifest': str(manifest), 'bundleReceipt': str(receipt)})
    with mock.patch.object(lifecycle, 'run') as execute:
        with pytest.raises(ValueError):
            lifecycle.manage(host, 'start', 'tunnel', evidence)
    execute.assert_not_called()


def test_full_validation_stops_before_receipt_on_failure():
    calls = []
    def fail(args, **kwargs):
        calls.append(args)
        if 'pytest' in args:
            raise RuntimeError('test failure')
        return ''
    with mock.patch.object(validation, 'run', side_effect=fail):
        with pytest.raises(RuntimeError):
            validation.validate(full=True)
    assert any('begin' in a for a in calls)
    assert not any('finish' in a for a in calls)


def test_full_validation_preserves_required_checks():
    with mock.patch.object(validation, 'run') as execute:
        validation.validate(full=True)
    calls = [c.args[0] for c in execute.call_args_list]
    assert any('pack:check' in a for a in calls)
    assert any('compileall' in a for a in calls)
    assert any('pip' in a and 'check' in a for a in calls)
    assert 'finish' in calls[-1]


def test_offline_plan_never_connects_and_reports_branch_blocker():
    publisher = object.__new__(publish.Publisher)
    publisher.args = argparse.Namespace(resume=False, reuse_validation=False)
    publisher.commit = 'a' * 40
    publisher.units = {'unit': b'x'}
    def git(*args):
        if args[:2] == ('rev-parse', '--abbrev-ref'):
            return 'codex/macos-native-migration'
        if args[0] == 'remote':
            return 'https://github.com/guomxin/cli-helper.git'
        if args[0] == 'status':
            return ''
        return 'a' * 40
    with mock.patch.object(publish, 'git', side_effect=git), mock.patch.object(publisher, 'ssh') as ssh:
        result = publisher.plan(offline=True)
    ssh.assert_not_called()
    assert result['releasePreflight'] == 'not_checked_offline'
    assert not result['releaseBranchAllowed']
    assert result['blockers']


def test_resume_uses_saved_transaction_without_upload():
    publisher = object.__new__(publish.Publisher)
    publisher.args = argparse.Namespace(resume=True, remote_root='/home/test/agentbridge')
    publisher.release = 'a' * 12
    with mock.patch.object(publisher, 'ssh') as ssh, mock.patch.object(publish, 'run') as execute:
        publisher.deploy({})
    execute.assert_not_called()
    assert '--complete' in ssh.call_args.args[0]
    assert 'transaction.json' in ssh.call_args.args[0]


def test_failed_warmup_retains_pending(host):
    root = Path(host['runtimeDir'])
    root.mkdir()
    common.save(root / 'warmup.pending.json', {'fingerprint': 'abc'})
    common.save(root / 'gateway-baseline.json', {'fingerprint': 'abc', 'runtime': {'pid': 1}})
    with mock.patch.object(lifecycle, 'observe', return_value={'pid': 1}), mock.patch.object(lifecycle, 'fingerprint', return_value='abc'), mock.patch.object(acceptance, 'host_run', return_value={'status': 'error'}):
        with pytest.raises(ValueError):
            acceptance.warmup(host)
    assert (root / 'warmup.pending.json').exists()


def test_failed_deployment_cannot_push(host):
    publisher = object.__new__(publish.Publisher)
    publisher.args = argparse.Namespace(offline=False, reuse_validation=True, resume=False,
        host_profile='private', cutover='private', identity_label=[], expect_endpoint=[],
        profile='private', host='example.invalid', remote_root='/home/test')
    publisher.commit = 'a' * 40
    publisher.units = {'unit': b'x'}
    plan = {'blockers': [], 'remoteUrl': 'https://github.com/guomxin/cli-helper.git'}
    with mock.patch.object(publisher, 'plan', return_value=plan), \
         mock.patch.object(lifecycle, 'settings', return_value=host), \
         mock.patch.object(lifecycle, 'authorize'), \
         mock.patch.object(lifecycle, 'fingerprint', return_value='same'), \
         mock.patch.object(publish, 'environment', return_value=publisher.units), \
         mock.patch.object(publish, 'git', return_value=publisher.commit), \
         mock.patch.object(publish, 'test_run'), \
         mock.patch.object(publish, 'run', return_value='{}') as execute, \
         mock.patch.object(publisher, 'deploy', side_effect=RuntimeError('injected failure')):
        with pytest.raises(RuntimeError, match='injected failure'):
            publisher.execute()
    assert not any('push' in c.args[0] for c in execute.call_args_list)


def test_preparation_profile_cannot_activate_with_cutover_flags(host):
    host['preparationOnly'] = True
    with mock.patch.object(lifecycle, 'cutover') as verify:
        with pytest.raises(ValueError, match='Preparation'):
            lifecycle.authorize(host, 'unused')
    verify.assert_not_called()


def test_reviewed_profile_and_final_source_binding(host):
    host.update(preparationOnly=False, profileSha256='reviewed', sourceManifestSha256='final', sourceCommit='commit')
    evidence={'reviewedHostProfileSha256':'reviewed','transferMode':'ssh-direct',
              'expectedManifestSha256':'final','expectedSourceCommit':'commit'}
    with mock.patch.object(lifecycle,'cutover',return_value=evidence):
        assert lifecycle.authorize(host,'unused') == evidence
        host['sourceManifestSha256']='snapshot'
        with pytest.raises(ValueError,match='final handoff'): lifecycle.authorize(host,'unused')
        host['sourceManifestSha256']='final';host['profileSha256']='changed'
        with pytest.raises(ValueError,match='reviewed'): lifecycle.authorize(host,'unused')
