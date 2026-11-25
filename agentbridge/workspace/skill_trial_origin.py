"""Validate the original Workspace dispatch before a host operates a skill trial."""


def verify_skill_origin(service, subject, instance, dispatch_id, origin, *, host_type="nanobot"):
    if not isinstance(origin, dict) or set(origin) != {
        "endpointId",
        "endpointKey",
        "conversationRef",
        "requestKey",
    }:
        raise PermissionError("缺少原试用端点")
    row = service.workspace.get_host_dispatch(dispatch_id, user_subject=subject)
    binding = service.workspace.dispatch_binding(row)
    if (
        host_type not in {"nanobot", "pi"}
        or row["agent_host"] != host_type
        or row["instance_id"] != instance
        or (
            binding["endpoint_id"],
            binding["endpoint_key"],
            binding["session_key"],
            row["idempotency_key"],
        )
        != (
            origin["endpointId"],
            origin["endpointKey"],
            origin["conversationRef"],
            origin["requestKey"],
        )
    ):
        raise PermissionError("短试用不属于此次端点、实例和请求")
