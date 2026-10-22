"""Exact, owned query selections queue normal host-controlled preparation.

This deterministic route implements the reviewed bundled OA pending Skill. It
never approves a card, acquires a coordinator lease, or executes a business
commit. Unknown instructions and incomplete query results fail closed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from uuid import uuid4

from agentbridge.adapters.seeyon_pending_batch import (
    PENDING_BATCH_PREPARE_CAPABILITY, PendingBatchSelectionError, select_pending_batch_items,
)
from agentbridge.core.business_skills import SkillRejected, skill_bundle, validate_binding
from agentbridge.core.operations import _operation_from_row
from agentbridge.core.store_transactions import store_transaction
from agentbridge.core.task_state_rules import TERMINAL_TASK_STATUSES


SKILL_ID = "oa-pending"
# A published/customized instruction bundle requires an explicit route review.
REVIEWED_SKILL_HASH = "95de0088c98d702c64b0e9f93debfb9b2b4a1e283fd0292afce00740d28289c8"
SOURCE_TTL_SECONDS = 600
COMMAND_KIND = "oa_pending_prepare"
_MUTABLE_COMMAND_FIELDS = {"state", "operationId", "error"}
_LABELS = {
    "meeting_reply": "会议回复",
    "missed_punch": "补签审批", "efficiency_data": "效能数据", "travel_expense": "差旅报销",
    "business_trip": "出差审批", "labor_contract_renewal": "合同续签",
    "intellectual_property_declaration": "知识产权申报", "intellectual_property_reward": "知识产权奖励",
    "overtime": "加班审批", "leave": "请假审批",
    "resignation": "离职审批", "work_handover": "工作交接", "flight_application": "乘机申请",
    "attendance_confirmation": "考勤确认", "weekly_report": "周报阅办", "quarterly_performance": "季度绩效阅办", "standard_collaboration": "协同审批",
}


class ActionableResultError(ValueError):
    def __init__(self, code, message, status=409):
        self.code = code
        self.status = status
        super().__init__(message)


def _now():
    return datetime.now(timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def _fail(code, message):
    status = (429 if code == "ACTION_PREPARATION_BUSY" else
              400 if code in {"ACTION_REQUEST_INVALID", "ACTION_SELECTION_INVALID"} else
              403 if code == "ACTION_ENDPOINT_INVALID" else 404 if code == "ACTION_SOURCE_NOT_FOUND" else 409)
    raise ActionableResultError(code, message, status)


def _date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        _fail("ACTION_SOURCE_INVALID", "待办查询时间无效，请重新查询。")


def _owned_task(connection, subject, task_id):
    row = connection.execute("SELECT * FROM agent_tasks WHERE task_id=? AND user_subject=?", (task_id, subject)).fetchone()
    if row is None:
        _fail("ACTION_SOURCE_NOT_FOUND", "待办查询或办理任务不存在。")
    return row


def _source(connection, subject, source_task_id, operation_id, *, fresh=True):
    task = _owned_task(connection, subject, source_task_id)
    if task["status"] != "succeeded":
        _fail("ACTION_SOURCE_NOT_COMPLETE", "待办查询尚未完成，请稍候刷新。")
    row = connection.execute(
        "SELECT o.* FROM operations o JOIN task_operations t ON t.operation_id=o.operation_id "
        "WHERE t.task_id=? AND t.user_subject=? AND o.operation_id=? AND o.user_subject=?",
        (source_task_id, subject, operation_id, subject),
    ).fetchone()
    if row is None or row["capability_name"] != "oa.workflow.pending.list" or row["status"] != "succeeded":
        _fail("ACTION_SOURCE_NOT_FOUND", "办理目标必须来自当前用户已完成的待办查询。")
    operation = _operation_from_row(row)
    finished = _date(operation.get("finished_at"))
    if fresh and (finished > _now() + timedelta(seconds=60) or _now() >= finished + timedelta(seconds=SOURCE_TTL_SECONDS)):
        _fail("ACTION_SOURCE_EXPIRED", "待办查询已超过10分钟，请重新查询后选择事项。")
    pending = operation.get("result")
    if not isinstance(pending, dict) or (pending.get("coverage") or {}).get("status") != "complete":
        _fail("ACTION_SOURCE_INCOMPLETE", "待办来源不完整，请重新查询；不能把部分结果作为完整办理范围。")
    rows = pending.get("items")
    if not isinstance(rows, list) or len(rows) > 100 or any(
        not isinstance(item, dict) or not isinstance(item.get("affair_id"), str)
        or not item["affair_id"].strip() or len(item["affair_id"]) > 256 for item in rows
    ):
        _fail("ACTION_SOURCE_INVALID", "待办来源缺少有效事项标识或超过100项，请重新查询并缩小范围。")
    ids = [item["affair_id"] for item in rows]
    if len(ids) != len(set(ids)):
        _fail("ACTION_SOURCE_INVALID", "待办来源存在重复标识，请重新查询。")
    return operation, pending


def _require_grants(service, subject, items):
    service.user_grants.require_capability(subject, "oa.workflow.pending.list")
    capabilities = {item["display_summary"][f"{kind}_capability"] for item in items for kind in ("prepare", "commit")}
    for capability in sorted(capabilities):
        service.user_grants.require_capability(subject, capability)


def _reviewed_current_skill(service, subject, profile, connection):
    item = service.skills.current(SKILL_ID, connection)
    service.skills._allowed(subject, SKILL_ID, profile, connection)
    if item.get("content_hash") != REVIEWED_SKILL_HASH:
        _fail("ACTION_SKILL_CHANGED", "办理助手的执行说明已更新，请通过对话重新加载助手后办理。")
    return item


def _select(service, pending, affair_ids):
    try:
        return select_pending_batch_items(pending, {"affair_ids": affair_ids, "max_items": 100}, service.registry)
    except PendingBatchSelectionError as exc:
        raise ActionableResultError(exc.code, str(exc)) from exc


def _target_key(operation_id, row):
    return _hash({"operationId": operation_id, "row": row})


def _source_preparations(connection, subject, source_task_id, operation_id):
    """Project owned, frozen preparation records; never infer a write result."""
    rows = connection.execute(
        "SELECT task_id FROM agent_tasks WHERE user_subject=? "
        "AND json_extract(summary_json,'$.command.kind')=? "
        "AND json_extract(summary_json,'$.command.sourceTaskId')=? "
        "AND json_extract(summary_json,'$.command.operationIdSource')=? "
        "ORDER BY created_at DESC,rowid DESC",
        (subject, COMMAND_KIND, source_task_id, operation_id),
    ).fetchall()
    result = {}
    for row in rows:
        task = _owned_task(connection, subject, row["task_id"])
        command = json.loads(task["summary_json"]).get("command") or {}
        task, command = _load_command(connection, subject, task["task_id"], command.get("commandKey"))
        keys = command["requestIdentity"]["targetKeys"]
        affairs = command["affairIds"]
        items = {item["resource_ref"]: item for item in connection.execute(
            "SELECT i.resource_ref,i.state,i.ordinal,b.current_ordinal FROM task_batch_items i JOIN task_batches b ON b.batch_id=i.batch_id "
            "WHERE b.parent_task_id=? AND b.user_subject=?", (task["task_id"], subject))}
        state = task["status"]
        if state in {"active", "running"}:
            current = connection.execute(
                "SELECT capability_name,status FROM operations WHERE operation_id=? AND user_subject=?",
                (task["current_operation_id"], subject),
            ).fetchone()
            commits = {item["display_summary"]["commit_capability"] for item in command["selectedItems"]}
            state = ("running" if current and current["capability_name"] in commits and current["status"] in {"pending", "running"} else
                     "queued" if command.get("state") == "pending" and not current else "preparing")
        for key, affair in zip(keys, affairs, strict=True):
            item = items.get(affair)
            item_state = item["state"] if item else state
            if item and item_state in {"queued", "preparing", "waiting_user"}:
                if state == "running" and item["ordinal"] == item["current_ordinal"]:
                    item_state = "running"
                elif task["status"] in TERMINAL_TASK_STATUSES:
                    item_state = "skipped"
            result.setdefault(key, {"taskId": task["task_id"], "state": item_state})
    return result


def preparation_source_task_ids(connection, subject, task_id):
    """Expose only a validated owned source for refreshing an existing card."""
    task = _owned_task(connection, subject, task_id)
    command = json.loads(task["summary_json"]).get("command") or {}
    if command.get("kind") != COMMAND_KIND:
        return []
    try:
        _, command = _load_command(connection, subject, task_id, command.get("commandKey"))
        _owned_task(connection, subject, command["sourceTaskId"])
    except (ActionableResultError, KeyError):
        return []
    return [command["sourceTaskId"]] if command["sourceTaskId"] != task_id else []


def list_actionable_results(service, subject, source_task_id):
    """Return UI-safe choices, retaining unsupported rows as disabled choices."""
    groups = []
    with service.tasks._connect() as connection:
        _owned_task(connection, subject, source_task_id)
        ids = connection.execute(
            "SELECT o.operation_id FROM operations o JOIN task_operations t ON t.operation_id=o.operation_id "
            "WHERE t.task_id=? AND t.user_subject=? AND o.user_subject=? "
            "AND o.capability_name='oa.workflow.pending.list' AND o.status='succeeded' ORDER BY o.created_at,o.rowid",
            (source_task_id, subject, subject),
        ).fetchall()
        if not ids:
            return []
        try:
            service.user_grants.require_capability(subject, "oa.workflow.pending.list")
        except PermissionError:
            return []
        for entry in ids:
            expired = False
            try:
                operation, pending = _source(connection, subject, source_task_id, entry[0])
            except ActionableResultError as exc:
                if exc.code != "ACTION_SOURCE_EXPIRED":
                    continue
                try:
                    operation, pending = _source(connection, subject, source_task_id, entry[0], fresh=False)
                except ActionableResultError:
                    continue
                expired = True
            preparations = _source_preparations(connection, subject, source_task_id, entry[0])
            # Retain historical progress only for queries that actually led to
            # preparation. Expiry never creates another actionable window.
            if expired and not preparations:
                continue
            targets = []
            for row in pending["items"]:
                target = {"targetKey": _target_key(entry[0], row),
                    **{key: str(row.get(key) or "")[:500] for key in ("title", "sender", "date", "category")},
                    "workflowType": None, "workflowLabel": "暂未支持的待办", "enabled": False, "disabledReason": None,
                    "preparation": preparations.get(_target_key(entry[0], row))}
                try:
                    selected = _select(service, pending, [row["affair_id"]])
                    display = selected[0]["display_summary"]
                    target.update(workflowType=display["profile"], workflowLabel=_LABELS[display["profile"]])
                    _require_grants(service, subject, selected)
                    _reviewed_current_skill(service, subject, "single", connection)
                    target["enabled"] = True
                except (ActionableResultError, SkillRejected, PermissionError) as exc:
                    target["disabledReason"] = str(exc) if not isinstance(exc, PermissionError) else "当前没有办理此类待办的权限。"
                if target["preparation"]:
                    target["enabled"] = False
                elif expired:
                    target.update(enabled=False, disabledReason="待办查询已超过10分钟，请重新查询后办理。")
                targets.append(target)
            finished = _date(operation["finished_at"])
            groups.append({"sourceTaskId": source_task_id, "operationId": entry[0], "title": "OA 待办",
                "fetchedAt": finished.isoformat(), "expiresAt": (finished + timedelta(seconds=SOURCE_TTL_SECONDS)).isoformat(),
                "coverage": "complete", "targets": targets})
    return groups


def _account(connection, account):
    row = connection.execute(
        "SELECT a.*,e.state AS endpoint_state,e.agent_host,e.client_type,e.conversation_ref FROM workspace_accounts a "
        "JOIN client_endpoints e ON e.endpoint_id=a.endpoint_id AND e.user_subject=a.user_subject "
        "WHERE a.account_id=? AND a.user_subject=?", (account.get("account_id"), account.get("user_subject")),
    ).fetchone()
    if row is None or row["state"] != "active" or row["endpoint_state"] != "active" or row["agent_host"] != "openclaw" \
        or row["client_type"] not in {"web", "webchat"} or row["conversation_ref"] != row["openclaw_session_key"] \
        or any(account.get(key) != row[key] for key in ("endpoint_id", "endpoint_key", "openclaw_session_key")):
        _fail("ACTION_ENDPOINT_INVALID", "Workspace 会话绑定已失效，请刷新后重试。")
    return row


def _body(body):
    if not isinstance(body, dict) or set(body) != {"operationId", "targetKeys", "requestKey"}:
        _fail("ACTION_REQUEST_INVALID", "办理请求只允许选择查询结果。")
    keys = body["targetKeys"]
    if not isinstance(keys, list) or not 1 <= len(keys) <= 100 or any(
        not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) for key in keys
    ) or len(set(keys)) != len(keys):
        _fail("ACTION_SELECTION_INVALID", "请选择1至100个不同的待办事项。")
    if not isinstance(body["operationId"], str) or not 1 <= len(body["operationId"]) <= 128 \
        or not isinstance(body["requestKey"], str) or not re.fullmatch(r"[a-zA-Z0-9_:-]{16,128}", body["requestKey"]):
        _fail("ACTION_REQUEST_INVALID", "办理请求标识无效，请刷新后重试。")


def _intent(command):
    return {key: value for key, value in command.items() if key not in _MUTABLE_COMMAND_FIELDS | {"intentHash"}}


def _load_command(connection, subject, task_id, command_key):
    task = _owned_task(connection, subject, task_id)
    command = json.loads(task["summary_json"]).get("command")
    if not isinstance(command, dict) or command.get("kind") != COMMAND_KIND or command.get("commandKey") != command_key:
        _fail("ACTION_COMMAND_INVALID", "办理请求与原任务不一致。")
    events = connection.execute(
        "SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? AND event_type='task.prepare.requested' AND causation_ref=?",
        (task_id, subject, command_key),
    ).fetchall()
    if len(events) != 1 or command.get("intentHash") != _hash(_intent(command)) \
        or json.loads(events[0][0]).get("intentHash") != command["intentHash"]:
        _fail("ACTION_COMMAND_INVALID", "办理请求的冻结记录不完整。")
    return task, command


def _binding(service, subject, task_id, command, connection):
    binding = service.skills.for_task(subject, task_id, connection)
    if binding is None or binding["binding_id"] != command["skillBindingId"] or binding["skill_id"] != SKILL_ID \
        or binding["snapshot"]["content_hash"] != command["skillHash"]:
        _fail("ACTION_SKILL_BINDING_INVALID", "办理助手与原任务绑定不一致。")
    bundle = skill_bundle(binding)
    if bundle["loaded_resources"] != command["loadedResources"] or _hash(bundle["content"]) != command["instructionsHash"]:
        _fail("ACTION_SKILL_BINDING_INVALID", "办理助手必读说明与冻结记录不一致。")
    validate_binding(service, subject, binding, capability=command["capabilityName"])
    return binding


def _telegram_endpoint(connection, subject, endpoint_id):
    """Validate a live private Telegram route; never manufacture a web account."""
    row = connection.execute("SELECT * FROM client_endpoints WHERE endpoint_id=? AND user_subject=?", (endpoint_id, subject)).fetchone()
    if row is None:
        _fail("ACTION_ENDPOINT_INVALID", "Telegram 会话绑定已失效。")
    endpoint = dict(row)
    route = json.loads(endpoint["route_json"])
    conversation = endpoint["conversation_ref"]
    match = re.fullmatch(r"agent:[^:]+:telegram:(?:direct|dm):([^:]+)", conversation, re.IGNORECASE)
    sender = endpoint["external_subject"]
    route_account = route.get("accountId") if isinstance(route, dict) else None
    endpoint_account = endpoint["account_id"]
    accounts_valid = all(value is None or isinstance(value, str) for value in (route_account, endpoint_account))
    canonical_route_account = (route_account or "").strip() or "default" if accounts_valid else None
    canonical_endpoint_account = (endpoint_account or "").strip() or "default" if accounts_valid else None
    if (endpoint["state"] != "active" or endpoint["agent_host"] != "openclaw"
            or endpoint["client_type"].lower() != "telegram" or not match or match[1] != sender
            or not isinstance(route, dict) or route.get("channel") != "telegram"
            or not accounts_valid or canonical_route_account != canonical_endpoint_account
            or str(route.get("to") or "").removeprefix("telegram:") != sender
            or sender.startswith("-") or route.get("threadId") not in (None, "")):
        _fail("ACTION_ENDPOINT_INVALID", "办理入口仅限原 Telegram 私聊会话。")
    token = connection.execute("SELECT user_subject,state,expires_at FROM mcp_identity_tokens WHERE token_id=?", (endpoint["token_id"],)).fetchone()
    if token is None or token["user_subject"] != subject or token["state"] != "active" or _date(token["expires_at"]) <= _now():
        _fail("ACTION_ENDPOINT_INVALID", "Telegram 身份已失效，请重新连接。")
    # Timeline ingestion refreshes this same endpoint after the host issues a
    # card. Bind the verified route meaning rather than its JSON spelling:
    # default accounts, bare/prefixed sender and absent topic have equivalent
    # host representations. Keep every other identity/route field strict.
    canonical_route = {**route, "channel": "telegram", "to": sender, "threadId": None,
        "accountId": "default" if canonical_route_account == "default" else route_account}
    binding = {key: endpoint[key] for key in (
        "endpoint_id", "user_subject", "token_id", "agent_host", "endpoint_key", "client_type",
        "external_subject", "account_id", "conversation_ref")}
    binding.update(bindingVersion="telegram-private-route-v1", client_type="telegram",
        account_id="default" if canonical_endpoint_account == "default" else endpoint_account,
        route=canonical_route)
    endpoint["_binding_hash"] = _hash(binding)
    return endpoint


def queue_preparation(service, account, source_task_id, body):
    """Atomically persist an intent, task and Skill binding; perform no invoke."""
    _body(body)
    subject = account.get("user_subject")
    with store_transaction(service.tasks) as connection:
        workspace = _account(connection, account)
        return _queue_preparation(service, subject, source_task_id, body, dict(workspace), connection)


def queue_endpoint_preparation(service, subject, endpoint_id, source_task_id, body, *, binding_hash, view_hash, source_hash):
    """Queue only an exact ticket-bound Telegram endpoint with live permissions."""
    _body(body)
    with store_transaction(service.tasks) as connection:
        endpoint = _telegram_endpoint(connection, subject, endpoint_id)
        if endpoint["_binding_hash"] != binding_hash:
            _fail("ACTION_ENDPOINT_INVALID", "Telegram 会话已变化，请从聊天重新打开。")
        _, pending = _source(connection, subject, source_task_id, body["operationId"], fresh=False)
        if _hash(pending) != source_hash:
            _fail("ACTION_SOURCE_CHANGED", "原查询内容已变化，请重新查询。")
        binding = {"endpoint_id": endpoint_id, "endpoint_key": endpoint["endpoint_key"],
            "openclaw_session_key": endpoint["conversation_ref"], "_request_scope": endpoint_id,
            "_telegram": True, "_binding_hash": binding_hash}
        result = _queue_preparation(service, subject, source_task_id, body, binding, connection)
        previous = connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? "
            "AND event_type='task.result_view.prepared' AND causation_ref=?", (result["taskId"], subject, view_hash)).fetchone()
        if previous is None:
            service.tasks._append_event(connection, task_id=result["taskId"], user_subject=subject,
                event_type="task.result_view.prepared", payload={"viewTokenHash": view_hash, "sourceTaskId": source_task_id},
                causation_ref=view_hash, created_at=_now().isoformat())
        return result


def _queue_preparation(service, subject, source_task_id, body, workspace, connection):
    request_identity = {"sourceTaskId": source_task_id, "operationId": body["operationId"], "targetKeys": body["targetKeys"]}
    scope = workspace.get("_request_scope") or workspace["account_id"]
    request_hash = _hash({("endpointId" if workspace.get("_telegram") else "accountId"): scope, "requestKey": body["requestKey"]})
    identity_hash = _hash(request_identity)
    previous_request = connection.execute(
        "SELECT payload_json FROM task_events WHERE user_subject=? "
        "AND event_type IN ('task.prepare.requested','task.prepare.request_alias') "
        "AND json_extract(payload_json,'$.requestKeyHash')=? LIMIT 1", (subject, request_hash),
    ).fetchone()
    if previous_request and json.loads(previous_request[0]).get("requestIdentityHash") != identity_hash:
        _fail("ACTION_REQUEST_CONFLICT", "此请求已绑定另一组待办，请重新选择。")
    # Two surfaces selecting the same ordered source targets converge even
    # when each generated a different browser request key.
    host_key = f"{'telegram-prepare' if workspace.get('_telegram') else 'workspace-prepare'}:{scope}:{identity_hash}"
    existing = connection.execute(
        "SELECT task_id FROM agent_tasks WHERE user_subject=? AND agent_host='openclaw' AND host_task_key=?", (subject, host_key),
    ).fetchone()
    if existing:
        row = _owned_task(connection, subject, existing[0])
        old = json.loads(row["summary_json"]).get("command") or {}
        _, command = _load_command(connection, subject, existing[0], old.get("commandKey"))
        if command.get("requestIdentity") != request_identity or row["origin_endpoint_id"] != workspace["endpoint_id"]:
            _fail("ACTION_REQUEST_CONFLICT", "此请求已绑定另一组待办，请重新选择。")
        _binding(service, subject, existing[0], command, connection)
        _require_grants(service, subject, command["selectedItems"])
        if not previous_request:
            service.tasks._append_event(connection, task_id=existing[0], user_subject=subject,
                event_type="task.prepare.request_alias", payload={"requestKeyHash": request_hash,
                    "requestIdentityHash": identity_hash}, causation_ref=request_hash, created_at=_now().isoformat())
        return {"taskId": existing[0], "commandKey": command["commandKey"], "status": "existing"}
    pending_count = connection.execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE user_subject=? AND origin_endpoint_id=? "
        "AND agent_host='openclaw' AND status IN ('active','running','waiting_user') "
        "AND json_extract(summary_json,'$.command.kind')=? AND json_extract(summary_json,'$.command.state')='pending'",
        (subject, workspace["endpoint_id"], COMMAND_KIND),
    ).fetchone()[0]
    if pending_count >= 32:
        _fail("ACTION_PREPARATION_BUSY", "当前会话已有32个待准备请求，请稍候恢复本次准备。")
    operation, pending = _source(connection, subject, source_task_id, body["operationId"])
    by_key = {_target_key(body["operationId"], row): row["affair_id"] for row in pending["items"]}
    if any(key not in by_key for key in body["targetKeys"]):
        _fail("ACTION_TARGET_UNAVAILABLE", "所选事项不属于这次待办查询，请重新选择。")
    affair_ids = [by_key[key] for key in body["targetKeys"]]
    prepared = _source_preparations(connection, subject, source_task_id, body["operationId"])
    if any(key in prepared for key in body["targetKeys"]):
        _fail("ACTION_TARGET_ALREADY_PREPARED", "所选事项已有办理任务，请查看原任务；重新办理前请重新查询待办。")
    selected = _select(service, pending, affair_ids)
    _require_grants(service, subject, selected)
    profile = "single" if len(selected) == 1 else "batch"
    item = _reviewed_current_skill(service, subject, profile, connection)
    binding_id = service.skills.bind(subject, SKILL_ID, profile, expected_version=item["manifest"]["version"], connection=connection)
    binding = service.skills.binding(subject, binding_id, connection)
    bundle = skill_bundle(binding)
    capability = selected[0]["display_summary"]["prepare_capability"] if profile == "single" else PENDING_BATCH_PREPARE_CAPABILITY
    arguments = {"affair_id": affair_ids[0]} if profile == "single" else {"affair_ids": affair_ids, "max_items": len(affair_ids)}
    command = {"kind": COMMAND_KIND, "commandKey": str(uuid4()), "requestIdentity": request_identity,
        "sourceTaskId": source_task_id, "operationIdSource": operation["operation_id"], "sourceHash": _hash(pending),
        "selectedItems": selected, "affairIds": affair_ids, "capabilityName": capability,
        "capabilityVersion": service.registry.get(capability).version, "arguments": arguments,
        "skillBindingId": binding_id, "skillHash": item["content_hash"], "loadedResources": bundle["loaded_resources"],
        "instructionsHash": _hash(bundle["content"]), "requestedAt": _now().isoformat(), "state": "pending"}
    if workspace.get("_telegram"):
        command.update(originKind="telegram", endpointBindingHash=workspace["_binding_hash"])
    command["intentHash"] = _hash(_intent(command))
    task, _ = service.tasks.ensure_task(user_subject=subject, agent_host="openclaw", host_task_key=host_key,
        origin_endpoint_id=workspace["endpoint_id"], active_conversation_ref=workspace["openclaw_session_key"],
        title=(f"核对并办理{len(selected)}项OA待办" if profile == "batch" else "核对并办理OA待办"),
        summary={"command": command}, connection=connection)
    service.skills.attach(subject, task["task_id"], binding_id, connection=connection)
    if workspace.get("_telegram"):
        subscription = connection.execute("SELECT subscription_id,event_filters_json FROM task_subscriptions "
            "WHERE task_id=? AND endpoint_id=? AND user_subject=? AND state='active'",
            (task["task_id"], workspace["endpoint_id"], subject)).fetchone()
        if subscription:
            filters = json.loads(subscription["event_filters_json"])
            if "task.prepare.requested" not in filters and "*" not in filters:
                connection.execute("UPDATE task_subscriptions SET event_filters_json=? WHERE subscription_id=?",
                    (_json([*filters, "task.prepare.requested"]), subscription["subscription_id"]))
    service.tasks._append_event(connection, task_id=task["task_id"], user_subject=subject,
        event_type="task.prepare.requested", payload={"commandKey": command["commandKey"], "intentHash": command["intentHash"],
            "selectedCount": len(selected), "requestKeyHash": request_hash, "requestIdentityHash": identity_hash},
        causation_ref=command["commandKey"], created_at=command["requestedAt"])
    return {"taskId": task["task_id"], "commandKey": command["commandKey"], "status": "queued"}


def _record_result(service, subject, task_id, command_key, response):
    with store_transaction(service.tasks) as connection:
        task, command = _load_command(connection, subject, task_id, command_key)
        summary = json.loads(task["summary_json"])
        command["state"] = str(response.get("status") or "failed")
        if response.get("operationId"):
            command["operationId"] = response["operationId"]
        if response.get("error"):
            command["error"] = response["error"]
        summary["command"] = command
        connection.execute("UPDATE agent_tasks SET summary_json=?,version=version+1,updated_at=? WHERE task_id=?",
            (_json(summary), _now().isoformat(), task_id))


def _existing_operation(service, subject, task_id, command):
    operation = service.operations.find_idempotent(user_subject=subject, capability_name=command["capabilityName"],
        capability_version=command["capabilityVersion"], idempotency_key=f"workspace-prepare:{task_id}:{command['commandKey']}")
    if operation is not None and operation["input_hash"] != _hash(command["arguments"]):
        _fail("ACTION_COMMAND_INVALID", "已有办理操作与冻结目标不一致。")
    return operation


def _record_queue_wait(service, subject, task_id, command, context, observed_at):
    """Best-effort timing after the caller has closed its ledger read."""
    try:
        started = _date(command["requestedAt"])
        trace, _ = service.runtime_governance.ensure_trace(user_subject=subject, task_id=task_id,
            request_id=context["requestId"], origin_endpoint_id=context.get("endpointId"),
            host_type=context.get("hostType") or "openclaw", host_instance_id=context.get("hostInstanceId"),
            host_run_id=context.get("hostRunId"), request_kind="interaction", lock_timeout=.02)
        service.runtime_governance.record_timing(trace_id=trace["trace_id"], stage="workspace.preparation.queue_wait",
            started_at=started.isoformat(), finished_at=observed_at.isoformat(),
            duration_ms=max(0, round((observed_at - started).total_seconds() * 1000)),
            metadata={"clockSource": "central.wall", "durationClock": "central.wall", "measurementKind": "span",
                "requestId": context["requestId"]}, dedupe_key="preparation-queue-wait:" + command["commandKey"])
    except Exception:
        pass  # Diagnostics never change command execution or recovery.


def execute_preparation(service, subject, task_id, command_key, *, runtime_context=None):
    """Called only after the private host tool validates L3, lease and origin."""
    from agentbridge.core.central_service import operation_response
    context = {**(runtime_context or {})}
    context["requestId"] = context.get("requestId") or command_key
    with service._task_call_lock(task_id):
        with service.tasks._connect() as connection:
            task, command = _load_command(connection, subject, task_id, command_key)
            if context.get("endpointId") != task["origin_endpoint_id"]:
                _fail("ACTION_ENDPOINT_INVALID", "办理请求必须由原Workspace会话协调。")
            if command.get("originKind") == "telegram":
                endpoint = _telegram_endpoint(connection, subject, task["origin_endpoint_id"])
                if endpoint["_binding_hash"] != command.get("endpointBindingHash") or endpoint["conversation_ref"] != task["active_conversation_ref"]:
                    _fail("ACTION_ENDPOINT_INVALID", "办理请求必须由原 Telegram 私聊会话协调。")
        _record_queue_wait(service, subject, task_id, command, context, _now())
        try:
            with service.tasks._connect() as connection:
                _binding(service, subject, task_id, command, connection)
                _require_grants(service, subject, command["selectedItems"])
                existing = _existing_operation(service, subject, task_id, command)
                if existing is None:
                    if task["status"] not in {"active", "running", "waiting_user"} or command.get("state") != "pending":
                        _fail("ACTION_COMMAND_NOT_PENDING", "办理任务已结束，不能再次创建准备操作。")
                    _, pending = _source(connection, subject, command["sourceTaskId"], command["operationIdSource"])
                    if _hash(pending) != command["sourceHash"] or _select(service, pending, command["affairIds"]) != command["selectedItems"]:
                        _fail("ACTION_SOURCE_CHANGED", "原查询目标与冻结记录不一致，请重新查询。")
                    binding = _reviewed_current_skill(service, subject,
                        "single" if len(command["selectedItems"]) == 1 else "batch", connection)
                    if binding["content_hash"] != command["skillHash"] or service.registry.get(command["capabilityName"]).version != command["capabilityVersion"]:
                        _fail("ACTION_SKILL_CHANGED", "办理说明或操作版本已更新，请重新查询后办理。")
            if existing is not None:
                response = {**operation_response(existing), "reused": True}
                # The response may have been lost before normal task observation.
                interaction = response.get("interaction") or {}
                service.observe_host_task(user_subject=subject, task_id=task_id, operation_ids=[existing["operation_id"]],
                    interaction_ids=[interaction["interactionId"]] if interaction.get("interactionId") else [])
            else:
                response = service.invoke(user_subject=subject, capability_name=command["capabilityName"],
                    arguments=command["arguments"], idempotency_key=f"workspace-prepare:{task_id}:{command_key}",
                    task_id=task_id, request_id=context.get("requestId"), host_type=context.get("hostType") or "openclaw",
                    host_instance_id=context.get("hostInstanceId"), host_run_id=context.get("hostRunId"),
                    origin_endpoint_id=context.get("endpointId"))
            _record_result(service, subject, task_id, command_key, response)
            return response
        except (ActionableResultError, SkillRejected, PermissionError) as exc:
            code = getattr(exc, "code", "ACTION_PERMISSION_DENIED")
            message = str(exc) if not isinstance(exc, PermissionError) else "办理权限已变化，请刷新后重试。"
            response = {"status": "failed", "taskId": task_id, "error": {"code": code, "message": message}, "result": None}
            _record_result(service, subject, task_id, command_key, response)
            if task["status"] in {"active", "running", "waiting_user"}:
                service.fail_host_task(user_subject=subject, task_id=task_id, error_code=code, message=message,
                    causation_ref=command_key)
            return response


def pending_preparations(service, subject, limit=16):
    """Bounded owned intents only; the host revalidates the exact Workspace."""
    limit = min(max(int(limit), 1), 64)
    with service.tasks._connect() as connection:
        rows = connection.execute(
            "SELECT t.*,e.endpoint_key AS prepare_endpoint_key FROM agent_tasks t "
            "JOIN workspace_accounts a ON a.endpoint_id=t.origin_endpoint_id AND a.user_subject=t.user_subject "
            "AND a.openclaw_session_key=t.active_conversation_ref AND a.state='active' "
            "JOIN client_endpoints e ON e.endpoint_id=t.origin_endpoint_id AND e.user_subject=t.user_subject "
            "AND e.conversation_ref=t.active_conversation_ref AND e.endpoint_key=a.endpoint_key "
            "AND e.agent_host='openclaw' AND e.client_type IN ('web','webchat') AND e.state='active' "
            "WHERE t.user_subject=? AND t.agent_host='openclaw' "
            "AND t.status IN ('active','running','waiting_user') "
            "AND json_extract(t.summary_json,'$.command.kind')=? AND json_extract(t.summary_json,'$.command.state')='pending' "
            "ORDER BY t.created_at,t.rowid LIMIT ?", (subject, COMMAND_KIND, limit),
        ).fetchall()
        telegram_rows = connection.execute(
            "SELECT t.*,e.endpoint_key AS prepare_endpoint_key FROM agent_tasks t "
            "JOIN client_endpoints e ON e.endpoint_id=t.origin_endpoint_id AND e.user_subject=t.user_subject "
            "AND e.conversation_ref=t.active_conversation_ref AND e.agent_host='openclaw' "
            "AND e.client_type='telegram' AND e.state='active' WHERE t.user_subject=? AND t.agent_host='openclaw' "
            "AND t.status IN ('active','running','waiting_user') "
            "AND json_extract(t.summary_json,'$.command.kind')=? AND json_extract(t.summary_json,'$.command.state')='pending' "
            "AND json_extract(t.summary_json,'$.command.originKind')='telegram' ORDER BY t.created_at,t.rowid LIMIT ?",
            (subject, COMMAND_KIND, limit)).fetchall()
        rows = sorted([*rows, *telegram_rows], key=lambda row: (row["created_at"], row["task_id"]))[:limit]
        result = []
        for row in rows:
            key = (json.loads(row["summary_json"]).get("command") or {}).get("commandKey")
            try:
                _, command = _load_command(connection, subject, row["task_id"], key)
                if command.get("originKind") == "telegram":
                    endpoint = _telegram_endpoint(connection, subject, row["origin_endpoint_id"])
                    if endpoint["_binding_hash"] != command.get("endpointBindingHash"):
                        continue
            except ActionableResultError:
                continue
            item = {"taskId": row["task_id"], "commandKey": command["commandKey"],
                "sessionKey": row["active_conversation_ref"], "endpointKey": row["prepare_endpoint_key"]}
            if command.get("originKind") == "telegram":
                item["endpoint"] = {"endpointId": endpoint["endpoint_id"], "clientType": endpoint["client_type"],
                    "externalSubject": endpoint["external_subject"], "accountId": endpoint["account_id"],
                    "conversationRef": endpoint["conversation_ref"], "route": json.loads(endpoint["route_json"])}
            result.append(item)
        return result
