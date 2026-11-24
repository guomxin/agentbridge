"""Browser acceptance of Workspace OA shortcuts on a real private NanoBot host with synthetic OA."""

import argparse
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.flow import TrustedFlowApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.support.business_draft_acceptance import OWNER
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_controlled_write_probe import ORIGIN, run
from tests.support.workspace_host_acceptance import add_fixture_signin
from tests.test_workspace import PASSWORD


class FixtureLogin:
    def __init__(self, service):
        self.service = service

    def authenticate(self, *, challenge_id, csrf_token, csrf_cookie, credentials):
        valid = bool(credentials) and all(v == "fixture-only" for v in credentials.values())
        credentials.clear()
        challenge = self.service.challenges.get(challenge_id)
        if not valid or challenge["system_id"] != "oa" or challenge["user_subject"] != OWNER:
            raise PermissionError("Synthetic OA credential rejected")
        self.service.challenges.claim(challenge_id, csrf_token=csrf_token, csrf_cookie=csrf_cookie)
        self.service.sessions.activate(challenge["session_id"], observed_principal_ref=OWNER)
        self.service.challenges.complete(challenge_id, result={"observed_principal_ref": OWNER})
        return {"status": "succeeded", "challengeId": challenge_id}


def serve(*, fixture, binding, instance, gateway, model, expired):
    service = fixture.service
    for key, profile in (("fixture-single", "missed_punch"), ("fixture-batch-1", "attendance_confirmation"),
                         ("fixture-batch-2", "weekly_report")):
        fixture.oa.add_affair(key, profile=profile)
    call_id = uuid4().hex
    tool = "mcp_ab_oa_workflow_pending_list"
    def query(body):
        if tool not in {entry["function"]["name"] for entry in body["tools"]}:
            raise ValueError("Fixture owner has no OA read permission")
        if any(row.get("role") == "tool" and row.get("tool_call_id") == call_id for row in body["messages"]):
            model.results.append("list")
            return "已读取合成 OA 待办，可在任务结果中重新查询或办理。", None
        return None, {"id": call_id, "type":"function","function":{"name":tool,"arguments":"{}"}}
    model.reply = query
    events = list(gateway.send_dispatch({"idempotency_key": "fixture-initial-query"}, binding,
                                       "查询本次合成 OA 待办。", [], None))
    assert events[-1].get("state") == "final" and model.results == ["list"], "Initial OA query did not complete"
    if expired:
        session = service.sessions.find(user_subject=OWNER, system_id="oa")
        service.sessions.mark_expired(session["session_id"])
    port = free_port()
    # Browser cookies are host scoped. Two independent cases can now remain open at once.
    host = "localhost" if expired else "127.0.0.1"
    web = f"http://{host}:{port}"
    local_cards = fixture.card_url
    fields = TrustedFieldApplication(submission_store=service.field_submissions)
    actions = TrustedActionApplication(authorization_store=service.write_authorizations)
    cards = create_auth_http_server(
        config=validate_auth_server_config(host="127.0.0.1", port=fixture.meta["card_port"],
            public_base_url=local_cards, tls_cert=None, tls_key=None),
        application=TrustedAuthApplication(challenge_store=service.challenges, broker=FixtureLogin(service)),
        field_application=fields, action_application=actions,
        flow_application=TrustedFlowApplication(service=service, field_application=fields, action_application=actions,
                                               workspace_base_url=web))
    workspace = WorkspaceApplication(service=service, host_routes={instance: gateway})
    with service.tasks._connect() as db:
        source = db.execute("SELECT task_id,status FROM agent_tasks WHERE host_task_key=? AND user_subject=?",
                            (events[0]["runId"], OWNER)).fetchone()
    groups = workspace.actionable_results(fixture.account, source["task_id"])
    assert source["status"] == "succeeded" and groups and len(groups[0]["targets"]) == 3, "Missing OA seed results"
    server = create_workspace_http_server(
        config=validate_workspace_server_config(host="127.0.0.1", port=port, public_base_url=web,
                                               tls_cert=None, tls_key=None), application=workspace)
    add_fixture_signin(server, web, {"cardreview": PASSWORD})
    base = server.RequestHandlerClass
    journal = (fixture.home.parent / "state/users" / hashlib.sha256(OWNER.encode()).hexdigest() /
               "coordinated-private.sqlite3")

    def receipt():
        with sqlite3.connect(service.db_path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            tasks = [dict(kind=kind, state=state, command=command) for kind, state, command in db.execute("""
                SELECT json_extract(summary_json,'$.command.kind'),status,json_extract(summary_json,'$.command.state')
                FROM agent_tasks WHERE user_subject=? AND origin_endpoint_id=? ORDER BY rowid""",
                (OWNER, binding["endpoint_id"]))]
            operations = [dict(capability=cap, state=state, count=n) for cap, state, n in db.execute(
                "SELECT capability_name,status,COUNT(*) FROM operations WHERE user_subject=? GROUP BY 1,2", (OWNER,))]
            batches = [dict(total=total, succeeded=done, state=state) for total,done,state in db.execute(
                "SELECT total_count,succeeded_count,state FROM task_batches WHERE user_subject=?", (OWNER,))]
            sessions = [dict(state=state,count=n) for state,n in db.execute(
                "SELECT state,count(*) FROM auth_challenges WHERE user_subject=? GROUP BY state", (OWNER,))]
        return {"tasks": tasks, "operations": operations, "batches": batches, "credentialChallenges": sessions,
                "syntheticOaWrites": len(fixture.oa.writes), "modelCalls": len(model.results)}

    def local_card(url):
        target = urlsplit(url)
        if target.scheme != "https" or "https://"+target.netloc != ORIGIN or target.fragment:
            raise ValueError("Unexpected isolated card issuer")
        if not target.path.startswith(("/auth/", "/input/", "/authorize/", "/flow/")):
            raise ValueError("Unexpected isolated card path")
        return local_cards + target.path + ("?"+target.query if target.query else "")

    class FixtureEntry(base):
        def do_GET(self):
            if self.path not in ("/fixture-card", "/fixture-receipt"):
                return super().do_GET()
            if self.headers.get("Host") != urlsplit(web).netloc:
                return self.send_error(403)
            account = self._authenticate()
            if not account:
                return
            if account["user_subject"] != OWNER:
                return self.send_error(403)
            if self.path == "/fixture-receipt":
                return self._json(200, receipt())
            if not journal.is_file():
                return self.send_error(404, "No card observed by the original coordinator")
            with sqlite3.connect(journal.resolve().as_uri()+"?mode=ro", uri=True) as db:
                current = db.execute("""SELECT interaction FROM coordinated WHERE phase='waiting' AND endpoint=?
                    ORDER BY rowid DESC LIMIT 1""", (binding["endpoint_id"],)).fetchone()
            if not current:
                return self.send_error(404, "No private card awaiting this endpoint")
            result = service.present_interaction(user_subject=OWNER, agent_host="nanobot",
                endpoint_key=binding["endpoint_key"], interaction_id=current[0])
            if result["endpoint"]["endpointId"] != binding["endpoint_id"]:
                return self.send_error(403)
            self.send_response(303)
            self.send_header("Location",local_card(result["interaction"]["presentation"]["url"]))
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Cache-Control","no-store")
            self.end_headers()

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = FixtureEntry
    for listener in (cards, server):
        threading.Thread(target=listener.serve_forever, daemon=True).start()
    try:
        print(json.dumps({"workspace":web+"/fixture-signin", "originalCard":web+"/fixture-card",
            "receipt":web+"/fixture-receipt", "initialOaSession":"expired" if expired else "active",
            "tls":"signed card path served by original central form on isolated loopback HTTP"}), flush=True)
        threading.Event().wait()
    finally:
        for listener in (server, cards):
            listener.shutdown()
            listener.server_close()
        workspace.close()


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nanobot-python", required=True, type=Path)
    parser.add_argument("--expired", action="store_true")
    args = parser.parse_args()
    run(args.nanobot_python, False, browser=True, exercise=partial(serve, expired=args.expired))
