"""Explicit, endpoint-owned OA reads through the existing durable L3 runner.

The HTTP boundary only queues this closed command. No capability, arguments,
selection, batch or write authority can be supplied by a caller. The OA pending
Skill is deliberately not bound: its manifest excludes list-only queries. Any
later selection still passes the existing reviewed Skill and write guards.
Workspace may also persist the user's closed query text on its timeline; only
its hash joins the frozen command, never the text or any model dispatch.
"""
from __future__ import annotations

from datetime import timedelta
import json
import re
import unicodedata
from uuid import uuid4

from agentbridge.core import actionable_results as actions
from agentbridge.core.store_transactions import store_transaction

COMMAND_KIND = "oa_pending_query"
CAPABILITY = "oa.workflow.pending.list"
QUERY_TTL_SECONDS = 600
CURRENT_OWN_OA_PENDING = frozenset({
    "查看oa待办", "查询oa待办", "查oa待办", "查一下oa待办",
    "查看我的oa待办", "查询我的oa待办", "查看当前oa待办", "查询当前oa待办",
    "查看我当前的oa待办", "查询我当前的oa待办",
    "查看本人当前oa待办", "查询本人当前oa待办",
    "帮我查一下oa待办", "帮我查询oa待办", "帮我查看oa待办",
})


def _is_current_own_pending_query(message):
    if not isinstance(message, str) or len(message) > 80:
        return False
    plain = unicodedata.normalize("NFKC", message).strip().replace(" ", "").replace("\t", "")
    plain = re.sub(r"[。.!?]$", "", plain).lower()
    return plain.removesuffix("事项") in CURRENT_OWN_OA_PENDING


def _body(body, *, allow_user_message=False):
    allowed = ({"requestKey"}, {"requestKey", "userMessage"}) if allow_user_message else ({"requestKey"},)
    if not isinstance(body, dict) or set(body) not in allowed or not isinstance(body["requestKey"], str) \
            or not re.fullmatch(r"[a-zA-Z0-9_:-]{16,128}", body["requestKey"]):
        actions._fail("ACTION_REQUEST_INVALID", "查询请求包含不支持的字段或无效请求标识。")
    if "userMessage" in body and not _is_current_own_pending_query(body["userMessage"]):
        actions._fail("ACTION_REQUEST_INVALID", "快捷查询文字必须是查询本人当前 OA 待办的固定表达。")


def queue_query(service, account, body):
    _body(body, allow_user_message=True)
    with store_transaction(service.tasks) as connection:
        workspace = dict(actions._account(connection, account))
        return _queue(service, account["user_subject"], workspace, body, connection)


def queue_endpoint_query(service, subject, endpoint_id, body, *, binding_hash, source_task_id, view_hash, remaining_context=None):
    """Called after the result-view ticket authenticates its original source."""
    _body(body)
    if not isinstance(view_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", view_hash):
        actions._fail("ACTION_REQUEST_INVALID", "原查询入口标识无效。")
    with store_transaction(service.tasks) as connection:
        endpoint = actions._telegram_endpoint(connection, subject, endpoint_id)
        source = actions._owned_task(connection, subject, source_task_id)
        if endpoint["_binding_hash"] != binding_hash or source["origin_endpoint_id"] != endpoint_id \
                or source["active_conversation_ref"] != endpoint["conversation_ref"]:
            actions._fail("ACTION_ENDPOINT_INVALID", "查询请求必须来自原 Telegram 私聊。")
        workspace = {"endpoint_id": endpoint_id, "endpoint_key": endpoint["endpoint_key"],
            "openclaw_session_key": endpoint["conversation_ref"], "_telegram": True,
            "_binding_hash": binding_hash, "_request_scope": endpoint_id,
            "_source_task_id": source_task_id, "_view_hash": view_hash}
        if remaining_context is not None:
            workspace["_remaining_context"] = remaining_context
        return _queue(service, subject, workspace, body, connection)


def _queue(service, subject, workspace, body, connection):
    service.user_grants.require_capability(subject, CAPABILITY)
    scope = workspace.get("_request_scope") or workspace["account_id"]
    identity = {"action": COMMAND_KIND, "endpointId": workspace["endpoint_id"],
        "sourceTaskId": workspace.get("_source_task_id"), "viewHash": workspace.get("_view_hash")}
    remaining = workspace.get("_remaining_context")
    if remaining is not None:
        from agentbridge.core.oa_batch_remaining import validate_context
        validate_context(service, connection, subject, remaining)
        identity["remainingContextHash"] = actions._hash(remaining)
    message = body.get("userMessage")
    if message is not None:
        identity["messageHash"] = actions._hash({"text": message})
    request_hash = actions._hash({"scope": scope, "requestKey": body["requestKey"]})
    identity_hash = actions._hash(identity)
    host_key = "quick-query:" + actions._hash({"scope": scope, "requestKey": body["requestKey"]})
    existing = connection.execute("SELECT task_id FROM agent_tasks WHERE user_subject=? AND agent_host='openclaw' AND host_task_key=?",
        (subject, host_key)).fetchone()
    if existing:
        row = actions._owned_task(connection, subject, existing[0])
        old = json.loads(row["summary_json"]).get("command") or {}
        _, command = actions._load_command(connection, subject, existing[0], old.get("commandKey"))
        if command.get("requestIdentity") != identity or row["origin_endpoint_id"] != workspace["endpoint_id"] \
                or row["active_conversation_ref"] != workspace["openclaw_session_key"]:
            actions._fail("ACTION_REQUEST_CONFLICT", "原查询请求不属于当前入口。")
        _contract(service, command)
        receipt = {"taskId": row["task_id"], "commandKey": command["commandKey"], "status": "existing"}
        if message is not None:
            receipt["userMessageKey"] = _append_user_message(service, subject, workspace, message, request_hash, connection)
        return receipt
    count = connection.execute("SELECT COUNT(*) FROM agent_tasks WHERE user_subject=? AND origin_endpoint_id=? "
        "AND status IN ('active','running','waiting_user') AND json_extract(summary_json,'$.command.kind')=?",
        (subject, workspace["endpoint_id"], COMMAND_KIND)).fetchone()[0]
    if count >= 4:
        actions._fail("ACTION_PREPARATION_BUSY", "当前会话已有查询正在等待，请恢复原查询或稍后再试。")
    message_key = None
    if message is not None:
        # The message precedes the task card and commits with the closed read
        # intent. It never creates a model dispatch or a business authorization.
        message_key = _append_user_message(service, subject, workspace, message, request_hash, connection)
    command = {"kind": COMMAND_KIND, "commandKey": str(uuid4()), "requestIdentity": identity,
        "capabilityName": CAPABILITY, "capabilityVersion": service.registry.get(CAPABILITY).version,
        "arguments": {}, "requestedAt": actions._now().isoformat(), "state": "pending"}
    if remaining is not None:
        command["remainingContext"] = remaining
    if workspace.get("_telegram"):
        command.update(originKind="telegram", endpointBindingHash=workspace["_binding_hash"],
            sourceTaskId=workspace["_source_task_id"], viewHash=workspace["_view_hash"])
    command["intentHash"] = actions._hash(actions._intent(command))
    task, _ = service.tasks.ensure_task(user_subject=subject, agent_host="openclaw", host_task_key=host_key,
        origin_endpoint_id=workspace["endpoint_id"], active_conversation_ref=workspace["openclaw_session_key"],
        title=f"重新查询原批次剩余{len(remaining['targets'])}项" if remaining else "查询 OA 待办",
        summary={"command": command}, connection=connection)
    if workspace.get("_telegram"):
        subscription = connection.execute("SELECT subscription_id,event_filters_json FROM task_subscriptions "
            "WHERE task_id=? AND endpoint_id=? AND user_subject=? AND state='active'",
            (task["task_id"], workspace["endpoint_id"], subject)).fetchone()
        if subscription:
            filters = json.loads(subscription["event_filters_json"])
            if "task.prepare.requested" not in filters and "*" not in filters:
                connection.execute("UPDATE task_subscriptions SET event_filters_json=? WHERE subscription_id=?",
                    (actions._json([*filters, "task.prepare.requested"]), subscription["subscription_id"]))
        service.tasks._append_event(connection, task_id=task["task_id"], user_subject=subject,
            event_type="task.result_view.refreshed", payload={"viewTokenHash": workspace["_view_hash"],
                "sourceTaskId": workspace["_source_task_id"]}, causation_ref=workspace["_view_hash"],
            created_at=command["requestedAt"])
    service.tasks._append_event(connection, task_id=task["task_id"], user_subject=subject,
        event_type="task.prepare.requested", payload={"commandKey": command["commandKey"],
            "intentHash": command["intentHash"], "requestKeyHash": request_hash, "requestIdentityHash": identity_hash,
            "queryOnly": True}, causation_ref=command["commandKey"], created_at=command["requestedAt"])
    return {"taskId": task["task_id"], "commandKey": command["commandKey"], "status": "queued",
        **({"userMessageKey": message_key} if message_key else {})}


def _append_user_message(service, subject, workspace, message, request_hash, connection):
    message_key = "workspace:quick-query:user:" + request_hash
    entry, _ = service.tasks._append_timeline_message_in_connection(connection,
        user_subject=subject, source_endpoint_id=workspace["endpoint_id"], message_key=message_key,
        role="user", text=message)
    if (entry["role"] != "user" or entry["text"] != message.strip()
            or entry["source_endpoint_id"] != workspace["endpoint_id"] or entry.get("task_id") is not None
            or entry.get("payload") != {}):
        actions._fail("ACTION_REQUEST_CONFLICT", "原查询文字与当前请求不一致。")
    return message_key


def _contract(service, command):
    if command.get("kind") != COMMAND_KIND or command.get("capabilityName") != CAPABILITY \
            or command.get("arguments") != {} or command.get("capabilityVersion") != service.registry.get(CAPABILITY).version:
        actions._fail("ACTION_COMMAND_INVALID", "快捷查询合同已变化，请发起新的查询。")


def _origin(service, connection, subject, task, command):
    if command.get("originKind") == "telegram":
        endpoint = actions._telegram_endpoint(connection, subject, task["origin_endpoint_id"])
        if endpoint["_binding_hash"] != command.get("endpointBindingHash") \
                or endpoint["conversation_ref"] != task["active_conversation_ref"]:
            actions._fail("ACTION_ENDPOINT_INVALID", "原 Telegram 查询会话已变化。")
    else:
        account = connection.execute("SELECT * FROM workspace_accounts WHERE endpoint_id=? AND user_subject=?",
            (task["origin_endpoint_id"], subject)).fetchone()
        if account is None or account["openclaw_session_key"] != task["active_conversation_ref"]:
            actions._fail("ACTION_ENDPOINT_INVALID", "原 Workspace 查询会话已变化。")
        actions._account(connection, dict(account))


def _continued_operation(service, subject, task_id, command, original):
    """Recover only this query's durable login read, never an arbitrary last read."""
    if (original.get("error") or {}).get("code") != "LOGIN_REQUIRED":
        return original
    task = service.tasks.get_task(task_id, user_subject=subject)
    current_id = task.get("current_operation_id")
    if not current_id or current_id == original["operation_id"]:
        return original
    current = service.operations.get(current_id)
    direct_key = f"quick-query-session-ready:{task_id}:{command['commandKey']}"
    valid_keys = {direct_key}
    interaction_id = task.get("current_interaction_id")
    if interaction_id:
        preceding = service.tasks.operation_before_interaction(
            task_id=task_id, user_subject=subject, interaction_id=interaction_id)
        if preceding:
            parent = service.operations.get(preceding["operation_id"])
            if parent["operation_id"] == original["operation_id"] or (
                parent.get("idempotency_key") == direct_key and parent["capability_name"] == CAPABILITY
                and parent["input_hash"] == actions._hash({})
            ):
                valid_keys.add(f"login-read:{interaction_id}:{parent['operation_id']}")
    if (current["user_subject"] != subject or current["capability_name"] != CAPABILITY
            or current["capability_version"] != command["capabilityVersion"]
            or current["input_hash"] != actions._hash({}) or current.get("idempotency_key") not in valid_keys
            or service.tasks.task_id_for_operation(current_id, user_subject=subject) != task_id):
        actions._fail("ACTION_COMMAND_INVALID", "原查询登录续查记录不一致。")
    return current


def _login_response(service, subject, task_id, command, response, context):
    """Expose a trusted OA credential on the same task and reuse Central resume."""
    if response.get("status") != "requires_user_action" or (response.get("error") or {}).get("code") != "LOGIN_REQUIRED":
        return response
    task = service.tasks.get_task(task_id, user_subject=subject)
    if task["status"] not in {"active", "running", "waiting_user"}:
        return {**response, "status": "failed", "interaction": None, "nextAction": None,
            "error": {"code": "ACTION_COMMAND_NOT_PENDING", "message": "原查询已结束，请查看原任务。"}}
    interaction_id = task.get("current_interaction_id")
    if interaction_id:
        record = service.interactions.get(interaction_id, user_subject=subject)
        preceding = service.tasks.operation_before_interaction(
            task_id=task_id, user_subject=subject, interaction_id=interaction_id)
        if (record["interaction_type"] == "credential" and record["system_id"] == "oa"
                and preceding and preceding["operation_id"] == response.get("operationId")):
            interaction = service.get_interaction(user_subject=subject, interaction_id=interaction_id)["interaction"]
            if interaction.get("resume", {}).get("ready"):
                # This is the existing persisted read continuation, with its own
                # idempotency key, grant check and original-task notification.
                return service.resume_interaction(user_subject=subject, interaction_id=interaction_id)
            return {**response, "interaction": interaction, "nextAction": {
                "type": "wait_for_interaction", "interactionId": interaction_id}}
    try:
        login = service.start_login(user_subject=subject, system_id="oa", expected_principal_ref=None,
            card_base_url=service.trusted_card_base_url, task_id=task_id)
    except ValueError:
        actions._fail("ACTION_LOGIN_UNAVAILABLE", "原 OA 身份或登录配置不可用，请从原任务核对连接状态。")
    interaction = login.get("interaction")
    if interaction:
        service.observe_host_task(user_subject=subject, task_id=task_id,
            operation_ids=[response["operationId"]], interaction_ids=[interaction["interactionId"]])
        return {**response, "interaction": interaction, "nextAction": login["nextAction"]}
    if login.get("status") == "succeeded":
        # Another task may restore the session between the failed read and card
        # creation. Recover this fixed read once; never create another query.
        return service.invoke(user_subject=subject, capability_name=CAPABILITY, arguments={},
            idempotency_key=f"quick-query-session-ready:{task_id}:{command['commandKey']}", task_id=task_id,
            request_id=context["requestId"], host_type=context.get("hostType") or "openclaw",
            host_instance_id=context.get("hostInstanceId"), host_run_id=context.get("hostRunId"),
            origin_endpoint_id=task["origin_endpoint_id"])
    actions._fail("ACTION_LOGIN_UNAVAILABLE", "暂时无法创建原查询的登录入口，请从原任务核对连接状态。")


def execute_query(service, subject, task_id, command_key, *, runtime_context):
    """The existing host tool has already checked the exact L3 lease fence."""
    from agentbridge.core.central_service import operation_response
    context = {**runtime_context, "requestId": runtime_context.get("requestId") or command_key}
    with service._task_call_lock(task_id):
        with service.tasks._connect() as connection:
            task, command = actions._load_command(connection, subject, task_id, command_key)
            if context.get("endpointId") != task["origin_endpoint_id"]:
                actions._fail("ACTION_ENDPOINT_INVALID", "快捷查询必须由原会话协调。")
            _origin(service, connection, subject, task, command)
        try:
            _contract(service, command)
            service.user_grants.require_capability(subject, CAPABILITY)
            if command.get("remainingContext") and command.get("state") == "failed":
                return {"status":"failed", "taskId":task_id, "reused":True,
                    "error":command.get("error") or {"code":"BATCH_REMAINING_UNAVAILABLE", "message":"原剩余查询已结束，请重新核对原批次。"}, "result":None}
            existing = actions._existing_operation(service, subject, task_id, command)
            if existing is not None:
                existing = _continued_operation(service, subject, task_id, command, existing)
            if command.get("remainingContext"):
                from agentbridge.core.oa_batch_remaining import probe_query_identity
                with service.tasks._connect() as connection:
                    observed = connection.execute("SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? "
                        "AND event_type='task.batch_remaining.observed' AND causation_ref=?", (task_id, subject, command_key)).fetchall()
                expected = {"contextHash":actions._hash(command["remainingContext"]),
                    "operationId":existing["operation_id"] if existing else None,
                    "resultHash":actions._hash(existing.get("result")) if existing else None}
                if not existing or len(observed) != 1 or json.loads(observed[0][0]) != expected:
                    probe_query_identity(service, subject, task_id, command)
            if existing is None:
                if task["status"] not in {"active", "running", "waiting_user"} or command["state"] != "pending":
                    actions._fail("ACTION_COMMAND_NOT_PENDING", "查询已结束，请查看原结果或发起新查询。")
                if actions._now() >= actions._date(command["requestedAt"]) + timedelta(seconds=QUERY_TTL_SECONDS):
                    actions._fail("ACTION_SOURCE_EXPIRED", "查询等待已超过10分钟，请发起新的查询。")
                actions._record_queue_wait(service, subject, task_id, command, context, actions._now())
                response = service.invoke(user_subject=subject, capability_name=CAPABILITY, arguments={},
                    idempotency_key=f"workspace-prepare:{task_id}:{command_key}", task_id=task_id,
                    request_id=context["requestId"], host_type=context.get("hostType") or "openclaw",
                    host_instance_id=context.get("hostInstanceId"), host_run_id=context.get("hostRunId"),
                    origin_endpoint_id=task["origin_endpoint_id"])
            else:
                response = {**operation_response(existing), "reused": True}
                interaction = response.get("interaction") or {}
                if task["status"] in {"active", "running", "waiting_user"}:
                    service.observe_host_task(user_subject=subject, task_id=task_id, operation_ids=[existing["operation_id"]],
                        interaction_ids=[interaction["interactionId"]] if interaction.get("interactionId") else [])
            if command.get("remainingContext") and response.get("status") == "requires_user_action":
                actions._fail("BATCH_REMAINING_IDENTITY_CHANGED", "原 OA 登录已失效，请先恢复原账号，再重新查询剩余事项。")
            response = _login_response(service, subject, task_id, command, response, context)
            if (response.get("status") == "requires_user_action" and not response.get("interaction")
                    and (response.get("error") or {}).get("code") == "LOGIN_REQUIRED"):
                actions._fail("ACTION_LOGIN_UNAVAILABLE", "原查询登录恢复尚未完成，请查看连接状态后重新查询。")
            remaining_report = None
            if command.get("remainingContext") and response.get("status") == "succeeded":
                from agentbridge.core.oa_batch_remaining import observe_query_result
                with store_transaction(service.tasks) as connection:
                    remaining_report = observe_query_result(service, subject, task_id, command, response, connection=connection)
                    actions._record_result(service, subject, task_id, command_key, response, connection=connection)
                    service.tasks.complete_task(user_subject=subject, task_id=task_id,
                        reason="explicit_oa_remaining_query_verified", causation_ref=command_key, connection=connection)
            else:
                actions._record_result(service, subject, task_id, command_key, response)
            if response.get("status") == "succeeded":
                if not command.get("remainingContext"):
                    service.finish_host_task(user_subject=subject, task_id=task_id, outcome="succeeded",
                        reason="explicit_oa_pending_query_completed", causation_ref=command_key)
                response = {**response, "queryResult": {"kind": COMMAND_KIND, "taskId": task_id,
                    "endpointId": task["origin_endpoint_id"], "count": len((response.get("result") or {}).get("items") or []),
                    "complete": ((response.get("result") or {}).get("coverage") or {}).get("status") == "complete",
                    **({"remaining":remaining_report, "count":remaining_report["matchedCount"]} if remaining_report else {})}}
            return response
        except (actions.ActionableResultError, PermissionError) as exc:
            code = getattr(exc, "code", "ACTION_PERMISSION_DENIED")
            message = str(exc) if not isinstance(exc, PermissionError) else "查询权限已变化，请重新连接或联系管理员。"
            response = {"status": "failed", "taskId": task_id, "error": {"code": code, "message": message}, "result": None}
            if command.get("remainingContext"):
                with store_transaction(service.tasks) as connection:
                    current, recorded = actions._load_command(connection, subject, task_id, command_key)
                    # Revocation after an attested completion blocks current
                    # access; it does not rewrite the historical read outcome.
                    if recorded.get("state") != "succeeded":
                        actions._record_result(service, subject, task_id, command_key, response, connection=connection)
                        if current["status"] in {"active", "running", "waiting_user"}:
                            service.tasks.fail_task(user_subject=subject, task_id=task_id, error_code=code, message=message,
                                causation_ref=command_key, connection=connection)
            else:
                actions._record_result(service, subject, task_id, command_key, response)
                if service.tasks.get_task(task_id, user_subject=subject)["status"] in {"active", "running", "waiting_user"}:
                    service.fail_host_task(user_subject=subject, task_id=task_id, error_code=code, message=message,
                        causation_ref=command_key)
            return response
