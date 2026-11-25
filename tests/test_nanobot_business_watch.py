"""Explicit watch destinations remain personal across hosts, pairing changes and retries."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from agentbridge.core.business_content_identity import principal_binding
from tests.authorization_fixtures import grant_permissions
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.support.nanobot_telegram_loopback import fixture as channel_fixture
from nanobot_agentbridge.watch_delivery import deliver_watch


def test_watch_outbox_uses_explicit_verified_nanobot_endpoint_and_current_attempt(mirror):
    service, _, _, alice, bob, ask, deploy = mirror
    ask(alice, "materialize-peer", "普通消息")
    grant_permissions(service.user_grants, alice, ["oa.workflow.read", "oa.workflow.watch"])
    session = service.sessions.find(user_subject=alice, system_id="oa")
    assert session
    session = service.sessions.activate(session["session_id"], observed_principal_ref=session["expected_principal_ref"])
    args = {"user_subject": alice, "agent_host": "nanobot", "endpoint_key": "telegram:8001:501", "host_instance_id": INSTANCE}
    endpoint = service.tasks.endpoint_for_key(**{k:v for k,v in args.items() if k!="host_instance_id"})
    store = service.business_watch.store
    watch, _ = store.create(user_subject=alice, principal_ref=principal_binding(session),
                            system_instance=service.business_watch.system_instance,
                            source={"collection":"done","affair_id":"8100000","summary_id":"8200000","process_id":"8300000","case_id":"8400000"},
                            title="合成流程", endpoints=[endpoint["endpoint_id"]], allow_mark_read=True, include_private=True)
    for node in ("一审", "复核"):
        watch = store.apply_observation(watch["watch_id"], user_subject=alice, expected_version=watch["version"],
            snapshot={"workflow_state":"in_progress","current_nodes":[{"name":node,"handlers":["甲"]}],"current_handlers":["甲"],
                      "observed_at":datetime.now(timezone.utc).isoformat(),"unavailable_fields":[]}, include_private=True)["watch"]
    assert not any(n["deliveryMode"] == "business_watch" for n in
                   service.claim_host_notifications(**args, include_workspace_chat=True)["notifications"])
    rows = service.claim_host_notifications(**args, include_workspace_chat=True, include_business_watch=True)["notifications"]
    notice, = [row for row in rows if row["deliveryMode"] == "business_watch"]
    assert notice["message"] is None and notice["watchId"] == watch["watch_id"]
    assert not service.tasks.list_outbox(user_subject=bob)
    attempt = {"delivery_id":notice["deliveryId"],"attempt_count":notice["attemptCount"]}
    assert "复核" in service.validate_host_notification(**args, **attempt)["message"]
    assert service.validate_host_notification(**args, **{**attempt, "attempt_count":2}) == {"allowed":False, "stale":True}
    deploy("8002")
    assert not service.validate_host_notification(**args, **attempt)["allowed"]


@pytest.mark.parametrize("permission", ["allowed", "stale", "revoked", "lost"])
def test_native_watch_send_fresh_payload_only_no_unknown_replay(tmp_path, monkeypatch, permission):
    with channel_fixture(tmp_path, allow_pairing=True) as (state, _, create):
        pilot = create()
        binding, digest = pilot._binding(501)
        requests = []
        original = pilot.gateway._request
        def validate(action, body, **kwargs):
            if action == "telegram-watch-validate":
                requests.append(action)
                return {"allowed":permission in {"allowed","lost"},"stale":permission=="stale","message":"新核验的变化"}
            if action == "telegram-notification-receipt":
                requests.append(body["delivered"])
                return {"recorded": True}
            return original(action, body, **kwargs)
        monkeypatch.setattr(pilot.gateway, "_request", validate)
        notice={"deliveryId":str(uuid4()),"runId":"watch:"+str(uuid4()),"attempt":1,"message":"失效缓存"}
        state["lost_delivery"] = permission=="lost"
        deliver_watch(pilot.terminal,501,binding,digest,notice)
        state["lost_delivery"] = False
        deliver_watch(pilot.terminal,501,binding,digest,{**notice,"attempt":2})
        if permission in {"allowed","lost"}:
            assert [row["text"] for row in state["deliveries"]] == ["新核验的变化"]
            assert requests == ["telegram-watch-validate", permission=="allowed", permission=="allowed"]
        else:
            assert not state["deliveries"]
            if permission == "stale":
                assert requests == ["telegram-watch-validate"] * 2
        pilot.db.close()
