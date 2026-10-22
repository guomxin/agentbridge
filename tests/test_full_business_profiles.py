"""Closed action coverage, real schema validation, and review binding contracts."""
from copy import deepcopy
import unittest

from agentbridge.core.business_draft_profiles import (
    BY_PREPARE, PROFILES, BATCH_PREPARES, profile_for_source, profile_for_definition, review_payload,
)
from agentbridge.core.business_drafts import BusinessDraftService, BusinessDraftError
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS, _TRUSTED_WRITE_COMMITS


class FullBusinessProfileTests(unittest.TestCase):
    def source(self, capability):
        profile = BY_PREPARE[capability]
        schema = deepcopy(_TRUSTED_WRITE_DEFINITIONS[capability]["field_schema"])
        schema.update(_agentbridge_business_draft="v1", _agentbridge_business_draft_profile=profile.marker)
        return {"capability_name": capability, "form_schema": schema}

    def values(self, schema):
        values = {}
        for field in schema["fields"]:
            name, control = field["name"], field["control"]
            values[name] = ("2026-10-20" if control == "date" else
                "2026-10-20T09:00" if control == "datetime-local" and name == "start_time" else
                "2026-10-20T10:00" if control == "datetime-local" else
                str(field["options"][0]["value"]) if control in {"select", "segmented"} else
                "2" if control == "number" else "合成核对内容")
        return values

    def test_complete_current_catalog_is_explicitly_partitioned(self):
        self.assertEqual(len(_TRUSTED_WRITE_DEFINITIONS), 34)
        self.assertEqual(len(_TRUSTED_WRITE_COMMITS), 32)
        field_names = {name for name, definition in _TRUSTED_WRITE_DEFINITIONS.items()
                       if definition.get("field_schema") and name not in BATCH_PREPARES}
        self.assertEqual(set(BY_PREPARE), field_names)
        self.assertEqual(len(PROFILES), 29)
        self.assertEqual({name for name, definition in _TRUSTED_WRITE_DEFINITIONS.items() if not definition.get("field_schema")}, {
            "smartlight.alarm.work_area.submit.prepare", "smartlight.alarm.work_area.revoke.prepare", "smartlight.alarm.dispose.prepare"})
        for prepare in field_names:
            with self.subTest(prepare=prepare):
                profile = profile_for_source(self.source(prepare))
                self.assertEqual(profile.commit, _TRUSTED_WRITE_DEFINITIONS[prepare]["commit_capability"])
                self.assertTrue(profile.submit_label)
                self.assertTrue(profile.effect)
                self.assertEqual(profile.version, "1")

    def test_all_29_field_contracts_normalize_and_reject_required_omissions(self):
        for profile in PROFILES:
            source = self.source(profile.prepare)
            raw = self.values(source["form_schema"])
            with self.subTest(profile=profile.profile_id):
                normalized = BusinessDraftService._schema_values(source, raw)
                self.assertEqual(set(normalized), set(profile.fields))
                for field in source["form_schema"]["fields"]:
                    name = field["name"]
                    if field.get("value_type") == "boolean":
                        self.assertIs(normalized[name], True)
                    if field["control"] in {"select", "segmented"}:
                        with self.assertRaises(BusinessDraftError) as caught:
                            BusinessDraftService._schema_values(source, {**raw, name: "invented-option"})
                        self.assertIn(name, caught.exception.field_errors)
                    if field.get("required"):
                        with self.assertRaises(BusinessDraftError) as caught:
                            BusinessDraftService._schema_values(source, {**raw, name: ""})
                        self.assertIn(name, caught.exception.field_errors)
                    if field.get("max_length"):
                        with self.assertRaises(BusinessDraftError):
                            BusinessDraftService._schema_values(source, {**raw, name: "字" * (field["max_length"] + 1)})

    def test_optional_clear_remark_is_preserved_and_false_is_not_missing(self):
        source = self.source("smartlight.alarm.remark.update.prepare")
        self.assertEqual(BusinessDraftService._schema_values(source, {"remark": "  "}), {"remark": ""})
        source = self.source("oa.leave.submit.prepare")
        raw = self.values(source["form_schema"])
        raw["has_direct_supervisor"] = "false"
        self.assertIs(BusinessDraftService._schema_values(source, raw)["has_direct_supervisor"], False)
        with self.assertRaises(BusinessDraftError):
            BusinessDraftService._schema_values(source, {**raw, "end_time": raw["start_time"]})

    def test_confirmation_and_notifications_never_claim_approval_or_add_readonly_fields(self):
        for prepare, action, label in (("oa.attendance_confirmation.prepare", "confirmation", "确认并提交考勤单"),
                                      ("oa.weekly_report.acknowledgement.prepare", "acknowledgement", "确认并阅办知会"),
                                      ("oa.intellectual_property_reward.acknowledgement.prepare", "acknowledgement", "确认并阅办知会")):
            profile = BY_PREPARE[prepare]
            ui = profile.ui(self.source(prepare)["form_schema"])
            self.assertEqual(ui["actionKind"], action)
            self.assertEqual(ui["submitLabel"], label)
            self.assertEqual([field["name"] for field in ui["fields"]], ["opinion"])
            self.assertNotIn("审批通过", str(ui))

    def test_ui_preserves_enum_boolean_limits_and_safe_static_presentation(self):
        for profile in PROFILES:
            schema = self.source(profile.prepare)["form_schema"]
            schema["fields"][0]["private_value"] = "must-not-leak"
            ui = profile.ui(schema)
            self.assertNotIn("private_value", str(ui))
            for source, projected in zip(schema["fields"], ui["fields"]):
                for key in ("options", "value_type", "minimum", "maximum", "step", "max_length"):
                    if key in source:
                        self.assertEqual(projected[key], source[key])
            self.assertNotIn("fields", profile.presentation())
            self.assertNotIn("must-not-leak", str(profile.presentation()))

    def test_unknown_profile_versions_fields_and_legacy_sources_do_not_enable_final(self):
        for profile in PROFILES:
            with self.subTest(profile=profile.profile_id):
                source = self.source(profile.prepare)
                source["form_schema"]["_agentbridge_business_draft_profile"]["version"] = "future"
                with self.assertRaises(BusinessDraftError):
                    profile_for_source(source)
                source = self.source(profile.prepare)
                source["form_schema"]["fields"].append({"name": "unexpected"})
                with self.assertRaises(BusinessDraftError):
                    profile_for_source(source)
                source = self.source(profile.prepare)
                del source["form_schema"]["_agentbridge_business_draft"]
                with self.assertRaises(BusinessDraftError):
                    profile_for_source(source)

    def test_batch_wrapper_profile_comes_from_exact_item_commit(self):
        profiles = [profile for profile in PROFILES if profile.view_kind == "approval"]
        self.assertEqual(len(profiles), 17)
        for wrapper in BATCH_PREPARES:
            for profile in profiles:
                definition = _TRUSTED_WRITE_DEFINITIONS[profile.prepare]
                self.assertEqual(profile_for_definition(wrapper, definition), profile)
                source = self.source(profile.prepare)
                source["capability_name"] = wrapper
                self.assertEqual(profile_for_source(source), profile)
        self.assertIsNone(profile_for_definition("oa.workflow.pending.batch.prepare", {"commit_capability": "unsupported"}))

    def test_non_approval_plans_without_action_contract_are_supported_and_contracts_bound(self):
        for profile in PROFILES:
            if profile.view_kind in {"work_log", "approval"}:
                continue
            prepared = {"plan": {"target": {"id": "fixed"}, "exact_input": {}, "expected_effect": {},
                "form_contract": {"version": "one"}, "preconditions": {"checked_at": "first", "frame_module_id": "transient-one"}},
                "summary": {"fields": [], "effect": "synthetic"}}
            first = review_payload(profile, prepared)[1]
            prepared["plan"]["preconditions"].update(checked_at="second", frame_module_id="transient-two")
            self.assertEqual(first, review_payload(profile, prepared)[1])
            prepared["plan"]["form_contract"]["version"] = "two"
            self.assertNotEqual(first, review_payload(profile, prepared)[1])

    def test_materials_are_preserved_bound_and_explicit_incompleteness_blocks(self):
        profile = BY_PREPARE["oa.attendance_confirmation.prepare"]
        prepared = {"plan": {"target": {}, "exact_input": {}, "expected_effect": {}}, "summary": {
            "sections": [{"kind": "text", "text": "complete original evidence"}], "attachments": [{"name": "name only"}],
            "materialsVersion": "1", "completeness": {"status": "complete"}, "truncated": False}}
        summary, semantic = review_payload(profile, prepared)
        self.assertEqual(summary["sections"], prepared["summary"]["sections"])
        BusinessDraftService._require_complete_review(summary)
        prepared["summary"]["sections"][0]["text"] = "changed evidence"
        self.assertNotEqual(semantic, review_payload(profile, prepared)[1])
        for incomplete in ({"completeness": {"status": "unverified"}}, {"truncated": True}):
            with self.assertRaises(BusinessDraftError) as caught:
                BusinessDraftService._require_complete_review(incomplete)
            self.assertEqual(caught.exception.code, "DRAFT_MATERIALS_INCOMPLETE")
