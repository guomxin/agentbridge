import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentbridge.core.interactions import (
    InteractionIntegrityError,
    InteractionNotFound,
    InteractionStore,
    build_interaction_envelope,
)


class InteractionStoreTests(unittest.TestCase):
    def test_register_is_idempotent_and_cross_user_reads_are_hidden(self):
        with TemporaryDirectory() as tmp:
            store = InteractionStore(Path(tmp) / "agentbridge.db")
            first = self._register(store)
            second = self._register(store)

            self.assertEqual(first["interaction_id"], second["interaction_id"])
            self.assertEqual(
                store.get(first["interaction_id"], user_subject="user-a")[
                    "resume_spec"
                ]["capability"],
                "oa.business_trip.prepare",
            )
            with self.assertRaises(InteractionNotFound):
                store.get(first["interaction_id"], user_subject="user-b")

    def test_existing_resource_cannot_be_rebound(self):
        with TemporaryDirectory() as tmp:
            store = InteractionStore(Path(tmp) / "agentbridge.db")
            self._register(store)

            with self.assertRaises(InteractionIntegrityError):
                self._register(store, session_id="session-b")

    def test_envelope_projects_resource_state_without_exposing_resource_id(self):
        with TemporaryDirectory() as tmp:
            store = InteractionStore(Path(tmp) / "agentbridge.db")
            record = self._register(store)
            resource = {
                "state": "submitted",
                "card_url": "https://cards.example.test/input/resource-1",
            }

            envelope = build_interaction_envelope(record, resource)

            self.assertEqual(envelope["type"], "business_input")
            self.assertEqual(envelope["state"], "completed")
            self.assertTrue(envelope["resume"]["ready"])
            self.assertFalse(envelope["resume"]["completed"])
            self.assertTrue(
                envelope["presentation"]["modelMustNotCollectValues"]
            )
            self.assertNotIn("resource_id", envelope)
            self.assertNotIn("resume_spec", envelope)

    def test_execution_authorization_requires_host_owned_user_decision(self):
        with TemporaryDirectory() as tmp:
            store = InteractionStore(Path(tmp) / "agentbridge.db")
            record = store.register(
                interaction_type="execution_authorization",
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                operation_id="operation-1",
                resource_id="authorization-1",
                title="确认保存草稿",
                message="请核对并确认。",
                display={"fieldCount": 7},
                resume_spec={
                    "kind": "capability",
                    "capability": "oa.business_trip.save_draft",
                    "arguments": {"authorization_id": "authorization-1"},
                },
                created_at="2026-07-14T00:00:00+00:00",
                expires_at="2026-07-14T00:15:00+00:00",
            )
            resource = {
                "state": "pending",
                "card_url": "https://cards.example.test/authorize/authorization-1",
            }

            envelope = build_interaction_envelope(record, resource)

            self.assertTrue(
                envelope["presentation"]["modelMustNotCollectValues"]
            )

    @staticmethod
    def _register(store, *, session_id="session-a"):
        return store.register(
            interaction_type="business_input",
            user_subject="user-a",
            system_id="oa",
            session_id=session_id,
            operation_id="operation-1",
            resource_id="resource-1",
            title="填写出差申请",
            message="请填写字段。",
            display={"fieldCount": 7},
            resume_spec={
                "kind": "capability",
                "capability": "oa.business_trip.prepare",
                "arguments": {"input_submission_id": "resource-1"},
            },
            created_at="2026-07-14T00:00:00+00:00",
            expires_at="2026-07-14T00:15:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
