from __future__ import annotations

from typing import Any, Mapping



CENTRAL_TERMINAL_STATES = frozenset(
    {"completed", "succeeded", "failed", "canceled", "cancelled", "unknown"}
)


class ReferenceHostRecovery:
    """Restore only tasks owned by this Reference Host instance."""

    def __init__(self, task_driver: Any) -> None:
        self.task_driver = task_driver

    async def recover_all(self) -> dict[str, Any]:
        reports = []
        for identity in self.task_driver.identities.list():
            try:
                reports.append(await self.recover_identity(identity.label))
            except Exception as exc:
                reports.append(
                    {
                        "identityLabel": identity.label,
                        "restored": [],
                        "observeOnly": [],
                        "failed": [],
                        "recoveryErrorCode": _error_code(exc),
                    }
                )
        return {
            "status": "succeeded",
            "hostInstanceId": self.task_driver.instance_id,
            "identities": reports,
        }

    async def recover_identity(self, identity_label: str) -> dict[str, Any]:
        await self.task_driver.ensure_identity(identity_label)
        identity = self.task_driver.identities.get(identity_label)
        restored: list[str] = []
        observe_only: list[str] = []
        failed: list[str] = []

        local_tasks = self.task_driver.state.list_tasks(
            identity_label=identity_label,
            active_only=True,
            limit=500,
        )
        for task in local_tasks:
            local_task_id = task["local_task_id"]
            if not task.get("task_id"):
                arguments = task.get("restartable_read_arguments") or {}
                if arguments and task.get("tool_name"):
                    self.task_driver._plans[local_task_id] = (
                        task["tool_name"],
                        dict(arguments),
                    )
                    await self.task_driver._bind_and_execute(
                        local_task_id,
                        dict(arguments),
                        propagate_error=False,
                    )
                    restored.append(local_task_id)
                else:
                    self.task_driver.state.finish_task(
                        local_task_id,
                        status="failed",
                        error_summary={
                            "code": "HOST_RESTART_BEFORE_BIND",
                            "message": "任务在绑定中央任务前中断，且没有可安全重放的读取参数。",
                        },
                    )
                    failed.append(local_task_id)
                continue
            outcome = await self._recover_bound_task(local_task_id)
            if outcome == "restored":
                restored.append(local_task_id)
            elif outcome == "observe_only":
                observe_only.append(local_task_id)
            elif outcome == "failed":
                failed.append(local_task_id)

        try:
            remote = await self.task_driver.client(identity_label).call_tool(
                "agentbridge_host_task_recovery_list",
                {
                    "agent_host": self.task_driver.implementation.name,
                    "endpoint_key": identity.endpoint_key,
                    "limit": 100,
                    "include_user_endpoints": False,
                },
                meta=self.task_driver.host_context(),
                recovery_class="read",
            )
        except Exception as exc:
            # A fresh identity has no endpoint until its first task. That is not
            # a failed recovery and must not prevent the host from starting.
            if "endpoint" not in str(exc).casefold():
                raise
            remote_payload: Mapping[str, Any] = {}
        else:
            remote_payload = remote.payload()

        for item in remote_payload.get("recoveries") or []:
            if not isinstance(item, Mapping):
                continue
            central_task = item.get("task") or {}
            task_id = str(central_task.get("taskId") or "").strip()
            if not task_id:
                continue
            local = self.task_driver.state.task_for_central_id(
                identity_label=identity_label,
                task_id=task_id,
            )
            if local is None:
                endpoint = item.get("endpoint") or {}
                endpoint_id = str(endpoint.get("endpointId") or "").strip()
                if not endpoint_id:
                    failed.append(f"central:{task_id}")
                    continue
                local = self.task_driver.state.create_task(
                    identity_label=identity_label,
                    tool_name="agentbridge_recovered_task",
                    conversation_ref=identity.conversation_ref,
                    title=str(central_task.get("title") or "恢复中的 AgentBridge 任务"),
                )
                self.task_driver.state.bind_central_task(
                    local_task_id=local["local_task_id"],
                    task_id=task_id,
                    endpoint_id=endpoint_id,
                    lease_version=max(int((item.get("lease") or {}).get("version") or 1), 1),
                )
                outcome = await self._recover_bound_task(local["local_task_id"])
                if outcome == "restored":
                    restored.append(local["local_task_id"])
                elif outcome == "observe_only":
                    observe_only.append(local["local_task_id"])

        return {
            "identityLabel": identity_label,
            "restored": sorted(set(restored)),
            "observeOnly": sorted(set(observe_only)),
            "failed": sorted(set(failed)),
        }

    async def _recover_bound_task(self, local_task_id: str) -> str:
        task = self.task_driver.state.get_task(local_task_id)
        lease_result = await self.task_driver.client(task["identity_label"]).call_tool(
            "agentbridge_host_coordinator_lease_get",
            {"task_id": task["task_id"]},
            meta=self.task_driver.host_context(),
            recovery_class="read",
        )
        lease = lease_result.payload().get("coordinatorLease")
        if not isinstance(lease, Mapping):
            self.task_driver.state.update_task(local_task_id, status="observe_only")
            return "observe_only"
        if lease.get("hostInstanceId") != self.task_driver.instance_id:
            self.task_driver.state.update_task(local_task_id, status="observe_only")
            self.task_driver.state.append_event(
                local_task_id=local_task_id,
                event_key=f"recovery:{task['task_id']}:lease-observe-only",
                kind="host.recovery.observe_only",
                summary="任务由另一宿主实例协调，本实例只观察，不接管续办",
                payload={"leaseVersion": int(lease.get("version") or 0)},
            )
            return "observe_only"

        self.task_driver.state.update_task(
            local_task_id,
            lease_version=max(int(lease.get("version") or 1), 1),
        )
        renewed = await self.task_driver.renew_coordinator_lease(local_task_id)
        snapshot = await self.task_driver.refresh_task_snapshot(local_task_id)
        central_task = snapshot.get("task") or {}
        central_status = str(central_task.get("status") or "").casefold()
        interaction = snapshot.get("interaction")
        if isinstance(interaction, Mapping) and interaction.get("interactionId"):
            presented = await self.task_driver.present_interaction(
                local_task_id,
                str(interaction["interactionId"]),
            )
            self.task_driver.state.update_task(
                local_task_id,
                status="waiting_user",
                active_interaction_id=str(presented["interactionId"]),
                lease_version=int(renewed["version"]),
            )
            self.task_driver.interactions.start(local_task_id, presented)
        elif central_status in CENTRAL_TERMINAL_STATES:
            status = _local_terminal_status(central_status)
            self.task_driver.state.finish_task(
                local_task_id,
                status=status,
                result_summary={"centralStatus": central_status},
            )
        else:
            self.task_driver.state.update_task(
                local_task_id,
                status="recovering",
                lease_version=int(renewed["version"]),
            )
        self.task_driver.state.append_event(
            local_task_id=local_task_id,
            event_key=f"recovery:{task['task_id']}:{renewed['version']}",
            kind="host.recovery.restored",
            summary="Reference Host 已从中央任务状态恢复",
            payload={"leaseVersion": int(renewed["version"])},
        )
        return "restored"


def _local_terminal_status(value: str) -> str:
    if value in {"completed", "succeeded"}:
        return "succeeded"
    if value in {"canceled", "cancelled"}:
        return "canceled"
    if value == "unknown":
        return "unknown"
    return "failed"


def _error_code(exc: Exception) -> str:
    value = getattr(exc, "code", None) or exc.__class__.__name__
    return "".join(
        character if character.isalnum() else "_"
        for character in str(value).upper()
    ).strip("_")[:120]
