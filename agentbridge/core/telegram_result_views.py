"""Opaque, endpoint-bound Telegram result selection; never a business decision.

The URL is issued only by a registered host. Its random secret is never stored:
append-only task events retain a digest and an exact endpoint/source binding.
Selection reuses the original durable prepare/Skill/lease path; GET never invokes
or resumes a capability and an unknown result never offers replay.
"""
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
import re
import secrets
from urllib.parse import urlsplit, urlunsplit, urlencode

from agentbridge.core import actionable_results as actions
from agentbridge.core.store_transactions import store_transaction
from agentbridge.core.trusted_flow_presentation import continuous_flow_presentation
from agentbridge.core.task_state_rules import TERMINAL_TASK_STATUSES, BATCH_STATES, ACTIVE_BATCH_STATES

SCHEMA_VERSION = "agentbridge.result-view.v1"
TICKET_TTL_SECONDS = 3600
_EVENT = "task.result_view.issued"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_TERMINAL = TERMINAL_TASK_STATUSES | (BATCH_STATES - ACTIVE_BATCH_STATES) | {"unknown", "completed", "skipped"}


def _digest(token):
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        actions._fail("RESULT_VIEW_UNAVAILABLE", "办理入口不可用，请从原 Telegram 聊天重新打开。")
    return sha256(token.encode()).hexdigest()


class ResultViewService:
    def __init__(self, service):
        self.service = service

    def issue(self, subject, task_id, endpoint_id, host_context=None):
        """Host-only issuer. The MCP boundary validates the L3 registration."""
        service = self.service
        token = secrets.token_urlsafe(32)
        token_hash = _digest(token)
        with store_transaction(service.tasks) as connection:
            endpoint = actions._telegram_endpoint(connection, subject, endpoint_id)
            task = actions._owned_task(connection, subject, task_id)
            if (task["agent_host"] != "openclaw" or task["origin_endpoint_id"] != endpoint_id
                    or task["active_conversation_ref"] != endpoint["conversation_ref"]):
                actions._fail("ACTION_ENDPOINT_INVALID", "查询任务与原 Telegram 私聊不一致。")
            if host_context and (host_context.get("endpointId") != endpoint_id
                    or host_context.get("tokenId") != endpoint["token_id"]):
                actions._fail("ACTION_ENDPOINT_INVALID", "宿主身份与办理入口不一致。")
            groups = self._groups(subject, task_id)
            selection_available = any(item["enabled"] for group in groups for item in group["targets"])
            existing = self._preparation_ids(connection, subject, endpoint, groups)
            if not groups or not (selection_available or existing):
                actions._fail("ACTION_SOURCE_UNAVAILABLE", "当前查询没有可直接办理的完整待办，请重新查询或通过对话继续。")
            if len(groups) > 16:
                actions._fail("ACTION_SOURCE_LIMIT", "本任务包含超过16组待办查询，请在新对话中缩小查询范围后选择。")
            sources = []
            for group in groups:
                _, pending = actions._source(connection, subject, task_id, group["operationId"])
                sources.append({"operationId": group["operationId"], "sourceHash": actions._hash(pending),
                    "sourceExpiresAt": group["expiresAt"]})
            source_expires = max(source["sourceExpiresAt"] for source in sources)
            created = actions._now()
            issued_count = connection.execute("SELECT COUNT(*) FROM task_events WHERE user_subject=? "
                "AND event_type=? AND created_at>? AND json_extract(payload_json,'$.endpointId')=?",
                (subject, _EVENT, (created - timedelta(seconds=TICKET_TTL_SECONDS)).isoformat(), endpoint_id)).fetchone()[0]
            if issued_count >= 64:
                actions._fail("ACTION_PREPARATION_BUSY", "办理入口请求较多，请使用原聊天中仍有效的入口。")
            expires = (created + timedelta(seconds=TICKET_TTL_SECONDS)).isoformat()
            payload = {"version": "1", "tokenHash": token_hash, "endpointId": endpoint_id,
                "endpointBindingHash": endpoint["_binding_hash"], "sourceTaskId": task_id,
                "sources": sources, "sourceExpiresAt": source_expires, "expiresAt": expires}
            service.tasks._append_event(connection, task_id=task_id, user_subject=subject,
                event_type=_EVENT, payload=payload, causation_ref=token_hash, created_at=created.isoformat())
        base = str(service.trusted_card_base_url or "").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            actions._fail("RESULT_VIEW_UNAVAILABLE", "安全办理地址尚未配置。")
        return {"protocolVersion": "0.1", "status": "succeeded", "resultView": {
            "schemaVersion": SCHEMA_VERSION, "title": "选择并办理 OA 待办", "sourceTaskId": task_id,
            "presentation": {"owner": "agentbridge", "url": base + "/results/" + token,
                "endpointId": endpoint_id, "modelMustNotCollectValues": True},
            "expiresAt": expires, "sourceExpiresAt": source_expires, "selectionAvailable": selection_available}}

    def _context(self, token):
        token_hash = _digest(token)
        service = self.service
        with service.tasks._connect() as connection:
            rows = connection.execute("SELECT task_id,user_subject,payload_json FROM task_events "
                "WHERE event_type=? AND causation_ref=? LIMIT 2", (_EVENT, token_hash)).fetchall()
            if len(rows) != 1:
                actions._fail("RESULT_VIEW_UNAVAILABLE", "办理入口不可用，请从原 Telegram 聊天重新打开。")
            event = rows[0]
            payload = json.loads(event["payload_json"])
            if payload.get("tokenHash") != token_hash or payload.get("version") != "1" or payload.get("sourceTaskId") != event["task_id"]:
                actions._fail("RESULT_VIEW_UNAVAILABLE", "办理入口记录无效。")
            if actions._now() >= actions._date(payload.get("expiresAt")):
                actions._fail("RESULT_VIEW_EXPIRED", "办理入口已过期，已准备事项请使用原聊天里的业务卡片继续。")
            subject = event["user_subject"]
            endpoint = actions._telegram_endpoint(connection, subject, payload.get("endpointId"))
            if endpoint["_binding_hash"] != payload.get("endpointBindingHash"):
                actions._fail("ACTION_ENDPOINT_INVALID", "Telegram 会话已变化，请从原聊天重新打开。")
            task = actions._owned_task(connection, subject, event["task_id"])
            if task["origin_endpoint_id"] != endpoint["endpoint_id"] or task["active_conversation_ref"] != endpoint["conversation_ref"]:
                actions._fail("ACTION_ENDPOINT_INVALID", "原查询会话绑定已变化。")
            sources = payload.get("sources")
            if sources is None and payload.get("operationId"):
                sources = [{key: payload[key] for key in ("operationId", "sourceHash", "sourceExpiresAt")}]
            if (not isinstance(sources, list) or not 1 <= len(sources) <= 16
                    or any(not isinstance(source, dict) or set(source) != {"operationId", "sourceHash", "sourceExpiresAt"} for source in sources)
                    or len({source["operationId"] for source in sources}) != len(sources)):
                actions._fail("RESULT_VIEW_UNAVAILABLE", "办理入口来源记录无效。")
            for source in sources:
                operation, pending = actions._source(connection, subject, event["task_id"], source["operationId"], fresh=False)
                expected_expiry = (actions._date(operation["finished_at"]) + timedelta(seconds=actions.SOURCE_TTL_SECONDS)).isoformat()
                if actions._hash(pending) != source.get("sourceHash") or expected_expiry != source["sourceExpiresAt"]:
                    actions._fail("ACTION_SOURCE_CHANGED", "原待办查询内容或时效已变化，请重新查询。")
            payload["sources"] = sources
            service.user_grants.require_capability(subject, "oa.workflow.pending.list")
            actions._reviewed_current_skill(service, subject, "single", connection)
        return {**payload, "tokenHash": token_hash, "subject": subject, "endpoint": endpoint}

    def _groups(self, subject, task_id):
        # Telegram already has a separate persistent prepared-task list. Keep
        # its selection groups within their original per-source TTL.
        return [group for group in actions.list_actionable_results(self.service, subject, task_id)
                if actions._date(group["expiresAt"]) > actions._now()]

    def _preparation_ids(self, connection, subject, endpoint, groups):
        """Only reference existing tasks from this exact private host route."""
        ids = []
        for group in groups:
            for target in group["targets"]:
                preparation = target.get("preparation") or {}
                task_id = preparation.get("taskId")
                if not task_id or preparation.get("state") == "identity_unverified" or task_id in ids:
                    continue
                task = actions._owned_task(connection, subject, task_id)
                command = json.loads(task["summary_json"]).get("command") or {}
                if (task["origin_endpoint_id"] == endpoint["endpoint_id"]
                        and task["active_conversation_ref"] == endpoint["conversation_ref"]
                        and command.get("kind") == actions.COMMAND_KIND and command.get("originKind") == "telegram"
                        and command.get("endpointBindingHash") == endpoint["_binding_hash"]):
                    actions._load_command(connection, subject, task_id, command.get("commandKey"))
                    ids.append(task_id)
                    continue
                try:
                    origin = self._recovery_origin(connection, subject, task_id, current=False)
                    context = {"subject": subject, "sourceTaskId": group["sourceTaskId"],
                               "sources": [{"operationId": group["operationId"]}]}
                    if self._source_matches_recovery(connection, context, origin):
                        original_id = origin["task"]["task_id"]
                        if original_id not in ids:
                            ids.append(original_id)
                except (actions.ActionableResultError, KeyError, ValueError, PermissionError, RuntimeError):
                    continue
        return ids

    def _reference_matches(self, connection, context, task_id, command):
        """A fresh owned source may reference the exact same unresolved affair.

        No ticket event, presentation, binding, decision or capability is created
        by this read. Source hashes and the live OA principal are checked again.
        """
        wanted = {(item["resource_ref"], item["display_summary"]["prepare_capability"],
                   item["display_summary"]["commit_capability"]) for item in command["selectedItems"]}
        selections = []
        for source in context["sources"]:
            _, pending = actions._source(connection, context["subject"], context["sourceTaskId"], source["operationId"], fresh=False)
            if actions._hash(pending) != source["sourceHash"]:
                actions._fail("ACTION_SOURCE_CHANGED", "原查询内容已变化，请重新查询。")
            for row in pending["items"]:
                if row["affair_id"] not in {item[0] for item in wanted}:
                    continue
                try:
                    item = actions._select(self.service, pending, [row["affair_id"]])[0]
                except actions.ActionableResultError:
                    continue
                display = item["display_summary"]
                key = (item["resource_ref"], display["prepare_capability"], display["commit_capability"])
                if key in wanted:
                    selections.append({"affairId": key[0], "prepareCapability": key[1], "commitCapability": key[2]})
        session = actions._oa_session(connection, context["subject"])
        if not session or not selections:
            return False
        return any(item["taskId"] == task_id and item["identityVerified"] for item in actions.oa_preparation_conflicts(
            self.service, user_subject=context["subject"], session=session, selections=selections, connection=connection))

    def read(self, token):
        context = self._context(token)
        operation_ids = {source["operationId"] for source in context["sources"]}
        all_groups = [group for group in actions.list_actionable_results(self.service, context["subject"], context["sourceTaskId"])
            if group["operationId"] in operation_ids]
        groups = [group for group in all_groups if actions._date(group["expiresAt"]) > actions._now()]
        with self.service.tasks._connect() as connection:
            ids = connection.execute("SELECT t.task_id FROM agent_tasks t JOIN task_events e ON e.task_id=t.task_id "
                "AND e.user_subject=t.user_subject WHERE t.user_subject=? AND e.event_type='task.result_view.prepared' "
                "AND e.causation_ref=? ORDER BY t.created_at DESC,t.task_id DESC LIMIT 32",
                (context["subject"], context["tokenHash"])).fetchall()
            referenced = self._preparation_ids(connection, context["subject"], context["endpoint"], all_groups)
        preparations = [self._status(context, task_id) for task_id in dict.fromkeys([*(row[0] for row in ids), *referenced])]
        selection_available = any(target["enabled"] for group in groups for target in group["targets"])
        return {"schemaVersion": SCHEMA_VERSION, "title": "选择并办理 OA 待办",
            "sourceTaskId": context["sourceTaskId"], "groups": groups, "expiresAt": context["expiresAt"],
            "sourceExpiresAt": context["sourceExpiresAt"], "selectionAvailable": selection_available,
            "message": "请选择事项，随后在业务界面核对材料并最终确认。" if selection_available else "当前没有可新选的事项；已准备事项仍可从下方或原聊天业务卡片继续。",
            "preparations": preparations}

    def prepare(self, token, body):
        context = self._context(token)
        sources = {source["operationId"]: source for source in context["sources"]}
        if not isinstance(body, dict) or not isinstance(body.get("operationId"), str) or body["operationId"] not in sources:
            actions._fail("ACTION_SELECTION_INVALID", "只能选择本入口同一组原查询中的事项。")
        return actions.queue_endpoint_preparation(self.service, context["subject"], context["endpointId"],
            context["sourceTaskId"], body, binding_hash=context["endpointBindingHash"], view_hash=context["tokenHash"],
            source_hash=sources[body["operationId"]]["sourceHash"])

    def refresh(self, token, body):
        """An explicit POST creates a new read task; it never renews old choices."""
        from agentbridge.core.quick_queries import queue_endpoint_query
        context = self._context(token)
        queued = queue_endpoint_query(self.service, context["subject"], context["endpointId"], body,
            binding_hash=context["endpointBindingHash"], source_task_id=context["sourceTaskId"],
            view_hash=context["tokenHash"])
        state = self.query_status(token, queued["taskId"])
        result = {**queued, "query": state}
        if state["state"] == "succeeded" and state.get("entryAvailable"):
            # Ticket issuance is a POST effect; read/status never issue tickets.
            result["resultView"] = self.issue(context["subject"], queued["taskId"], context["endpointId"])["resultView"]
        return result

    def query_status(self, token, task_id):
        from agentbridge.core.quick_queries import COMMAND_KIND, _contract
        context = self._context(token)
        service, subject = self.service, context["subject"]
        with service.tasks._connect() as connection:
            task = actions._owned_task(connection, subject, task_id)
            command = json.loads(task["summary_json"]).get("command") or {}
            linked = connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? "
                "AND event_type='task.result_view.refreshed' AND causation_ref=?",
                (task_id, subject, context["tokenHash"])).fetchone()
            if (not linked or command.get("kind") != COMMAND_KIND or command.get("originKind") != "telegram"
                    or command.get("sourceTaskId") != context["sourceTaskId"] or command.get("viewHash") != context["tokenHash"]
                    or task["origin_endpoint_id"] != context["endpointId"]
                    or task["active_conversation_ref"] != context["endpoint"]["conversation_ref"]
                    or command.get("endpointBindingHash") != context["endpointBindingHash"]):
                actions._fail("ACTION_SOURCE_NOT_FOUND", "查询任务不属于本入口。")
            actions._load_command(connection, subject, task_id, command.get("commandKey"))
            _contract(service, command)
        groups = self._groups(subject, task_id) if task["status"] == "succeeded" else []
        with service.tasks._connect() as connection:
            existing = self._preparation_ids(connection, subject, context["endpoint"], groups)
        selection_available = any(target["enabled"] for group in groups for target in group["targets"])
        return {"taskId": task_id, "state": task["status"], "title": "查询 OA 待办",
            "terminal": task["status"] in _TERMINAL, "groups": groups,
            "selectionAvailable": selection_available, "entryAvailable": selection_available or bool(existing),
            "message": "已获取新的待办查询结果。" if task["status"] == "succeeded" else _status_message(task["status"])}

    def status(self, token, queued_task_id):
        return self._status(self._context(token), queued_task_id)

    def _status(self, context, task_id):
        service, subject = self.service, context["subject"]
        with service.tasks._connect() as connection:
            task = actions._owned_task(connection, subject, task_id)
            try:
                origin = self._recovery_origin(connection, subject, task_id, current=False)
            except (KeyError, ValueError, PermissionError, RuntimeError):
                origin = None
            if origin and self._source_matches_recovery(connection, context, origin):
                return self._recovery_status(context, origin)

            linked = connection.execute("SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? "
                "AND event_type='task.result_view.prepared' AND causation_ref=?",
                (task_id, subject, context["tokenHash"])).fetchone()
            command = json.loads(task["summary_json"]).get("command") or {}
            if (task["origin_endpoint_id"] != context["endpointId"]
                    or task["active_conversation_ref"] != context["endpoint"]["conversation_ref"]
                    or command.get("kind") != actions.COMMAND_KIND or command.get("originKind") != "telegram"
                    or command.get("endpointBindingHash") != context["endpointBindingHash"]):
                actions._fail("ACTION_SOURCE_NOT_FOUND", "办理任务不属于本入口。")
            actions._load_command(connection, subject, task_id, command.get("commandKey"))
            original = (linked and command.get("sourceTaskId") == context["sourceTaskId"]
                        and command.get("operationIdSource") in {source["operationId"] for source in context["sources"]})
            referenced = self._reference_matches(connection, context, task_id, command)
            if not original and not referenced:
                actions._fail("ACTION_SOURCE_NOT_FOUND", "办理任务不属于本入口的精确待办或原 OA 身份。")
            actions._binding(service, subject, task_id, command, connection)
            actions._require_grants(service, subject, command["selectedItems"])
            state = task["status"]
            if task["current_operation_id"]:
                operation = connection.execute("SELECT status FROM operations WHERE operation_id=? AND user_subject=?",
                    (task["current_operation_id"], subject)).fetchone()
                if operation and operation[0] in {"unknown", "outcome_unknown"}:
                    state = "outcome_unknown"
            result = {"taskId": task_id, "state": state, "title": task["title"],
                "selectedTitles": [str(item.get("display_summary", {}).get("title") or "")[:500]
                    for item in command["selectedItems"]],
                "terminal": state in _TERMINAL, "message": _status_message(state)}
            interaction_id = task["current_interaction_id"]
        batch = service.tasks.get_batch_for_task(parent_task_id=task_id, user_subject=subject)
        if batch:
            result["batch"] = {"schemaVersion": "agentbridge.batch-view.v1", "totalCount": batch["total_count"],
                "currentIndex": batch["current_ordinal"], "succeededCount": batch["succeeded_count"], "state": batch["state"]}
            if batch["state"] in _TERMINAL:
                result.update(terminal=True, state=batch["state"], message=_status_message(batch["state"]))
        if not result["terminal"] and interaction_id and referenced:
            record = service.interactions.get(interaction_id, user_subject=subject)
            result["canOpen"] = record["interaction_type"] in {"business_input", "execution_authorization"}
            interaction = self._read_active_interaction(context, task_id, interaction_id)
            if interaction:
                result["interaction"] = interaction
        return result

    @staticmethod
    def _request_body(body, keys):
        if (not isinstance(body, dict) or set(body) != set(keys)
                or not isinstance(body.get("taskId"), str)
                or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", body["taskId"])
                or not isinstance(body.get("requestKey"), str)
                or not re.fullmatch(r"[A-Za-z0-9_:-]{16,128}", body["requestKey"])):
            actions._fail("ACTION_REQUEST_INVALID", "恢复或打开请求标识无效。")

    def _recovery_origin(self, connection, subject, task_id, *, current):
        from agentbridge.core.oa_pending_recovery import _origin, recovery_source_context, recovery_permit
        task = actions._owned_task(connection, subject, task_id)
        if json.loads(task["summary_json"]).get("oaPendingRecovery"):
            linked = recovery_source_context(self.service, connection, subject, task_id)
            origin = _origin(self.service, connection, subject, linked["origin"]["task"]["task_id"], current=current)
            if current:
                recovery_permit(self.service, subject=subject, task_id=task_id, session=origin["session"], connection=connection)
            return origin
        return _origin(self.service, connection, subject, task_id, current=current)

    def _source_matches_recovery(self, connection, context, origin):
        """A cross-endpoint exception is only for an attested exact OA affair."""
        subject = context["subject"]
        session = actions._oa_session(connection, subject)
        if (not actions._principal(session) or actions._principal(session)
                != actions._principal(origin["plan"].get("session_binding") or {})):
            return False
        for source in context["sources"]:
            _, pending = actions._source(connection, subject, context["sourceTaskId"], source["operationId"], fresh=False)
            if source.get("sourceHash") and actions._hash(pending) != source["sourceHash"]:
                actions._fail("ACTION_SOURCE_CHANGED", "原待办查询内容已变化。")
            for row in pending["items"]:
                if row["affair_id"] != origin["target"]["affair_id"]:
                    continue
                try:
                    item = actions._select(self.service, pending, [row["affair_id"]])[0]
                except actions.ActionableResultError:
                    continue
                display = item["display_summary"]
                if (display["prepare_capability"] == origin["prepare"]
                        and display["commit_capability"] == origin["operation"]["capability_name"]):
                    return True
        return False

    def _checked_recovery(self, context, task_id):
        with self.service.tasks._connect() as connection:
            origin = self._recovery_origin(connection, context["subject"], task_id, current=True)
            if (origin["task"]["task_id"] != task_id
                    or not self._source_matches_recovery(connection, context, origin)):
                actions._fail("ACTION_SOURCE_NOT_FOUND", "恢复请求不属于本入口的原事项。")
            return origin

    def _recovery_status(self, context, origin):
        from agentbridge.core.oa_pending_recovery import pending_recovery_presentation
        task = origin["task"]
        result = {"taskId": task["task_id"], "state": "outcome_unknown", "terminal": True,
            "title": task["title"], "selectedTitles": [str(origin["target"].get("title") or "OA 待办")[:500]],
            "message": _status_message("outcome_unknown"),
            "oaPendingRecovery": pending_recovery_presentation(self.service, subject=context["subject"], task_id=task["task_id"])}
        batch = self.service.tasks.get_batch_for_task(parent_task_id=task["task_id"], user_subject=context["subject"])
        if batch:
            result["batch"] = {"schemaVersion": "agentbridge.batch-view.v1", "totalCount": batch["total_count"],
                "currentIndex": batch["current_ordinal"], "succeededCount": batch["succeeded_count"], "state": batch["state"]}
        return result

    def recovery_probe(self, token, body):
        from agentbridge.core.oa_pending_recovery import probe_unknown_pending
        self._request_body(body, {"taskId", "requestKey"})
        context = self._context(token)
        origin = self._checked_recovery(context, body["taskId"])
        assessment = probe_unknown_pending(self.service, subject=context["subject"], task_id=body["taskId"],
            endpoint_id=context["endpointId"], request_key=body["requestKey"])
        # Revalidate the private route after the bounded remote read.
        self._context(token)
        return {**self._recovery_status(context, origin), "assessment": assessment}

    def recovery_prepare(self, token, body):
        from agentbridge.core.oa_pending_recovery import prepare_recovery_attempt
        self._request_body(body, {"taskId", "requestKey", "assessmentId", "assessmentHash", "expectedRevision", "acknowledgement"})
        context = self._context(token)
        origin = self._checked_recovery(context, body["taskId"])
        prepared = prepare_recovery_attempt(self.service, subject=context["subject"], task_id=body["taskId"],
            endpoint_id=context["endpointId"], request_key=body["requestKey"],
            assessment_id=body["assessmentId"], assessment_hash=body["assessmentHash"],
            expected_revision=body["expectedRevision"], acknowledgement=body["acknowledgement"])
        self._context(token)
        return {**self._recovery_status(context, origin), "preparationStatus": prepared["status"],
            **({"recoveryTaskId": prepared["taskId"]} if prepared["taskId"] != body["taskId"] else {})}

    def recovery_open(self, token, body):
        from agentbridge.core.oa_pending_recovery import recovery_child_for_origin, recovery_permit
        self._request_body(body, {"taskId", "requestKey"})
        context = self._context(token)
        origin = self._checked_recovery(context, body["taskId"])
        child_id = recovery_child_for_origin(self.service, subject=context["subject"], task_id=body["taskId"])
        if not child_id:
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "请先只读核对并明确确认新的办理。")
        recovery_permit(self.service, subject=context["subject"], task_id=child_id, session=origin["session"])
        child = self.service.tasks.get_task(child_id, user_subject=context["subject"])
        if child["status"] in _TERMINAL:
            return {**self._recovery_status(context, origin), "recoveryTaskId": child_id, "interaction": None}
        interaction = self._open_task(token, context, child_id, body)
        return {**self._recovery_status(context, origin), "recoveryTaskId": child_id, "interaction": interaction}

    def open(self, token, body):
        self._request_body(body, {"taskId", "requestKey"})
        context = self._context(token)
        state = self._status(context, body["taskId"])
        if state.get("oaPendingRecovery"):
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "请通过原事项恢复入口继续。")
        if state["terminal"]:
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "当前事项已结束，请查看结果或原聊天。")
        return {"taskId": body["taskId"], "interaction": self._open_task(token, context, body["taskId"], body)}

    def _open_task(self, token, context, task_id, body):
        """Issue only the current endpoint's presentation after explicit POST."""
        service, subject = self.service, context["subject"]
        task = service.tasks.get_task(task_id, user_subject=subject)
        interaction_id = task.get("current_interaction_id")
        if task["status"] in _TERMINAL or not interaction_id:
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "新的办理界面尚不可打开，请查看当前任务状态。")
        service.validate_skill_task(subject, task_id)
        record = service.interactions.get(interaction_id, user_subject=subject)
        if record["interaction_type"] not in {"business_input", "execution_authorization"}:
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "请回原聊天完成登录后继续。")
        causation = "result-open:" + actions._hash({"subject": subject, "view": context["tokenHash"], "key": body["requestKey"]})
        identity = {"taskId": task_id, "requestedTaskId": body["taskId"], "interactionId": interaction_id,
                    "endpointBindingHash": context["endpointBindingHash"]}
        digest = actions._hash(identity)
        with service.tasks._connect() as connection:
            prior = connection.execute("SELECT payload_json FROM task_events WHERE user_subject=? "
                "AND event_type='task.result_view.opened' AND causation_ref=?", (subject, causation)).fetchall()
            if prior and (len(prior) != 1 or json.loads(prior[0][0]).get("requestHash") != digest):
                actions._fail("ACTION_REQUEST_INVALID", "此打开请求已绑定另一办理界面。")
        service.present_interaction(user_subject=subject, agent_host=context["endpoint"]["agent_host"],
            endpoint_key=context["endpoint"]["endpoint_key"], interaction_id=interaction_id, continuous_flow=True)
        fresh = self._context(token)
        if fresh["endpointBindingHash"] != context["endpointBindingHash"]:
            actions._fail("ACTION_ENDPOINT_INVALID", "Telegram 会话已变化。")
        if body["taskId"] != task_id:
            from agentbridge.core.oa_pending_recovery import recovery_permit
            origin = self._checked_recovery(fresh, body["taskId"])
            recovery_permit(service, subject=subject, task_id=task_id, session=origin["session"])
        else:
            self._status(fresh, task_id)
        if service.tasks.get_task(task_id, user_subject=subject).get("current_interaction_id") != interaction_id:
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "办理界面已变化，请重新打开当前事项。")
        interaction = self._read_active_interaction(context, task_id, interaction_id)
        if not interaction:
            actions._fail("ACTION_SOURCE_UNAVAILABLE", "当前办理界面已不可操作，请查看任务结果。")
        with store_transaction(service.tasks) as connection:
            prior = connection.execute("SELECT payload_json FROM task_events WHERE user_subject=? "
                "AND event_type='task.result_view.opened' AND causation_ref=?", (subject, causation)).fetchall()
            if prior and (len(prior) != 1 or json.loads(prior[0][0]).get("requestHash") != digest):
                actions._fail("ACTION_REQUEST_INVALID", "此打开请求已绑定另一办理界面。")
            if not prior:
                service.tasks._append_event(connection, task_id=task_id, user_subject=subject,
                    event_type="task.result_view.opened", payload={"requestHash": digest, **identity,
                        "viewTokenHash": context["tokenHash"], "presentationId": interaction["presentation"]["presentationId"]},
                    causation_ref=causation, created_at=actions._now().isoformat())
        url = urlsplit(interaction["presentation"]["url"])
        interaction["presentation"]["url"] = urlunsplit(url._replace(query=urlencode({"result": token})))
        return interaction

    def navigation_back(self, token, *, task_id, interaction_id, endpoint_id, presentation_id):
        """Validate a supplied return ticket without persisting or minting one."""
        context = self._context(token)
        if not presentation_id or endpoint_id != context["endpointId"]:
            actions._fail("ACTION_ENDPOINT_INVALID", "返回入口与当前页面端点不一致。")
        self._status(context, task_id)  # Includes exact source/Skill/principal checks.
        subject = context["subject"]
        with self.service.tasks._connect() as connection:
            current = connection.execute("SELECT i.* FROM interactions i JOIN task_interactions t "
                "ON t.interaction_id=i.interaction_id AND t.user_subject=i.user_subject "
                "WHERE i.interaction_id=? AND i.user_subject=? AND t.task_id=?",
                (interaction_id, subject, task_id)).fetchone()
            if not current or current["interaction_type"] not in {"business_input", "execution_authorization"}:
                actions._fail("ACTION_SOURCE_NOT_FOUND", "返回入口不属于当前办理界面。")
            table, id_column = (("field_submission_presentations", "submission_id")
                if current["interaction_type"] == "business_input"
                else ("write_authorization_presentations", "authorization_id"))
            presentation = connection.execute(f"SELECT * FROM {table} WHERE {id_column}=? "
                "AND presentation_id=? AND endpoint_id=? AND user_subject=? AND state IN ('active','submitted','decided')",
                (current["resource_id"], presentation_id, endpoint_id, subject)).fetchone()
            if not presentation or actions._date(presentation["expires_at"]) <= actions._now():
                actions._fail("ACTION_SOURCE_NOT_FOUND", "返回入口的当前页面凭据已不可用。")
            # An explicit POST binds the exact source presentation. A receipt
            # may inherit navigation only through its immutable draft decision,
            # never merely because it shares a task or an OA affair.
            sql = ("SELECT 1 FROM task_events WHERE task_id=? AND user_subject=? "
                "AND event_type='task.result_view.opened' AND json_extract(payload_json,'$.viewTokenHash')=? "
                "AND json_extract(payload_json,'$.interactionId')=? "
                "AND json_extract(payload_json,'$.presentationId')=? "
                "AND json_extract(payload_json,'$.endpointBindingHash')=? LIMIT 1")
            row = connection.execute(sql, (task_id, subject, context["tokenHash"], interaction_id,
                presentation_id, context["endpointBindingHash"])).fetchone()
            if not row and current["interaction_type"] == "execution_authorization":
                row = connection.execute("SELECT 1 "
                    "FROM business_draft_decisions r JOIN business_drafts d ON d.draft_id=r.draft_id "
                    "AND d.source_submission_id=r.source_submission_id AND d.task_id=r.task_id "
                    "AND d.user_subject=r.user_subject JOIN field_submission_presentations p "
                    "ON p.submission_id=r.source_submission_id AND p.user_subject=r.user_subject "
                    "JOIN task_events e ON e.task_id=r.task_id AND e.user_subject=r.user_subject "
                    "AND e.event_type='task.result_view.opened' "
                    "AND json_extract(e.payload_json,'$.interactionId')=d.source_interaction_id "
                    "AND json_extract(e.payload_json,'$.presentationId')=p.presentation_id "
                    "WHERE r.task_id=? AND r.user_subject=? AND r.interaction_id=? AND r.authorization_id=? "
                    "AND p.endpoint_id=? AND p.state IN ('active','submitted') "
                    "AND json_extract(e.payload_json,'$.viewTokenHash')=? "
                    "AND json_extract(e.payload_json,'$.endpointBindingHash')=? LIMIT 1",
                    (task_id, subject, interaction_id, current["resource_id"], endpoint_id,
                     context["tokenHash"], context["endpointBindingHash"])).fetchone()
            if not row:
                actions._fail("ACTION_SOURCE_NOT_FOUND", "返回入口没有当前页面的可信打开记录。")
        return {"kind": "result_list", "url": "/results/" + token, "label": "返回待办列表"}

    def _read_active_interaction(self, context, task_id, interaction_id):
        """Read an existing endpoint presentation; never create/claim/decide on GET."""
        from agentbridge.core.interactions import _interaction_from_row, build_interaction_envelope
        from agentbridge.core.field_submissions import _submission_from_row
        from agentbridge.core.write_authorizations import _authorization_from_row
        service, subject = self.service, context["subject"]
        with service.tasks._connect() as connection:
            row = connection.execute("SELECT i.* FROM interactions i JOIN task_interactions t ON t.interaction_id=i.interaction_id "
                "WHERE i.interaction_id=? AND i.user_subject=? AND t.task_id=? AND t.user_subject=?",
                (interaction_id, subject, task_id, subject)).fetchone()
            if row is None or actions._date(row["expires_at"]) <= actions._now():
                return None
            record = _interaction_from_row(row)
            kind = record["interaction_type"]
            if kind == "business_input":
                resource = connection.execute("SELECT * FROM field_submissions WHERE submission_id=? AND user_subject=?",
                    (record["resource_id"], subject)).fetchone()
                presentation = connection.execute("SELECT * FROM field_submission_presentations WHERE submission_id=? "
                    "AND endpoint_id=? AND user_subject=? AND state='active'", (record["resource_id"], context["endpointId"], subject)).fetchone()
                if resource is None:
                    return None
                service.field_submissions._verify_integrity(resource, include_values=False)
                resource = _submission_from_row(resource, include_values=False)
            elif kind == "execution_authorization":
                resource = connection.execute("SELECT * FROM write_authorizations WHERE authorization_id=? AND user_subject=?",
                    (record["resource_id"], subject)).fetchone()
                presentation = connection.execute("SELECT * FROM write_authorization_presentations WHERE authorization_id=? "
                    "AND endpoint_id=? AND user_subject=? AND state='active'", (record["resource_id"], context["endpointId"], subject)).fetchone()
                if resource is None:
                    return None
                resource = _authorization_from_row(resource, include_plan=False)
            else:
                return None  # Login remains available in the original trusted chat card.
            if (presentation is None or actions._date(presentation["expires_at"]) <= actions._now()
                    or resource["state"] != "pending" or resource["session_id"] != record["session_id"]
                    or resource["system_id"] != record["system_id"]):
                return None
            session = actions._oa_session(connection, subject)
            if (session.get("state") != "active" or session.get("session_id") != record["session_id"]
                    or not actions._principal(session)):
                return None
            trusted = urlsplit(str(service.trusted_card_base_url or ""))
            candidate = urlsplit(presentation["card_url"])
            if (candidate.scheme != trusted.scheme or candidate.netloc != trusted.netloc
                    or candidate.username or candidate.password or candidate.query or candidate.fragment):
                return None
            envelope = build_interaction_envelope(record, resource)
            envelope["presentation"].update(url=presentation["card_url"], endpointId=context["endpointId"],
                presentationId=presentation["presentation_id"], individualized=True)
            return continuous_flow_presentation(envelope, task_id=task_id, enabled=True)


def _status_message(state):
    if state in {"outcome_unknown", "unknown"}:
        return "业务结果待核实，请先到业务系统核对，不要重复提交。"
    return {"succeeded": "业务操作已通过原有结果核验。", "failed": "办理未完成，请查看原聊天的失败说明。",
        "canceled": "本次办理已取消。", "expired": "本次办理已过期，请返回原聊天查看。",
        "superseded": "此卡已被替代，请回原聊天查看当前办理卡片和结果。",
        "skipped": "此事项已跳过，未在本次批次办理；请查看原任务。",
        "completed": "本次任务已结束，请回原聊天查看具体办理结果。",
        "partially_succeeded": "部分事项已完成，其余事项请核对原任务。",
        "waiting_user": "办理界面已准备，核对材料后再最终确认。"}.get(state, "正在准备或继续办理，请稍候；此状态不代表业务已成功。")
