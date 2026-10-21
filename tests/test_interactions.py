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

    def test_business_view_hints_are_static_and_fail_closed_for_unknown_contracts(self):
        from copy import deepcopy
        from agentbridge.core.business_draft_profiles import PROFILES
        with TemporaryDirectory() as tmp:
            record = self._register(InteractionStore(Path(tmp) / "agentbridge.db"))
            record["display"]["businessView"] = {"entryLabel": "injected"}
            for profile in PROFILES:
                with self.subTest(profile=profile.profile_id):
                    resource = {"state": "pending", "card_url": "https://cards.example.test/input/source",
                        "capability_name": profile.prepare, "values": {"secret": "private input"},
                        "form_schema": {"_agentbridge_business_draft": "v1",
                            "_agentbridge_business_draft_profile": profile.marker,
                            "schema_version": profile.schema_version,
                            "fields": [{"name": name, "label": "private label"} for name in profile.fields],
                            "review": {"secret": "private evidence"}}}
                    envelope = build_interaction_envelope(record, resource)
                    self.assertEqual(envelope["display"]["businessView"], profile.presentation())
                    self.assertNotIn("private", str(envelope))
                    self.assertEqual(record["display"]["businessView"]["entryLabel"], "injected")
                    for mutate in (
                        lambda r: r.update(capability_name="unknown.prepare"),
                        lambda r: r["form_schema"].update(_agentbridge_business_draft_profile={"id": profile.profile_id, "version": "2"}),
                        lambda r: r["form_schema"].update(schema_version="unknown.v2"),
                        lambda r: r["form_schema"].update(fields=[{"name": "unexpected"}]),
                        lambda r: r["form_schema"].update(fields=None),
                    ):
                        unknown = deepcopy(resource)
                        mutate(unknown)
                        self.assertNotIn("businessView", build_interaction_envelope(record, unknown)["display"])

    def test_action_hints_cover_registered_commits_and_drop_untrusted_reserved_values(self):
        from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS
        with TemporaryDirectory() as tmp:
            record = self._register(InteractionStore(Path(tmp) / "agentbridge.db"))
            record["display"].update(actionView={"schemaVersion": "agentbridge.action-view.v1", "capabilityId": "forged.commit"},
                batch={"schemaVersion": "agentbridge.batch-view.v1", "currentIndex": 99, "totalCount": 99})
            commits = {row["commit_capability"] for row in _TRUSTED_WRITE_DEFINITIONS.values()}
            for capability in [*_TRUSTED_WRITE_DEFINITIONS, *sorted(commits)]:
                with self.subTest(capability=capability):
                    resource = {"state": "pending", "card_url": "https://cards.example.test/input/source",
                        "capability_name": capability, "values": {"secret": "private field"}}
                    envelope = build_interaction_envelope(record, resource)
                    expected = _TRUSTED_WRITE_DEFINITIONS.get(capability, {}).get("commit_capability", capability)
                    self.assertEqual(envelope["display"]["actionView"], {
                        "schemaVersion": "agentbridge.action-view.v1", "capabilityId": expected})
                    self.assertNotIn("batch", envelope["display"])
                    self.assertNotIn("private field", str(envelope))
            resource["capability_name"] = "unregistered.prepare"
            self.assertNotIn("actionView", build_interaction_envelope(record, resource)["display"])
            self.assertEqual(record["display"]["actionView"]["capabilityId"], "forged.commit")

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
