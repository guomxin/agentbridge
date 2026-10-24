"""Content continuity keys stay narrower than execution and user material."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from agentbridge.core.business_content_identity import (
    has_instance_identity, identity_from_target, principal_binding,
    read_content_identity, validated_identity,
)
from agentbridge.core.business_draft_profiles import BY_PREPARE, PROFILES
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture


class PrivateContentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.profile = BY_PREPARE["oa.leave.approval.prepare"]
        self.target = dict(affair_id="affair-1", summary_id="summary-1", process_id="process-1",
            template_id="template-1", form_app_id="app-1", form_record_id="record-1",
            node_policy="approve", body_type="20", profile="leave", title="private title",
            business_snapshot={"reason": "private reason"}, opinion="private input")

    def test_principal_requires_verified_match_and_system_namespace(self):
        session = dict(system_id="oa", expected_principal_ref="member:1", downstream_principal_ref="member:1")
        binding = principal_binding(session)
        self.assertTrue(binding.startswith("sha256:"))
        self.assertNotIn("member", binding)
        for change in ({"expected_principal_ref": None}, {"downstream_principal_ref": "member:2"}, {"system_id": None}):
            self.assertIsNone(principal_binding({**session, **change}))
        self.assertNotEqual(binding, principal_binding({**session, "system_id": "taihua"}))
        self.assertEqual(binding, principal_binding({**session, "session_id": "new-login"}))

    def test_closed_identity_drops_all_content_and_keeps_node(self):
        marker = identity_from_target(self.profile, self.target)
        self.assertEqual(validated_identity(self.profile, marker), marker)
        self.assertEqual(marker["identity"]["node_policy"], "approve")
        self.assertNotIn("private", repr(marker))
        changed = identity_from_target(self.profile, {**self.target, "node_policy": "inform"})
        self.assertNotEqual(marker, changed)
        self.assertIsNone(validated_identity(self.profile, {**marker, "title": "injected"}))
        extra = deepcopy(marker)
        extra["identity"]["title"] = "injected"
        self.assertIsNone(validated_identity(self.profile, extra))

    def test_missing_or_malformed_existing_instance_is_never_broad_scope(self):
        for key in ("affair_id", "summary_id", "process_id", "node_policy", "body_type"):
            with self.subTest(key=key):
                missing = {k: v for k, v in self.target.items() if k != key}
                self.assertIsNone(identity_from_target(self.profile, missing))
        for value in (None, "", True, {}, "a\nb", "x" * 257):
            with self.subTest(value=value):
                self.assertIsNone(identity_from_target(self.profile, {**self.target, "affair_id": value}))
        self.assertIsNone(identity_from_target(self.profile, {**self.target, "profile": "overtime"}))

    def test_creation_profiles_cannot_reuse_template_as_business_instance(self):
        creations = [p for p in PROFILES if p.action_kind in {"create", "save_draft", "submit"}]
        self.assertEqual(len(PROFILES), 29)
        self.assertEqual(len(creations), 8)
        for profile in creations:
            with self.subTest(profile=profile.profile_id):
                self.assertFalse(has_instance_identity(profile))
                self.assertIsNone(read_content_identity(None, None, profile=profile, arguments={"template_id": "same"}))

    def test_approval_reuses_preflight_without_second_material_read(self):
        with patch("agentbridge.core.write_catalog.resolve_write_function", side_effect=AssertionError("duplicate read")):
            actual = read_content_identity(None, None, profile=self.profile, arguments={"affair_id": "affair-1"},
                preflight_result={"_content_target": self.target})
        self.assertEqual(actual, identity_from_target(self.profile, self.target))

    def test_fresh_approval_read_requires_registered_preflight_and_original_target(self):
        preflight = Mock(return_value={"_content_target": self.target, "review": {"completeness": {"status": "complete"}}})
        with patch("agentbridge.core.write_catalog.resolve_write_function", return_value=preflight):
            marker = read_content_identity("adapter", "worker", profile=self.profile, arguments={"affair_id": "affair-1"})
        preflight.assert_called_once_with("adapter", "worker", {"affair_id": "affair-1"}, "leave")
        self.assertEqual(marker, identity_from_target(self.profile, self.target))

    def test_matching_instance_with_incomplete_materials_cannot_restore(self):
        from agentbridge.core.business_drafts import BusinessDraftError
        for review in (None, {"truncated": True}, {"completeness": {"status": "partial"}}):
            preflight = Mock(return_value={"_content_target": self.target, "review": review})
            with self.subTest(review=review), patch("agentbridge.core.write_catalog.resolve_write_function", return_value=preflight):
                with self.assertRaises(BusinessDraftError) as caught:
                    read_content_identity(None, None, profile=self.profile, arguments={"affair_id": "affair-1"})
                self.assertEqual(caught.exception.code, "DRAFT_MATERIALS_INCOMPLETE")

    def test_alarm_read_uses_target_id_only_without_synthetic_remark(self):
        profile = BY_PREPARE["smartlight.alarm.remark.update.prepare"]
        adapter = Mock()
        adapter.alarm_remark_snapshot.return_value = {"alarmId": "alarm-1", "rtuId": "rtu-1", "deviceCode": "lamp-1", "remark": "private"}
        marker = read_content_identity(adapter, "worker", profile=profile, arguments={"alarm_id": " alarm-1 "})
        adapter.alarm_remark_snapshot.assert_called_once_with("worker", "alarm-1")
        self.assertNotIn("remark", marker["identity"])
        self.assertNotIn("private", repr(marker))


class PrivateContentSourceIntegrationTests(unittest.TestCase):
    def test_new_source_freezes_verified_principal_and_exact_instance(self):
        with TemporaryDirectory() as directory:
            fixture = BusinessViewAcceptanceFixture(Path(directory))
            try:
                case = next(case for case in fixture.meta["view_cases"] if case["profile"] == "leave")
                source = fixture.service.field_submissions.get(case["input_id"])
                schema = source["form_schema"]
                session = fixture.service.sessions.get(source["session_id"])
                self.assertEqual(schema["_agentbridge_content_principal"], principal_binding(session))
                identity = schema["_agentbridge_content_identity"]
                self.assertEqual(validated_identity(BY_PREPARE["oa.leave.approval.prepare"], identity), identity)
                self.assertEqual(identity["identity"]["affair_id"], case["arguments"]["affair_id"])
                room = next(case for case in fixture.meta["view_cases"] if case["profile"] == "meeting_room")
                room_schema = fixture.service.field_submissions.get(room["input_id"])["form_schema"]
                self.assertNotIn("_agentbridge_content_identity", room_schema)
            finally:
                fixture.close()
