"""Native read-only acceptance and isolated no-tool cold/hot warm-up."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shlex
import ssl
import time
import urllib.request
from common import ROOT, read, run
import lifecycle


def host_run(host, args, timeout=180):
    env = {**os.environ, **host['environment']}
    return json.loads(run([host['node'], host['openclaw'], *args], env=env, timeout=timeout))


def smoke(host, check='Release', label=None):
    config = read(host['configPath'])
    env = {**os.environ, **host['environment']}
    def resolve(value):
        def replace(match):
            if not env.get(match[1]):
                raise ValueError('Missing MCP environment reference')
            return env[match[1]]
        return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', replace, value)
    server = config.get('mcp', {}).get('servers', {}).get('agentbridge')
    selected = None
    if server:
        if label:
            raise ValueError('Identity selection requires plugin configuration')
        resolved = {'url': resolve(server['url']), 'timeout': server.get('timeout', 60),
                    'headers': {'Authorization': resolve(server['headers']['Authorization'])}}
    else:
        plugin = config['plugins']['entries']['agentbridge-interactions']['config']
        bindings = [b for b in plugin['identityBindings'] if env.get(b.get('tokenEnv'))
                    and (label is None or b.get('label', '').lower() == label.lower())]
        if not bindings or (label and len(bindings) != 1):
            raise ValueError('Identity binding did not resolve uniquely')
        binding = bindings[0]
        selected = binding.get('label')
        resolved = {'url': resolve(plugin['mcpUrl']), 'timeout': plugin.get('mcpTimeoutSeconds', 60),
                    'headers': {'Authorization': 'Bearer ' + env[binding['tokenEnv']]}}
    if not resolved['url'].startswith('https://') or not resolved['headers']['Authorization'].startswith('Bearer '):
        raise ValueError('Authenticated HTTPS MCP endpoint required')
    args = [host['node'], ROOT / 'scripts/agentbridge-mcp-smoke.mjs', '--check', check, '--server-name', 'agentbridge']
    if selected:
        args += ['--identity-label', selected]
    result = json.loads(run(args, env=env, input=json.dumps(resolved), timeout=180))
    if result.get('status') != 'succeeded':
        raise ValueError('MCP smoke failed')
    return result


def warmup(host):
    pending = Path(host['runtimeDir']) / 'warmup.pending.json'
    if not pending.exists():
        return {'status': 'skipped', 'reason': 'no_pending_warmup'}
    before = lifecycle.observe(host)
    fingerprint = lifecycle.fingerprint(host)
    baseline = read(Path(host['runtimeDir']) / 'gateway-baseline.json')
    if (lifecycle.decision(fingerprint, baseline, before)['required']
            or read(pending).get('fingerprint') != fingerprint):
        raise ValueError('Pending warmup inputs are not verified')
    for label, timeout in [('cold', 420), ('hot', 90)]:
        started = time.monotonic()
        result = host_run(host, ['agent', '--agent', 'main', '--session-key',
            'agent:main:agentbridge-release-warmup', '--message',
            f'AgentBridge deployment warm-up {label}. Do not call any tool. Reply exactly READY.',
            '--thinking', 'off', '--timeout', str(timeout), '--json'], timeout + 15)
        reply = result.get('result', {}).get('meta', {}).get('finalAssistantVisibleText')
        if result.get('status') != 'ok' or result.get('summary') != 'completed' or reply != 'READY':
            raise ValueError('Warmup failed; pending evidence retained')
        if label == 'hot' and time.monotonic() - started > 180:
            raise ValueError('Hot path exceeded maximum duration')
    if lifecycle.observe(host) != before or lifecycle.fingerprint(host) != fingerprint:
        raise ValueError('Gateway changed during warmup')
    pending.unlink()
    return {'status': 'succeeded'}


def runtime(host):
    observed = lifecycle.observe(host)
    gateway = host_run(host, ['gateway', 'status', '--require-rpc', '--json'])
    plugin = host_run(host, ['plugins', 'inspect', 'agentbridge-interactions', '--json'])
    expected = read(ROOT / 'integrations/openclaw-agentbridge/package.json')['version']
    if (not gateway.get('rpc', {}).get('ok')
            or gateway.get('cli', {}).get('version') != '2026.7.1'
            or gateway.get('gateway', {}).get('version') != '2026.7.1'
            or gateway.get('pluginVersionDrift', {}).get('drifts')
            or plugin.get('plugin', {}).get('status') != 'loaded'
            or plugin.get('plugin', {}).get('version') != expected):
        raise ValueError('Gateway RPC/version/plugin mismatch')
    # CLI plugin inspection alone is insufficient: require current Gateway registration.
    started = run(['/bin/ps', '-p', str(observed['pid']), '-o', 'lstart='])
    from datetime import datetime
    started_at = datetime.strptime(started.strip(), '%a %b %d %H:%M:%S %Y').timestamp()
    log_dir = Path(host.get('openclawLogDir', '/tmp/openclaw'))
    registered = False
    for path in log_dir.glob('openclaw-*.log'):
        for line in path.read_text(errors='replace').splitlines()[-4000:]:
            try:
                item = json.loads(line)
                message = item.get('message', '')
                match = re.match(r'AgentBridge interaction plugin registered \(version=([^,]+),', message)
                if match and match[1] == expected and datetime.fromisoformat(item['time'].replace('Z', '+00:00')).timestamp() >= started_at - 5:
                    registered = True
            except (ValueError, KeyError, TypeError):
                continue
    if not registered or lifecycle.observe(host) != observed:
        raise ValueError('No registration from current Gateway process')
    return {'rpc': 'ok', 'version': '2026.7.1', 'pluginVersion': expected}


def acceptance(publisher, host, labels=(), endpoints=()):
    root = publisher.args.remote_root
    context = ssl.create_default_context(cafile=host['caCertificate'])
    for port in (8782, 8783):
        with urllib.request.urlopen(f'https://{publisher.args.host}:{port}/readyz', context=context, timeout=15) as response:
            if json.load(response).get('status') != 'ready':
                raise ValueError('Remote TLS/readiness failed')
    release = smoke(host)
    command = '\n'.join(['set -euo pipefail',
        'systemctl is-active --quiet agentbridge',
        'test "$(systemctl show agentbridge -p MainPID --value)" -gt 0',
        'test "$(sed -n \'s/^AGENTBRIDGE_RELEASE_ID=//p\' ' + shlex.quote(root + '/config/release.env') + ')" = ' + shlex.quote(publisher.release),
        'systemctl is-active --quiet agentbridge-backup.timer',
        'test "$(systemctl show agentbridge-backup.service -p Result --value)" = success',
        "journalctl -q -u agentbridge --since '-30 minutes' --priority=err --no-pager | wc -l"])
    recent_errors = int(publisher.ssh('bash -s', input=command))
    forward_probe = ('import json,urllib.request; '
                     'r=json.load(urllib.request.urlopen("http://127.0.0.1:'
                     + str(host['tunnel']['remotePort']) + '/readyz",timeout=5)); '
                     'assert r.get("ready") is True and not r.get("failing")')
    publisher.ssh(shlex.join([root + '/current/venv/bin/python', '-I', '-c', forward_probe]))
    verified_runtime = runtime(host)
    if Path(host['runtimeDir'], 'warmup.pending.json').exists():
        raise ValueError('Warmup still pending')
    if labels:
        if len(labels) < 2 or len(set(x.lower() for x in labels)) != len(labels):
            raise ValueError('At least two distinct identity labels required')
        baselines = {}
        for cycle in range(2):
            subjects = set()
            for label in labels:
                result = smoke(host, 'SessionStatus', label)
                if result.get('sessionStatus') != 'active' or not result.get('userSubject') or not result.get('sessionId'):
                    raise ValueError('Identity session is not active')
                identity = (result.get('systemId'), result['userSubject'], result['sessionId'])
                if label in baselines and baselines[label] != identity:
                    raise ValueError('Identity changed during stability check')
                baselines[label] = identity
                subjects.add(result['userSubject'])
            if len(subjects) != len(labels):
                raise ValueError('Identity subjects overlap')
            if cycle == 0:
                time.sleep(1)
        if not endpoints:
            raise ValueError('Explicit expected omnichannel endpoints required')
        args = [root + '/current/venv/bin/python', '-P', '-m', 'bscli.cli.main', '--home', root + '/data', 'diagnostics', 'omnichannel']
        for endpoint in endpoints:
            if not re.fullmatch(r'[A-Za-z0-9_.@-]+=[A-Za-z0-9_.-]+', endpoint):
                raise ValueError('Invalid endpoint expectation')
            args += ['--expect-endpoint', endpoint]
        if json.loads(publisher.ssh(shlex.join(args))).get('status') != 'succeeded':
            raise ValueError('Omnichannel isolation failed')
    return {'status': 'succeeded', 'businessWrites': 0, 'recentErrorCount': recent_errors,
            'gateway': verified_runtime, 'releaseSmoke': release['status'], 'identities': len(labels)}
