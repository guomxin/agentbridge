#!/usr/bin/env python3
"""Serve a read-only Workspace with isolated, synthetic task-search history.

Run with the project Python and a new home. ``public.json`` describes the seed;
``login.json`` (0600) contains only the synthetic fixture accounts. Touch
``fail-next-task-search`` to fail one authenticated GET /api/tasks, or write a
delay in seconds (0..10, empty means 3) to ``delay-next-task-search``. These
controls never affect another route, source code, or a shared service.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def private_json(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def seed_task(fixture, account, *, key, title, state, system, updated):
    service = fixture.service
    subject = account["user_subject"]
    summary = {"syntheticTaskSearch": True}
    if system == "database":
        # Database query groups use this existing Workspace source marker;
        # no fake business grant or nonexistent database operation is created.
        summary["workspaceQueryGroup"] = {"toolName": "database_execute"}
    task, _ = service.tasks.ensure_task(user_subject=subject, agent_host="openclaw",
        host_task_key="task-search-acceptance:" + key, origin_endpoint_id=account["endpoint_id"],
        active_conversation_ref=account["openclaw_session_key"], title=title, summary=summary)
    task_id, operation_id = task["task_id"], None
    if system in {"oa", "taihua"}:
        capability = "oa.workflow.detail.get" if system == "oa" else "taihua.work_log.my.list"
        if state == "waiting_user":
            capability = "oa.leave.approval.prepare" if system == "oa" else "taihua.work_log.create.prepare"
        operation, _ = service.operations.create(user_subject=subject, capability_name=capability,
            capability_version="1", input_summary={"synthetic": True},
            idempotency_key="task-search-acceptance:" + key)
        operation_id = operation["operation_id"]
        if state == "waiting_user":
            session = service.sessions.get_or_create(user_subject=subject, system_id=system,
                expected_principal_ref=subject)
            submission = service.field_submissions.create(user_subject=subject, system_id=system,
                session_id=session["session_id"], capability_name=capability, capability_version="1",
                create_operation_id=operation_id, supersession_key="task-search:" + key,
                form_schema={"title": "合成待填写事项", "system": "合成验收系统",
                    "notice": "隔离只读验收，网页不允许提交。", "effect": "仅显示等待状态",
                    "fields": [{"name": "reason", "label": "说明", "control": "textarea",
                                "required": True, "max_length": 200}]},
                card_base_url=fixture.card_url, ttl_seconds=1800)
            interaction = service._business_input_interaction(submission)
            operation = service.operations.mark_requires_user_action(operation_id,
                code="FIELD_INPUT_REQUIRED", message="等待本人处理合成事项", next_action={"interaction": interaction})
            service.tasks.link_operation(task_id=task_id, user_subject=subject, operation=operation)
            service.tasks.link_interaction(task_id=task_id, user_subject=subject,
                interaction_record=service.interactions.get(interaction["interactionId"], user_subject=subject),
                interaction=interaction)
        else:
            if state == "failed":
                operation = service.operations.mark_failed(operation_id, code="SYNTHETIC_READ_FAILED",
                    message="合成历史失败，用于检索验收")
            elif state == "outcome_unknown":
                operation = service.operations.mark_unknown(operation_id, code="RESULT_UNKNOWN",
                    message="合成待核对状态，未执行业务或消费授权")
            elif state == "running":
                operation = service.operations.mark_running(operation_id)
            else:
                operation = service.operations.mark_succeeded(operation_id, {"synthetic": True, "items": []})
            service.tasks.link_operation(task_id=task_id, user_subject=subject, operation=operation)
    elif state == "failed":
        service.tasks.fail_task(task_id=task_id, user_subject=subject,
            error_code="SYNTHETIC_TASK_FAILED", message="合成失败任务")
    elif state == "outcome_unknown":
        service.tasks.mark_task_outcome_unknown(task_id=task_id, user_subject=subject,
            error_code="SYNTHETIC_RESULT_UNKNOWN")
    elif state == "succeeded":
        service.tasks.complete_task(task_id=task_id, user_subject=subject, reason="synthetic_read_only_acceptance")
    else:
        raise ValueError("Waiting/running seeds require a consistent operation")
    # Only historical timestamps are synthetic. State, operation ownership and
    # current-operation links above come from the real stores, without invoke.
    created = (datetime.fromisoformat(updated) - timedelta(hours=1)).isoformat()
    with service.tasks._connect() as db:
        db.execute("UPDATE agent_tasks SET created_at=?,updated_at=?,finished_at="
                   "CASE WHEN finished_at IS NULL THEN NULL ELSE ? END WHERE task_id=?",
                   (created, updated, updated, task_id))
        db.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (updated, task_id))
        if operation_id:
            db.execute("UPDATE operations SET created_at=?,updated_at=?,finished_at="
                       "CASE WHEN finished_at IS NULL THEN NULL ELSE ? END WHERE operation_id=?",
                       (created, updated, updated, operation_id))
            db.execute("UPDATE task_operations SET linked_at=? WHERE task_id=?", (updated, task_id))
    actual = service.tasks.get_task(task_id, user_subject=subject)
    if actual["status"] != state:
        raise AssertionError("Seed state does not match its real operation observation")
    return {"taskId": task_id, "title": title, "status": state, "system": system, "updatedAt": updated}


def seed_history(fixture, count):
    from tests.authorization_fixtures import grant_permissions
    from agentbridge.core.user_grants import PERMISSIONS
    from tests.test_workspace import PASSWORD

    primary = fixture.account
    secondary = fixture.service.workspace.authenticate(username="cardother", password=PASSWORD)
    grant_permissions(fixture.service.user_grants, secondary["user_subject"], PERMISSIONS)
    now = datetime.now(timezone.utc)
    same_time = (now - timedelta(days=1)).replace(hour=4, minute=0, second=0, microsecond=123456).isoformat()
    old_time = (now - timedelta(days=240)).replace(hour=4, minute=0, second=0, microsecond=0).isoformat()
    rows = []
    for index in range(count):
        system = ("oa", "taihua", "database", "other")[index % 4]
        state = "succeeded"
        if system in {"oa", "taihua"}:
            state = ("waiting_user", "failed", "outcome_unknown", "running", "succeeded")[index // 4 % 5]
        elif index % 12 == 2:
            state = "failed"
        title = f"任务检索合成历史 {index + 1:03d} · {system}"
        updated = same_time if index < 96 else (now - timedelta(days=2 + index // 16)).isoformat()
        rows.append(seed_task(fixture, primary, key=f"history-{index:04d}", title=title,
            state=state, system=system, updated=updated))
    special = {}
    for key, title, state, system, updated in (
        ("old-unknown", "很旧的待核对事项·检索锚点", "outcome_unknown", "oa", old_time),
        ("old-waiting", "很旧的等待本人处理事项", "waiting_user", "taihua", old_time),
        ("old-failed", "旧事项失败·100%_AbC", "failed", "oa", old_time),
        ("long-title", ("超长标题验收·" + "需要完整阅读并核对原始任务标题，" * 20)[:240],
            "succeeded", "oa", same_time),
        ("database", "数据库来源标记·只读历史分析", "succeeded", "database", same_time),
        ("other", "无系统来源的普通任务·其他", "succeeded", "other", same_time),
    ):
        row = seed_task(fixture, primary, key=key, title=title, state=state, system=system, updated=updated)
        special[key] = row
        rows.append(row)
    private_rows = [seed_task(fixture, secondary, key=f"private-{index}",
        title=f"第二账号专属标记 TASK_SEARCH_OTHER_PRIVATE {index + 1}",
        state="succeeded", system="oa", updated=same_time) for index in range(5)]
    for index in range(160):
        fixture.service.tasks.append_timeline_message(user_subject=primary["user_subject"],
            source_endpoint_id=primary["endpoint_id"], message_key=f"task-search-long-history:{index}",
            role="user" if index % 2 == 0 else "assistant",
            text=f"长历史合成文字 {index + 1:03d}。" + "用于检查内部滚动、输入框、历史保留及任务检索切换。" * 25)
    fixture.service.tasks.append_timeline_message(user_subject=secondary["user_subject"],
        source_endpoint_id=secondary["endpoint_id"], message_key="task-search-other-private-message",
        role="assistant", text="第二账号私有历史 TASK_SEARCH_OTHER_PRIVATE")
    return {"primaryTaskCount": len(rows), "secondaryTaskCount": len(private_rows),
        "longHistoryMessages": 160, "sameTimestampTaskCount": 99, "sameTimestamp": same_time,
        "oldShanghaiDate": (datetime.fromisoformat(old_time) + timedelta(hours=8)).date().isoformat(),
        "primaryStatusCounts": dict(Counter(row["status"] for row in rows)),
        "primarySystemCounts": dict(Counter(row["system"] for row in rows)), "specialCases": special,
        "secondaryPrivateMarker": "TASK_SEARCH_OTHER_PRIVATE"}


def wrap_read_only(server, home, stop):
    original = server.RequestHandlerClass
    lock = threading.Lock()
    events = []

    def consume_controls():
        with lock:
            fail = home / "fail-next-task-search"
            delayed = home / "delay-next-task-search"
            failure, seconds = fail.exists(), 0.0
            if failure:
                fail.unlink()
            if delayed.exists():
                if delayed.is_symlink():
                    delayed.unlink()
                    raise ValueError("Delay control must be a regular local marker")
                raw = delayed.read_text()[:32].strip()
                delayed.unlink()
                seconds = float(raw) if raw else 3.0
                if not math.isfinite(seconds) or not 0 <= seconds <= 10:
                    raise ValueError("Delay control must be 0..10 seconds")
            return failure, seconds

    class ReadOnlyHandler(original):
        def log_message(self, *_args):
            pass

        def send_response(self, code, message=None):
            self.acceptance_status = code
            return super().send_response(code, message)

        def do_GET(self):
            if urlsplit(self.path).path != "/api/tasks":
                return super().do_GET()
            started = time.monotonic()
            failure, seconds = False, 0.0
            try:
                # Preserve authentication and Host checks before consuming a
                # local fault marker. Base handling still checks them again.
                if not self._host_allowed():
                    return self._json(400, {"error": {"code": "INVALID_HOST"}})
                if self._authenticate() is None:
                    return
                try:
                    failure, seconds = consume_controls()
                except (ValueError, OSError):
                    return self._json(500, {"error": {"code": "SYNTHETIC_CONTROL_INVALID"}})
                if seconds:
                    stop.wait(seconds)
                if failure:
                    return self._json(503, {"error": {"code": "SYNTHETIC_TASK_SEARCH_UNAVAILABLE",
                        "message": "合成临时失败，请重新读取。"}})
                return super().do_GET()
            finally:
                with lock:
                    events.append({"request": len(events) + 1, "status": getattr(self, "acceptance_status", 0),
                        "durationMs": round((time.monotonic() - started) * 1000, 3),
                        "injectedFailure": failure, "injectedDelaySeconds": seconds})
                    private_json(home / "request-metrics.json", {"taskSearchRequests": len(events), "requests": events})

        def do_POST(self):
            if urlsplit(self.path).path in {"/api/login", "/api/logout"}:
                return super().do_POST()
            self.close_connection = True
            return self._json(403, {"error": {"code": "FIXTURE_READ_ONLY",
                "message": "隔离验收只允许读取。"}})

        def do_PUT(self):
            self.close_connection = True
            return self._json(403, {"error": {"code": "FIXTURE_READ_ONLY"}})

        do_PATCH = do_PUT
        do_DELETE = do_PUT

    server.RequestHandlerClass = ReadOnlyHandler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, help="New empty isolated directory; defaults to a private temporary home")
    parser.add_argument("--tasks", type=int, default=224, help="Base primary tasks, 201..1000; six boundary cases are added")
    parser.add_argument("--shutdown-after", type=float, default=0, help="Optional bounded startup smoke in seconds")
    args = parser.parse_args()
    if not 201 <= args.tasks <= 1000 or not math.isfinite(args.shutdown_after) or args.shutdown_after < 0:
        parser.error("Invalid bounded fixture size or shutdown duration")
    os.umask(0o077)
    home = args.home.expanduser().absolute() if args.home else Path(tempfile.mkdtemp(prefix="task-search-acceptance-"))
    if home.is_symlink() or (home.exists() and any(home.iterdir())):
        parser.error("Use a new empty isolated home")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home.chmod(0o700)
    # The fixture creates a new key inside this empty home. Never inherit a
    # production key or initialize against the caller's default runtime.
    os.environ.pop("AGENTBRIDGE_SESSION_KEY_FILE", None)
    from tests.support.latency_acceptance import LatencyAcceptanceFixture
    from tests.test_workspace import PASSWORD
    fixture = LatencyAcceptanceFixture(home, seed=False)
    stop = threading.Event()
    servers, threads = [], []
    try:
        shape = seed_history(fixture, args.tasks)
        # Defense in depth: even an accidental future GET cannot invoke or
        # resume a capability; no model or coordinator is started.
        def denied(*_args, **_kwargs):
            raise RuntimeError("Read-only synthetic task-search fixture")
        fixture.service.invoke = denied
        fixture.service.resume_interaction = denied
        cards, workspace = fixture.servers()
        cards.server_close()
        servers.append(workspace)
        wrap_read_only(workspace, home, stop)
        audit = fixture.audit()
        if audit["decisionCount"] or audit["authorizationStates"] or audit["downstreamWriteCount"]:
            raise AssertionError("Read-only search seed created business authorization or writes")
        with fixture.service.tasks._connect() as db:
            mismatches = db.execute("SELECT COUNT(*) FROM task_operations l JOIN agent_tasks t ON t.task_id=l.task_id "
                "JOIN operations o ON o.operation_id=l.operation_id "
                "WHERE l.user_subject<>t.user_subject OR l.user_subject<>o.user_subject").fetchone()[0]
        if mismatches:
            raise AssertionError("Synthetic operation ownership mismatch")
        private_json(home / "login.json", {"syntheticOnly": True, "accounts": [
            {"username": "cardreview", "password": PASSWORD}, {"username": "cardother", "password": PASSWORD}]})
        private_json(home / "public.json", {"fixture": "task-search-read-only-v1", "home": str(home),
            "workspaceUrl": fixture.workspace_url, "sourceRoot": str(ROOT), **shape,
            "businessAudit": audit, "operationOwnershipMismatches": mismatches,
            "allowedPostRoutes": ["/api/login", "/api/logout"], "modelStarted": False,
            "coordinatorStarted": False, "cardServerStarted": False,
            "faultMarkers": ["fail-next-task-search", "delay-next-task-search"],
            "evidenceBoundary": "Real stores and Workspace HTTP; synthetic history only; no downstream execution"})
        private_json(home / "request-metrics.json", {"taskSearchRequests": 0, "requests": []})
        thread = threading.Thread(target=workspace.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        threads.append(thread)
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_args: stop.set())
        print(json.dumps({"workspaceUrl": fixture.workspace_url, "home": str(home)}, ensure_ascii=False), flush=True)
        stop.wait(args.shutdown_after or None)
    finally:
        stop.set()
        for server in servers:
            if threads:
                server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
        fixture.close()


if __name__ == "__main__":
    main()
