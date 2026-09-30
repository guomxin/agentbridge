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
import unicodedata
from uuid import uuid4
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2]
MAGIC = b"ABMAC1\x00"
LIMIT = 1024 * 1024 * 1024  # fail closed above 1 GiB; no plaintext temp archives
EXCLUDE = {"node_modules", "__pycache__", ".git", ".gitrepo", ".venv"}


class MigrationError(ValueError):
    """Only messages authored by this tool, safe to show without input values."""


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_output(path: Path) -> None:
    if path.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Private migration output must stay outside the repository")


def new_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def git(*args: str) -> str:
    return git_bytes(*args).decode("utf-8").strip()


def git_bytes(*args: str) -> bytes:
    metadata = ROOT / (".gitrepo" if (ROOT / ".gitrepo").exists() else ".git")
    return subprocess.check_output(
        ["git", f"--git-dir={metadata}", f"--work-tree={ROOT}", *args],
        cwd=ROOT,
    )


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
                yield child, ("repository-history-not-included" if child.name in {".git", ".gitrepo"}
                              else "reinstall")
            else:
                yield from files(child)
    else:
        raise ValueError("Unsupported source file type")


def validate_name(name: str) -> None:
    parts = PurePosixPath(name).parts
    if (not parts or name != PurePosixPath(name).as_posix()
            or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(c) < 32 for c in name)
            or any(p in {".", ".."} or p.endswith((".", " ")) for p in parts)):
        raise ValueError("Unsafe archive path")


def validate_tree(names):
    """Account for default Mac case/Unicode folding and file/directory conflicts."""
    nodes = {}
    leaves = set()
    directories = set()
    for name in names:
        validate_name(name)
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            original = "/".join(parts[:index])
            normalized = unicodedata.normalize("NFD", original).casefold()
            if normalized in nodes and nodes[normalized] != original:
                raise MigrationError("Case or Unicode path collision; resolve before export/stage")
            nodes[normalized] = original
            if index < len(parts) and normalized in leaves:
                raise MigrationError("Archive file/directory collision")
            if index < len(parts):
                directories.add(normalized)
        normalized = unicodedata.normalize("NFD", name).casefold()
        if normalized in leaves or normalized in directories:
            raise MigrationError("Duplicate path or archive file/directory collision")
        leaves.add(normalized)


def source_evidence(home: Path, state: Path):
    config_path = Path(os.environ.get("OPENCLAW_CONFIG_PATH", state / "openclaw.json"))
    config = read_json(config_path) if config_path.exists() else {}
    agent = config.get("agents", {})
    plugin_projects = []
    for path in sorted((state / "npm/projects").glob("*/package.json")):
        plugin_projects.append({"path": str(path), "dependencies": read_json(path).get("dependencies", {})})
    return {
        "stateDir": str(state), "configPath": str(config_path),
        "defaultWorkspace": agent.get("defaults", {}).get("workspace"),
        "agents": [{key: row.get(key) for key in ("id", "workspace", "agentDir")}
                   for row in agent.get("list", [])],
        "pluginPaths": config.get("plugins", {}).get("load", {}).get("paths", []),
        "pluginProjects": plugin_projects,
        "environmentNamesOnly": sorted(key for key in os.environ if
            key.startswith(("OPENCLAW_", "AGENTBRIDGE_", "NODE_"))
            or key in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "CODEX_HOME"}),
    }


def inventory(output: Path) -> None:
    private_output(output)
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", home / ".local/share"))
    state = Path(os.environ.get("OPENCLAW_STATE_DIR", home / ".openclaw")).expanduser()
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
    candidates = {
        "openclaw": state,
        "openclaw-config": Path(os.environ.get("OPENCLAW_CONFIG_PATH", state / "openclaw.json")),
        "host-workspace-git": state / "workspace/.git",
        "ssh": home / ".ssh",
        "pki": home / ".agentbridge/pki",
        "deploy": local / "AgentBridge/deploy/production",
        "gateway-launcher": local / "AgentBridge/openclaw-gateway-runtime.cmd",
        "git-local-config": ROOT / (".gitrepo/config" if (ROOT / ".gitrepo").exists() else ".git/config"),
        "git-known-hosts": ROOT / ".bscli/github_known_hosts",
        "codex-config": codex / "config.toml",
        "codex-instructions": codex / "AGENTS.md",
        "codex-skills": codex / "skills",
        "codex-memories": codex / "memories",
        "codex-rules": codex / "rules",
        "codex-automations": codex / "automations",
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
                    row["omitted"].append({"path": str(item), "reason": reason,
                                           "linkTarget": str(item.resolve()) if reason == "link" else None})
                else:
                    row["files"] += 1
                    row["bytes"] += item.stat().st_size
            sources.append({"label": label, "path": str(path)})
        summary.append(row)
    output.mkdir(parents=True, exist_ok=False)
    if os.name == "posix":
        output.chmod(0o700)
    evidence = output / "source-evidence"
    evidence.mkdir(mode=0o700)
    new_file(evidence / "runtime-paths.json", json_bytes(source_evidence(home, state)))
    # A private patch preserves tracked local work; it is not applied automatically.
    new_file(evidence / "tracked-changes.patch", git_bytes("diff", "--binary", "HEAD"))
    new_file(evidence / "source.json", json_bytes({
        "sourceCommit": git("rev-parse", "HEAD"),
        "gitStatus": git("-c", "core.quotePath=false", "status", "--short"),
        "untrackedFiles": git("-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"),
        "untrackedContentsIncluded": False,
    }))
    sources.append({"label": "source-evidence", "path": str(evidence.resolve())})
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


def add_source(plan_path: Path, label: str, source: Path):
    private_output(plan_path)
    if not re.fullmatch(r"[a-z0-9-]+", label) or not source.is_absolute() or not source.exists():
        raise MigrationError("Source needs a valid label and an existing absolute path")
    plan = read_json(plan_path)
    if plan.get("schema") != "agentbridge.mac-migration.plan.v1":
        raise MigrationError("Unknown plan schema")
    for row in plan["sources"]:
        if row["label"] == label:
            if Path(row["path"]).resolve() == source.resolve():
                return  # idempotent registration
            raise MigrationError("Label already belongs to another source")
    plan["sources"].append({"label": label, "path": str(source.resolve())})
    temporary = plan_path.with_suffix(".json.pending")
    new_file(temporary, json_bytes(plan))
    os.replace(temporary, plan_path)


def assert_windows_quiesced():
    if os.name != "nt":
        raise MigrationError("Final source export requires the original Windows workstation")
    command = r'''
$ErrorActionPreference = 'Stop'
$tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName -match 'AgentBridge|OpenClaw' })
$active = @($tasks | Where-Object { $_.Settings.Enabled -or $_.State -eq 'Running' })
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq 'node.exe' -and $_.CommandLine -match 'openclaw.*gateway') -or
    ($_.Name -match 'powershell|pwsh' -and $_.CommandLine -match '(Start-AgentBridgeOpenClawGuard|Start-AgentBridgeWorkspaceTunnel|Invoke-AgentBridgeOpenClawGatewayForeground)\.ps1') -or
    ($_.Name -eq 'ssh.exe' -and $_.CommandLine -match '18789')
})
$listeners = @(Get-NetTCPConnection -ErrorAction Stop | Where-Object { $_.State -eq 'Listen' -and $_.LocalPort -eq 18789 })
@{activeTasks=$active.Count;hostProcesses=$processes.Count;listeners=$listeners.Count} | ConvertTo-Json -Compress
'''
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=30, check=True)
    counts = json.loads(result.stdout)
    if any(counts.get(key, -1) != 0 for key in ("activeTasks", "hostProcesses", "listeners")):
        raise MigrationError("Final export blocked: disable old self-heal tasks and stop Gateway/tunnel first")


def collect_plan(plan):
    if plan.get("schema") != "agentbridge.mac-migration.plan.v1" or not plan.get("sources"):
        raise MigrationError("Unknown or empty migration plan")
    entries, labels, omissions, total = [], set(), [], 0
    for source in plan["sources"]:
        label, root = source["label"], Path(source["path"])
        if not re.fullmatch(r"[a-z0-9-]+", label) or label in labels:
            raise MigrationError("Invalid or duplicate source label")
        labels.add(label)
        if not root.is_absolute() or not root.exists():
            raise MigrationError("A source is missing or is not an absolute path; inspect plan.json")
        for item, reason in files(root):
            if reason:
                omissions.append({"label": label, "path": str(item), "reason": reason,
                                  "linkTarget": str(item.resolve()) if reason == "link" else None})
                continue
            name = label + "/" + (item.relative_to(root).as_posix() if root.is_dir() else item.name)
            total += item.stat().st_size
            if total > LIMIT:
                raise MigrationError("Plan exceeds 1 GiB; split reviewed sources instead of dropping files")
            entries.append((name, item))
    validate_tree([name for name, _ in entries] + ["manifest.json"])
    return entries, omissions, total


def check_plan(plan_path, report_path):
    private_output(report_path)
    plan = read_json(plan_path)
    entries, omissions, total = collect_plan(plan)
    sources = {row["label"]: Path(row["path"]) for row in plan["sources"]}
    required = {"openclaw", "ssh", "pki", "deploy", "portable-ca", "source-evidence"}
    report = {"schema": "agentbridge.mac-migration.plan-check.v1", "files": len(entries),
              "bytes": total, "omitted": omissions, "missingFinalLabels": sorted(required - sources.keys()),
              "planSha256": file_sha256(plan_path), "reviewRequired": True, "migrationComplete": False}
    new_file(report_path, json_bytes(report))
    print(json.dumps({"files": len(entries), "bytes": total,
                      "omissions": len(omissions), "missingFinalLabels": report["missingFinalLabels"]}))


def final_plan_checks(plan, writers_stopped):
    if not writers_stopped:
        raise MigrationError("Final export needs --writers-stopped after a real quiescence check")
    sources = {row["label"]: Path(row["path"]) for row in plan["sources"]}
    if {"openclaw", "ssh", "pki", "deploy", "portable-ca", "source-evidence"} - sources.keys():
        raise MigrationError("Final plan is missing required source labels; run check-plan")
    for label, name in (("openclaw", ".env"), ("deploy", "environment.json"),
                        ("portable-ca", "root-ca.crt"), ("portable-ca", "root-ca.encrypted.pem"),
                        ("source-evidence", "source.json")):
        if not (sources[label] / name).is_file():
            raise MigrationError("A required final source file is missing")
    if read_json(sources["source-evidence"] / "source.json")["sourceCommit"] != git("rev-parse", "HEAD"):
        raise MigrationError("Source evidence is stale; rerun inventory at the final code commit")
    if (sources["source-evidence"] / "tracked-changes.patch").read_bytes() != git_bytes("diff", "--binary", "HEAD"):
        raise MigrationError("Tracked local changes differ from the inventory patch; refresh source evidence")
    key_data = (sources["portable-ca"] / "root-ca.encrypted.pem").read_bytes()
    if b"ENCRYPTED PRIVATE KEY" not in key_data:
        raise MigrationError("Portable CA must contain an encrypted private key")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    original = x509.load_pem_x509_certificate((sources["pki"] / "root-ca.crt").read_bytes())
    portable = x509.load_pem_x509_certificate((sources["portable-ca"] / "root-ca.crt").read_bytes())
    receipt = read_json(sources["portable-ca"] / "receipt.json")
    if (portable.fingerprint(hashes.SHA256()) != original.fingerprint(hashes.SHA256())
            or receipt.get("rootSha256") != portable.fingerprint(hashes.SHA256()).hex()
            or receipt.get("encryptedKeySha256") != hashlib.sha256(key_data).hexdigest()):
        raise MigrationError("Portable CA does not match original root/export receipt; re-export from Windows")
    assert_windows_quiesced()


def password(confirm: bool = False) -> bytes:
    if not sys.stdin.isatty():
        raise MigrationError("Use a local interactive terminal for the passphrase; never put it in arguments")
    value = getpass.getpass("Migration passphrase (at least 16 characters): ")
    if len(value) < 16:
        raise MigrationError("Passphrase too short: use at least 16 characters")
    if confirm and value != getpass.getpass("Repeat passphrase: "):
        raise MigrationError("Passphrases differ: enter the same passphrase twice")
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


def export_bundle(plan_path: Path, output: Path, secret: bytes, *, kind="snapshot", writers_stopped=False) -> None:
    private_output(output)
    if output.exists():
        raise FileExistsError("Export already exists")
    receipt_path = output.with_suffix(output.suffix + ".receipt.json")
    if receipt_path.exists():
        raise MigrationError("Export receipt already exists; choose a new bundle path")
    plan = read_json(plan_path)
    if kind not in {"snapshot", "final"}:
        raise MigrationError("Unsupported bundle kind")
    if kind == "final":
        final_plan_checks(plan, writers_stopped)
    entries, omissions, total = collect_plan(plan)
    for source in plan["sources"]:
        root = Path(source["path"])
        if root.is_dir() and output.resolve().is_relative_to(root.resolve()):
            raise ValueError("Output must be outside source directories")
    manifest = {"schema": "agentbridge.mac-migration.bundle.v1", "files": [], "kind": kind,
                "sourceCommit": git("rev-parse", "HEAD"), "planSha256": file_sha256(plan_path),
                "omitted": omissions, "consistency": "operator-must-quiesce-writers-for-final-export",
                "createdAt": datetime.now(timezone.utc).isoformat()}
    initial_stats = {name: (p.stat().st_size, p.stat().st_mtime_ns) for name, p in entries}
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
        if kind == "final":
            final_plan_checks(plan, writers_stopped)
            final_entries, _, _ = collect_plan(plan)
            if {name: (p.stat().st_size, p.stat().st_mtime_ns) for name, p in final_entries} != initial_stats:
                raise MigrationError("Final source tree changed during export; stop all writers and retry")
        archive.writestr("manifest.json", json_bytes(manifest))
    encrypted = seal(buffer.getvalue(), secret)
    if len(encrypted) > LIMIT + 1024 * 1024:
        raise MigrationError("Encrypted archive exceeds envelope size limit")
    # Verify authentication before publishing; caller separately stages and checks hashes.
    if unseal(encrypted, secret) != buffer.getvalue():
        raise ValueError("Encryption round trip failed")
    # A crash before completion leaves only a named encrypted partial, never a usable-looking final package.
    partial = output.with_name(output.name + "." + uuid4().hex + ".partial")
    new_file(partial, encrypted)
    try:
        os.link(partial, output)  # atomic, refuses to replace an existing export
    except OSError as error:
        raise MigrationError("Cannot publish bundle atomically; use a local filesystem supporting hard links") from error
    partial.unlink()
    receipt = {"status": "exported", "kind": kind, "files": len(entries), "omissions": len(omissions),
               "sha256": hashlib.sha256(encrypted).hexdigest(), "sourceCommit": manifest["sourceCommit"],
               "planSha256": manifest["planSha256"], "createdAt": manifest["createdAt"]}
    new_file(receipt_path, json_bytes(receipt))
    print(json.dumps(receipt))


def stage_bundle(bundle: Path, destination: Path | None, secret: bytes, *, expected_sha256=None,
                 require_final=False) -> None:
    if destination is not None:
        private_output(destination)
    if destination is not None and destination.exists():
        raise ValueError("Staging destination must not exist; live directories are never overwritten")
    if expected_sha256 is not None and file_sha256(bundle) != expected_sha256.lower():
        raise MigrationError("Bundle SHA256 does not match the separately supplied source receipt")
    if bundle.stat().st_size > LIMIT + 1024 * 1024:
        raise ValueError("Oversized bundle")
    plain = unseal(bundle.read_bytes(), secret)  # authenticate BEFORE any disk writes
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        validate_tree(names)
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
        if require_final and manifest.get("kind") != "final":
            raise MigrationError("This is not a final bundle; preparation snapshots must not activate a host")
        expected = {row["name"]: row for row in manifest["files"]}
        if len(expected) != len(manifest["files"]) or set(expected) != set(names) - {"manifest.json"}:
            raise ValueError("Manifest membership mismatch")
        for name, row in expected.items():
            data = archive.read(name)
            if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Archive hash mismatch")
        if destination is None:
            print(json.dumps({"status": "verified-only", "kind": manifest.get("kind", "legacy-unclassified"),
                              "files": len(expected), "activated": False}))
            return
        # A new staging root prevents overwriting an existing installation.
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name in names:
            target = destination / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            new_file(target, archive.read(name))
    print(json.dumps({"status": "staged-only", "kind": manifest.get("kind", "legacy-unclassified"),
                      "files": len(expected), "activated": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("inventory")
    scan.add_argument("--output", type=Path, required=True)
    export = sub.add_parser("export")
    export.add_argument("--plan", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--kind", choices=("snapshot", "final"), default="snapshot")
    export.add_argument("--writers-stopped", action="store_true")
    add = sub.add_parser("add-source")
    add.add_argument("--plan", type=Path, required=True)
    add.add_argument("--label", required=True)
    add.add_argument("--path", type=Path, required=True)
    check = sub.add_parser("check-plan")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--report", type=Path, required=True)
    sub.add_parser("check-quiesced")
    stage = sub.add_parser("stage")
    stage.add_argument("--bundle", type=Path, required=True)
    stage.add_argument("--destination", type=Path, required=True)
    stage.add_argument("--expected-sha256", required=True)
    stage.add_argument("--require-final", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    verify.add_argument("--require-final", action="store_true")
    args = parser.parse_args()
    if args.command == "inventory":
        inventory(args.output)
    elif args.command == "export":
        if args.kind == "final":
            final_plan_checks(read_json(args.plan), args.writers_stopped)
        export_bundle(args.plan, args.output, password(confirm=True), kind=args.kind, writers_stopped=args.writers_stopped)
    elif args.command == "add-source":
        add_source(args.plan, args.label, args.path)
    elif args.command == "check-plan":
        check_plan(args.plan, args.report)
    elif args.command == "check-quiesced":
        assert_windows_quiesced()
    else:
        if not re.fullmatch(r"[a-fA-F0-9]{64}", args.expected_sha256):
            raise MigrationError("Expected SHA256 must be the 64 hex characters from the source receipt")
        if file_sha256(args.bundle) != args.expected_sha256.lower():
            raise MigrationError("Bundle hash mismatch; no passphrase or extraction is needed")
        stage_bundle(args.bundle, getattr(args, "destination", None), password(),
                     expected_sha256=args.expected_sha256, require_final=args.require_final)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not echo exception values that could contain secrets/config contents.
        detail = str(error) if isinstance(error, MigrationError) else "Check local inputs; no host activation performed"
        print(f"Migration stopped ({type(error).__name__}): {detail}", file=sys.stderr)
        sys.exit(1)
