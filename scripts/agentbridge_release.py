"""Linux release transaction. Invoked by scripts/agentbridge-native publish.

An explicit predecessor contract AND unchanged SQLite schemas permit program
rollback. A backup is evidence for manual recovery, never permission to rewind
live business data. Interrupted transactions require operator inspection.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


DATABASES = ("agentbridge.db", "database/catalog.sqlite3",
             "database/grants.sqlite3", "database/reports.sqlite3")
TERMINAL = {"confirmed", "rolled_back", "preparation_failed"}


def atomic_write(path, data, mode=0o640):
    path = Path(path)
    temporary = path.with_name(path.name + ".next")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def point(path, target):
    temporary = path.with_name(path.name + ".next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, path)
    sync_directory(path.parent)


def schemas(home):
    result = {}
    for relative in DATABASES:
        path = home / relative
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            result[relative] = {
                "schema": db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                     "ORDER BY type,name").fetchall(),
                "userVersion": db.execute("PRAGMA user_version").fetchone()[0],
            }
    # Normalize tuples for comparison with persisted JSON on inspection.
    return json.loads(json.dumps(result))


def schema_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def authorized_schema_transition(policy, before, after):
    if policy["dataCompatibility"] == "no-migration":
        return before == after
    if policy["dataCompatibility"] != "reviewed-schema-transition":
        return False
    transitions = policy.get("schemaTransitions", {})
    if not transitions or set(before) != set(after) or not set(transitions).issubset(before):
        return False
    return all(
        (schema_digest(before[name]) == transitions[name].get("beforeSha256")
         and schema_digest(after[name]) == transitions[name].get("afterSha256"))
        if name in transitions else before[name] == after[name]
        for name in before
    )


class Release:
    def __init__(self, config, *, runner=subprocess.run, unit_root=Path("/etc/systemd/system")):
        self.config = config
        # macOS /var is an alias of /private/var. Compare canonical paths on
        # both sides of the confirmed-current gate (also valid on Linux).
        self.root = Path(config["root"]).resolve()
        self.release_id = config["releaseId"]
        self.directory = self.root / "releases" / self.release_id
        self.current = self.root / "current"
        self.envfile = self.root / "config/release.env"
        self.service = config["service"]
        self.unit_root = unit_root
        self.runner = runner
        self.state = {"releaseId": self.release_id, "stages": [],
                      "automaticDataRestore": False}

    def run(self, *args, capture=False, timeout=180):
        result = self.runner([str(a) for a in args], check=True, timeout=timeout,
                             cwd="/", text=True, capture_output=capture)
        return result.stdout.strip() if capture else None

    def stage(self, name, **fields):
        self.state.update(fields, status=name)
        self.state["stages"].append({"stage": name, "at": time.time()})
        atomic_write(self.directory / "deployment.json",
                     json.dumps(self.state, indent=2).encode())
        try:
            print(json.dumps({"releaseId": self.release_id, "stage": name}), flush=True)
        except OSError:
            # The durable receipt, not an SSH stdout pipe, defines the stage.
            pass

    def active_release(self):
        for line in self.envfile.read_text().splitlines():
            if line.startswith("AGENTBRIDGE_RELEASE_ID="):
                return line.split("=", 1)[1]
        raise RuntimeError("Missing running release identity")

    def python(self, previous=False):
        if previous:
            return Path(self.state["previousPython"])
        return self.directory / "venv/bin/python"

    def user_python(self, previous, *args, capture=True, timeout=360):
        return self.run("runuser", "-u", "agentbridge", "--", "env",
                        f"HOME={self.root / 'data'}",
                        f"AGENTBRIDGE_SESSION_KEY_FILE={self.root / 'config/session.key'}",
                        self.python(previous), "-P", *args, capture=capture, timeout=timeout)

    def previous_cli(self):
        # The pre-switch backup runs in the OLD environment. Its module name
        # cannot be inferred from this candidate's name, nor from an import error.
        probe = """import importlib.util, pathlib, sys
for name in ('agentbridge', 'bscli'):
    spec = importlib.util.find_spec(name)
    if spec is not None and spec.origin:
        assert pathlib.Path(spec.origin).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
        print(name + '.cli.main')
        break
"""
        module = self.user_python(True, "-c", probe)
        if module not in ("agentbridge.cli.main", "bscli.cli.main"):
            raise RuntimeError("Previous installed CLI could not be identified")
        return module

    def prepare(self):
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.run("chown", "root:agentbridge", self.directory)
        existing = self.directory / "deployment.json"
        if existing.exists():
            old = json.loads(existing.read_text())
            if old["status"] not in TERMINAL:
                raise RuntimeError("Unfinished transaction: inspect deployment.json before retry")
            if old["status"] == "confirmed":
                if self.active_release() != self.release_id or self.current.resolve() != self.directory:
                    raise RuntimeError("Confirmed release is no longer current; explicit rollback review required")
                self.state = old
                self.ready()
                return False
            raise RuntimeError("Failed candidate is preserved; use a new committed release after diagnosis")
        previous = self.active_release()
        self.run("systemctl", "is-active", "--quiet", self.service)
        policy = self.config["policy"]
        if previous not in policy["compatibleFrom"] or policy["dataCompatibility"] not in {"no-migration", "reviewed-schema-transition"}:
            raise RuntimeError("Release policy does not authorize this predecessor")
        if policy["dataCompatibility"] == "reviewed-schema-transition":
            before = schemas(self.root / "data")
            transitions = policy.get("schemaTransitions", {})
            if not transitions or any(name not in before or schema_digest(before[name]) != transition.get("beforeSha256")
                                      for name, transition in transitions.items()):
                raise RuntimeError("Reviewed migration baseline does not match current SQLite schema")
        if self.current.exists() and not self.current.is_symlink():
            raise RuntimeError("current must be a symlink")
        if self.current.is_symlink() and not self.current.resolve().is_relative_to(self.root / "releases"):
            raise RuntimeError("current escapes the release directory")
        previous_python = (self.current.resolve() if self.current.is_symlink() else self.root) / "venv/bin/python"
        self.stage("preparing", previousRelease=previous, previousPython=str(previous_python),
                   previousCurrent=os.readlink(self.current) if self.current.is_symlink() else None,
                   policy=policy)
        self.state["previousCli"] = self.previous_cli()
        atomic_write(self.directory / "transaction.json", json.dumps(self.config).encode(), 0o600)
        shutil.copyfile(__file__, self.directory / "release.py")
        wheel = Path(self.config["wheel"])
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != self.config["sha256"]:
            raise RuntimeError("Uploaded wheel hash mismatch")
        target = self.directory / self.config["wheelName"]
        shutil.copyfile(wheel, target)
        for name in ("artifact", "validation"):
            atomic_write(self.directory / (name + ".json"),
                         base64.b64decode(self.config[name]))
        # New venv, never pip-install into the running or legacy environment.
        self.run(previous_python, "-m", "venv", self.directory / "venv")
        self.run(self.python(), "-m", "pip", "install", "--disable-pip-version-check",
                 f"{target}[database-analysis]", timeout=900)
        self.run(self.python(), "-m", "pip", "check")
        self.run(self.python(), "-m", "compileall", "-q", self.directory / "venv")
        self.user_python(False, "-c", "from agentbridge.adapters.page_scripts import load_seeyon_action_page_script as load; "
                         "assert all(load(a)['script_source'] for a in ('ContinueSubmit','SaveDraft'))")
        module = self.user_python(False, "-c", "import agentbridge; print(agentbridge.__file__)")
        if not Path(module).is_relative_to(self.directory / "venv"):
            raise RuntimeError("service resolves unexpected agentbridge module")
        staged_units = self.directory / "units"
        staged_units.mkdir(exist_ok=True)
        for name, encoded in self.config["units"].items():
            atomic_write(staged_units / name, base64.b64decode(encoded), 0o644)
        # Verify using absolute candidate interpreter: current does not exist on bootstrap.
        verify = self.directory / "verify-units"
        verify.mkdir(exist_ok=True)
        for source in staged_units.iterdir():
            content = source.read_text().replace(str(self.current), str(self.directory))
            atomic_write(verify / source.name, content.encode(), 0o644)
        self.run("systemd-analyze", "verify", *sorted(verify.iterdir()))
        saved = self.directory / "previous"
        saved.mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(self.envfile, saved / "release.env")
        for name in self.config["units"]:
            source = self.unit_root / name
            if not source.is_file():
                raise RuntimeError(f"Missing previous unit: {name}")
            shutil.copy2(source, saved / name)
        packages = json.loads(self.run(self.python(), "-m", "pip", "list", "--format=json", capture=True))
        self.stage("prepared", installedPackages=packages)
        return True

    def ready(self, previous=False):
        expected = self.python(previous)
        for _ in range(45):
            try:
                self.run("systemctl", "is-active", "--quiet", self.service)
                pid = int(self.run("systemctl", "show", self.service, "-p", "MainPID", "--value", capture=True))
                command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                actual = Path(os.fsdecode(command[0]))
                # Python's resolved executable is shared; compare venv path before resolving python.
                if actual.parent.parent.resolve() != expected.parent.parent.resolve() or b"-P" not in command:
                    raise RuntimeError("service did not stabilize on the release unit")
                if Path(f"/proc/{pid}/cwd").resolve() != self.root:
                    raise RuntimeError("Unexpected service working directory")
                for port in (8782, 8783):
                    response = json.loads(self.run("curl", "--insecure", "--fail", "--silent", "--max-time", "3",
                                                  f"https://{self.config['host']}:{port}/readyz", capture=True))
                    if response.get("status") != "ready":
                        raise RuntimeError("service readiness did not stabilize before backup")
                return
            except (subprocess.SubprocessError, OSError, ValueError, RuntimeError):
                time.sleep(1)
        raise RuntimeError("service readiness did not stabilize before backup")

    def switch(self):
        previous_cli = self.state["previousCli"]
        # Timer is paused; an already running backup is allowed to finish first.
        self.state["timerWasActive"] = self.run("systemctl", "show", self.service + "-backup.timer",
                                              "-p", "ActiveState", "--value", capture=True) == "active"
        self.stage("stopping")
        self.run("systemctl", "stop", self.service + "-backup.timer")
        for _ in range(180):
            status = self.run("systemctl", "show", self.service + "-backup.service",
                              "-p", "ActiveState", "--value", capture=True)
            if status not in ("active", "activating", "deactivating"):
                break
            time.sleep(2)
        else:
            raise RuntimeError("Previous backup did not finish; switch refused")
        self.run("systemctl", "stop", self.service)
        self.stage("stopped")
        self.state["schemaBefore"] = schemas(self.root / "data")
        backup = json.loads(self.user_python(True, "-m", previous_cli, "--home", self.root / "data",
                                            "diagnostics", "backup-create", "--output-dir", self.root / "backups",
                                            "--release-id", self.state["previousRelease"]))
        if not backup.get("passed") or not backup.get("manifestPath"):
            raise RuntimeError("Pre-switch recovery point was not validated")
        self.stage("recovery_point_created", recoveryPoint=backup)
        self.stage("switching")
        point(self.current, self.directory)
        for name in self.config["units"]:
            atomic_write(self.unit_root / name, (self.directory / "units" / name).read_bytes(), 0o644)
        atomic_write(self.envfile, f"AGENTBRIDGE_RELEASE_ID={self.release_id}\n".encode())
        self.run("chown", "root:agentbridge", self.envfile)
        self.run("systemctl", "daemon-reload")
        self.run("systemctl", "start", self.service)
        self.stage("checking")
        self.ready()
        if not authorized_schema_transition(self.config["policy"], self.state["schemaBefore"], schemas(self.root / "data")):
            raise RuntimeError("Release schema contract violated: SQLite schema changed unexpectedly")
        self.stage("confirmed")

    def recover(self):
        self.stage("recovering")
        self.run("systemctl", "stop", self.service)
        if "schemaBefore" in self.state and schemas(self.root / "data") != self.state["schemaBefore"]:
            self.stage("manual_recovery_required", reason="Schema changed; data restore and program rollback forbidden automatically")
            return
        saved = self.directory / "previous"
        for name in self.config["units"]:
            atomic_write(self.unit_root / name, (saved / name).read_bytes(), 0o644)
        atomic_write(self.envfile, (saved / "release.env").read_bytes())
        self.run("chown", "root:agentbridge", self.envfile)
        if self.state["previousCurrent"] is None:
            self.current.unlink(missing_ok=True)
        else:
            point(self.current, self.state["previousCurrent"])
        self.run("systemctl", "daemon-reload")
        self.run("systemctl", "start", self.service)
        self.ready(previous=True)
        if self.state.get("timerWasActive"):
            self.run("systemctl", "start", self.service + "-backup.timer")
        self.stage("rolled_back")

    def execute(self):
        try:
            if not self.prepare():
                self.complete()
                return
        except Exception as error:
            if self.state["stages"] and self.state["status"] == "preparing":
                self.stage("preparation_failed", failure=str(error))
            raise
        try:
            self.switch()
        except Exception as error:
            self.state["failure"] = str(error)
            try:
                self.recover()
            except Exception as recovery_error:
                self.stage("manual_recovery_required", recoveryError=str(recovery_error))
            raise
        self.complete()

    def complete(self):
        """Resume only confirmed post-switch work; never re-install or rewind data."""
        if (self.state.get("status") != "confirmed" or self.active_release() != self.release_id
                or self.current.resolve() != self.directory):
            raise RuntimeError("Completion requires the exact confirmed current release")
        progress = self.state.setdefault("completion", {})
        for phase, args in (
            ("backup_timer", ("systemctl", "enable", "--now", self.service + "-backup.timer")),
            ("post_release_backup", ("systemctl", "start", self.service + "-backup.service")),
        ):
            if progress.get(phase, {}).get("status") == "succeeded":
                continue
            progress[phase] = {"status": "running", "at": time.time()}
            self.stage("confirmed", completion=progress)
            try:
                self.run(*args, timeout=400)
            except Exception as error:
                progress[phase] = {"status": "failed", "at": time.time(), "error": str(error)}
                self.stage("confirmed", completion=progress)
                raise
            progress[phase] = {"status": "succeeded", "at": time.time()}
            self.stage("confirmed", completion=progress)


def main():
    import fcntl
    recovering = sys.argv[1] == "--recover"
    completing = sys.argv[1] == "--complete"
    config = json.loads(Path(sys.argv[2] if recovering or completing else sys.argv[1]).read_text())
    root = Path(config["root"])
    with (root / ".release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if completing:
            transaction = Release(config)
            transaction.state = json.loads((transaction.directory / "deployment.json").read_text())
            transaction.complete()
            transaction.ready()
            return
        if recovering:
            transaction = Release(config)
            transaction.state = json.loads((transaction.directory / "deployment.json").read_text())
            status = transaction.state["status"]
            if status in ("preparing", "prepared"):
                transaction.stage("preparation_failed", reason="Operator closed interrupted preparation; live service untouched")
            elif status in ("stopping", "stopped", "recovery_point_created", "switching", "checking", "recovering"):
                transaction.recover()
            else:
                raise RuntimeError("This state requires manual review; recovery cannot be replayed")
            return
        # Never silently bypass an interrupted transaction with a different candidate.
        for receipt in (root / "releases").glob("*/deployment.json"):
            if json.loads(receipt.read_text())["status"] not in TERMINAL:
                raise RuntimeError(f"Unfinished release requires inspection: {receipt}")
        Release(config).execute()


if __name__ == "__main__":
    main()
