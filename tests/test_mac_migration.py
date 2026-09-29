from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from bscli.core.internal_pki import InternalCertificateAuthorityStore
from scripts.migration.prepare import export_bundle, seal, stage_bundle, unseal, validate_name
from scripts.migration.portable_ca import export_ca, PortableCA
from scripts.migration.mac_preflight import node_supported, version


class TestProtector:
    def protect(self, plaintext, *, context):
        return context + b"|" + plaintext

    def unprotect(self, ciphertext, *, context):
        prefix = context + b"|"
        if not ciphertext.startswith(prefix):
            raise ValueError("Wrong context")
        return ciphertext[len(prefix):]


class MacMigrationTests(unittest.TestCase):
    def test_bundle_roundtrip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "config.json").write_text('{"token":"fixture-only"}', encoding="utf-8")
            (source / "node_modules").mkdir()
            (source / "node_modules/native.bin").write_bytes(b"windows-native")
            plan = root / "plan.json"
            plan.write_text(json.dumps({"schema": "agentbridge.mac-migration.plan.v1", "sources": [
                {"label": "openclaw", "path": str(source)}]}), encoding="utf-8")
            bundle, target = root / "bundle.abmac", root / "stage"
            secret = b"fixture passphrase only"
            export_bundle(plan, bundle, secret)
            self.assertNotIn(b"fixture-only", bundle.read_bytes())
            stage_bundle(bundle, target, secret)
            self.assertEqual((target / "openclaw/config.json").read_bytes(), (source / "config.json").read_bytes())
            self.assertFalse((target / "openclaw/node_modules").exists())
            self.assertEqual(len(json.loads((target / "manifest.json").read_text())["omitted"]), 1)
            with self.assertRaises(ValueError):
                stage_bundle(bundle, target, secret)

    def test_wrong_password_and_tamper_do_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            encrypted = seal(b"fixture archive", b"correct password")
            with self.assertRaises(InvalidTag):
                unseal(encrypted, b"wrong password")
            bundle = root / "tampered.abmac"
            bundle.write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
            with self.assertRaises(InvalidTag):
                stage_bundle(bundle, root / "stage", b"correct password")
            self.assertFalse((root / "stage").exists())

    def test_reject_paths(self):
        for name in ("../secret", "/etc/file", "a/../../b", "C:/file", "a\\b", "a//b", "./a"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_name(name)

    def test_authenticated_archive_still_requires_safe_paths_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, name in enumerate(("../escape", "safe/file")):
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as archive:
                    archive.writestr(name, b"contents")
                    archive.writestr("manifest.json", json.dumps({
                        "schema": "agentbridge.mac-migration.bundle.v1",
                        "files": [{"name": name, "size": 8, "sha256": hashlib.sha256(b"wrong").hexdigest()}],
                    }))
                bundle = root / f"{index}.abmac"
                bundle.write_bytes(seal(data.getvalue(), b"password"))
                target = root / f"stage{index}"
                with self.assertRaises(ValueError):
                    stage_bundle(bundle, target, b"password")
                self.assertFalse(target.exists())

    def test_ca_export_preserves_root_and_issues_verifiable_leaf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "windows"
            store = InternalCertificateAuthorityStore(source, TestProtector())
            store.issue_server_certificate(server_ip="192.0.2.1", output_dir=root / "old-leaf")
            before = (source / "root-ca.key.dpapi").read_bytes()
            receipt = export_ca(source, root / "portable", b"test passphrase only", TestProtector())
            ca = PortableCA(root / "portable", b"test passphrase only")
            result = ca.issue_server_certificate(server_ip="192.0.2.2", output_dir=root / "new-leaf")
            self.assertFalse(result.created_root_ca)
            cert = x509.load_pem_x509_certificate((source / "root-ca.crt").read_bytes())
            leaf = x509.load_pem_x509_certificate((root / "new-leaf/server.crt").read_bytes())
            leaf.verify_directly_issued_by(cert)
            self.assertEqual(receipt["rootSha256"], cert.fingerprint(hashes.SHA256()).hex())
            self.assertEqual(before, (source / "root-ca.key.dpapi").read_bytes())
            self.assertIn(b"ENCRYPTED PRIVATE KEY", (root / "portable/root-ca.encrypted.pem").read_bytes())
            with self.assertRaises(ValueError):
                PortableCA(root / "portable", b"wrong")._load_root()
            with self.assertRaises(ValueError):
                PortableCA(root / "missing", b"password")
            (root / "portable/root-ca.encrypted.pem").unlink()
            with self.assertRaises(FileNotFoundError):
                ca.issue_server_certificate(server_ip="192.0.2.2", output_dir=root / "never")
            self.assertFalse((root / "portable/root-ca.key.dpapi").exists())

    def test_node_engine_matches_pinned_openclaw(self):
        for value in ("22.22.3", "24.15.0", "25.9.0", "26.5.0"):
            self.assertTrue(node_supported(value))
        for value in (None, "22.22.2", "23.9.0", "24.14.0", "25.8.0"):
            self.assertFalse(node_supported(value))

    def test_node_v_prefix_is_detected(self):
        import subprocess
        with patch("scripts.migration.mac_preflight.shutil.which", return_value="node"), patch(
            "scripts.migration.mac_preflight.subprocess.run",
            return_value=subprocess.CompletedProcess(["node", "--version"], 0, "v26.5.0\n", ""),
        ):
            self.assertEqual(version("node"), "26.5.0")
