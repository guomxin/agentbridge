from __future__ import annotations

import asyncio
from typing import Any



class ReferenceHostRuntimeCollector:
    def __init__(self, task_driver: Any, *, interval_seconds: float = 60.0) -> None:
        self.task_driver = task_driver
        self.interval_seconds = max(float(interval_seconds), 15.0)
        self._task: asyncio.Task[None] | None = None
        self._initial_done = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._initial_done.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="reference-host-runtime-collector",
            )

    async def wait_initial(self, timeout: float = 2.0) -> None:
        if self._task is None or self._initial_done.is_set():
            return
        try:
            await asyncio.wait_for(self._initial_done.wait(), timeout=timeout)
        except TimeoutError:
            pass

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def collect_once(self) -> list[dict[str, Any]]:
        results = []
        for identity in self.task_driver.identities.list():
            try:
                await self.task_driver.ensure_identity(identity.label)
                result = await self.task_driver.client(identity.label).call_tool(
                    "agentbridge_host_runtime_snapshot",
                    {"snapshot": self.task_driver.runtime_snapshot(identity.label)},
                    meta=self.task_driver.host_context(),
                    recovery_class="prepare",
                )
                payload = result.payload()
                if result.is_error or payload.get("status") != "succeeded":
                    error_code = str(
                        (payload.get("error") or {}).get("code")
                        or "HOST_RUNTIME_SIGNAL_REJECTED"
                    )
                    error = RuntimeError(error_code)
                    error.code = error_code
                    raise error
            except Exception as exc:
                results.append(
                    {
                        "identityLabel": identity.label,
                        "status": "failed",
                        "errorCode": _error_code(exc),
                    }
                )
            else:
                results.append(payload)
        return results

    async def _run(self) -> None:
        while True:
            try:
                await self.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A runtime sample is evidence, not a business operation. A
                # later cycle may recover without changing task outcomes.
                pass
            finally:
                self._initial_done.set()
            await asyncio.sleep(self.interval_seconds)


def _error_code(exc: Exception) -> str:
    value = getattr(exc, "code", None) or exc.__class__.__name__
    return "".join(
        character if character.isalnum() else "_"
        for character in str(value).upper()
    ).strip("_")[:120]
