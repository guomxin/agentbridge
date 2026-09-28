"""Failure injection uses real files/SQLite; service and package commands are fakes."""
import base64
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("release_transaction", Path(__file__).resolve().parents[1] / "scripts/agentbridge_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class FixtureRelease(release.Release):
    fail_install = False
    fail_health = False
    fail_backup = False
    change_schema = False
    fail_after_confirmation = False

    def run(self, *args, capture=False, timeout=180):
        args = tuple(str(a) for a in args)
        self.commands.append(args)
        if self.fail_install and "install" in args:
            raise RuntimeError("injected install failure")
        if self.fail_after_confirmation and args[:3] == ("systemctl", "start", "agentbridge-backup.service"):
            raise RuntimeError("injected ancillary backup failure")
        if "ActiveState" in args:
            return "active" if args[2].endswith(".timer") else "inactive"
        if "--format=json" in args:
            return "[]"
        return ""

    def user_python(self, previous, *args, **kwargs):
        if "backup-create" in args:
            if self.fail_backup:
                raise RuntimeError("injected recovery point failure")
            return json.dumps({"passed": True, "manifestPath": "fixture-backup.manifest.json"})
        if "import bscli; print(bscli.__file__)" in args:
            return str(self.directory / "venv/lib/site-packages/bscli/__init__.py")
        return ""

    def ready(self, previous=False):
        self.commands.append(("ready", previous))
        if not previous and self.change_schema:
            with closing(sqlite3.connect(self.root / "data/agentbridge.db")) as db:
                db.execute("CREATE TABLE incompatible(new_field TEXT)")
        if not previous and self.fail_health:
            raise RuntimeError("injected candidate health failure")


class ReleaseTransactionTests(unittest.TestCase):
    def test_reviewed_transition_requires_exact_before_and_after(self):
        before = {"agentbridge.db": {"schema": ["old"]}, "other.db": {"schema": []}}
        after = {"agentbridge.db": {"schema": ["new"]}, "other.db": {"schema": []}}
        policy = {"dataCompatibility": "reviewed-schema-transition", "schemaTransitions": {
            "agentbridge.db": {"beforeSha256": release.schema_digest(before["agentbridge.db"]),
                              "afterSha256": release.schema_digest(after["agentbridge.db"])}}}
        self.assertTrue(release.authorized_schema_transition(policy, before, after))
        self.assertFalse(release.authorized_schema_transition(policy, after, after))
        self.assertFalse(release.authorized_schema_transition(policy, before, before))
        self.assertFalse(release.authorized_schema_transition(policy, before, {**after, "other.db": {"schema": ["bad"]}}))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "config").mkdir()
        (root / "config/release.env").write_text("AGENTBRIDGE_RELEASE_ID=old\n")
        units = root / "systemd"
        units.mkdir()
        names = ("agentbridge.service", "agentbridge-backup.service", "agentbridge-backup.timer")
        for name in names:
            (units / name).write_text("old " + name)
        for relative in release.DATABASES:
            path = root / "data" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE evidence(value TEXT)")
                db.execute("INSERT INTO evidence VALUES ('keep-business-data')")
                db.commit()
        wheel = root / "fixture.whl"
        wheel.write_bytes(b"candidate")
        config = {"root": str(root), "releaseId": "new", "service": "agentbridge", "host": "fixture",
                  "policy": {"compatibleFrom": ["old"], "dataCompatibility": "no-migration"},
                  "wheel": str(wheel), "wheelName": wheel.name,
                  "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                  "artifact": "e30=", "validation": "e30=",
                  "units": {n: base64.b64encode(("new " + n).encode()).decode() for n in names}}
        self.subject = FixtureRelease(config, unit_root=units)
        self.subject.commands = []

    def assert_old(self):
        s = self.subject
        self.assertEqual(s.active_release(), "old")
        self.assertFalse(s.current.is_symlink())
        self.assertEqual((s.unit_root / "agentbridge.service").read_text(), "old agentbridge.service")
        with closing(sqlite3.connect(s.root / "data/agentbridge.db")) as db:
            self.assertEqual(db.execute("SELECT value FROM evidence").fetchall(), [("keep-business-data",)])

    def test_install_failure_never_stops_or_mutates_live_release(self):
        s = self.subject
        s.fail_install = True
        with self.assertRaisesRegex(RuntimeError, "install failure"):
            s.execute()
        self.assert_old()
        self.assertEqual(s.state["status"], "preparation_failed")
        self.assertFalse(any(c[:2] == ("systemctl", "stop") for c in s.commands))

    def test_bad_hash_never_stops_live_release(self):
        s = self.subject
        s.config["sha256"] = "bad"
        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            s.execute()
        self.assert_old()
        self.assertFalse(any(c[:2] == ("systemctl", "stop") for c in s.commands))

    def test_wrong_predecessor_is_refused(self):
        self.subject.config["policy"]["compatibleFrom"] = ["other"]
        with self.assertRaisesRegex(RuntimeError, "predecessor"):
            self.subject.execute()
        self.assert_old()

    def test_backup_failure_restores_old_service_without_switch(self):
        s = self.subject
        s.fail_backup = True
        with self.assertRaisesRegex(RuntimeError, "recovery point failure"):
            s.execute()
        self.assert_old()
        self.assertEqual(s.state["status"], "rolled_back")
        self.assertIn(("ready", True), s.commands)

    @unittest.skipIf(os.name == "nt", "POSIX symlink transaction is also executed on Linux")
    def test_health_failure_restores_units_pointer_and_identity_not_data(self):
        s = self.subject
        s.fail_health = True
        with self.assertRaisesRegex(RuntimeError, "health failure"):
            s.execute()
        self.assert_old()
        self.assertEqual(s.state["status"], "rolled_back")
        self.assertFalse(s.state["automaticDataRestore"])

    @unittest.skipIf(os.name == "nt", "POSIX symlink transaction is also executed on Linux")
    def test_schema_change_stops_service_and_forbids_automatic_rollback(self):
        s = self.subject
        s.change_schema = True
        with self.assertRaisesRegex(RuntimeError, "contract violated"):
            s.execute()
        self.assertEqual(s.state["status"], "manual_recovery_required")
        self.assertEqual(s.active_release(), "new")
        self.assertNotIn(("ready", True), s.commands)
        self.assertEqual(s.commands[-1], ("systemctl", "stop", "agentbridge"))

    @unittest.skipIf(os.name == "nt", "POSIX symlink transaction is also executed on Linux")
    def test_success_preserves_previous_and_recovery_point(self):
        s = self.subject
        s.execute()
        self.assertEqual(s.state["status"], "confirmed")
        self.assertEqual(s.current.resolve(), s.directory)
        self.assertEqual(s.active_release(), "new")
        self.assertIn("recoveryPoint", s.state)
        self.assertEqual((s.directory / "previous/release.env").read_text(), "AGENTBRIDGE_RELEASE_ID=old\n")
        stages = [stage["stage"] for stage in s.state["stages"]]
        self.assertLess(stages.index("recovery_point_created"), stages.index("switching"))

    @unittest.skipIf(os.name == "nt", "POSIX symlink transaction is also executed on Linux")
    def test_confirmed_retry_checks_health_without_install_or_restart(self):
        s = self.subject
        s.execute()
        s.commands.clear()
        s.execute()
        self.assertEqual(s.commands, [("chown", "root:agentbridge", str(s.directory)), ("ready", False)])

    @unittest.skipIf(os.name == "nt", "POSIX symlink transaction is also executed on Linux")
    def test_ancillary_failure_does_not_rollback_confirmed_release(self):
        s = self.subject
        s.fail_after_confirmation = True
        with self.assertRaisesRegex(RuntimeError, "ancillary"):
            s.execute()
        self.assertEqual(s.state["status"], "confirmed")
        self.assertEqual(s.active_release(), "new")
        self.assertNotIn(("ready", True), s.commands)
        receipt = json.loads((s.directory / "deployment.json").read_text())
        self.assertEqual(receipt["completion"]["post_release_backup"]["status"], "failed")
        recovered = FixtureRelease(s.config, unit_root=s.unit_root)
        recovered.state = receipt
        recovered.commands = []
        recovered.complete()
        self.assertEqual(recovered.commands, [("systemctl", "start", "agentbridge-backup.service")])
        recovered.commands.clear()
        recovered.complete()
        self.assertEqual(recovered.commands, [])

    def test_completion_rejects_unconfirmed_or_different_current_release(self):
        s = self.subject
        for state in ("checking", "confirmed"):
            s.state["status"] = state
            with self.assertRaisesRegex(RuntimeError, "exact confirmed"):
                s.complete()
        self.assertEqual(s.commands, [])

    def test_interrupted_transaction_is_not_silently_replayed(self):
        s = self.subject
        s.prepare()
        s.stage("switching")
        with self.assertRaisesRegex(RuntimeError, "Unfinished transaction"):
            s.prepare()

    def test_lost_stdout_does_not_change_durable_stage(self):
        s = self.subject
        s.prepare()
        with patch("builtins.print", side_effect=BrokenPipeError("SSH disconnected")):
            s.stage("checking")
        self.assertEqual(json.loads((s.directory / "deployment.json").read_text())["status"], "checking")

    @unittest.skipIf(os.name == "nt", "POSIX symlink transaction is also executed on Linux")
    def test_second_release_failure_restores_previous_version_pointer(self):
        first = self.subject
        first.execute()
        config = dict(first.config, releaseId="next", policy={"compatibleFrom": ["new"], "dataCompatibility": "no-migration"})
        second = FixtureRelease(config, unit_root=first.unit_root)
        second.commands = []
        second.fail_health = True
        with self.assertRaisesRegex(RuntimeError, "health failure"):
            second.execute()
        self.assertEqual(second.state["status"], "rolled_back")
        self.assertEqual(second.current.resolve(), first.directory)
        self.assertEqual(second.active_release(), "new")


if __name__ == "__main__":
    unittest.main()
