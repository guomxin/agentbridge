#!/usr/bin/env python3
"""Isolated Workspace watch QA: real HTTP/stores/grants, synthetic OA reads.

Only login/logout and business-watch platform mutations are permitted. No model,
coordinator, capability invoke, business decision, authorization or commit runs.
Use a new empty --home; synthetic credentials are written only to login.json.
"""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def denied(*_args, **_kwargs):
    raise RuntimeError("Synthetic watch fixture forbids business execution and real downstream IO")


class SyntheticWorker:
    def __init__(self, session):
        self.principal = session["expected_principal_ref"]
        self.state = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def restore_session_state(self, state):
        self.state = deepcopy(state)

    def capture_session_state(self):
        return deepcopy(self.state)


class SyntheticObservations:
    def __init__(self, home, stop):
        self.home, self.stop = home, stop
        self.lock = threading.Lock()
        self.counts = {}

    def __call__(self, _adapter, _worker, *, source, collection):
        from agentbridge.adapters.seeyon_central import SeeyonReadContractMismatch
        with self.lock:
            key = (collection, source["summary_id"], source["process_id"], source["case_id"])
            count = self.counts.get(key, 0)
            self.counts[key] = count + 1
            controls = {}
            for name in ("fail", "delay", "partial"):
                path = self.home / f"{name}-next-watch-check"
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("Fixture controls must be regular local files")
                    controls[name] = path.read_text()[:32].strip()
                    path.unlink()
            private_json(self.home / "observation-metrics.json", {
                "syntheticReadCount": sum(self.counts.values()), "syntheticTargets": len(self.counts),
                "realDownstreamCalls": 0, "businessWrites": 0})
        if "delay" in controls:
            seconds = float(controls["delay"] or 3)
            if not math.isfinite(seconds) or not 0 <= seconds <= 10:
                raise ValueError("Fixture delay must be 0..10 seconds")
            self.stop.wait(seconds)
        if "fail" in controls:
            raise SeeyonReadContractMismatch("Synthetic observation unavailable")
        unavailable = ["workflow_state", "current_nodes", "current_handlers"] if "partial" in controls else []
        node = ("财务复核", "项目核对", "财务复核")[count % 3]
        return {"target": {"collection": collection, **source},
            "workflow_state": "unknown" if unavailable else "in_progress",
            "current_nodes": None if unavailable else [{"name": node, "handlers": ["合成经办人甲", "合成协办人乙"]}],
            "current_handlers": None if unavailable else ["合成经办人甲", "合成协办人乙"],
            "observed_at": datetime.now(timezone.utc).isoformat(), "unavailable_fields": unavailable,
            "completeness": "partial" if unavailable else "complete", "read_side_effects": ["may_mark_read"],
            "background_read_safe": False, "safety_level": "foreground_only_may_mark_read"}


def audit(service):
    with sqlite3.connect(service.db_path) as db:
        return {"businessDecisionCount": db.execute("SELECT COUNT(*) FROM business_draft_decisions").fetchone()[0],
            "businessAuthorizationCount": db.execute("SELECT COUNT(*) FROM write_authorizations").fetchone()[0],
            "businessCommitCount": db.execute("SELECT COUNT(*) FROM write_authorizations WHERE commit_operation_id IS NOT NULL").fetchone()[0],
            "realDownstreamCalls": 0, "businessWrites": 0}


def source(number, title):
    return {"affair_id": str(1000 + number), "summary_id": str(2000 + number),
            "process_id": str(3000 + number), "case_id": str(4000 + number), "title": title}


def reference(service, account, value):
    from agentbridge.core.business_content_identity import principal_binding
    session = service.sessions.find(user_subject=account["user_subject"], system_id="oa")
    record = {"user_subject": account["user_subject"], "session_id": session["session_id"],
        "principal_ref": principal_binding(session), "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        "query": {"collection": "done"}}
    return service.business_watch.target_reference(record, {"title": value["title"], "_source": value})


def seed(service, accounts, home):
    watches, special = [], {}
    primary, secondary = accounts
    for index in range(40):
        title = f"业务关注合成事项 {index + 1:02d} · 状态与处理节点"
        if index == 39:
            title = ("长标题关注验收·" + "跨部门项目结算资料与处理节点需要完整阅读，" * 22)[:480]
        item = service.business_watch.create(primary["user_subject"], {
            "target_ref": reference(service, primary, source(index, title)), "allow_mark_read": True})["watch"]
        if index == 39:
            for _ in range(24):
                item = service.business_watch.check(primary["user_subject"], item["watch_id"], expected_version=item["version"])["watch"]
            special["longTitleAndHistory"] = item["watch_id"]
        if 25 <= index < 30:
            item = service.business_watch.update(primary["user_subject"], item["watch_id"], {"action": "pause", "expected_version": item["version"]})["watch"]
            special.setdefault("paused", item["watch_id"])
        elif 30 <= index < 35:
            item = service.business_watch.update(primary["user_subject"], item["watch_id"], {"action": "stop", "expected_version": item["version"]})["watch"]
            special.setdefault("stopped", item["watch_id"])
        elif 35 <= index < 39:
            ended = {"workflow_state": "ended", "current_nodes": [], "current_handlers": [],
                "observed_at": datetime.now(timezone.utc).isoformat(), "unavailable_fields": []}
            item = service.business_watch.store.apply_observation(item["watch_id"], user_subject=primary["user_subject"],
                expected_version=item["version"], snapshot=ended)["watch"]
            special.setdefault("completed", item["watch_id"])
        watches.append(item)
    for index in range(3):
        title = f"第二账号专属关注 BUSINESS_WATCH_OTHER_PRIVATE {index + 1}"
        service.business_watch.create(secondary["user_subject"], {
            "target_ref": reference(service, secondary, source(100 + index, title)), "allow_mark_read": True})
    for index in range(160):
        service.tasks.append_timeline_message(user_subject=primary["user_subject"],
            source_endpoint_id=primary["endpoint_id"], message_key=f"watch-long-history:{index}",
            role="user" if index % 2 == 0 else "assistant",
            text=f"关注功能长聊天合成文字 {index + 1:03d}。" + "用于检查聊天内部滚动、输入框、历史保留与关注页切换。" * 25)
    service.tasks.append_timeline_message(user_subject=secondary["user_subject"], source_endpoint_id=secondary["endpoint_id"],
        message_key="watch-other-private-chat", role="assistant", text="第二账号私有聊天 BUSINESS_WATCH_OTHER_PRIVATE")
    candidate = source(999, "尚未关注·平台创建与默认读取同意验收")
    private_json(home / "create-case.json", {"syntheticOnly": True,
        "body": {"target_ref": reference(service, primary, candidate)},
        "notice": "默认缺少allow_mark_read，必须拒绝且不执行合成读取；显式true仅用于隔离平台设置验收。"})
    return {"primaryWatchCount": len(watches), "secondaryWatchCount": 3, "longHistoryMessages": 160,
        "secondaryPrivateMarker": "BUSINESS_WATCH_OTHER_PRIVATE", "specialCases": special,
        "createCaseFile": str(home / "create-case.json")}


def wrap_server(server, service, home):
    original = server.RequestHandlerClass
    lock, requests = threading.Lock(), []

    class WatchAcceptanceHandler(original):
        def send_response(self, code, message=None):
            self.acceptance_status = code
            return super().send_response(code, message)

        def _record(self, method, started):
            if not urlsplit(self.path).path.startswith("/api/business-watches"):
                return
            with lock:
                requests.append({"method": method, "status": getattr(self, "acceptance_status", 0),
                    "durationMs": round((time.monotonic()-started)*1000, 3)})
                private_json(home / "request-metrics.json", {"watchRequests": len(requests), "requests": requests})
                private_json(home / "business-audit.json", audit(service))

        def do_GET(self):
            started = time.monotonic()
            try:
                return super().do_GET()
            finally:
                self._record("GET", started)

        def do_POST(self):
            path = urlsplit(self.path).path
            allowed = path in {"/api/login", "/api/logout", "/api/business-watches"} or bool(
                re.fullmatch(r"/api/business-watches/[0-9a-f-]{36}/(?:update|check)", path))
            if not allowed:
                self.close_connection = True
                return self._json(403, {"error": {"code": "FIXTURE_WATCH_ONLY",
                    "message": "隔离验收仅允许关注平台设置和合成核验。"}})
            started = time.monotonic()
            try:
                return super().do_POST()
            finally:
                self._record("POST", started)

        def do_PUT(self):
            self.close_connection = True
            return self._json(403, {"error": {"code": "FIXTURE_WATCH_ONLY"}})

        do_PATCH = do_PUT
        do_DELETE = do_PUT

    server.RequestHandlerClass = WatchAcceptanceHandler


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--shutdown-after", type=float, default=0)
    args = parser.parse_args()
    if not math.isfinite(args.shutdown_after) or args.shutdown_after < 0:
        parser.error("Invalid shutdown duration")
    os.umask(0o077)
    home = args.home.expanduser().absolute() if args.home else Path(tempfile.mkdtemp(prefix="business-watch-acceptance-"))
    if home.is_symlink() or (home.exists() and any(home.iterdir())):
        parser.error("Use a new empty isolated home")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    home.chmod(0o700)
    key = home / "session.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    os.environ["AGENTBRIDGE_SESSION_KEY_FILE"] = str(key)
    from tests.authorization_fixtures import authorized_service
    from tests.test_workspace import PASSWORD, _create_account
    from tests.support.interaction_acceptance import ReadOnlyGateway, free_port
    from agentbridge.workspace.application import WorkspaceApplication
    from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
    service = authorized_service(home=home, base_url="http://127.0.0.1:1/seeyon/", worker_factory=denied)
    accounts = [_create_account(service, user_subject=subject, username=username, endpoint_key="telegram:*:"+username)
        for subject, username in (("user-a", "watchreview"), ("user-b", "watchother"))]
    for account in accounts:
        session = service.sessions.get_or_create(user_subject=account["user_subject"], system_id="oa", expected_principal_ref=account["user_subject"])
        service.sessions.activate(session["session_id"], observed_principal_ref=account["user_subject"])
        service.session_states.save(session["session_id"], {"cookies": []})
    service.invoke = service.resume_interaction = denied
    service._worker_factories_by_system = {system: denied for system in service._worker_factories_by_system}
    service.worker_factory = lambda session, _adapter: SyntheticWorker(session)
    stop, server, thread, app = threading.Event(), None, None, None
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(service.adapter, "probe_business_draft_session", side_effect=lambda worker:
                {"authenticated": True, "observed_principal_ref": worker.principal}))
            observations = SyntheticObservations(home, stop)
            stack.enter_context(patch("agentbridge.adapters.seeyon_watch.read_workflow_state", side_effect=observations))
            shape = seed(service, accounts, home)
            result = audit(service)
            if any(result.values()):
                raise AssertionError("Synthetic watch fixture created business effects")
            app = WorkspaceApplication(service=service, gateway=ReadOnlyGateway())
            port = free_port()
            origin = f"http://127.0.0.1:{port}"
            server = create_workspace_http_server(config=validate_workspace_server_config(host="127.0.0.1", port=port,
                public_base_url=origin, tls_cert=None, tls_key=None), application=app)
            wrap_server(server, service, home)
            private_json(home / "login.json", {"syntheticOnly": True, "accounts": [
                {"username": account["username"], "password": PASSWORD} for account in accounts]})
            private_json(home / "public.json", {"fixture": "business-watch-synthetic-v1", "home": str(home),
                "workspaceUrl": origin, "sourceRoot": str(ROOT), **shape, "businessAudit": result,
                "modelStarted": False, "coordinatorStarted": False, "cardServerStarted": False,
                "allowedPostRoutes": ["/api/login", "/api/logout", "/api/business-watches", "/api/business-watches/{id}/update", "/api/business-watches/{id}/check"],
                "faultMarkers": ["fail-next-watch-check", "delay-next-watch-check", "partial-next-watch-check"],
                "evidenceBoundary": "Real Workspace HTTP/authentication/grants/watch service/session lock/stores; synthetic downstream principal probe and workflow snapshots only. No real OA, model or business execution."})
            private_json(home / "request-metrics.json", {"watchRequests": 0, "requests": []})
            private_json(home / "business-audit.json", result)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            thread.start()
            for number in (signal.SIGTERM, signal.SIGINT):
                signal.signal(number, lambda *_args: stop.set())
            print(json.dumps({"workspaceUrl": origin, "home": str(home)}, ensure_ascii=False), flush=True)
            deadline = time.monotonic()+args.shutdown_after if args.shutdown_after else None
            while not stop.wait(.1):
                if deadline is not None and time.monotonic()>=deadline:
                    break
                marker=home/'run-due-watch'
                if not marker.exists(): continue
                try:
                    if marker.is_symlink() or not marker.is_file():
                        raise ValueError('Fixture control must be a regular file')
                    watch_id=marker.read_text().strip(); marker.unlink()
                    if not re.fullmatch(r'[0-9a-f-]{36}',watch_id):
                        raise ValueError('Invalid isolated watch')
                    # Only fixture scheduling is accelerated. Production frequency,
                    # permissions, leases, consent and observation code are unchanged.
                    with service.business_watch.store._connect() as db:
                        changed=db.execute('UPDATE business_watches SET next_check_at=? WHERE watch_id=?',
                            (datetime.now(timezone.utc).isoformat(),watch_id)).rowcount
                    if changed!=1: raise ValueError('Unknown isolated watch')
                    result=service.business_watch.run_due_checks(limit=4)
                    private_json(home/'scheduler-cycle.json',{'counts':result,
                        'syntheticDueTimeOverride':True,'realDownstreamCalls':0,'businessWrites':0})
                except Exception as exc:
                    private_json(home/'scheduler-cycle.json',{'errorType':type(exc).__name__,
                        'syntheticDueTimeOverride':True,'realDownstreamCalls':0,'businessWrites':0})
    finally:
        stop.set()
        if server:
            if thread:
                server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=2)
        if app:
            app.close()
        private_json(home / "business-audit.json", audit(service))


if __name__ == "__main__":
    main()
