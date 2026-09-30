"""Create a separate, inactive macOS preparation copy from a verified SSH snapshot."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
from direct_transfer import verify


def write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def adapt(source, receipt, digest, commit, destination, repo, node, openclaw):
    source, destination, repo = source.resolve(), destination.absolute(), repo.resolve()
    if destination.exists() or destination.is_relative_to(source) or destination.is_relative_to(repo):
        raise ValueError('Adaptation requires a new directory outside source and repository')
    result = verify(source, receipt, digest, commit)
    destination.mkdir(mode=0o700, parents=True)
    state = destination / 'openclaw'
    shutil.copytree(source / 'openclaw', state)
    for name in ('deploy', 'ssh', 'portable-ca'):
        shutil.copytree(source / name, destination / name)
    if (source / 'host-workspace-git').is_dir():
        shutil.copytree(source / 'host-workspace-git', state / 'workspace/.git')
    for path in destination.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    for name in ('gateway.cmd', 'gateway.vbs'):
        (state / name).unlink(missing_ok=True)
    profile_path = destination / 'deploy/environment.json'
    profile = json.loads(profile_path.read_text(encoding='utf-8-sig'))
    for name, entry in profile['units'].items():
        target = destination / 'deploy' / name
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('Reviewed production unit hash differs from snapshot')
        entry['path'] = str(target)
    profile_path.unlink()
    write(profile_path, profile)
    config = json.loads((source / 'openclaw-config/openclaw.json').read_text(encoding='utf-8-sig'))
    runtime = json.loads((source / 'source-evidence/runtime-paths.json').read_text())
    mappings = [(runtime['stateDir'].replace('\\', '/'), str(state)),
                (str(PureWindowsPath(runtime['stateDir']).parent / '.agentbridge/pki').replace('\\', '/'), str(destination / 'portable-ca'))]
    for source_path in runtime.get('pluginPaths', []):
        if source_path.replace('\\', '/').endswith('/integrations/openclaw-agentbridge'):
            mappings.append((source_path.replace('\\', '/'), str(repo / 'integrations/openclaw-agentbridge')))
    changed = []
    def convert(value, key=''):
        if isinstance(value, dict):
            return {k: convert(v, key + '/' + k) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v, key + '/' + str(i)) for i, v in enumerate(value)]
        if isinstance(value, str) and re.match(r'^[A-Za-z]:[\\/]', value):
            normalized = value.replace('\\', '/')
            for old, new in mappings:
                if normalized == old or normalized.startswith(old + '/'):
                    changed.append(key)
                    return new + normalized[len(old):]
            raise ValueError('Unmapped Windows configuration path at ' + key)
        return value
    config = convert(config)
    # Preparation copy cannot receive messages even if somebody starts it by mistake.
    for channel in config.get('channels', {}).values():
        if isinstance(channel, dict):
            channel['enabled'] = False
    config['plugins']['entries']['openclaw-weixin']['enabled'] = False
    config.setdefault('update', {}).setdefault('auto', {})['enabled'] = False
    config['gateway']['bind'] = 'loopback'
    path = state / 'openclaw.json'
    path.unlink()
    write(path, config)
    # Preserve exact dependency lockfiles. Reinstall via npm ci in this copy only.
    projects = []
    for path in sorted((state / 'npm/projects').glob('*/package.json')):
        package = json.loads(path.read_text())
        projects.append({'directory': str(path.parent), 'dependencies': package.get('dependencies', {})})
    skill_root = state / 'plugin-skills'
    skill_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    distribution = openclaw.resolve().parent
    links = {'browser-automation': distribution / 'dist/extensions/browser/skills/browser-automation',
             'canvas': distribution / 'dist/extensions/canvas/skills/canvas'}
    for name, target in links.items():
        link = skill_root / name
        if not target.is_dir() or link.exists() or link.is_symlink():
            raise ValueError('Missing same-version skill or conflicting destination')
        link.symlink_to(target, target_is_directory=True)
    host = {'schema': 'agentbridge.mac-host.v1', 'preparationOnly': True,
            'sourceManifestSha256': digest, 'sourceCommit': commit,
            'node': str(node.resolve()), 'openclaw': str(openclaw.resolve()),
            'stateDir': str(state), 'configPath': str(state / 'openclaw.json'),
            'envFile': str(state / '.env'), 'runtimeDir': str(destination / 'runtime'),
            'caCertificate': str(destination / 'portable-ca/root-ca.crt'), 'port': 18789,
            'tunnel': {'host': profile['host'], 'user': 'root', 'remotePort': 18789,
                       'identityFile': str(destination / 'ssh/id_ed25519_10_10_50_213'),
                       'knownHostsFile': str(destination / 'ssh/known_hosts')}}
    write(destination / 'mac-host.json', host)
    write(destination / 'adaptation.json', {'schema': 'agentbridge.mac-adaptation.v1',
        'status': 'prepared-inactive', 'source': result, 'changedConfigPaths': changed,
        'channelsDisabled': True, 'projects': projects, 'historyRewritten': False,
        'originalSnapshotModified': False, 'productionActivated': False})
    return {'status': 'prepared-inactive', 'projects': len(projects), 'mappedConfigPaths': changed,
            'destination': str(destination), 'productionActivated': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'receipt', 'destination', 'repo', 'node', 'openclaw'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--expected-sha256', required=True)
    p.add_argument('--expected-commit', required=True)
    a = p.parse_args()
    print(json.dumps(adapt(a.source, a.receipt, a.expected_sha256, a.expected_commit,
                           a.destination, a.repo, a.node, a.openclaw)))


if __name__ == '__main__':
    main()
