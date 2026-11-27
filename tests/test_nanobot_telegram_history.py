import json

import pytest

from agentbridge.core.chat_mirror import public_mirror_text
from tests.support.nanobot_telegram_loopback import fixture as transport, update
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_telegram_pairing import pair_fixture


def test_confirmed_turn_retries_only_its_history_after_restart(tmp_path):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state.update(updates=[update(10, text="首轮合成问答")], lost_timeline=True, lost_accept=True)
        pilot.poll_once()
        assert list(state["timeline"].values()) == ["首轮合成问答"]
        assert len(state["admissions"]) == len(state["deliveries"]) == 1
        pilot.db.close()

        pilot = create()
        state.update(updates=[update(11, text="首轮合成问答")], lost_timeline=False)
        with pilot.db:
            pilot.db.execute("UPDATE telegram_chat_projection SET next_attempt=0")
        pilot.poll_once()
        assert list(state["timeline"].values()) == ["首轮合成问答", "合成结果 alpha"]
        assert list(key[-1] for key in state["timeline"]) == ["user", "assistant"]
        assert not pilot.db.execute("SELECT 1 FROM telegram_chat_projection").fetchone()
        assert len(state["admissions"]) == len(state["deliveries"]) == 1
        pilot.db.close()


def test_unknown_send_keeps_host_answer_but_commands_are_not_chat_answers(tmp_path):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state.update(lost_delivery=True, updates=[update(1), update(2, 502, text="/start")])
        pilot.poll_once()
        assert list(state["timeline"].values()) == ["查询我的待办", "合成结果 alpha"]
        state.update(lost_delivery=False, updates=[update(3), update(4, 502, text="/start")])
        pilot.poll_once()
        assert len(state["deliveries"]) == 2 and len(state["admissions"]) == 1
        assert len(state["timeline"]) == 2
        pilot.db.close()


def test_revocation_before_projection_discards_original_pending_copy(tmp_path):
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        state.update(pending=True, updates=[update(1)])
        pilot.ingest(state["updates"][0])
        state.update(verified=set(), pending=False)
        pilot.reconcile()
        assert not state["timeline"] and not state["deliveries"]
        assert not pilot.db.execute("SELECT 1 FROM telegram_chat_projection").fetchone()
        pilot.db.close()


@pytest.mark.parametrize("text", [
    "/start " + "a" * 32, "https://fixture.invalid/results/" + "x" * 32,
    "填写 https://fixture.invalid/flow/input/private", "密码：模拟凭据", "Bearer example_only",
    "https://fixture.invalid/resource?token=simulated", "确认码 12345678",
])
def test_private_material_excluded_before_channel_storage(text):
    assert public_mirror_text(text) is None


def test_pasted_private_link_never_enters_the_model_or_projection(tmp_path):
    with transport(tmp_path) as (state, _, create):
        private = "https://fixture.invalid/flow/authorize/synthetic-private"
        pilot = create()
        state.update(updates=[update(1, text=private)])
        pilot.poll_once()
        assert not state["admissions"] and not state["timeline"]
        assert [row["text"] for row in state["deliveries"]] == ["请在本人工作台的可信界面填写受保护的信息。"]
        assert not pilot.db.execute("SELECT 1 FROM telegram_chat_projection").fetchone()
        pilot.db.close()


def test_length_limit_counts_utf16_and_does_not_forward_private_or_malformed_answers():
    from nanobot_agentbridge.telegram_timeline import telegram_answer, telegram_text_fits
    emoji = "😀" * 2048
    assert telegram_text_fits(emoji) and telegram_answer(emoji) == emoji
    assert not telegram_text_fits(emoji + "a") and telegram_answer(emoji + "a") == emoji + "a"
    assert telegram_answer("文" * 50_001) == "文" * 50_001
    assert "原对话" in telegram_answer("文" * 200_001)
    assert not telegram_text_fits("\ud800") and public_mirror_text("\ud800") is None
    assert "private" not in telegram_answer("https://fixture.invalid/auth/private")


@pytest.mark.parametrize("lose_second", [True, False])
def test_long_original_reply_publishes_independently_of_platform_receipts(tmp_path, lose_second):
    with transport(tmp_path) as (state, _, create):
        expected = "文" * 4095 + "😀" + "末尾" * 2200
        state.update(final_answer=expected, updates=[update(1)],
                     lost_delivery_filter=lambda body: lose_second and len(state["deliveries"]) == 2)
        pilot = create()
        pilot.poll_once()
        pilot.db.close()

        pilot = create()
        state.update(updates=[update(2)], lost_delivery_filter=lambda _: False)
        pilot.poll_once()
        texts = [item["text"] for item in state["deliveries"]]
        assert all(len(text.encode("utf-16-le")) <= 8192 for text in texts)
        assert len(state["admissions"]) == 1
        rows = list(state["timeline"].values())
        if lose_second:
            assert pilot._record(501,9)["state"] == "delivery_unknown"
            assert len(texts) == 2 and rows == ["查询我的待办", expected]
        else:
            assert len(texts) == 4 and "".join(texts) == expected
            assert rows == ["查询我的待办", expected]
            published = [body for path, body in state["calls"] if path == "/v1/telegram-timeline" and body["role"] == "assistant"]
            assert len(published) == 1 and published[0]["deliveredMessageIds"] == [701, 702, 703, 704]
            assert published[0]["projectionMode"] == "host_result_v2"
        pilot.db.close()


def test_resume_only_unsent_segments_after_identity_service_returns(tmp_path, monkeypatch):
    from nanobot_agentbridge.telegram import TelegramUnavailable
    with transport(tmp_path) as (state, _, create):
        pilot = create()
        answer = "首" * 4096 + "次段已核对"
        state.update(updates=[update(1)], final_answer=answer)
        live_binding = pilot._binding
        def temporarily_offline(chat):
            if state["deliveries"]:
                raise TelegramUnavailable()
            return live_binding(chat)
        monkeypatch.setattr(pilot, "_binding", temporarily_offline)
        pilot.poll_once()
        assert pilot._record(501, 9)["state"] == "working"
        assert [d["text"] for d in state["deliveries"]] == ["首" * 2700]
        pilot.db.close()
        pilot = create()
        with pilot.db:
            pilot.db.execute("UPDATE telegram_chat_projection SET next_attempt=0")
        pilot.poll_once()
        assert [d["text"] for d in state["deliveries"]] == ["首" * 2700, "首" * 1396 + "次段已核对"]
        assert list(state["timeline"].values()) == ["查询我的待办", answer]
        assert len(state["admissions"]) == 1
        pilot.db.close()


def test_central_paired_source_preserves_metadata_and_never_echoes(pair_fixture, tmp_path, monkeypatch):
    fixture, links, accounts = pair_fixture
    a, b = fixture.subjects.values()
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"routes": {INSTANCE: {"instanceId": INSTANCE,
        "telegram": {"botId": "8001", "username": "fixture_bot"},
        "subjects": {a: "tg-fixture-web-cred-a", b: "tg-fixture-web-cred-b"}}}}))
    routes.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", str(routes))
    end = links.ensure_endpoint(a, fixture.tokens["a"], INSTANCE, "8001", "501")
    args = dict(user_subject=a, token_id=fixture.tokens["a"], agent_host="nanobot",
                host_instance_id=INSTANCE, endpoint_key=end["endpointKey"], client_type="telegram",
                external_subject="501", conversation_ref=end["conversationRef"], account_id="8001",
                route={"channel": "telegram", "to": "501", "accountId": "8001"})
    account = fixture.service.workspace.get_account(accounts[a])
    account["_workspace_view_instance"] = INSTANCE
    def endpoint():
        return fixture.service.tasks.endpoint_for_key(user_subject=a, agent_host="nanobot", endpoint_key=end["endpointKey"])
    original = endpoint()
    for role in ("user", "assistant"):
        source = dict(args, message_key="synthetic-key-" + role, role=role, text="合成消息 " + role)
        for _ in range(2):
            result = fixture.service.append_host_timeline_message(**source)
            assert result["endpoint"]["endpointId"] == end["endpointId"]
    from tests.test_workspace_query_projection import register
    register(fixture.service, a, "nanobot", INSTANCE, fixture.tokens["a"])
    upgraded = fixture.service.append_host_timeline_message(**source, projection_v2=True)
    assert upgraded["reused"]["entry"]
    assert upgraded["projectionReceipt"]["sourceEndpointId"] == end["endpointId"]
    assert upgraded["projectionReceipt"]["entryId"] == result["entry"]["entryId"]
    with pytest.raises(ValueError):
        fixture.service.append_host_timeline_message(**{**source,"text":"同键异载荷"},projection_v2=True)
    assert endpoint() == original
    def visible(owner, selected):
        view = {**owner, "_workspace_view_instance": selected}
        ids = fixture.service.workspace.visible_chat_endpoints(view)
        rows = fixture.service.tasks.list_timeline(user_subject=owner["user_subject"], entry_type="chat_message", chat_endpoints=ids)
        return [row["role"] for row in reversed(rows) if row["source_endpoint_id"] == end["endpointId"]]
    assert sorted(visible(account, INSTANCE)) == ["assistant", "user"]
    assert not visible(account, "openclaw-default")
    assert not visible(fixture.service.workspace.get_account(accounts[b]), INSTANCE)
    for different in ({"host_instance_id":"other"}, {"user_subject": b}, {"token_id": fixture.tokens["b"]},
                      {"endpoint_key":"telegram:8001:888"}, {"conversation_ref":"different"}, {"route":{}}):
        with pytest.raises(PermissionError):
            fixture.service.append_host_timeline_message(**{**source, **different})
    with fixture.service.workspace._connect() as db:
        assert [tuple(row) for row in db.execute("SELECT role,source_endpoint_id FROM user_timeline WHERE source_endpoint_id=? ORDER BY sequence",
                    (end["endpointId"],))] == [(role,end["endpointId"]) for role in ("user","assistant")]
        assert not db.execute("SELECT 1 FROM notification_outbox WHERE payload_type='timeline_message'").fetchone()
