"""Versioned, fail-closed multi-database recovery points; never starts a service."""
from __future__ import annotations

from contextlib import ExitStack, closing
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
from uuid import uuid4
import zipfile

from bscli.core.data_source_secrets import DataSourceSecretStore
from bscli.core.session_secrets import _default_protector

SCHEMA = "agentbridge.recovery-bundle.v2"
DATABASES = ("agentbridge.db", "database/catalog.sqlite3", "database/grants.sqlite3",
             "database/reports.sqlite3")
POLICY = {"sessions": "reauthenticate", "reports": "expire", "downloads": "expire",
          "keys": "external-required", "activation": "manual-review-no-auto-replay"}
KEY_CONTEXT = b"agentbridge.recovery-key-proof.v2"
KEY_PROOF = b"agentbridge-recovery-key-verified"


class RecoveryError(RuntimeError):
    pass


class SnapshotConflict(RecoveryError):
    """Only stable resource names/reasons, never source values or credentials."""


def _pin_database(connection):
    connection.execute("BEGIN")
    connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()


def _hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _safe(root, name):
    relative = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or relative.is_absolute()
            or any(p in ("", ".", "..") for p in name.split("/"))):
        raise RecoveryError("invalid recovery member path")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise RecoveryError("recovery member escapes root")
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents) if p != root.parent):
        raise RecoveryError("recovery paths must not contain links")
    return path


def _inventory(home):
    result = {}
    for folder in ("systems", "database/credentials"):
        directory = _safe(home, folder)
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            _safe(home, path.relative_to(home).as_posix())
            if path.is_file():
                if path.suffix not in (".json", ".bin"):
                    continue  # transient atomic-write files are not referenced by catalogs
                stat = path.stat()
                result[path.relative_to(home).as_posix()] = (
                    stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, _hash(path))
    return result


def _copy_database(source, target, deadline):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise RecoveryError("recovery snapshot deadline exceeded")
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination, pages=256, progress=progress, sleep=0.02)
    target.chmod(0o600)


def _snapshot(home, stage, deadline):
    # Keep each monitoring connection alive. data_version values are only comparable
    # on the SAME connection, outside a pinned read transaction.
    with ExitStack() as stack:
        paths = {name: _safe(home, name) for name in DATABASES}
        if not all(path.is_file() for path in paths.values()):
            raise RecoveryError("all four runtime databases are required")
        identities = {name: (p.stat().st_dev, p.stat().st_ino) for name, p in paths.items()}
        connections = {name: stack.enter_context(closing(_connect(path)))
                       for name, path in paths.items()}
        versions = {name: c.execute("PRAGMA data_version").fetchone()[0]
                    for name, c in connections.items()}
        files = _inventory(home)
        # Pin all read views within a verified commit-free acquisition interval.
        # WAL writers may then continue while SQLite copies these fixed views.
        readers = {name: stack.enter_context(closing(_connect(path)))
                   for name, path in paths.items()}
        for c in readers.values():
            _pin_database(c)
        changed = [name for name, c in connections.items()
                   if c.execute("PRAGMA data_version").fetchone()[0] != versions[name]]
        if changed:
            raise SnapshotConflict("database commit during snapshot acquisition: " + ", ".join(changed))
        recovery_point_at = datetime.now(timezone.utc).isoformat()
        for name, c in readers.items():
            _copy_database(c, stage / name, deadline)
        for name in files:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(_safe(home, name), target)
            target.chmod(0o600)
            if _hash(target) != files[name][-1]:
                raise SnapshotConflict("protected file changed while copying: " + name)
        # Files lack SQLite snapshot isolation; retain whole-copy validation.
        after_files = _inventory(home)
        if files != after_files:
            changed_files = sorted(name for name in files.keys() | after_files.keys()
                                   if files.get(name) != after_files.get(name))
            raise SnapshotConflict("protected file inventory changed: " + ", ".join(changed_files))
        for name, c in connections.items():
            stat = paths[name].stat()
            if (stat.st_dev, stat.st_ino) != identities[name]:
                raise SnapshotConflict("database file replaced: " + name)
        return recovery_point_at


def _validate_home(home, protector):
    from bscli.core.runtime_backup import validate_runtime_backup
    central = validate_runtime_backup(home / DATABASES[0])
    if not central["passed"]:
        raise RecoveryError("central runtime relationship validation failed")
    counts = {}
    with ExitStack() as stack:
        dbs = {name: stack.enter_context(closing(_connect(home / name))) for name in DATABASES}
        for name, c in dbs.items():
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RecoveryError("database integrity check failed")
        central_db = dbs[DATABASES[0]]
        central_tables = {r[0] for r in central_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for link, target, identifier in (("task_operations", "operations", "operation_id"),
                                          ("task_interactions", "interactions", "interaction_id")):
            if link in central_tables:
                bad = central_db.execute(f"""SELECT COUNT(*) FROM {link} l
                    LEFT JOIN agent_tasks t ON t.task_id=l.task_id
                    LEFT JOIN {target} o ON o.{identifier}=l.{identifier}
                    WHERE t.task_id IS NULL OR o.{identifier} IS NULL
                    OR l.user_subject<>t.user_subject OR l.user_subject<>o.user_subject""").fetchone()[0]
                if bad:
                    raise RecoveryError("task ownership relationship is invalid")
        cat, grants, reports = (dbs[name] for name in DATABASES[1:])
        records = {}
        credential_count = 0
        secrets = DataSourceSecretStore(home / "database/credentials", protector=protector)
        for sid, raw in cat.execute("SELECT id, record FROM sources"):
            record = json.loads(raw)
            if record.get("source_id") != sid or type(record.get("revision")) is not int or record["revision"] < 1:
                raise RecoveryError("source identity or revision is invalid")
            records[sid] = record
            for field in ("active", "draft"):
                config = record.get(field)
                if config:
                    from bscli.database.sources import validate
                    validate({k: v for k, v in config.items() if k != "credential_ref"})
                    ref = config.get("credential_ref")
                    if not isinstance(ref, str) or not ref.startswith(sid + ":"):
                        raise RecoveryError("source credential reference is invalid")
                    try:
                        payload = secrets.load(ref)
                        if not isinstance(payload.get("password"), str) or not payload["password"]:
                            raise ValueError()
                    except Exception:
                        raise RecoveryError("credential missing or cannot decrypt with recovery key") from None
                    credential_count += 1
        from bscli.database.independent import CAPABILITIES
        subjects = set()
        for table in ("mcp_identity_tokens", "workspace_accounts"):
            if table in central_tables:
                subjects.update(r[0] for r in central_db.execute(f"SELECT user_subject FROM {table}"))
        grant_count = 0
        for subject, sid, raw, revision in grants.execute(
                "SELECT subject,source,capabilities,revision FROM source_grants"):
            capabilities = json.loads(raw)
            if (sid not in records or not subject or subject not in subjects
                    or type(revision) is not int or revision < 1
                    or not isinstance(capabilities, list)
                    or any(cap not in CAPABILITIES for cap in capabilities)):
                raise RecoveryError("source grant relationship or revision is invalid")
            grant_count += 1
        # Report payloads are deliberately excluded and expired during restoration.
        counts.update(sources=len(records), grants=grant_count, credentialReferences=credential_count,
                      reports=reports.execute("SELECT COUNT(*) FROM reports").fetchone()[0])
        for name, c in dbs.items():
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts[name] = {table: c.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                            for table in sorted(tables)}
    for path in (home / "systems").glob("*.json"):
        from bscli.core.config import ConfigStore
        ConfigStore(home).load_system(path.stem)
    return {"passed": True, "rowCounts": counts, "credentialsDecryptable": True}


def create_recovery_bundle(home, output_dir, *, release_id="development", protector=None,
                           attempts=3, timeout_seconds=90):
    started = time.monotonic()
    home, destination = Path(home).resolve(), Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    protector = protector or _default_protector()
    generation = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    archive = destination / f"agentbridge-{generation}.zip"
    manifest_path = destination / f"agentbridge-{generation}.manifest.json"
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=destination) as temporary:
        stage = Path(temporary)
        deadline = started + timeout_seconds
        conflicts = []
        for attempt in range(1, attempts + 1):
            candidate = stage / str(attempt)
            candidate.mkdir(mode=0o700)
            try:
                recovery_point_at = _snapshot(home, candidate, deadline)
                break
            except SnapshotConflict as error:
                conflicts.append(str(error))
                if attempt < attempts:
                    delay = min(0.1 * 2 ** (attempt - 1), 1.0)
                    if time.monotonic() + delay >= deadline:
                        raise RecoveryError("recovery snapshot deadline exceeded; " + str(error)) from error
                    time.sleep(delay)
        else:
            raise RecoveryError("runtime changed during every snapshot; no recovery point published; "
                                + "; ".join(conflicts))
        validation = _validate_home(candidate, protector)
        # Read-only validation of a WAL-mode snapshot may create empty WAL/SHM
        # sidecars. Only the completed SQLite backup files and declared assets
        # belong to the archive; never serialize validation scratch files.
        member_names = set(DATABASES) | set(_inventory(candidate))
        members = {name: {"sha256": _hash(candidate / name), "byteSize": (candidate / name).stat().st_size}
                   for name in sorted(member_names)}
        temporary_archive = stage / "bundle.zip"
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name in members:
                z.write(candidate / name, name)
        manifest = {"schemaVersion": SCHEMA, "generation": generation,
                    "createdAt": datetime.now(timezone.utc).isoformat(), "releaseId": release_id,
                    "recoveryPointAt": recovery_point_at,
                    "archiveFile": archive.name, "sha256": _hash(temporary_archive),
                    "members": members, "policy": POLICY, "validation": validation,
                    "consistency": "coordinated-pinned-read-snapshots", "attempts": attempt,
                    "conflicts": conflicts,
                    "durationSeconds": round(time.monotonic() - started, 3),
                    "keyProof": base64.b64encode(protector.protect(KEY_PROOF, context=KEY_CONTEXT)).decode()}
        temporary_manifest = stage / "manifest.json"
        temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_archive.chmod(0o600)
        temporary_manifest.chmod(0o600)
        os.replace(temporary_archive, archive)
        os.replace(temporary_manifest, manifest_path)  # only complete generations are discoverable
    return {"schemaVersion": SCHEMA, "passed": True, "manifestPath": str(manifest_path),
            "sha256": manifest["sha256"], "generation": generation, "attempts": attempt,
            "durationSeconds": manifest["durationSeconds"], "validation": validation}


def _extract(manifest_path, root):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != SCHEMA or manifest.get("policy") != POLICY:
        raise RecoveryError("unsupported recovery manifest or policy")
    archive = _safe(manifest_path.parent, manifest["archiveFile"])
    if archive.parent != manifest_path.parent or _hash(archive) != manifest.get("sha256"):
        raise RecoveryError("recovery archive hash mismatch")
    members = manifest["members"]
    if not set(DATABASES) <= set(members):
        raise RecoveryError("recovery manifest is missing a required database")
    with zipfile.ZipFile(archive) as z:
        if len(z.namelist()) != len(members) or set(z.namelist()) != set(members):
            raise RecoveryError("archive inventory does not match manifest")
        for info in z.infolist():
            target = _safe(root, info.filename)
            if info.file_size != members[info.filename]["byteSize"]:
                raise RecoveryError("recovery member size mismatch")
            if info.filename not in DATABASES and not (
                    info.filename.startswith("systems/") and info.filename.endswith(".json") or
                    info.filename.startswith("database/credentials/") and info.filename.endswith(".bin")):
                raise RecoveryError("unexpected recovery member")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with z.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
            if _hash(target) != members[info.filename]["sha256"]:
                raise RecoveryError("recovery member hash mismatch")
    return manifest


def _key_check(manifest, protector):
    try:
        if protector.unprotect(base64.b64decode(manifest["keyProof"], validate=True), context=KEY_CONTEXT) != KEY_PROOF:
            raise ValueError()
    except Exception:
        raise RecoveryError("recovery key is missing or does not match") from None


def validate_recovery_bundle(manifest_path, *, protector=None):
    protector = protector or _default_protector()
    with tempfile.TemporaryDirectory(prefix="agentbridge-verify-") as temporary:
        root = Path(temporary)
        manifest = _extract(manifest_path, root)
        _key_check(manifest, protector)
        validation = _validate_home(root, protector)
    return {**validation, "schemaVersion": SCHEMA, "manifestHashMatches": True,
            "sha256": manifest["sha256"]}


def restore_recovery_bundle(manifest_path, output_dir, *, protector=None):
    started = time.monotonic()
    protector = protector or _default_protector()
    parent = Path(output_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Always create a new directory: never overwrite a live home or a previous drill.
    root = Path(tempfile.mkdtemp(prefix="restore-", dir=parent))
    try:
        manifest = _extract(manifest_path, root)
        _key_check(manifest, protector)
        validation = _validate_home(root, protector)
        normalized = {}
        with closing(sqlite3.connect(root / DATABASES[0])) as c, c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("sessions", "document_downloads", "timeline_attachments", "task_artifacts"):
                if table in tables:
                    normalized[table] = c.execute(f"UPDATE {table} SET state='expired'").rowcount
        with closing(sqlite3.connect(root / DATABASES[3])) as c, c:
            normalized["reports"] = c.execute("UPDATE reports SET status='expired'").rowcount
        write_rejected = True
        for name in DATABASES:
            with closing(_connect(root / name)) as c:
                c.execute("PRAGMA query_only=ON")
                try:
                    c.execute("CREATE TABLE recovery_write_probe (id INTEGER)")
                    write_rejected = False
                except sqlite3.OperationalError as exc:
                    if "readonly" not in str(exc).lower() and "read-only" not in str(exc).lower():
                        raise
        if not write_rejected:
            raise RecoveryError("restored database read-only probe failed")
        report = {"schemaVersion": "agentbridge.recovery-drill.v2", "passed": True,
                  "sourceReleaseId": manifest["releaseId"], "generation": manifest["generation"],
                  "sourceHashMatches": True, "readOnlyOpen": True, "writeRejected": True,
                  "validation": validation, "normalized": normalized, "policy": POLICY,
                  "durationSeconds": round(time.monotonic() - started, 3),
                  "backupAgeSeconds": round((datetime.now(timezone.utc) - datetime.fromisoformat(manifest["recoveryPointAt"])).total_seconds(), 3),
                  "businessCalls": 0, "businessListReads": 0, "businessWrites": 0,
                  "activationAllowed": False, "drillDirectory": str(root)}
        report_path = root / "restore-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.chmod(0o600)
        return {**report, "reportPath": str(report_path)}
    except Exception:
        # Only this invocation's freshly allocated child; never caller's destination.
        shutil.rmtree(root)
        raise
