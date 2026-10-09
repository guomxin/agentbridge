import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from agentbridge.core.mcp_identities import McpIdentityTokenStore


class McpIdentityTokenStoreTests(unittest.TestCase):
    def test_renewal_audit_failure_rolls_back_and_late_replay_is_idempotent(self):
        now = [datetime(2026, 9, 23, tzinfo=timezone.utc)]
        with TemporaryDirectory() as tmp:
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db", clock=lambda: now[0])
            token = store.issue(user_subject="a", expected_principal_ref="A", ttl_seconds=300)
            kwargs = dict(new_expires_at=(now[0] + timedelta(days=1)).isoformat(), expected_revision=0,
                          request_id="request", actor="admin", reason="test")
            def fail(*args):
                raise RuntimeError("audit unavailable")
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                store.renew(token["token_id"], **kwargs, audit_callback=fail)
            self.assertEqual(store.get(token["token_id"])["edit_revision"], 0)
            store.renew(token["token_id"], **kwargs)
            now[0] += timedelta(days=2)
            self.assertEqual(store.renew(token["token_id"], **kwargs)["edit_revision"], 1)
            self.assertIsNone(store.verify(token["token"]))

    def test_renew_expired_token_keeps_secret_and_revocation_cannot_be_reversed(self):
        from agentbridge.core.mcp_identities import TokenEditConflict

        now = datetime(2026, 9, 23, tzinfo=timezone.utc)
        clock = [now]
        with TemporaryDirectory() as tmp:
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db", clock=lambda: clock[0])
            issued = store.issue(
                user_subject="user-a", expected_principal_ref="Alice",
                ttl_seconds=300,
            )
            clock[0] = now + timedelta(minutes=6)
            self.assertIsNone(store.verify(issued["token"]))
            new_expiry = (clock[0] + timedelta(days=30)).isoformat()
            renewed = store.renew(
                issued["token_id"], new_expires_at=new_expiry,
                expected_revision=0, request_id="renew-1", actor="admin", reason="scheduled renewal",
            )
            self.assertEqual(renewed["token_id"], issued["token_id"])
            self.assertEqual(renewed["edit_revision"], 1)
            self.assertIsNotNone(store.verify(issued["token"]))
            self.assertEqual(store.renew(
                issued["token_id"], new_expires_at=new_expiry,
                expected_revision=0, request_id="renew-1", actor="admin", reason="scheduled renewal",
            )["edit_revision"], 1)
            with self.assertRaises(TokenEditConflict):
                store.renew(
                    issued["token_id"], new_expires_at=(clock[0] + timedelta(days=40)).isoformat(),
                    expected_revision=0, request_id="renew-2", actor="admin", reason="retry",
                )
            store.revoke(issued["token_id"])
            with self.assertRaises(PermissionError):
                store.renew(
                    issued["token_id"], new_expires_at=(clock[0] + timedelta(days=40)).isoformat(),
                    expected_revision=2, request_id="renew-3", actor="admin", reason="retry",
                )

    def test_stored_retired_scopes_are_neither_displayed_nor_authorized(self):
        import json
        import sqlite3
        from contextlib import closing
        from agentbridge.admin.application import AdminControlPlane

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "agentbridge.db"
            store = McpIdentityTokenStore(path)
            issued = store.issue(user_subject="user-a", expected_principal_ref="Alice",
                                 )
            historical = ["oa:read", "taihua:analytics:read", "taihua:analytics:export", "taihua:read"]
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE mcp_identity_tokens SET scopes_json=? WHERE token_id=?",
                                   (json.dumps(historical), issued["token_id"]))
            expected = ["oa:read", "taihua:read"]
            self.assertEqual(store.get(issued["token_id"])["scopes"], expected)
            self.assertEqual(store.list()[0]["scopes"], expected)
            self.assertIsNone(store.verify(issued["token"], required_scopes={"oa:read"}))
            self.assertEqual(store.resolve_client(issued["token_id"])["scopes"], ["agentbridge:connect"])
            for scope in historical[1:3]:
                self.assertIsNone(store.verify(issued["token"], required_scopes={scope}))
                with self.assertRaises(PermissionError):
                    store.resolve_client(issued["token_id"], required_scopes={scope})
            from types import SimpleNamespace
            admin = SimpleNamespace(identity_store=store)
            self.assertEqual(AdminControlPlane.list_tokens(admin)[0]["scopes"], expected)
            store.revoke(issued["token_id"])
            self.assertEqual(AdminControlPlane.list_tokens(admin)[0]["scopes"], expected)
            with closing(sqlite3.connect(path)) as connection:
                saved = connection.execute("SELECT scopes_json FROM mcp_identity_tokens").fetchone()[0]
            self.assertEqual(json.loads(saved), historical)

    def test_issue_persists_only_hash_and_verifies_identity(self):
        with TemporaryDirectory() as tmp:
            now = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
            db_path = Path(tmp) / "agentbridge.db"
            store = McpIdentityTokenStore(db_path, clock=lambda: now)

            issued = store.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                label="desktop-agent",
                ttl_seconds=3600,
            )
            verified = store.verify(issued["token"], required_scopes={"agentbridge:connect"})

            self.assertTrue(issued["token"].startswith("abmcp_"))
            self.assertEqual(verified["user_subject"], "user-a")
            self.assertEqual(verified["expected_principal_ref"], "Alice")
            self.assertEqual(verified["scopes"], ["agentbridge:connect"])
            self.assertNotIn(issued["token"].encode("utf-8"), db_path.read_bytes())
            self.assertNotIn("token", store.get(issued["token_id"]))

    def test_revoked_and_expired_tokens_fail_verification(self):
        with TemporaryDirectory() as tmp:
            clock = MutableClock(datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc))
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db", clock=clock)
            revoked = store.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                ttl_seconds=3600,
            )
            expired = store.issue(
                user_subject="user-b",
                expected_principal_ref="Bob",
                ttl_seconds=300,
            )

            record = store.revoke(revoked["token_id"])
            clock.value += timedelta(minutes=6)

            self.assertEqual(record["state"], "revoked")
            self.assertIsNone(store.verify(revoked["token"]))
            self.assertIsNone(store.verify(expired["token"]))

    def test_revoking_one_identity_does_not_affect_another_user(self):
        with TemporaryDirectory() as tmp:
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db")
            first = store.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                ttl_seconds=3600,
            )
            second = store.issue(
                user_subject="user-b",
                expected_principal_ref="Bob",
                ttl_seconds=3600,
            )

            store.revoke(first["token_id"])

            self.assertIsNone(store.verify(first["token"]))
            verified = store.verify(second["token"])
            self.assertEqual(verified["user_subject"], "user-b")
            self.assertEqual(store.get(first["token_id"])["state"], "revoked")
            self.assertEqual(store.get(second["token_id"])["state"], "active")
    def test_identity_tokens_are_listed_without_secrets_and_can_be_filtered(self):
        with TemporaryDirectory() as tmp:
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db")
            first = store.issue(
                user_subject="user-a",
                expected_principal_ref="Alice",
                label="phone",
            )
            store.issue(
                user_subject="user-b",
                expected_principal_ref="Bob",
                label="desktop",
            )

            records = store.list(user_subject="user-a")

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["token_id"], first["token_id"])
            self.assertNotIn("token", records[0])




    def test_legacy_business_scopes_are_rejected_at_issuance(self):
        with TemporaryDirectory() as tmp:
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db")
            with self.assertRaisesRegex(ValueError, "no longer grant"):
                store.issue(user_subject="user-a", expected_principal_ref="Alice", scopes=["oa:read"])
            self.assertEqual(store.list(), [])

    def test_all_tokens_follow_user_grants_and_history_never_restores_access(self):
        import sqlite3
        from contextlib import closing
        from tests.authorization_fixtures import grant_permissions
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "agentbridge.db"
            store = McpIdentityTokenStore(path)
            first = store.issue(user_subject="user-a", expected_principal_ref="Alice")
            second = store.issue(user_subject="user-a", expected_principal_ref="Alice")
            other = store.issue(user_subject="user-b", expected_principal_ref="Bob")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE mcp_identity_tokens SET scopes_json = ? WHERE user_subject = ?',
                           ('["oa:read", "oa:write:submit"]', 'user-a'))
            for token in (first, second):
                self.assertIsNone(store.verify(token["token"], required_scopes={"oa:read"}))
                with self.assertRaises(PermissionError):
                    store.resolve_client(token["token_id"], required_scopes={"oa:read"})
            grant_permissions(store.user_grants, "user-a", ["oa.workflow.read"])
            for token in (first, second):
                self.assertIsNotNone(store.verify(token["token"], required_scopes={"oa:read"}))
                self.assertIsNone(store.verify(token["token"], required_scopes={"oa:write:submit"}))
            self.assertIsNone(store.verify(other["token"], required_scopes={"oa:read"}))
            grant_permissions(store.user_grants, "user-a", [])
            for token in (first, second):
                self.assertIsNone(store.verify(token["token"], required_scopes={"oa:read"}))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('DELETE FROM user_business_grants WHERE user_subject = ?', ('user-a',))
            self.assertIsNone(store.verify(first["token"], required_scopes={"oa:read"}))

    def test_unsupported_scope_and_unsafe_subject_are_rejected(self):
        with TemporaryDirectory() as tmp:
            store = McpIdentityTokenStore(Path(tmp) / "agentbridge.db")

            with self.assertRaisesRegex(ValueError, "unsupported scope"):
                store.issue(
                    user_subject="user-a",
                    expected_principal_ref="Alice",
                    scopes=["oa:write"],
                )
            with self.assertRaisesRegex(ValueError, "user_subject is invalid"):
                store.issue(
                    user_subject="user\na",
                    expected_principal_ref="Alice",
                )


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


if __name__ == "__main__":
    unittest.main()
