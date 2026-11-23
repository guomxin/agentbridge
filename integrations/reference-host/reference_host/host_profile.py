from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping


HOST_CONTRACT_SCHEMA = "agentbridge.host.v1"
HOST_VERSION = "0.1.0"
HOST_TYPE = "reference-host"
HOST_CONTEXT_META_KEY = "io.agentbridge/host-context"
HOST_PROFILE_META_KEY = "io.agentbridge/host-profile"
TASK_CONTEXT_META_KEY = "io.agentbridge/task"
PRIVATE_INTERACTION_META_KEY = "io.agentbridge/interaction"

HOST_CAPABILITY_NAMES = (
    "mcpApps",
    "privateResultMeta",
    "interactionPollResume",
    "taskTimeline",
    "proactiveDelivery",
    "artifactDelivery",
    "restartRecovery",
    "coordinatorLease",
    "batchTaskTimeline",
    "runtimeSignals",
    "boundedTransportRecovery",
)


@dataclass(frozen=True)
class CoordinatorImplementation:
    """Identify the implementation that owns this coordinator and its task leases."""
    name: str = HOST_TYPE
    version: str = HOST_VERSION
    endpoint_types: tuple[str, ...] = ("web_private",)


DEFAULT_IMPLEMENTATION = CoordinatorImplementation()


def host_instance_id() -> str:
    value = os.environ.get(
        "AGENTBRIDGE_REFERENCE_HOST_INSTANCE_ID",
        "reference-host-local",
    ).strip()
    if not value or len(value) > 160:
        raise ValueError("reference host instance ID is invalid")
    return value


def build_host_profile(*, instance_id: str | None = None, implementation: CoordinatorImplementation = DEFAULT_IMPLEMENTATION) -> dict[str, Any]:
    return {
        "schema": HOST_CONTRACT_SCHEMA,
        "hostInstanceId": instance_id or host_instance_id(),
        "implementation": {
            "name": implementation.name,
            "version": implementation.version,
        },
        "levels": ["L1", "L2", "L3"],
        "capabilities": {
            # Reference Host uses its audited private presenter. It does not
            # claim a native MCP Apps AppBridge implementation.
            "mcpApps": False,
            "privateResultMeta": True,
            "interactionPollResume": True,
            "taskTimeline": True,
            "proactiveDelivery": True,
            "artifactDelivery": True,
            "restartRecovery": True,
            "coordinatorLease": True,
            "batchTaskTimeline": True,
            "runtimeSignals": True,
            "boundedTransportRecovery": True,
        },
        "endpointTypes": list(implementation.endpoint_types),
    }


def host_context_meta(*, instance_id: str | None = None, implementation: CoordinatorImplementation = DEFAULT_IMPLEMENTATION) -> dict[str, Any]:
    return {
        HOST_CONTEXT_META_KEY: {
            "version": "1",
            "agentHost": implementation.name,
            "hostInstanceId": instance_id or host_instance_id(),
            "hostVersion": implementation.version,
        }
    }


def registration_meta(*, instance_id: str | None = None, implementation: CoordinatorImplementation = DEFAULT_IMPLEMENTATION) -> dict[str, Any]:
    return {
        **host_context_meta(instance_id=instance_id, implementation=implementation),
        HOST_PROFILE_META_KEY: build_host_profile(instance_id=instance_id, implementation=implementation),
    }


def task_call_meta(
    *,
    task_id: str,
    lease_version: int | None = None,
    host_run_id: str | None = None,
    tool_call_id: str | None = None,
    endpoint_id: str | None = None,
    conversation_ref: str | None = None,
    instance_id: str | None = None,
    implementation: CoordinatorImplementation = DEFAULT_IMPLEMENTATION,
) -> dict[str, Any]:
    task_context = {
        "taskId": _text(task_id, "taskId", 128),
    }
    optional = {
        "hostRunId": (host_run_id, 256),
        "toolCallId": (tool_call_id, 256),
        "endpointId": (endpoint_id, 128),
        "conversationRef": (conversation_ref, 1024),
    }
    for name, (value, maximum) in optional.items():
        if value is not None:
            task_context[name] = _text(value, name, maximum)
    if lease_version is not None:
        if isinstance(lease_version, bool) or int(lease_version) < 1:
            raise ValueError("coordinator lease version is invalid")
        task_context["coordinatorLeaseVersion"] = str(int(lease_version))
    return {
        **host_context_meta(instance_id=instance_id, implementation=implementation),
        TASK_CONTEXT_META_KEY: task_context,
    }


def require_accepted_level(
    negotiation: Mapping[str, Any],
    minimum_level: str = "L3",
) -> str:
    levels = ("L1", "L2", "L3")
    accepted = str(negotiation.get("acceptedLevel") or "").upper()
    minimum = str(minimum_level or "L1").upper()
    if accepted not in levels or minimum not in levels:
        raise RuntimeError("AgentBridge returned an invalid host compatibility level")
    if levels.index(accepted) < levels.index(minimum):
        raise PermissionError(
            f"AgentBridge accepted {accepted}; {minimum} is required"
        )
    return accepted


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, (str, int)):
        raise ValueError(f"{name} is required")
    normalized = str(value).strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized
