from __future__ import annotations

import unittest

from agentbridge.core.trusted_flow_presentation import continuous_flow_presentation


class TrustedFlowPresentationTests(unittest.TestCase):
    def test_opt_in_keeps_original_endpoint_binding(self):
        interaction = {
            "type": "business_input",
            "presentation": {
                "url": "https://trusted.example/input/abc/present/endpoint-proof",
                "presentationId": "endpoint-proof",
            },
        }
        result = continuous_flow_presentation(interaction, task_id="task-a", enabled=True)
        self.assertEqual(
            result["presentation"]["url"],
            "https://trusted.example/flow/input/abc/present/endpoint-proof",
        )
        self.assertEqual(result["presentation"]["presentationId"], "endpoint-proof")
        self.assertNotIn("flowVersion", interaction["presentation"])

    def test_legacy_unbound_and_identity_interactions_keep_existing_url(self):
        for kind, task_id, enabled in (
            ("business_input", "task-a", False),
            ("execution_authorization", None, True),
            ("credential", "task-a", True),
        ):
            with self.subTest(kind=kind, task_id=task_id, enabled=enabled):
                interaction = {"type": kind, "presentation": {"url": "https://trusted.example/input/a"}}
                self.assertIs(
                    continuous_flow_presentation(interaction, task_id=task_id, enabled=enabled),
                    interaction,
                )
