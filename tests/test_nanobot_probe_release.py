"""Qualification launches must keep the same archived adapter and library together."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.support import nanobot_probe_release as release


def fixture(root):
    root.mkdir()
    adapter = root / "nanobot_agentbridge"
    adapter.mkdir()
    (adapter / "__init__.py").write_text('SOURCE="archive"\n')
    (adapter / "server.py").write_text('')
    (root / "agentbridge.whl").write_bytes(b"fixed wheel")
    (root / "probe-release.json").write_text(json.dumps({
        "schema": "agentbridge.nanobot-probe-release.v1",
        "commit": release.candidate(), "files": release.files(root)}))
    return root


def test_staged_release_checks_wheel_adapter_and_candidate(tmp_path, monkeypatch):
    original = fixture(tmp_path / "original")
    monkeypatch.setenv(release.ENV, str(original))
    copied = release.copy_release(tmp_path / "copy")
    assert copied != original and release.files(copied) == release.files(original)
    (copied / "agentbridge.whl").write_bytes(b"changed")
    with pytest.raises(ValueError): release.checked_release(copied)
    (original / "nanobot_agentbridge/server.py").write_text("changed")
    with pytest.raises(ValueError): release.copy_release(tmp_path / "bad")


def test_staged_release_rejects_missing_library_wrong_commit_and_symlink(tmp_path):
    original = fixture(tmp_path / "original")
    proof = json.loads((original / "probe-release.json").read_text())
    (original / "agentbridge.whl").unlink()
    with pytest.raises(ValueError): release.checked_release(original)
    outside = tmp_path / "elsewhere.whl"
    outside.write_bytes(b"fixed wheel")
    (original / "agentbridge.whl").symlink_to(outside)
    with pytest.raises(ValueError): release.checked_release(original)
    (original / "agentbridge.whl").unlink()
    (original / "agentbridge.whl").write_bytes(outside.read_bytes())
    proof["commit"] = "0" * 40
    (original / "probe-release.json").write_text(json.dumps(proof))
    with pytest.raises(ValueError): release.checked_release(original)


def test_activation_pins_package_before_fixture_modifies_import_path(tmp_path):
    original = fixture(tmp_path / "original")
    other = tmp_path / "other" / "nanobot_agentbridge"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text('raise AssertionError("source adapter loaded")')
    script = """import sys
sys.path.insert(0,sys.argv[1])
from tests.support.nanobot_probe_release import activate
activate(sys.argv[2])
sys.path.insert(0,sys.argv[3])
import nanobot_agentbridge.server
print(nanobot_agentbridge.SOURCE)
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(release.ROOT),
                             str(original), str(other.parent)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "archive"
