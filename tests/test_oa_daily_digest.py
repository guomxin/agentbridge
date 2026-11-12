"""Publication uses real account, endpoint, timeline and outbox stores."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Barrier

import pytest

from agentbridge.core import oa_daily_digest as digest
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.oa_daily_digest_report import DigestReport
from agentbridge.core.tasks import TaskIntegrityError
from tests.authorization_fixtures import authorized_service, grant_permissions
from tests.test_workspace import _create_account


@pytest.fixture
def context(tmp_path, monkeypatch):
    key = tmp_path / "session.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(key))
    service = authorized_service(home=tmp_path, base_url="https://oa.example.test/seeyon/")
    identities = McpIdentityTokenStore(service.db_path)
    for subject in ("user-a", "user-b"):
        _create_account(service, user_subject=subject, username=f"workspace-{subject}",
                        endpoint_key=f"enrollment:{subject}")

    def telegram(subject, sender, *, key=None, route_to=None):
        token = identities.issue(user_subject=subject, expected_principal_ref=subject)
        return service.tasks.ensure_endpoint(
            user_subject=subject, token_id=token["token_id"], agent_host="openclaw",
            endpoint_key=key or f"telegram:default:{sender}", client_type="telegram",
            external_subject=sender, account_id="default",
            conversation_ref=f"agent:main:telegram:direct:{sender}",
            route={"channel": "telegram", "to": route_to or sender, "accountId": "default"},
            capabilities=["direct_status"],
        )[0]

    target = telegram("user-a", "1001")
    companion = telegram("user-a", "1002")
    other_user = telegram("user-b", "2001")
    service.tasks.ensure_endpoint(
        user_subject="user-a", token_id="synthetic-wechat-token", agent_host="openclaw",
        endpoint_key="wechat:user-a", client_type="openclaw-weixin", external_subject="wechat-user-a",
        conversation_ref="agent:main:openclaw-weixin:direct:wechat-user-a", capabilities=["direct_status"],
    )
    clock = [datetime(2026, 11, 11, 16, 30, tzinfo=timezone.utc)]
    monkeypatch.setattr(digest, "_now", lambda: clock[0])
    calls = []

    def collect(_service, **kwargs):
        calls.append(kwargs)
        return DigestReport(text="OA 日报\n合成待办和关注事项", watch_coverage_complete=True)

    monkeypatch.setattr(digest, "collect_oa_daily_digest", collect)
    return service, target, companion, other_user, telegram, clock, calls


def arguments(endpoint, *, subject="user-a"):
    return dict(user_subject=subject, agent_host="openclaw", telegram_endpoint_key=endpoint["endpoint_key"])


def test_publish_and_read_exact_telegram_receipt(context):
    service, target, companion, other_user, _, _, calls = context
    scope = arguments(target)
    published = digest.publish_oa_daily_digest(service, trial=False, **scope)
    assert published["status"] == "published"
    assert published["localDate"] == "2026-11-12"
    assert published["deliveryState"] == "pending"
    assert published["acknowledgedAt"] is None
    assert len(calls) == 1

    rows = service.tasks.list_timeline(user_subject="user-a", entry_type="chat_message")
    assert [(row["role"], row["text"]) for row in rows] == [("assistant", "OA 日报\n合成待办和关注事项")]
    assert rows[0]["entry_id"] == published["timelineEntryId"]
    assert "targetEndpointBinding" not in rows[0]["payload"]
    assert service.tasks.get_endpoint(rows[0]["source_endpoint_id"], user_subject="user-a")["client_type"] == "web"

    delivery, = service.tasks.list_outbox(user_subject="user-a")
    assert delivery["endpoint_id"] == target["endpoint_id"]
    assert delivery["event_id"] == rows[0]["entry_id"]
    assert digest.is_current_digest_destination(service, delivery)
    assert not service.tasks.list_outbox(user_subject="user-b")
    assert not service.tasks.list_timeline(user_subject="user-b", entry_type="chat_message")

    for different_scope in (arguments(companion), arguments(other_user, subject="user-b")):
        assert digest.get_oa_daily_digest_receipt(service, digest_id=published["digestId"], **different_scope) == {"status": "not_found"}
    with pytest.raises(TaskIntegrityError):
        digest.publish_oa_daily_digest(service, trial=False, **arguments(companion))
    with pytest.raises(PermissionError):
        digest.publish_oa_daily_digest(service, trial=True, **arguments(other_user))
    assert len(calls) == 1

    service.tasks.claim_outbox(user_subject="user-a", endpoint_id=target["endpoint_id"])
    service.tasks.acknowledge_outbox(user_subject="user-a", endpoint_id=target["endpoint_id"],
                                    delivery_id=published["deliveryId"], succeeded=True)
    receipt = digest.get_oa_daily_digest_receipt(service, local_date="2026-11-12", **scope)
    assert receipt["deliveryState"] == "acknowledged"
    assert receipt["acknowledgedAt"] is not None
    assert receipt["attemptCount"] == 1
    assert receipt["digestId"] == published["digestId"]
    reused = digest.publish_oa_daily_digest(service, trial=False, **scope)
    assert reused == {**receipt, "status": "reused"}
    assert len(calls) == 1
    assert service.tasks.list_outbox(user_subject="user-a")[0]["attempt_count"] == 1


def test_concurrent_publication_converges_to_one_digest(context, monkeypatch):
    service, target, _, _, _, _, _ = context
    barrier = Barrier(2, timeout=5)

    def collect(*_args, **_kwargs):
        barrier.wait()
        return DigestReport(text="OA 日报", watch_coverage_complete=True)

    monkeypatch.setattr(digest, "collect_oa_daily_digest", collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(digest.publish_oa_daily_digest, service, trial=False, **arguments(target)) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert {result["status"] for result in results} == {"published", "reused"}
    assert len({result["digestId"] for result in results}) == 1
    assert len(service.tasks.list_outbox(user_subject="user-a")) == 1


def test_retry_retains_unread_watch_window_and_distinguishes_trial(context, monkeypatch):
    service, target, _, _, _, clock, _ = context
    calls = []

    def collect(_service, **kwargs):
        calls.append(kwargs)
        return DigestReport(text="OA 日报", watch_coverage_complete=len(calls) != 2)

    monkeypatch.setattr(digest, "collect_oa_daily_digest", collect)
    scope = arguments(target)
    trial = digest.publish_oa_daily_digest(service, trial=True, **scope)
    initial = clock[0]
    assert calls[0]["since"] == initial - timedelta(days=1)
    assert digest.publish_oa_daily_digest(service, trial=True, **scope)["digestId"] == trial["digestId"]
    assert digest.get_oa_daily_digest_receipt(service, trial=True, **scope)["digestId"] == trial["digestId"]
    clock[0] += timedelta(hours=10)
    scheduled = digest.publish_oa_daily_digest(service, trial=False, **scope)
    assert scheduled["digestId"] != trial["digestId"]
    assert calls[1]["since"] == initial
    clock[0] += timedelta(days=1)
    digest.publish_oa_daily_digest(service, trial=False, **scope)
    assert calls[2]["since"] == initial
    assert len(service.tasks.list_outbox(user_subject="user-a")) == 3


@pytest.mark.parametrize("change", ["recipient", "permissions", "oa_session"])
def test_destination_or_permissions_change_during_read_prevents_publish(context, monkeypatch, change):
    service, target, _, _, telegram, _, _ = context

    def collect(*_args, **_kwargs):
        if change == "recipient":
            telegram("user-a", "1005", key=target["endpoint_key"])
        elif change == "permissions":
            grant_permissions(service.user_grants, "user-a", ["oa.workflow.read"])
        else:
            service.sessions.get_or_create(user_subject="user-a", system_id="oa", expected_principal_ref="new-owner")
        return DigestReport(text="旧绑定下的合成内容", watch_coverage_complete=True)

    monkeypatch.setattr(digest, "collect_oa_daily_digest", collect)
    with pytest.raises(PermissionError, match="发生变化"):
        digest.publish_oa_daily_digest(service, trial=False, **arguments(target))
    assert not service.tasks.list_outbox(user_subject="user-a")
    assert not service.tasks.list_timeline(user_subject="user-a", entry_type="chat_message")


def test_delivery_binding_rejects_post_publication_rebind(context):
    service, target, _, _, telegram, _, _ = context
    digest.publish_oa_daily_digest(service, trial=True, **arguments(target))
    delivery, = service.tasks.list_outbox(user_subject="user-a")
    telegram("user-a", "1005", key=target["endpoint_key"])
    assert not digest.is_current_digest_destination(service, delivery)


def test_endpoint_key_pins_sender_and_allows_coherent_wildcard_account(context):
    service, target, _, _, telegram, _, calls = context
    telegram("user-a", "1005", key=target["endpoint_key"])
    with pytest.raises(PermissionError, match="收件人或机器人账号不一致"):
        digest.publish_oa_daily_digest(service, trial=False, **arguments(target))
    assert not calls
    wildcard = telegram("user-a", "1007", key="telegram:*:1007")
    result = digest.publish_oa_daily_digest(service, trial=False, **arguments(wildcard))
    assert service.tasks.get_delivery(result["deliveryId"])["endpoint_id"] == wildcard["endpoint_id"]
