"""Verify authenticated SSH handoffs without treating live snapshots as final.

The expected manifest digest and source commit must come from the source handoff,
not be inferred from the received manifest. Never activates or modifies the tree.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

SCHEMA = 'agentbridge.mac-migration.direct.v1'
FINAL_CHECKS = ('gatewayStopped', 'guardDisabled', 'tunnelStopped', 'writersStopped', 'sourceTreeStable')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def verify(directory, receipt_path, expected_sha256, expected_commit, *, require_final=False):
    directory = Path(directory).absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('A real staging directory is required')
    directory = directory.resolve()
    receipt_path = Path(receipt_path).resolve()
    if receipt_path.is_relative_to(directory):
        raise ValueError('Receiver receipt must be outside the immutable staging tree')
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or not re.fullmatch(r'[0-9a-f]{40}', expected_commit):
        raise ValueError('Independent manifest SHA256 and full source commit required')
    manifest_path = directory / 'manifest.json'
    if not stat.S_ISREG(manifest_path.lstat().st_mode) or digest(manifest_path) != expected_sha256:
        raise ValueError('Manifest hash mismatch')
    manifest, receipt = load(manifest_path), load(receipt_path)
    if (manifest.get('schema') != SCHEMA or manifest.get('transport') != 'ssh'
            or manifest.get('kind') not in ('snapshot', 'final')
            or manifest.get('sourceCommit') != expected_commit
            or receipt.get('manifestSha256') != expected_sha256
            or receipt.get('kind') != manifest['kind']
            or receipt.get('status') != 'verified-staged-only' or receipt.get('activated') is not False):
        raise ValueError('Handoff manifest/receiver receipt mismatch')
    if require_final and manifest['kind'] != 'final':
        raise ValueError('Live snapshot is ineligible for production cutover')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or receipt.get('files') != len(rows):
        raise ValueError('Invalid file inventory')
    expected, folded = {}, set()
    for row in rows:
        name = row['name']
        if (not name or '\\' in name or ':' in name or name.startswith('/')
                or any(p in ('', '.', '..') for p in name.split('/'))
                or name == 'manifest.json' or name.casefold() in folded
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                or type(row['size']) is not int or row['size'] < 0):
            raise ValueError('Unsafe or duplicate manifest member')
        folded.add(name.casefold())
        expected[name] = row
    actual, observed = set(), {}
    for path in directory.rglob('*'):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('Links and special files are forbidden in staging')
        if not path.is_file() or path == manifest_path:
            continue
        name = path.relative_to(directory).as_posix()
        if name not in expected:
            raise ValueError('Unlisted staging file')
        before = path.stat()
        row = expected[name]
        if before.st_size != row['size'] or digest(path) != row['sha256']:
            raise ValueError('Transferred file integrity mismatch')
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError('Staging tree changed during verification')
        actual.add(name)
        observed[name] = (after.st_ino, after.st_size, after.st_mtime_ns)
    if actual != set(expected) or digest(manifest_path) != expected_sha256:
        raise ValueError('Incomplete or changed staging inventory')
    # Detect replacement/changes after an early member was hashed as well.
    current = {}
    for path in directory.rglob('*'):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError('Staging link appeared during verification')
        if path.is_file() and path != manifest_path:
            current[path.relative_to(directory).as_posix()] = (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
    if current != observed:
        raise ValueError('Staging tree changed during verification')
    evidence_path = directory / 'source-evidence/source.json'
    if evidence_path.is_file() and load(evidence_path).get('sourceCommit') != expected_commit:
        raise ValueError('Source evidence commit differs from handoff')
    if require_final:
        quiescence = manifest.get('quiescence', {})
        if (manifest.get('consistency') != 'quiesced-final'
                or any(quiescence.get(k) is not True for k in FINAL_CHECKS)
                or receipt.get('sourceCommit') != expected_commit):
            raise ValueError('Final source quiescence evidence is missing')
        for field in ('stoppedAt', 'checkedBefore', 'checkedAfter'):
            value = datetime.fromisoformat(quiescence[field].replace('Z', '+00:00'))
            if value.tzinfo is None:
                raise ValueError('Quiescence times need explicit timezone')
        times = [datetime.fromisoformat(quiescence[k].replace('Z', '+00:00')) for k in ('stoppedAt','checkedBefore','checkedAfter')]
        if times != sorted(times):
            raise ValueError('Invalid quiescence chronology')
        required = {'openclaw/.env', 'openclaw/openclaw.json', 'deploy/environment.json',
                    'deploy/agentbridge.service', 'deploy/agentbridge-backup.service',
                    'deploy/agentbridge-backup.timer', 'portable-ca/root-ca.crt',
                    'portable-ca/root-ca.encrypted.pem', 'portable-ca/receipt.json',
                    'pki/root-ca.crt', 'source-evidence/source.json',
                    'source-evidence/coverage-review.json', 'source-evidence/runtime-paths.json',
                    'source-evidence/tracked-changes.patch'}
        if not required <= actual or not any(n.startswith('ssh/') for n in actual):
            raise ValueError('Required final materials missing')
        plan_path = directory / 'source-evidence/plan.json'
        if (not plan_path.is_file() or digest(plan_path) != manifest.get('planSha256')
                or receipt.get('planSha256') != manifest.get('planSha256')):
            raise ValueError('Reviewed final plan digest missing or mismatched')
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        portable = directory / 'portable-ca'
        cert = x509.load_pem_x509_certificate((portable / 'root-ca.crt').read_bytes())
        original = x509.load_pem_x509_certificate((directory / 'pki/root-ca.crt').read_bytes())
        ca_receipt = load(portable / 'receipt.json')
        if (cert.fingerprint(hashes.SHA256()) != original.fingerprint(hashes.SHA256())
                or cert.fingerprint(hashes.SHA256()).hex() != ca_receipt.get('rootSha256')
                or digest(portable / 'root-ca.encrypted.pem') != ca_receipt.get('encryptedKeySha256')
                or b'BEGIN ENCRYPTED PRIVATE KEY' not in (portable / 'root-ca.encrypted.pem').read_bytes()):
            raise ValueError('Original encrypted CA evidence mismatch')
    return {'status': 'verified-staged-only', 'transport': 'ssh', 'kind': manifest['kind'],
            'files': len(actual), 'manifestSha256': expected_sha256, 'sourceCommit': expected_commit,
            'activated': False, 'finalEligible': require_final}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--expected-commit', required=True)
    parser.add_argument('--require-final', action='store_true')
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, args.receipt, args.expected_sha256,
                            args.expected_commit, require_final=args.require_final)))


if __name__ == '__main__':
    main()
