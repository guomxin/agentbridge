import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from agentbridge.core.tasks import TaskHubStore
from agentbridge.workspace.host_instances import (
    LEGACY_INSTANCE_ID,
    HostInstanceConflict,
    WorkspaceHostInstances,
)
from agentbridge.workspace.stores import WorkspaceStore


class WorkspaceHostInstanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "central.sqlite"
        self.workspace = WorkspaceStore(self.path)
        self.tasks = TaskHubStore(self.path, maintain_on_startup=False)
        self.add_account("old", legacy=True)
        self.directory = WorkspaceHostInstances(self.path)

    def add_account(self, name, *, legacy=False):
        with self.workspace._connect() as db:
            db.execute(
                """INSERT INTO workspace_accounts
                (account_id, username, password_hash, user_subject, endpoint_key, openclaw_session_key,
                 state, created_at, updated_at) VALUES (?, ?, 'unused', ?, ?, ?, 'active', 't', 't')""",
                (
                    name,
                    name,
                    f"subject-{name}",
                    f"old-web-{name}",
                    f"old-session-{name}",
                ),
            )
        if legacy:
            endpoint = self.endpoint(
                name, "openclaw", key=f"old-web-{name}", session=f"old-session-{name}"
            )
            self.workspace.attach_endpoint(account_id=name, endpoint_id=endpoint)

    def endpoint(self, name, host, *, key, session, client_type="web", external=None):
        endpoint, _ = self.tasks.ensure_endpoint(
            user_subject=f"subject-{name}",
            agent_host=host,
            account_id=name,
            token_id="test-account",
            client_type=client_type,
            external_subject=external or name,
            endpoint_key=key,
            conversation_ref=session,
        )
        return endpoint["endpoint_id"]

    def save(
        self,
        instance="office",
        subjects=("subject-old",),
        state="enabled",
        revision=0,
        **changes,
    ):
        args = dict(
            host_type="nanobot",
            display_name=instance,
            route_ref=f"route-{instance}",
            adapter_version="0.4.2",
            state=state,
            subjects=subjects,
            expected_revision=revision,
            actor="admin",
            reason="synthetic test",
        )
        args.update(changes)
        return self.directory.save_instance(instance, **args)

    def bind(self, name="old", instance="office", session=None):
        endpoint = self.endpoint(
            name,
            "nanobot",
            key=f"{name}-{instance}",
            session=session or f"{name}-session-{instance}",
        )
        self.directory.bind(
            name,
            instance,
            endpoint_id=endpoint,
            credential_ref=f"cred-{name}-{instance}",
        )
        return endpoint

    def resolve(self, user="old", **kwargs):
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.directory.resolve(db, user, **kwargs)

    def test_legacy_migration_retains_original_web_endpoint_and_session_exactly_once(
        self,
    ):
        route = self.resolve(expected_revision=1)
        self.assertEqual(route["instance_id"], LEGACY_INSTANCE_ID)
        self.assertEqual(route["session_key"], "old-session-old")
        self.assertEqual(route["endpoint_key"], "old-web-old")
        self.assertEqual(
            route["endpoint_id"], self.workspace.get_account("old")["endpoint_id"]
        )
        self.save()
        self.bind()
        result = self.directory.set_default("old", "office", expected_revision=1)
        self.assertEqual(result["revision"], 2)
        self.directory = WorkspaceHostInstances(self.path)
        self.assertEqual(self.resolve()["instance_id"], "office")
        self.assertEqual(
            self.resolve(existing=True, instance_id=LEGACY_INSTANCE_ID), route
        )

    def test_account_scope_public_projection_and_provisioned_endpoint(self):
        self.add_account("other", legacy=True)
        self.save()
        choices = self.directory.choices("old")
        office = next(row for row in choices["items"] if row["instance_id"] == "office")
        self.assertFalse(office["selectable"])
        with self.assertRaises(PermissionError):
            self.directory.set_default(
                "old", "office", expected_revision=choices["revision"]
            )
        endpoint = self.endpoint(
            "other", "nanobot", key="other-nb", session="other-session"
        )
        with self.assertRaises(PermissionError):
            self.directory.bind(
                "old", "office", endpoint_id=endpoint, credential_ref="cred-old"
            )
        with self.assertRaises(PermissionError):
            self.directory.bind(
                "other", "office", endpoint_id=endpoint, credential_ref="cred-other"
            )
        private = self.directory.admin_list()
        self.assertEqual(
            next(item for item in private if item["instance_id"] == "office")[
                "subjects"
            ],
            ["subject-old"],
        )
        self.bind()
        public = json.dumps(self.directory.choices("old"))
        for private_value in (
            "subject-old",
            "cred-old-office",
            "route-office",
            "session_key",
            "endpoint_key",
        ):
            self.assertNotIn(private_value, public)
        self.assertNotIn("office", json.dumps(self.directory.choices("other")))
        with self.assertRaises(PermissionError):
            self.resolve("other", instance_id="office", existing=True)

    def test_initial_selection_only_for_one_bound_instance_and_atomic_rollback(self):
        self.add_account("fresh")
        self.save(subjects=None)
        self.save("second", subjects=None)
        self.assertEqual(self.directory.choices("fresh")["revision"], 0)
        self.bind("fresh")
        with self.workspace._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(
                self.directory.resolve(db, "fresh")["instance_id"], "office"
            )
            db.rollback()
        self.bind("fresh", "second")
        result = self.directory.choices("fresh")
        self.assertIsNone(result["default_instance_id"])
        self.assertEqual(result["revision"], 0)
        with self.assertRaises(PermissionError):
            self.resolve("fresh")
        first = self.directory.set_default("fresh", "second", expected_revision=0)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(self.resolve("fresh")["route_ref"], "route-second")
        self.directory.set_default("fresh", "office", expected_revision=1)
        self.assertEqual(self.resolve("fresh")["session_key"], "fresh-session-office")
        self.add_account("only")
        self.bind("only")
        self.assertEqual(
            self.directory.choices("only")["default_instance_id"], "office"
        )
        self.bind("only", "second")
        self.assertEqual(
            self.directory.choices("only")["default_instance_id"], "office"
        )

    def test_draining_existing_only_then_security_revocation_no_silent_fallback(self):
        self.save()
        self.bind()
        self.directory.set_default("old", "office", expected_revision=1)
        self.save(state="draining", revision=1)
        self.assertTrue(self.directory.choices("old")["default_unavailable"])
        self.assertEqual(
            self.resolve(instance_id="office", existing=True)["credential_ref"],
            "cred-old-office",
        )
        with self.assertRaises(PermissionError):
            self.resolve()
        self.save(state="revoked", revision=2)
        self.assertNotIn("office", json.dumps(self.directory.choices("old")))
        with self.assertRaises(PermissionError):
            self.resolve(instance_id="office", existing=True)
        self.save(subjects=[], revision=3)
        with self.assertRaises(PermissionError):
            self.resolve(instance_id="office", existing=True)
        with self.assertRaises(ValueError):
            self.resolve(existing=True)

    def test_scope_revocation_and_endpoint_changes_rechecked_in_caller_transaction(
        self,
    ):
        self.save()
        endpoint = self.bind()
        self.directory.set_default("old", "office", expected_revision=1)
        self.save(subjects=[], revision=1)
        with self.assertRaises(PermissionError):
            self.resolve(existing=True, instance_id="office")
        self.save(revision=2)
        with self.workspace._connect() as db:
            db.execute(
                "UPDATE client_endpoints SET conversation_ref='replaced' WHERE endpoint_id=?",
                (endpoint,),
            )
        with self.assertRaises(PermissionError):
            self.resolve()
        new_endpoint = self.endpoint(
            "old", "nanobot", key="replacement", session="second-session"
        )
        with self.assertRaises(ValueError):
            self.directory.bind(
                "old", "office", endpoint_id=new_endpoint, credential_ref="cred-old"
            )

    def test_admin_revisions_route_identity_and_audit(self):
        self.save()
        with self.assertRaises(HostInstanceConflict):
            self.save()
        for change in ({"host_type": "openclaw"}, {"route_ref": "changed"}):
            with self.assertRaises(ValueError):
                self.save(revision=1, **change)
        with self.assertRaises(ValueError):
            self.save("second", route_ref="route-office")
        with self.assertRaises(ValueError):
            self.save("second", route_ref="https://untrusted.test")
        self.save(revision=1, display_name="办公助手")
        with sqlite3.connect(self.path) as db:
            audit = db.execute(
                "SELECT revision, actor, reason FROM workspace_host_directory_audit "
                "WHERE instance_id='office' ORDER BY revision"
            ).fetchall()
        self.assertEqual(
            audit, [(1, "admin", "synthetic test"), (2, "admin", "synthetic test")]
        )

    def test_stale_tabs_concurrent_changes_only_one_wins(self):
        for instance in ("one", "two"):
            self.save(instance)
            self.bind(instance=instance)
        barrier = Barrier(2)

        def change(target):
            barrier.wait(timeout=5)
            try:
                return self.directory.set_default("old", target, expected_revision=1)[
                    "default_instance_id"
                ]
            except HostInstanceConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change, ("one", "two")))
        selected = self.directory.choices("old")
        self.assertCountEqual(results, [None, selected["default_instance_id"]])
        self.assertEqual(selected["revision"], 2)
        with self.assertRaises(HostInstanceConflict):
            self.resolve(expected_revision=1)
        with self.assertRaises(HostInstanceConflict):
            self.resolve(instance_id=LEGACY_INSTANCE_ID, expected_revision=2)

    def test_duplicate_endpoint_or_private_ref_and_nonweb_endpoint_denied(self):
        self.add_account("other")
        self.save(subjects=None)
        self.save("second", subjects=None)
        endpoint = self.bind()
        with self.assertRaises(ValueError):
            self.directory.bind(
                "old", "second", endpoint_id=endpoint, credential_ref="cred-old-second"
            )
        other = self.endpoint("other", "nanobot", key="other", session="s")
        with self.assertRaises(ValueError):
            self.directory.bind(
                "other", "office", endpoint_id=other, credential_ref="cred-old-office"
            )
        for options in (dict(client_type="telegram"), dict(external="other")):
            untrusted = self.endpoint(
                "old", "nanobot", key="mismatch", session="bad", **options
            )
            with self.assertRaises(PermissionError):
                self.directory.bind(
                    "old", "second", endpoint_id=untrusted, credential_ref="cred-second"
                )
        with self.workspace._connect() as db, self.assertRaises(RuntimeError):
            self.directory.resolve(db, "old")


if __name__ == "__main__":
    unittest.main()
