import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.adapters.page_scripts import load_seeyon_action_page_script
from agentbridge.adapters.seeyon_write import (
    append_oa_write_audit,
    build_oa_write_plan,
    get_write_action_spec,
    list_write_action_specs,
)


class SeeyonWriteTests(unittest.TestCase):
    def test_page_script_registry_loads_continue_submit_and_launch_save_draft(self):
        continue_submit = load_seeyon_action_page_script("ContinueSubmit")
        save_draft = load_seeyon_action_page_script("SaveDraft")

        self.assertEqual(continue_submit["script_name"], "seeyon.continue_submit.v1")
        self.assertIn("function agentbridgePageScript", continue_submit["script_source"])
        self.assertEqual(continue_submit["outcome_key"], "__agentbridgeContinueSubmitLast")
        self.assertEqual(save_draft["script_name"], "seeyon.launch_save_draft.v1")
        self.assertIn("function agentbridgePageScript", save_draft["script_source"])
        self.assertEqual(save_draft["outcome_key"], "__agentbridgeLaunchSaveDraftLast")

    def test_write_action_specs_centralize_promotion_and_verification(self):
        specs = {spec.code: spec for spec in list_write_action_specs()}

        self.assertEqual(specs["ContinueSubmit"].action_type, "workflow.submit")
        self.assertEqual(specs["ContinueSubmit"].promotion_status, "promoted")
        self.assertEqual(specs["ContinueSubmit"].verification_method, "pending_disappearance")
        self.assertTrue(specs["ContinueSubmit"].execute_allowed)
        self.assertEqual(specs["Archive"].promotion_status, "dry_run_only")
        self.assertEqual(specs["Archive"].verification_method, "not_promoted")
        self.assertFalse(specs["Archive"].execute_allowed)
        self.assertEqual(get_write_action_spec("UnknownAction").action_type, "workflow.unpromoted")

    def test_build_write_plan_includes_non_sent_payload_preview(self):
        plan = build_oa_write_plan(
            affair_id="affair-1",
            action="ContinueSubmit",
            opinion="approve",
            mode="dry-run",
            source_url="http://oa.example.test/detail?a=1",
        )

        self.assertEqual(plan["request"]["status"], "not_sent")
        self.assertIsNone(plan["request"]["method"])
        self.assertIsNone(plan["request"]["url"])
        self.assertEqual(
            plan["request"]["payload_preview"],
            {
                "affairId": "affair-1",
                "actionCode": "ContinueSubmit",
                "opinionText": "approve",
                "sourceUrl": "http://oa.example.test/detail?a=1",
                "dryRunOnly": True,
            },
        )
        self.assertEqual(
            plan["request"]["payload_fields"],
            [
                {"name": "affairId", "value_present": True},
                {"name": "actionCode", "value_present": True},
                {"name": "opinionText", "value_present": True, "length": 7},
                {"name": "sourceUrl", "value_present": True},
            ],
        )

    def test_archive_write_plan_is_dry_run_only_until_promoted(self):
        plan = build_oa_write_plan(
            affair_id="archive-1",
            action="Archive",
            opinion="",
            mode="dry-run",
        )

        self.assertEqual(plan["action"], {"code": "Archive", "label": "处理后归档", "risk": "high"})
        self.assertEqual(plan["governance"]["action_type"], "workflow.archive")
        self.assertEqual(plan["governance"]["verification_method"], "not_promoted")
        self.assertEqual(plan["promotion"]["status"], "dry_run_only")
        self.assertFalse(plan["promotion"]["execute_allowed"])
        self.assertIn("execution mapping", plan["promotion"]["requirements"][0])
        self.assertFalse(plan["safety"]["will_execute"])

    def test_write_audit_redacts_payload_preview_opinion_text(self):
        plan = build_oa_write_plan(
            affair_id="affair-1",
            action="ContinueSubmit",
            opinion="approve",
            mode="dry-run",
        )

        with TemporaryDirectory() as tmp:
            audit_path = append_oa_write_audit(Path(tmp), plan)
            rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 1)
        self.assertNotIn("approve", json.dumps(rows[0], ensure_ascii=False))
        self.assertIsNone(rows[0]["request"]["body"])
        self.assertEqual(rows[0]["request"]["payload_preview"]["opinionText"], None)
        self.assertEqual(rows[0]["request"]["payload_fields"][2]["length"], 7)


if __name__ == "__main__":
    unittest.main()
