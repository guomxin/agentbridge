"""Opt-in presentation of a task-bound trusted interaction."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


FLOW_VERSION = "agentbridge.trusted-flow.v1"
FLOW_ENDPOINT_CAPABILITY = "continuous_flow_v1"


def continuous_flow_presentation(
    interaction: dict, *, task_id: str | None, enabled: bool
) -> dict:
    if not enabled or not task_id:
        return interaction
    prefix = {
        "business_input": "/input/",
        "execution_authorization": "/authorize/",
    }.get(interaction.get("type"))
    presentation = interaction.get("presentation") or {}
    url = urlsplit(presentation.get("url") or "")
    if not prefix or not url.path.startswith(prefix):
        return interaction
    return {
        **interaction,
        "presentation": {
            **presentation,
            "url": urlunsplit(url._replace(path="/flow" + url.path)),
            "flowVersion": FLOW_VERSION,
        },
    }
