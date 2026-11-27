"""Stage one fixed Git artifact for real-SDK probes without installing it globally."""

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
ENV = "AGENTBRIDGE_NANOBOT_PROBE_RELEASE"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(release):
    return {str(path.relative_to(release)): digest(path)
            for path in sorted(release.rglob("*")) if path.is_file()
            and path.name != "probe-release.json"}


def candidate():
    metadata = ROOT / (".gitrepo" if (ROOT / ".gitrepo").exists() else ".git")
    return subprocess.check_output(["git", f"--git-dir={metadata}", f"--work-tree={ROOT}", "rev-parse", "HEAD"], text=True).strip()


def checked_release(path):
    release = Path(path).resolve(strict=True)
    proof = json.loads((release / "probe-release.json").read_text())
    if (proof.get("schema") != "agentbridge.nanobot-probe-release.v1"
            or proof.get("commit") != candidate()
            or proof.get("files") != files(release)
            or "agentbridge.whl" not in proof["files"]
            or "nanobot_agentbridge/server.py" not in proof["files"]
            or any(p.is_symlink() for p in release.rglob("*"))):
        raise ValueError("Fixed NanoBot probe release differs from this candidate")
    return release


def prepare(output):
    # Reuse the official Git archive, wheel inspection and isolated install probe.
    sys.path.insert(0, str(ROOT / "scripts"))
    from agentbridge_artifact import build
    output = Path(output).resolve()
    manifest = json.loads(build(ROOT, output / "artifact").read_text())
    release = output / "release"
    release.mkdir(mode=0o700)
    source = output / "artifact/source/integrations"
    for name, folder in (("nanobot_agentbridge", "nanobot-agentbridge"),
                         ("reference_host", "reference-host")):
        shutil.copytree(source / folder / name, release / name)
    shutil.copyfile(manifest["wheel"], release / "agentbridge.whl")
    (release / "agentbridge.whl").chmod(0o600)
    proof = {"schema": "agentbridge.nanobot-probe-release.v1",
             "commit": manifest["commit"], "files": files(release)}
    path = release / "probe-release.json"
    path.write_text(json.dumps(proof, sort_keys=True) + "\n")
    path.chmod(0o600)
    return checked_release(release)


def copy_release(destination):
    destination = Path(destination)
    if configured := os.environ.get(ENV):
        original = checked_release(configured)
    else:
        # Standalone probes keep their existing CLI and stage the same official artifact.
        original = prepare(destination.parent / "probe-artifact")
    shutil.copytree(original, destination)
    return checked_release(destination)


def activate(path):
    release = checked_release(path)
    sys.path[:0] = [str(release), str(release / "agentbridge.whl")]
    package = importlib.import_module("nanobot_agentbridge")
    if not Path(package.__file__).resolve().is_relative_to(release):
        raise ValueError("NanoBot probe imported an adapter outside its fixed release")
    return release
