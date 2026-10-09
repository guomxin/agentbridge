import unittest

from agentbridge.adapters.seeyon_matter_intent import build_matter_intent_preflight, normalize_matter_intent


class SeeyonMatterIntentTests(unittest.TestCase):
    def test_approve_intent_maps_to_continue_submit_when_action_is_available(self):
        result = build_matter_intent_preflight(
            source_item={"title": "Weekly report", "affair_id": "affair-1"},
            evidence={
                "identity": {"title": "Weekly report", "affair_id": "affair-1"},
                "actions": {"codes": ["ContinueSubmit"], "items": [{"code": "ContinueSubmit", "label": "提交"}]},
            },
            intent="approve",
            opinion="read",
        )

        self.assertEqual(result["intent"]["code"], "approve")
        self.assertEqual(result["binding"]["action"], "ContinueSubmit")
        self.assertEqual(result["decision"]["status"], "ready_for_execute")
        self.assertFalse(result["execution_contract"]["will_execute"])
        self.assertEqual(result["intent"]["opinion_length"], 4)

    def test_archive_intent_remains_dry_run_only_when_archive_action_is_available(self):
        result = build_matter_intent_preflight(
            source_item={"title": "Archive confirmation", "affair_id": "affair-2"},
            evidence={
                "identity": {"title": "Archive confirmation", "affair_id": "affair-2"},
                "actions": {"codes": ["Archive"], "items": [{"code": "Archive", "label": "处理后归档"}]},
            },
            intent="archive",
            opinion="已阅",
        )

        self.assertEqual(result["binding"]["action"], "Archive")
        self.assertEqual(result["binding"]["promotion_status"], "dry_run_only")
        self.assertEqual(result["decision"]["status"], "dry_run_only")
        self.assertEqual(result["execution_contract"]["low_level_execute_command"], "")

    def test_missing_page_action_blocks_the_business_intent(self):
        result = build_matter_intent_preflight(
            source_item={"title": "Archive confirmation", "affair_id": "affair-3"},
            evidence={
                "identity": {"title": "Archive confirmation", "affair_id": "affair-3"},
                "actions": {"codes": ["Archive"], "items": [{"code": "Archive", "label": "处理后归档"}]},
            },
            intent="approve",
        )

        self.assertEqual(result["binding"]["action"], "ContinueSubmit")
        self.assertEqual(result["decision"]["status"], "blocked")
        self.assertIn("does not expose action ContinueSubmit", result["decision"]["blocked_reasons"][0])

    def test_normalize_matter_intent_accepts_aliases_and_rejects_unknown_values(self):
        self.assertEqual(normalize_matter_intent("submit").code, "approve")
        self.assertEqual(normalize_matter_intent("archive_after_process").code, "archive")
        with self.assertRaises(ValueError):
            normalize_matter_intent("delete")


if __name__ == "__main__":
    unittest.main()
