import unittest

from agentbridge.integrations.openclaw import render_openclaw_interaction


class OpenClawInteractionRendererTests(unittest.TestCase):
    def test_telegram_private_chat_uses_web_app_button(self):
        rendered = render_openclaw_interaction(
            _interaction(),
            channel="telegram",
            private_chat=True,
        )

        button = rendered["presentation"]["blocks"][-1]["buttons"][0]
        self.assertEqual(
            button["webApp"]["url"],
            "https://cards.example.test/input/opaque",
        )
        self.assertNotIn("url", button)
        self.assertEqual(
            rendered["automation"]["poll"]["tool"],
            "agentbridge_interaction_get",
        )

    def test_generic_channel_uses_portable_url_button(self):
        rendered = render_openclaw_interaction(_interaction())

        button = rendered["presentation"]["blocks"][-1]["buttons"][0]
        self.assertEqual(
            button["url"],
            "https://cards.example.test/input/opaque",
        )
        self.assertNotIn("webApp", button)

    def test_telegram_private_http_uses_portable_url_button(self):
        interaction = _interaction()
        interaction["presentation"]["url"] = (
            "http://10.10.50.213:8780/input/opaque"
        )

        rendered = render_openclaw_interaction(
            interaction,
            channel="telegram",
            private_chat=True,
        )

        button = rendered["presentation"]["blocks"][-1]["buttons"][0]
        self.assertEqual(
            button["url"],
            "http://10.10.50.213:8780/input/opaque",
        )
        self.assertNotIn("webApp", button)

    def test_completed_interaction_has_no_user_button_and_is_ready_to_resume(self):
        interaction = _interaction()
        interaction["state"] = "completed"
        interaction["resume"] = {
            "tool": "agentbridge_interaction_resume",
            "ready": True,
            "completed": False,
        }

        rendered = render_openclaw_interaction(interaction)

        self.assertTrue(rendered["automation"]["resume"]["ready"])
        self.assertFalse(
            any(
                block["type"] == "buttons"
                for block in rendered["presentation"]["blocks"]
            )
        )


def _interaction():
    return {
        "schemaVersion": "agentbridge.interaction.v1",
        "interactionId": "interaction-123456",
        "type": "business_input",
        "state": "pending",
        "title": "填写出差申请",
        "message": "请在安全页面填写。",
        "operationId": "operation-1",
        "presentation": {
            "owner": "agentbridge",
            "preferred": "embedded_secure_web_app",
            "fallback": "url",
            "url": "https://cards.example.test/input/opaque",
            "modelMustNotCollectValues": True,
        },
        "display": {"systemName": "致远 OA", "fieldCount": 7},
        "expiresAt": "2026-07-14T00:15:00+00:00",
        "poll": {
            "tool": "agentbridge_interaction_get",
            "recommendedIntervalSeconds": 2,
        },
        "resume": {
            "tool": "agentbridge_interaction_resume",
            "ready": False,
            "completed": False,
        },
    }


if __name__ == "__main__":
    unittest.main()
