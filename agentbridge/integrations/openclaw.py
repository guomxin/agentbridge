from __future__ import annotations

from typing import Any

from agentbridge.core.interactions import INTERACTION_SCHEMA_VERSION


_BUTTON_LABELS = {
    "credential": "安全登录",
    "business_input": "填写信息",
    "execution_authorization": "核对并确认",
}


def render_openclaw_interaction(
    interaction: dict[str, Any],
    *,
    channel: str | None = None,
    private_chat: bool = False,
) -> dict[str, Any]:
    """Translate a trusted interaction into OpenClaw presentation blocks."""

    if interaction.get("schemaVersion") != INTERACTION_SCHEMA_VERSION:
        raise ValueError("unsupported AgentBridge interaction schema")
    interaction_type = str(interaction.get("type") or "")
    if interaction_type not in _BUTTON_LABELS:
        raise ValueError("unsupported AgentBridge interaction type")
    presentation = interaction.get("presentation")
    if not isinstance(presentation, dict):
        raise ValueError("interaction presentation is missing")

    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": str(interaction.get("message") or "")},
        {
            "type": "context",
            "text": _context_text(interaction),
        },
    ]
    if interaction.get("state") == "pending":
        url = str(presentation.get("url") or "")
        if not url.startswith(("http://", "https://")):
            raise ValueError("interaction secure web URL is invalid")
        button: dict[str, Any] = {
            "label": _BUTTON_LABELS[interaction_type],
            "priority": 100,
        }
        if channel == "telegram" and private_chat and url.startswith("https://"):
            button["webApp"] = {"url": url}
        else:
            button["url"] = url
        blocks.append(
            {
                "type": "buttons",
                "buttons": [button],
            }
        )

    return {
        "presentation": {
            "title": str(interaction.get("title") or "AgentBridge"),
            "tone": _tone(interaction.get("state")),
            "blocks": blocks,
        },
        "automation": {
            "interactionId": interaction["interactionId"],
            "poll": interaction["poll"],
            "resume": interaction["resume"],
        },
    }


def _context_text(interaction: dict[str, Any]) -> str:
    display = interaction.get("display")
    if not isinstance(display, dict):
        display = {}
    parts = ["AgentBridge 可信交互"]
    if display.get("systemName"):
        parts.append(str(display["systemName"]))
    if isinstance(display.get("fieldCount"), int):
        parts.append(f"{display['fieldCount']} 个字段")
    parts.append(f"状态：{interaction.get('state', 'unknown')}")
    return " · ".join(parts)


def _tone(state: object) -> str:
    return {
        "pending": "warning",
        "processing": "info",
        "completed": "success",
        "declined": "neutral",
        "expired": "danger",
        "failed": "danger",
        "superseded": "neutral",
    }.get(str(state), "neutral")
