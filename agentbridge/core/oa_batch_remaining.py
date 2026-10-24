"""Re-query untouched items of a stopped OA batch; never resume its decisions."""
from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
import time

from agentbridge.core import actionable_results as actions
from agentbridge.core.business_draft_profiles import BATCH_PREPARES, BY_PREPARE
from agentbridge.core.store_transactions import store_transaction

STOPPED = frozenset({"failed", "partially_succeeded", "outcome_unknown", "expired"})


def read_observation_state(connection, task, operation):
    """Keep this exact durable orchestration open after its read succeeds."""
    command = json.loads(task["summary_json"]).get("command") or {}
    if command.get("kind") != "oa_pending_query" or not command.get("remainingContext"):
        return None
    _, command = actions._load_command(connection, task["user_subject"], task["task_id"], command.get("commandKey"))
    if (operation.get("capability_name") != "oa.workflow.pending.list"
            or operation.get("capability_version") != command.get("capabilityVersion")
            or operation.get("input_hash") != actions._hash({}) or command.get("arguments") != {}
            or operation.get("idempotency_key") != f"workspace-prepare:{task['task_id']}:{command['commandKey']}"):
        return None
    from agentbridge.core.task_state_rules import TERMINAL_TASK_STATUSES
    if task["status"] in TERMINAL_TASK_STATUSES:
        return task["status"]
    if command.get("state") == "failed":
        return "failed"
    if command.get("state") == "pending" and task["status"] in {"active", "running", "waiting_user"}:
        return "running"
    return None


def _context(service, connection, subject, task_id):
    task = actions._owned_task(connection, subject, task_id)
    batch = connection.execute("SELECT * FROM task_batches WHERE parent_task_id=? AND user_subject=?",
        (task_id, subject)).fetchone()
    if (not batch or batch["system_id"] != "oa" or batch["capability_name"] not in BATCH_PREPARES
            or batch["state"] not in STOPPED or task["status"] not in STOPPED):
        actions._fail("BATCH_REMAINING_UNAVAILABLE", "只有已停止批次中尚未触达的后续事项可以重新查询。")
    rows = connection.execute("SELECT * FROM task_batch_items WHERE batch_id=? ORDER BY ordinal", (batch["batch_id"],)).fetchall()
    snapshot = [{"ordinal": row["ordinal"], "resourceRefHash": row["resource_ref_hash"],
                 "sourceFingerprint": row["source_fingerprint"]} for row in rows]
    if (len(rows) != batch["total_count"] or actions._hash(snapshot) != batch["source_snapshot_hash"]
            or any(hashlib.sha256(row["resource_ref"].encode()).hexdigest() != row["resource_ref_hash"] for row in rows)):
        actions._fail("BATCH_REMAINING_INVALID", "原批次目标记录不完整，请核对原任务。")
    command = json.loads(task["summary_json"]).get("command") or {}
    if command.get("kind") == actions.COMMAND_KIND:
        _, command = actions._load_command(connection, subject, task_id, command.get("commandKey"))
    fields, authorizations = actions._task_resources(service, connection, subject, task_id)
    recorded = actions._recorded_principal(connection, task, command, fields, authorizations)
    session = actions._oa_session(connection, subject)
    if session.get("state") != "active" or not recorded or recorded != actions._principal(session):
        actions._fail("BATCH_REMAINING_IDENTITY_CHANGED", "请恢复原批次的 OA 账号，再重新查询剩余事项。")
    touched = {(field["form_schema"].get("_agentbridge_resume_arguments") or {}).get("affair_id") for field in fields}
    touched.update((auth["plan"].get("target") or {}).get("affair_id") for auth in authorizations)
    targets = []
    for row in rows:
        if (row["ordinal"] <= batch["current_ordinal"] or row["state"] != "queued"
                or row["operation_id"] or row["interaction_id"] or row["finished_at"] or row["error_code"]
                or json.loads(row["result_summary_json"] or "{}") or row["resource_ref"] in touched):
            continue
        display = json.loads(row["display_summary_json"] or "{}")
        legacy = batch["capability_name"] == "oa.missed_punch.approval.batch.prepare"
        if legacy:
            from agentbridge.adapters.seeyon_missed_punch import (
                MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY, MISSED_PUNCH_APPROVE_CAPABILITY,
            )
            display.update(profile="missed_punch", prepare_capability=MISSED_PUNCH_APPROVAL_PREPARE_CAPABILITY,
                commit_capability=MISSED_PUNCH_APPROVE_CAPABILITY)
        profile = display.get("profile")
        definition = BY_PREPARE.get(display.get("prepare_capability"))
        if (definition is None or definition.commit != display.get("commit_capability")
                or profile != definition.review_kind
                or (not legacy and any(not isinstance(display.get(kind + "_version"), str)
                    or not display[kind + "_version"] for kind in ("prepare", "commit")))):
            actions._fail("BATCH_REMAINING_INVALID", "原批次缺少可核对的业务动作合同。")
        # The legacy missed-punch batch did not freeze per-item versions. Its
        # immutable profile still fixes the capability pair; new versions are
        # frozen by this explicit query and checked again at selection.
        versions = {kind + "Version": display.get(kind + "_version") or service.registry.get(display[kind + "_capability"]).version
                    for kind in ("prepare", "commit")}
        targets.append({"itemId":row["item_id"], "ordinal":row["ordinal"], "affairId":row["resource_ref"],
            "title":str(display.get("title") or "OA 待办")[:500], "profile":profile or definition.review_kind,
            "prepareCapability":definition.prepare, "commitCapability":definition.commit,
            "sourceFingerprint":row["source_fingerprint"], "fingerprintContract":"legacy_missed_punch" if legacy else "generic", **versions})
    if not targets:
        actions._fail("BATCH_REMAINING_EMPTY", "此批次没有可重新查询的未触达后续事项。")
    service.user_grants.require_capability(subject, "oa.workflow.pending.list")
    return {"version":"1", "originTaskId":task_id, "batchId":batch["batch_id"],
        "batchVersion":batch["version"], "currentOrdinal":batch["current_ordinal"],
        "principalHash":actions._hash({"system":"oa", "principal":recorded}), "targets":targets}


def validate_context(service, connection, subject, frozen):
    if not isinstance(frozen, dict) or not isinstance(frozen.get("originTaskId"), str):
        actions._fail("BATCH_REMAINING_INVALID", "剩余事项查询合同无效。")
    current = _context(service, connection, subject, frozen["originTaskId"])
    if current != frozen:
        actions._fail("BATCH_REMAINING_CHANGED", "原批次或剩余范围已变化，请重新查看原任务。")
    return current


def remaining_presentation(service, subject, task_id):
    try:
        with service.tasks._connect() as connection:
            context = _context(service, connection, subject, task_id)
        return {"available":True, "reason":None, "batchId":context["batchId"], "batchVersion":context["batchVersion"],
            "remainingCount":len(context["targets"]), "items":[{"ordinal":item["ordinal"], "title":item["title"],
                "state":"untouched"} for item in context["targets"]]}
    except (actions.ActionableResultError, PermissionError) as error:
        return {"available":False, "reason":getattr(error, "code", "BATCH_REMAINING_PERMISSION_DENIED"),
            "remainingCount":0, "items":[]}


def _request(body):
    from agentbridge.core.quick_queries import _body
    if not isinstance(body, dict) or set(body) != {"requestKey", "expectedBatchVersion"} or type(body["expectedBatchVersion"]) is not int:
        actions._fail("ACTION_REQUEST_INVALID", "剩余查询只接受当前批次版本与请求标识。")
    _body({"requestKey":body["requestKey"]})


def queue_remaining_query(service, account, task_id, body):
    from agentbridge.core.quick_queries import _queue
    _request(body)
    with store_transaction(service.tasks) as connection:
        workspace = dict(actions._account(connection, account))
        context = _context(service, connection, account["user_subject"], task_id)
        if context["batchVersion"] != body["expectedBatchVersion"]:
            actions._fail("BATCH_REMAINING_CHANGED", "批次版本已变化，请刷新原任务。")
        workspace["_remaining_context"] = context
        return _queue(service, account["user_subject"], workspace, {"requestKey":body["requestKey"]}, connection)


def queue_endpoint_remaining_query(service, context, task_id, body):
    from agentbridge.core.quick_queries import queue_endpoint_query
    _request(body)
    with service.tasks._connect() as connection:
        remaining = _context(service, connection, context["subject"], task_id)
        if remaining["batchVersion"] != body["expectedBatchVersion"]:
            actions._fail("BATCH_REMAINING_CHANGED", "批次版本已变化，请刷新原任务。")
    return queue_endpoint_query(service, context["subject"], context["endpointId"], {"requestKey":body["requestKey"]},
        binding_hash=context["endpointBindingHash"], source_task_id=context["sourceTaskId"], view_hash=context["tokenHash"],
        remaining_context=remaining)


def match_remaining(service, connection, subject, command, pending):
    frozen = validate_context(service, connection, subject, command["remainingContext"])
    if not isinstance(pending, dict) or (pending.get("coverage") or {}).get("status") != "complete":
        actions._fail("ACTION_SOURCE_INCOMPLETE", "本次待办读取不完整，不能重新核对剩余范围。")
    rows = pending.get("items")
    if (not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict)
            or not isinstance(row.get("affair_id"), str) or not row["affair_id"].strip()
            or len(row["affair_id"]) > 256 for row in rows)):
        actions._fail("ACTION_SOURCE_INVALID", "待办结果缺少有效事项标识。")
    from agentbridge.adapters.seeyon_pending_verification import _consistent_complete_collection
    if pending["coverage"].get("hasMore") is True or not _consistent_complete_collection(pending, rows):
        actions._fail("ACTION_SOURCE_INCOMPLETE", "本次待办读取数量与完整范围不一致，未开放剩余事项办理。")
    by_id = {row["affair_id"]:row for row in rows}
    if len(by_id) != len(rows):
        actions._fail("ACTION_SOURCE_INVALID", "待办结果有重复事项，未开放选择。")
    matched, report = [], []
    for target in frozen["targets"]:
        item = {"ordinal":target["ordinal"], "title":target["title"], "state":"missing"}
        row = by_id.get(target["affairId"])
        if row is not None:
            try:
                selected = actions._select(service, pending, [target["affairId"]])[0]
                display = selected["display_summary"]
                same = all(display[kind + "_capability"] == target[kind + "Capability"] and
                    display[kind + "_version"] == target[kind + "Version"] for kind in ("prepare", "commit"))
                # Fingerprints include the source display/category/profile and
                # versions. Changed rows remain visible as a scope discrepancy,
                # never silently substituted into a new batch.
                fingerprint = selected["source_fingerprint"]
                if target["fingerprintContract"] == "legacy_missed_punch":
                    from agentbridge.adapters.seeyon_missed_punch import select_missed_punch_approval_batch_items
                    legacy = select_missed_punch_approval_batch_items([row], limit=1)
                    fingerprint = legacy[0]["source_fingerprint"] if len(legacy) == 1 else None
                if same and fingerprint == target["sourceFingerprint"]:
                    matched.append(row)
                    item["state"] = "matched"
                else:
                    item.update(state="changed", reason="原查询中的事项说明或业务动作已变化，请另行重新查询核对。")
            except actions.ActionableResultError:
                item.update(state="changed", reason="此事项当前不再匹配原业务动作。")
        else:
            item["reason"] = "本次完整待办列表未找到此项；不代表已经办理成功。"
        report.append(item)
    return matched, {"originalTaskId":frozen["originTaskId"], "originalBatchId":frozen["batchId"],
        "requestedCount":len(report), "matchedCount":len(matched), "missingCount":sum(item["state"] == "missing" for item in report),
        "changedCount":sum(item["state"] == "changed" for item in report), "items":report}


def source_scope(service, connection, subject, source_task_id, pending, *, operation_id):
    task = actions._owned_task(connection, subject, source_task_id)
    command = json.loads(task["summary_json"]).get("command") or {}
    if command.get("kind") != "oa_pending_query" or "remainingContext" not in command:
        return None
    _, command = actions._load_command(connection, subject, source_task_id, command.get("commandKey"))
    evidence = connection.execute("SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? "
        "AND event_type='task.batch_remaining.observed' AND causation_ref=?", (source_task_id, subject, command["commandKey"])).fetchall()
    expected = {"contextHash":actions._hash(command["remainingContext"]), "operationId":command.get("operationId"),
                "resultHash":actions._hash(pending)}
    if (command.get("state") != "succeeded" or command.get("operationId") != operation_id
            or len(evidence) != 1 or json.loads(evidence[0][0]) != expected):
        actions._fail("BATCH_REMAINING_UNVERIFIED", "剩余范围尚未完成身份与来源核验。")
    return match_remaining(service, connection, subject, command, pending)


def _query_context(service, connection, subject, task_id, command):
    from agentbridge.core.quick_queries import _origin, _contract
    task, current = actions._load_command(connection, subject, task_id, command["commandKey"])
    if (current["intentHash"] != command["intentHash"] or current["state"] not in {"pending", "succeeded"}
            or (task["status"] not in {"active", "running", "waiting_user"}
                and not (task["status"] == "succeeded" and current["state"] == "succeeded"))):
        actions._fail("BATCH_REMAINING_CHANGED", "本次剩余查询已结束或变更，请查看原任务。")
    _origin(service, connection, subject, task, current)
    _contract(service, current)
    validate_context(service, connection, subject, command["remainingContext"])


def probe_query_identity(service, subject, task_id, command):
    from agentbridge.browser.read_budget import read_deadline_scope, remaining_seconds, check_read_deadline
    from agentbridge.adapters.base import AdapterLoginRequired
    with service.tasks._connect() as connection:
        _query_context(service, connection, subject, task_id, command)
        session = actions._oa_session(connection, subject)
    runtime = service._runtime_for_system("oa")
    if runtime is None:
        actions._fail("BATCH_REMAINING_READ_UNAVAILABLE", "OA 身份核对暂不可用。")
    adapter, factory = runtime
    trace = service.runtime_governance.trace_for_task(task_id, user_subject=subject)
    try:
        with service.runtime_governance.timing(trace_id=(trace or {}).get("trace_id"), stage="batch_remaining.identity_probe"), \
                read_deadline_scope(time.monotonic() + 30), \
                service._session_lock(session["session_id"], wait_seconds=remaining_seconds(30), purpose="oa_batch_remaining"), ExitStack() as stack:
            check_read_deadline()
            with service.tasks._connect() as connection:
                _query_context(service, connection, subject, task_id, command)
            state = service.session_states.load(session["session_id"])
            if state is None:
                actions._fail("BATCH_REMAINING_IDENTITY_CHANGED", "请恢复原 OA 登录后重新查询。")
            worker = stack.enter_context(factory(session, adapter))
            check_read_deadline()
            worker.restore_session_state(state)
            stack.enter_context(adapter.capability_scope(worker))
            probe = adapter.probe_business_draft_session(worker)
            check_read_deadline()
            if str(probe.get("observed_principal_ref") or "").strip() != actions._principal(session):
                actions._fail("BATCH_REMAINING_IDENTITY_CHANGED", "实际 OA 账号与原批次不同，未查询剩余事项。")
            with service.tasks._connect() as connection:
                _query_context(service, connection, subject, task_id, command)
            service.session_states.save(session["session_id"], worker.capture_session_state())
    except (actions.ActionableResultError, PermissionError):
        raise
    except (AdapterLoginRequired, TimeoutError, RuntimeError, ValueError, OSError):
        actions._fail("BATCH_REMAINING_READ_UNAVAILABLE", "原 OA 身份暂未核实，未开放剩余事项办理。")


def observe_query_result(service, subject, task_id, command, response, *, connection=None):
    with store_transaction(service.tasks, connection) as connection:
        _query_context(service, connection, subject, task_id, command)
        _, report = match_remaining(service, connection, subject, command, response.get("result"))
        payload = {"contextHash":actions._hash(command["remainingContext"]), "operationId":response["operationId"],
                   "resultHash":actions._hash(response["result"])}
        previous = connection.execute("SELECT payload_json FROM task_events WHERE task_id=? AND user_subject=? "
            "AND event_type='task.batch_remaining.observed' AND causation_ref=?", (task_id, subject, command["commandKey"])).fetchall()
        if previous and (len(previous) != 1 or json.loads(previous[0][0]) != payload):
            actions._fail("BATCH_REMAINING_CHANGED", "原剩余查询的核验记录不一致。")
        if not previous:
            service.tasks._append_event(connection, task_id=task_id, user_subject=subject,
                event_type="task.batch_remaining.observed", payload=payload, causation_ref=command["commandKey"],
                created_at=actions._now().isoformat())
        return report
