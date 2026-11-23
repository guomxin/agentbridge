"""Two private, loopback-only views of a simulated Telegram Bot API with a real NanoBot host."""

import asyncio
import json
import re
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from aiohttp import web

from agentbridge.workspace.gateway import GatewayRequestError
from tests.support.nanobot_telegram_loopback import fixture, update


class FaultGateway:
    """Inject loss around an actual HTTP request, leaving the host and its ledger intact."""

    def __init__(self, actual):
        self.actual = actual
        self.instance_id = actual.instance_id
        self.next = ""

    def __getattr__(self, key):
        return getattr(self.actual, key)

    def send_dispatch(self, *args):
        mode, self.next = self.next, ""
        if mode == "admission-unknown":
            raise GatewayRequestError("GATEWAY_CONNECTION_CLOSED", "合成受理结果未知")
        stream = self.actual.send_dispatch(*args)
        try:
            first = next(stream)
            if mode == "lost-accept":
                raise GatewayRequestError(
                    "GATEWAY_CONNECTION_CLOSED", "合成受理回执丢失"
                )
            yield first
        finally:
            stream.close()


async def run_console(home, gateway, bindings, question, scenarios, *, pairing=False):
    loop = asyncio.get_running_loop()
    runners = []
    origin = {}
    sequence = 0
    last = {501: 0, 502: 0}
    last_text = {}
    deliveries_lost = set()
    notification_losses = set()
    by_sender = dict(zip(("501", "502"), bindings, strict=True))
    fault = FaultGateway(gateway)
    async_lock = asyncio.Lock()
    home.mkdir(mode=0o700, exist_ok=True)
    with ExitStack() as stack:
        state, _, create = stack.enter_context(
            fixture(home, actual_gateway=fault, actual_bindings={} if pairing else by_sender, allow_pairing=pairing)
        )
        executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))

        async def dispatch(fn, *args):
            return await loop.run_in_executor(executor, fn, *args)

        pilot = await dispatch(create)
        sequence=await dispatch(lambda: pilot.db.execute("SELECT COALESCE(MAX(next_update),0) FROM channel_meta").fetchone()[0])
        for uid in last:
            last[uid]=await dispatch(lambda who: pilot.db.execute("SELECT COALESCE(MAX(message),0) FROM messages WHERE chat=?",(who,)).fetchone()[0],uid)
        await dispatch(pilot.verify)

        def history(uid):
            rows = pilot.db.execute(
                "SELECT message, state,run_id,receipt FROM messages WHERE bot=? AND chat=? ORDER BY message",
                (pilot.bot_id, uid),
            ).fetchall()
            result = []
            for row in rows:
                reply = next(
                    (
                        item["text"]
                        for item in state["deliveries"]
                        if item["chat_id"] == uid
                        and (item.get("reply_parameters") or {}).get("message_id") == row["message"]
                    ),
                    None,
                )
                result.append(
                    {
                        "message": row["message"],
                        "state": row["state"],
                        "run": row["run_id"],
                        "receipt": row["receipt"],
                        "answer": reply,
                        "sendCalls": sum(
                            item["chat_id"] == uid
                            and (item.get("reply_parameters") or {}).get("message_id") == row["message"]
                            for item in state["deliveries"]
                        ),
                    }
                )
            return result

        def enqueue(uid, operation, code=None):
            nonlocal sequence
            if operation == "replay" and not last[uid]:
                return
            if operation != "replay":
                last[uid] += 1
            sequence += 1
            number = last[uid]
            if operation != "replay":
                text = {"chat": "合成电报问答", "long-chat": "合成长文私聊"}.get(operation, question)
                last_text[uid] = "/start " + code if code else text
            state["updates"] = [update(sequence, uid, number, text=last_text.get(uid, question))]
            fault.next = (
                operation if operation in {"admission-unknown", "lost-accept"} else ""
            )
            if operation == "lost-notification":
                notification_losses.add((uid, number))
            if operation == "lost-delivery":
                deliveries_lost.add((uid, number))
            pilot.poll_once()

        def lose_for(body):
            message = (body.get("reply_parameters") or {}).get("message_id")
            if message is None:
                return False
            key=(body["chat_id"],message)
            if key in notification_losses and body.get("text","").startswith("原任务"):
                return True
            return (
                body["chat_id"],
                message,
            ) in deliveries_lost

        state["lost_delivery_filter"] = lose_for

        def recheck(uid):
            peer = 502 if uid == 501 else 501
            row = pilot.db.execute(
                "SELECT message,run_id FROM messages WHERE chat=? AND run_id IS NOT NULL ORDER BY message DESC LIMIT 1",
                (peer,),
            ).fetchone()
            if not row:
                return "请先由另一位用户发起一次已受理查询。"
            try:
                gateway._request(
                    "events",
                    {
                        "binding": gateway.resolve_telegram_sender(8001,uid) if pairing else by_sender[str(uid)],
                        "runId": row["run_id"],
                        "requestKey": pilot._key(peer, row["message"]),
                        "after": 0,
                    },
                )
            except GatewayRequestError as e:
                if e.code in {"NANOBOT_BINDING_UNAVAILABLE", "NANOBOT_REQUEST_FAILED"}:
                    return "已用本人的绑定读取对方原运行，宿主拒绝。"
                raise
            raise RuntimeError("Unexpected cross-subject result")

        def snapshot(uid):
            rows = history(uid)
            with sqlite3.connect(home.parent / "state" / "runs" / "runs.sqlite3") as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM runs WHERE subject=?",
                    (by_sender[str(uid)]["user_subject"],),
                ).fetchone()[0]
            notifications=[]
            if pairing:
                notifications=[{"message":item["message"],"state":item["state"],"attempts":sum(
                    v.get("chat_id")==uid and (v.get("reply_parameters") or {}).get("message_id")==item["message"] and
                    v.get("text","").startswith("原任务") for v in state["deliveries"])}
                    for item in pilot.db.execute("""SELECT m.message,n.state FROM notification_receipts n JOIN messages m
                        ON n.bot=m.bot AND n.chat=m.chat AND n.run=m.run_id WHERE n.chat=? ORDER BY m.message""",(uid,))]
                central_file=home.parent.parent/"mcp"/"central"/"agentbridge.db"
                if central_file.exists():
                    with sqlite3.connect(central_file) as db:
                        states=db.execute("""SELECT t.host_task_key,t.status,o.delivery_id,o.state,o.attempt_count FROM agent_tasks t
                            JOIN notification_outbox o USING(task_id) JOIN client_endpoints c ON c.endpoint_id=o.endpoint_id
                            WHERE t.user_subject=? AND t.agent_host='nanobot' AND c.endpoint_key=? AND c.endpoint_id=t.origin_endpoint_id
                            AND json_extract(o.payload_json,'$.eventType') IN ('task.completed','task.operation.succeeded','task.failed','task.operation.failed','task.canceled')""",
                            (by_sender[str(uid)]["user_subject"],f"telegram:8001:{uid}")).fetchall()
                    local={item["message"]:item for item in notifications}
                    for number,run,delivery in pilot.db.execute("""SELECT m.message,n.run,n.delivery FROM notification_receipts n JOIN messages m
                            ON n.bot=m.bot AND n.chat=m.chat AND n.run=m.run_id WHERE n.chat=?""",(uid,)):
                        match=next((row for row in states if row[0]==run and row[2]==delivery),None)
                        if match and number in local:
                            local[number].update(business=match[1],centralDelivery=match[3],centralAttempts=match[4])
            return {
                "notifications":notifications, "rows": rows,
                "mirrors": [item["text"] for item in state["deliveries"]
                            if item["chat_id"] == uid and not item.get("reply_parameters")],
                "hostRuns": count,
                "modelTurns": scenarios[0 if uid == 501 else 1].tool_requested,
            }

        async def consumer():
            while True:
                async with async_lock:
                    await dispatch(pilot.poll_once)
                await asyncio.sleep(0.35)

        polling = asyncio.create_task(consumer())

        def app_for(uid):
            cookie = "tg_fixture_" + str(uid)
            session = secrets.token_urlsafe(32)
            app = web.Application(client_max_size=2048)

            def auth(r, post=False):
                if r.headers.get("Host") != origin[uid].split("://")[1]:
                    raise web.HTTPForbidden()
                if post and r.headers.get("Origin") != origin[uid]:
                    raise web.HTTPForbidden()
                if not secrets.compare_digest(r.cookies.get(cookie, ""), session):
                    raise web.HTTPForbidden()

            async def index(r):
                if r.headers.get("Host") != origin[uid].split("://")[1]:
                    raise web.HTTPForbidden()
                html = (
                    (Path(__file__).with_suffix(".html"))
                    .read_text()
                    .replace("USER_NAME", "甲" if uid == 501 else "乙").replace("PAIR_HIDDEN", "" if pairing else "hidden")
                )
                response = web.Response(
                    text=html,
                    content_type="text/html",
                    headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
                )
                response.set_cookie(
                    cookie, session, httponly=True, samesite="Strict", path="/"
                )
                return response

            async def status(r):
                auth(r)
                async with async_lock:
                    data = await dispatch(snapshot, uid)
                return web.json_response(data, headers={"Cache-Control": "no-store"})

            async def action(r):
                auth(r, True)
                payload = await r.json()
                command = payload["command"]
                results = {}
                async with async_lock:
                    if command in {
                        "send",
                        "chat",
                        "long-chat",
                        "replay",
                        "lost-accept",
                        "admission-unknown",
                        "lost-delivery",
                        "lost-notification",
                    }:
                        await dispatch(enqueue, uid, command)
                    elif command=="pair" and pairing:
                        code=payload.get("code","")
                        if not isinstance(code,str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32}",code):
                            raise web.HTTPBadRequest()
                        await dispatch(enqueue,uid,"pair",code)
                    elif command == "peer":
                        results["check"] = await dispatch(recheck, uid)
                    elif command == "restart":
                        nonlocal pilot
                        await dispatch(pilot.db.close)
                        pilot = await dispatch(create)
                        await dispatch(pilot.verify)
                    else:
                        raise web.HTTPBadRequest()
                    results.update(await dispatch(snapshot, uid))
                return web.json_response(results, headers={"Cache-Control": "no-store"})

            app.router.add_get("/", index)
            app.router.add_get("/state", status)
            app.router.add_post("/action", action)
            return app

        try:
            ports_file=home/"fixture-ports.json"
            ports=json.loads(ports_file.read_text()) if ports_file.exists() else {}
            for uid, domain in [(501, "127.0.0.1"), (502, "localhost")]:
                runner = web.AppRunner(app_for(uid), access_log=None)
                await runner.setup()
                runners.append(runner)
                site = web.TCPSite(runner, "127.0.0.1", ports.get(str(uid),0))
                await site.start()
                origin[uid] = f"http://{domain}:{runner.addresses[0][1]}"
            ports_file.write_text(json.dumps({uid:int(address.split(":")[-1]) for uid,address in origin.items()}))
            print(json.dumps({"telegram_console": origin}), flush=True)
            await polling
        finally:
            polling.cancel()
            for runner in runners:
                await runner.cleanup()
            await dispatch(pilot.db.close)
