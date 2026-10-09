from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, TextContent

from agentbridge.core.interactions import INTERACTION_SCHEMA_VERSION
from agentbridge.core.host_contract import (
    HOST_CONTRACT_SCHEMA,
    HOST_CONTEXT_META_KEY,
    HOST_PROFILE_META_KEY,
    TASK_CONTEXT_META_KEY,
    host_transport_recovery_strategy,
)
from agentbridge.core.planning_policy import COMPOSED_TASK_PLANNING_POLICY


MCP_APP_RESOURCE_URI = "ui://agentbridge/trusted-interaction.html"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
MCP_PROFILE_RESOURCE_URI = "agentbridge://server/profile"
PRIVATE_INTERACTION_META_KEY = "io.agentbridge/interaction"
WITHHELD_TRUSTED_URL = "[trusted AgentBridge URL withheld from model context]"


def interaction_tool_meta() -> dict[str, Any]:
    return {
        "ui": {
            "resourceUri": MCP_APP_RESOURCE_URI,
            "visibility": ["model", "app"],
        },
        # Older MCP Apps hosts still inspect this compatibility key.
        "ui/resourceUri": MCP_APP_RESOURCE_URI,
    }


def build_server_profile(*, mcp_url: str) -> dict[str, Any]:
    return {
        "name": "AgentBridge",
        "profileVersion": "agentbridge.server.v1",
        "mcp": {
            "endpoint": mcp_url,
            "transport": "streamable_http",
            "authorization": {
                "mode": "server_issued_bearer",
                "identityBinding": "token_to_user_subject",
                "oauthBootstrap": "planned",
            },
        },
        "interactions": {
            "schemaVersion": INTERACTION_SCHEMA_VERSION,
            "types": [
                "credential",
                "business_input",
                "execution_authorization",
            ],
            "delivery": [
                {
                    "method": "mcp_app",
                    "status": "available",
                    "resourceUri": MCP_APP_RESOURCE_URI,
                    "mimeType": MCP_APP_MIME_TYPE,
                    "autoPollAndResume": True,
                    "requires": "MCP Apps host support",
                },
                {
                    "method": "host_adapter",
                    "status": "available",
                    "privateMetaKey": PRIVATE_INTERACTION_META_KEY,
                    "autoPollAndResume": "adapter_defined",
                    "requires": "private user-channel binding",
                },
                {
                    "method": "model_visible_url",
                    "status": "disabled",
                    "reason": "trusted card URLs must not enter model context",
                },
                {
                    "method": "mcp_url_elicitation",
                    "status": "deferred",
                    "reason": "requires independent browser-user identity binding",
                },
            ],
        },
        "clientFootprint": {
            "required": [
                "MCP-capable agent host",
                "TLS trust for the AgentBridge endpoint",
                "AgentBridge MCP authorization",
            ],
            "notRequired": [
                "Chrome extension",
                "local OA connector",
                "local browser worker",
                "AgentBridge business CLI",
            ],
            "optional": [
                "host interaction adapter when MCP Apps is unavailable",
                "thin CLI for operations and compatibility",
            ],
        },
        "safety": {
            "identitySource": "authenticated MCP token",
            "modelMustNotCollectInteractionValues": True,
            "governedWriteFlow": "prepare -> authorize -> commit -> verify",
        },
        "planning": deepcopy(COMPOSED_TASK_PLANNING_POLICY),
        "hostContract": {
            "schemaVersion": HOST_CONTRACT_SCHEMA,
            "negotiationTool": "agentbridge_server_profile",
            "levels": ["L1", "L2", "L3"],
            "defaultLevel": "L1",
            "runtimeContextMetaKey": HOST_CONTEXT_META_KEY,
            "callContextMetaKey": TASK_CONTEXT_META_KEY,
            "profileMetaKey": HOST_PROFILE_META_KEY,
            "schemaResourcePrefix": "agentbridge://host-contract/v1/",
            "requiresExactVersionRegistrationFor": ["L2", "L3"],
            "coordination": {
                "multiplePresenters": True,
                "singleCoordinatorLease": True,
                "explicitTakeover": True,
            },
            "transportRecovery": {
                call_class: host_transport_recovery_strategy(call_class)
                for call_class in ("read", "prepare", "commit", "completedResume")
            },
        },
    }


def package_interaction_result(response: dict[str, Any]) -> dict[str, Any] | CallToolResult:
    interaction = response.get("interaction")
    if not _is_interaction(interaction):
        return response

    private_interaction = deepcopy(interaction)
    trusted_url = private_interaction["presentation"]["url"]
    public_response = _redact_value(deepcopy(response), trusted_url)
    public_interaction = public_response["interaction"]
    public_interaction["presentation"].update(
        {
            "preferred": "mcp_app",
            "fallback": "host_adapter",
            "uiResourceUri": MCP_APP_RESOURCE_URI,
            "hostHandled": True,
        }
    )

    text = (
        f"AgentBridge requires trusted user interaction: {interaction['title']}. "
        "The host has already received the AgentBridge secure interaction surface. "
        "Do not ask for credentials, business fields, or authorization decisions "
        "in the conversation. Briefly ask the user to use the displayed card, without "
        "repeating card fields or internal IDs. Do not call agentbridge_interaction_get "
        f"again in this turn. Internal interaction ID: {interaction['interactionId']}. "
        "Only if the user reports in a later turn that the card is missing may you call "
        "agentbridge_interaction_get with this ID."
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=public_response,
        _meta={
            PRIVATE_INTERACTION_META_KEY: private_interaction,
            "io.agentbridge/ui": {
                "resourceUri": MCP_APP_RESOURCE_URI,
                "modelVisibleUrl": False,
            },
        },
    )


def load_mcp_app_html() -> str:
    path = Path(__file__).with_name("static") / "trusted-interaction.html"
    return path.read_text(encoding="utf-8")


def server_profile_json(*, mcp_url: str) -> str:
    return json.dumps(
        build_server_profile(mcp_url=mcp_url),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _is_interaction(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("schemaVersion") != INTERACTION_SCHEMA_VERSION:
        return False
    presentation = value.get("presentation")
    return (
        isinstance(value.get("interactionId"), str)
        and isinstance(value.get("title"), str)
        and isinstance(presentation, dict)
        and isinstance(presentation.get("url"), str)
        and bool(presentation["url"])
    )


def _redact_value(value: Any, trusted_url: str) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value(item, trusted_url) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, trusted_url) for item in value]
    if isinstance(value, str) and trusted_url in value:
        return value.replace(trusted_url, WITHHELD_TRUSTED_URL)
    return value
