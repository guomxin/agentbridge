"""Only newly accepted public chat may reach the account's own deployed private Bot."""

import json
from uuid import uuid4

import pytest

from agentbridge.core.tasks import TaskNotFound
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401


@pytest.fixture
def mirror(pair_fixture, tmp_path, monkeypatch):  # noqa: F811 -- imported pytest fixture
    fixture, links, accounts = pair_fixture
    a, b = fixture.subjects.values()
    route = tmp_path / "routes.json"

    def deploy(bot):
        route.write_text(
            json.dumps(
                {
                    "routes": {
                        INSTANCE: {
                            "instanceId": INSTANCE,
                            "telegram": {"botId": bot},
                            "subjects": {
                                a: "tg-fixture-web-cred-a",
                                b: "tg-fixture-web-cred-b",
                            },
                        }
                    }
                }
            )
        )
        route.chmod(0o600)

    deploy("8001")
    monkeypatch.setenv("AGENTBRIDGE_NANOBOT_ROUTES_FILE", str(route))
    service, workspace = fixture.service, fixture.service.workspace
    for account in accounts.values():
        workspace.hosts.set_default(
            account,
            INSTANCE,
            expected_revision=workspace.hosts.choices(account)["revision"],
        )

    def ask(subject, key, text):
        return workspace.accept_chat_request(
            account_id=accounts[subject],
            user_subject=subject,
            message=text,
            idempotency_key=key,
            attachments=[],
            tasks=service.tasks,
            attachment_store=service.timeline_attachments,
            media_base_url="https://test.invalid",
            instance_id=INSTANCE,
        )

    return service, links, accounts, a, b, ask, deploy


def test_first_web_question_creates_only_verified_endpoint_and_old_consumer_does_not_claim(
    mirror,
):
    service, links, accounts, a, b, ask, _ = mirror
    workspace, store = service.workspace, service.tasks
    assert links.mirror_peers(a, INSTANCE, 8001) == {"senderIds": ["501"]}
    assert links.mirror_peers(b, INSTANCE, 8001) == {"senderIds": []}
    assert links.mirror_peers(a, "some-other-instance", 8001) == {"senderIds": []}
    assert links.mirror_peers(a, INSTANCE, 8002) == {"senderIds": []}
    dispatch, reused = ask(a, "stable-question", "今天星期几？")
    assert not reused and ask(a, "stable-question", "今天星期几？")[1]
    ask(b, "other-account", "只在自己的网页显示")
    web = workspace.current_host(accounts[a])
    claimed = workspace.claim_next_host_dispatch(
        account_id=accounts[a], claim_owner="test"
    )
    assert claimed["dispatch_id"] == dispatch["dispatch_id"]
    workspace.mark_host_dispatch_accepted(
        dispatch["dispatch_id"], claim_token=claimed["claim_token"], run_id="answer-run"
    )
    store.append_workspace_response(
        user_subject=a,
        source_endpoint_id=web["endpoint_id"],
        message_key="workspace:assistant:answer-run",
        text="星期一。",
    )
    kwargs = {
        "user_subject": a,
        "agent_host": "nanobot",
        "endpoint_key": "telegram:8001:501",
        "host_instance_id": INSTANCE,
    }
    endpoint = store.endpoint_for_key(
        **{k: v for k, v in kwargs.items() if k != "host_instance_id"}
    )
    assert "timeline_message" in endpoint["capabilities"]
    assert service.claim_host_notifications(**kwargs)["notifications"] == []
    assert [
        r["attempt_count"]
        for r in store.list_outbox(user_subject=a, endpoint_id=endpoint["endpoint_id"])
    ] == [0, 0]
    rows = service.claim_host_notifications(**kwargs, include_workspace_chat=True)[
        "notifications"
    ]
    assert [(r["timeline"]["role"], r["timeline"]["text"]) for r in rows] == [
        ("user", "今天星期几？"),
        ("assistant", "星期一。"),
    ]
    assert all(
        r["timeline"]["source"]
        == {"endpointId": web["endpoint_id"], "clientType": "web"}
        and not r["attachments"]
        and not r["interaction"]
        and r["task"] is None
        for r in rows
    )
    assert store.list_outbox(user_subject=b) == []
    with pytest.raises(TaskNotFound):
        service.claim_host_notifications(
            **{**kwargs, "host_instance_id": "other"}, include_workspace_chat=True
        )
    with pytest.raises(TaskNotFound):
        service.claim_host_notifications(
            **{**kwargs, "user_subject": b}, include_workspace_chat=True
        )
    for row in rows:
        attempt = {
            "delivery_id": row["deliveryId"],
            "attempt_count": row["attemptCount"],
        }
        assert service.validate_host_notification(**kwargs, **attempt) == {
            "allowed": True
        }
        service.acknowledge_host_notification(**kwargs, **attempt, succeeded=True)
    assert not service.claim_host_notifications(**kwargs, include_workspace_chat=True)[
        "notifications"
    ]


def test_current_route_checked_again_after_claim_and_before_send(mirror):
    service, links, _, a, _, ask, deploy = mirror
    ask(a, "own", "一条普通问题")
    args = {
        "user_subject": a,
        "agent_host": "nanobot",
        "endpoint_key": "telegram:8001:501",
        "host_instance_id": INSTANCE,
    }
    (item,) = service.claim_host_notifications(**args, include_workspace_chat=True)[
        "notifications"
    ]
    attempt = {"delivery_id": item["deliveryId"], "attempt_count": item["attemptCount"]}
    assert service.validate_host_notification(**args, **attempt)["allowed"]
    deploy("8002")
    assert not links.mirror_peers(a, INSTANCE, 8001)["senderIds"]
    assert not service.validate_host_notification(**args, **attempt)["allowed"]
    deploy("8001")
    with service.workspace._connect() as db:
        db.execute(
            "UPDATE client_endpoints SET state='revoked' WHERE agent_host='openclaw' AND client_type='telegram'"
        )
    assert not links.mirror_peers(a, INSTANCE, 8001)["senderIds"]
    assert not service.validate_host_notification(**args, **attempt)["allowed"]


def test_no_history_replay_no_fake_web_source_no_private_output(mirror):
    service, _, accounts, a, _, ask, _ = mirror
    workspace, store = service.workspace, service.tasks
    web = workspace.current_host(accounts[a])
    base = {"user_subject": a, "source_endpoint_id": web["endpoint_id"]}
    # Public MCP timeline ingestion has no authority to assert local Workspace dispatch provenance.
    store.append_timeline_message(
        **base, role="user", message_key="workspace:user:made-up", text="合成历史"
    )
    for text in [
        "https://test.invalid/flow/input/synthetic-card",
        "Bearer synthetic-token",
    ]:
        ask(a, str(uuid4()), text)
    assert not store.list_outbox(user_subject=a)
    ask(a, "new", "新消息")
    endpoint = store.endpoint_for_key(
        user_subject=a, agent_host="nanobot", endpoint_key="telegram:8001:501"
    )
    store.append_timeline_message(
        user_subject=a,
        source_endpoint_id=endpoint["endpoint_id"],
        role="assistant",
        message_key="workspace:assistant:pretend",
        text="来自电报",
    )
    with workspace._connect() as db:
        dispatch = db.execute(
            "SELECT dispatch_id FROM agent_host_dispatches WHERE idempotency_key='new'"
        ).fetchone()[0]
        db.execute(
            "UPDATE agent_host_dispatches SET accepted_run_id='card-run' WHERE dispatch_id=?",
            (dispatch,),
        )
        db.execute(
            """INSERT INTO agent_host_dispatch_events(event_id,dispatch_id,event_name,payload_json,created_at)
            VALUES(?,?,'host_progress','{"kind":"private_interaction","status":"closed"}',CURRENT_TIMESTAMP)""",
            (str(uuid4()), dispatch),
        )
    store.append_workspace_response(
        **base, message_key="workspace:assistant:card-run", text="卡片处理完成"
    )
    assert [r["payload"]["text"] for r in store.list_outbox(user_subject=a)] == [
        "新消息"
    ]


def test_revoked_and_repaired_same_sender_does_not_receive_old_grant(mirror):
    service, links, accounts, _, b, ask, _ = mirror

    def pair():
        request = links.begin(accounts[b], INSTANCE, 8001)
        confirmation = links.present(b, INSTANCE, 8001, 509, request["code"])
        links.confirm(
            accounts[b], INSTANCE, 8001, request["challenge_id"], confirmation
        )

    pair()
    ask(b, "before", "上次配对期间的私聊")
    links.revoke(accounts[b], INSTANCE, 8001)
    pair()
    ask(b, "after", "本次配对期间的私聊")
    rows = service.claim_host_notifications(
        user_subject=b,
        agent_host="nanobot",
        endpoint_key="telegram:8001:509",
        host_instance_id=INSTANCE,
        include_workspace_chat=True,
    )["notifications"]
    assert [row["timeline"]["text"] for row in rows] == ["本次配对期间的私聊"]
    assert "pairProof" not in rows[0]["timeline"]


def test_same_user_with_two_bot_instances_keeps_their_web_chats_separate(mirror):
    import os
    from pathlib import Path

    service, links, accounts, a, _, ask, _ = mirror
    workspace, store = service.workspace, service.tasks
    other = "second-nanobot"
    workspace.hosts.save_instance(
        other,
        host_type="nanobot",
        display_name="Second synthetic Bot",
        route_ref=other,
        adapter_version="0.3.0",
        state="enabled",
        subjects=[a],
        expected_revision=0,
        actor="fixture",
        reason="second instance",
    )
    web, _ = store.ensure_endpoint(
        user_subject=a,
        token_id="test-web",
        agent_host="nanobot",
        endpoint_key="web-second-nanobot",
        client_type="web",
        external_subject=accounts[a],
        account_id=accounts[a],
        conversation_ref="session-second-nanobot",
    )
    workspace.hosts.bind(
        accounts[a],
        other,
        endpoint_id=web["endpoint_id"],
        credential_ref="second-credential",
    )
    config = Path(os.environ["AGENTBRIDGE_NANOBOT_ROUTES_FILE"])
    document = json.loads(config.read_text())
    document["routes"][other] = {
        "instanceId": other,
        "telegram": {"botId": "8002"},
        "subjects": {a: "second-credential"},
    }
    config.write_text(json.dumps(document))
    assert links.mirror_peers(a, other, 8002)["senderIds"] == ["501"]
    ask(a, "first-bot", "第一个实例问候")
    workspace.hosts.set_default(
        accounts[a],
        other,
        expected_revision=workspace.hosts.choices(accounts[a])["revision"],
    )
    workspace.accept_chat_request(
        account_id=accounts[a],
        user_subject=a,
        message="第二个实例问候",
        idempotency_key="second-bot",
        attachments=[],
        tasks=store,
        attachment_store=service.timeline_attachments,
        media_base_url="https://test.invalid",
        instance_id=other,
    )
    for bot, instance, expected in (
        (8001, INSTANCE, "第一个实例问候"),
        (8002, other, "第二个实例问候"),
    ):
        rows = service.claim_host_notifications(
            user_subject=a,
            agent_host="nanobot",
            endpoint_key=f"telegram:{bot}:501",
            host_instance_id=instance,
            include_workspace_chat=True,
        )["notifications"]
        assert [row["timeline"]["text"] for row in rows] == [expected]


def test_two_distinct_legacy_private_senders_disable_automatic_mirror(mirror):
    service, links, _, a, _, ask, _ = mirror
    source = service.tasks.endpoint_for_key(
        user_subject=a, agent_host="openclaw", endpoint_key="test-old-private-501"
    )
    ask(a, "old-route", "路由发生变化前的消息")
    claim_args = {
        "user_subject": a,
        "agent_host": "nanobot",
        "endpoint_key": "telegram:8001:501",
        "host_instance_id": INSTANCE,
    }
    (held,) = service.claim_host_notifications(
        **claim_args, include_workspace_chat=True
    )["notifications"]
    service.tasks.ensure_endpoint(
        user_subject=a,
        token_id=source["token_id"],
        agent_host="openclaw",
        endpoint_key="another-legacy-sender",
        client_type="telegram",
        external_subject="999",
        conversation_ref="agent:main:telegram:direct:999",
        route={"channel": "telegram", "to": "999", "accountId": "default"},
    )
    assert not links.mirror_peers(a, INSTANCE, 8001)["senderIds"]
    assert not service.validate_host_notification(
        **claim_args, delivery_id=held["deliveryId"], attempt_count=held["attemptCount"]
    )["allowed"]
    ask(a, "ambiguous-route", "路由不唯一时的消息")
    assert len(service.tasks.list_outbox(user_subject=a)) == 1


@pytest.mark.parametrize("also_workspace", [True, False])
def test_explicit_nanobot_digest_is_scoped_and_rechecked_before_send(
    mirror, pair_fixture, monkeypatch, also_workspace,
):
    from datetime import datetime
    from agentbridge.core import oa_daily_digest as digest
    from agentbridge.core import tasks
    from agentbridge.core.oa_daily_digest_report import DigestReport
    from agentbridge.core.business_watches import BusinessWatchError

    service, links, accounts, owner, other, _, deploy = mirror
    target = links.ensure_endpoint(owner, pair_fixture[0].tokens["a"], INSTANCE, 8001, 501)
    account = service.workspace.get_account(accounts[owner])
    settings = service.oa_digest_settings
    default = settings.get(account)
    choices = {option["id"]: option["label"] for option in default["telegramOptions"]}
    assert "NanoBot" in choices[target["endpointId"]]
    assert default["telegramEndpointId"] is None and not default["enabled"]
    body = {"enabled": True, "time": "08:30", "workspace": also_workspace,
            "telegramEndpointId": target["endpointId"], "expectedVersion": 0}
    with pytest.raises(BusinessWatchError):
        settings.update(service.workspace.get_account(accounts[other]), body)

    saved = settings.update(account, body)
    now = datetime.fromisoformat(saved["nextRunAt"])
    settings.clock = lambda: now
    monkeypatch.setattr(tasks, "_utc_now", lambda: now.isoformat())
    monkeypatch.setattr(digest, "collect_oa_daily_digest", lambda *a, **kw: DigestReport("OA 日报 合成条目", True))
    claimed = settings._claim_due(now)
    result = digest.publish_scheduled_oa_digest(service, claimed, now=now, clock=settings._now)
    assert result["status"] == "published" and bool(result["timelineEntryId"]) == also_workspace
    assert digest.publish_scheduled_oa_digest(service, claimed, now=now, clock=settings._now)["status"] == "reused"
    queued, = service.tasks.list_outbox(user_subject=owner)
    assert queued["endpoint_id"] == target["endpointId"]
    assert not service.tasks.list_outbox(user_subject=other)
    claim_scope = dict(user_subject=owner, agent_host="nanobot", endpoint_key=target["endpointKey"], host_instance_id=INSTANCE)
    assert not service.claim_host_notifications(**claim_scope)["notifications"]
    event, = service.claim_host_notifications(**claim_scope, include_workspace_chat=True)["notifications"]
    assert event["timeline"]["text"] == "OA 日报 合成条目"
    assert event["timeline"]["source"]["endpointId"] == target["workspaceEndpointId"]
    assert not {"pairProof", "targetEndpointBinding"} & event["timeline"].keys()
    receipt = dict(**claim_scope, delivery_id=event["deliveryId"], attempt_count=event["attemptCount"])
    assert service.validate_host_notification(**receipt)["allowed"]
    if also_workspace:
        settings.update(account, {**body, "expectedVersion": saved["version"], "enabled": False})
    else:
        deploy("8002")
        assert not settings.get(account)["telegramAvailable"]
    assert not service.validate_host_notification(**receipt)["allowed"]
