"""Existing portable roots must survive the platform retirement unchanged."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import argparse
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from agentbridge.core.internal_pki import InternalCertificateAuthorityStore
from agentbridge.cli.main import main

PASSWORD = b'fixture CA passphrase'


@pytest.fixture
def ca(tmp_path):
    store = InternalCertificateAuthorityStore(tmp_path / 'ca', PASSWORD)
    store.create_root()
    return store


def test_explicit_root_creation_encrypted_and_no_overwrite(ca):
    encrypted = ca.protected_private_key_path.read_bytes()
    assert b'BEGIN ENCRYPTED PRIVATE KEY' in encrypted
    assert ca.protected_private_key_path.stat().st_mode & 0o077 == 0
    with pytest.raises(TypeError):
        serialization.load_pem_private_key(encrypted, None)
    before = ca.certificate_path.read_bytes()
    with pytest.raises(FileExistsError):
        ca.create_root()
    assert ca.certificate_path.read_bytes() == before


def test_issuance_reuses_portable_root_and_only_exports_leaf(ca, tmp_path):
    before = ca.protected_private_key_path.read_bytes()
    first = ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'first')
    second = ca.issue_server_certificate(server_ip='192.0.2.2', output_dir=tmp_path / 'second')
    assert not first.created_root_ca and not second.created_root_ca
    assert first.root_fingerprint_sha256 == second.root_fingerprint_sha256
    assert first.server_fingerprint_sha256 != second.server_fingerprint_sha256
    assert ca.protected_private_key_path.read_bytes() == before
    root = x509.load_pem_x509_certificate(ca.certificate_path.read_bytes())
    leaf = x509.load_pem_x509_certificate(first.server_certificate_path.read_bytes())
    leaf.verify_directly_issued_by(root)
    assert ExtendedKeyUsageOID.SERVER_AUTH in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert str(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress)[0]) == '192.0.2.1'
    assert not (tmp_path / 'first/root-ca.encrypted.pem').exists()
    assert first.server_private_key_path.stat().st_mode & 0o077 == 0


def test_missing_partial_wrong_password_never_creates_new_root(tmp_path, ca):
    empty = InternalCertificateAuthorityStore(tmp_path / 'missing', PASSWORD)
    with pytest.raises(ValueError, match='Existing CA'):
        empty.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'never')
    assert not empty.root.exists()
    with pytest.raises(ValueError):
        InternalCertificateAuthorityStore(ca.root, b'wrong')._load_root()
    ca.protected_private_key_path.unlink()
    before = ca.certificate_path.read_bytes()
    with pytest.raises(ValueError, match='incomplete'):
        ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'never')
    assert ca.certificate_path.read_bytes() == before
    assert not (tmp_path / 'never').exists()


def test_mismatched_root_and_key_rejected(ca, tmp_path):
    other = InternalCertificateAuthorityStore(tmp_path / 'other', PASSWORD)
    other.create_root()
    ca.certificate_path.write_bytes(other.certificate_path.read_bytes())
    with pytest.raises(ValueError, match='do not match'):
        ca._load_root()


def test_private_key_permissions_symlinks_and_unencrypted_pem_rejected(ca, tmp_path):
    cert, key = ca._load_root()
    ca.protected_private_key_path.chmod(0o644)
    with pytest.raises(ValueError, match='private regular'):
        ca._load_root()
    ca.protected_private_key_path.chmod(0o600)
    ca.protected_private_key_path.rename(tmp_path / 'key')
    ca.protected_private_key_path.symlink_to(tmp_path / 'key')
    with pytest.raises(OSError):
        ca._load_root()
    ca.protected_private_key_path.unlink()
    ca.protected_private_key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ca.protected_private_key_path.chmod(0o600)
    with pytest.raises(TypeError):
        ca._load_root()


def test_validity_relabel_output_and_force(ca, tmp_path):
    for params, match in [({'server_valid_days':398}, '397'), ({'root_common_name':'other'}, 'common name')]:
        with pytest.raises(ValueError, match=match):
            ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'leaf', **params)
    with pytest.raises(ValueError, match='must differ'):
        ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=ca.root)
    with pytest.raises(ValueError, match='validity window'):
        ca._load_root(now=datetime.now(timezone.utc) + timedelta(days=8000))
    ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'leaf')
    with pytest.raises(FileExistsError):
        ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'leaf')
    assert not ca.issue_server_certificate(server_ip='192.0.2.1', output_dir=tmp_path / 'leaf', force=True).created_root_ca


def test_cli_pin_and_verify_do_not_disclose_password(ca, tmp_path, capsys):
    cert, _ = ca._load_root()
    pin = cert.fingerprint(hashes.SHA256()).hex()
    args = ['--home', str(tmp_path), 'pki', 'verify-root', '--state-dir', str(ca.root), '--expected-sha256', pin]
    with patch('getpass.getpass', return_value=PASSWORD.decode()):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'verified'
        assert main(args[:-1] + ['0' * 64]) == 2
        output = capsys.readouterr().out
        assert 'fingerprint' in output.lower() and PASSWORD.decode() not in output
