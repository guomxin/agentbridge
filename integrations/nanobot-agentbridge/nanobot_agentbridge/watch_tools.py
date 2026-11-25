"""Expose reviewed watch operations through NanoBot SDK tools."""
from nanobot.agent.tools.base import Tool, ToolResult
from .watch_actions import CREATE, UPDATE, WATCH_MUTATIONS, WatchActions


class WatchTool(WatchActions, Tool):
    def __init__(self, definition, request, context, skill_run):
        super().__init__(definition, request, context, skill_run, error=ToolResult.error, host_type="nanobot")
