"""A private Telegram evaluation keeps its original Pi web account and current grant."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.host_instances import LEGACY_INSTANCE_ID
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.test_pi_workspace import pi, private_json  # noqa: F401 -- pytest fixture
from tests.test_workspace_skill_trial import PROPOSAL


@pytest.fixture
def evaluation(pi):
    service, owner = pi.service, pi.accounts["a"]
    pi.spec["requiredLevel"] = "L3"
    private_json(pi.route_file, pi.manifest)
    identity = McpIdentityTokenStore(service.db_path).issue(
        user_subject="user-a",
        expected_principal_ref="user-a",
        ttl_seconds=600,
        label="workspace-pi:pi-office:pi-a",
    )
    links = TelegramPilotLinks(service)
    args = (owner["account_id"], "pi-office", "8801")

    def pair():
        challenge = links.begin(*args)
        confirmed = links.present(
            "user-a", "pi-office", "8801", "501", challenge["code"]
        )
        links.confirm(*args, challenge["challenge_id"], confirmed)
        value = links.ensure_endpoint(
            "user-a", identity["token_id"], "pi-office", "8801", "501"
        )
        return {
            "instance": "pi-office",
            "token_id": identity["token_id"],
            "version": "0.1.6",
            "source": {
                k: value[k]
                for k in ("endpointId", "endpointKey", "conversationRef", "pairingRef")
            },
        }

    origin = pair()
    profile = json.loads(
        (
            Path(__file__).parents[1] / "schemas/agent-host/v1/test-vectors.json"
        ).read_text()
    )["profiles"][0]["value"]
    with service.workspace._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO agent_host_compatibility VALUES ('pi','0.1.6','L3','fixture','fixture','now')"
        )
    assert (
        service.negotiate_host(
            user_subject="user-a",
            token_id=identity["token_id"],
            profile={
                **profile,
                "hostInstanceId": "pi-office",
                "implementation": {"name": "pi", "version": "0.1.6"},
            },
        )["acceptedLevel"]
        == "L3"
    )
    draft = service.skill_authoring.save(
        "user-a", proposal=PROPOSAL, request_key="draft"
    )
    data = {
        "draft_id": draft["draft_id"],
        "expected_revision": 1,
        "request_key": "pi:draft:" + "0" * 64,
        "cases": [
            {
                "profile": "preview",
                "kind": "output",
                "prompt": "合成样例",
                "contains": ["完成"],
            }
        ],
    }
    return origin, data, pair, lambda: links.revoke(*args)


def test_pi_telegram_job_uses_original_model_and_checks_each_call(pi, evaluation):
    origin, data, _, revoke = evaluation
    authoring = pi.service.skill_authoring
    assert (
        pi.app.hosts.choices(pi.accounts["a"]["account_id"])["default_instance_id"]
        == LEGACY_INSTANCE_ID
    )
    job = authoring.dispatch("user-a", "evaluate", data, telegram_origin=origin)
    assert (
        authoring.dispatch("user-a", "evaluate", data, telegram_origin=origin)["job_id"]
        == job["job_id"]
    )
    with pytest.raises(PermissionError):
        authoring.dispatch("user-a", "evaluate", data)
    with pytest.raises(ValueError):
        authoring.dispatch("user-a", "evaluate", {**data, "telegram_origin": origin})

    def first_sample(binding, system, prompt):
        assert binding["host_type"] == "pi" and binding["credential_ref"] == "pi-a"
        assert binding["endpoint_key"].startswith("workspace:") and prompt == "合成样例"
        assert "telegram_origin" not in binding
        revoke()
        return {"text": "完成", "model": "synthetic"}

    with patch.object(
        pi.app,
        "_bound_client",
        return_value=Mock(skill_completion=Mock(side_effect=first_sample)),
    ) as client:
        assert authoring.workbench.run_once(
            None, bound_completion=pi.app._bound_skill_completion
        )
    result = authoring.workbench.jobs("user-a", job["job_id"])
    assert client.return_value.skill_completion.call_count == 1
    assert (
        result["state"] == "failed"
        and result["result"] is None
        and result["progress"] == 0
    )


def test_old_same_sender_repair_and_foreign_sources_do_not_run(pi, evaluation):
    origin, data, pair, revoke = evaluation
    authoring = pi.service.skill_authoring
    foreign = {**origin["source"], "endpointId": pi.accounts["b"]["endpoint_id"]}
    for bad in (
        {**origin, "source": None},
        {**origin, "source": foreign},
        {**origin, "instance": LEGACY_INSTANCE_ID},
        {**origin, "token_id": "another-token"},
        {**origin, "source": {**origin["source"], "conversationRef": "changed"}},
    ):
        with pytest.raises(PermissionError):
            authoring.dispatch("user-a", "evaluate", data, telegram_origin=bad)
    old = authoring.dispatch("user-a", "evaluate", data, telegram_origin=origin)
    revoke()
    replacement = pair()
    assert replacement["source"]["pairingRef"] != origin["source"]["pairingRef"]
    with pytest.raises(PermissionError):
        authoring.dispatch("user-a", "evaluate", data, telegram_origin=origin)
    with patch.object(pi.app, "_bound_client") as client:
        assert authoring.workbench.run_once(
            None, bound_completion=pi.app._bound_skill_completion
        )
        client.assert_not_called()
    assert authoring.workbench.jobs("user-a", old["job_id"])["state"] == "failed"
    # A pending legacy Pi request used the same private key convention but has no trustworthy origin.
    legacy = authoring.dispatch(
        "user-a", "evaluate", {**data, "request_key": "pi:draft:" + "1" * 64}
    )
    with patch.object(pi.app, "_skill_completion") as default_model:
        assert authoring.workbench.run_once(
            None, bound_completion=pi.app._bound_skill_completion
        )
        default_model.assert_not_called()
    assert authoring.workbench.jobs("user-a", legacy["job_id"])["state"] == "failed"
