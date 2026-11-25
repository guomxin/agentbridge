"""NanoBot SDK tool wrapping the host-independent private plan workflow."""
from nanobot.agent.tools.base import Tool
from .coordinated_tools import run_governed
from .plan_submission import PLAN, PlanSubmission


class PlanTool(PlanSubmission, Tool):
    def __init__(self, request, context, definition, skill_scope, allowed_model_tools, allowed_prepares=None):
        super().__init__(request, context, definition, skill_scope, allowed_model_tools, allowed_prepares,
                         host_type="nanobot", govern=run_governed)
