"""Verify ordinary chat across the isolated Workspace and Telegram transports."""

import json
import sqlite3
import time
from contextlib import ExitStack
from uuid import uuid4

import httpx

from tests.support.nanobot_b4_files import login


def wait_for(check, description):
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.4)
    raise AssertionError(f"Timed out waiting for {description}")


def run(home):
    public = json.loads((home / "public.json").read_text())
    ports = json.loads((home / "b4host/telegram/fixture-ports.json").read_text())
    web_text = ("合成网页问答", "合成网页答复")
    telegram_text = ("合成电报问答", "合成电报答复")

    def timeline(client, instance="nanobot-office"):
        response = client.get("/api/timeline", params={"entry_type": "chat_message", "view_instance": instance})
        response.raise_for_status()
        return [(item["role"], item["text"]) for item in response.json()["items"]]

    with ExitStack() as resources:
        web = resources.enter_context(login(public["signin"]["a"], "pilota"))
        other_web = resources.enter_context(login(public["signin"]["b"], "pilotb"))

        def bot(who, host):
            url = f"http://{host}:{ports[who]}"
            client = resources.enter_context(httpx.Client(base_url=url, headers={"Origin": url}, trust_env=False, timeout=45))
            client.get("/").raise_for_status()
            return client

        private, other_private = bot("501", "127.0.0.1"), bot("502", "localhost")
        before = private.get("/state").json()
        assert not before["rows"], "Fresh binding must receive a mirror before its first Telegram run"
        response = web.post("/api/chat/send-stream", json={"message": web_text[0], "idempotencyKey": "chat-" + str(uuid4())})
        response.raise_for_status()
        assert web_text[1] in response.text
        wait_for(lambda: len(private.get("/state").json()["mirrors"]) >= 2, "two private Telegram mirror receipts")
        mirrors = private.get("/state").json()["mirrors"]
        assert len(mirrors) == 2 and all(text in mirror for text, mirror in zip(web_text, mirrors, strict=True))
        assert other_private.get("/state").json()["mirrors"] == []

        private.post("/action", json={"command": "chat"}).raise_for_status()
        expected = [("user", telegram_text[0]), ("assistant", telegram_text[1])]
        def telegram_entries():
            return [row for row in timeline(web) if row[1] in telegram_text]
        wait_for(lambda: telegram_entries() == expected, "two Telegram roles on the Workspace timeline")
        private.post("/action", json={"command": "replay"}).raise_for_status()
        private.post("/action", json={"command": "restart"}).raise_for_status()
        state = private.get("/state").json()
        assert len(state["rows"]) == 1 and state["rows"][0]["state"] == "delivered"
        assert state["rows"][0]["sendCalls"] == 1 and state["mirrors"] == mirrors
        assert state["hostRuns"] == before["hostRuns"] + 2
        assert telegram_entries() == expected
        for visible in (timeline(other_web), timeline(web, "openclaw-default")):
            assert all(text not in web_text + telegram_text for _, text in visible)

        private.post("/action", json={"command": "long-chat"}).raise_for_status()
        long_answer = "合成长文开始" + "甲🟢" * 2200 + "合成长文结束"
        wait_for(lambda: timeline(web).count(("assistant", long_answer)) == 1, "one complete multipart reply")
        long_receipt = private.get("/state").json()["rows"][-1]
        assert long_receipt["sendCalls"] == 2 and long_receipt["state"] == "delivered"
        assert not other_private.get("/state").json()["mirrors"]

        legacy = "校验原宿主普通通道"
        hosts = web.get("/api/hosts").json()
        selected = web.post("/api/hosts/default", json={
            "instanceId": "openclaw-default", "expectedRevision": hosts["revision"]})
        selected.raise_for_status()
        try:
            response = web.post("/api/chat/send-stream", json={
                "message": legacy, "idempotencyKey": "legacy-" + str(uuid4())})
            response.raise_for_status()
        finally:
            web.post("/api/hosts/default", json={
                "instanceId": "nanobot-office", "expectedRevision": selected.json()["revision"]}).raise_for_status()

        with sqlite3.connect(home / "mcp/central/agentbridge.db") as db:
            destinations = db.execute("""SELECT e.agent_host,e.client_type,o.state,u.role
                FROM notification_outbox o JOIN user_timeline u ON o.event_id=u.entry_id
                JOIN client_endpoints e ON e.endpoint_id=o.endpoint_id
                WHERE o.payload_type='timeline_message' AND u.text IN (?,?,?,?) ORDER BY u.sequence""",
                (*web_text, *telegram_text)).fetchall()
            assert destinations == [("nanobot", "telegram", "acknowledged", role) for role in ("user", "assistant")]
            original = db.execute("""SELECT e.agent_host,e.external_subject FROM notification_outbox o
                JOIN user_timeline u ON o.event_id=u.entry_id JOIN client_endpoints e ON e.endpoint_id=o.endpoint_id
                WHERE o.payload_type='timeline_message' AND u.text=?""", (legacy,)).fetchall()
            assert original == [("openclaw", "501")], original
        return {"webToTelegramRoles": 2, "telegramToWebRoles": 2, "ordinaryHostRuns": 2, "longReplyParts": 2,
                "crossAccountOrOpenClaw": 0, "duplicateSends": 0}
