"""Preserve the existing CA across Windows DPAPI -> encrypted PKCS#8 migration.

No new root is ever created. Passwords are prompted in a local terminal. The
encrypted portable root is for offline issuance, never for the Gateway process.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bscli.core.internal_pki import InternalCertificateAuthorityStore
from bscli.core.session_secrets import WindowsDpapiProtector
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

try:
    from .prepare import MigrationError, json_bytes, new_file, password, private_output
except ImportError:
    from prepare import MigrationError, json_bytes, new_file, password, private_output


def match(certificate, key):
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("Unsupported CA key")
    if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Certificate is not a CA")
    def public(value):
        return value.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(certificate.public_key()) != public(key.public_key()):
        raise ValueError("CA key and certificate do not match")
    certificate.verify_directly_issued_by(certificate)
    if not certificate.not_valid_before_utc <= datetime.now(timezone.utc) < certificate.not_valid_after_utc:
        raise ValueError("CA certificate is outside its validity window")


def export_ca(source: Path, destination: Path, secret: bytes, protector=None):
    private_output(destination)
    if destination.exists():
        raise FileExistsError("CA export already exists")
    store = InternalCertificateAuthorityStore(source, protector or WindowsDpapiProtector())
    # Read only: never use issue_server_certificate here (it can create a new root).
    certificate, key = store._load_root()
    match(certificate, key)
    encrypted = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(secret))
    match(certificate, serialization.load_pem_private_key(encrypted, secret))
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    new_file(destination / "root-ca.crt", certificate.public_bytes(serialization.Encoding.PEM))
    new_file(destination / "root-ca.encrypted.pem", encrypted)
    receipt = {"status": "exported-encrypted", "rootSha256": certificate.fingerprint(hashes.SHA256()).hex(),
               "encryptedKeySha256": hashlib.sha256(encrypted).hexdigest(),
               "rootNotAfter": certificate.not_valid_after_utc.isoformat(), "newRootCreated": False}
    new_file(destination / "receipt.json", json_bytes(receipt))
    return receipt


class PortableCA(InternalCertificateAuthorityStore):
    def __init__(self, root: Path, secret: bytes):
        super().__init__(root, protector=None)
        self.secret = secret
        if not self.certificate_path.is_file() or not self.protected_private_key_path.is_file():
            raise ValueError("Existing portable CA required; refusing to create a root")

    @property
    def protected_private_key_path(self):
        return self.root / "root-ca.encrypted.pem"

    def _load_root(self):
        certificate = x509.load_pem_x509_certificate(self.certificate_path.read_bytes())
        key = serialization.load_pem_private_key(self.protected_private_key_path.read_bytes(), self.secret)
        match(certificate, key)
        return certificate, key

    def _load_or_create_root(self, **kwargs):
        # Override the creation path as well: missing state must always fail closed.
        certificate, key = self._load_root()
        return certificate, key, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-windows")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--destination", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--expected-sha256", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--root", type=Path, required=True)
    issue.add_argument("--server-ip", required=True)
    issue.add_argument("--output", type=Path, required=True)
    issue.add_argument("--days", type=int, default=397)
    args = parser.parse_args()
    if args.command == "export-windows":
        result = export_ca(args.source, args.destination, password(confirm=True))
    else:
        ca = PortableCA(args.root, password())
        certificate, _ = ca._load_root()
        if args.command == "verify":
            if certificate.fingerprint(hashes.SHA256()).hex() != args.expected_sha256.lower():
                raise ValueError("Root fingerprint differs from the source receipt")
            result = {"status": "verified", "rootSha256": certificate.fingerprint(hashes.SHA256()).hex()}
        else:
            private_output(args.output)
            result = ca.issue_server_certificate(server_ip=args.server_ip, output_dir=args.output,
                                                 server_valid_days=args.days).as_dict()
    print(json_bytes(result).decode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        if isinstance(error, MigrationError):
            print(str(error), file=sys.stderr)
        print(f"CA operation stopped ({type(error).__name__}); inspect local inputs. No server deployment performed.",
              file=sys.stderr)
        sys.exit(1)
