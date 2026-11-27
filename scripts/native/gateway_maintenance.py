"""Explicit, candidate-bound Gateway stop/start for a plugin archive switch.

The receipt is local operator evidence, like host authorization, not a remote
attestation. An unrelated disabled job never becomes permission to enable it.
"""
from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import time

from common import external, private_file, read, save, sha, timestamp
import lifecycle

SCHEMA = 'agentbridge.gateway-publish-maintenance.v1'


def sync_directory(path):
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def persist(path, value):
    save(path, value)
    sync_directory(path)


def command(argv):
    try:
        return subprocess.run([str(x) for x in argv], capture_output=True,
                              text=True, timeout=10,
                              env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
    except subprocess.TimeoutExpired:
        raise RuntimeError('Gateway maintenance observation timed out') from None


def loaded():
    result = command(['/bin/launchctl', 'print', lifecycle.service_target('gateway')])
    if result.returncode == 0:
        return True
    # Do not turn permissions, IPC failures or arbitrary nonzero exits into an
    # absent job. This is the native launchctl service-not-found diagnostic.
    if re.search(r'(?im)^\s*Could not find service "ai\.agentbridge\.gateway" in domain for ', result.stderr):
        return False
    raise ValueError('Gateway launchd state is unknown; maintenance cannot continue')


def disabled():
    result = command(['/bin/launchctl', 'print-disabled', f'gui/{os.getuid()}'])
    if result.returncode or not re.search(r'(?s)disabled services = \{.*\}\s*$', result.stdout):
        raise ValueError('Gateway disabled state is unknown')
    match = re.search(r'(?m)^\s*"ai\.agentbridge\.gateway"\s*=>\s*(\S+)\s*$', result.stdout)
    if match and match[1] in ('true', 'false', 'enabled', 'disabled'):
        return match[1] in ('true', 'disabled')
    if 'ai.agentbridge.gateway' in result.stdout:
        raise ValueError('Gateway disabled override is unrecognized')
    return False  # No override in a complete successful result: launchd default.


def installed(config):
    path = private_file(Path.home() / 'Library/LaunchAgents' / (lifecycle.LABELS['gateway'] + '.plist'))
    data = path.read_bytes()
    if data != plistlib.dumps(lifecycle.plists(config)['gateway']):
        raise ValueError('Installed Gateway plist differs from reviewed bytes')
    return sha(data)


def no_listener(config):
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', config['port'])) == 0:
            raise ValueError('Gateway maintenance requires a released listener')


def original_exited(original):
    result = command(['/bin/ps', '-p', str(original['pid']), '-o', 'lstart='])
    if result.returncode == 1 and not result.stdout.strip():
        return True
    if result.returncode:
        raise ValueError('Original Gateway process exit is unverified')
    return result.stdout.strip() != original['started']


def stopped(config, receipt):
    if loaded() or not disabled() or not original_exited(receipt['originalRuntime']):
        raise ValueError('Original Gateway has not completed its authorized maintenance stop')
    no_listener(config)


def stop(config, authorization, candidate, replacement, destination):
    if not re.fullmatch(r'[0-9a-f]{40}', candidate or ''):
        raise ValueError('Maintenance stop requires the exact publish candidate')
    if lifecycle.run(['git', 'rev-parse', 'HEAD']) != candidate:
        raise ValueError('Maintenance candidate differs from this checkout')
    lifecycle.authorize(config, authorization)
    replacement = private_file(replacement)
    before, after = read(config['configPath']), read(replacement)
    expected = copy.deepcopy(before)
    old_paths = before.get('plugins', {}).get('load', {}).get('paths')
    new_paths = after.get('plugins', {}).get('load', {}).get('paths')
    if (not isinstance(old_paths, list) or not isinstance(new_paths, list)
            or len(old_paths) != len(new_paths)
            or sum(a != b for a, b in zip(old_paths, new_paths)) != 1
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in new_paths)):
        raise ValueError('Maintenance replacement must change exactly one absolute plugin load path')
    expected['plugins']['load']['paths'] = new_paths
    if expected != after:
        raise ValueError('Maintenance replacement contains changes outside the plugin load path')
    if Path(destination).expanduser().is_symlink():
        raise ValueError('Maintenance receipt must not be a symbolic link')
    destination = external(destination)
    # Creation is exclusive. Never overwrite a previous stop/start boundary.
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lifecycle.lease(config):
        plist_sha = installed(config)
        if not loaded() or disabled():
            raise ValueError('Maintenance requires an originally loaded, enabled Gateway')
        original = lifecycle.observe(config)
        target = {**config, 'configPath': str(replacement)}
        receipt = {'schema': SCHEMA, 'phase': 'stopping', 'candidate': candidate,
                   'bindings': lifecycle.authorization_bindings(config),
                   'authorizationSha256': sha(private_file(authorization).read_bytes()),
                   'installedPlistSha256': plist_sha,
                   'originalConfigSha256': sha(private_file(config['configPath']).read_bytes()),
                   'replacementConfigSha256': sha(replacement.read_bytes()),
                   'targetFingerprint': lifecycle.fingerprint(target),
                   'originalRuntime': original, 'createdAt': timestamp()}
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(destination)
        lifecycle.run(['/bin/launchctl', 'disable', lifecycle.service_target('gateway')])
        lifecycle.run(['/bin/launchctl', 'bootout', lifecycle.service_target('gateway')])
        deadline = time.monotonic() + 45
        while True:
            try:
                stopped(config, receipt)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise ValueError('Maintenance stop is incomplete; inspect the original receipt') from None
                time.sleep(0.25)
        receipt.update(phase='stopped', stoppedAt=timestamp())
        persist(destination, receipt)
    return {'status': 'stopped-for-publish', 'maintenanceReceipt': str(destination),
            'candidate': candidate, 'configurationChanged': False}


def check(config, authorization, candidate, path, *, runtime=True):
    lifecycle.authorize(config, authorization)
    path = private_file(path)
    receipt = read(path)
    created = datetime.fromisoformat(receipt.get('createdAt', ''))
    now = datetime.now(timezone.utc)
    if (receipt.get('schema') != SCHEMA or receipt.get('candidate') != candidate
            or receipt.get('phase') not in ('stopped', 'started')
            or receipt.get('bindings') != lifecycle.authorization_bindings(config)
            or receipt.get('authorizationSha256') != sha(private_file(authorization).read_bytes())
            or receipt.get('replacementConfigSha256') != sha(private_file(config['configPath']).read_bytes())
            or receipt.get('targetFingerprint') != lifecycle.fingerprint(config)
            or receipt.get('installedPlistSha256') != installed(config)
            or created.tzinfo is None or not now - timedelta(hours=24) <= created <= now):
        raise ValueError('Gateway maintenance receipt is stale, incomplete or does not match this publication')
    if runtime:
        if receipt['phase'] == 'stopped':
            stopped(config, receipt)
        elif not loaded() or disabled() or lifecycle.observe(config) != receipt.get('startedRuntime'):
            raise ValueError('Gateway changed after maintenance start; no automatic re-enable')
    return receipt


def start(config, authorization, candidate, path):
    """Called only by publish after the central deployment has returned success."""
    with lifecycle.lease(config):
        receipt = check(config, authorization, candidate, path)
        if receipt['phase'] == 'started':
            return {'status': 'retained-maintenance-start'}
        # Seal before enable: an interruption cannot silently grant a second
        # start. Unknown outcomes require inspection, never an automatic retry.
        receipt.update(phase='starting', startAttemptedAt=timestamp())
        persist(path, receipt)
        root = Path(config['runtimeDir'])
        current = receipt['targetFingerprint']
        save(root / 'warmup.pending.json', {'fingerprint': current})
        lifecycle.run(['/bin/launchctl', 'enable', lifecycle.service_target('gateway')])
        path_plist = Path.home() / 'Library/LaunchAgents' / (lifecycle.LABELS['gateway'] + '.plist')
        lifecycle.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', path_plist])
        deadline = time.monotonic() + 600
        while True:
            try:
                observed = lifecycle.observe(config)
                break
            except (RuntimeError, ValueError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Gateway readiness timed out; maintenance remains starting') from None
                time.sleep(1)
        if lifecycle.fingerprint(config) != current or installed(config) != receipt['installedPlistSha256']:
            raise ValueError('Gateway inputs changed during maintenance start')
        save(root / 'gateway-baseline.json', {'fingerprint': current, 'runtime': observed})
        receipt.update(phase='started', startedRuntime=observed, startedAt=timestamp())
        persist(path, receipt)
    return {'status': 'started-awaiting-acceptance', 'kind': 'gateway'}
