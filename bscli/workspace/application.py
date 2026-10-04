from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

from bscli.core.central_service import CentralCapabilityService
from bscli.core.user_grants import UserGrantConflict
from bscli.core.task_plans import task_plan_response
from bscli.core.timeline_attachments import (
    TimelineAttachmentExpired,
    TimelineAttachmentIntegrityError,
    TimelineAttachmentNotFound,
    public_attachment,
)
from bscli.workspace.gateway import GatewayRequestError, OpenClawGatewayClient


_LOG = logging.getLogger(__name__)

MAX_CHAT_ATTACHMENTS = 4
MAX_CHAT_ATTACHMENT_BYTES = 6 * 1024 * 1024
MAX_CHAT_ATTACHMENTS_TOTAL_BYTES = 12 * 1024 * 1024
_CHAT_IMAGE_TYPES = {
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
    "image/webp": (b"RIFF", ".webp"),
}
_HOST_DISPATCH_RETRY_DELAYS = (1.0, 2.0, 3.0, 5.0, 8.0, 10.0)
_HOST_TRANSPORT_ERROR_CODES = {
    "GATEWAY_CONNECTION_CLOSED",
    "GATEWAY_CONNECTION_FAILED",
    "GATEWAY_PROCESS_FAILED",
    "GATEWAY_TIMEOUT",
}
_HOST_SAFE_PRE_ACCEPT_STAGES = {"connect", "preflight_abort", "bind"}
_HOST_DISPATCH_TERMINAL_STATES = {
    "completed",
    "failed",
    "expired",
    "failed_before_accept",
    "acceptance_unknown",
    "result_unknown",
    "canceled",
}


class WorkspaceArtifactError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceChatResult:
    run_id: str | None
    status: str


class WorkspaceApplication:
    def __init__(
        self,
        *,
        service: CentralCapabilityService,
        gateway: OpenClawGatewayClient | None = None,
    ) -> None:
        self.service = service
        self.store = service.workspace
        self.gateway = gateway
        self._dispatch_instance_id = f"workspace-{uuid4()}"
        self._dispatch_stop = threading.Event()
        self._dispatch_workers_lock = threading.Lock()
        self._dispatch_workers: dict[str, threading.Thread] = {}
        self._readiness_lock = threading.Lock()
        self._readiness_cache: tuple[float, dict] | None = None
        if gateway is not None:
            for account_id in self.store.recover_host_dispatches():
                self._ensure_dispatch_worker(account_id)

    def start_workbench(self):
        if self.gateway is not None:
            self.service.skill_authoring.workbench.start(self._skill_completion, self._capture_skill_method)

    def _skill_completion(self, system: str, prompt: str) -> dict:
        return self._gateway().call('agentbridge.skills.complete', {'system': system, 'prompt': prompt}, timeout_seconds=100)

    def _capture_skill_method(self, capture):
        try:
            dispatch = self.store.get_host_dispatch(capture['dispatch_id'], user_subject=capture['owner_subject'])
            account = self.store.get_account(dispatch['account_id'])
            material, attachments = self._host_dispatch_payload(dispatch)
            if not attachments and not re.search(r'(创建|做个|发布|审批|评测|试用|沉淀).{0,10}(助手|skill|草稿)|草稿', material, re.I):
                self.service.skill_authoring.workbench.generate(account['user_subject'],
                    material=('用户：' + material + '\n助手：' + capture['assistant_text'])[:16000],
                    request_key='workspace:' + dispatch['dispatch_id'], automatic=True,
                    scope='workspace:' + account['account_id'])
        except (PermissionError, ValueError, KeyError, UserGrantConflict):
            return  # Opt-out, unavailable material and quota cannot affect a completed task.

    def start_enrollment(self) -> dict:
        return self.store.start_link()

    def enrollment_status(self, enrollment_token: str) -> dict | None:
        return self.store.link_status(enrollment_token)

    def complete_enrollment(
        self,
        *,
        enrollment_token: str,
        username: str,
        password: str,
    ) -> dict:
        account = self.store.create_account(
            enrollment_token=enrollment_token,
            username=username,
            password=password,
        )
        registered = self.service.register_workspace_endpoint(
            account_id=account["account_id"],
        )
        session = self.store.create_session(account["account_id"])
        return {
            "account": _public_account(registered["account"]),
            "session": session,
        }

    def login(self, *, username: str, password: str) -> dict | None:
        account = self.store.authenticate(
            username=username,
            password=password,
        )
        if account is None:
            return None
        account = self.store.record_login(account["account_id"])
        session = self.store.create_session(account["account_id"])
        return {
            "account": _public_account(account),
            "session": session,
        }

    def session(
        self,
        session_token: str | None,
        *,
        csrf_token: str | None = None,
        touch: bool = True,
    ) -> dict | None:
        account = self.store.verify_session(
            session_token,
            csrf_token=csrf_token,
            touch=touch,
        )
        if account is not None and touch and account.get("endpoint_id"):
            try:
                self.service.tasks.touch_endpoint(
                    endpoint_id=account["endpoint_id"],
                    user_subject=account["user_subject"],
                )
            except (KeyError, RuntimeError, ValueError, sqlite3.Error):
                # Session verification remains available during optional
                # Task Hub projection failures.
                pass
        return account

    @staticmethod
    def public_account(account: dict | None) -> dict | None:
        return _public_account(account) if account else None

    def logout(self, session_token: str | None) -> None:
        self.store.revoke_session(session_token)

    def list_tasks(
        self,
        account: dict,
        *,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        return [
            _public_task(task)
            for task in self.service.tasks.list_tasks(
                user_subject=account["user_subject"],
                active_only=active_only,
                limit=limit,
            )
        ]

    def task_detail(self, account: dict, task_id: str) -> dict:
        self.service.require_task_result_access(user_subject=account["user_subject"], task_id=task_id)
        task = self.service.tasks.get_task(
            task_id,
            user_subject=account["user_subject"],
        )
        events = self.service.tasks.list_events(
            task_id=task_id,
            user_subject=account["user_subject"],
            limit=200,
        )
        artifacts = self.service.tasks.list_artifacts(
            task_id=task_id,
            user_subject=account["user_subject"],
            limit=100,
        )
        artifacts.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                str(item.get("artifact_id") or ""),
            )
        )
        interactions = []
        for linked in self.service.tasks.list_task_interactions(
            task_id=task_id,
            user_subject=account["user_subject"],
            limit=100,
        ):
            try:
                response = self.service.get_interaction(
                    user_subject=account["user_subject"],
                    interaction_id=linked["interaction_id"],
                )
                interaction = response["interaction"]
                if interaction.get("state") in {"pending", "processing"}:
                    response = self.service.present_interaction(
                        user_subject=account["user_subject"],
                        agent_host="openclaw",
                        endpoint_key=account["endpoint_key"],
                        interaction_id=linked["interaction_id"],
                    )
                    interaction = response["interaction"]
                interactions.append(
                    {
                        **interaction,
                        "linkedAt": linked["linked_at"],
                        "lastObservedAt": linked["last_observed_at"],
                    }
                )
            except (KeyError, RuntimeError):
                continue
        interaction = next(
            (
                item
                for item in interactions
                if item.get("interactionId")
                == task.get("current_interaction_id")
            ),
            None,
        )
        plan = self.service.task_plans.get_for_task(
            parent_task_id=task_id,
            user_subject=account["user_subject"],
        )
        return {
            "task": _public_task(task),
            "skill": self.service.skills.task_presentation(account["user_subject"], task_id),
            "plan": task_plan_response(plan) if plan is not None else None,
            "events": [_public_event(event) for event in events],
            "artifacts": [_public_artifact(item) for item in artifacts],
            "interaction": interaction,
            "interactions": interactions,
        }

    def artifact_history(self, account: dict, *, limit: int = 20) -> list[dict]:
        limit = min(max(int(limit), 1), 50)
        artifacts = self.service.tasks.list_user_artifacts(
            user_subject=account["user_subject"],
            limit=min(max(limit * 10, 100), 500),
        )
        artifacts.sort(
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )
        grouped: dict[str, list[dict]] = {}
        for artifact in artifacts:
            grouped.setdefault(artifact["task_id"], []).append(artifact)
        items = []
        for task_id, task_artifacts in grouped.items():
            if len(items) >= limit:
                break
            task = self.service.tasks.get_task(
                task_id,
                user_subject=account["user_subject"],
            )
            task_artifacts.sort(
                key=lambda item: (
                    str(item.get("created_at") or ""),
                    str(item.get("artifact_id") or ""),
                )
            )
            items.append(
                {
                    "task": _public_task(task),
                    "plan": None,
                    "events": [],
                    "artifacts": [
                        _public_artifact(item) for item in task_artifacts
                    ],
                    "interaction": None,
                }
            )
        return items

    def analytics_report(self, account: dict, report_id: str) -> dict:
        raise WorkspaceArtifactError('CAPABILITY_RETIRED', '旧数据库报告入口已退役。请重新查询导出。')

    def database_report(self, account: dict, source_id: str, report_id: str) -> dict:
        from bscli.database.independent import IndependentDatabase, DatabaseRejected
        from bscli.database.reports import Reports
        try:
            return Reports(IndependentDatabase(self.service.home)).web_download(account['user_subject'],source_id,report_id)
        except DatabaseRejected as exc:
            raise WorkspaceArtifactError(str(exc),'此文件不可下载，请检查权限或重新导出。') from exc

    def database_original(self, account: dict, source_id: str, log_id: str) -> dict:
        from bscli.database.independent import IndependentDatabase
        return IndependentDatabase(self.service.home).read_original(account['user_subject'], source_id, log_id)

    def reissue_artifact(
        self,
        account: dict,
        *,
        task_id: str,
        artifact_id: str,
    ) -> dict:
        result = self.service.reissue_document_download(
            user_subject=account["user_subject"],
            task_id=task_id,
            artifact_id=artifact_id,
        )
        if result.get("status") != "succeeded":
            code = str(result.get("error", {}).get("code") or "REISSUE_FAILED")
            messages = {
                "LOGIN_REQUIRED": "对应系统登录已失效，请先登录后再重新生成下载。",
                "DOWNLOAD_NOT_FOUND": "原文件记录不存在，无法重新生成下载。",
                "DOWNLOAD_ACCESS_DENIED": "无权重新生成这份文件。",
                "DOWNLOAD_INTEGRITY_FAILED": "原文件记录校验失败，无法重新生成下载。",
                "ARTIFACT_REISSUE_UNSUPPORTED": "这个文件暂不支持重新生成下载。",
                "ARTIFACT_REISSUE_CONFLICT": "文件已在另一端更新，请刷新后重试。",
                "REPORT_REGENERATION_FAILED": "报告数据暂时无法重新读取，请稍后再试。",
            }
            raise WorkspaceArtifactError(
                code,
                messages.get(code, "重新生成文件失败，请稍后再试。"),
            )
        return self.task_detail(account, task_id)

    def continue_task(self, account: dict, task_id: str) -> dict:
        response = self.service.resolve_host_task_continuation(
            user_subject=account["user_subject"],
            agent_host="openclaw",
            endpoint_key=account["endpoint_key"],
            task_id=task_id,
            reuse_selected=False,
            allow_follow_up=False,
        )
        if response.get("status") != "selected":
            raise KeyError(f"task not found: {task_id}")
        task = response["task"]
        return {
            "status": "selected",
            "task": {
                "task_id": task["taskId"],
                "title": task["title"],
                "status": task["status"],
            },
            "continuation": response["continuation"],
            "message": f"继续刚才选择的“{task['title']}”任务。",
        }

    def list_events(
        self,
        account: dict,
        *,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        return [
            _public_event(event)
            for event in self.service.tasks.list_user_events(
                user_subject=account["user_subject"],
                after_event_id=after_event_id,
                limit=limit,
            )
        ]

    def latest_event_id(self, account: dict) -> str | None:
        return self.service.tasks.latest_user_event_id(
            user_subject=account["user_subject"],
        )

    def event_cursor(self, account: dict) -> str:
        return self.service.tasks.current_user_event_cursor(
            user_subject=account["user_subject"],
        )

    def list_timeline(
        self,
        account: dict,
        *,
        after_sequence: int | None = None,
        before_sequence: int | None = None,
        entry_type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        endpoints = {
            endpoint["endpoint_id"]: endpoint
            for endpoint in self.service.tasks.list_endpoints(
                user_subject=account["user_subject"],
                active_only=False,
                limit=100,
            )
        }
        return [
            _public_timeline_entry(
                entry,
                source=endpoints.get(entry.get("source_endpoint_id")),
                is_origin=(
                    entry.get("source_endpoint_id") == account.get("endpoint_id")
                ),
            )
            for entry in self.service.tasks.list_timeline(
                user_subject=account["user_subject"],
                after_sequence=after_sequence,
                before_sequence=before_sequence,
                entry_type=entry_type,
                limit=limit,
            )
        ]

    def timeline_cursor(self, account: dict) -> int:
        return self.service.tasks.latest_timeline_sequence(
            user_subject=account["user_subject"],
        )

    def timeline_attachment(self, account: dict, attachment_id: str) -> dict:
        try:
            attachment = self.service.timeline_attachments.ready_payload(
                attachment_id
            )
        except TimelineAttachmentNotFound as exc:
            raise WorkspaceArtifactError(
                "TIMELINE_ATTACHMENT_NOT_FOUND",
                "The timeline attachment was not found.",
            ) from exc
        except TimelineAttachmentExpired as exc:
            raise WorkspaceArtifactError(
                "TIMELINE_ATTACHMENT_EXPIRED",
                "The timeline attachment has expired.",
            ) from exc
        except TimelineAttachmentIntegrityError as exc:
            raise WorkspaceArtifactError(
                "TIMELINE_ATTACHMENT_INTEGRITY_FAILED",
                "The timeline attachment failed its integrity check.",
            ) from exc
        if attachment["user_subject"] != account["user_subject"]:
            raise PermissionError("timeline attachment belongs to another user")
        return attachment

    def list_endpoints(self, account: dict) -> list[dict]:
        return [
            _public_endpoint(endpoint)
            for endpoint in self.service.tasks.list_endpoints(
                user_subject=account["user_subject"],
                active_only=False,
                limit=100,
            )
        ]

    def gateway_status(self) -> dict:
        if self.gateway is None:
            return {
                "available": False,
                "code": "GATEWAY_NOT_CONFIGURED",
            }
        try:
            result = self.gateway.call(
                "system.info",
                {},
                timeout_seconds=8,
            )
        except GatewayRequestError as exc:
            return {
                "available": False,
                "code": exc.code,
            }
        return {
            "available": True,
            "version": _safe_text(
                result.get("version") if isinstance(result, dict) else None,
                80,
            ),
        }

    def chat_history(self, account: dict, *, limit: int = 100) -> dict:
        gateway = self._gateway()
        result = gateway.call(
            "chat.history",
            {
                "sessionKey": account["openclaw_session_key"],
                "limit": min(max(int(limit), 1), 200),
                "maxChars": 200_000,
            },
            timeout_seconds=20,
        )
        return {
            "messages": _visible_messages(result),
            "sessionId": (
                _safe_text(result.get("sessionId"), 256)
                if isinstance(result, dict)
                else None
            ),
        }

    def chat_stream(
        self,
        account: dict,
        *,
        timeout_seconds: float = 25,
    ) -> Iterator[dict[str, Any]]:
        return self._gateway().stream(
            session_key=account["openclaw_session_key"],
            timeout_seconds=timeout_seconds,
        )

    def send_chat_stream(
        self,
        account: dict,
        *,
        message: str,
        idempotency_key: str | None = None,
        attachments: list[dict] | None = None,
    ) -> Iterator[dict[str, Any]]:
        self._gateway()
        message = str(message or "").strip()
        normalized_attachments = _validated_chat_attachments(attachments)
        if not message and normalized_attachments:
            message = "请处理附加图片中的内容。"
        if not message or len(message) > 20_000:
            raise ValueError("chat message is empty or too long")
        effective_key = _safe_text(idempotency_key, 128) or str(uuid4())
        dispatch, _reused = self.store.accept_chat_request(
            account_id=account["account_id"],
            user_subject=account["user_subject"],
            message=message,
            idempotency_key=effective_key,
            attachments=normalized_attachments,
            tasks=self.service.tasks,
            attachment_store=self.service.timeline_attachments,
            media_base_url=self.service.trusted_card_base_url,
        )
        self._ensure_dispatch_worker(account["account_id"])
        return self._stream_host_dispatch(
            user_subject=account["user_subject"],
            dispatch_id=dispatch["dispatch_id"],
        )

    def list_chat_dispatches(
        self,
        account: dict,
        *,
        active_only: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        items = []
        for dispatch in self.store.list_host_dispatches(
            user_subject=account["user_subject"],
            active_only=active_only,
            limit=limit,
        ):
            try:
                message = self.service.tasks.get_timeline_message(
                    user_subject=account["user_subject"],
                    message_key=dispatch["message_key"],
                )
            except Exception:
                message = {"text": ""}
            items.append(
                {
                    "dispatchId": dispatch["dispatch_id"],
                    "state": dispatch["state"],
                    "runId": dispatch["accepted_run_id"],
                    "idempotencyKey": dispatch["idempotency_key"],
                    "requestMessage": message.get("text") or "",
                    "attemptCount": dispatch["attempt_count"],
                    "retryCount": dispatch["retry_count"],
                    "createdAt": dispatch["created_at"],
                    "updatedAt": dispatch["updated_at"],
                    "deadlineAt": dispatch["deadline_at"],
                    "lastErrorCode": dispatch["last_error_code"],
                }
            )
        return items

    def cancel_chat_dispatch(
        self,
        account: dict,
        dispatch_id: str,
    ) -> dict:
        dispatch = self.store.cancel_host_dispatch(
            dispatch_id,
            user_subject=account["user_subject"],
        )
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text="已取消等待，本次请求未进入业务系统。",
        )
        return {
            "dispatchId": dispatch["dispatch_id"],
            "state": dispatch["state"],
        }

    def close(self) -> None:
        self.service.skill_authoring.workbench.close()
        self._dispatch_stop.set()
        with self._dispatch_workers_lock:
            workers = list(self._dispatch_workers.values())
        for worker in workers:
            worker.join(timeout=2)

    def _ensure_dispatch_worker(self, account_id: str) -> None:
        if self.gateway is None or self._dispatch_stop.is_set():
            return
        with self._dispatch_workers_lock:
            existing = self._dispatch_workers.get(account_id)
            if existing is not None and existing.is_alive():
                return
            worker = threading.Thread(
                target=self._dispatch_worker,
                args=(account_id,),
                name=f"workspace-dispatch-{account_id[:8]}",
                daemon=True,
            )
            self._dispatch_workers[account_id] = worker
            worker.start()

    def _dispatch_worker(self, account_id: str) -> None:
        current = threading.current_thread()
        try:
            while not self._dispatch_stop.is_set():
                dispatch = self.store.claim_next_host_dispatch(
                    account_id=account_id,
                    claim_owner=self._dispatch_instance_id,
                )
                if dispatch is None:
                    if not self.store.host_dispatch_work_pending(account_id):
                        return
                    self._dispatch_stop.wait(0.2)
                    continue
                try:
                    self._process_host_dispatch(dispatch)
                except Exception as exc:
                    _LOG.exception(
                        "Workspace durable dispatch failed dispatch_id=%s "
                        "error=%s",
                        dispatch["dispatch_id"],
                        exc.__class__.__name__,
                    )
                    self._finish_dispatch_internal_error(dispatch, exc)
        finally:
            with self._dispatch_workers_lock:
                if self._dispatch_workers.get(account_id) is current:
                    self._dispatch_workers.pop(account_id, None)

    def _process_host_dispatch(self, dispatch: dict) -> None:
        if dispatch["state"] == "accepted":
            self._observe_accepted_dispatch(dispatch)
            return
        if self.store.host_dispatch_deadline_passed(dispatch):
            account = self.store.get_account(dispatch["account_id"])
            if dispatch.get("request_sent_at"):
                self._finish_acceptance_unknown(dispatch, account)
            else:
                self._finish_expired_dispatch(dispatch, account)
            return
        if dispatch["state"] == "reconciling_acceptance":
            self._reconcile_host_acceptance(dispatch)
            return

        account = self.store.get_account(dispatch["account_id"])
        ready = self._host_readiness()
        if not ready["available"]:
            if ready["recoverable"]:
                self._defer_dispatch(dispatch, ready["code"])
            else:
                self._fail_dispatch_before_accept(
                    dispatch,
                    account,
                    code=ready["code"],
                    text="智能体连接配置无效，本次请求未进入业务系统。",
                )
            return

        try:
            message, attachments = self._host_dispatch_payload(dispatch)
        except Exception as exc:
            self._fail_dispatch_before_accept(
                dispatch,
                account,
                code="HOST_DISPATCH_PAYLOAD_UNAVAILABLE",
                text="原消息或附件已不可用，本次请求未进入业务系统。",
            )
            _LOG.warning(
                "Workspace durable payload unavailable dispatch_id=%s error=%s",
                dispatch["dispatch_id"],
                exc.__class__.__name__,
            )
            return

        dispatch = self.store.record_host_dispatch_attempt(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
        )
        accepted = False
        terminal = False
        run_id = ""
        source = None
        try:
            grant = self.store.issue_gateway_grant(account["account_id"])
            source = self._gateway().send_stream(
                session_key=grant["session_key"],
                endpoint_key=grant["endpoint_key"],
                grant=grant["grant"],
                binding_grant_provider=lambda: self.store.issue_gateway_grant(
                    account["account_id"]
                )["grant"],
                message=message,
                idempotency_key=dispatch["idempotency_key"],
                attachments=attachments,
                timeout_seconds=300,
                retry_before_accept=False,
                abort_accepted_on_close=False,
            )
            for item in source:
                item_type = item.get("type")
                if item_type == "accepted":
                    run_id = (
                        _safe_text(item.get("runId"), 256)
                        or dispatch["idempotency_key"]
                    )
                    dispatch = self.store.mark_host_dispatch_accepted(
                        dispatch["dispatch_id"],
                        claim_token=dispatch["claim_token"],
                        run_id=run_id,
                        status=_safe_text(item.get("status"), 80)
                        or "started",
                        recovered_from_run_id=_safe_text(item.get("recoveredFromRunId"), 256) or None,
                    )
                    accepted = True
                    recovery_attempts = dispatch["retry_count"] + max(
                        dispatch["attempt_count"] - 1,
                        0,
                    )
                    if recovery_attempts:
                        self._record_gateway_recovery(
                            account=account,
                            effective_key=dispatch["idempotency_key"],
                            attempt=recovery_attempts,
                            status="succeeded",
                        )
                    continue
                if item_type == "progress":
                    self.store.append_host_dispatch_stream_event(
                        dispatch["dispatch_id"],
                        claim_token=dispatch["claim_token"],
                        event_name="host_progress",
                        payload=item,
                        had_tool_activity=(item.get("kind") == "tool"),
                    )
                    continue
                if item_type != "chat":
                    continue
                state = item.get("state")
                if state not in {"final", "error", "aborted"}:
                    self.store.append_host_dispatch_stream_event(
                        dispatch["dispatch_id"],
                        claim_token=dispatch["claim_token"],
                        event_name="host_chat_delta",
                        payload=item,
                    )
                    continue
                self._finish_accepted_dispatch(
                    dispatch,
                    account,
                    item,
                )
                terminal = True
                return
            if accepted and not terminal:
                self._observe_accepted_dispatch(dispatch)
                return
            if not accepted:
                self._reconcile_host_acceptance(dispatch)
        except GatewayRequestError as exc:
            _LOG.warning(
                "Workspace durable Gateway failure dispatch_id=%s run_id=%s "
                "code=%s stage=%s accepted=%s",
                dispatch["dispatch_id"],
                run_id,
                exc.code,
                _safe_text(exc.details.get("stage"), 80),
                accepted,
            )
            if accepted:
                self._observe_accepted_dispatch(dispatch)
            elif _is_safe_recoverable_pre_accept_error(exc):
                self._defer_dispatch(dispatch, exc.code)
            elif _may_have_reached_host(exc):
                dispatch = self.store.mark_host_dispatch_reconciling(
                    dispatch["dispatch_id"],
                    claim_token=dispatch["claim_token"],
                    error_code=exc.code,
                )
                self._reconcile_host_acceptance(dispatch)
            else:
                self._fail_dispatch_before_accept(
                    dispatch,
                    account,
                    code=exc.code,
                    text=_workspace_gateway_failure_text(exc),
                )
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()

    def _host_readiness(self) -> dict:
        now = time.monotonic()
        with self._readiness_lock:
            if self._readiness_cache and now - self._readiness_cache[0] <= 2:
                return dict(self._readiness_cache[1])
        try:
            self._gateway().call("system.info", {}, timeout_seconds=5)
            result = {"available": True, "recoverable": False, "code": None}
        except GatewayRequestError as exc:
            result = {
                "available": False,
                "recoverable": exc.code in _HOST_TRANSPORT_ERROR_CODES,
                "code": exc.code,
            }
        with self._readiness_lock:
            self._readiness_cache = (time.monotonic(), dict(result))
        return result

    def _host_dispatch_payload(
        self,
        dispatch: dict,
    ) -> tuple[str, list[dict]]:
        entry = self.service.tasks.get_timeline_message(
            user_subject=dispatch["user_subject"],
            message_key=dispatch["message_key"],
        )
        attachments = []
        for attachment_id in dispatch["attachment_refs"]:
            record = self.service.timeline_attachments.ready_payload(
                attachment_id
            )
            if (
                record["user_subject"] != dispatch["user_subject"]
                or record["message_key"] != dispatch["message_key"]
            ):
                raise PermissionError(
                    "host dispatch attachment belongs to another message"
                )
            attachments.append(
                {
                    "type": "image",
                    "mimeType": record["content_type"],
                    "fileName": record["filename"],
                    "content": base64.b64encode(record["body"]).decode("ascii"),
                }
            )
        return str(entry["text"]), attachments

    def _defer_dispatch(self, dispatch: dict, error_code: str) -> None:
        retry_count = max(int(dispatch.get("retry_count") or 0), 0)
        delay = _HOST_DISPATCH_RETRY_DELAYS[
            min(retry_count, len(_HOST_DISPATCH_RETRY_DELAYS) - 1)
        ]
        updated = self.store.mark_host_dispatch_waiting(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            error_code=error_code,
            delay_seconds=delay,
        )
        if updated["state"] == "expired":
            account = self.store.get_account(updated["account_id"])
            self._append_workspace_failure(
                account,
                effective_key=updated["idempotency_key"],
                text=(
                    "连接暂未恢复，本次请求未进入业务系统，请稍后重试。"
                ),
            )

    def _reconcile_host_acceptance(self, dispatch: dict) -> None:
        account = self.store.get_account(dispatch["account_id"])
        try:
            evidence = self._gateway().run_history_evidence(
                session_key=dispatch["conversation_ref"],
                run_ref=dispatch["idempotency_key"],
                timeout_seconds=8,
            )
        except AttributeError:
            self._finish_acceptance_unknown(dispatch, account)
            return
        except GatewayRequestError as exc:
            if exc.code in _HOST_TRANSPORT_ERROR_CODES:
                self._defer_acceptance_reconciliation(dispatch, account, exc.code)
            else:
                self._finish_acceptance_unknown(dispatch, account)
            return
        if evidence.get("prompt_observed"):
            run_id = (
                _safe_text(evidence.get("observed_run_id"), 256)
                or dispatch["idempotency_key"]
            )
            dispatch = self.store.mark_host_dispatch_accepted(
                dispatch["dispatch_id"],
                claim_token=dispatch["claim_token"],
                run_id=run_id,
                status="recovered",
            )
            if evidence.get("final_text"):
                self._finish_accepted_dispatch(
                    dispatch,
                    account,
                    {
                        "type": "chat",
                        "runId": run_id,
                        "state": "final",
                        "text": evidence["final_text"],
                        "recovered": True,
                    },
                )
            else:
                self._observe_accepted_dispatch(dispatch)
            return
        self._defer_acceptance_reconciliation(
            dispatch,
            account,
            "HOST_ACCEPTANCE_NOT_OBSERVED",
        )

    def _defer_acceptance_reconciliation(
        self,
        dispatch: dict,
        account: dict,
        error_code: str,
    ) -> None:
        updated = self.store.defer_host_dispatch_reconciliation(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            error_code=error_code,
            delay_seconds=2,
        )
        if updated["state"] == "acceptance_unknown":
            self._append_workspace_failure(
                account,
                effective_key=dispatch["idempotency_key"],
                text=(
                    "无法确认智能体是否已接收，已停止重发，"
                    "请等待系统核对。"
                ),
            )

    def _finish_acceptance_unknown(
        self,
        dispatch: dict,
        account: dict,
    ) -> None:
        text = "无法确认智能体是否已接收，已停止重发，请等待系统核对。"
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text=text,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="acceptance_unknown",
            error_code="HOST_ACCEPTANCE_UNKNOWN",
            event={
                "type": "chat",
                "runId": dispatch["idempotency_key"],
                "state": "error",
                "text": text,
                "safeToRetry": False,
            },
        )

    def _observe_accepted_dispatch(self, dispatch: dict) -> None:
        account = self.store.get_account(dispatch["account_id"])
        run_id = dispatch["accepted_run_id"] or dispatch["idempotency_key"]
        evidence = None
        if self._dispatch_stop.is_set():
            return
        try:
            evidence = self._gateway().run_history_evidence(
                session_key=dispatch["conversation_ref"], run_ref=run_id, timeout_seconds=8,
            )
        except (AttributeError, GatewayRequestError):
            pass
        if evidence and evidence.get("final_text"):
            self._finish_accepted_dispatch(
                dispatch, account,
                {"type": "chat", "runId": run_id, "state": "final",
                 "text": evidence["final_text"], "recovered": True},
            )
            return
        if not self.store.host_dispatch_result_deadline_passed(dispatch):
            self.store.defer_host_dispatch_result(
                dispatch["dispatch_id"], claim_token=dispatch["claim_token"],
                had_tool_activity=bool(evidence and evidence.get("had_tool_activity")),
            )
            return
        text = (
            "请求已接收，但在结果恢复时限内未能确认最终结果。"
            "这不代表后台任务失败；请核对任务状态，系统不会自动重发。"
        )
        self._append_workspace_message(
            account,
            role="assistant",
            text=text,
            message_key=f"workspace:assistant:error:{run_id}",
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="result_unknown",
            error_code="HOST_RUN_RESULT_UNAVAILABLE",
            event={
                "type": "chat",
                "runId": run_id,
                "state": "error",
                "text": text,
                "safeToRetry": False,
                "hadToolActivity": bool(
                    dispatch.get("had_tool_activity") or (evidence and evidence.get("had_tool_activity"))
                ),
            },
        )

    def _finish_accepted_dispatch(
        self,
        dispatch: dict,
        account: dict,
        item: dict,
    ) -> None:
        state = item.get("state")
        run_id = (
            _safe_text(item.get("runId"), 256)
            or dispatch["accepted_run_id"]
            or dispatch["idempotency_key"]
        )
        if state == "final":
            text = _safe_text(item.get("text"), 200_000)
            if text:
                self._append_workspace_message(
                    account,
                    role="assistant",
                    text=text,
                    message_key=f"workspace:assistant:{run_id}",
                )
            terminal_state = "completed"
        else:
            text = _terminal_chat_failure_text(item)
            self._append_workspace_message(
                account,
                role="assistant",
                text=text,
                message_key=f"workspace:assistant:error:{run_id}",
            )
            terminal_state = "failed"
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state=terminal_state,
            error_code=(
                None if terminal_state == "completed" else "HOST_RUN_FAILED"
            ),
            event={**item, "runId": run_id, "text": text},
        )

    def _fail_dispatch_before_accept(
        self,
        dispatch: dict,
        account: dict,
        *,
        code: str,
        text: str,
    ) -> None:
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text=text,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="failed_before_accept",
            error_code=code,
            event={
                "type": "chat",
                "runId": dispatch["idempotency_key"],
                "state": "error",
                "text": text,
                "safeToRetry": True,
            },
        )

    def _finish_expired_dispatch(
        self,
        dispatch: dict,
        account: dict,
    ) -> None:
        text = "连接暂未恢复，本次请求未进入业务系统，请稍后重试。"
        self._append_workspace_failure(
            account,
            effective_key=dispatch["idempotency_key"],
            text=text,
        )
        self.store.finish_host_dispatch(
            dispatch["dispatch_id"],
            claim_token=dispatch["claim_token"],
            state="expired",
            error_code="HOST_ACCEPT_TIMEOUT",
            event={
                "type": "chat",
                "runId": dispatch["idempotency_key"],
                "state": "error",
                "text": text,
                "safeToRetry": True,
            },
        )

    def _finish_dispatch_internal_error(
        self,
        dispatch: dict,
        exc: Exception,
    ) -> None:
        try:
            current = self.store.get_host_dispatch(dispatch["dispatch_id"])
            if current["state"] in _HOST_DISPATCH_TERMINAL_STATES:
                return
            if not current.get("claim_token") or current["claim_token"] != dispatch.get("claim_token"):
                return
            if current["state"] == "accepted":
                self.store.defer_host_dispatch_result(
                    current["dispatch_id"], claim_token=current["claim_token"],
                )
                return
            account = self.store.get_account(current["account_id"])
            if current.get("request_sent_at"):
                self._finish_acceptance_unknown(current, account)
                return
            self._fail_dispatch_before_accept(
                current,
                account,
                code="HOST_DISPATCH_INTERNAL_ERROR",
                text="智能体请求协调失败，本次请求已安全停止。",
            )
        except Exception:
            _LOG.error(
                "Workspace durable dispatch could not record internal failure "
                "dispatch_id=%s original_error=%s",
                dispatch.get("dispatch_id"),
                exc.__class__.__name__,
            )

    def _stream_host_dispatch(
        self,
        *,
        user_subject: str,
        dispatch_id: str,
    ) -> Iterator[dict[str, Any]]:
        sequence = 0
        while True:
            events = self.store.list_host_dispatch_events(
                dispatch_id,
                after_sequence=sequence,
                limit=200,
            )
            for event in events:
                sequence = event["sequence"]
                yield {**event["payload"], "dispatchId": dispatch_id}
            dispatch = self.store.get_host_dispatch(
                dispatch_id,
                user_subject=user_subject,
            )
            if dispatch["state"] in _HOST_DISPATCH_TERMINAL_STATES:
                while True:
                    terminal_events = self.store.list_host_dispatch_events(
                        dispatch_id,
                        after_sequence=sequence,
                        limit=200,
                    )
                    if not terminal_events:
                        break
                    for event in terminal_events:
                        sequence = event["sequence"]
                        yield {**event["payload"], "dispatchId": dispatch_id}
                return
            time.sleep(0.1)

    def _record_gateway_recovery(
        self,
        *,
        account: dict,
        effective_key: str,
        attempt: int,
        status: str,
        error_code: str | None = None,
    ) -> None:
        try:
            action, reused = self.service.runtime_governance.start_recovery_action(
                action_type="workspace_host_pre_accept_recovery",
                target_type="workspace_account",
                target_id=str(account["account_id"]),
                actor="agentbridge",
                reason="Agent Host recovered before accepting the Workspace request.",
                idempotency_key=(
                    f"workspace:{account['account_id']}:{effective_key}:"
                    f"gateway-recovery:{max(attempt, 1)}"
                ),
                side_effect_boundary="B0_NO_EFFECT",
                before={"attempt": max(attempt - 1, 0)},
            )
            if reused and action["status"] != "running":
                return
            self.service.runtime_governance.finish_recovery_action(
                action["action_id"],
                status=status,
                after={"attempt": max(attempt, 1)},
                error_code=error_code,
            )
        except Exception as exc:
            _LOG.warning(
                "Workspace Gateway recovery ledger failed: error=%s",
                exc.__class__.__name__,
            )

    def send_chat(
        self,
        account: dict,
        *,
        message: str,
        idempotency_key: str | None = None,
        attachments: list[dict] | None = None,
    ) -> WorkspaceChatResult:
        run_id = ""
        status = "completed"
        for item in self.send_chat_stream(
            account,
            message=message,
            idempotency_key=idempotency_key,
            attachments=attachments,
        ):
            if item.get("type") != "accepted":
                continue
            run_id = _safe_text(item.get("runId"), 256)
            status = _safe_text(item.get("status"), 80) or "accepted"
        if not run_id:
            raise GatewayRequestError(
                "GATEWAY_RESPONSE_INVALID",
                "OpenClaw Gateway did not accept the Workspace run.",
            )
        return WorkspaceChatResult(run_id=run_id, status=status)

    def _append_workspace_message(
        self,
        account: dict,
        *,
        role: str,
        text: str,
        message_key: str,
        payload: dict[str, Any] | None = None,
        required: bool = False,
    ) -> None:
        endpoint_id = _safe_text(account.get("endpoint_id"), 128)
        if not endpoint_id:
            if required:
                raise RuntimeError("workspace endpoint is unavailable")
            return
        try:
            self.service.tasks.append_timeline_message(
                user_subject=account["user_subject"],
                source_endpoint_id=endpoint_id,
                message_key=message_key,
                role=role,
                text=text,
                payload=payload,
            )
        except (KeyError, RuntimeError, ValueError, sqlite3.Error):
            if required:
                raise
            # Chat remains available if the optional cross-end projection fails.
            return

    def _append_workspace_failure(
        self,
        account: dict,
        *,
        effective_key: str,
        text: str,
    ) -> None:
        self._append_workspace_message(
            account,
            role="assistant",
            text=text,
            message_key=f"workspace:assistant:error:{effective_key}",
        )

    def _gateway(self) -> OpenClawGatewayClient:
        if self.gateway is None:
            raise GatewayRequestError(
                "GATEWAY_NOT_CONFIGURED",
                "OpenClaw Gateway is not configured.",
            )
        return self.gateway


def _is_safe_recoverable_pre_accept_error(
    error: GatewayRequestError,
) -> bool:
    if error.code not in _HOST_TRANSPORT_ERROR_CODES:
        return False
    stage = _safe_text(error.details.get("stage"), 80)
    if stage:
        return stage in _HOST_SAFE_PRE_ACCEPT_STAGES
    return error.code in {"GATEWAY_CONNECTION_FAILED", "GATEWAY_PROCESS_FAILED"}


def _may_have_reached_host(error: GatewayRequestError) -> bool:
    stage = _safe_text(error.details.get("stage"), 80)
    if stage in _HOST_SAFE_PRE_ACCEPT_STAGES:
        return False
    return error.code in {
        "GATEWAY_CONNECTION_CLOSED",
        "GATEWAY_RESPONSE_INVALID",
        "GATEWAY_TIMEOUT",
    }


def _validated_chat_attachments(values: list[dict] | None) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_CHAT_ATTACHMENTS:
        raise ValueError("chat attachments must contain at most 4 images")
    normalized = []
    total_bytes = 0
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError("chat attachment is invalid")
        mime_type = str(value.get("mimeType") or "").strip().lower()
        signature = _CHAT_IMAGE_TYPES.get(mime_type)
        if signature is None:
            raise ValueError("chat attachment type is unsupported")
        content = str(value.get("content") or "").strip()
        if not content or content.startswith("data:"):
            raise ValueError("chat attachment content is invalid")
        try:
            body = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("chat attachment content is invalid") from exc
        if not body or len(body) > MAX_CHAT_ATTACHMENT_BYTES:
            raise ValueError("chat attachment exceeds the per-image size limit")
        if mime_type == "image/webp":
            magic_matches = (
                body.startswith(signature[0])
                and len(body) >= 12
                and body[8:12] == b"WEBP"
            )
        else:
            magic_matches = body.startswith(signature[0])
        if not magic_matches:
            raise ValueError("chat attachment content does not match its image type")
        total_bytes += len(body)
        if total_bytes > MAX_CHAT_ATTACHMENTS_TOTAL_BYTES:
            raise ValueError("chat attachments exceed the total size limit")
        supplied_name = str(value.get("fileName") or "").replace("\\", "/")
        supplied_name = supplied_name.rsplit("/", 1)[-1].strip()
        if not supplied_name or any(ord(character) < 32 for character in supplied_name):
            supplied_name = f"image-{index + 1}{signature[1]}"
        normalized.append(
            {
                "type": "image",
                "mimeType": mime_type,
                "fileName": supplied_name[:120],
                "content": content,
            }
        )
    return normalized


def _workspace_gateway_failure_text(error: GatewayRequestError) -> str:
    code = str(error.code or "GATEWAY_REQUEST_FAILED")
    details = error.details if isinstance(error.details, dict) else {}
    if code == "WORKSPACE_RUN_IN_PROGRESS":
        return "\u4e0a\u4e00\u6761\u7f51\u9875\u4efb\u52a1\u4ecd\u5728\u5904\u7406\uff0c\u672c\u6b21\u8bf7\u6c42\u6ca1\u6709\u6392\u961f\u3002"
    if code == "GATEWAY_RUN_TIMEOUT_ABORTED":
        if details.get("hadToolActivity") is True:
            return "\u667a\u80fd\u4f53\u8fd0\u884c\u8d85\u65f6\uff0c\u5df2\u505c\u6b62\u540e\u7eed\u5904\u7406\uff1b\u5982\u6d89\u53ca\u5199\u64cd\u4f5c\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u7ed3\u679c\u3002"
        return "\u667a\u80fd\u4f53\u8fd0\u884c\u8d85\u65f6\uff0c\u5df2\u5b89\u5168\u4e2d\u6b62\uff1b\u672c\u6b21\u5c1a\u672a\u8c03\u7528\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_RUN_TIMEOUT_ABORT_UNCONFIRMED":
        return "OpenClaw \u6682\u65f6\u65e0\u54cd\u5e94\uff0c\u672c\u6b21\u8bf7\u6c42\u5df2\u505c\u6b62\u7ee7\u7eed\u6392\u961f\uff1b\u5982\u6d89\u53ca\u5199\u64cd\u4f5c\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u7ed3\u679c\u3002"
    if code == "GATEWAY_SESSION_NOT_IDLE":
        return "\u4e0a\u4e00\u6761\u667a\u80fd\u4f53\u4efb\u52a1\u672a\u80fd\u53ca\u65f6\u7ed3\u675f\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_SESSION_STATE_UNAVAILABLE":
        return "\u6682\u65f6\u65e0\u6cd5\u786e\u8ba4\u667a\u80fd\u4f53\u4f1a\u8bdd\u662f\u5426\u7a7a\u95f2\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_START_STALLED_ABORTED":
        return "\u667a\u80fd\u4f53\u81ea\u52a8\u6062\u590d\u540e\u4ecd\u672a\u80fd\u542f\u52a8\uff0c\u5df2\u5b89\u5168\u4e2d\u6b62\uff1b\u672c\u6b21\u5c1a\u672a\u8c03\u7528\u4e1a\u52a1\u7cfb\u7edf\u3002"
    if code == "GATEWAY_START_STALLED_ABORT_UNCONFIRMED":
        return "\u667a\u80fd\u4f53\u542f\u52a8\u72b6\u6001\u65e0\u6cd5\u786e\u8ba4\uff0c\u5df2\u505c\u6b62\u81ea\u52a8\u6062\u590d\uff1b\u8bf7\u7a0d\u540e\u67e5\u8be2\u4efb\u52a1\u72b6\u6001\u3002"
    if code == "GATEWAY_START_RECOVERY_BLOCKED_TOOL_ACTIVITY":
        return "\u667a\u80fd\u4f53\u542f\u52a8\u5f02\u5e38\uff0c\u4f46\u68c0\u6d4b\u5230\u672c\u8f6e\u5df2\u5c1d\u8bd5\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff1b\u5df2\u505c\u6b62\u81ea\u52a8\u91cd\u653e\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u72b6\u6001\u3002"
    if code == "GATEWAY_START_RECOVERY_EVIDENCE_UNAVAILABLE":
        return "\u667a\u80fd\u4f53\u542f\u52a8\u5f02\u5e38\uff0c\u4e14\u65e0\u6cd5\u786e\u8ba4\u672c\u8f6e\u662f\u5426\u5df2\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff1b\u5df2\u505c\u6b62\u81ea\u52a8\u91cd\u653e\u3002"
    if code.startswith("GATEWAY_"):
        if details.get("hadToolActivity") is True:
            return (
                "OpenClaw \u5b9e\u65f6\u8fde\u63a5\u5df2\u4e2d\u65ad\uff1b\u672c\u6b21\u5df2\u8c03\u7528\u4e1a\u52a1\u80fd\u529b\uff0c"
                "\u4f46\u672a\u80fd\u53d6\u5f97\u6700\u7ec8\u7b54\u590d\u3002AgentBridge \u4e0d\u4f1a\u81ea\u52a8\u91cd\u653e\uff1b"
                "\u8bf7\u5237\u65b0\u67e5\u770b\u5df2\u843d\u76d8\u7ed3\u679c\uff0c\u6d89\u53ca\u5199\u64cd\u4f5c\u65f6\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u3002"
            )
        return "OpenClaw \u6682\u65f6\u65e0\u54cd\u5e94\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u7ee7\u7eed\u8fdb\u5165\u4e1a\u52a1\u7cfb\u7edf\u3002"
    return f"\u667a\u80fd\u4f53\u672a\u80fd\u5b8c\u6210\u672c\u6b21\u8bf7\u6c42\uff08\u9519\u8bef\u7801\uff1a{code}\uff09\u3002"


def _terminal_chat_failure_text(item: dict[str, Any]) -> str:
    supplied = _safe_text(item.get("text"), 2_000)
    if supplied:
        return supplied
    if item.get("state") == "aborted":
        if item.get("hadToolActivity") is True:
            return "\u667a\u80fd\u4f53\u8fd0\u884c\u5df2\u505c\u6b62\uff1b\u4efb\u52a1\u5df2\u7ecf\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff0c\u8bf7\u5148\u6838\u5bf9\u4e1a\u52a1\u7cfb\u7edf\u72b6\u6001\u3002"
        return "\u667a\u80fd\u4f53\u8fd0\u884c\u5df2\u505c\u6b62\uff0c\u5c1a\u672a\u8c03\u7528\u4e1a\u52a1\u5de5\u5177\uff0c\u53ef\u4ee5\u5b89\u5168\u5730\u91cd\u65b0\u53d1\u9001\u3002"
    return "\u667a\u80fd\u4f53\u672a\u80fd\u5b8c\u6210\u672c\u6b21\u8bf7\u6c42\u3002"


def _public_account(account: dict) -> dict:
    return {
        "accountId": account["account_id"],
        "username": account["username"],
        "state": account["state"],
        "lastLoginAt": account["last_login_at"],
    }


def _public_task(task: dict) -> dict:
    names = (
        "task_id",
        "agent_host",
        "title",
        "status",
        "summary",
        "current_operation_id",
        "current_interaction_id",
        "version",
        "created_at",
        "updated_at",
        "finished_at",
    )
    return {name: task.get(name) for name in names}


def _public_event(event: dict) -> dict:
    names = (
        "event_id",
        "task_id",
        "event_type",
        "created_at",
    )
    return {name: event.get(name) for name in names}


def _public_artifact(artifact: dict) -> dict:
    state = artifact.get("state")
    return {
        "artifact_id": artifact.get("artifact_id"),
        "task_id": artifact.get("task_id"),
        "artifact_type": artifact.get("artifact_type"),
        "filename": artifact.get("filename"),
        "content_type": artifact.get("content_type"),
        "byte_size": artifact.get("byte_size"),
        "download_url": (
            artifact.get("download_url") if state == "ready" else None
        ),
        "state": state,
        "created_at": artifact.get("created_at"),
        "expires_at": artifact.get("expires_at"),
    }


def _public_timeline_entry(
    entry: dict,
    *,
    source: dict | None,
    is_origin: bool,
) -> dict:
    client_type = str((source or {}).get("client_type") or "system")
    display_label = (source or {}).get("label") or {
        "web": "Agent Workspace",
        "webchat": "Agent Workspace",
        "telegram": "Telegram",
        "openclaw-weixin": "微信",
        "system": "AgentBridge",
    }.get(client_type, "已关联客户端")
    dedupe_key = str(entry.get("dedupe_key") or "")
    message_key = (
        dedupe_key.removeprefix("message:")
        if entry.get("entry_type") == "chat_message"
        and dedupe_key.startswith("message:")
        else None
    )
    return {
        "entry_id": entry["entry_id"],
        "sequence": entry["sequence"],
        "entry_type": entry["entry_type"],
        "message_key": message_key,
        "task_id": entry.get("task_id"),
        "role": entry.get("role"),
        "text": entry.get("text"),
        "payload": entry.get("payload") or {},
        "created_at": entry["created_at"],
        "source": {
            "client_type": client_type,
            "display_label": display_label,
            "is_origin": bool(is_origin),
        },
    }


def _public_endpoint(endpoint: dict) -> dict:
    client_type = str(endpoint.get("client_type") or "unknown")
    display_label = endpoint.get("label") or {
        "web": "Agent Workspace",
        "telegram": "Telegram",
        "openclaw-weixin": "微信",
    }.get(client_type, "已关联客户端")
    return {
        "endpoint_id": endpoint.get("endpoint_id"),
        "client_type": client_type,
        "display_label": display_label,
        "capabilities": list(endpoint.get("capabilities") or []),
        "state": endpoint.get("state"),
        "created_at": endpoint.get("created_at"),
        "updated_at": endpoint.get("updated_at"),
        "last_seen_at": endpoint.get("last_seen_at"),
    }


def _visible_messages(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    values = result.get("messages")
    if not isinstance(values, list):
        return []
    messages: list[dict] = []
    for value in values[-200:]:
        if not isinstance(value, dict):
            continue
        role = str(value.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(value.get("content"))
        if not text:
            continue
        messages.append(
            {
                "id": _safe_text(
                    value.get("id") or value.get("messageId"),
                    256,
                ),
                "role": role,
                "text": text,
                "timestamp": _safe_text(
                    _visible_timestamp(
                        value.get("timestamp")
                        or value.get("createdAt")
                        or value.get("created_at")
                    ),
                    80,
                ),
            }
        )
    return messages


def _visible_timestamp(value: Any) -> str | None:
    text = _safe_text(value, 80)
    if not text:
        return None
    if text.isdigit():
        numeric = int(text)
        if 1_000_000_000 <= numeric < 10_000_000_000:
            seconds = float(numeric)
        elif 1_000_000_000_000 <= numeric < 10_000_000_000_000:
            seconds = numeric / 1_000
        else:
            return text
        try:
            return datetime.fromtimestamp(
                seconds,
                tz=timezone.utc,
            ).isoformat(timespec="milliseconds")
        except (OverflowError, OSError, ValueError):
            return text
    return text


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content[:50_000]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"text", "input_text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        if sum(len(part) for part in parts) >= 50_000:
            break
    return "\n".join(parts)[:50_000]


def _safe_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    normalized = str(value).strip()
    return normalized[:maximum] if normalized else None
