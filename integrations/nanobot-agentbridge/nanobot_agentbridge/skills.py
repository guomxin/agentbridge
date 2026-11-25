"""Run an immutable Workspace draft sample without exposing business or native tools."""

from .skill_scope import SkillRun, run_trial, sample_resources

from nanobot import Nanobot
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry

from .provider import CheckedProvider


class IsolatedSampleTools(ToolRegistry):
    def register(self, tool):
        # NanoBot's AgentLoop attempts to auto-register its own native tools.
        return None


async def run_workspace_trial(config, session, binding, request, meta, emit):
    async def complete(prompt):
        dispatch = request["trial"]["dispatch_id"]
        registry = IsolatedSampleTools()
        checked = CheckedProvider(config)
        loop = AgentLoop.from_config(config, tool_registry=registry, provider=checked)
        async with Nanobot(loop, config=config) as bot:
            run = await bot.run_streamed(
                prompt,
                session_key="sdk:agentbridge:trial:" + dispatch,
                channel="workspace",
                chat_id=binding.endpoint,
                sender_id=binding.subject,
            )
            try:
                async for _ in run.stream_events():
                    pass
                answer = checked.result(await run.wait())
            finally:
                if not run.done:
                    await run.cancel()
        return answer

    answer = await run_trial(session, request, meta, complete)
    await emit(answer)
    return answer
