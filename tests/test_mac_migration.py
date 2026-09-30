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
from scripts.migration.prepare import (export_bundle, seal, stage_bundle, unseal, validate_name,
                                       validate_tree, add_source, collect_plan, final_plan_checks, MigrationError)
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
        for name in ("../secret", "/etc/file", "a/../../b", "C:/file", "a\\b", "a//b", "./a",
                     "a\x00b", "a./file", "a /file"):
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

    def test_mac_case_unicode_and_directory_collisions_fail_before_writes(self):
        for names in (("a", "a/b"), ("a/b", "a"), ("A/a", "a/b"),
                      ("é/a", "e\u0301/b"), ("a", "a")):
            with self.subTest(names=names), self.assertRaises(MigrationError):
                validate_tree(names)
        validate_tree(["a/b", "a/c", "d/e"])

    def test_bom_plan_and_idempotent_ca_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "ca"
            source.mkdir()
            (source / "root-ca.crt").write_text("fixture", encoding="utf-8")
            plan = root / "plan.json"
            plan.write_text(json.dumps({"schema": "agentbridge.mac-migration.plan.v1", "sources": []}),
                            encoding="utf-8-sig")
            add_source(plan, "portable-ca", source)
            add_source(plan, "portable-ca", source)
            data = json.loads(plan.read_text(encoding="utf-8"))
            self.assertEqual(len(data["sources"]), 1)
            self.assertEqual(len(collect_plan(data)[0]), 1)
            with self.assertRaises(MigrationError):
                final_plan_checks(data, False)
            with self.assertRaises(MigrationError):
                final_plan_checks(data, True)

    def test_receipt_hash_verification_and_snapshot_cannot_be_final(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "config").write_bytes(b"config")
            plan = root / "plan.json"
            plan.write_text(json.dumps({"schema": "agentbridge.mac-migration.plan.v1", "sources": [
                {"label": "openclaw", "path": str(source)}]}), encoding="utf-8-sig")
            bundle = root / "test.abmac"
            export_bundle(plan, bundle, b"fixture password")
            receipt = json.loads((root / "test.abmac.receipt.json").read_text())
            stage_bundle(bundle, None, b"fixture password", expected_sha256=receipt["sha256"])
            with self.assertRaises(MigrationError):
                stage_bundle(bundle, root / "wrong", b"fixture password", expected_sha256="0" * 64)
            with self.assertRaises(MigrationError):
                stage_bundle(bundle, root / "wrong", b"fixture password", require_final=True)
            self.assertFalse((root / "wrong").exists())

    def test_openclaw_prerelease_and_timeout_are_not_pinned_success(self):
        import subprocess
        with patch("scripts.migration.mac_preflight.shutil.which", return_value="openclaw"):
            for output, expected in (("OpenClaw 2026.7.1 (2d2ddc4)", "2026.7.1"),
                                     ("OpenClaw 2026.7.1-beta.1", "2026.7.1-beta.1"),
                                     ("plugin 2026.7.1 loaded\nOpenClaw 2026.8.1", "2026.8.1")):
                with patch("scripts.migration.mac_preflight.subprocess.run",
                           return_value=subprocess.CompletedProcess([], 0, output, "")):
                    self.assertEqual(version("openclaw"), expected)
            with patch("scripts.migration.mac_preflight.subprocess.run",
                       side_effect=subprocess.TimeoutExpired("openclaw", 30)):
                self.assertIsNone(version("openclaw"))

    def test_final_export_requires_matching_ca_and_quiescence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sources = {}
            for label in ("openclaw", "ssh", "pki", "deploy", "source-evidence"):
                sources[label] = root / label
                sources[label].mkdir()
            (sources["openclaw"] / ".env").write_text("fixture", encoding="utf-8")
            (sources["deploy"] / "environment.json").write_text("{}", encoding="utf-8")
            (sources["source-evidence"] / "source.json").write_text('{"sourceCommit":"abc"}', encoding="utf-8")
            (sources["source-evidence"] / "tracked-changes.patch").write_bytes(b"")
            InternalCertificateAuthorityStore(sources["pki"], TestProtector()).issue_server_certificate(
                server_ip="192.0.2.1", output_dir=root / "leaf")
            sources["portable-ca"] = root / "portable-ca"
            export_ca(sources["pki"], sources["portable-ca"], b"fixture password", TestProtector())
            plan = {"schema": "agentbridge.mac-migration.plan.v1", "sources": [
                {"label": label, "path": str(path)} for label, path in sources.items()]}
            with patch("scripts.migration.prepare.git", return_value="abc"), patch(
                "scripts.migration.prepare.git_bytes", return_value=b""), patch(
                "scripts.migration.prepare.assert_windows_quiesced") as stopped:
                final_plan_checks(plan, True)
                stopped.assert_called_once()
                plan_path = root / "plan.json"
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                bundle = root / "final.abmac"
                export_bundle(plan_path, bundle, b"fixture password", kind="final", writers_stopped=True)
                stage_bundle(bundle, root / "stage-final", b"fixture password", require_final=True)
                manifest = json.loads((root / "stage-final/manifest.json").read_text())
                self.assertEqual(manifest["kind"], "final")
                self.assertEqual(manifest["sourceCommit"], "abc")
                stopped.side_effect = MigrationError("still running")
                with self.assertRaises(MigrationError):
                    final_plan_checks(plan, True)
                stopped.reset_mock(side_effect=True)
                (sources["portable-ca"] / "root-ca.encrypted.pem").write_bytes(b"ENCRYPTED PRIVATE KEY changed")
                with self.assertRaises(MigrationError):
                    final_plan_checks(plan, True)
                stopped.assert_not_called()

    def test_fetch_fixed_commit_from_git_and_preserve_local_work(self):
        import subprocess
        from scripts.migration.fetch_code import fetch_code, run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin, seed, target = root / "origin.git", root / "seed", root / "target"
            for path, args in ((origin, ["--bare"]), (seed, [])):
                subprocess.run(["git", "init", *args, str(path)], check=True, capture_output=True)
            run(seed, "config", "user.name", "Migration Test")
            run(seed, "config", "user.email", "migration@example.test")
            (seed / "file.txt").write_text("baseline", encoding="utf-8")
            run(seed, "add", "file.txt")
            run(seed, "commit", "-m", "fixture")
            commit = run(seed, "rev-parse", "HEAD")
            run(seed, "remote", "add", "origin", str(origin))
            run(seed, "push", "origin", "HEAD:refs/heads/main")
            run(origin, "symbolic-ref", "HEAD", "refs/heads/main")
            with patch("scripts.migration.fetch_code.REMOTE", str(origin)), patch(
                "scripts.migration.fetch_code.ALLOWED_REMOTES", {str(origin)}):
                fetch_code(target, commit)
                self.assertEqual(run(target, "rev-parse", "HEAD"), commit)
                fetch_code(target, commit)  # repeat without losing work
                (target / "file.txt").write_text("local work", encoding="utf-8")
                with self.assertRaises(ValueError):
                    fetch_code(target, commit)
                self.assertEqual((target / "file.txt").read_text(), "local work")
                run(target, "config", "user.name", "Migration Test")
                run(target, "config", "user.email", "migration@example.test")
                run(target, "add", "file.txt")
                run(target, "commit", "-m", "local-only")
                local_commit = run(target, "rev-parse", "HEAD")
                with self.assertRaises(ValueError):
                    fetch_code(target, commit)
                self.assertEqual(run(target, "rev-parse", "HEAD"), local_commit)
            with self.assertRaises(ValueError):
                fetch_code(root / "never", "main")
            self.assertFalse((root / "never").exists())
