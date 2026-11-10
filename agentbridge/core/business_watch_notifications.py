"""Reference-only watch events in the existing Task Hub notification ledger.

The change callback uses its caller's SQLite transaction and never sends a
message. Business text is read only by the delivery validator after checking
the current watch, permissions, principal, session and explicit endpoint choice.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentbridge.core.business_watches import BusinessWatchError
from agentbridge.core.tasks import TaskIntegrityError, TaskNotFound


WATCH_EVENT_TYPES = {"business_watch.changed", "business_watch.completed"}
_EVENT_TYPES = {"watch.changed": "business_watch.changed", "watch.completed": "business_watch.completed"}
_RULES = ("nodes", "handlers", "ended")


def watch_task_id(watch_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, "agentbridge:business-watch-task:v1:" + watch_id))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def on_watch_change(service, connection: sqlite3.Connection, event: dict, private_watch: dict) -> None:
    """Append a generic succeeded task/event, timeline reference and outbox.

    No ``ensure_task`` call is made: its companion subscriptions would widen
    the owner's explicit delivery selection. A user without a live endpoint
    keeps the watch change history, with no invented client identity.
    """
    if not connection.in_transaction:
        raise ValueError("watch notification callback requires an active transaction")
    watch_id, owner = private_watch["watch_id"], private_watch["user_subject"]
    if event.get("watch_id") != watch_id or event.get("event_type") not in _EVENT_TYPES:
        raise ValueError("watch change does not match its notification owner")
    sequence = event.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("watch change sequence is invalid")
    persisted = connection.execute("SELECT event_type,created_at FROM business_watch_changes "
        "WHERE event_id=? AND watch_id=? AND user_subject=? AND sequence=?",
        (event["event_id"], watch_id, owner, sequence)).fetchone()
    if persisted is None or persisted["event_type"] != event["event_type"]:
        raise ValueError("watch change is not persisted in the supplied transaction")
    task_id = watch_task_id(watch_id)
    causation = f"business-watch:{watch_id}:{sequence}"
    # Re-entry in the same writer transaction or a later retry cannot append a
    # second event. A->B->A->B has distinct monotonic sequences, not hash dedupe.
    if connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? AND causation_ref=?",
                          (task_id, owner, causation)).fetchone():
        return
    endpoints = connection.execute("SELECT * FROM client_endpoints WHERE user_subject=? AND state='active' "
        "ORDER BY CASE WHEN client_type IN ('web','webchat') THEN 0 ELSE 1 END,created_at,endpoint_id", (owner,)).fetchall()
    if not endpoints:
        return
    now, origin = persisted["created_at"], endpoints[0]
    task = connection.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is not None and (task["user_subject"] != owner or json.loads(task["summary_json"]) != {"businessWatchId": watch_id}):
        raise TaskIntegrityError("watch notification task identity mismatch")
    if task is None:
        connection.execute("INSERT INTO agent_tasks "
            "(task_id,user_subject,agent_host,host_task_key,origin_endpoint_id,title,status,summary_json,"
            "active_conversation_ref,version,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,'业务关注更新','succeeded',?,?,1,?,?,?)",
            (task_id, owner, origin["agent_host"], "business-watch:" + watch_id, origin["endpoint_id"],
             _json({"businessWatchId": watch_id}), "business-watch:" + watch_id, now, now, now))
    else:
        connection.execute("UPDATE agent_tasks SET title='业务关注更新',status='succeeded',version=version+1,"
                           "updated_at=?,finished_at=? WHERE task_id=?", (now, now, task_id))
    connection.execute("UPDATE task_subscriptions SET state='paused',updated_at=? WHERE task_id=?", (now, task_id))
    selected = set(private_watch.get("endpoints") or [])
    for endpoint in endpoints:
        if endpoint["endpoint_id"] not in selected or str(endpoint["client_type"]).lower() in {"web", "webchat"}:
            continue
        connection.execute("INSERT INTO task_subscriptions "
            "(subscription_id,task_id,endpoint_id,user_subject,event_filters_json,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'active',?,?) ON CONFLICT(task_id,endpoint_id) DO UPDATE SET "
            "event_filters_json=excluded.event_filters_json,state='active',updated_at=excluded.updated_at",
            (str(uuid4()), task_id, endpoint["endpoint_id"], owner, _json(sorted(WATCH_EVENT_TYPES)), now, now))
    service.tasks._append_event(connection, task_id=task_id, user_subject=owner,
        event_type=_EVENT_TYPES[event["event_type"]], payload={"watchId": watch_id, "sequence": sequence},
        causation_ref=causation, created_at=now)


def _display(value, maximum):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()
    return text if len(text) <= maximum else text[:maximum - 1] + "…"


def _summary(watch, event, rules):
    labels = {"nodes": "节点变化", "handlers": "处理人变化", "ended": "流程结束"}
    lines = ["业务关注更新：" + _display(watch["title"], 120), "变化：" + "、".join(labels[rule] for rule in rules)]
    diff = event["diff"]
    if "ended" in rules and diff["ended"]["after"] is True:
        lines.append("流程已结束，不代表付款完成。")
    else:
        if "nodes" in rules:
            names = diff["nodes"]["after"]
            lines.append("当前节点：" + ("、".join(_display(name, 60) for name in names[:5]) or "无活动节点"))
        if "handlers" in rules:
            names = diff["handlers"]["after"]["current_handlers"] or []
            lines.append("当前处理人：" + ("、".join(_display(name, 40) for name in names[:5]) or "无活动处理人"))
    lines.append("观察时间：" + event["observed_at"])
    return "\n".join(lines)


def _current_delivery_watch(service, owner, watch_id, endpoint_id):
    watches = service.business_watch
    if watches.controls.is_paused(owner):
        return None
    watch = watches._owned(owner, watch_id)
    watches._session(owner, principal=watch["principal_ref"])
    if (watch["state"] not in {"active", "completed"} or not watch["allow_mark_read"]
            or datetime.fromisoformat(watch["expires_at"]) <= watches.store._now()
            or endpoint_id not in watch["endpoints"]):
        return None
    endpoint = service.tasks.get_endpoint(endpoint_id, user_subject=owner)
    if endpoint["state"] != "active" or str(endpoint["client_type"]).lower() in {"web", "webchat"}:
        return None
    return watch


def validate_watch_delivery(service, delivery: dict) -> dict | None:
    """Return ephemeral, currently authorized content for claim/ACK, or None.

    ``delivery`` is the real ``TaskHubStore.claim_outbox/get_delivery`` shape.
    The caller handles None as no-op/cancelled, without a payload retry. This
    function performs no OA read, model call, task execution or delivery write.
    """
    try:
        wrapper = delivery["payload"]
        if (delivery["payload_type"] != "task_event" or not isinstance(wrapper, dict)
                or wrapper.get("eventType") not in WATCH_EVENT_TYPES
                or wrapper.get("eventId") != delivery.get("event_id")):
            return None
        reference = wrapper.get("payload")
        if not isinstance(reference, dict) or set(reference) != {"watchId", "sequence"}:
            return None
        watch_id, sequence = reference["watchId"], reference["sequence"]
        if not isinstance(watch_id, str) or type(sequence) is not int or sequence < 1:
            return None
        owner, endpoint_id = delivery["user_subject"], delivery["endpoint_id"]
        if delivery["task_id"] != watch_task_id(watch_id) or wrapper.get("taskId") != delivery["task_id"]:
            return None
        task = service.tasks.get_task(delivery["task_id"], user_subject=owner)
        if task["summary"] != {"businessWatchId": watch_id}:
            return None
        watch = _current_delivery_watch(service, owner, watch_id, endpoint_id)
        if watch is None:
            return None
        events = service.business_watch.store.history(watch_id, user_subject=owner, limit=1, before_seq=sequence + 1)
        if not events or events[0]["sequence"] != sequence:
            return None
        event = events[0]
        if _EVENT_TYPES.get(event["event_type"]) != wrapper["eventType"]:
            return None
        rules = [rule for rule in _RULES if rule in watch["rules"] and rule in event["change_types"]]
        if not rules or (watch["state"] == "completed" and
                (event["event_type"] != "watch.completed" or sequence != watch["change_seq"])):
            return None
        message = _summary(watch, event, rules)
        current = _current_delivery_watch(service, owner, watch_id, endpoint_id)
        if current is None or current["version"] != watch["version"]:
            return None
        return {"message": message, "watchId": watch_id, "sequence": sequence,
                "eventTypes": rules, "observedAt": event["observed_at"]}
    except (BusinessWatchError, PermissionError, TaskNotFound, KeyError, TypeError, ValueError):
        # A changed or corrupt reference never makes the generic Task Hub path
        # disclose content. Exceptions and internal fields are not payloads.
        return None
