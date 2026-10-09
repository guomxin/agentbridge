import json
import unittest

from mcp.types import CallToolResult

from agentbridge.mcp.presentation import (
    MCP_APP_MIME_TYPE,
    MCP_APP_RESOURCE_URI,
    PRIVATE_INTERACTION_META_KEY,
    WITHHELD_TRUSTED_URL,
    build_server_profile,
    interaction_tool_meta,
    package_interaction_result,
)


class McpPresentationTests(unittest.TestCase):
    def test_server_profile_describes_low_install_remote_mcp(self):
        profile = build_server_profile(mcp_url="https://agentbridge.example/mcp")

        self.assertEqual(profile["mcp"]["transport"], "streamable_http")
        self.assertEqual(
            profile["interactions"]["delivery"][0],
            {
                "method": "mcp_app",
                "status": "available",
                "resourceUri": MCP_APP_RESOURCE_URI,
                "mimeType": MCP_APP_MIME_TYPE,
                "autoPollAndResume": True,
                "requires": "MCP Apps host support",
            },
        )
        self.assertIn("Chrome extension", profile["clientFootprint"]["notRequired"])
        self.assertEqual(
            profile["interactions"]["delivery"][3]["status"],
            "deferred",
        )
        self.assertEqual(
            profile["planning"]["schemaVersion"],
            "agentbridge.composed-task-planning-policy.v1",
        )
        self.assertIn("later action", profile["planning"]["modelContext"])
        self.assertIn("source_id", profile["planning"]["modelContext"])
        self.assertIn("database.report.export", profile["planning"]["modelContext"])
        self.assertIn("不建立专用计划", profile["planning"]["modelContext"])
        self.assertNotIn("OA 转泰华", profile["planning"]["modelContext"])

    def test_interaction_tool_metadata_supports_modern_and_legacy_hosts(self):
        metadata = interaction_tool_meta()

        self.assertEqual(metadata["ui"]["resourceUri"], MCP_APP_RESOURCE_URI)
        self.assertEqual(metadata["ui/resourceUri"], MCP_APP_RESOURCE_URI)
        self.assertEqual(metadata["ui"]["visibility"], ["model", "app"])

    def test_trusted_url_moves_to_private_result_metadata(self):
        card_url = "https://cards.example.test/input/opaque-resource"
        response = {
            "status": "requires_user_action",
            "nextAction": {"cardUrl": card_url},
            "interaction": _interaction(card_url),
        }

        packaged = package_interaction_result(response)

        self.assertIsInstance(packaged, CallToolResult)
        payload = packaged.model_dump(by_alias=True)
        model_visible = json.dumps(
            {
                "content": payload["content"],
                "structuredContent": payload["structuredContent"],
            }
        )
        self.assertNotIn(card_url, model_visible)
        self.assertIn(WITHHELD_TRUSTED_URL, model_visible)
        self.assertEqual(
            payload["_meta"][PRIVATE_INTERACTION_META_KEY]["presentation"]["url"],
            card_url,
        )
        self.assertEqual(response["nextAction"]["cardUrl"], card_url)
        self.assertTrue(
            payload["structuredContent"]["interaction"]["presentation"][
                "modelMustNotCollectValues"
            ]
        )
        model_text = payload["content"][0]["text"]
        self.assertIn("already received", model_text)
        self.assertIn("Do not call agentbridge_interaction_get again in this turn", model_text)
        self.assertIn("in a later turn", model_text)

    def test_non_interaction_result_is_unchanged(self):
        response = {"status": "succeeded", "result": {"count": 0}}

        self.assertIs(package_interaction_result(response), response)


def _interaction(card_url):
    return {
        "schemaVersion": "agentbridge.interaction.v1",
        "interactionId": "interaction-1234567890",
        "type": "business_input",
        "state": "pending",
        "title": "Business trip input",
        "message": "Enter the requested business fields.",
        "presentation": {
            "owner": "agentbridge",
            "preferred": "embedded_secure_web_app",
            "fallback": "url",
            "url": card_url,
            "modelMustNotCollectValues": True,
        },
        "display": {"systemName": "OA", "fieldCount": 6},
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
