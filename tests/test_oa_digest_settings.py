"""Durable Workspace preferences and scheduler fencing with isolated stores."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from threading import Event

import pytest

from agentbridge.core import oa_daily_digest
from agentbridge.core.business_watches import BusinessWatchError
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.core.oa_digest_settings import OADigestSettings
from tests.authorization_fixtures import authorized_service, grant_permissions
from tests.test_workspace import _create_account


@pytest.fixture
def setup(tmp_path, monkeypatch):
    key = tmp_path / "session.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    monkeypatch.setenv("AGENTBRIDGE_SESSION_KEY_FILE", str(key))
    service = authorized_service(home=tmp_path, base_url="https://oa.example.test/seeyon/")
    identities = McpIdentityTokenStore(service.db_path)
    owner = _create_account(service, user_subject="user-a", username="digest-owner", endpoint_key="owner-enrollment")
    other = _create_account(service, user_subject="user-b", username="digest-other", endpoint_key="other-enrollment")

    def telegram(subject, sender):
        identity = identities.issue(user_subject=subject, expected_principal_ref=subject)
        return service.tasks.ensure_endpoint(
            user_subject=subject, token_id=identity["token_id"], agent_host="openclaw",
            endpoint_key=f"telegram:default:{sender}", client_type="telegram", external_subject=sender,
            account_id="default", conversation_ref=f"agent:main:telegram:direct:{sender}",
            route={"channel": "telegram", "to": sender, "accountId": "default"},
            capabilities=["direct_status"],
        )[0]["endpoint_id"]

    own_id, other_id = telegram("user-a", "1001"), telegram("user-b", "2001")
    clock = [datetime(2026, 11, 12, tzinfo=timezone.utc)]
    settings = OADigestSettings(service, clock=lambda: clock[0])
    return service, settings, owner, other, own_id, other_id, telegram, clock


def body(*, enabled=True, time="08:30", workspace=True, telegram=None, version=0):
    return {"enabled": enabled, "time": time, "workspace": workspace,
            "telegramEndpointId": telegram, "expectedVersion": version}


def test_default_save_version_and_account_bound_channels(setup):
    _, settings, owner, other, own_id, other_id, _, _ = setup
    default = settings.get(owner)
    assert not default["enabled"] and default["version"] == 0 and default["nextRunAt"] is None
    assert default["time"] == "08:30" and default["workspace"]
    assert [item["id"] for item in default["telegramOptions"]] == [own_id]
    assert [item["id"] for item in settings.get(other)["telegramOptions"]] == [other_id]
    for wrong in (body(telegram=other_id), body(workspace=False), body(time="24:00"),
                  {**body(), "user_subject": "user-b"}):
        with pytest.raises(BusinessWatchError):
            settings.update(owner, wrong)
    with pytest.raises(BusinessWatchError, match="当前工作台账号"):
        settings.get({**owner, "user_subject": "user-b"})
    saved = settings.update(owner, body(telegram=own_id))
    assert saved["enabled"] and saved["workspace"] and saved["telegramAvailable"]
    assert saved["version"] == 1 and saved["nextRunAt"] == "2026-11-12T00:30:00+00:00"
    assert settings.update(owner, body(telegram=own_id, version=1)) == saved
    with pytest.raises(BusinessWatchError) as error:
        settings.update(owner, body(enabled=False))
    assert error.value.code == "DIGEST_VERSION_CONFLICT" and error.value.status == 409
    assert not settings.get(other)["enabled"]


def test_stale_endpoint_and_revoked_read_permission_do_not_prevent_stop(setup):
    service, settings, owner, _, own_id, _, telegram, _ = setup
    settings.update(owner, body(workspace=False, telegram=own_id))
    assert telegram("user-a", "1001") == own_id  # New credential changes the exact saved binding.
    changed = settings.get(owner)
    assert changed["telegramEndpointId"] == own_id and not changed["telegramAvailable"]
    assert changed["nextRunAt"] is None and changed["enabled"]
    grant_permissions(service.user_grants, "user-a", ["oa.workflow.watch"])
    stopped = settings.update(owner, body(enabled=False, workspace=False, telegram=own_id, version=1))
    assert not stopped["canConfigure"] and not stopped["enabled"]
    assert stopped["telegramEndpointId"] == own_id and not stopped["telegramAvailable"]
    with pytest.raises(BusinessWatchError) as error:
        settings.update(owner, body(version=2))
    assert error.value.code == "DIGEST_PERMISSION_REQUIRED"


def test_claim_is_serial_and_configuration_change_discards_inflight_completion(setup, monkeypatch):
    _, settings, owner, _, _, _, _, clock = setup
    settings.update(owner, body())
    clock[0] += timedelta(minutes=30)
    claimed, release = Event(), Event()

    def collecting(_service, setting, *, now, clock):
        assert setting["next_run_at"] == now.isoformat() and clock() == now
        assert setting["lease_token"] and setting["version"] == 1
        claimed.set()
        assert release.wait(5)
        return {"status": "published"}

    monkeypatch.setattr(oa_daily_digest, "publish_scheduled_oa_digest", collecting)
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(settings.run_due)
        try:
            assert claimed.wait(5)
            assert settings.run_due()["claimed"] == 0
            assert not settings.update(owner, body(enabled=False, version=1))["enabled"]
        finally:
            release.set()
        assert result.result(timeout=5)["discarded"] == 1
    assert settings.get(owner)["lastRunAt"] is None
    assert settings.run_due()["claimed"] == 0


def test_expired_lease_recovers_today_and_prior_dates_are_skipped(setup, monkeypatch):
    service, settings, owner, _, _, _, _, clock = setup
    settings.update(owner, body())
    clock[0] += timedelta(minutes=31)
    interrupted = settings._claim_due(clock[0])
    assert interrupted["version"] == 1

    recovered = OADigestSettings(service, clock=lambda: clock[0])
    seen = []

    def reused(_service, setting, **_kwargs):
        seen.append(setting)
        return {"status": "reused"}

    monkeypatch.setattr(oa_daily_digest, "publish_scheduled_oa_digest", reused)
    assert recovered.run_due()["claimed"] == 0
    clock[0] += timedelta(minutes=3)
    assert recovered.run_due()["reused"] == 1
    assert seen[0]["lease_token"] != interrupted["lease_token"]
    assert recovered.get(owner)["nextRunAt"] == "2026-11-13T00:30:00+00:00"
    # Rescheduling after a publication cannot add a second report on the same date.
    assert recovered.update(owner, body(time="09:00", version=1))["nextRunAt"] == "2026-11-13T01:00:00+00:00"
    clock[0] += timedelta(days=3, hours=2)
    assert recovered.run_due()["claimed"] == 0
    assert recovered.get(owner)["nextRunAt"] == "2026-11-16T01:00:00+00:00"
    assert len(seen) == 1
