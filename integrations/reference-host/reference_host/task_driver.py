from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Mapping
from uuid import uuid4

from .artifact_driver import ArtifactDriver
from .host_profile import (
    DEFAULT_IMPLEMENTATION,
    CoordinatorImplementation,
    PRIVATE_INTERACTION_META_KEY,
    host_context_meta,
    host_instance_id,
    registration_meta,
    require_accepted_level,
    task_call_meta,
)
from .identity import IdentityConfig, IdentityRegistry
from .interaction_driver import InteractionDriver, TERMINAL_INTERACTION_STATES
from .mcp_client import (
    AgentBridgeMcpClient,
    McpCallResult,
    is_transient_transport_error,
    recovery_class_for_tool,
)
from .state import ACTIVE_TASK_STATES, ReferenceHostState


FINAL_FAILURE_STATUSES = frozenset(
    {"failed", "rejected", "unknown", "outcome_unknown", "error"}
)
INTERACTION_STATUSES = frozenset(
    {"requires_user_action", "waiting_user", "pending", "processing"}
)


class ReferenceTaskDriver:
    def __init__(
        self,
        *,
        identities: IdentityRegistry,
        state: ReferenceHostState,
        mcp_url: str,
        ca_bundle: str | None = None,
        implementation: CoordinatorImplementation = DEFAULT_IMPLEMENTATION,
        instance_id: str | None = None,
        allow_insecure_tls: bool = False,
        private_transport: bool = False,
        load_tool_catalog: bool = True,
        poll_interval_seconds: float = 2.0,
        maximum_interaction_wait_seconds: float = 900.0,
        long_poll_seconds: float = 15.0,
    ) -> None:
        self.identities = identities
        self.state = state
        self.instance_id = instance_id or host_instance_id()
        self.implementation = implementation
        self._load_tool_catalog = load_tool_catalog
        self._clients = {
            identity.label: AgentBridgeMcpClient(
                mcp_url=mcp_url,
                token_provider=identity.token,
                ca_bundle=ca_bundle,
                allow_insecure_tls=allow_insecure_tls,
                private_transport=private_transport,
            )
            for identity in identities.list()
        }
        self._identity_profiles: dict[str, dict[str, Any]] = {}
        self._identity_errors: dict[str, str] = {}
        self._tools: dict[str, dict[str, dict[str, Any]]] = {}
        self._initialization_locks: dict[str, asyncio.Lock] = {}
        self._plans: dict[str, tuple[str, dict[str, Any]]] = {}
        self._interaction_urls: dict[str, str] = {}
        self._background_runs: dict[str, asyncio.Task[None]] = {}
        self._started_at = monotonic()
        self._transport_error_count = 0
        self._last_error_code: str | None = None
        self.interactions = InteractionDriver(
            self,
            poll_interval_seconds=poll_interval_seconds,
            maximum_wait_seconds=maximum_interaction_wait_seconds,
            long_poll_seconds=long_poll_seconds,
        )
        self.artifacts = ArtifactDriver(self)

    def host_context(self) -> dict[str, Any]:
        return host_context_meta(instance_id=self.instance_id, implementation=self.implementation)

    def _identity_profile_meta(self) -> dict[str, Any]:
        return self.host_context()

    async def initialize(self) -> dict[str, Any]:
        ready = []
        for identity in self.identities.list():
            try:
                await self.ensure_identity(identity.label)
            except Exception as exc:
                self._identity_errors[identity.label] = _error_code(exc)
            else:
                ready.append(identity.label)
        if not ready:
            raise RuntimeError("No Reference Host identity could register with AgentBridge")
        return {
            "ready": ready,
            "failed": dict(self._identity_errors),
        }

    async def close(self) -> None:
        runs = [task for task in self._background_runs.values() if not task.done()]
        for task in runs:
            task.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)
        self._background_runs.clear()
        await self.interactions.stop()
        closers = [
            closer()
            for client in self._clients.values()
            if callable(closer := getattr(client, "close", None))
        ]
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)

    async def ensure_identity(self, identity_label: str) -> dict[str, Any]:
        if identity_label in self._identity_profiles:
            return self._identity_profiles[identity_label]
        lock = self._initialization_locks.setdefault(identity_label, asyncio.Lock())
        async with lock:
            if identity_label in self._identity_profiles:
                return self._identity_profiles[identity_label]
            client = self.client(identity_label)
            profile_result = await client.call_tool(
                "agentbridge_server_profile",
                {},
                meta=registration_meta(instance_id=self.instance_id, implementation=self.implementation),
                recovery_class="read",
            )
            profile = profile_result.payload()
            require_accepted_level(profile.get("negotiation") or {}, "L3")
            identity_result = await client.call_tool(
                "agentbridge_host_identity_profile",
                {"agent_host": self.implementation.name},
                meta=self._identity_profile_meta(),
                recovery_class="read",
            )
            identity_profile = identity_result.payload()
            configured = self.identities.get(identity_label)
            if (configured.expected_subject is not None and
                (identity_profile.get("identity") or {}).get("userSubject") != configured.expected_subject):
                raise PermissionError("Central credential does not belong to the configured subject")
            require_accepted_level(identity_profile.get("host") or {}, "L3")
            identity_profile["planning"] = profile.get("planning")
            listed_tools = await client.list_tools() if self._load_tool_catalog else []
            allowed = set(
                (identity_profile.get("agentToolAccess") or {}).get(
                    "allowedToolNames"
                )
                or []
            )
            tools = {
                str(tool["name"]): tool
                for tool in listed_tools
                if tool.get("name") in allowed
                and not str(tool.get("name") or "").startswith(
                    "agentbridge_host_"
                )
                and tool.get("name") != "agentbridge_interaction_resume"
            }
            self._identity_profiles[identity_label] = identity_profile
            self._tools[identity_label] = tools
            self._identity_errors.pop(identity_label, None)
            return identity_profile

    async def start_task(
        self,
        *,
        identity_label: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        local_task_id, normalized_arguments = await self._prepare_task(
            identity_label=identity_label,
            tool_name=tool_name,
            arguments=arguments,
            title=title,
        )
        await self._bind_and_execute(
            local_task_id,
            normalized_arguments,
            propagate_error=True,
        )
        return self.view_task(local_task_id)

    async def enqueue_task(
        self,
        *,
        identity_label: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        local_task_id, normalized_arguments = await self._prepare_task(
            identity_label=identity_label,
            tool_name=tool_name,
            arguments=arguments,
            title=title,
        )
        run = asyncio.create_task(
            self._bind_and_execute(
                local_task_id,
                normalized_arguments,
                propagate_error=False,
            ),
            name=f"reference-host-task:{local_task_id}",
        )
        self._background_runs[local_task_id] = run
        run.add_done_callback(
            lambda _run, task_id=local_task_id: self._background_runs.pop(
                task_id, None
            )
        )
        return self.view_task(local_task_id)

    async def _prepare_task(
        self,
        *,
        identity_label: str,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        title: str | None,
    ) -> tuple[str, dict[str, Any]]:
        await self.ensure_identity(identity_label)
        tool = self.tool(identity_label, tool_name)
        identity = self.identities.get(identity_label)
        normalized_arguments = dict(arguments or {})
        recovery_class = recovery_class_for_tool(tool)
        restartable_arguments = (
            normalized_arguments if recovery_class == "read" else None
        )
        task = self.state.create_task(
            identity_label=identity_label,
            tool_name=tool_name,
            conversation_ref=identity.conversation_ref,
            title=title or str(tool.get("title") or tool_name),
            restartable_read_arguments=restartable_arguments,
        )
        local_task_id = task["local_task_id"]
        if recovery_class == "prepare":
            properties = (tool.get("inputSchema") or {}).get("properties") or {}
            if (
                "idempotency_key" in properties
                and "idempotency_key" not in normalized_arguments
            ):
                normalized_arguments["idempotency_key"] = (
                    f"{self.implementation.name}:{local_task_id}"
                )
        self._plans[local_task_id] = (tool_name, normalized_arguments)
        return local_task_id, normalized_arguments

    async def _bind_and_execute(
        self,
        local_task_id: str,
        normalized_arguments: dict[str, Any],
        *,
        propagate_error: bool,
    ) -> None:
        task = self.state.get_task(local_task_id)
        identity = self.identities.get(task["identity_label"])
        tool_name = task["tool_name"]
        try:
            central = await self.client(task["identity_label"]).call_tool(
                "agentbridge_host_task_ensure",
                {
                    "agent_host": self.implementation.name,
                    "host_task_key": (
                        f"{identity.conversation_ref}|{local_task_id}"
                    ),
                    "endpoint_key": identity.endpoint_key,
                    "client_type": "web",
                    "external_subject": identity.external_subject,
                    **({"account_id": identity.account_id} if identity.account_id else {}),
                    "conversation_ref": identity.conversation_ref,
                    "title": task["title"],
                    "label": f"{self.implementation.name} - {identity.label}",
                    "route": {},
                    "capabilities": [
                        self.implementation.name.replace("-","_"),
                        "trusted_interaction",
                        "task_timeline",
                        "artifact_delivery",
                    ],
                    "task_scope": "independent",
                },
                meta=self.host_context(),
                recovery_class="prepare",
            )
            payload = central.payload()
            central_task = payload.get("task") or {}
            endpoint = payload.get("endpoint") or {}
            lease = payload.get("coordinatorLease") or {}
            task_id = str(central_task.get("taskId") or "").strip()
            endpoint_id = str(endpoint.get("endpointId") or "").strip()
            lease_version = int(lease.get("version") or 0)
            if identity.expected_endpoint is not None and endpoint_id != identity.expected_endpoint:
                raise PermissionError("Central task belongs to a different endpoint")
            if not task_id or not endpoint_id or lease_version < 1:
                raise RuntimeError(
                    "AgentBridge did not return a complete host task binding"
                )
            self.state.bind_central_task(
                local_task_id=local_task_id,
                task_id=task_id,
                endpoint_id=endpoint_id,
                lease_version=lease_version,
            )
            self.state.append_event(
                local_task_id=local_task_id,
                event_key=f"central:{task_id}:bound",
                kind="host.task.bound",
                summary="已建立中央任务与协调 Lease",
                payload={"taskId": task_id, "leaseVersion": lease_version},
            )
            await self._execute_plan(local_task_id, tool_name, normalized_arguments)
        except Exception as exc:
            await self._fail_local_task(local_task_id, exc)
            if propagate_error:
                raise

    async def _execute_plan(
        self,
        local_task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        task = self.state.get_task(local_task_id)
        lease = await self.renew_coordinator_lease(local_task_id)
        run_ref = f"reference:{local_task_id}:{uuid4()}"
        result = await self.call_for_task(
            local_task_id,
            tool_name,
            arguments,
            lease=lease,
            host_run_id=run_ref,
            tool_call_id=run_ref,
            recovery_class=recovery_class_for_tool(
                self.tool(task["identity_label"], tool_name)
            ),
        )
        await self.process_tool_result(local_task_id, result)

    async def call_for_task(
        self,
        local_task_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        lease: Mapping[str, Any] | None = None,
        host_run_id: str | None = None,
        tool_call_id: str | None = None,
        recovery_class: str = "unsafe",
    ) -> McpCallResult:
        task = self.state.get_task(local_task_id)
        if not task.get("task_id") or not task.get("endpoint_id"):
            raise RuntimeError("reference task is not bound to AgentBridge")
        lease_version = int(
            (lease or {}).get("version") or task.get("lease_version") or 0
        )
        return await self.client(task["identity_label"]).call_tool(
            tool_name,
            dict(arguments),
            meta=task_call_meta(
                implementation=self.implementation,
                task_id=task["task_id"],
                lease_version=lease_version,
                host_run_id=host_run_id,
                tool_call_id=tool_call_id,
                endpoint_id=task["endpoint_id"],
                conversation_ref=task["conversation_ref"],
                instance_id=self.instance_id,
            ),
            recovery_class=recovery_class,
        )

    async def renew_coordinator_lease(
        self,
        local_task_id: str,
    ) -> dict[str, Any]:
        task = self.state.get_task(local_task_id)
        result = await self.client(task["identity_label"]).call_tool(
            "agentbridge_host_coordinator_lease_acquire",
            {
                "task_id": task["task_id"],
                "lease_seconds": 600,
                "takeover": False,
                "expected_version": task.get("lease_version"),
            },
            meta=self.host_context(),
            recovery_class="unsafe",
        )
        lease = result.payload().get("coordinatorLease") or {}
        if lease.get("hostInstanceId") != self.instance_id:
            raise PermissionError(
                "Reference Host does not own the task coordinator Lease"
            )
        version = int(lease.get("version") or 0)
        if version < 1:
            raise RuntimeError("AgentBridge returned an invalid coordinator Lease")
        self.state.update_task(local_task_id, lease_version=version)
        return lease

    async def process_tool_result(
        self,
        local_task_id: str,
        result: McpCallResult,
        *,
        resumed_from: str | None = None,
    ) -> None:
        task = self.state.get_task(local_task_id)
        payload = result.payload()
        operation_ids, interaction_ids = _collect_references(payload)
        private_interaction = result.private_meta.get(
            PRIVATE_INTERACTION_META_KEY
        )
        interaction = (
            private_interaction
            if isinstance(private_interaction, Mapping)
            else payload.get("interaction")
        )
        if not isinstance(interaction, Mapping) and interaction_ids:
            interaction = {"interactionId": interaction_ids[-1]}
        if operation_ids or interaction_ids:
            await self.call_for_task(
                local_task_id,
                "agentbridge_host_task_observe",
                {
                    "agent_host": self.implementation.name,
                    "task_id": task["task_id"],
                    "operation_ids": operation_ids,
                    "interaction_ids": interaction_ids,
                },
                recovery_class="unsafe",
            )
        if isinstance(interaction, Mapping) and interaction.get("interactionId"):
            presented = await self.present_interaction(
                local_task_id,
                str(interaction["interactionId"]),
            )
            self.state.update_task(
                local_task_id,
                status="waiting_user",
                active_interaction_id=presented["interactionId"],
                result_summary=_result_summary(payload),
            )
            self.state.append_event(
                local_task_id=local_task_id,
                event_key=(
                    f"interaction:{presented['interactionId']}:"
                    f"{presented.get('state') or 'pending'}"
                ),
                kind="interaction.waiting",
                summary=str(
                    presented.get("title") or "等待完成可信交互"
                ),
                payload=_public_interaction(presented),
            )
            self.interactions.start(local_task_id, presented)
            return

        next_action = payload.get("nextAction")
        next_action = next_action if isinstance(next_action, Mapping) else {}
        if next_action.get("type") == "retry_original_request":
            plan = self._plans.get(local_task_id)
            if plan is None:
                restart_arguments = task.get("restartable_read_arguments") or {}
                if restart_arguments and task.get("tool_name"):
                    plan = (task["tool_name"], dict(restart_arguments))
            if plan and plan[0] not in {
                "oa_session_login",
                "taihua_session_login",
                "yuque_session_login",
                "smartlight_session_login",
            }:
                self.state.append_event(
                    local_task_id=local_task_id,
                    event_key=f"continuation:{resumed_from or local_task_id}",
                    kind="task.continuing",
                    summary="登录已完成，正在自动继续原请求",
                )
                await self._execute_plan(local_task_id, plan[0], dict(plan[1]))
                return

        await self.refresh_task_snapshot(local_task_id)
        status = str(payload.get("status") or "succeeded").lower()
        error = payload.get("error")
        if result.is_error or status in FINAL_FAILURE_STATUSES:
            final_status = "unknown" if status in {"unknown", "outcome_unknown"} else "failed"
            self.state.finish_task(
                local_task_id,
                status=final_status,
                result_summary=_result_summary(payload),
                error_summary=(error if isinstance(error, Mapping) else {}),
            )
            summary = _error_summary(error) or "业务调用未成功"
        else:
            self.state.finish_task(
                local_task_id,
                status="succeeded",
                result_summary=_result_summary(payload),
            )
            summary = "业务调用已完成"
        self.state.append_event(
            local_task_id=local_task_id,
            event_key=f"result:{resumed_from or local_task_id}:{status}",
            kind="task.result",
            summary=summary,
            payload=_result_summary(payload),
        )
        if not operation_ids and not interaction_ids:
            await self._finish_central_task(
                local_task_id,
                "failed" if status in FINAL_FAILURE_STATUSES else "succeeded",
                error=error if isinstance(error, Mapping) else None,
            )

    async def present_interaction(
        self,
        local_task_id: str,
        interaction_id: str,
    ) -> dict[str, Any]:
        task = self.state.get_task(local_task_id)
        result = await self.client(task["identity_label"]).call_tool(
            "agentbridge_host_interaction_present",
            {
                "agent_host": self.implementation.name,
                "endpoint_key": self.identities.get(
                    task["identity_label"]
                ).endpoint_key,
                "interaction_id": interaction_id,
            },
            meta=self.host_context(),
            recovery_class="read",
        )
        interaction = result.payload().get("interaction") or {}
        url = ((interaction.get("presentation") or {}).get("url"))
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RuntimeError("AgentBridge returned no trusted interaction URL")
        self._interaction_urls[local_task_id] = url
        return dict(interaction)

    async def interaction_url(
        self,
        local_task_id: str,
        *,
        refresh: bool = False,
    ) -> str:
        if refresh:
            self._interaction_urls.pop(local_task_id, None)
        value = self._interaction_urls.get(local_task_id)
        if value:
            return value
        task = self.state.get_task(local_task_id)
        interaction_id = task.get("active_interaction_id")
        if not interaction_id:
            raise KeyError("reference task has no active trusted interaction")
        await self.present_interaction(local_task_id, interaction_id)
        return self._interaction_urls[local_task_id]

    async def get_interaction(
        self,
        local_task_id: str,
        interaction_id: str,
        *,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        task = self.state.get_task(local_task_id)
        arguments: dict[str, Any] = {"interaction_id": interaction_id}
        if isinstance(wait_seconds, (int, float)) and not isinstance(wait_seconds, bool) and wait_seconds > 0:
            arguments["wait_seconds"] = round(min(float(wait_seconds), 20.0), 3)
        result = await self.client(task["identity_label"]).call_tool(
            "agentbridge_interaction_get",
            arguments,
            meta=self.host_context(),
            recovery_class="read",
        )
        interaction = result.private_meta.get(PRIVATE_INTERACTION_META_KEY)
        if not isinstance(interaction, Mapping):
            interaction = result.payload().get("interaction")
        if not isinstance(interaction, Mapping):
            raise RuntimeError("AgentBridge returned no interaction state")
        return dict(interaction)

    async def note_interaction_state(
        self,
        local_task_id: str,
        interaction: Mapping[str, Any],
    ) -> None:
        interaction_id = str(interaction.get("interactionId") or "unknown")
        state = str(interaction.get("state") or "unknown")
        self.state.append_event(
            local_task_id=local_task_id,
            event_key=f"interaction:{interaction_id}:{state}",
            kind="interaction.updated",
            summary=_interaction_state_summary(interaction),
            payload=_public_interaction(interaction),
        )

    async def resume_interaction(
        self,
        local_task_id: str,
        interaction_id: str,
    ) -> None:
        lease = await self.renew_coordinator_lease(local_task_id)
        result = await self.call_for_task(
            local_task_id,
            "agentbridge_interaction_resume",
            {
                "interaction_id": interaction_id,
                "idempotency_key": f"{self.implementation.name}:{interaction_id}",
            },
            lease=lease,
            recovery_class="unsafe",
        )
        self._interaction_urls.pop(local_task_id, None)
        await self.process_tool_result(
            local_task_id,
            result,
            resumed_from=interaction_id,
        )

    async def close_interaction(
        self,
        local_task_id: str,
        interaction: Mapping[str, Any],
    ) -> None:
        state = str(interaction.get("state") or "failed").lower()
        self._interaction_urls.pop(local_task_id, None)
        if state in TERMINAL_INTERACTION_STATES:
            self.state.finish_task(
                local_task_id,
                status="canceled" if state == "declined" else "failed",
                error_summary={"code": state.upper()},
            )
            try:
                await self._finish_central_task(
                    local_task_id,
                    "failed",
                    error={
                        "code": state.upper(),
                        "message": _interaction_state_summary(interaction),
                    },
                )
            except Exception as exc:
                self.state.append_event(
                    local_task_id=local_task_id,
                    event_key=(
                        "interaction:"
                        f"{interaction.get('interactionId') or 'unknown'}:"
                        f"central-finish:{_error_code(exc)}"
                    ),
                    kind="interaction.central_finish_failed",
                    summary="交互终态已保存，但中央任务收口暂时失败",
                    payload={"errorCode": _error_code(exc)},
                )
        else:
            snapshot = await self.refresh_task_snapshot(local_task_id)
            central_status = str(
                (snapshot.get("task") or {}).get("status") or ""
            ).casefold()
            if central_status in {"failed", "unknown"}:
                self.state.finish_task(
                    local_task_id,
                    status="unknown" if central_status == "unknown" else "failed",
                    error_summary={"code": central_status.upper()},
                )
            else:
                self.state.finish_task(
                    local_task_id,
                    status="succeeded",
                    result_summary={"interactionState": state},
                )

    async def mark_poll_deadline(
        self,
        local_task_id: str,
        interaction_id: str,
    ) -> None:
        self.state.update_task(local_task_id, status="waiting_user")
        self.state.append_event(
            local_task_id=local_task_id,
            event_key=f"interaction:{interaction_id}:poll-deadline",
            kind="interaction.poll_paused",
            summary="后台轮询已暂停，任务仍可从中央状态恢复",
        )

    async def mark_interaction_driver_error(
        self,
        local_task_id: str,
        interaction_id: str,
        exc: Exception,
    ) -> None:
        self.state.update_task(local_task_id, status="recovering")
        self.state.append_event(
            local_task_id=local_task_id,
            event_key=(
                f"interaction:{interaction_id}:driver-error:"
                f"{exc.__class__.__name__}"
            ),
            kind="interaction.driver_error",
            summary="可信交互状态检查暂时失败，等待恢复",
            payload={"errorCode": _error_code(exc)},
        )

    async def refresh_task_snapshot(
        self,
        local_task_id: str,
    ) -> dict[str, Any]:
        task = self.state.get_task(local_task_id)
        if not task.get("task_id"):
            return {}
        identity = self.identities.get(task["identity_label"])
        result = await self.client(task["identity_label"]).call_tool(
            "agentbridge_host_task_snapshot",
            {
                "agent_host": self.implementation.name,
                "endpoint_key": identity.endpoint_key,
                "task_id": task["task_id"],
                "event_limit": 200,
                "artifact_limit": 50,
            },
            meta=self.host_context(),
            recovery_class="read",
        )
        snapshot = result.payload()
        events = snapshot.get("events") or []
        maximum_sequence = int(task.get("last_sequence") or 0)
        for event in events:
            if not isinstance(event, Mapping):
                continue
            sequence = int(event.get("sequence") or 0)
            maximum_sequence = max(maximum_sequence, sequence)
            self.state.append_event(
                local_task_id=local_task_id,
                event_key=f"central-event:{event.get('eventId')}",
                kind=str(event.get("kind") or "task.event"),
                summary=_central_event_summary(event),
                payload={"centralSequence": sequence},
                created_at=event.get("createdAt"),
            )
        self.state.update_task(local_task_id, last_sequence=maximum_sequence)
        self.artifacts.update_from_snapshot(
            local_task_id,
            [item for item in snapshot.get("artifacts") or [] if isinstance(item, Mapping)],
        )
        return snapshot

    async def _finish_central_task(
        self,
        local_task_id: str,
        outcome: str,
        *,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        task = self.state.get_task(local_task_id)
        lease = await self.renew_coordinator_lease(local_task_id)
        await self.call_for_task(
            local_task_id,
            "agentbridge_host_task_finish",
            {
                "agent_host": self.implementation.name,
                "task_id": task["task_id"],
                "outcome": outcome,
                "reason": "reference_host_tool_completed",
                "error_code": (error or {}).get("code"),
                "message": (error or {}).get("message"),
                "causation_ref": local_task_id,
            },
            lease=lease,
            recovery_class="unsafe",
        )

    async def _fail_local_task(
        self,
        local_task_id: str,
        exc: Exception,
    ) -> None:
        if is_transient_transport_error(exc):
            self._transport_error_count += 1
        self._last_error_code = _error_code(exc)
        self.state.finish_task(
            local_task_id,
            status="failed",
            error_summary={
                "code": _error_code(exc),
                "message": str(exc)[:500],
            },
        )
        self.state.append_event(
            local_task_id=local_task_id,
            event_key=f"local:{local_task_id}:failed:{_error_code(exc)}",
            kind="task.failed",
            summary="Reference Host 未能完成本次任务",
            payload={"errorCode": _error_code(exc)},
        )

    def client(self, identity_label: str) -> AgentBridgeMcpClient:
        try:
            return self._clients[identity_label]
        except KeyError as exc:
            raise KeyError(f"reference-host identity not found: {identity_label}") from exc

    def tool(self, identity_label: str, tool_name: str) -> dict[str, Any]:
        try:
            return self._tools[identity_label][tool_name]
        except KeyError as exc:
            raise PermissionError(
                f"tool is not available to this identity: {tool_name}"
            ) from exc

    def list_tools(self, identity_label: str) -> list[dict[str, Any]]:
        tools = self._tools.get(identity_label) or {}
        return [tools[name] for name in sorted(tools)]

    def view_task(self, local_task_id: str) -> dict[str, Any]:
        task = self.state.get_task(local_task_id)
        return {
            "localTaskId": task["local_task_id"],
            "identityLabel": task["identity_label"],
            "toolName": task["tool_name"],
            "taskId": task.get("task_id"),
            "title": task["title"],
            "status": task["status"],
            "activeInteraction": (
                {
                    "interactionId": task["active_interaction_id"],
                    "openPath": f"/api/tasks/{local_task_id}/interaction/open",
                }
                if task.get("active_interaction_id")
                else None
            ),
            "artifacts": self.artifacts.list_for_task(local_task_id),
            "result": task["result_summary"],
            "error": task["error_summary"],
            "createdAt": task["created_at"],
            "updatedAt": task["updated_at"],
            "finishedAt": task.get("finished_at"),
        }

    def list_task_views(
        self,
        *,
        identity_label: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return [
            self.view_task(task["local_task_id"])
            for task in self.state.list_tasks(
                identity_label=identity_label,
                active_only=active_only,
                limit=limit,
            )
        ]

    def runtime_snapshot(self, identity_label: str) -> dict[str, Any]:
        counts = self.state.counts(identity_label=identity_label)
        snapshot = {
            "status": "healthy",
            "observedAt": datetime.now(timezone.utc).isoformat(),
            "uptimeSeconds": round(max(monotonic() - self._started_at, 0.0), 3),
            "activeTaskCount": counts["active"],
            "waitingInteractionCount": counts["waiting"],
            "transportErrorCount": self._transport_error_count,
        }
        if self._last_error_code:
            snapshot["lastErrorCode"] = self._last_error_code
        return snapshot

    def identity_errors(self) -> dict[str, str]:
        return dict(self._identity_errors)


def _collect_references(value: Any) -> tuple[list[str], list[str]]:
    operations: list[str] = []
    interactions: list[str] = []

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 10 or len(operations) + len(interactions) >= 40:
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key == "operationId" and isinstance(child, str):
                    if child and child not in operations:
                        operations.append(child[:256])
                elif key == "interactionId" and isinstance(child, str):
                    if child and child not in interactions:
                        interactions.append(child[:256])
                else:
                    visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value)
    return operations[:20], interactions[:20]


def _result_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    result = result if isinstance(result, Mapping) else {}
    summary = {
        "status": str(payload.get("status") or "unknown"),
    }
    for source in (payload, result):
        for name in (
            "count",
            "total",
            "requestedCount",
            "preparedCount",
            "failedCount",
        ):
            value = source.get(name)
            if isinstance(value, int) and name not in summary:
                summary[name] = value
    error = payload.get("error")
    if isinstance(error, Mapping):
        summary["errorCode"] = error.get("code")
        summary["message"] = str(error.get("message") or "")[:500]
    return summary


def _public_interaction(interaction: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "interactionId": interaction.get("interactionId"),
        "type": interaction.get("type"),
        "state": interaction.get("state"),
        "title": interaction.get("title"),
        "message": interaction.get("message"),
        "expiresAt": interaction.get("expiresAt"),
    }


def _interaction_state_summary(interaction: Mapping[str, Any]) -> str:
    state = str(interaction.get("state") or "unknown")
    labels = {
        "pending": "等待用户操作",
        "processing": "可信页面正在处理",
        "completed": "可信交互已完成",
        "declined": "用户已取消",
        "expired": "可信交互已过期",
        "failed": "可信交互失败",
        "superseded": "可信交互已被替代",
    }
    return labels.get(state, f"可信交互状态：{state}")


def _central_event_summary(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "task.event")
    return {
        "task.created": "中央任务已创建",
        "task.interaction.waiting": "中央任务等待用户操作",
        "task.interaction.completed": "中央可信交互已完成",
        "task.operation.succeeded": "业务操作已成功",
        "task.operation.failed": "业务操作已失败",
        "task.operation.outcome_unknown": "业务操作结果未知",
        "task.artifact.ready": "任务文件已就绪",
        "task.completed": "中央任务已完成",
        "task.failed": "中央任务已失败",
    }.get(kind, kind)


def _error_summary(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    message = str(value.get("message") or "").strip()
    return message[:500] if message else str(value.get("code") or "") or None


def _error_code(exc: Exception) -> str:
    value = getattr(exc, "code", None) or exc.__class__.__name__
    return "".join(
        character if character.isalnum() else "_"
        for character in str(value).upper()
    ).strip("_")[:120]
