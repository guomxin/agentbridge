"""Owned task views of a saved OA query; continuation accepts no new filters."""
from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.oa_history_queries import OaHistoryQueryError


def _binding(service, subject):
    session = service.sessions.find(user_subject=subject, system_id="oa")
    principal = principal_binding(session) if session else None
    if not session or session["state"] != "active" or not principal:
        raise OaHistoryQueryError("OA_HISTORY_SESSION_CHANGED", "OA 登录或账号已变化，请重新登录后查询。", status=401)
    return dict(user_subject=subject, session_id=session["session_id"], principal_ref=principal)


def _query_ids(service, subject, task_id):
    service.tasks.get_task(task_id, user_subject=subject)
    service.require_task_result_access(user_subject=subject, task_id=task_id)
    result = []
    for operation_id in service.tasks.operation_ids_for_task(task_id=task_id, user_subject=subject):
        operation = service.operations.get(operation_id)
        value = operation.get("result")
        if (operation["user_subject"] == subject and operation["capability_name"] in
                {"oa.workflow.history.query", "oa.workflow.history.next"} and
                isinstance(value, dict) and value.get("schema_version") == "agentbridge.oa_history_query.v1"
                and isinstance(value.get("query_id"), str)):
            if value["query_id"] not in result:
                result.append(value["query_id"])
    return result


def presentation(service, subject, task_id):
    ids = _query_ids(service, subject, task_id)
    if not ids:
        return None
    try:
        return service.oa_history_queries.latest_response(ids[-1], **_binding(service, subject))
    except OaHistoryQueryError as exc:
        # An expired or changed identity cannot expose the saved rows/cursor.
        return {"schema_version": "agentbridge.oa_history_query.v1", "query_id": ids[-1],
                "state": "unavailable", "items": [], "has_more": False,
                "next_cursor": None, "error": {"code": exc.code, "message": str(exc)}}


def next_page(service, account, task_id, body):
    if not isinstance(body, dict) or set(body) != {"cursor"}:
        raise ValueError("历史查询续取仅接受原查询游标。")
    subject = account["user_subject"]
    from agentbridge.core.actionable_results import _account
    with service.tasks._connect() as connection:
        _account(connection, account)
    ids = _query_ids(service, subject, task_id)
    binding = _binding(service, subject)
    resolved = service.oa_history_queries.resolve(body["cursor"], **binding)
    if resolved["record"]["query_id"] not in ids:
        raise OaHistoryQueryError("OA_HISTORY_QUERY_NOT_FOUND", "原任务不包含此查询。", status=404)
    # Central invocation rechecks grants, session/profile locks and identity.
    # The immutable cursor receipt makes network retries safe without a new query.
    response = service.invoke(user_subject=subject, capability_name="oa.workflow.history.next",
        arguments={"cursor": body["cursor"]}, host_type="workspace",
        origin_endpoint_id=account["endpoint_id"])
    if response.get("status") != "succeeded":
        error = response.get("error") or {}
        code = error.get("code", "OA_HISTORY_QUERY_FAILED")
        status = 401 if "LOGIN" in code or "SESSION" in code else 403 if "AUTHORIZATION" in code or "PERMISSION" in code else 409
        raise OaHistoryQueryError(code, error.get("message") or "查询未完成，请稍后重试原查询。", status=status)
    service.require_task_result_access(user_subject=subject, task_id=task_id)
    return {"oaHistoryQuery": response["result"]}
