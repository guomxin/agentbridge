from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from agentbridge.core import recovery_bundle as bundle
from agentbridge.core.config import ConfigStore, SystemProfile
from agentbridge.core.data_source_secrets import DataSourceSecretStore
from agentbridge.core.session_secrets import AesGcmSessionStateProtector
from agentbridge.database.independent import DatabaseGrants
from agentbridge.database.sources import Sources


class RecoveryBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.protector = AesGcmSessionStateProtector(b"a" * 32)
        with closing(sqlite3.connect(self.home / "agentbridge.db")) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE operations (operation_id TEXT PRIMARY KEY);
                CREATE TABLE interactions (interaction_id TEXT PRIMARY KEY);
                CREATE TABLE agent_tasks (task_id TEXT PRIMARY KEY, user_subject TEXT);
                CREATE TABLE runtime_traces (trace_id TEXT, task_id TEXT, user_subject TEXT);
                CREATE TABLE runtime_incidents (incident_id TEXT PRIMARY KEY);
                CREATE TABLE sessions (session_id TEXT, state TEXT);
                CREATE TABLE document_downloads (download_id TEXT, state TEXT);
                CREATE TABLE timeline_attachments (attachment_id TEXT, state TEXT);
                CREATE TABLE task_artifacts (artifact_id TEXT, state TEXT);
                CREATE TABLE mcp_identity_tokens (user_subject TEXT);
                INSERT INTO mcp_identity_tokens VALUES ('alice');
                INSERT INTO agent_tasks VALUES ('t1','alice');
                INSERT INTO runtime_traces VALUES ('r1','t1','alice');
                INSERT INTO sessions VALUES ('s1','active');
                INSERT INTO document_downloads VALUES ('d1','ready');
                INSERT INTO timeline_attachments VALUES ('a1','ready');
                INSERT INTO task_artifacts VALUES ('a1','ready');
            """)
        with patch("agentbridge.core.data_source_secrets._default_protector", return_value=self.protector):
            sources = Sources(self.home)
        config = dict(source_id="demo", name="Demo", description="Fixture", engine="postgresql",
                      host="database.invalid", port=5432, dbname="demo", username="reader",
                      sslmode="verify-full", allowed_relations=["analysis.logs"], template_pack="generic",
                      statement_timeout_ms=1000, export_rows=100, export_bytes=4096)
        sources.write("demo", "save", revision=0, actor="operator", reason="test", config=config, password="test-secret")
        DatabaseGrants(self.home / "database/grants.sqlite3").set("alice", ["database.free.read"], source_id="demo")
        with closing(sqlite3.connect(self.home / "database/reports.sqlite3")) as c:
            c.executescript("CREATE TABLE reports (id TEXT, status TEXT); INSERT INTO reports VALUES ('r1','ready'); INSERT INTO reports VALUES ('r2','running');")
        ConfigStore(self.home).save_system(SystemProfile("demo", "Demo", "https://example.invalid", ["https://example.invalid"]))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **kwargs):
        return bundle.create_recovery_bundle(self.home, self.root / "backups", protector=self.protector, **kwargs)

    def test_restore_real_encrypted_credentials_and_expire_ephemeral_state(self):
        before = {p: bundle._hash(p) for p in self.home.rglob("*") if p.is_file()}
        backup = self.create(release_id="test-commit")
        manifest = json.loads(Path(backup["manifestPath"]).read_text())
        self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in manifest["members"]))
        with patch("socket.create_connection", side_effect=AssertionError("no network")):
            report = bundle.restore_recovery_bundle(backup["manifestPath"], self.root / "drills", protector=self.protector)
        self.assertTrue(report["passed"])
        self.assertTrue(report["writeRejected"])
        self.assertFalse(report["activationAllowed"])
        self.assertEqual(report["sourceReleaseId"], "test-commit")
        self.assertEqual(report["validation"]["rowCounts"]["grants"], 1)
        restored = Path(report["drillDirectory"])
        with closing(sqlite3.connect(restored / "database/reports.sqlite3")) as c:
            self.assertEqual(c.execute("SELECT DISTINCT status FROM reports").fetchall(), [("expired",)])
        with closing(sqlite3.connect(restored / "agentbridge.db")) as c:
            self.assertEqual(c.execute("SELECT state FROM sessions").fetchone()[0], "expired")
            self.assertEqual(c.execute("SELECT state FROM task_artifacts").fetchone()[0], "expired")
        self.assertEqual(before, {p: bundle._hash(p) for p in before})
        self.assertNotIn("test-secret", Path(backup["manifestPath"]).read_text())

    def test_wrong_key_and_missing_credential_fail_closed(self):
        backup = self.create()
        with self.assertRaisesRegex(bundle.RecoveryError, "key"):
            bundle.restore_recovery_bundle(backup["manifestPath"], self.root / "drills",
                                           protector=AesGcmSessionStateProtector(b"b" * 32))
        self.assertEqual(list((self.root / "drills").iterdir()), [])
        next((self.home / "database/credentials").glob("*.bin")).unlink()
        with self.assertRaisesRegex(bundle.RecoveryError, "credential"):
            self.create()

    def test_rejects_missing_database_and_bad_grant(self):
        DatabaseGrants(self.home / "database/grants.sqlite3").set("alice", [], source_id="absent")
        with self.assertRaisesRegex(bundle.RecoveryError, "grant"):
            self.create()
        (self.home / "database/reports.sqlite3").unlink()
        with self.assertRaisesRegex(bundle.RecoveryError, "four"):
            self.create()

    def test_real_concurrent_commit_retries_entire_generation(self):
        original = bundle._pin_database
        changed = False
        def pin(source):
            nonlocal changed
            original(source)
            if not changed:
                changed = True
                with closing(sqlite3.connect(self.home / "agentbridge.db")) as c, c:
                    c.execute("INSERT INTO agent_tasks VALUES ('t2','bob')")
        with patch.object(bundle, "_pin_database", side_effect=pin):
            backup = self.create()
        self.assertEqual(backup["attempts"], 2)
        self.assertEqual(backup["validation"]["rowCounts"]["agentbridge.db"]["agent_tasks"], 2)

    def test_writes_during_copy_preserve_pinned_generation(self):
        original = bundle._copy_database
        def copy(source, target, deadline):
            original(source, target, deadline)
            with closing(sqlite3.connect(self.home / "agentbridge.db")) as c, c:
                c.execute("UPDATE sessions SET state=state")
                c.execute("INSERT INTO runtime_incidents VALUES (hex(randomblob(8)))")
        with patch.object(bundle, "_copy_database", side_effect=copy):
            backup = self.create(attempts=2)
        self.assertEqual(backup["attempts"], 1)
        self.assertEqual(backup["validation"]["rowCounts"]["agentbridge.db"]["runtime_incidents"], 0)
        with closing(sqlite3.connect(self.home / "agentbridge.db")) as c:
            self.assertGreater(c.execute("SELECT COUNT(*) FROM runtime_incidents").fetchone()[0], 0)

    def test_continuous_acquisition_conflicts_publish_nothing_with_resource_diagnostics(self):
        original = bundle._pin_database
        def pin(source):
            original(source)
            with closing(sqlite3.connect(self.home / "agentbridge.db")) as c, c:
                c.execute("INSERT INTO runtime_incidents VALUES (hex(randomblob(8)))")
        with patch.object(bundle, "_pin_database", side_effect=pin):
            with self.assertRaisesRegex(bundle.RecoveryError, "acquisition: agentbridge.db"):
                self.create(attempts=2)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_file_change_during_snapshot_retries(self):
        original = bundle._copy_database
        changed = False
        def copy(source, target, deadline):
            nonlocal changed
            original(source, target, deadline)
            if not changed:
                changed = True
                ConfigStore(self.home).save_system(SystemProfile("demo", "Changed", "https://example.invalid", ["https://example.invalid"]))
        with patch.object(bundle, "_copy_database", side_effect=copy):
            self.assertEqual(self.create()["attempts"], 2)

    def test_tampered_archive_is_rejected(self):
        backup = self.create()
        manifest = json.loads(Path(backup["manifestPath"]).read_text())
        archive = Path(backup["manifestPath"]).parent / manifest["archiveFile"]
        with archive.open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(bundle.RecoveryError, "hash"):
            bundle.validate_recovery_bundle(backup["manifestPath"], protector=self.protector)

    def test_path_traversal_and_missing_member_fail_even_with_updated_archive_hash(self):
        backup = self.create()
        path = Path(backup["manifestPath"])
        manifest = json.loads(path.read_text())
        archive = path.parent / manifest["archiveFile"]
        with zipfile.ZipFile(archive, "a") as z:
            z.writestr("../escape", b"bad")
        manifest["members"]["../escape"] = {"byteSize": 3, "sha256": "0" * 64}
        manifest["sha256"] = bundle._hash(archive)
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(bundle.RecoveryError, "path"):
            bundle.restore_recovery_bundle(path, self.root / "drills", protector=self.protector)
        self.assertFalse((self.root / "drills/escape").exists())

    def test_legacy_dispatch_validates_v2(self):
        from agentbridge.core.runtime_backup import validate_backup_manifest, run_runtime_restore_drill
        backup = self.create()
        with patch.object(bundle, "_default_protector", return_value=self.protector):
            self.assertTrue(validate_backup_manifest(backup["manifestPath"])["passed"])
            self.assertTrue(run_runtime_restore_drill(backup["manifestPath"], self.root / "drills")["passed"])

    def test_deadline_failure_publishes_nothing(self):
        with self.assertRaisesRegex(bundle.RecoveryError, "deadline"):
            self.create(timeout_seconds=-1)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_unknown_subject_and_cross_user_task_links_rejected(self):
        with closing(sqlite3.connect(self.home / "agentbridge.db")) as c, c:
            c.execute("DELETE FROM mcp_identity_tokens")
        with self.assertRaisesRegex(bundle.RecoveryError, "grant"):
            self.create()
        with closing(sqlite3.connect(self.home / "agentbridge.db")) as c, c:
            c.execute("INSERT INTO mcp_identity_tokens VALUES ('alice')")
            c.execute("ALTER TABLE operations ADD COLUMN user_subject TEXT")
            c.execute("INSERT INTO operations VALUES ('op1','bob')")
            c.execute("CREATE TABLE task_operations (task_id TEXT,operation_id TEXT,user_subject TEXT)")
            c.execute("INSERT INTO task_operations VALUES ('t1','op1','alice')")
        with self.assertRaisesRegex(bundle.RecoveryError, "ownership"):
            self.create()

    def test_missing_member_rejected_without_touching_existing_restore_directory(self):
        backup = self.create()
        path = Path(backup["manifestPath"])
        manifest = json.loads(path.read_text())
        del manifest["members"]["database/grants.sqlite3"]
        path.write_text(json.dumps(manifest))
        drills = self.root / "drills"
        drills.mkdir()
        sentinel = drills / "existing.txt"
        sentinel.write_text("preserved")
        with self.assertRaisesRegex(bundle.RecoveryError, "required database"):
            bundle.restore_recovery_bundle(path, drills, protector=self.protector)
        self.assertEqual(sentinel.read_text(), "preserved")
