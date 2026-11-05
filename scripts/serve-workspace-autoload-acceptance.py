#!/usr/bin/env python3
"""Serve isolated, interleaved Workspace history for Chrome acceptance.

Only this fresh loopback fixture can inject a one-shot history failure. It never
loads production identities, invokes a model, or runs a business coordinator.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.auth.timeline_attachment import TrustedTimelineAttachmentApplication
from agentbridge.core.timeline_attachments import TimelineAttachmentStore, public_attachment
from tests.support.interaction_acceptance import OWNER, SyntheticBroker, write_metadata
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract
from tests.test_workspace import PASSWORD


def seed_history(fixture):
    """Keep each turn's three creation times adjacent, not grouped by type."""
    store = fixture.service.tasks
    account = fixture.account
    base = datetime.now(timezone.utc) - timedelta(days=3)
    turns = []
    for ordinal in range(1, 151):
        created = base + timedelta(minutes=ordinal * 15)
        title = f"历史场景 {ordinal:03d} · 查询结果"
        user, _ = store.append_timeline_message(
            user_subject=OWNER, source_endpoint_id=account["endpoint_id"],
            message_key=f"autoload:user:{ordinal}", role="user",
            text=f"历史场景 {ordinal:03d}：只读查询这一轮的合成记录。")
        task, _ = store.ensure_task(
            user_subject=OWNER, agent_host="openclaw",
            host_task_key=f"autoload:task:{ordinal}", origin_endpoint_id=account["endpoint_id"],
            active_conversation_ref=account["openclaw_session_key"], title=title)
        store.complete_task(task_id=task["task_id"], user_subject=OWNER, reason="synthetic_history_only")
        paragraph = "本段只用于确认翻阅时文字与结果卡一起出现，未查询真实业务，也没有执行审批或提交。"
        answer, _ = store.append_timeline_message(
            user_subject=OWNER, source_endpoint_id=account["endpoint_id"],
            message_key=f"autoload:assistant:{ordinal}", role="assistant",
            task_id=task["task_id"],
            text=f"历史场景 {ordinal:03d} 的合成查询已结束。\n\n" + paragraph * (24 if ordinal % 12 == 0 else 5))
        task_created = (created + timedelta(seconds=1)).isoformat()
        answer_created = (created + timedelta(seconds=2)).isoformat()
        # These are fresh generic fixture rows, not immutable authorizations or
        # material records. Old tasks recently updated must stay in old history.
        updated = datetime.now(timezone.utc).isoformat() if ordinal <= 8 else answer_created
        with store._connect() as connection:
            connection.execute("UPDATE agent_tasks SET created_at=?, updated_at=? WHERE task_id=?",
                               (task_created, updated, task["task_id"]))
            connection.execute("UPDATE user_timeline SET created_at=? WHERE entry_id=?",
                               (created.isoformat(), user["entry_id"]))
            connection.execute("UPDATE user_timeline SET created_at=? WHERE entry_id=?",
                               (answer_created, answer["entry_id"]))
            connection.execute("UPDATE user_timeline SET created_at=? WHERE task_id=? AND entry_type='task_event'",
                               (task_created, task["task_id"]))
            connection.execute("UPDATE task_events SET created_at=? WHERE task_id=?",
                               (task_created, task["task_id"]))
        turns.append({"ordinal": ordinal, "title": title, "task_id": task["task_id"],
                      "user_entry_id": user["entry_id"], "assistant_entry_id": answer["entry_id"],
                      "user_created_at": created.isoformat(), "task_created_at": task_created,
                      "assistant_created_at": answer_created, "recently_updated": ordinal <= 8})

    # Neither a message nor a task can be assumed to have a counterpart.
    standalone = []
    for ordinal in range(1, 9):
        created = base + timedelta(minutes=ordinal * 15, seconds=30)
        task, _ = store.ensure_task(
            user_subject=OWNER, agent_host="openclaw", host_task_key=f"autoload:task-only:{ordinal}",
            origin_endpoint_id=account["endpoint_id"], active_conversation_ref=account["openclaw_session_key"],
            title=f"独立历史卡 {ordinal:02d}（没有聊天文字）")
        store.complete_task(task_id=task["task_id"], user_subject=OWNER, reason="synthetic_task_only")
        with store._connect() as connection:
            connection.execute("UPDATE agent_tasks SET created_at=?, updated_at=? WHERE task_id=?",
                               (created.isoformat(), created.isoformat(), task["task_id"]))
        standalone.append(task["task_id"])
        entry, _ = store.append_timeline_message(
            user_subject=OWNER, source_endpoint_id=account["endpoint_id"],
            message_key=f"autoload:text-only:{ordinal}", role="assistant",
            text=f"独立历史文字 {ordinal:02d}（没有任务卡）。")
        with store._connect() as connection:
            connection.execute("UPDATE user_timeline SET created_at=? WHERE entry_id=?",
                               ((created + timedelta(seconds=1)).isoformat(), entry["entry_id"]))
    return turns, standalone


def seed_media(fixture):
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    result = []
    for label, remaining in (("可用历史原图", 86400), ("已过期历史原图", -1)):
        key = "autoload-media:" + label
        clock = datetime.now(timezone.utc) - timedelta(days=7) + timedelta(seconds=remaining)
        media = TimelineAttachmentStore(fixture.service.tasks.db_path, clock=lambda clock=clock: clock)
        record = media.create_many(
            user_subject=OWNER, message_key=key, media_base_url=fixture.card_url,
            attachments=[{"fileName": label + ".png", "mimeType": "image/png",
                          "content": base64.b64encode(png).decode()}])[0]
        entry, _ = fixture.service.tasks.append_timeline_message(
            user_subject=OWNER, source_endpoint_id=fixture.account["endpoint_id"],
            message_key=key, role="user", text=label + "（仅合成图片）",
            payload={"attachments": [public_attachment(record)]})
        result.append({"label": label, "entry_id": entry["entry_id"], "expected_expired": remaining < 0})
    return result


def observe_requests(server, home):
    original = server.RequestHandlerClass
    events, lock = [], threading.Lock()
    output = home / "history-http.json"
    marker = home / "fail-next-history"

    def save():
        temporary = home / "history-http.tmp"
        counts = {}
        for event in events:
            key = event["method"] + " " + event["route"]
            counts[key] = counts.get(key, 0) + 1
        write_metadata(temporary, {"requestCount": len(events), "counts": counts, "requests": events})
        temporary.replace(output)

    def route_name(path):
        path = urlsplit(path).path
        if path in {"/api/history", "/api/current", "/api/login", "/api/logout", "/api/session"}:
            return path
        if path == "/api/timeline/stream":
            return "timeline-stream"
        if path.startswith("/static/"):
            return "static"
        return "other-api" if path.startswith("/api/") else "document"

    class ObservedHandler(original):
        def measured(self, method, handler):
            started = time.monotonic()
            event = {"method": method, "route": route_name(self.path), "startedMonotonic": started}
            with lock:
                events.append(event)
                save()
            self._autoload_event = event
            try:
                path = urlsplit(self.path).path
                with lock:
                    fail = method == "GET" and path == "/api/history" and marker.exists()
                    if fail:
                        marker.unlink()
                if fail:
                    event["injectedFailure"] = True
                    return self._json(503, {"error": {"code": "SYNTHETIC_HISTORY_UNAVAILABLE",
                        "message": "合成历史单次故障；再次读取应正常。"}})
                if method == "POST" and path not in {"/api/login", "/api/logout"}:
                    return self._json(403, {"error": {"code": "FIXTURE_READ_ONLY",
                        "message": "此历史验收夹具禁止业务写入。"}})
                return handler()
            finally:
                with lock:
                    event["durationSeconds"] = time.monotonic() - started
                    save()

        def send_response(self, code, message=None):
            if hasattr(self, "_autoload_event"):
                self._autoload_event["status"] = code
            return super().send_response(code, message)

        def do_GET(self):
            return self.measured("GET", super().do_GET)

        def do_POST(self):
            return self.measured("POST", super().do_POST)

    server.RequestHandlerClass = ObservedHandler
    with lock:
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    if args.home.exists():
        raise SystemExit("Use a fresh isolated home")
    fixture = LatencyAcceptanceFixture(args.home, seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    servers = []
    running_servers = []
    try:
        turns, standalone = seed_history(fixture)
        old = fixture.add_query_case(case_key="autoload-expired-query", extra_profiles=("missed_punch", "weekly_report"))
        with fixture.service.tasks._connect() as connection:
            connection.execute("UPDATE operations SET finished_at=? WHERE operation_id=?",
                ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(), old["operation_id"]))
        media = seed_media(fixture)
        original_cards, workspace = fixture.servers()
        original_cards.server_close()
        cards = create_auth_http_server(config=validate_auth_server_config(
            host="127.0.0.1", port=fixture.meta["card_port"], public_base_url=fixture.card_url,
            tls_cert=None, tls_key=None),
            application=TrustedAuthApplication(challenge_store=fixture.service.challenges,
                                             broker=SyntheticBroker(fixture.service.challenges)),
            timeline_attachment_application=TrustedTimelineAttachmentApplication(
                attachment_store=fixture.service.timeline_attachments))
        servers = [cards, workspace]
        observe_requests(workspace, fixture.home)
        with fixture.service.tasks._connect() as connection:
            messages = connection.execute("SELECT COUNT(*) FROM user_timeline WHERE user_subject=? AND entry_type='chat_message'", (OWNER,)).fetchone()[0]
            tasks = connection.execute("SELECT COUNT(*) FROM agent_tasks WHERE user_subject=?", (OWNER,)).fetchone()[0]
        # Never merge fixture.public_metadata(): inherited helpers include the
        # synthetic password. Browser credentials belong only in login.json.
        metadata = {"fixture": "workspace-autoload-interleaved-v1", "workspace_url": fixture.workspace_url,
            "card_origin": fixture.card_url, "username": "cardreview", "other_username": "cardother",
            "historyShape": {"messages": messages, "tasks": tasks, "interleavedTurns": len(turns)},
            "turns": turns, "standalone_task_ids": standalone, "media": media,
            "expired_query_task": old["task_id"], "fail_marker": str(fixture.home / "fail-next-history"),
            "request_log": str(fixture.home / "history-http.json"),
            "evidence_boundary": "Real Central stores, HTTP, authorization and UI; synthetic OA/materials only. No model, coordinator or real business writes."}
        write_metadata(fixture.home / "public.json", metadata)
        write_metadata(fixture.home / "login.json", {"username": "cardreview", "other_username": "cardother", "password": PASSWORD})
        write_metadata(fixture.home / "ledger.json", fixture.audit())
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            running_servers.append(server)
        print(json.dumps({"workspace_url": fixture.workspace_url, "home": str(fixture.home),
                          "messages": messages, "tasks": tasks}), flush=True)
        signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
        while not fixture.stop_event.wait(.5):
            write_metadata(fixture.home / "ledger.json", fixture.audit())
    finally:
        for server in running_servers:
            server.shutdown()
        for server in servers:
            server.server_close()
        fixture.close()


if __name__ == "__main__":
    main()
