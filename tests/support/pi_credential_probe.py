"""Pi SDK and real central L2 credential continuation, with an isolated synthetic credential broker."""
from argparse import ArgumentParser
from contextlib import nullcontext
import asyncio
from pathlib import Path
import json
import secrets
import signal
import sqlite3
import tempfile
import threading
import sys
import time

from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_credential_probe import ORIGIN, credential_service
from tests.support.pi_sdk_probe import PACKAGE, host_fixture, private
from tests.support.pi_workspace_acceptance import central_fixture


class Model:
    def __init__(self):
        self.failures = []
        self.calls = 0
        self.outcome = ""

    def reply(self, payload):
        assert ORIGIN not in json.dumps(payload)
        names = {item["function"]["name"] for item in payload["tools"]}
        assert "oa_workflow_pending_list" in names and "oa_session_login" in names
        assert not any(n.startswith("agentbridge_host_") or n.endswith(("_resume", "_commit")) for n in names)
        self.calls += 1
        last = payload["messages"][-1]
        if last["role"] == "tool":
            text = str(last["content"])
            self.outcome = next((label for marker, label in [
                ("会话状态待核对", "status_unconfirmed"), ("凭证状态待核对", "credential_unconfirmed"),
                ("需要可信私有交互", "private_only"), ("未确认本次读取", "read_unconfirmed"), ("权限", "permission"),
                ("采购申请", "read_returned"), ("已关闭", "card_closed")] if marker in text), "unrecognized")
            if "过期" in text or "关闭" in text:
                return "凭证卡已关闭。", None
            assert "采购申请" in text and "验收申请" in text
            return "本人有采购申请和验收申请。", None
        return None, {"id": "query-" + str(self.calls), "type": "function",
                      "function": {"name": "oa_workflow_pending_list", "arguments": '{"limit":3}'}}


def exercise(directory, node, sdk, expire=False, *, serve=False, desktop=None):
    port = free_port()
    token = private(directory / "route.token", secrets.token_urlsafe(40))
    state = {}

    def configure(central):
        service = central.service
        assert service.home.resolve().is_relative_to(directory.resolve())
        with sqlite3.connect(service.db_path) as db:
            db.execute("""INSERT OR REPLACE INTO agent_host_compatibility VALUES
                ('pi','0.1.4','L2','isolated-pi-l2-check','isolated-fixture',CURRENT_TIMESTAMP)""")
        session = service.sessions.find(user_subject=central.subjects["a"], system_id="oa")
        service.sessions.mark_expired(session["session_id"])
        state["central"] = central

    model = Model()
    with central_fixture(directory, host_url=f"http://127.0.0.1:{port}", token_file=token, maximum_level="L2", configure=configure,
            desk_bot=desktop.central_bot if desktop else None,
            central_options={"trusted_card_origin": ORIGIN, "service_class": credential_service()}) as (meta, accounts):
        pilot = desktop.pilot(accounts) if desktop else {}
        with host_fixture(directory, node, sdk, meta, accounts, port=port, token_file=token,
                model=model, engine_python=desktop.engine if desktop else None,
                options={"trustedCardOrigins": [ORIGIN], "requiredLevel": "L2", **pilot}) as (post, _, _, _), (
                desktop.consumer(directory, port, token, accounts) if desktop else nullcontext()) as check_channel:
            assert post("ready").json()["acceptedLevel"] == "L2"
            if serve:
                stopped = threading.Event()
                signal.signal(signal.SIGTERM, lambda *_: stopped.set())
                signal.signal(signal.SIGINT, lambda *_: stopped.set())
                print(json.dumps({"signin": meta["signin"], "brokerTrigger": str(directory / "confirm-fixture")}), flush=True)
                while not stopped.wait(.25):
                    if check_channel:
                        check_channel()
                    trigger = directory / "confirm-fixture"
                    if not trigger.is_file():
                        continue
                    trigger.unlink()
                    service = state["central"].service
                    subject = accounts["a"]["user_subject"]
                    with sqlite3.connect(service.db_path) as db:
                        pending = db.execute("SELECT challenge_id,session_id FROM auth_challenges WHERE state='pending' AND user_subject=?",
                                             (subject,)).fetchone()
                    if pending:
                        cid, sid = pending
                        csrf = service.challenges.issue_csrf(cid)
                        service.challenges.claim(cid, csrf_cookie=csrf, csrf_token=csrf)
                        service.sessions.activate(sid, observed_principal_ref=subject)
                        service.challenges.complete(cid, result={"observed_principal_ref": subject})
                return
            key = {"binding": accounts["a"], "requestKey": "credential"}
            run = post("accept", **key, message="查本人待办").json()["runId"]
            for _ in range(200):
                public = post("lookup", **key).json()
                if public.get("hasPrivateCard") or public["state"] != "accepted":
                    break
                time.sleep(.1)
            assert public.get("hasPrivateCard"), (public["state"], model.calls, model.outcome, model.failures)
            args = {**key, "runId": run}
            shown = post("card", **args).json()
            url = shown["url"]
            assert url.startswith(ORIGIN + "/") and post("card", **args).json()["url"] == url
            assert post("card", **{**args, "binding": accounts["b"]}).status_code == 403
            # The SDK's original listener is waiting. Race a second real Pi
            # presenter against its existing row without acquiring its resume.
            sys.path.insert(0, str(PACKAGE))
            try:
                from pi_agentbridge.cards import Cards
                from pi_agentbridge.central import connect
                from pi_agentbridge.runtime import private_file
                spec = json.loads(private_file(directory / "host.json"))
                async def compete():
                    async with connect(spec, accounts["a"], private_file(meta["tokenFiles"]["a"]).strip()) as central:
                        with sqlite3.connect(directory / "pi-state/pi-runs.sqlite3") as db:
                            state_before = db.execute("SELECT interaction,state,revision FROM cards WHERE run=?", (run,)).fetchone()
                            listener = Cards(db, [ORIGIN])
                            outcome = await listener.resume_credential(central, accounts["a"], run, shown["interaction"])
                            after = db.execute("SELECT interaction,state,revision FROM cards WHERE run=?", (run,)).fetchone()
                            assert outcome == "unknown" and state_before == after == (shown["interaction"]["interactionId"],"waiting",1)
                asyncio.run(compete())
            finally:
                sys.path.remove(str(PACKAGE))
            service = state["central"].service
            subject = accounts["a"]["user_subject"]
            with sqlite3.connect(service.db_path) as db:
                cid, sid = db.execute("""SELECT challenge_id,session_id FROM auth_challenges
                    WHERE state='pending' AND user_subject=?""", (subject,)).fetchone()
                if expire:
                    db.execute("UPDATE auth_challenges SET expires_at='2000-01-01T00:00:00Z' WHERE challenge_id=?", (cid,))
            if not expire:
                csrf = service.challenges.issue_csrf(cid)
                service.challenges.claim(cid, csrf_cookie=csrf, csrf_token=csrf)
                service.sessions.activate(sid, observed_principal_ref=subject)
                service.challenges.complete(cid, result={"observed_principal_ref": subject})
            for _ in range(200):
                public = post("lookup", **key).json()
                if public["state"] != "accepted":
                    break
                time.sleep(.12)
            assert public["state"] == "completed" and ("已关闭" if expire else "验收申请") in public["answer"], (public["state"], model.failures)
            assert not post("card", **args).json()["available"] and not public["hasPrivateCard"]
            assert not model.failures and model.calls == 2
            original_resumes = service.verified_resumes
            assert original_resumes == (0 if expire else 1)
            assert len(state["central"].calls) == (0 if expire else 1)
            assert ORIGIN not in json.dumps(post("events", **args, after=0).json())
            for path in (directory / "pi-state").rglob("*"):
                if path.is_file():
                    assert url.encode() not in path.read_bytes()
            if not expire:
                sys.path.insert(0, str(PACKAGE))
                try:
                    from pi_agentbridge.central import connect
                    from pi_agentbridge.runtime import private_file
                    async def repeat():
                        spec = json.loads(private_file(directory / "host.json"))
                        async with connect(spec, accounts["a"], private_file(meta["tokenFiles"]["a"]).strip()) as central:
                            ident = shown["interaction"]["interactionId"]
                            statuses, poll_ids = [], []
                            for _ in range(2):
                                poll = await central.session.call_tool("agentbridge_interaction_get",
                                    {"interaction_id":ident, "wait_seconds":0}, meta=central.meta)
                                observed = (poll.meta or {}).get("io.agentbridge/interaction") or (poll.structuredContent or {}).get("interaction") or {}
                                assert not poll.isError and observed.get("state") == "completed"
                                poll_ids.append(observed.get("interactionId"))
                                resumed = await central.session.call_tool("agentbridge_interaction_resume",
                                    {"interaction_id":ident, "idempotency_key":f"pi:{run}:{ident}"},
                                    meta={**central.meta, "io.agentbridge/session-ready-only":True})
                                assert not resumed.isError
                                statuses.append((resumed.structuredContent or {}).get("status"))
                            assert poll_ids == [ident,ident] and statuses[0] == statuses[1] and statuses[0] in {"already_resumed","succeeded"}
                    asyncio.run(repeat())
                finally:
                    sys.path.remove(str(PACKAGE))
                assert len(state["central"].calls) == 1 and not post("card", **args).json()["available"] and model.calls == 2
            return {"level": "L2", "expired": expire, "resumes": original_resumes, "duplicateReceiptChecks":0 if expire else 2,
                    "competingListenerRefused": True,
                    "downstreamReads": len(state["central"].calls), "crossSubjectDenied": True, "modelCalls": model.calls}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--sdk-dir", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    for expire in (False, True):
        with tempfile.TemporaryDirectory(prefix="pi-l2-") as directory:
            outcome = exercise(Path(directory), args.node.resolve(), args.sdk_dir.resolve(), expire, serve=args.serve)
            if args.serve:
                break
            print(json.dumps(outcome), flush=True)


if __name__ == "__main__":
    main()
