"""Regression tests for Batch 2 Central state-machine, protocol fence, and Workspace fixes."""
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core import cross_endpoint_reference as cross_ref
from agentbridge.core import native_telegram_card_state as card_state
from agentbridge.core import telegram_notification_delivery as notify_v2
from agentbridge.core import telegram_timeline_projection as tg_history
from agentbridge.core import workspace_media_delivery as media_delivery
from agentbridge.core import workspace_query_delivery as query_delivery
from agentbridge.core import workspace_text_delivery as text_delivery
from agentbridge.core.commit_notifications import current_version
from agentbridge.core.telegram_result_views import ResultViewService
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.telegram_pair_migration import PROTOCOL_META
from tests.support.latency_acceptance import OWNER
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_text_delivery import admit, control, text_claim


def test_text_delivery_overdue_sending_and_expired_part_unblock_subsequent_answer(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    # Case 1: overdue sending on user question transitions to unknown and allows assistant reply.
    dispatch = admit(fixture)
    long_answer = "答复内容" * 1200  # Multi-part assistant reply (>4000 chars)
    service.tasks.append_workspace_response(
        user_subject=OWNER, source_endpoint_id=dispatch["origin_endpoint_id"],
        message_key="workspace:assistant:" + dispatch["idempotency_key"], text=long_answer,
    )
    q_item, = text_claim(service, endpoint, token)
    assert control(service, endpoint, token, q_item, "issue")["available"]
    assert control(service, endpoint, token, q_item, "begin")["allowed"]
    # begin must extend due by at least ~120s so an in-flight send is not swept immediately.
    with service.tasks._connect() as db:
        due = actions._date(db.execute(
            "SELECT due FROM workspace_text_deliveries WHERE delivery_id=?", (q_item["deliveryId"],)
        ).fetchone()[0])
        assert due > actions._now() + timedelta(seconds=90)
        # Simulate host crash past grace period.
        db.execute("UPDATE workspace_text_deliveries SET due=? WHERE delivery_id=?",
                   ((actions._now() - timedelta(seconds=1)).isoformat(), q_item["deliveryId"]))
    next_items = text_claim(service, endpoint, token)
    assert len(next_items) == 1 and next_items[0]["sourceEntryId"] != q_item["sourceEntryId"]
    with service.tasks._connect() as db:
        row = db.execute("SELECT state, outcome FROM workspace_text_deliveries WHERE delivery_id=?",
                         (q_item["deliveryId"],)).fetchone()
        assert tuple(row) == ("unknown", "delivery_unknown")

    # Case 2: multi-part entry expiring at attempt>=5 blocks its later parts so a later status is not deadlocked.
    part1 = next_items[0]
    with service.tasks._connect() as db:
        db.execute("UPDATE workspace_text_deliveries SET state='retry_wait', attempt=5, due=? WHERE delivery_id=?",
                   ((actions._now() - timedelta(seconds=1)).isoformat(), part1["deliveryId"]))
    assert not text_claim(service, endpoint, token)
    with service.tasks._connect() as db:
        states = [r[0] for r in db.execute(
            "SELECT state FROM workspace_text_deliveries WHERE entry_id=? ORDER BY part_index",
            (part1["sourceEntryId"],),
        )]
        assert states[0] == "expired"
        assert all(s == "blocked" for s in states[1:])


def test_text_delivery_late_ack_does_not_unblock_when_earlier_part_is_not_sent(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch = admit(fixture)
    q_item, = text_claim(service, endpoint, token)
    assert control(service, endpoint, token, q_item, "issue")["available"]
    assert control(service, endpoint, token, q_item, "begin")["allowed"]
    assert control(service, endpoint, token, q_item, "ack", outcome="sent", receipt=10)["recorded"]
    long_answer = "分段测试" * 2600  # >=3 parts
    service.tasks.append_workspace_response(
        user_subject=OWNER, source_endpoint_id=dispatch["origin_endpoint_id"],
        message_key="workspace:assistant:" + dispatch["idempotency_key"], text=long_answer,
    )
    with service.tasks._connect() as db:
        rows = db.execute(
            "SELECT delivery_id, part_index FROM workspace_text_deliveries WHERE kind='assistant' ORDER BY part_index"
        ).fetchall()
        assert len(rows) >= 3
        # Force part 1 rejected, part 2 unknown with attempt=1, part 3 blocked.
        db.execute("UPDATE workspace_text_deliveries SET state='rejected', outcome='rejected', attempt=1 WHERE delivery_id=?",
                   (rows[0]["delivery_id"],))
        db.execute("UPDATE workspace_text_deliveries SET state='unknown', outcome='delivery_unknown', attempt=1 WHERE delivery_id=?",
                   (rows[1]["delivery_id"],))
        db.execute("UPDATE workspace_text_deliveries SET state='blocked', attempt=0 WHERE delivery_id=?",
                   (rows[2]["delivery_id"],))
        pair_proof = db.execute("SELECT pair_proof FROM workspace_query_destinations LIMIT 1").fetchone()[0]
    res = text_delivery.handle(
        service, subject=OWNER, host="openclaw", instance="openclaw-gateway",
        endpoint_key=endpoint["endpoint_key"], token_id=token, pairing_ref=pair_proof,
        delivery_id=rows[1]["delivery_id"], attempt_count=1, action="ack", outcome="sent", receipt=99,
    )
    assert res == {"recorded": True}
    with service.tasks._connect() as db:
        assert db.execute("SELECT state FROM workspace_text_deliveries WHERE delivery_id=?",
                          (rows[2]["delivery_id"],)).fetchone()[0] == "blocked"


def test_query_and_notification_overdue_sending_and_expired_coverage(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    # Activate v2 notifications.
    service.claim_host_notifications(
        user_subject=OWNER, agent_host="openclaw", endpoint_key=endpoint["endpoint_key"],
        host_instance_id="openclaw-gateway", notification_token_id=token,
    )
    dispatch = admit(fixture)
    q_item, = text_claim(service, endpoint, token)
    for action, kw in (("issue", {}), ("begin", {}), ("ack", {"outcome": "sent", "receipt": 1})):
        control(service, endpoint, token, q_item, action, **kw)
    task, _ = service.tasks.ensure_task(
        user_subject=OWNER, agent_host="openclaw", host_task_key=dispatch["idempotency_key"],
        origin_endpoint_id=dispatch["origin_endpoint_id"], active_conversation_ref=dispatch["conversation_ref"],
        title="待办查询",
    )
    op, _ = service.operations.create(
        user_subject=OWNER, capability_name=query_delivery.CAPABILITY, capability_version="1", input_summary={},
    )
    done = service.operations.mark_succeeded(op["operation_id"], {"items": [], "coverage": {"status": "complete", "hasMore": False}})
    service.tasks.link_operation(task_id=task["task_id"], user_subject=OWNER, operation=done)
    service.tasks.complete_task(task_id=task["task_id"], user_subject=OWNER, reason="done")

    # Expire query_delivery by attempt>=5; task.completed notification must NOT be covered/suppressed.
    with service.tasks._connect() as db:
        qrow = db.execute("SELECT delivery_id FROM workspace_query_deliveries WHERE task_id=?", (task["task_id"],)).fetchone()
        assert qrow is not None
        db.execute("UPDATE workspace_query_deliveries SET state='retry_wait', attempt=5 WHERE delivery_id=?", (qrow[0],))
    claimed = [
        n for n in service.claim_host_notifications(
            user_subject=OWNER, agent_host="openclaw", endpoint_key=endpoint["endpoint_key"],
            host_instance_id="openclaw-gateway", notification_token_id=token,
        )["notifications"]
        if n["deliveryMode"] == notify_v2.MODE
    ]
    assert len(claimed) == 1 and claimed[0]["sourceTaskId"] == task["task_id"]
    item = claimed[0]
    scope = dict(subject=OWNER, host="openclaw", instance="openclaw-gateway", token_id=token,
                 endpoint_key=endpoint["endpoint_key"], pairing_ref=item["pairingRef"],
                 delivery_id=item["deliveryId"], attempt_count=item["attemptCount"])
    assert notify_v2.handle(service, **scope, action="issue")["available"]
    assert notify_v2.handle(service, **scope, action="begin")["allowed"]
    with service.tasks._connect() as db:
        due = actions._date(db.execute(
            "SELECT due FROM telegram_notification_deliveries WHERE delivery_id=?", (item["deliveryId"],)
        ).fetchone()[0])
        assert due > actions._now() + timedelta(seconds=90)
        db.execute("UPDATE telegram_notification_deliveries SET due=? WHERE delivery_id=?",
                   ((actions._now() - timedelta(seconds=1)).isoformat(), item["deliveryId"]))
    assert not notify_v2.claim(service, OWNER, "openclaw", "openclaw-gateway", endpoint["endpoint_key"], token, 10, 30)
    with service.tasks._connect() as db:
        assert tuple(db.execute(
            "SELECT state, outcome FROM telegram_notification_deliveries WHERE delivery_id=?", (item["deliveryId"],)
        ).fetchone()) == ("unknown", "delivery_unknown")


def test_status_delivery_resolves_by_telegram_receipt(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch, _ = service.workspace.accept_chat_request(
        account_id=fixture.account["account_id"], user_subject=OWNER,
        message="请列出我的待办记录", idempotency_key=uuid4().hex, attachments=[],
        tasks=service.tasks, attachment_store=service.timeline_attachments,
        media_base_url="https://fixture.invalid",
    )
    claim = service.workspace.claim_next_host_dispatch(account_id=fixture.account["account_id"], claim_owner="fixture")
    service.workspace.mark_host_dispatch_accepted(
        dispatch["dispatch_id"], claim_token=claim["claim_token"], run_id=dispatch["idempotency_key"],
    )
    q_item, = text_claim(service, endpoint, token)
    for action, kw in (("issue", {}), ("begin", {}), ("ack", {"outcome": "sent", "receipt": 11})):
        control(service, endpoint, token, q_item, action, **kw)
    service.workspace.finish_host_dispatch(
        dispatch["dispatch_id"], claim_token=claim["claim_token"], state="failed",
        event={"type": "chat", "state": "error", "text": "host failed"}, error_code="HOST_FAILED",
    )
    status_item, = text_claim(service, endpoint, token)
    for action, kw in (("issue", {}), ("begin", {}), ("ack", {"outcome": "sent", "receipt": 12})):
        control(service, endpoint, token, status_item, action, **kw)
    resolved = cross_ref.resolve(
        service, subject=OWNER, host="openclaw", instance="openclaw-gateway", token=token,
        endpoint_key=endpoint["endpoint_key"], reference={"telegramReceipt": 12},
    )
    assert resolved["decision"] == "selected"
    assert resolved["entries"][0]["role"] == "status"
    assert resolved["entries"][0]["entryId"] == dispatch["dispatch_id"]


def test_workspace_application_referenceable_filters_expired_and_revoked(openclaw):
    fixture, token, tg = openclaw
    service = fixture.service
    app = WorkspaceApplication(service=service, gateway=SimpleNamespace(readiness=lambda: {"available": True}))
    body = dict(
        user_subject=OWNER, token_id=token, agent_host="openclaw", host_instance_id="openclaw-gateway",
        endpoint_key=tg["endpoint_key"], client_type="telegram", external_subject="501",
        account_id=None, conversation_ref=tg["conversation_ref"], route={"channel": "telegram", "to": "501"},
        role="user", projection_v2=True,
    )
    fresh_id = service.append_host_timeline_message(**body, message_key="tg:ref:fresh", text="近期消息")["entry"]["entryId"]
    old_id = service.append_host_timeline_message(**body, message_key="tg:ref:old", text="过期消息")["entry"]["entryId"]
    with service.tasks._connect() as db:
        db.execute("UPDATE user_timeline SET created_at=? WHERE entry_id=?",
                   ((actions._now() - timedelta(hours=25)).isoformat(), old_id))
    entries = {item["entry_id"]: item for item in app.list_timeline(fixture.account)}
    assert entries[fresh_id].get("referenceable") is True
    assert not entries[old_id].get("referenceable")
    # Rotating pairing authority immediately removes referenceable from historical messages even while visible.
    with service.tasks._connect() as db:
        db.execute("UPDATE telegram_history_receipts SET authority=? WHERE entry_id=?", ("0" * 64, fresh_id))
    entries_after = {item["entry_id"]: item for item in app.list_timeline(fixture.account)}
    assert fresh_id in entries_after
    assert not entries_after[fresh_id].get("referenceable")


def _register_nanobot_l3(service, owner, token_id):
    from pathlib import Path
    with service.tasks._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO agent_host_compatibility VALUES('nanobot',?,'L3','synthetic','test',CURRENT_TIMESTAMP)",
            (query_delivery.VERSIONS["nanobot"],),
        )
    vec = json.loads((Path(__file__).resolve().parents[1] / "schemas/agent-host/v1/test-vectors.json").read_text())["profiles"][0]["value"]
    service.negotiate_host(
        user_subject=owner, token_id=token_id,
        profile={**vec, "hostInstanceId": INSTANCE, "implementation": {"name": "nanobot", "version": query_delivery.VERSIONS["nanobot"]}},
    )
    return service.require_host_registration(
        user_subject=owner, agent_host="nanobot", host_instance_id=INSTANCE,
        host_version=query_delivery.VERSIONS["nanobot"], minimum_level="L3",
    )


def test_non_public_nanobot_question_still_pins_paired_for_public_reply(mirror, pair_fixture):
    service, links, accounts, owner, _, ask, _ = mirror
    fixture = pair_fixture[0]
    token_id = fixture.tokens["a"]
    _register_nanobot_l3(service, owner, token_id)
    links.ensure_endpoint(owner, token_id, INSTANCE, 8001, 501)
    with service.workspace._connect() as db:
        binding = db.execute(
            "SELECT * FROM workspace_host_bindings WHERE account_id=? AND instance_id=?",
            (accounts[owner], INSTANCE),
        ).fetchone()
    # Question contains an 8-digit date -> legacy _enqueue_private_bot_chat skips it,
    # but workspace_query_destinations MUST still pin selection='paired' for the assistant reply.
    run_id = uuid4().hex
    dispatch, _ = ask(owner, run_id, "请帮我核对 20261128 的公开摘要")
    claim = service.workspace.claim_next_host_dispatch(account_id=accounts[owner], claim_owner="fixture")
    service.workspace.mark_host_dispatch_accepted(
        dispatch["dispatch_id"], claim_token=claim["claim_token"], run_id=run_id,
    )
    service.tasks.append_workspace_response(
        user_subject=owner, source_endpoint_id=binding["endpoint_id"],
        message_key="workspace:assistant:" + run_id,
        text="这是一条干净的公开回复",
    )
    with service.tasks._connect() as db:
        pin = db.execute("SELECT selection FROM workspace_query_destinations WHERE source=?",
                         (binding["endpoint_id"],)).fetchone()
        assert pin and pin["selection"] == "paired"
        reply_row = db.execute("SELECT state FROM workspace_text_deliveries WHERE kind='assistant'").fetchone()
        assert reply_row and reply_row["state"] == "pending"


def test_revoked_nanobot_pairing_blocked_across_present_state_validate_context_and_result_view(mirror, pair_fixture):
    service, links, accounts, owner, _, _, _ = mirror
    fixture = pair_fixture[0]
    token_id = fixture.tokens["a"]
    bot, sender = "8001", "501"
    endpoint_key = f"telegram:{bot}:{sender}"
    reg = _register_nanobot_l3(service, owner, token_id)
    ensured = links.ensure_endpoint(owner, token_id, INSTANCE, bot, sender)
    tg_ep = service.tasks.endpoint_for_key(user_subject=owner, agent_host="nanobot", endpoint_key=endpoint_key)
    assert ensured["endpointId"] == tg_ep["endpoint_id"]
    task, _ = service.tasks.ensure_task(
        user_subject=owner, agent_host="nanobot", host_task_key="revoked-fence-task",
        origin_endpoint_id=tg_ep["endpoint_id"], active_conversation_ref=tg_ep["conversation_ref"], title="测试",
    )
    interaction_id = "interaction-" + "1" * 16
    # Revoke NanoBot Telegram pairing!
    links.revoke(accounts[owner], INSTANCE, bot)

    with pytest.raises(PermissionError):
        service.present_interaction(
            user_subject=owner, agent_host="nanobot", endpoint_key=endpoint_key,
            interaction_id=interaction_id, host_instance_id=INSTANCE,
            token_id=token_id, pairing_protocol=None,
        )
    with pytest.raises((PermissionError, actions.ActionableResultError)):
        card_state.read_state(
            service, subject=owner, token=token_id, registration=reg,
            host_instance=INSTANCE, host="nanobot", endpoint_key=endpoint_key,
            interaction_id=interaction_id, pairing_protocol=None,
        )
    with pytest.raises(PermissionError):
        service.get_host_cross_endpoint_context(
            user_subject=owner, agent_host="nanobot", endpoint_key=endpoint_key,
            host_instance_id=INSTANCE, token_id=token_id, pairing_protocol=None,
        )
    with pytest.raises(actions.ActionableResultError):
        ResultViewService(service).issue(
            owner, task["task_id"], tg_ep["endpoint_id"],
            {"endpointId": tg_ep["endpoint_id"], "tokenId": token_id,
             "hostInstanceId": INSTANCE, "pairingProtocol": None},
        )
    # Re-pair explicitly and mark independent v2: an active pairing MUST STILL reject v1 callers (pairing_protocol=None)!
    ch = links.begin(accounts[owner], INSTANCE, bot)
    proof = links.present(owner, INSTANCE, bot, sender, ch["code"])
    links.confirm(accounts[owner], INSTANCE, bot, ch["challenge_id"], proof)
    with service.tasks._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version=? WHERE instance_id=?",
                   (query_delivery.VERSIONS["nanobot"], INSTANCE))
        db.execute("INSERT OR REPLACE INTO workspace_telegram_independent_pairs VALUES(?,?,?,2)",
                   (INSTANCE, bot, owner))
    with pytest.raises(PermissionError, match="protocol"):
        service.present_interaction(
            user_subject=owner, agent_host="nanobot", endpoint_key=endpoint_key,
            interaction_id=interaction_id, host_instance_id=INSTANCE,
            token_id=token_id, pairing_protocol=None,
        )
    with pytest.raises(PermissionError, match="protocol"):
        card_state.read_state(
            service, subject=owner, token=token_id, registration=reg,
            host_instance=INSTANCE, host="nanobot", endpoint_key=endpoint_key,
            interaction_id=interaction_id, pairing_protocol=None,
        )
    with pytest.raises(PermissionError, match="protocol"):
        service.get_host_cross_endpoint_context(
            user_subject=owner, agent_host="nanobot", endpoint_key=endpoint_key,
            host_instance_id=INSTANCE, token_id=token_id, pairing_protocol=None,
        )
    with pytest.raises(actions.ActionableResultError):
        ResultViewService(service).issue(
            owner, task["task_id"], tg_ep["endpoint_id"],
            {"endpointId": tg_ep["endpoint_id"], "tokenId": token_id,
             "hostInstanceId": INSTANCE, "pairingProtocol": None},
        )


def test_timeline_projection_normalizes_utc_z_received_at(mirror, pair_fixture):
    service, links, accounts, owner, _, _, _ = mirror
    fixture = pair_fixture[0]
    token_id = fixture.tokens["a"]
    bot, sender = "8001", "501"
    endpoint_key = f"telegram:{bot}:{sender}"
    _register_nanobot_l3(service, owner, token_id)
    # Create explicit pilot link (generationScoped=True) by revoking inherited and pairing explicitly.
    links.revoke(accounts[owner], INSTANCE, bot)
    ch = links.begin(accounts[owner], INSTANCE, bot)
    proof = links.present(owner, INSTANCE, bot, sender, ch["code"])
    links.confirm(accounts[owner], INSTANCE, bot, ch["challenge_id"], proof)
    links.ensure_endpoint(owner, token_id, INSTANCE, bot, sender)
    tg_ep = service.tasks.endpoint_for_key(user_subject=owner, agent_host="nanobot", endpoint_key=endpoint_key)
    ts_dt = (actions._now() + timedelta(seconds=5)).replace(microsecond=0)
    ts_z = ts_dt.isoformat().replace("+00:00", "Z")
    from agentbridge.core import telegram_media_receipts as media_receipts
    src = media_receipts.source(
        service, subject=owner, host="nanobot", instance=INSTANCE, token=token_id,
        endpoint_key=tg_ep["endpoint_key"], source_message_id=777, received_at=ts_z,
    )
    assert src["recorded"] is True
    appended = tg_history.append(
        service, subject=owner, host="nanobot", instance=INSTANCE, token=token_id,
        endpoint_key=tg_ep["endpoint_key"], sender=sender, account=bot,
        conversation=tg_ep["conversation_ref"],
        route={"channel": "telegram", "accountId": bot, "to": sender},
        message_key="tg:z-time:777", role="user", text="带 Z 后缀的时间戳消息",
        task_id=None, source_message_id=777, received_at=ts_z, pairing_ref=src["pairingRef"],
    )
    assert appended["status"] == "succeeded"


def test_scripts_malformed_version_and_offline_route_transition_fail_closed():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/native"))
    import acceptance
    import nanobot_release

    with pytest.raises(ValueError, match="Invalid AgentBridge plugin version"):
        acceptance.required_runtime_hooks("0.4.135-beta")
    with pytest.raises(ValueError, match="Invalid AgentBridge plugin version"):
        acceptance.required_runtime_hooks("")
    pub = SimpleNamespace(release="aaaaaaaaaaaa")
    spec = {
        "instanceId": "nanobot-office",
        "subjects": {"a": "c1", "b": "c2"},
        "adapterVersion": "0.3.1",
        "routeTransition": {"toRelease": "bbbbbbbbbbbb"},
    }
    with pytest.raises(ValueError, match="different publication candidate"):
        nanobot_release.observability(pub, spec, offline=True)
