"""Verify centrally created Workspace commands and their original read receipts."""

QUERY = "oa_pending_query"
PREPARE = "oa_pending_prepare"
PENDING_LIST = "oa.workflow.pending.list"


async def command_snapshot(driver, local_id):
    req = driver.req
    snapshot = await driver.refresh_task_snapshot(local_id)
    task = snapshot.get("task") or {}
    command = (task.get("summary") or {}).get("command") or {}
    kind, capability = command.get("kind"), command.get("capabilityName")
    if (req["runId"] != req["commandKey"] or command.get("commandKey") != req["commandKey"]
            or task.get("taskId") != req["task"]["taskId"] or task.get("userSubject") != req["subject"]
            or task.get("agentHost") != driver.implementation.name or task.get("activeConversationRef") != req["conversation"]
            or task.get("originEndpointId") != req["endpoint"]
            or (snapshot.get("endpoint") or {}).get("endpointId") != req["endpoint"]
            or kind not in (QUERY, PREPARE) or not isinstance(capability, str) or not capability
            or (kind == QUERY) != (capability == PENDING_LIST)):
        raise PermissionError("Central command differs from the bound task")
    if req.get("commandKind", kind) != kind or req.get("commandCapability", capability) != capability:
        raise PermissionError("Central command scope changed")
    req.update(commandKind=kind, commandCapability=capability)
    return task, command


async def finish_query(driver, local_id, task, command):
    """Only the original terminal read can close a successful local query."""
    status = task.get("status")
    if status in {"failed", "canceled", "expired"}:
        phase = status
    elif status == "succeeded" and command.get("state") == "succeeded":
        operation = command.get("operationId")
        if not operation or task.get("currentOperationId") != operation:
            return False
        result = await driver.client("owner").call_tool("agentbridge_operation_get", {"operation_id": operation},
                                                        meta=driver.host_context(), recovery_class="read")
        observed = result.structured.get("operation") or {}
        if (result.is_error or observed.get("operationId") != operation or observed.get("taskId") != task["taskId"]
                or observed.get("userSubject") != task["userSubject"] or observed.get("capability") != PENDING_LIST
                or observed.get("status") != "succeeded"):
            return False
        phase = "verified"
    else:
        return False
    driver.state.finish_task(local_id, status="canceled" if status == "expired" else status, result_summary={"status": status})
    with driver.journal:
        driver.journal.execute("UPDATE coordinated SET phase=? WHERE run=?", (phase, driver.req["runId"]))
    return True


async def query_result(driver, local_id, result, resumed_from):
    data = result.structured
    task, command = await command_snapshot(driver, local_id)
    if result.is_error or data.get("capability") not in (None, PENDING_LIST):
        raise RuntimeError("Invalid fixed query receipt")
    if resumed_from:
        if (data.get("resumedFromInteractionId") != resumed_from or data.get("taskId") != task["taskId"]
                or (data.get("nextAction") or {}).get("type") != "original_request_completed"):
            raise RuntimeError("Credential resume did not return the original query")
    elif data.get("status") not in {"failed", "canceled", "expired"} and (data.get("commandKind") != QUERY or data.get("capability") != PENDING_LIST):
        raise RuntimeError("Missing fixed query scope")
    if data.get("status") == "requires_user_action" and not resumed_from:
        card = result.private_meta.get("io.agentbridge/interaction") or {}
        if (card.get("type") != "credential" or not card.get("interactionId")
                or task.get("currentInteractionId") != card["interactionId"]
                or (data.get("error") or {}).get("code") != "LOGIN_REQUIRED"):
            raise PermissionError("Fixed read requires the original OA credential card")
        return True
    if data.get("status") == "succeeded":
        receipt = data.get("queryResult") or {}
        if (receipt.get("kind") != QUERY or receipt.get("taskId") != task["taskId"]
                or receipt.get("endpointId") != driver.req["endpoint"]
                or type(receipt.get("count")) is not int or receipt["count"] < 0
                or type(receipt.get("complete")) is not bool or task.get("status") != "succeeded"
                or data.get("operationId") != command.get("operationId")):
            raise RuntimeError("Fixed query returned an incomplete result")
    elif data.get("status") not in {"failed", "canceled", "expired"} or task.get("status") not in {"failed", "canceled", "expired"}:
        raise RuntimeError("Fixed query has no confirmed terminal result")
    if not await finish_query(driver, local_id, task, command):
        raise RuntimeError("Central task did not verify the original query")
    return False
