"""Offline Windows -> Mac handoff. Never activates a host or writes production data.

Inventory is metadata-only. Export requires an interactive passphrase and a reviewed
plan; all archive plaintext stays in memory. Stage only into a NEW private directory.
Requires Python 3.12 and the project's cryptography dependency.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
MAGIC = b"ABMAC1\x00"
LIMIT = 1024 * 1024 * 1024  # fail closed above 1 GiB; no plaintext temp archives
EXCLUDE = {"node_modules", "__pycache__", ".git", ".gitrepo", ".venv"}


def private_output(path: Path) -> None:
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Private migration output must stay outside the repository")


def new_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def git(*args: str) -> str:
    metadata = ROOT / (".gitrepo" if (ROOT / ".gitrepo").exists() else ".git")
    return subprocess.check_output(
        ["git", f"--git-dir={metadata}", f"--work-tree={ROOT}", *args],
        cwd=ROOT, text=True, encoding="utf-8",
    ).strip()


def is_link(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def files(root: Path):
    """Never follow symlinks/junctions. Inventory records omissions explicitly."""
    if is_link(root):
        yield root, "link"
    elif root.is_file():
        yield root, None
    elif root.is_dir():
        for child in sorted(root.iterdir()):
            if child.name in EXCLUDE:
                yield child, "reinstall"
            else:
                yield from files(child)
    else:
        raise ValueError("Unsupported source file type")


def validate_name(name: str) -> None:
    parts = PurePosixPath(name).parts
    if (not parts or name != PurePosixPath(name).as_posix()
            or name.startswith("/") or "\\" in name or ":" in name
            or any(p in {".", ".."} for p in parts)):
        raise ValueError("Unsafe archive path")


def inventory(output: Path) -> None:
    private_output(output)
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / ".local/share"))
    candidates = {
        "openclaw": home / ".openclaw",
        "ssh": home / ".ssh",
        "pki": home / ".agentbridge/pki",
        "deploy": local / "AgentBridge/deploy/production",
        "codex-config": home / ".codex/config.toml",
        "codex-instructions": home / ".codex/AGENTS.md",
        "codex-skills": home / ".codex/skills",
        "codex-memories": home / ".codex/memories",
        "codex-rules": home / ".codex/rules",
        "agent-skills": home / ".agents/skills",
        "project-codex": ROOT / ".codex",
        "project-agents": ROOT / ".agents",
    }
    sources, summary = [], []
    for label, path in candidates.items():
        row = {"label": label, "path": str(path), "exists": path.exists(),
               "files": 0, "bytes": 0, "omitted": []}
        if path.exists():
            for item, reason in files(path):
                if reason:
                    row["omitted"].append({"path": str(item), "reason": reason})
                else:
                    row["files"] += 1
                    row["bytes"] += item.stat().st_size
            sources.append({"label": label, "path": str(path)})
        summary.append(row)
    output.mkdir(parents=True, exist_ok=False)
    if os.name == "posix":
        output.chmod(0o700)
    new_file(output / "plan.json", json_bytes({
        "schema": "agentbridge.mac-migration.plan.v1", "sources": sources,
        "notes": "Review omissions and add external workspaces before exporting. No auto-restore.",
    }))
    new_file(output / "inventory.json", json_bytes({
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "platform": platform.system(), "python": platform.python_version(),
        "sourceCommit": git("rev-parse", "HEAD"),
        "gitStatus": git("-c", "core.quotePath=false", "status", "--short"),
        "targetOpenClaw": "2026.7.1", "sources": summary,
        "manualCoverage": [
            "Uncommitted/untracked/ignored repository files; .bscli may contain local secrets",
            "OpenClaw external workspaces/plugin links, OS environment and proxy settings",
            "Codex chats, databases, plugins, automations: separate offline backup; do not overlay live DBs",
            "Browser logins, native keychain/DPAPI records: reauthenticate or convert explicitly",
            "Other projects and personal files are not covered by this CLIExp plan",
        ],
    }))
    print(json.dumps({"status": "inventoried", "directory": str(output),
                      "bytes": sum(row["bytes"] for row in summary)}))


def password(confirm: bool = False) -> bytes:
    if not sys.stdin.isatty():
        raise ValueError("Use a local interactive terminal for the passphrase; never put it in arguments")
    value = getpass.getpass("Migration passphrase (at least 16 characters): ")
    if len(value) < 16:
        raise ValueError("Passphrase too short")
    if confirm and value != getpass.getpass("Repeat passphrase: "):
        raise ValueError("Passphrases differ")
    return value.encode("utf-8")


def seal(plain: bytes, secret: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(secret)
    return header + AESGCM(key).encrypt(nonce, plain, header)


def unseal(data: bytes, secret: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    size = len(MAGIC)
    if not data.startswith(MAGIC) or len(data) < size + 44 or len(data) > LIMIT + 1024 * 1024:
        raise ValueError("Invalid or oversized migration envelope")
    salt, nonce = data[size:size + 16], data[size + 16:size + 28]
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(secret)
    return AESGCM(key).decrypt(nonce, data[size + 28:], data[:size + 28])


def export_bundle(plan_path: Path, output: Path, secret: bytes) -> None:
    private_output(output)
    if output.exists():
        raise FileExistsError("Export already exists")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "agentbridge.mac-migration.plan.v1":
        raise ValueError("Unknown plan schema")
    entries, labels, omissions, total = [], set(), [], 0
    for source in plan["sources"]:
        label, root = source["label"], Path(source["path"])
        if not re.fullmatch(r"[a-z0-9-]+", label) or label in labels:
            raise ValueError("Invalid or duplicate source label")
        labels.add(label)
        if not root.is_absolute() or not root.exists():
            raise ValueError("Source must be an existing absolute path")
        if root.is_dir() and output.resolve().is_relative_to(root.resolve()):
            raise ValueError("Output must be outside source directories")
        for item, reason in files(root):
            if reason:
                omissions.append({"label": label, "path": str(item), "reason": reason})
                continue
            name = label + "/" + (item.relative_to(root).as_posix() if root.is_dir() else item.name)
            validate_name(name)
            total += item.stat().st_size
            if total > LIMIT:
                raise ValueError("Plan exceeds 1 GiB; split into reviewed plans, never drop files silently")
            entries.append((name, item))
    manifest = {"schema": "agentbridge.mac-migration.bundle.v1", "files": [],
                "omitted": omissions, "consistency": "operator-must-quiesce-writers-for-final-export",
                "createdAt": datetime.now(timezone.utc).isoformat()}
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in entries:
            before = path.stat()
            if is_link(path):
                raise ValueError("Source became a link")
            with path.open("rb") as stream:
                data = stream.read(LIMIT - total + 1)
            after = path.stat()
            total += len(data)
            if total > LIMIT or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Source changed during export or size limit exceeded; quiesce and retry")
            archive.writestr(name, data)
            manifest["files"].append({"name": name, "size": len(data),
                                      "sha256": hashlib.sha256(data).hexdigest()})
        archive.writestr("manifest.json", json_bytes(manifest))
    encrypted = seal(buffer.getvalue(), secret)
    # Verify authentication before publishing; caller separately stages and checks hashes.
    if unseal(encrypted, secret) != buffer.getvalue():
        raise ValueError("Encryption round trip failed")
    new_file(output, encrypted)
    print(json.dumps({"status": "exported", "files": len(entries), "omissions": len(omissions),
                      "sha256": hashlib.sha256(encrypted).hexdigest()}))


def stage_bundle(bundle: Path, destination: Path, secret: bytes) -> None:
    private_output(destination)
    if destination.exists():
        raise ValueError("Staging destination must not exist; live directories are never overwritten")
    if bundle.stat().st_size > LIMIT + 1024 * 1024:
        raise ValueError("Oversized bundle")
    plain = unseal(bundle.read_bytes(), secret)  # authenticate BEFORE any disk writes
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError("Duplicate/case-colliding archive paths")
        if sum(item.file_size for item in infos) > LIMIT + 1024 * 1024:
            raise ValueError("Oversized archive contents")
        for item in infos:
            validate_name(item.filename)
            mode = item.external_attr >> 16
            if item.is_dir() or stat.S_ISLNK(mode):
                raise ValueError("Only regular files are accepted")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema") != "agentbridge.mac-migration.bundle.v1":
            raise ValueError("Unknown manifest schema")
        expected = {row["name"]: row for row in manifest["files"]}
        if len(expected) != len(manifest["files"]) or set(expected) != set(names) - {"manifest.json"}:
            raise ValueError("Manifest membership mismatch")
        for name, row in expected.items():
            data = archive.read(name)
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Archive hash mismatch")
        # A new staging root prevents overwriting an existing installation.
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name in names:
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            new_file(target, archive.read(name))
    print(json.dumps({"status": "staged-only", "files": len(expected), "activated": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("inventory")
    scan.add_argument("--output", type=Path, required=True)
    export = sub.add_parser("export")
    export.add_argument("--plan", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inventory":
        inventory(args.output)
    elif args.command == "export":
        export_bundle(args.plan, args.output, password(confirm=True))
    else:
        stage_bundle(args.bundle, args.destination, password())


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not echo exception values that could contain secrets/config contents.
        print(f"Migration stopped ({type(error).__name__}); no host activation was performed.", file=sys.stderr)
        sys.exit(1)
