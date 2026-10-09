import unittest
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from agentbridge.core.capability import CapabilityRegistry, CapabilitySpec
from agentbridge.core.capability_runtime import (
    CapabilityEngine,
    CapabilityRejected,
    RequiresUserAction,
)
from agentbridge.core.operations import OperationConflictError, OperationStore
from agentbridge.core.session_secrets import SessionStateStore
from agentbridge.core.sessions import SessionPrincipalMismatch, SessionRegistry


class AgentBridgeCoreTests(unittest.TestCase):
    def test_capability_registry_exposes_versioned_business_capability(self):
        registry = CapabilityRegistry()
        spec = CapabilitySpec(
            name="oa.template.list",
            version="0.1.0",
            description="List templates available to the current OA user.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            effect="read",
            adapter="seeyon-central",
            workflow="template-list-v1",
        )

        registry.register(spec)

        self.assertEqual(registry.get("oa.template.list"), spec)
        self.assertEqual(registry.list()[0].system, "oa")
        self.assertEqual(registry.describe("oa.template.list")["effect"], "read")

    def test_operation_store_reuses_idempotency_key_and_rejects_changed_input(self):
        with TemporaryDirectory() as tmp:
            store = OperationStore(Path(tmp) / "agentbridge.db")
            first, reused = store.create(
                user_subject="user-a",
                capability_name="oa.template.list",
                capability_version="0.1.0",
                input_summary={"keyword": "HR"},
                idempotency_key="same-request",
            )
            second, second_reused = store.create(
                user_subject="user-a",
                capability_name="oa.template.list",
                capability_version="0.1.0",
                input_summary={"keyword": "HR"},
                idempotency_key="same-request",
            )

            self.assertFalse(reused)
            self.assertTrue(second_reused)
            self.assertEqual(second["operation_id"], first["operation_id"])

            with self.assertRaises(OperationConflictError):
                store.create(
                    user_subject="user-a",
                    capability_name="oa.template.list",
                    capability_version="0.1.0",
                    input_summary={"keyword": "Finance"},
                    idempotency_key="same-request",
                )

            blank_one, blank_one_reused = store.create(
                user_subject="user-a",
                capability_name="oa.template.list",
                capability_version="0.1.0",
                input_summary={},
                idempotency_key="   ",
            )
            blank_two, blank_two_reused = store.create(
                user_subject="user-a",
                capability_name="oa.template.list",
                capability_version="0.1.0",
                input_summary={},
                idempotency_key="   ",
            )
            self.assertFalse(blank_one_reused)
            self.assertFalse(blank_two_reused)
            self.assertNotEqual(blank_one["operation_id"], blank_two["operation_id"])

    def test_operation_store_hashes_exact_write_input_but_persists_only_summary(self):
        with TemporaryDirectory() as tmp:
            store = OperationStore(Path(tmp) / "agentbridge.db")
            summary = {"reason": {"redacted": True, "length": 12}}
            created, _ = store.create(
                user_subject="user-a",
                capability_name="oa.business_trip.prepare",
                capability_version="0.1.0",
                input_summary=summary,
                input_identity={"reason": "private text"},
                idempotency_key="write-redaction",
            )

            self.assertEqual(created["input_summary"], summary)
            self.assertNotIn("private text", (Path(tmp) / "agentbridge.db").read_bytes().decode("utf-8", errors="ignore"))
            with self.assertRaises(OperationConflictError):
                store.create(
                    user_subject="user-a",
                    capability_name="oa.business_trip.prepare",
                    capability_version="0.1.0",
                    input_summary=summary,
                    input_identity={"reason": "changed text"},
                    idempotency_key="write-redaction",
                )

    def test_capability_engine_persists_success_and_reuses_result(self):
        with TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register(_template_capability())
            store = OperationStore(Path(tmp) / "agentbridge.db")
            calls = []

            def handler(_context, arguments):
                calls.append(arguments)
                return {"count": 1, "items": [{"title": "HR"}]}

            engine = CapabilityEngine(registry=registry, operation_store=store)
            engine.register_handler("oa.template.list", handler)

            first = engine.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
                idempotency_key="list-once",
            )
            second = engine.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
                idempotency_key="list-once",
            )

            self.assertEqual(first["status"], "succeeded")
            self.assertEqual(second["operationId"], first["operationId"])
            self.assertTrue(second["reused"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(store.get(first["operationId"])["status"], "succeeded")

    def test_capability_engine_accepts_json_schema_union_types(self):
        with TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register(
                CapabilitySpec(
                    name="smartlight.inspection_task.list",
                    version="0.1.0",
                    description="List inspection tasks.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "state": {"type": ["string", "integer", "null"]},
                        },
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    effect="read",
                    adapter="smartlight-central",
                    workflow="inspection-list-v1",
                )
            )
            store = OperationStore(Path(tmp) / "agentbridge.db")
            engine = CapabilityEngine(registry=registry, operation_store=store)
            engine.register_handler(
                "smartlight.inspection_task.list",
                lambda _context, arguments: {"state": arguments.get("state")},
            )

            for state in (2, "2", None):
                response = engine.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.inspection_task.list",
                    arguments={"state": state},
                )
                self.assertEqual(response["status"], "succeeded")
                self.assertEqual(response["result"]["state"], state)

            with self.assertRaisesRegex(ValueError, "must be"):
                engine.invoke(
                    user_subject="user-a",
                    capability_name="smartlight.inspection_task.list",
                    arguments={"state": []},
                )

    def test_capability_engine_records_requires_user_action(self):
        with TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register(_template_capability())
            store = OperationStore(Path(tmp) / "agentbridge.db")
            engine = CapabilityEngine(registry=registry, operation_store=store)

            def handler(_context, _arguments):
                raise RequiresUserAction(
                    "LOGIN_REQUIRED",
                    "The OA session is not active.",
                    next_action={"type": "session_login", "system": "oa"},
                )

            engine.register_handler("oa.template.list", handler)
            response = engine.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
            )

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(response["error"]["code"], "LOGIN_REQUIRED")
            self.assertEqual(response["nextAction"]["type"], "session_login")

    def test_capability_engine_preserves_controlled_failure_details(self):
        with TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register(_template_capability())
            store = OperationStore(Path(tmp) / "agentbridge.db")
            engine = CapabilityEngine(registry=registry, operation_store=store)

            def handler(_context, _arguments):
                raise CapabilityRejected(
                    "OA_BUSINESS_RULE_REJECTED",
                    "The selected interval is not eligible for this request.",
                )

            engine.register_handler("oa.template.list", handler)
            response = engine.invoke(
                user_subject="user-a",
                capability_name="oa.template.list",
                arguments={},
            )

            self.assertEqual(response["status"], "failed")
            self.assertEqual(response["error"]["code"], "OA_BUSINESS_RULE_REJECTED")
            self.assertIn("not eligible", response["error"]["message"])
            operation = store.get(response["operationId"])
            self.assertEqual(operation["status"], "failed")
            self.assertEqual(operation["error_code"], "OA_BUSINESS_RULE_REJECTED")

    def test_session_registry_isolates_profiles_and_checks_principal(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = SessionRegistry(root / "agentbridge.db", root / "profiles")
            user_a = registry.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Alice",
            )
            user_b = registry.get_or_create(
                user_subject="user-b",
                system_id="oa",
                expected_principal_ref="Bob",
            )

            self.assertNotEqual(user_a["profile_path"], user_b["profile_path"])
            self.assertFalse(Path(user_a["profile_path"]).exists())
            active = registry.activate(user_a["session_id"], observed_principal_ref="Alice")
            self.assertEqual(active["state"], "active")

            with self.assertRaises(SessionPrincipalMismatch):
                registry.activate(user_b["session_id"], observed_principal_ref="Mallory")
            self.assertEqual(registry.get(user_b["session_id"])["state"], "quarantined")

    def test_session_registry_resolves_distinct_principals_per_system(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = SessionRegistry(root / "agentbridge.db", root / "profiles")
            resolved = registry.ensure_principal_bindings(
                user_subject="user-a",
                system_ids={"oa", "taihua", "yuque"},
                principal_bindings={
                    "oa": "Alice OA",
                    "taihua": "alice.worklog",
                    "yuque": "alice-docs",
                },
            )

            self.assertEqual(
                resolved,
                {
                    "oa": "Alice OA",
                    "taihua": "alice.worklog",
                    "yuque": "alice-docs",
                },
            )
            for system_id, principal_ref in resolved.items():
                session = registry.find(user_subject="user-a", system_id=system_id)
                self.assertEqual(session["expected_principal_ref"], principal_ref)
                active = registry.activate(
                    session["session_id"],
                    observed_principal_ref=principal_ref,
                )
                self.assertEqual(active["state"], "active")

            reused = registry.ensure_principal_bindings(
                user_subject="user-a",
                system_ids={"oa", "taihua", "yuque"},
            )
            self.assertEqual(reused, resolved)
            with self.assertRaises(SessionPrincipalMismatch):
                registry.ensure_principal_bindings(
                    user_subject="user-a",
                    system_ids={"taihua"},
                    principal_bindings={"taihua": "different-account"},
                )
            self.assertEqual(
                registry.find(user_subject="user-a", system_id="taihua")["state"],
                "active",
            )

    def test_session_registry_rebind_expires_only_the_selected_system(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = SessionRegistry(root / "agentbridge.db", root / "profiles")
            oa = registry.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Alice OA",
            )
            taihua = registry.get_or_create(
                user_subject="user-a",
                system_id="taihua",
                expected_principal_ref="alice.worklog",
            )
            registry.activate(oa["session_id"], observed_principal_ref="Alice OA")
            registry.activate(
                taihua["session_id"],
                observed_principal_ref="alice.worklog",
            )

            rebound = registry.rebind_expected_principal(
                taihua["session_id"],
                expected_principal_ref="alice-new",
                reason="account renamed",
            )

            self.assertEqual(rebound["expected_principal_ref"], "alice-new")
            self.assertIsNone(rebound["downstream_principal_ref"])
            self.assertEqual(rebound["state"], "expired")
            self.assertIsNone(rebound["last_verified_at"])
            self.assertEqual(registry.get(oa["session_id"])["state"], "active")

    def test_session_registry_rejects_binding_changes_and_unverified_activation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = SessionRegistry(root / "agentbridge.db", root / "profiles")
            session = registry.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Alice",
            )

            with self.assertRaises(SessionPrincipalMismatch):
                registry.get_or_create(
                    user_subject="user-a",
                    system_id="oa",
                    expected_principal_ref="Bob",
                )
            self.assertEqual(registry.get(session["session_id"])["state"], "quarantined")

            unverified = registry.get_or_create(
                user_subject="user-b",
                system_id="oa",
                expected_principal_ref="Bob",
            )
            with self.assertRaises(SessionPrincipalMismatch):
                registry.activate(unverified["session_id"], observed_principal_ref=None)
            self.assertEqual(registry.get(unverified["session_id"])["state"], "quarantined")

    def test_session_registry_lists_active_sessions_and_records_activity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = SessionRegistry(root / "agentbridge.db", root / "profiles")
            active = registry.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Alice",
            )
            active = registry.activate(
                active["session_id"],
                observed_principal_ref="Alice",
            )
            registry.get_or_create(
                user_subject="user-b",
                system_id="oa",
                expected_principal_ref="Bob",
            )

            time.sleep(0.01)
            touched = registry.touch_activity(active["session_id"])

            self.assertGreater(touched["updated_at"], active["updated_at"])
            self.assertEqual(touched["last_user_activity_at"], touched["updated_at"])
            keepalive = registry.record_keepalive(active["session_id"])
            self.assertEqual(keepalive["updated_at"], touched["updated_at"])
            self.assertEqual(
                keepalive["last_user_activity_at"],
                touched["last_user_activity_at"],
            )
            self.assertIsNotNone(keepalive["last_keepalive_at"])
            self.assertEqual(
                [session["session_id"] for session in registry.list_active(system_id="oa")],
                [active["session_id"]],
            )
            expired = registry.mark_expired(active["session_id"])
            self.assertIsNotNone(expired["expired_at"])
            self.assertEqual(
                expired["last_user_activity_at"],
                touched["last_user_activity_at"],
            )
            with self.assertRaisesRegex(ValueError, "active session"):
                registry.touch_activity(active["session_id"])

    def test_session_registry_preserves_state_history_after_reauthentication(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = SessionRegistry(root / "agentbridge.db", root / "profiles")
            session = registry.get_or_create(
                user_subject="user-a",
                system_id="smartlight",
                expected_principal_ref="Alice",
            )
            registry.activate(
                session["session_id"],
                observed_principal_ref="Alice",
                source="credential_login",
            )
            registry.mark_expired(
                session["session_id"],
                "CAS renewal failed after application session expired.",
                source="keepalive",
            )
            registry.activate(
                session["session_id"],
                observed_principal_ref="Alice",
                source="credential_login",
            )

            events = registry.list_events(session_id=session["session_id"])

            self.assertEqual(events[0]["source"], "credential_login")
            self.assertEqual(events[0]["previous_state"], "expired")
            keepalive_failure = next(
                item
                for item in events
                if item["source"] == "keepalive" and item["new_state"] == "expired"
            )
            self.assertIn("CAS renewal failed", keepalive_failure["reason"])
            self.assertIsNone(registry.get(session["session_id"])["last_error"])

    def test_session_registry_migrates_legacy_activity_timestamps(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "agentbridge.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE sessions (
                        session_id TEXT PRIMARY KEY,
                        user_subject TEXT NOT NULL,
                        system_id TEXT NOT NULL,
                        expected_principal_ref TEXT,
                        downstream_principal_ref TEXT,
                        profile_path TEXT NOT NULL,
                        state TEXT NOT NULL,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_verified_at TEXT,
                        UNIQUE (user_subject, system_id)
                    );
                    INSERT INTO sessions VALUES (
                        'active-session', 'user-a', 'oa', 'Alice', 'Alice',
                        'profile-a', 'active', NULL,
                        '2026-07-20T00:00:00+00:00',
                        '2026-07-24T06:00:00+00:00',
                        '2026-07-20T00:00:00+00:00'
                    );
                    INSERT INTO sessions VALUES (
                        'expired-session', 'user-b', 'oa', 'Bob', 'Bob',
                        'profile-b', 'expired', 'OA expired',
                        '2026-07-20T00:00:00+00:00',
                        '2026-07-26T03:51:40+00:00',
                        '2026-07-23T12:54:52+00:00'
                    );
                    """
                )
            finally:
                connection.close()

            registry = SessionRegistry(db_path, root / "profiles")

            active = registry.get("active-session")
            expired = registry.get("expired-session")
            self.assertEqual(
                active["last_user_activity_at"],
                "2026-07-24T06:00:00+00:00",
            )
            self.assertIsNone(active["last_keepalive_at"])
            self.assertIsNone(active["expired_at"])
            self.assertIsNone(expired["last_user_activity_at"])
            self.assertEqual(expired["expired_at"], "2026-07-26T03:51:40+00:00")

    def test_session_state_store_encrypts_cookie_state_at_rest(self):
        with TemporaryDirectory() as tmp:
            store = SessionStateStore(Path(tmp), protector=ReversingProtector())
            state = {
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": "top-secret-cookie",
                        "domain": "oa.example.test",
                        "path": "/",
                    }
                ]
            }

            store.save("session-a", state)

            ciphertext = store.path_for("session-a").read_bytes()
            self.assertNotIn(b"top-secret-cookie", ciphertext)
            self.assertEqual(store.load("session-a"), state)



def _template_capability() -> CapabilitySpec:
    return CapabilitySpec(
        name="oa.template.list",
        version="0.1.0",
        description="List templates available to the current OA user.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        output_schema={"type": "object"},
        effect="read",
        adapter="seeyon-central",
        workflow="template-list-v1",
    )


class ReversingProtector:
    def protect(self, plaintext: bytes, *, context: bytes) -> bytes:
        return b"protected:" + context + b":" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, context: bytes) -> bytes:
        prefix = b"protected:" + context + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("invalid context")
        return ciphertext[len(prefix) :][::-1]


if __name__ == "__main__":
    unittest.main()
