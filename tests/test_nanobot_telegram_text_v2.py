"""Separate authoritative history from bounded, recoverable Telegram delivery."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from tests.support.nanobot_telegram_loopback import fixture as transport, update
from nanobot_agentbridge.telegram import TelegramRejected, TelegramUnavailable
from nanobot_agentbridge.telegram_timeline import publish_timeline, telegram_answer, terminal_answer


def test_explicit_429_resumes_only_frozen_unsent_part_after_restart(tmp_path, monkeypatch):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state.update(updates=[update(1)], final_answer="首" * 2700 + "尾😀")
        call = pilot.api.call
        def limited(method, **params):
            if method == "sendMessage" and state["deliveries"]:
                raise TelegramRejected(429, 7)
            return call(method, **params)
        monkeypatch.setattr(pilot.api, "call", limited)
        pilot.poll_once()
        assert len(state["deliveries"]) == 1
        assert list(state["timeline"].values())[-1] == state["final_answer"]
        pending = pilot.db.execute("SELECT * FROM telegram_direct_reply_attempts WHERE part=1").fetchone()
        assert pending["state"] == "retry_wait" and pending["attempt"] == 1
        pilot.db.close()
        pilot = create()
        monkeypatch.setattr(pilot.api, "call", call)
        pilot.poll_once()
        assert len(state["deliveries"]) == 1  # Durable retry_after survives restart.
        with pilot.db:
            pilot.db.execute("UPDATE telegram_direct_reply_attempts SET not_before=0")
        import nanobot_agentbridge.telegram_direct_replies as replies
        monkeypatch.setattr(replies, "format_ordinary_telegram_message", lambda _: pytest.fail("Frozen parts must not be reformatted"))
        pilot.poll_once()
        assert [item["text"] for item in state["deliveries"]] == ["首" * 2700, "尾😀"]
        assert len(state["admissions"]) == 1 and pilot._record(501, 9)["state"] == "delivered"
        pilot.db.close()


@pytest.mark.parametrize("failure, expected", [(TelegramRejected(400), "rejected"), (TelegramUnavailable(), "delivery_unknown")])
def test_rejected_and_unknown_are_distinct_and_neither_replays(tmp_path, monkeypatch, failure, expected):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state["updates"] = [update(1)]
        original = pilot.api.call
        calls = []
        def fail(method, **params):
            if method == "sendMessage":
                calls.append(params)
                raise failure
            return original(method, **params)
        monkeypatch.setattr(pilot.api, "call", fail)
        pilot.poll_once()
        assert pilot._record(501, 9)["state"] == expected
        assert list(state["timeline"].values()) == ["查询我的待办", "合成结果 alpha"]
        pilot.db.close()
        pilot = create()
        pilot.poll_once()
        assert len(calls) == 1 and not state["deliveries"]
        pilot.db.close()


def test_crash_after_begin_without_receipt_is_unknown_after_restart(tmp_path, monkeypatch):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state["updates"] = [update(1)]
        original = pilot.api.call
        def crash(method, **params):
            result = original(method, **params)
            if method == "sendMessage":
                raise SystemExit("simulated process death")
            return result
        monkeypatch.setattr(pilot.api, "call", crash)
        with pytest.raises(SystemExit):
            pilot.poll_once()
        pilot.db.close()
        pilot = create()
        pilot.poll_once()
        assert len(state["deliveries"]) == 1
        assert pilot._record(501, 9)["state"] == "delivery_unknown"
        assert list(state["timeline"].values())[-1] == "合成结果 alpha"
        pilot.db.close()


def test_received_prompt_survives_unknown_model_admission_without_redispatch(tmp_path, monkeypatch):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state["updates"] = [update(1)]
        calls = []
        def fail(*args):
            calls.append(args)
            raise RuntimeError("admission acknowledgement unavailable")
        monkeypatch.setattr(pilot.gateway, "send_dispatch", fail)
        monkeypatch.setattr(pilot.gateway, "dispatch_evidence", lambda *_: {"prompt_observed": False})
        pilot.poll_once()
        pilot.poll_once()
        assert list(state["timeline"].values()) == ["查询我的待办"]
        assert len(calls) == 1 and not state["admissions"] and not state["deliveries"]
        assert pilot._record(501, 9)["state"] == "admission_unknown"
        body = next(body for path, body in state["calls"] if path == "/v1/telegram-timeline")
        assert body["projectionMode"] == "telegram_received_v2" and body["runId"] is None
        pilot.db.close()


@pytest.mark.parametrize("status", ["stopped", "unknown"])
def test_terminal_state_projects_safe_explanation_once(tmp_path, monkeypatch, status):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state["updates"] = [update(1)]
        monkeypatch.setattr(pilot.gateway, "dispatch_evidence", lambda *_: {
            "prompt_observed": True, "observed_run_id": "run-1", "state": status,
            "final_text": "Bearer synthetic-secret-never-display"})
        pilot.poll_once()
        pilot.poll_once()
        assert list(state["timeline"].values()) == ["查询我的待办", terminal_answer(status)]
        assert [item["text"] for item in state["deliveries"]] == [terminal_answer(status)]
        assert len(state["admissions"]) == 1
        pilot.db.close()


def test_long_answer_uses_shared_unicode_limit_and_retains_full_projection(tmp_path):
    answer = "😀" * 50_001
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state.update(updates=[update(1)], final_answer=answer)
        pilot.poll_once()
        assert "".join(item["text"] for item in state["deliveries"]) == answer
        assert all(len(item["text"].encode("utf-16-le")) <= 5400 for item in state["deliveries"])
        assert list(state["timeline"].values())[-1] == answer
        pilot.db.close()


def test_legacy_working_and_sending_messages_do_not_acquire_new_protocol(tmp_path):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state.update(updates=[update(1)], pending=True)
        pilot.ingest(state["updates"][0])
        with pilot.db:
            pilot.db.execute("DELETE FROM telegram_direct_replies")
            pilot.db.execute("UPDATE messages SET state='sending'")
        pilot.db.close()
        pilot = create()
        state["pending"] = False
        pilot.poll_once()
        assert pilot._record(501, 9)["state"] == "delivery_unknown"
        assert not pilot.db.execute("SELECT 1 FROM telegram_direct_replies").fetchone()
        assert not state["deliveries"]
        pilot.db.close()


def test_binding_outage_before_send_never_reserves_card(tmp_path, monkeypatch):
    with transport(tmp_path) as (_, _, create):
        pilot = create()
        binding, digest = pilot._binding(501)
        def offline(_):
            raise TelegramUnavailable()
        monkeypatch.setattr(pilot, "_binding", offline)
        with pytest.raises(TelegramUnavailable):
            pilot._deliver_card_once(501, 9, binding, digest, "interaction:test", {"text": "合成提示"})
        assert not pilot.db.execute("SELECT 1 FROM telegram_card_deliveries").fetchone()
        pilot.db.close()


def test_binding_outage_before_terminal_notification_never_reserves_send(tmp_path, monkeypatch):
    with transport(tmp_path, allow_pairing=True) as (state, _, create):
        pilot = create()
        binding, digest = pilot._binding(501)
        def offline(_):
            raise TelegramUnavailable()
        monkeypatch.setattr(pilot, "_binding", offline)
        event = {"deliveryId":"synthetic-delivery", "runId":"synthetic-run", "attempt":1,"status":"succeeded"}
        with pytest.raises(TelegramUnavailable):
            pilot.terminal._deliver_terminal_event(501,binding,digest,event,9)
        assert not pilot.db.execute("SELECT 1 FROM notification_receipts").fetchone()
        assert not state["deliveries"]
        pilot.db.close()


@pytest.fixture
def timeline_host(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    entry = {"run_id": "original-run", "state": "completed", "answer": "合成公开答复"}
    state = {"entry": entry, "verified": True, "calls": [], "host_type": "nanobot"}
    runs = SimpleNamespace(_db=db, lookup=lambda *_, **__: state["entry"], matches_message=lambda _, text: text == "合成问题")
    host = SimpleNamespace(runs=runs, instance="synthetic-instance", worker_payload=lambda *_: {"hostType": state["host_type"]})
    record = {"endpoint_key": "telegram:8001:501", "endpoint_id": "synthetic-endpoint",
              "user_subject": "synthetic-owner", "session_key": "synthetic-conversation", "credential_ref": "original-generation"}
    class Session:
        async def call_tool(self, name, body, **_):
            if name == "agentbridge_host_telegram_endpoint":
                return SimpleNamespace(isError=False, structuredContent={"endpointId":record["endpoint_id"],
                    "endpointKey":record["endpoint_key"], "conversationRef":record["session_key"],
                    "pairingRef":"1"*64, "generationScoped":False, "receptionFloor":None})
            state["calls"].append((name, body))
            entry = {"entryId":"synthetic-entry","role":body["role"], "text":body["text"]}
            if state.get("history_pruned"):
                entry.pop("text")
                entry["retained"] = False
            return SimpleNamespace(isError=False, structuredContent={"status":"succeeded",
                "endpoint":{"endpointId":record["endpoint_id"]}, "entry":entry,
                "projectionReceipt":{"entryId":"synthetic-entry","messageKey":body["message_key"],
                    "sourceEndpointId":record["endpoint_id"],"role":body["role"],
                    "textSha256":state.get("override_digest", hashlib.sha256(body["text"].encode()).hexdigest())}})
    @asynccontextmanager
    async def session(*_, **__):
        yield Session(), {}, "L3"
    async def verified(*_, **__):
        return state["verified"]
    monkeypatch.setattr("nanobot_agentbridge.session_pool.open_host_session", session)
    monkeypatch.setattr("nanobot_agentbridge.telegram_identity.verify_telegram_binding", verified)
    yield host, record, state
    db.close()


@pytest.mark.parametrize("host_type", ["nanobot", "pi"])
def test_host_projection_proof_is_original_terminal_not_bot_receipt(timeline_host, host_type):
    from aiohttp import web
    host, record, state = timeline_host
    state["host_type"] = host_type
    body = {"messageId":9, "role":"assistant", "text":"合成公开答复", "runId":"original-run",
            "deliveredMessageIds":[], "projectionMode":"host_result_v2", "sourceState":"completed"}
    assert asyncio.run(publish_timeline(host, record, body)) == {"recorded":True}
    assert state["calls"][-1][1]["agent_host"] == host_type
    for changed in ({"runId":"other-run"}, {"text":"改写答复"}, {"sourceState":"unknown"}, {"projectionMode":None}):
        with pytest.raises(web.HTTPForbidden):
            asyncio.run(publish_timeline(host, record, {**body, **changed}))


def test_received_fact_requires_current_pair_and_immutable_original_scope(timeline_host):
    from aiohttp import web
    host, record, state = timeline_host
    state["entry"] = None
    body = {"messageId":9,"role":"user","text":"合成问题","runId":None,"deliveredMessageIds":[],
            "projectionMode":"telegram_received_v2"}
    state["verified"] = False
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(publish_timeline(host, record, body))
    assert not host.runs._db.execute("SELECT name FROM sqlite_master WHERE name='telegram_received_sources'").fetchone()
    state["verified"] = True
    assert asyncio.run(publish_timeline(host, record, body)) == {"recorded":True}
    assert asyncio.run(publish_timeline(host, record, body)) == {"recorded":True}
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(publish_timeline(host, record, {**body,"text":"另一条文本"}))
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(publish_timeline(host, {**record,"credential_ref":"replacement-pair"}, body))
    assert host.runs._db.execute("SELECT COUNT(*) FROM telegram_received_sources").fetchone()[0] == 1


def test_unknown_result_never_accepts_raw_provider_error(timeline_host):
    from aiohttp import web
    host, record, state = timeline_host
    state["entry"].update(state="unknown", answer="private provider failure")
    body = {"messageId":9,"role":"assistant","text":terminal_answer("unknown"),"runId":"original-run",
            "deliveredMessageIds":[],"projectionMode":"host_result_v2","sourceState":"unknown"}
    assert asyncio.run(publish_timeline(host, record, body))["recorded"]
    with pytest.raises(web.HTTPForbidden):
        asyncio.run(publish_timeline(host, record, {**body,"text":"private provider failure"}))
    assert "原对话" in telegram_answer("x" * 200_001)


def test_pruned_history_can_confirm_exact_original_receipt_without_recreating_text(timeline_host):
    from aiohttp import web
    host, record, state = timeline_host
    state["history_pruned"] = True
    body = {"messageId":9,"role":"assistant","text":"合成公开答复","runId":"original-run",
            "deliveredMessageIds":[],"projectionMode":"host_result_v2","sourceState":"completed"}
    assert asyncio.run(publish_timeline(host,record,body)) == {"recorded":True}
    assert state["calls"][-1][1]["projection_v2"] is True
    state["override_digest"] = "0" * 64
    with pytest.raises(web.HTTPServiceUnavailable):
        asyncio.run(publish_timeline(host,record,body))
