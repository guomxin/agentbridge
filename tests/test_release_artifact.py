"""Real Git archive, wheel build and isolated install regression tests."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("artifact", ROOT / "scripts/agentbridge_artifact.py")
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


class ReleaseArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="artifact-regression-")
        cls.root = Path(cls.temp.name) / "repo"
        cls.root.mkdir()
        (cls.root / "pyproject.toml").write_text('''[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
[project]
name = "agentbridge"
version = "0.1.0"
[tool.setuptools.packages.find]
include = ["agentbridge*"]
[tool.setuptools.package-data]
"agentbridge.adapters" = ["seeyon_page_scripts/*.js"]
"agentbridge.auth" = ["static/vendor/pdfjs/*/*", "static/vendor/pdfjs/*/build/*", "static/vendor/pdfjs/*/cmaps/*", "static/vendor/pdfjs/*/standard_fonts/*", "static/vendor/pdfjs/*/wasm/*"]
''', encoding="utf-8")
        for name in ("agentbridge/__init__.py", "agentbridge/adapters/__init__.py", "agentbridge/adapters/page_scripts.py", *artifact.REQUIRED):
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        auth = cls.root / "agentbridge/auth"
        auth.mkdir()
        (auth / "__init__.py").write_text("")
        shutil.copytree(ROOT / "agentbridge/auth/static/vendor/pdfjs", auth / "static/vendor/pdfjs")
        subprocess.run(["git", "init", str(cls.root)], check=True, capture_output=True)
        artifact.git(cls.root, "add", ".")
        artifact.git(cls.root, "-c", "user.name=Artifact Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
        (cls.root / "agentbridge/untracked_sentinel.py").write_text("raise RuntimeError('must not ship')\n", encoding="utf-8")
        # Also simulate old build products: neither may enter the new archive build.
        stale = cls.root / "build/lib/agentbridge/stale.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale = True\n", encoding="utf-8")
        cls.manifest_path = artifact.build(cls.root, Path(cls.temp.name) / "result")
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_fixed_archive_excludes_untracked_and_stale_modules_and_loads_installed_resources(self):
        names = {x["path"] for x in self.manifest["files"]}
        self.assertNotIn("agentbridge/untracked_sentinel.py", names)
        self.assertNotIn("agentbridge/stale.py", names)
        self.assertTrue(set(artifact.REQUIRED).issubset(names))
        self.assertTrue(all(self.manifest["installedProbe"]["loads"].values()))
        self.assertIn("site-packages", self.manifest["installedProbe"]["module"])
        vendor = ROOT / "agentbridge/auth/static/vendor/pdfjs/6.4.299/manifest.json"
        self.assertEqual(self.manifest["installedProbe"]["pdfjs"], {
            "version": "6.4.299", "files": len(json.loads(vendor.read_text())["files"]),
            "manifestSha256": artifact.digest(vendor)})

    def test_missing_resource_rejected(self):
        bad = Path(self.temp.name) / "missing.whl"
        with zipfile.ZipFile(bad, "w") as target, zipfile.ZipFile(self.manifest["wheel"]) as source:
            for name in source.namelist():
                if name != artifact.REQUIRED[0]:
                    target.writestr(name, source.read(name))
        with self.assertRaisesRegex(ValueError, "Missing required"):
            artifact.inspect_wheel(bad, Path(self.temp.name) / "result/source")

    def test_missing_pdf_binary_cmap_fonts_license_and_manifest_are_rejected(self):
        source_root = Path(self.temp.name) / "result/source"
        with zipfile.ZipFile(self.manifest["wheel"]) as source:
            vendor_names = [name for name in source.namelist() if "/static/vendor/pdfjs/" in name]
            examples = [next(name for name in vendor_names if name.endswith(suffix))
                        for suffix in (".bcmap", ".pfb", ".ttf", ".wasm", "/LICENSE", "/manifest.json")]
            for index, missing in enumerate(examples):
                with self.subTest(missing=missing):
                    bad = Path(self.temp.name) / f"missing-pdf-{index}.whl"
                    with zipfile.ZipFile(bad, "w") as target:
                        for name in source.namelist():
                            if name != missing:
                                target.writestr(name, source.read(name))
                    with self.assertRaisesRegex(ValueError, "Wheel/source mismatch"):
                        artifact.inspect_wheel(bad, source_root)

    def test_isolated_install_rejects_pdf_resource_whose_manifest_hash_no_longer_matches(self):
        directory = Path(self.temp.name) / "tampered-probe"
        directory.mkdir()
        bad = directory / Path(self.manifest["wheel"]).name
        with zipfile.ZipFile(self.manifest["wheel"]) as source, zipfile.ZipFile(bad, "w") as target:
            changed = next(name for name in source.namelist() if name.endswith(".bcmap"))
            for name in source.namelist():
                target.writestr(name, b"modified CMap" if name == changed else source.read(name))
        with self.assertRaises(subprocess.CalledProcessError):
            artifact.installed_probe(bad, directory)

    def test_receipt_rejects_changed_commit_environment_skips_and_artifact(self):
        receipt = Path(self.temp.name) / "receipt.json"
        current = {"commit": self.manifest["commit"], "environment": "test"}
        data = {"schema": "agentbridge.validation.v1", "status": "succeeded", "inputs": current,
                "plan": {"stages": [{"id": name} for name in artifact.CHECKS]},
                "checks": artifact.CHECKS, "skipped": [], "manifest": str(self.manifest_path),
                "manifestSha256": artifact.digest(self.manifest_path)}
        # Stage execution/receipt integrity is tested with real subprocesses in
        # test_validation_plan; this test isolates commit/environment/wheel gates.
        with patch.object(artifact, "inputs", return_value=current), \
             patch.object(artifact.validation_plan, "verify_stages", return_value=[]):
            artifact.save(receipt, data)
            self.assertEqual(artifact.verify(self.root, receipt)["sha256"], self.manifest["sha256"])
            for override in ({"status": "running"}, {"skipped": ["openclaw"]},
                             {"checks": ["python-full"]}, {"inputs": {"commit": "other"}},
                             {"inputs": {**current, "environment": "changed"}},
                             {"manifestSha256": "0" * 64}):
                with self.subTest(override=override):
                    artifact.save(receipt, {**data, **override})
                    with self.assertRaises(ValueError):
                        artifact.verify(self.root, receipt)
            artifact.save(receipt, data)
            wheel = Path(self.manifest["wheel"])
            original = wheel.read_bytes()
            try:
                wheel.write_bytes(original + b"tampered")
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    artifact.verify(self.root, receipt)
            finally:
                wheel.write_bytes(original)

    def test_dirty_tracked_input_rejected_without_touching_worktree(self):
        target = self.root / "agentbridge/__init__.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# local change\n")
            with self.assertRaisesRegex(ValueError, "clean tracked"):
                artifact.build(self.root, Path(self.temp.name) / "dirty-result")
            self.assertTrue(target.read_bytes().endswith(b"# local change\n"))
        finally:
            target.write_bytes(original)
