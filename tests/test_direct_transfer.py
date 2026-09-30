import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/migration'))
from direct_transfer import verify, FINAL_CHECKS

COMMIT = 'a' * 40


def handoff(tmp_path, files=None, kind='snapshot', extra=None):
    root = tmp_path / 'stage'
    root.mkdir()
    files = files or {'source-evidence/source.json': json.dumps({'sourceCommit': COMMIT}).encode(), 'openclaw/.env': b'test-only'}
    rows = []
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        rows.append({'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    manifest = {'schema': 'agentbridge.mac-migration.direct.v1', 'transport': 'ssh', 'kind': kind,
                'sourceCommit': COMMIT, 'files': rows, **(extra or {})}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    digest = hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'status': 'verified-staged-only', 'kind': kind,
        'manifestSha256': digest, 'sourceCommit': COMMIT, 'planSha256': manifest.get('planSha256'),
        'files': len(rows), 'activated': False}))
    return root, receipt, digest


def test_snapshot_verifies_but_cannot_activate(tmp_path):
    root, receipt, digest = handoff(tmp_path)
    assert verify(root, receipt, digest, COMMIT)['activated'] is False
    with pytest.raises(ValueError, match='snapshot'):
        verify(root, receipt, digest, COMMIT, require_final=True)


@pytest.mark.parametrize('mutation', ['file', 'manifest', 'extra', 'missing', 'symlink', 'receipt'])
def test_tamper_rejected(tmp_path, mutation):
    root, receipt, digest = handoff(tmp_path)
    if mutation == 'file':
        (root / 'openclaw/.env').write_bytes(b'changed!!')
    elif mutation == 'manifest':
        with (root / 'manifest.json').open('a') as f: f.write(' ')
    elif mutation == 'extra':
        (root / 'unlisted').write_bytes(b'x')
    elif mutation == 'missing':
        (root / 'openclaw/.env').unlink()
    elif mutation == 'symlink':
        (root / 'openclaw/.env').unlink()
        (root / 'openclaw/.env').symlink_to(receipt)
    else:
        d=json.loads(receipt.read_text());d['files']=0;receipt.write_text(json.dumps(d))
    with pytest.raises(ValueError):
        verify(root, receipt, digest, COMMIT)


def test_untrusted_digest_or_commit_rejected(tmp_path):
    root, receipt, digest = handoff(tmp_path)
    with pytest.raises(ValueError): verify(root, receipt, '0' * 64, COMMIT)
    with pytest.raises(ValueError): verify(root, receipt, digest, 'b' * 40)


def test_final_without_quiescence_rejected(tmp_path):
    root, receipt, digest = handoff(tmp_path, kind='final')
    with pytest.raises(ValueError, match='quiescence'):
        verify(root, receipt, digest, COMMIT, require_final=True)


def final_files():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from datetime import datetime, timezone, timedelta
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'Synthetic migration test')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(b'synthetic-test-only'))
    files = {n: b'{}' for n in ['openclaw/.env', 'openclaw/openclaw.json', 'deploy/environment.json',
        'deploy/agentbridge.service','deploy/agentbridge-backup.service','deploy/agentbridge-backup.timer',
        'source-evidence/coverage-review.json','source-evidence/runtime-paths.json',
        'source-evidence/tracked-changes.patch','source-evidence/plan.json','ssh/test-key']}
    files.update({'source-evidence/source.json': json.dumps({'sourceCommit':COMMIT}).encode(),
        'pki/root-ca.crt':pem, 'portable-ca/root-ca.crt':pem, 'portable-ca/root-ca.encrypted.pem':encrypted,
        'portable-ca/receipt.json':json.dumps({'rootSha256':cert.fingerprint(hashes.SHA256()).hex(),
            'encryptedKeySha256':hashlib.sha256(encrypted).hexdigest()}).encode()})
    return files


def test_complete_final_and_negative_stop_evidence(tmp_path):
    files = final_files()
    extra = {'consistency':'quiesced-final', 'planSha256':hashlib.sha256(files['source-evidence/plan.json']).hexdigest(),
        'quiescence':{**{k:True for k in FINAL_CHECKS}, 'stoppedAt':'2026-09-30T00:00:00Z',
            'checkedBefore':'2026-09-30T00:01:00Z','checkedAfter':'2026-09-30T00:02:00Z'}}
    root, receipt, digest = handoff(tmp_path, files, kind='final', extra=extra)
    assert verify(root, receipt, digest, COMMIT, require_final=True)['finalEligible']
    manifest=json.loads((root/'manifest.json').read_text())
    manifest['quiescence']['guardDisabled']=False
    (root/'manifest.json').write_text(json.dumps(manifest))
    digest=hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest()
    d=json.loads(receipt.read_text());d['manifestSha256']=digest;receipt.write_text(json.dumps(d))
    with pytest.raises(ValueError, match='quiescence'):
        verify(root,receipt,digest,COMMIT,require_final=True)
