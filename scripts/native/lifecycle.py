"""Single launchd owner, explicit cutover gate, content-based conditional restart."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import time
import sys
import urllib.request
from common import ROOT, environment_file, external, private_file, read, run, save, sha

LABELS = {'gateway': 'ai.agentbridge.gateway', 'tunnel': 'ai.agentbridge.workspace-tunnel'}


def settings(path):
    value = read(private_file(path))
    value['profileSha256'] = sha(private_file(path).read_bytes())
    if value.get('schema') != 'agentbridge.mac-host.v1':
        raise ValueError('Invalid mac host profile')
    for name in ('stateDir', 'configPath', 'envFile', 'runtimeDir', 'caCertificate'):
        value[name] = str(external(value[name]))
    for name in ('node', 'openclaw'):
        p = Path(value[name]).expanduser()
        if not p.is_absolute() or not p.is_file():
            raise ValueError('Absolute installed runtime paths required')
        value[name] = str(p.resolve())
    for name in ('configPath', 'envFile'):
        private_file(value[name])
    host_config = read(value['configPath'])
    diagnostics = host_config.get('diagnostics', {})
    if (diagnostics.get('stuckSessionWarnMs') != 30000
            or diagnostics.get('stuckSessionAbortMs') != 120000):
        raise ValueError('Reviewed host config must preserve 30000/120000ms stuck-session guardrails')
    if host_config.get('gateway', {}).get('auth', {}).get('mode') == 'none':
        raise ValueError('Production Gateway authentication cannot be disabled')
    if not Path(value['stateDir']).is_dir() or not Path(value['caCertificate']).is_file():
        raise ValueError('State directory and original CA certificate required')
    value['port'] = int(value.get('port', 18789))
    tunnel = value['tunnel']
    tunnel['remotePort'] = int(tunnel.get('remotePort', 18789))
    for p in (value['port'], tunnel['remotePort']):
        if not 1 <= p <= 65535:
            raise ValueError('Invalid port')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', tunnel['host']) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', tunnel['user']):
        raise ValueError('Invalid tunnel target')
    tunnel['identityFile'] = str(private_file(tunnel['identityFile']))
    tunnel['knownHostsFile'] = str(external(tunnel['knownHostsFile']))
    if not Path(tunnel['knownHostsFile']).is_file():
        raise ValueError('Pinned known-hosts file required')
    env = environment_file(value['envFile'])
    if os.environ.get('NODE_TLS_REJECT_UNAUTHORIZED') == '0':
        raise ValueError('TLS verification disabled in caller')
    # Pin the installed host, never rely on launchd shell PATH or default config.
    env.update(HOME=str(Path.home()), PATH=str(Path(value['node']).parent) + ':/usr/bin:/bin:/usr/sbin:/sbin',
               OPENCLAW_STATE_DIR=value['stateDir'], OPENCLAW_CONFIG_PATH=value['configPath'],
               NODE_EXTRA_CA_CERTS=value['caCertificate'])
    if not re.search(r'^OpenClaw 2026\.7\.1(?:\s|$)', run([value['node'], value['openclaw'], '--version']), re.M):
        raise ValueError('OpenClaw must be pinned at 2026.7.1')
    value['environment'] = env
    return value


def plists(config):
    runtime = Path(config['runtimeDir'])
    shared = {'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
              'ProcessType': 'Background', 'WorkingDirectory': config['stateDir'],
              'Umask': 0o077}
    gateway = {**shared, 'Label': LABELS['gateway'], 'EnvironmentVariables': config['environment'],
               'ProgramArguments': [config['node'], config['openclaw'], 'gateway', 'run',
                                    '--bind', 'loopback', '--port', str(config['port'])]}
    tunnel = config['tunnel']
    ssh = ['/usr/bin/ssh', '-F', '/dev/null', '-N', '-T', '-o', 'BatchMode=yes', '-o',
           'ExitOnForwardFailure=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
           '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2', '-o', 'ConnectTimeout=10',
           '-o', 'UserKnownHostsFile=' + tunnel['knownHostsFile'], '-i', tunnel['identityFile'],
           '-R', f"127.0.0.1:{tunnel['remotePort']}:127.0.0.1:{config['port']}",
           tunnel['user'] + '@' + tunnel['host']]
    result = {'gateway': gateway, 'tunnel': {**shared, 'Label': LABELS['tunnel'], 'ProgramArguments': ssh}}
    for key, value in result.items():
        value['StandardOutPath'] = str(runtime / (key + '.out.log'))
        value['StandardErrorPath'] = str(runtime / (key + '.err.log'))
    return result


def render(config, destination):
    destination = external(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for key, data in plists(config).items():
        path = destination / (LABELS[key] + '.plist')
        with path.open('xb') as stream:
            stream.write(plistlib.dumps(data))
        path.chmod(0o600)
        run(['/usr/bin/plutil', '-lint', path])
    return {'status': 'rendered-only', 'directory': str(destination), 'loaded': False}


def cutover(path):
    """Local operator evidence; not a substitute for live source/server inspection."""
    value = read(private_file(path))
    if value.get('schema') != 'agentbridge.mac-cutover.v1':
        raise ValueError('Explicit cutover evidence required')
    for name in ('windowsGatewayStopped', 'windowsGuardDisabled', 'windowsTunnelStopped',
                 'serverForwardReleased', 'm2Accepted'):
        if value.get(name) is not True:
            raise ValueError('Cutover blocked: ' + name)
    if value.get('transferMode') == 'ssh-direct':
        sys.path.insert(0, str(ROOT / 'scripts/migration'))
        from direct_transfer import verify
        verify(external(value['stagedDirectory']), external(value['transferReceipt']),
               value['expectedManifestSha256'], value['expectedSourceCommit'], require_final=True)
        return value
    manifest = read(external(value['stagedManifest']))
    receipt = read(external(value['bundleReceipt']))
    if (manifest.get('schema') != 'agentbridge.mac-migration.bundle.v1'
            or manifest.get('kind') != 'final' or receipt.get('kind') != 'final'
            or not re.fullmatch(r'[0-9a-f]{64}', value.get('expectedSha256', ''))
            or receipt.get('sha256') != value['expectedSha256']
            or receipt.get('sourceCommit') != manifest.get('sourceCommit')
            or receipt.get('planSha256') != manifest.get('planSha256')):
        raise ValueError('Final bundle evidence mismatch; snapshot cannot activate production')
    bundle = external(value['bundlePath'])
    if sha(bundle.read_bytes()) != value['expectedSha256']:
        raise ValueError('Final encrypted bundle hash mismatch')
    stage = external(value['stagedManifest']).parent
    for row in manifest['files']:
        p = (stage / row['name']).resolve()
        if not p.is_relative_to(stage) or sha(p.read_bytes()) != row['sha256']:
            raise ValueError('Final staging contents changed')
    return value


def authorize(config, evidence):
    if config.get('preparationOnly') is not False:
        raise ValueError('Preparation or unclassified host profile cannot activate production')
    value = cutover(evidence)
    if value.get('reviewedHostProfileSha256') != config.get('profileSha256'):
        raise ValueError('Host profile differs from reviewed cutover configuration')
    if value.get('transferMode') == 'ssh-direct':
        if (config.get('sourceManifestSha256') != value['expectedManifestSha256']
                or config.get('sourceCommit') != value['expectedSourceCommit']):
            raise ValueError('Host configuration was not adapted from this final handoff')
    return value


def fingerprint(config):
    plugin = ROOT / 'integrations/openclaw-agentbridge'
    paths = {f'plugin/{n}': plugin / n for n in ('index.js', 'package.json', 'openclaw.plugin.json',
             'package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock')}
    for p in (plugin / 'lib').rglob('*'):
        if p.is_file():
            paths['plugin/' + p.relative_to(plugin).as_posix()] = p
    paths.update({'host/config': Path(config['configPath']), 'host/env': Path(config['envFile']),
                  'workspace/env': ROOT / '.env', 'host/ca': Path(config['caCertificate']),
                  'host/node': Path(config['node']), 'host/openclaw': Path(config['openclaw'])})
    files = {}
    for name, path in sorted(paths.items()):
        data = path.read_bytes() if path.is_file() else None
        if data is not None and name.startswith('plugin/') and path.suffix in ('.js', '.mjs', '.cjs', '.json', '.yaml', '.yml', '.lock'):
            try:
                data = data.decode('utf-8').replace('\r\n', '\n').encode('utf-8')
            except UnicodeDecodeError:
                pass
        files[name] = sha(data) if data is not None else None
    files['host/launchd'] = sha(plistlib.dumps(plists(config)['gateway']))
    return sha(json.dumps(files, sort_keys=True).encode())


def service_target(kind):
    return f'gui/{os.getuid()}/{LABELS[kind]}'


def observe(config):
    output = run(['/bin/launchctl', 'print', service_target('gateway')])
    match = re.search(r'^\s*pid = (\d+)\s*$', output, re.M)
    if not match:
        raise ValueError('launchd gateway not running')
    pid = int(match[1])
    started = run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                  env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
    listeners = run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(config['port']), '-sTCP:LISTEN', '-Fpn'])
    owners = {int(line[1:]) for line in listeners.splitlines() if line.startswith('p')}
    addresses = [line[1:] for line in listeners.splitlines() if line.startswith('n')]
    if owners != {pid} or not addresses or any(not a.startswith(('127.0.0.1:', '[::1]:')) for a in addresses):
        raise ValueError('Gateway listener ownership/bind mismatch')
    with urllib.request.urlopen(f"http://127.0.0.1:{config['port']}/readyz", timeout=5) as response:
        ready = json.load(response)
    if ready.get('ready') is not True or ready.get('failing'):
        raise ValueError('Gateway not ready')
    return {'pid': pid, 'started': started}


def decision(current, baseline, runtime, force=False):
    reason = ('forced' if force else 'gateway_not_ready' if runtime is None else
              'baseline_missing' if not baseline else 'gateway_process_changed' if baseline.get('runtime') != runtime else
              'inputs_changed' if baseline.get('fingerprint') != current else 'inputs_unchanged')
    return {'required': reason != 'inputs_unchanged', 'reason': reason, 'fingerprint': current}


@contextmanager
def lease(config):
    root = external(config['runtimeDir'])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / 'lifecycle.lock').open('a') as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def manage(config, action, kind, evidence=None):
    if action in ('start', 'restart'):
        authorize(config, evidence)
    with lease(config):
        target = service_target(kind)
        if action == 'status':
            # Print output is private runtime data, return only state here.
            output = run(['/bin/launchctl', 'print', target])
            return {'loaded': True, 'running': bool(re.search(r'^\s*pid = \d+', output, re.M))}
        if action == 'stop':
            # bootout alone would start again at the next GUI login.
            run(['/bin/launchctl', 'disable', target])
            run(['/bin/launchctl', 'bootout', target])
            return {'status': 'stopped', 'kind': kind}
        root = Path(config['runtimeDir'])
        baseline_path = root / 'gateway-baseline.json'
        current = fingerprint(config)
        baseline = read(baseline_path) if baseline_path.exists() else None
        try:
            runtime = observe(config)
        except (RuntimeError, ValueError, OSError):
            runtime = None
        plan = decision(current, baseline, runtime)
        if kind == 'gateway' and action == 'restart' and not plan['required']:
            return {'status': 'retained', **plan}
        # Never replace a listener owned by another supervisor.
        if kind == 'gateway' and runtime is None:
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', config['port'])) == 0:
                    raise ValueError('Unverified listener exists; inspect before lifecycle action')
        agents = Path.home() / 'Library/LaunchAgents'
        agents.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = agents / (LABELS[kind] + '.plist')
        data = plistlib.dumps(plists(config)[kind])
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Installed launchd input changed; stop and review installation first')
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(data)
            path.chmod(0o600)
        if kind == 'gateway':
            save(root / 'warmup.pending.json', {'fingerprint': current})
        if action == 'start':
            run(['/bin/launchctl', 'enable', target])
            run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', path])
        else:
            run(['/bin/launchctl', 'kickstart', '-k', target])
        if kind == 'gateway':
            deadline = time.monotonic() + 600
            while True:
                try:
                    after = observe(config)
                    break
                except (RuntimeError, ValueError, OSError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Gateway readiness timed out; acceptance remains pending')
                    time.sleep(1)
            if fingerprint(config) != current:
                raise ValueError('Inputs changed during startup; cannot certify baseline')
            save(baseline_path, {'fingerprint': current, 'runtime': after})
        return {'status': 'started-awaiting-acceptance', 'kind': kind, 'linkVerified': False}
