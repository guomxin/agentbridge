"""Integration tests for NanoBot Telegram Batch 1 (rich cards, companion sync, formatting) and Batch 2 (result views, prepared commands, skills)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from uuid import uuid4

from agentbridge.core import actionable_results as actions
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.telegram_result_views import ResultViewService
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from agentbridge.workspace.telegram_pilot_links import (
    NANOBOT_TELEGRAM_CAPABILITIES,
    TelegramPilotLinks,
)
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture
from tests.support.nanobot_telegram_setup import INSTANCE, seed_telegram_peers
from tests.test_workspace import PASSWORD

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge")
)
from nanobot_agentbridge.telegram import TelegramAPI, TelegramPilot  # noqa: E402
from nanobot_agentbridge.telegram_presentation import EXTERNAL_BROWSER_LABEL  # noqa: E402

BOT = 8001
HTTPS_ORIGIN = "https://bridge.example.test"


def _future(minutes: int = 15) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _register_nanobot(service, user_subject: str, token_id: str, instance_id: str) -> dict:
    vectors = Path(__file__).resolve().parents[1] / "schemas/agent-host/v1/test-vectors.json"
    profile = json.loads(vectors.read_text(encoding="utf-8"))["profiles"][0]["value"]
    profile["implementation"]["name"] = "nanobot"
    profile["implementation"]["version"] = "0.3.0"
    profile["hostInstanceId"] = instance_id
    service.negotiate_host(user_subject=user_subject, token_id=token_id, profile=profile)
    return service.require_host_registration(
        user_subject=user_subject,
        agent_host="nanobot",
        host_instance_id=instance_id,
        host_version="0.3.0",
        minimum_level="L3",
    )


def _rich_loopback(home: Path):
    """Self-contained loopback Telegram Bot API + NanoBot Gateway double for rich card/result-view tests."""
    binding = {
        "user_subject": "alpha",
        "endpoint_id": "test-endpoint-501",
        "endpoint_key": f"telegram:{BOT}:501",
        "session_key": "agentbridge:telegram:00000000-0000-0000-0000-000000000501",
        "credential_ref": "test-credential-alpha",
    }
    token_file = home / "gw-token"
    token_file.write_text("local-only-synthetic-rich-token-for-test", encoding="utf-8")
    token_file.chmod(0o600)
    state = {
        "updates": [],
        "deliveries": [],
        "typing_calls": [],
        "receipts": [],
        "admissions": {},
        "received_sources": [],
        "pending": False,
        "has_private_card": False,
        "card_payload": None,
        "has_result_view": False,
        "result_view_payload": None,
        "final_answer": "**查询完成**\n共 `2` 项待办",
        "notifications": [],
        "mirror_peers": [],
        "lost_card_delivery": False,
        "typing_status": 200,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.startswith("/bottest-token/"):
                method = self.path.split("/")[-1]
                if method == "getMe":
                    result = {"id": BOT, "is_bot": True, "username": "rich_bot"}
                elif method == "getWebhookInfo":
                    result = {"url": ""}
                elif method == "getUpdates":
                    result = [u for u in state["updates"] if u["update_id"] >= body["offset"]]
                elif method == "sendChatAction":
                    state["typing_calls"].append(body)
                    if state["typing_status"] != 200:
                        self.send_error(state["typing_status"])
                        return
                    result = True
                elif method == "sendMessage":
                    state["deliveries"].append(body)
                    if state["lost_card_delivery"] and body.get("reply_markup"):
                        self.close_connection = True
                        return
                    result = {
                        "message_id": 900 + len(state["deliveries"]),
                        "chat": {"id": body["chat_id"], "type": "private"},
                        "from": {"id": BOT},
                        **(
                            {"reply_to_message": {"message_id": body["reply_parameters"]["message_id"]}}
                            if body.get("reply_parameters")
                            else {}
                        ),
                    }
                else:
                    self.send_error(404)
                    return
                answer = {"ok": True, "result": result}
            else:
                if self.headers.get("Authorization") != "Bearer " + token_file.read_text(encoding="utf-8"):
                    self.send_error(403)
                    return
                if self.path == "/v1/telegram-binding":
                    answer = {"verified": True}
                elif self.path == "/v1/telegram-received-source":
                    assert body["binding"] == binding
                    state["received_sources"].append(body)
                    # This older rich-card scenario exercises the inherited pairing path.
                    answer = {"accepted": True, "generationScoped": False}
                elif self.path == "/v1/telegram-timeline":
                    answer = {"recorded": True}
                elif self.path == "/v1/telegram-mirror-peers":
                    answer = {"senderIds": list(state["mirror_peers"])}
                elif self.path == "/v1/accept":
                    key = body["requestKey"]
                    assert any(key == f"tg:{BOT}:501:{item['messageId']}"
                               for item in state["received_sources"])
                    run = state["admissions"].setdefault(key, f"run-{len(state['admissions']) + 1}")
                    answer = {"runId": run, "state": "accepted"}
                elif self.path == "/v1/lookup":
                    run = state["admissions"][body["requestKey"]]
                    answer = {
                        "runId": run,
                        "state": "accepted" if state["pending"] else "completed",
                        "answer": state["final_answer"],
                        "hasPrivateCard": state["has_private_card"],
                        "cardRevision": 1 if state["has_private_card"] else 0,
                        "hasResultView": state["has_result_view"],
                    }
                elif self.path == "/v1/card":
                    answer = state["card_payload"] or {"available": False}
                elif self.path == "/v1/result-view":
                    answer = state["result_view_payload"] or {"available": False}
                elif self.path == "/v1/telegram-notifications":
                    answer = {"deliveries": list(state["notifications"])}
                elif self.path == "/v1/telegram-notification-receipt":
                    state["receipts"].append(body)
                    answer = {"recorded": True}
                else:
                    self.send_error(404)
                    return
                answer["instance"] = "nanobot-pilot"
            raw = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    gateway = NanoBotGatewayClient(url=base, token_file=token_file, instance_id="nanobot-pilot")

    def create_pilot() -> TelegramPilot:
        pilot = TelegramPilot(
            home / "messages.sqlite3",
            api=TelegramAPI("test-token", lambda: HTTPConnection("127.0.0.1", server.server_port, timeout=5)),
            bot_id=BOT,
            instance="nanobot-pilot",
            gateway=gateway,
            bindings=lambda: {"501": binding},
            allow_pairing=True,
        )
        pilot.next_notifications = 0
        return pilot

    return server, state, create_pilot


def test_companion_workspace_to_nanobot_telegram_card_and_status_sync_with_strict_isolation(tmp_path, monkeypatch):
    home = tmp_path / "central"
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(home / "session.key"))
    fixture = NanoBotMcpFixture(home, HTTPS_ORIGIN, 7199)
    try:
        endpoints = seed_telegram_peers(fixture)
        service = fixture.service
        links = TelegramPilotLinks(service)
        subject_a = fixture.subjects["a"]
        subject_b = fixture.subjects["b"]
        token_a = fixture.tokens["a"]
        token_b = fixture.tokens["b"]

        # Add a second NanoBot instance for subject_a to verify strict cross-instance isolation.
        with service.workspace._connect() as db:
            accounts = dict(db.execute("SELECT user_subject, account_id FROM workspace_accounts"))
        other_inst = "nanobot-second-instance"
        service.workspace.hosts.save_instance(
            other_inst,
            host_type="nanobot",
            display_name="Second NanoBot",
            route_ref=other_inst,
            adapter_version="0.3.0",
            state="enabled",
            subjects=[subject_a],
            actor="local-fixture",
            reason="cross-instance isolation check",
            expected_revision=0,
        )
        web2, _ = service.tasks.ensure_endpoint(
            user_subject=subject_a,
            token_id=token_a,
            agent_host="nanobot",
            endpoint_key="web-fixture-a-inst2",
            client_type="web",
            external_subject=accounts[subject_a],
            account_id=accounts[subject_a],
            conversation_ref="tg-fixture-web-a-inst2",
        )
        service.workspace.hosts.bind(
            accounts[subject_a],
            other_inst,
            endpoint_id=web2["endpoint_id"],
            credential_ref="tg-fixture-web-cred-a-inst2",
        )
        routes_file = tmp_path / "nanobot-routes.json"
        routes_file.write_text(
            json.dumps(
                {
                    "routes": {
                        INSTANCE: {
                            "instanceId": INSTANCE,
                            "telegram": {"botId": str(BOT), "username": "fixture_bot"},
                            "subjects": {
                                subject_a: "tg-fixture-web-cred-a",
                                subject_b: "tg-fixture-web-cred-b",
                            },
                        },
                        other_inst: {
                            "instanceId": other_inst,
                            "telegram": {"botId": "8002", "username": "fixture_bot_2"},
                            "subjects": {subject_a: "tg-fixture-web-cred-a-inst2"},
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        routes_file.chmod(0o600)
        monkeypatch.setenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", str(routes_file))

        # Materialize NanoBot Telegram endpoints via TelegramPilotLinks (verified existing peers).
        assert links.verified(subject_a, INSTANCE, BOT, 501)
        ensured_a = links.ensure_endpoint(subject_a, token_a, INSTANCE, BOT, 501)
        ensured_b = links.ensure_endpoint(subject_b, token_b, INSTANCE, BOT, 502)
        ep_a = service.tasks.get_endpoint(ensured_a["endpointId"], user_subject=subject_a)
        assert set(NANOBOT_TELEGRAM_CAPABILITIES).issubset(set(ep_a["capabilities"]))
        ep_b = service.tasks.get_endpoint(ensured_b["endpointId"], user_subject=subject_b)
        ws_ep_id = ensured_a["workspaceEndpointId"]
        ws_ep = service.tasks.get_endpoint(ws_ep_id, user_subject=subject_a)
        ensured_other = links.ensure_endpoint(subject_a, token_a, other_inst, 8002, 501)

        # Register L3 host for INSTANCE and create a Workspace-originated NanoBot task for subject_a.
        _register_nanobot(service, subject_a, token_a, INSTANCE)
        task, _ = service.tasks.ensure_task(
            user_subject=subject_a,
            agent_host="nanobot",
            host_task_key=f"ws-task-{uuid4().hex}",
            origin_endpoint_id=ws_ep["endpoint_id"],
            active_conversation_ref=ws_ep["conversation_ref"],
            title="工作台发起的请假审批",
        )
        # Trigger a real credential interaction card on this Workspace task.
        oa_sess = service.sessions.find(user_subject=subject_a, system_id="oa")
        if oa_sess:
            service.sessions.mark_expired(oa_sess["session_id"], "test_require_login")
        inv = service.start_login(
            user_subject=subject_a,
            system_id="oa",
            expected_principal_ref=subject_a,
            card_base_url=HTTPS_ORIGIN,
            task_id=task["task_id"],
        )
        interaction_id = (
            (inv.get("interaction") or {}).get("interactionId")
            or service.tasks.get_task(task["task_id"], user_subject=subject_a)["current_interaction_id"]
        )
        assert interaction_id

        # Legacy consumer (include_workspace_chat=False) MUST NOT receive companion Workspace events.
        legacy = service.claim_host_notifications(
            user_subject=subject_a,
            agent_host="nanobot",
            endpoint_key=ep_a["endpoint_key"],
            include_workspace_chat=False,
        )
        assert legacy["notifications"] == []

        # Full NanoBot Telegram consumer receives trusted_interaction card bound to Telegram endpoint.
        claimed = service.claim_host_notifications(
            user_subject=subject_a,
            agent_host="nanobot",
            endpoint_key=ep_a["endpoint_key"],
            include_workspace_chat=True,
        )
        waiting_items = [n for n in claimed["notifications"] if n["deliveryMode"] == "trusted_interaction"]
        assert len(waiting_items) == 1
        card = waiting_items[0]["interaction"]
        assert card["interactionId"] == interaction_id
        assert card["presentation"]["url"].startswith(HTTPS_ORIGIN)
        assert card["presentation"]["modelMustNotCollectValues"] is True

        # Strict isolation: OpenClaw TG (endpoints["a"]), other NanoBot instance, and User B receive ZERO deliveries.
        oc_ep = service.tasks.get_endpoint(endpoints["a"], user_subject=subject_a)
        oc_claimed = service.claim_host_notifications(
            user_subject=subject_a,
            agent_host="openclaw",
            endpoint_key=oc_ep["endpoint_key"],
        )
        assert oc_claimed["notifications"] == []

        other_claimed = service.claim_host_notifications(
            user_subject=subject_a,
            agent_host="nanobot",
            endpoint_key=ensured_other["endpointKey"],
            include_workspace_chat=True,
        )
        assert other_claimed["notifications"] == []

        b_claimed = service.claim_host_notifications(
            user_subject=subject_b,
            agent_host="nanobot",
            endpoint_key=ep_b["endpoint_key"],
            include_workspace_chat=True,
        )
        assert b_claimed["notifications"] == []

        # Complete the Workspace task -> paired NanoBot TG gets terminal status delivery.
        service.finish_host_task(
            user_subject=subject_a,
            task_id=task["task_id"],
            outcome="succeeded",
            reason="approved_and_verified",
        )
        after_finish = service.claim_host_notifications(
            user_subject=subject_a,
            agent_host="nanobot",
            endpoint_key=ep_a["endpoint_key"],
            include_workspace_chat=True,
        )
        status_events = [
            n["event"]["eventType"]
            for n in after_finish["notifications"]
            if n["deliveryMode"] == "status"
        ]
        assert "task.completed" in status_events

        # Revoke pairing (and mark existing openclaw peer revoked to simulate full unlink) ->
        # subsequent Workspace task NEVER pushes to revoked Telegram endpoint.
        with service.workspace._connect() as db:
            db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoints["a"],))
            db.commit()
        assert links.revoke(accounts[subject_a], INSTANCE, BOT)["state"] == "unlinked"
        task2, _ = service.tasks.ensure_task(
            user_subject=subject_a,
            agent_host="nanobot",
            host_task_key=f"ws-task-revoked-{uuid4().hex}",
            origin_endpoint_id=ws_ep["endpoint_id"],
            active_conversation_ref=ws_ep["conversation_ref"],
            title="解绑后的工作台任务",
        )
        service.finish_host_task(
            user_subject=subject_a,
            task_id=task2["task_id"],
            outcome="succeeded",
            reason="done_after_revoke",
        )
        with service.tasks._connect() as db:
            revoked_deliveries = db.execute(
                "SELECT COUNT(*) FROM notification_outbox o JOIN task_events e ON e.event_id=o.event_id "
                "WHERE e.task_id=? AND o.endpoint_id=?",
                (task2["task_id"], ep_a["endpoint_id"]),
            ).fetchone()[0]
        assert revoked_deliveries == 0
    finally:
        fixture.close()


def test_telegram_pilot_rich_card_dedupe_result_view_markdown_typing_and_lost_receipt(tmp_path):
    server, state, create_pilot = _rich_loopback(tmp_path)
    try:
        pilot = create_pilot()
        pilot.verify()
        card_url = f"{HTTPS_ORIGIN}/flow/input/tok-1234567890abcdef"
        interaction = {
            "schemaVersion": "agentbridge.interaction.v1",
            "interactionId": "interaction-rich-00000001",
            "type": "business_input",
            "state": "pending",
            "title": "请核对出差审批",
            "message": "确认后提交。",
            "presentation": {
                "owner": "agentbridge",
                "url": card_url,
                "modelMustNotCollectValues": True,
                "flowVersion": "agentbridge.trusted-flow.v1",
            },
            "display": {
                "systemName": "OA",
                "businessView": {
                    "schemaVersion": "agentbridge.business-view.v1",
                    "profileId": "oa.business_trip.approval",
                    "profileVersion": "1",
                    "viewKind": "approval",
                },
            },
            "expiresAt": _future(10),
        }
        result_view = {
            "schemaVersion": "agentbridge.result-view.v1",
            "sourceTaskId": "task-oa-pending-00000001",
            "endpointId": "test-endpoint-501",
            "url": f"{HTTPS_ORIGIN}/results/{'B' * 43}",
            "expiresAt": _future(30),
            "selectionAvailable": True,
            "sourceExpiresAt": _future(10),
        }

        # 1. Running turn with active card: sends typing action + delivers card once with Mini App & browser buttons.
        state["pending"] = True
        state["has_private_card"] = True
        state["card_payload"] = {
            "available": True,
            "url": card_url,
            "title": interaction["title"],
            "message": interaction["message"],
            "interactionId": interaction["interactionId"],
            "interactionType": interaction["type"],
            "cardRevision": 1,
            "interaction": interaction,
        }
        state["notifications"] = [
            {
                "kind": "trusted_interaction",
                "deliveryId": "deliv-card-1",
                "attempt": 1,
                "runId": f"interaction:{interaction['interactionId']}",
                "replyRunId": "run-1",
                "interaction": interaction,
            }
        ]
        state["updates"] = [
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"type": "private", "id": 501},
                    "from": {"id": 501, "is_bot": False},
                    "text": "帮我查 OA 待办",
                },
            }
        ]
        pilot.poll_once()

        assert len(state["received_sources"]) == 1
        assert state["received_sources"][0]["messageId"] == 10
        assert set(state["admissions"]) == {f"tg:{BOT}:501:10"}
        assert len(state["typing_calls"]) >= 1
        assert state["typing_calls"][0] == {"chat_id": 501, "action": "typing"}
        # Even though BOTH reconcile (/v1/card) AND TerminalMessages.poll (notifications) saw the same interaction,
        # telegram_card_deliveries deduplicates it to EXACTLY ONE Telegram message!
        assert len(state["deliveries"]) == 1
        card_msg = state["deliveries"][0]
        assert card_msg["parse_mode"] == "HTML"
        buttons = card_msg["reply_markup"]["inline_keyboard"][0]
        assert buttons[0] == {"text": "核对出差材料", "web_app": {"url": card_url}}
        assert buttons[1] == {"text": EXTERNAL_BROWSER_LABEL, "url": card_url}
        assert any(r["deliveryId"] == "deliv-card-1" and r["delivered"] is True for r in state["receipts"])

        # 2. Turn finishes with Markdown answer + Result View card.
        state["pending"] = False
        state["has_private_card"] = False
        state["has_result_view"] = True
        state["result_view_payload"] = {
            "available": True,
            "resultView": result_view,
            "count": 2,
            "complete": True,
        }
        state["notifications"] = [
            {
                "kind": "result_view",
                "deliveryId": "deliv-rv-1",
                "attempt": 1,
                "runId": f"result_view:{result_view['sourceTaskId']}",
                "resultView": result_view,
                "summary": "已查询最新 OA 待办。",
            }
        ]
        pilot.next_notifications = 0
        pilot.poll_once()

        # Delivery #2: Markdown formatted final answer (HTML); Delivery #3: Result View card (deduped across reconcile & poll).
        assert len(state["deliveries"]) == 3
        answer_msg = state["deliveries"][1]
        assert answer_msg["parse_mode"] == "HTML"
        assert "<b>查询完成</b>" in answer_msg["text"]
        assert "<code>2</code>" in answer_msg["text"]

        rv_msg = state["deliveries"][2]
        assert rv_msg["parse_mode"] == "HTML"
        assert "OA 待办 · 选择与办理" in rv_msg["text"]
        rv_buttons = rv_msg["reply_markup"]["inline_keyboard"][0]
        assert rv_buttons[0] == {"text": "选择并办理 OA 待办", "web_app": {"url": result_view["url"]}}
        assert rv_buttons[1] == {"text": EXTERNAL_BROWSER_LABEL, "url": result_view["url"]}

        # 3. Lost card delivery receipt NEVER resends on subsequent polls.
        state["lost_card_delivery"] = True
        interaction2 = {**interaction, "interactionId": "interaction-rich-00000002"}
        state["notifications"] = [
            {
                "kind": "trusted_interaction",
                "deliveryId": "deliv-card-lost",
                "attempt": 1,
                "runId": f"interaction:{interaction2['interactionId']}",
                "interaction": interaction2,
            }
        ]
        pilot.next_notifications = 0
        pilot.poll_once()
        count_after_lost = len(state["deliveries"])
        assert any(r["deliveryId"] == "deliv-card-lost" and r["delivered"] is False for r in state["receipts"])

        # Retry with connection restored must NOT resend!
        state["lost_card_delivery"] = False
        state["notifications"][0]["attempt"] = 2
        pilot.next_notifications = 0
        pilot.poll_once()
        assert len(state["deliveries"]) == count_after_lost
        pilot.db.close()
    finally:
        server.shutdown()


def test_nanobot_telegram_result_view_prepare_refresh_and_skills_batch2(tmp_path, monkeypatch):
    fixture = LatencyAcceptanceFixture(
        tmp_path, seed=False, latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0)
    )
    try:
        service = fixture.service
        store = McpIdentityTokenStore(service.db_path)
        token = store.issue(user_subject=OWNER, expected_principal_ref=OWNER, ttl_seconds=7200)
        instance_id = "nanobot-batch2-inst"

        # Establish real NanoBot Workspace binding + verified Telegram pairing (exact production path).
        service.workspace.hosts.save_instance(
            instance_id,
            host_type="nanobot",
            display_name="Batch 2 NanoBot",
            route_ref=instance_id,
            adapter_version="0.3.0",
            state="enabled",
            subjects=[OWNER],
            actor="test-admin",
            reason="Batch 2 ResultView test",
            expected_revision=0,
        )
        with service.workspace._connect() as db:
            account_id = db.execute(
                "SELECT account_id FROM workspace_accounts WHERE user_subject=? AND state='active'",
                (OWNER,),
            ).fetchone()["account_id"]
        web, _ = service.tasks.ensure_endpoint(
            user_subject=OWNER,
            token_id=token["token_id"],
            agent_host="nanobot",
            endpoint_key="web-batch2-owner",
            client_type="web",
            external_subject=account_id,
            account_id=account_id,
            conversation_ref="batch2-web-session",
        )
        service.workspace.hosts.bind(
            account_id,
            instance_id,
            endpoint_id=web["endpoint_id"],
            credential_ref="batch2-web-cred",
        )
        routes_file = tmp_path / "batch2-routes.json"
        routes_file.write_text(
            json.dumps(
                {
                    "routes": {
                        instance_id: {
                            "instanceId": instance_id,
                            "telegram": {"botId": str(BOT), "username": "batch2_bot"},
                            "subjects": {OWNER: "batch2-web-cred"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        routes_file.chmod(0o600)
        monkeypatch.setenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", str(routes_file))
        links = TelegramPilotLinks(service)
        pair_req = links.begin(account_id, instance_id, BOT)
        proof = links.present(OWNER, instance_id, BOT, 777, pair_req["code"])
        assert proof
        links.confirm(account_id, instance_id, BOT, pair_req["challenge_id"], proof)
        ensured = links.ensure_endpoint(OWNER, token["token_id"], instance_id, BOT, 777)
        endpoint = service.tasks.get_endpoint(ensured["endpointId"], user_subject=OWNER)
        session_ref = endpoint["conversation_ref"]

        registration = _register_nanobot(service, OWNER, token["token_id"], instance_id)
        service.skills.save(
            "user:" + OWNER,
            {"oa-pending": {"profiles": ["single", "batch"]}},
            expected_revision=0,
            actor="test-admin",
            reason="Batch 2 NanoBot Telegram Skill assignment",
        )
        fixture.oa.add_affair("synthetic-nb-tg-leave", profile="leave")

        # 1. Run OA pending list on NanoBot Telegram endpoint.
        source_task, _ = service.tasks.ensure_task(
            user_subject=OWNER,
            agent_host="nanobot",
            host_task_key=uuid4().hex,
            origin_endpoint_id=endpoint["endpoint_id"],
            active_conversation_ref=session_ref,
            title="NanoBot Telegram OA 待办",
        )
        res = service.invoke(
            user_subject=OWNER,
            capability_name="oa.workflow.pending.list",
            arguments={},
            task_id=source_task["task_id"],
            idempotency_key=uuid4().hex,
        )
        assert res["status"] == "succeeded"
        service.finish_host_task(
            user_subject=OWNER,
            task_id=source_task["task_id"],
            outcome="succeeded",
            reason="done",
        )

        # 2. Issue ResultView ticket for NanoBot Telegram endpoint.
        views = ResultViewService(service)
        issued = views.issue(
            OWNER,
            source_task["task_id"],
            endpoint["endpoint_id"],
            {"endpointId": endpoint["endpoint_id"], "tokenId": token["token_id"]},
        )
        rv = issued["resultView"]
        assert rv["sourceTaskId"] == source_task["task_id"]
        assert rv["presentation"]["endpointId"] == endpoint["endpoint_id"]
        assert rv["selectionAvailable"] is True
        ticket = rv["presentation"]["url"].rsplit("/", 1)[1]

        # 3. Read ticket & queue preparation -> creates agent_host="nanobot" task + coordinator lease + prepared_command!
        page = views.read(ticket)
        assert page["selectionAvailable"] is True
        group = page["groups"][0]
        target_key = group["targets"][0]["targetKey"]
        queued = views.prepare(
            ticket,
            {
                "operationId": group["operationId"],
                "targetKeys": [target_key],
                "requestKey": uuid4().hex,
            },
        )
        prep_task = service.tasks.get_task(queued["taskId"], user_subject=OWNER)
        assert prep_task["agent_host"] == "nanobot"
        lease = service.get_host_coordinator_lease(user_subject=OWNER, task_id=queued["taskId"])
        assert lease is not None
        assert lease["hostInstanceId"] == instance_id
        assert lease["state"] == "active"

        # 4. pending_preparations returns NanoBot Telegram command ONLY to matching instance registration.
        pending_own = actions.pending_preparations(service, OWNER, registration=registration)
        assert any(cmd["taskId"] == queued["taskId"] for cmd in pending_own)
        other_reg = {**registration, "hostInstanceId": "nanobot-other-inst"}
        assert not any(cmd["taskId"] == queued["taskId"] for cmd in actions.pending_preparations(service, OWNER, registration=other_reg))
        oc_reg = {**registration, "agentHost": "openclaw", "hostInstanceId": "openclaw-gw"}
        assert not any(cmd["taskId"] == queued["taskId"] for cmd in actions.pending_preparations(service, OWNER, registration=oc_reg))

        # 5. claim_host_notifications surfaces deliveryMode == "prepared_command" for NanoBot TG!
        claimed = service.claim_host_notifications(
            user_subject=OWNER,
            agent_host="nanobot",
            endpoint_key=endpoint["endpoint_key"],
            include_workspace_chat=True,
        )
        prep_notifs = [n for n in claimed["notifications"] if n["deliveryMode"] == "prepared_command"]
        assert len(prep_notifs) == 1
        assert prep_notifs[0]["task"]["taskId"] == queued["taskId"]
        assert prep_notifs[0]["event"]["payload"]["commandKey"] == queued["commandKey"]

        # 6. Refresh query from ResultView ticket -> creates agent_host="nanobot" oa_pending_query task + lease + prepared_command!
        refreshed = views.refresh(ticket, {"requestKey": uuid4().hex})
        refresh_task = service.tasks.get_task(refreshed["taskId"], user_subject=OWNER)
        assert refresh_task["agent_host"] == "nanobot"
        refresh_lease = service.get_host_coordinator_lease(user_subject=OWNER, task_id=refreshed["taskId"])
        assert refresh_lease is not None
        assert refresh_lease["hostInstanceId"] == instance_id

        claimed2 = service.claim_host_notifications(
            user_subject=OWNER,
            agent_host="nanobot",
            endpoint_key=endpoint["endpoint_key"],
            include_workspace_chat=True,
        )
        assert any(
            n["deliveryMode"] == "prepared_command" and n["task"]["taskId"] == refreshed["taskId"]
            for n in claimed2["notifications"]
        )
    finally:
        fixture.close()


def test_nanobot_mcp_agentbridge_result_view_accepts_nanobot_and_rejects_unknown_host():
    from unittest.mock import patch
    from tests.test_central_mcp import CentralMcpFixture, CentralMcpTests

    with CentralMcpFixture() as (service, store, token, client):
        task_id = "task-1234567890-abcdef"
        params = {"name": "agentbridge_result_view", "arguments": {"operation": "issue", "task_id": task_id}}
        nb_host = {"version": "1", "agentHost": "nanobot", "hostInstanceId": "nanobot-inst-1", "hostVersion": "0.3.0"}
        bad_host = {"version": "1", "agentHost": "custom-host", "hostInstanceId": "custom-1", "hostVersion": "1.0.0"}
        with patch.object(ResultViewService, "issue", return_value={"status": "succeeded", "resultView": {}}) as issue:
            service.register_host(
                user_subject="alice",
                agent_host="nanobot",
                host_instance_id="nanobot-inst-1",
                host_version="0.3.0",
                declared_level="L3",
                capabilities=["host_control", "trusted_interaction"],
            )
            with patch.object(service, "validate_host_call_context", return_value={}):
                ok_resp = CentralMcpTests._request(
                    client,
                    "tools/call",
                    request_id=9101,
                    token=token,
                    params={
                        **params,
                        "_meta": {
                            "io.agentbridge/host-context": nb_host,
                            "io.agentbridge/task": {"taskId": task_id, "endpointId": "endpoint-nb-1"},
                        },
                    },
                )
                assert not ok_resp.json()["result"]["isError"], ok_resp.json()
                issue.assert_called_once()

                bad_resp = CentralMcpTests._request(
                    client,
                    "tools/call",
                    request_id=9102,
                    token=token,
                    params={
                        **params,
                        "_meta": {
                            "io.agentbridge/host-context": bad_host,
                            "io.agentbridge/task": {"taskId": task_id, "endpointId": "endpoint-nb-1"},
                        },
                    },
                )
                assert bad_resp.json()["result"]["isError"]


def test_task_context_records_telegram_pending_query_only_when_nonempty(tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from nanobot_agentbridge.task_context import TaskContext, pending_query_for_run

    def _mcp_result(payload: dict) -> SimpleNamespace:
        return SimpleNamespace(isError=False, meta=None, structuredContent=payload)

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    tg_req = {
        "userDir": str(user_dir),
        "runId": "run-tg-1",
        "endpoint": "ep-tg-1",
        "endpointKey": f"telegram:{BOT}:501",
        "conversation": "agentbridge:telegram:00000000-0000-0000-0000-000000000501",
    }
    ctx = TaskContext(None, tg_req, {})
    ctx.meta = {"io.agentbridge/task": {"taskId": "task-pending-1234567890", "coordinatorLeaseVersion": "3"}}
    with sqlite3.connect(ctx.db) as db:
        db.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("run-tg-1", "ep-tg-1", "task-pending-1234567890", 3))
    # Empty list (count=0) must NOT record a result view trigger.
    ctx.observe_read(
        "oa_workflow_pending_list",
        _mcp_result({"status": "succeeded", "result": {"items": [], "complete": True}}),
    )
    assert pending_query_for_run(user_dir, "run-tg-1", "ep-tg-1") is None

    # Non-empty Telegram pending list records taskId, lease version, count, and completeness.
    ctx.observe_read(
        "oa_workflow_pending_list",
        _mcp_result(
            {
                "status": "succeeded",
                "result": {"items": [{"affairId": "1"}, {"affairId": "2"}], "coverage": {"status": "complete"}},
            }
        ),
    )
    recorded = pending_query_for_run(user_dir, "run-tg-1", "ep-tg-1")
    assert recorded == {
        "taskId": "task-pending-1234567890",
        "version": 3,
        "count": 2,
        "complete": True,
    }
    # Different endpoint cannot read another endpoint's pending query.
    assert pending_query_for_run(user_dir, "run-tg-1", "ep-other") is None

    # Workspace channel does NOT record Telegram pending_queries.
    ws_req = {**tg_req, "runId": "run-ws-1", "endpoint": "ep-ws-1", "endpointKey": "workspace:inst:acc"}
    ws_ctx = TaskContext(None, ws_req, {})
    ws_ctx.meta = {"io.agentbridge/task": {"taskId": "task-ws-1234567890", "coordinatorLeaseVersion": "1"}}
    with sqlite3.connect(ws_ctx.db) as db:
        db.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("run-ws-1", "ep-ws-1", "task-ws-1234567890", 1))
    ws_ctx.observe_read(
        "oa_workflow_pending_list",
        _mcp_result({"status": "succeeded", "result": {"items": [{"affairId": "1"}], "coverage": {"status": "complete"}}}),
    )
    assert pending_query_for_run(user_dir, "run-ws-1", "ep-ws-1") is None


def test_notifications_handler_dispatches_prepared_command_waiting_card_result_view_and_filters_quiet_status(
    tmp_path, monkeypatch
):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from nanobot_agentbridge.notifications import notifications as handle_notifications

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    ep_id = "ep-tg-501"
    conv = "agentbridge:telegram:00000000-0000-0000-0000-000000000501"
    card_url = f"{HTTPS_ORIGIN}/flow/input/tok-abcdef0123456789"
    rv_url = f"{HTTPS_ORIGIN}/results/{'C' * 43}"
    d_prep = "00000000-0000-4000-8000-00000000d001"
    d_quiet = "00000000-0000-4000-8000-00000000d002"
    d_wait = "00000000-0000-4000-8000-00000000d003"
    d_query = "00000000-0000-4000-8000-00000000d004"

    claimed_items = [
        # 1. prepared_command -> dispatched to host.commands.accept and acked
        {
            "deliveryId": d_prep,
            "attemptCount": 1,
            "deliveryMode": "prepared_command",
            "task": {
                "taskId": "00000000-0000-4000-8000-000000000101",
                "userSubject": "alice",
                "agentHost": "nanobot",
                "originEndpointId": ep_id,
                "activeConversationRef": conv,
            },
            "event": {
                "eventType": "task.prepare.requested",
                "payload": {"commandKey": "00000000-0000-4000-8000-000000000202"},
            },
        },
        # 2. quiet companion status -> acked silently without Telegram message
        {
            "deliveryId": d_quiet,
            "attemptCount": 1,
            "deliveryMode": "status",
            "task": {"taskId": "task-ws-1", "userSubject": "alice", "agentHost": "nanobot"},
            "event": {"eventType": "task.interaction.completed", "payload": {}},
            "message": "交互已完成",
        },
        # 3. origin_handled task.interaction.waiting for TG command -> presents card
        {
            "deliveryId": d_wait,
            "attemptCount": 1,
            "deliveryMode": "origin_handled",
            "task": {
                "taskId": "00000000-0000-4000-8000-000000000101",
                "userSubject": "alice",
                "agentHost": "nanobot",
                "originEndpointId": ep_id,
                "activeConversationRef": conv,
                "summary": {"command": {"originKind": "telegram", "kind": "oa_pending_prepare"}},
            },
            "event": {
                "eventType": "task.interaction.waiting",
                "payload": {"interactionId": "interaction-cmd-00000001"},
            },
        },
        # 4. origin_handled task.completed for oa_pending_query -> issues result_view
        {
            "deliveryId": d_query,
            "attemptCount": 1,
            "deliveryMode": "origin_handled",
            "task": {
                "taskId": "00000000-0000-4000-8000-000000000303",
                "userSubject": "alice",
                "agentHost": "nanobot",
                "originEndpointId": ep_id,
                "activeConversationRef": conv,
                "status": "succeeded",
                "summary": {"command": {"originKind": "telegram", "kind": "oa_pending_query"}},
            },
            "event": {"eventType": "task.completed", "payload": {}},
        },
    ]

    acked = []
    accepted_cmds = []

    class FakeSession:
        async def call_tool(self, name, args, meta=None):
            if name == "agentbridge_host_notification_claim":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={"endpoint": {"endpointId": ep_id}, "notifications": claimed_items},
                )
            if name == "agentbridge_host_notification_ack":
                acked.append(args["delivery_id"])
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "delivery": {"deliveryId": args["delivery_id"], "attemptCount": args["attempt_count"]}
                    },
                )
            if name == "agentbridge_host_interaction_present":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "endpoint": {"endpointId": ep_id},
                        "interaction": {
                            "schemaVersion": "agentbridge.interaction.v1",
                            "interactionId": args["interaction_id"],
                            "type": "business_input",
                            "state": "pending",
                            "title": "核对请假材料",
                            "message": "请打开核对。",
                            "presentation": {
                                "owner": "agentbridge",
                                "url": card_url,
                                "modelMustNotCollectValues": True,
                                "flowVersion": "agentbridge.trusted-flow.v1",
                            },
                            "display": {},
                            "expiresAt": _future(10),
                        },
                    },
                )
            if name == "agentbridge_host_coordinator_lease_get":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={"coordinatorLease": {"hostInstanceId": "nb-inst-1", "version": 2}},
                )
            if name == "agentbridge_result_view":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "resultView": {
                            "schemaVersion": "agentbridge.result-view.v1",
                            "sourceTaskId": args["task_id"],
                            "expiresAt": _future(30),
                            "selectionAvailable": True,
                            "sourceExpiresAt": _future(10),
                            "presentation": {
                                "owner": "agentbridge",
                                "endpointId": ep_id,
                                "url": rv_url,
                                "modelMustNotCollectValues": True,
                            },
                        }
                    },
                )
            raise AssertionError(f"Unexpected tool call: {name}")

    class FakeCommands:
        async def accept(self, record, body):
            accepted_cmds.append(body)
            return {"accepted": True}

    host = SimpleNamespace(
        coordinator=True,
        card_origins=[HTTPS_ORIGIN],
        commands=FakeCommands(),
        worker_payload=lambda rec, _run: {
            "instance": "nb-inst-1",
            "subject": "alice",
            "endpoint": ep_id,
            "endpointKey": f"telegram:{BOT}:501",
            "conversation": conv,
            "workspaceEndpointId": "ws-ep-1",
            "userDir": str(user_dir),
            "trustedCardOrigins": [HTTPS_ORIGIN],
        },
    )
    record = {"workspaceEndpointId": "ws-ep-1", "credential_ref": "synthetic-original-pair"}

    @asynccontextmanager
    async def fake_auth(_req):
        yield FakeSession(), {}, "L3"

    async def fake_verify(*_args, **_kwargs):
        return True

    fake_web = SimpleNamespace(
        HTTPForbidden=type("HTTPForbidden", (Exception,), {}),
        HTTPBadRequest=type("HTTPBadRequest", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=fake_web))
    monkeypatch.setitem(sys.modules, "nanobot_agentbridge.worker", SimpleNamespace(authenticated_session=fake_auth))
    monkeypatch.setattr("nanobot_agentbridge.telegram_identity.verify_telegram_binding", fake_verify)

    result = asyncio.run(handle_notifications(host, record, "telegram-notifications", {}))
    assert accepted_cmds == [
        {
            "taskId": "00000000-0000-4000-8000-000000000101",
            "commandKey": "00000000-0000-4000-8000-000000000202",
        }
    ]
    assert d_prep in acked
    assert d_quiet in acked
    kinds = [d["kind"] for d in result["deliveries"]]
    assert kinds == ["trusted_interaction", "result_view"]
    assert result["deliveries"][0]["interaction"]["interactionId"] == "interaction-cmd-00000001"
    assert result["deliveries"][1]["resultView"]["url"] == rv_url


def test_recovery_reconciles_waiting_coordinated_task_on_completed_chat_run(tmp_path, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    import sqlite3
    from types import SimpleNamespace

    from nanobot_agentbridge.runs import RunCoordinator

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(mode=0o700)
    runs_dir.chmod(0o700)

    ep_id = "ep-tg-501"
    conv = "agentbridge:telegram:00000000-0000-0000-0000-000000000501"
    task_id = "9add7759-0f2f-4d9a-8546-d5b0e45b2b60"
    batch_id = "90f84f1d-2a65-4520-ac98-360cd9bfc2e0"
    runs = RunCoordinator(runs_dir)

    async def finish_run():
        entry = runs.accept(
            subject="alice",
            endpoint=ep_id,
            conversation=conv,
            request_key="tg:8001:501:31",
            payload_digest="digest-1",
            execute=lambda _rid, _emit: asyncio.sleep(0, result="可信办理卡仍在等待你的确认。"),
        )
        await asyncio.sleep(0.05)
        return entry["run_id"]

    run_id = asyncio.run(finish_run())
    assert runs.snapshot(run_id, subject="alice", endpoint=ep_id, conversation=conv)["state"] == "completed"

    with sqlite3.connect(user_dir / "coordinated-private.sqlite3") as db:
        db.execute(
            "CREATE TABLE coordinated (run TEXT PRIMARY KEY, endpoint TEXT NOT NULL, central_task TEXT NOT NULL, local_task TEXT, interaction TEXT, kind TEXT, phase TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE write_scope (run TEXT PRIMARY KEY, prepare TEXT NOT NULL, expected_commit TEXT NOT NULL, argument_digest TEXT NOT NULL)"
        )
        db.execute("CREATE TABLE batch_scope (run TEXT PRIMARY KEY, batch TEXT NOT NULL)")
        db.execute(
            "INSERT INTO coordinated VALUES (?,?,?,?,?,?,?)",
            (run_id, ep_id, task_id, "local-1", "inter-1", "business_input", "waiting"),
        )
        db.execute(
            "INSERT INTO write_scope VALUES (?,?,?,?)",
            (run_id, "oa_workflow_pending_batch_prepare", "oa.missed_punch.approve", "arg-digest"),
        )
        db.execute("INSERT INTO batch_scope VALUES (?,?)", (run_id, batch_id))
    with sqlite3.connect(user_dir / "coordinated-runs.sqlite3") as db:
        db.execute(
            "CREATE TABLE tasks (run_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, version INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO tasks VALUES (?,?,?,?)", (run_id, ep_id, task_id, 1))

    batch_verified = [False]
    spawned = []

    class FakeSession:
        async def call_tool(self, name, args, meta=None):
            assert name == "agentbridge_host_task_snapshot"
            if batch_verified[0]:
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "task": {"taskId": task_id, "userSubject": "alice", "originEndpointId": ep_id},
                        "batchVerification": {
                            "taskId": task_id,
                            "batchId": batch_id,
                            "state": "succeeded",
                            "allVerified": True,
                            "totalCount": 2,
                            "verifiedCount": 2,
                        },
                    },
                )
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "task": {"taskId": task_id, "userSubject": "alice", "originEndpointId": ep_id, "status": "waiting_user"},
                    "batchVerification": {"allVerified": False},
                },
            )

    @asynccontextmanager
    async def fake_auth(_req):
        yield FakeSession(), {}, "L3"

    class FakeProc:
        returncode = 0

        async def communicate(self, data):
            spawned.append(json.loads(data.decode()))
            return b'{"outcome":"waiting"}', b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*_args, **_kwargs):
        return FakeProc()

    host = SimpleNamespace(
        runs=runs,
        coordinator="/fake/coordinator",
        worker_environment=lambda _d: {},
        worker_payload=lambda rec, rid: {
            "userDir": str(user_dir),
            "controlledPrepares": {
                "oa_workflow_pending_batch_prepare": {"commitCapability": "oa.missed_punch.approve"}
            },
        },
    )
    record = {
        "user_subject": "alice",
        "endpoint_id": ep_id,
        "endpoint_key": f"telegram:{BOT}:501",
        "session_key": conv,
    }

    monkeypatch.setitem(sys.modules, "nanobot_agentbridge.worker", SimpleNamespace(authenticated_session=fake_auth))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    from nanobot_agentbridge.recovery import reconcile as run_reconcile

    # 1. Completed chat run with phase='waiting' MUST reopen recoveryOnly coordinator monitoring!
    asyncio.run(run_reconcile(host, record, run_id, task_id, "waiting"))
    assert len(spawned) == 1
    assert spawned[0]["recoveryOnly"] is True
    assert spawned[0]["task"]["taskId"] == task_id

    # 2. Once central verifies the batch, reconcile updates coordinated.phase='verified' and does NOT spawn again.
    batch_verified[0] = True
    asyncio.run(run_reconcile(host, record, run_id, task_id, "waiting"))
    assert len(spawned) == 1
    with sqlite3.connect(user_dir / "coordinated-private.sqlite3") as db:
        assert db.execute("SELECT phase FROM coordinated WHERE run=?", (run_id,)).fetchone()[0] == "verified"

    # 3. Terminal non-waiting phase on a completed run MUST NOT spawn coordinator.
    asyncio.run(run_reconcile(host, record, run_id, task_id, "expired"))
    assert len(spawned) == 1
    runs._db.close()
    runs._lock.close()

