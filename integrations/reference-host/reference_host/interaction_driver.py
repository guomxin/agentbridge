from __future__ import annotations

import asyncio
from typing import Any, Mapping, TYPE_CHECKING
from .recovery_policy import interaction_resume_allowed, resume_claim_allowed

if TYPE_CHECKING:
    from .task_driver import ReferenceTaskDriver


TERMINAL_INTERACTION_STATES = frozenset(
    {"declined", "expired", "failed", "superseded"}
)


class InteractionDriver:
    def __init__(
        self,
        task_driver: "ReferenceTaskDriver",
        *,
        poll_interval_seconds: float = 2.0,
        maximum_wait_seconds: float = 900.0,
    ) -> None:
        self.task_driver = task_driver
        self.poll_interval_seconds = max(float(poll_interval_seconds), 0.05)
        self.maximum_wait_seconds = max(float(maximum_wait_seconds), 1.0)
        self._monitors: dict[str, asyncio.Task] = {}
        self._resume_started: set[str] = set()

    def start(self, local_task_id: str, interaction: Mapping[str, Any]) -> None:
        interaction_id = str(interaction.get("interactionId") or "").strip()
        if not interaction_id:
            raise ValueError("interaction ID is required")
        current = self._monitors.get(local_task_id)
        if current and not current.done():
            current.cancel()
        self._monitors[local_task_id] = asyncio.create_task(
            self._monitor(local_task_id, interaction_id),
            name=f"reference-host-interaction:{interaction_id}",
        )

    async def stop(self) -> None:
        monitors = [task for task in self._monitors.values() if not task.done()]
        for task in monitors:
            task.cancel()
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)
        self._monitors.clear()

    async def _monitor(self, local_task_id: str, interaction_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.maximum_wait_seconds
        try:
            while loop.time() < deadline:
                current = await self.task_driver.get_interaction(
                    local_task_id,
                    interaction_id,
                )
                state = str(current.get("state") or "").lower()
                resume = current.get("resume")
                resume = resume if isinstance(resume, Mapping) else {}
                await self.task_driver.note_interaction_state(
                    local_task_id,
                    current,
                )
                if interaction_resume_allowed({"state": state, "resume": resume}):
                    if not resume_claim_allowed(claimed=interaction_id in self._resume_started):
                        return
                    self._resume_started.add(interaction_id)
                    await self.task_driver.resume_interaction(
                        local_task_id,
                        interaction_id,
                    )
                    return
                if state == "completed":
                    if resume.get("completed") is True:
                        await self.task_driver.close_interaction(
                            local_task_id,
                            current,
                        )
                        return
                    # Completion and resumability can be persisted in adjacent
                    # transactions. Keep polling until resume becomes ready.
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue
                if state in TERMINAL_INTERACTION_STATES:
                    await self.task_driver.close_interaction(
                        local_task_id,
                        current,
                    )
                    return
                await asyncio.sleep(self.poll_interval_seconds)
            await self.task_driver.mark_poll_deadline(
                local_task_id,
                interaction_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.task_driver.mark_interaction_driver_error(
                local_task_id,
                interaction_id,
                exc,
            )
        finally:
            current = self._monitors.get(local_task_id)
            if current is asyncio.current_task():
                self._monitors.pop(local_task_id, None)
