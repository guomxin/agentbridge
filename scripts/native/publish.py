"""Native release planning/transport. Reuses the existing Linux transaction."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import re
import shlex
import sys
import uuid
from common import ROOT, external, private_file, read, run, save, sha, timestamp
from validation import validate, test_run


def git(*args):
    return run(['git', *args])


def environment(path, host, root):
    profile = read(external(path))
    if profile.get('schema') != 'agentbridge.environment.v1' or profile.get('host') != host or profile.get('root') != root:
        raise ValueError('Environment profile differs from explicit target')
    units = {}
    for name in ('agentbridge.service', 'agentbridge-backup.service', 'agentbridge-backup.timer'):
        entry = profile['units'][name]
        data = external(entry['path']).read_bytes()
        if sha(data) != entry['sha256']:
            raise ValueError('Reviewed environment unit hash mismatch: ' + name)
        units[name] = data
    return units


def compatibility(current, candidate, policy, resume=False):
    if not all(re.fullmatch(r'[0-9a-f]{12}', x) for x in (current, candidate)):
        raise ValueError('Exact current and candidate release IDs required')
    if current == candidate:
        return 'already_current'
    if resume:
        raise ValueError('Resume candidate is not current')
    if (policy.get('schemaVersion') != 'agentbridge.release-policy.v1'
            or current not in policy.get('compatibleFrom', [])
            or policy.get('dataCompatibility') not in ('no-migration', 'reviewed-schema-transition')
            or (policy['dataCompatibility'] == 'reviewed-schema-transition' and not policy.get('schemaTransitions'))):
        raise ValueError('Release policy does not authorize deployed predecessor')
    return 'compatible'


class Publisher:
    def __init__(self, args):
        self.args = args
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', args.host)
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]*', args.user)
                or not re.fullmatch(r'/home/[A-Za-z0-9._/-]+', args.remote_root)
                or '..' in args.remote_root):
            raise ValueError('Invalid explicit deployment target')
        self.units = environment(args.profile, args.host, args.remote_root)
        self.policy = read(ROOT / 'deploy/release-policy.json')
        self.commit = git('rev-parse', 'HEAD')
        if not re.fullmatch(r'[0-9a-f]{40}', self.commit):
            raise ValueError('Invalid candidate commit')
        self.release = self.commit[:12]
        self.target = args.user + '@' + args.host
        self.ssh_options = ['-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                            '-o', 'UserKnownHostsFile=' + str(external(args.known_hosts)),
                            '-i', str(private_file(args.identity))]
        if not external(args.known_hosts).is_file():
            raise ValueError('Known-hosts file required')

    def ssh(self, command, *, input=None, timeout=900):
        return run(['ssh', *self.ssh_options, '-T', self.target, command], input=input, timeout=timeout)

    def plan(self, *, offline=False):
        branch = git('rev-parse', '--abbrev-ref', 'HEAD')
        remote = git('remote', 'get-url', '--push', 'origin')
        # Fixed repository allowlist, HTTPS clone and SSH release are both supported.
        if remote not in ('https://github.com/guomxin/cli-helper.git', 'git@github.com:guomxin/cli-helper.git'):
            raise ValueError('Release remote mismatch')
        try:
            main = git('rev-parse', 'refs/heads/main')
        except RuntimeError:
            main = None
        clean = not git('status', '--porcelain', '--untracked-files=no')
        branch_ok = branch == 'main' or (branch == 'HEAD' and main == self.commit)
        current = None if offline else self.ssh("sed -n 's/^AGENTBRIDGE_RELEASE_ID=//p' " + shlex.quote(self.args.remote_root + '/config/release.env'))
        preflight = 'not_checked_offline' if offline else compatibility(current, self.release, self.policy, self.args.resume)
        return {'status': 'planned', 'commit': self.commit, 'branch': branch, 'remoteUrl': remote,
                'trackedFilesClean': clean, 'releaseBranchAllowed': branch_ok,
                'releasePreflight': preflight, 'currentRelease': current,
                'unitHashes': {n: sha(v) for n, v in self.units.items()},
                'resumeCompletion': self.args.resume, 'productionActions': False,
                'validation': 'reuse_receipt' if self.args.reuse_validation or self.args.resume else 'full',
                'requiredAcceptance': ['governance', 'TLS/readiness', 'release-smoke', 'backup',
                    'gateway-runtime', 'conditional-restart', 'pending-warmup', 'identity-isolation'],
                'blockers': ([] if branch_ok else ['candidate must be integrated into main']) +
                            ([] if clean else ['commit tracked changes']) +
                            (['remote predecessor unverified'] if offline else [])}

    def deploy(self, artifact):
        if self.args.resume:
            saved = self.args.remote_root + '/releases/' + self.release
            self.ssh(shlex.join([saved + '/venv/bin/python', '-I', saved + '/release.py',
                                '--complete', saved + '/transaction.json']))
            return
        wheel = Path(artifact['wheel'])
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', wheel.name):
            raise ValueError('Unsafe wheel filename')
        remote_wheel = '/tmp/' + self.release + '-' + uuid.uuid4().hex + '-' + wheel.name
        encode = lambda b: base64.b64encode(b).decode('ascii')
        transaction = {'root': self.args.remote_root, 'releaseId': self.release,
            'service': 'agentbridge', 'host': self.args.host, 'wheel': remote_wheel,
            'wheelName': wheel.name, 'sha256': artifact['sha256'], 'policy': self.policy,
            'artifact': encode(Path(artifact['manifest']).read_bytes()),
            'validation': encode((ROOT / 'output/release-validation/full.json').read_bytes()),
            'units': {n: encode(b) for n, b in self.units.items()}}
        # No package/system dependency installation bypass. Original transaction handles rollback.
        script = '\n'.join([
            'set -euo pipefail', 'umask 077',
            'wheel=' + shlex.quote(remote_wheel),
            'tmp=$(mktemp -d /tmp/agentbridge-native.XXXXXX)',
            "trap 'rm -f -- \"$wheel\"; rm -rf -- \"$tmp\"' EXIT",
            'for cmd in Xvfb x11vnc websockify xauth; do command -v "$cmd" >/dev/null; done',
            'test -d /usr/share/novnc',
            "printf '%s' " + shlex.quote(encode((ROOT / 'scripts/agentbridge_release.py').read_bytes())) + ' | base64 --decode > "$tmp/release.py"',
            "printf '%s' " + shlex.quote(encode(json.dumps(transaction).encode())) + ' | base64 --decode > "$tmp/config.json"',
            shlex.quote(self.args.remote_root + '/venv/bin/python') + ' -I "$tmp/release.py" "$tmp/config.json"',
        ])
        run(['scp', *self.ssh_options, wheel, self.target + ':' + remote_wheel])
        self.ssh('bash -s', input=script, timeout=1800)

    def execute(self):
        from acceptance import acceptance, warmup
        import lifecycle
        if self.args.offline:
            raise ValueError('Offline is plan-only')
        plan = self.plan()
        if plan['blockers']:
            raise ValueError('; '.join(plan['blockers']))
        git_env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        if plan['remoteUrl'].startswith('git@'):
            if not self.args.github_identity or not self.args.github_known_hosts:
                raise ValueError('GitHub SSH push requires explicit identity and pinned known-hosts')
            identity = private_file(self.args.github_identity)
            known = external(self.args.github_known_hosts)
            if not re.search(r'(?m)^github\.com ssh-ed25519 ', known.read_text()):
                raise ValueError('Pinned GitHub ED25519 host key required')
            git_env['GIT_SSH_COMMAND'] = shlex.join(['ssh', '-F', '/dev/null', '-i', str(identity),
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'UserKnownHostsFile=' + str(known), '-o', 'ConnectTimeout=15'])
        host = lifecycle.settings(self.args.host_profile)
        lifecycle.authorize(host, self.args.cutover)
        if self.args.identity_label and (len(set(x.lower() for x in self.args.identity_label)) < 2
                or not self.args.expect_endpoint):
            raise ValueError('Isolation requires two distinct identities and expected endpoints')
        if any(not re.fullmatch(r'[A-Za-z0-9_.@-]+=[A-Za-z0-9_.-]+', e) for e in self.args.expect_endpoint):
            raise ValueError('Invalid endpoint expectation')
        before = lifecycle.fingerprint(host)
        if not (self.args.reuse_validation or self.args.resume):
            validate(full=True)
        artifact = json.loads(run([sys.executable, ROOT / 'scripts/agentbridge_artifact.py', 'verify', '--root', ROOT]))
        test_run([sys.executable, '-m', 'unittest', 'tests.test_runtime_governance', 'tests.test_runtime_backup'])
        # Recheck before the first remote mutation, after potentially long validation.
        if git('rev-parse', 'HEAD') != self.commit or self.plan()['blockers']:
            raise ValueError('Candidate changed during validation')
        if environment(self.args.profile, self.args.host, self.args.remote_root) != self.units:
            raise ValueError('Environment changed during validation')
        self.deploy(artifact)
        if lifecycle.fingerprint(host) != before:
            raise ValueError('Host inputs changed during deployment')
        root = Path(host['runtimeDir'])
        baseline = read(root / 'gateway-baseline.json') if (root / 'gateway-baseline.json').exists() else None
        try:
            observed = lifecycle.observe(host)
        except (RuntimeError, ValueError, OSError):
            observed = None
        restart = lifecycle.decision(before, baseline, observed)
        if restart['required']:
            if self.args.resume:
                raise ValueError('Resume requires verified unchanged Gateway; restart forbidden')
            lifecycle.manage(host, 'restart', 'gateway', self.args.cutover)
        warmup(host)
        result = acceptance(self, host, self.args.identity_label, self.args.expect_endpoint)
        # Push is the last step; no push on any failed acceptance or changed candidate.
        run([sys.executable, ROOT / 'scripts/agentbridge_artifact.py', 'verify', '--root', ROOT])
        if git('rev-parse', 'HEAD') != self.commit:
            raise ValueError('Candidate changed during acceptance')
        run(['git', 'push', '--porcelain', 'origin', 'HEAD:refs/heads/main'], env=git_env)
        if run(['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'], env=git_env).split()[0] != self.commit:
            raise ValueError('GitHub verification mismatch')
        return {'status': 'succeeded', 'commit': self.commit, 'acceptance': result, 'pushed': True}
