"""Expose the shared draft protocol through NanoBot SDK tools."""
from nanobot.agent.tools.base import Tool, ToolResult
from .draft_actions import ACTIONS, AUTHORING, DESCRIPTION, DraftActions
from .skill_model import provider_completion


class DraftTool(DraftActions, Tool):
    def __init__(self, config, request, context, skill_run, *, registry=None):
        async def complete(system, prompt):
            return await provider_completion(config.model_copy(deep=True), system, prompt)
        super().__init__(request, context, skill_run, registry=registry, complete=complete, error=ToolResult.error, host_type="nanobot")
