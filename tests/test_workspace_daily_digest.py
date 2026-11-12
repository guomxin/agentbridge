"""The digest settings use the actual authenticated Workspace HTTP surface."""

from datetime import datetime
from zoneinfo import ZoneInfo

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from tests.authorization_fixtures import grant_permissions
from tests.test_workspace import _request
from tests.test_workspace_watches import http_context, request


PATH = "/api/oa-daily-digest"


def bind_telegram(ctx, subject, sender):
    token = McpIdentityTokenStore(ctx.service.db_path).issue(user_subject=subject, expected_principal_ref=subject)
    endpoint, _ = ctx.service.tasks.ensure_endpoint(
        user_subject=subject, token_id=token["token_id"], agent_host="openclaw",
        endpoint_key=f"telegram:default:{sender}", client_type="telegram", external_subject=sender,
        conversation_ref=f"agent:main:telegram:direct:{sender}", account_id="default", label="合成私聊",
        route={"channel": "telegram", "to": sender, "accountId": "default"},
        capabilities=["timeline_message"],
    )
    return endpoint["endpoint_id"]


def form(settings, **overrides):
    return {
        "enabled": settings["enabled"], "time": settings["time"], "workspace": settings["workspace"],
        "telegramEndpointId": settings["telegramEndpointId"], "expectedVersion": settings["version"],
        **overrides,
    }


def test_digest_settings_require_session_origin_and_csrf(http_context):
    ctx = http_context
    assert _request(ctx.port, "GET", PATH)[0] == 401
    status, _, settings = request(ctx, "GET", PATH)
    assert status == 200 and settings["enabled"] is False
    enable = form(settings, enabled=True, workspace=True)
    cookies = ctx.cookies["user-a"]
    assert _request(ctx.port, "POST", PATH, origin=ctx.origin, body=enable)[0] == 401
    assert _request(ctx.port, "POST", PATH, origin=ctx.origin, body=enable, cookies=cookies)[0] == 401
    assert _request(ctx.port, "POST", PATH, origin="https://elsewhere.example", body=enable, cookies=cookies,
                    csrf=cookies["agentbridge_workspace_csrf"])[0] == 403
    assert request(ctx, "GET", PATH)[2] == settings
    assert ctx.workers == []


def test_digest_settings_are_owned_and_cas_updates_do_not_read_oa(http_context):
    ctx = http_context
    own = bind_telegram(ctx, "user-a", "10201")
    other = bind_telegram(ctx, "user-b", "10202")
    status, headers, settings = request(ctx, "GET", PATH)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert [row["id"] for row in settings["telegramOptions"]] == [own]
    assert set(settings["telegramOptions"][0]) == {"id", "label"}
    status, _, rejected = request(ctx, "POST", PATH, body=form(settings, enabled=True, telegramEndpointId=other))
    assert status == 409 and rejected["error"]["code"] == "DIGEST_TELEGRAM_UNAVAILABLE"
    assert request(ctx, "POST", PATH, body=form(settings, user_subject="user-b"))[0] == 400

    submitted = form(settings, enabled=True, time="08:30", workspace=True, telegramEndpointId=own)
    status, _, saved = request(ctx, "POST", PATH, body=submitted)
    assert status == 200 and saved["enabled"] and saved["workspace"]
    assert saved["telegramEndpointId"] == own and saved["version"] > settings["version"]
    local = datetime.fromisoformat(saved["nextRunAt"]).astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.hour, local.minute) == (8, 30)
    assert request(ctx, "GET", PATH)[2] == saved
    assert request(ctx, "POST", PATH, body=submitted)[0] == 409

    _, _, secondary = request(ctx, "GET", PATH, subject="user-b")
    assert secondary["enabled"] is False and secondary["telegramEndpointId"] is None
    assert [row["id"] for row in secondary["telegramOptions"]] == [other]
    assert ctx.workers == [] and not ctx.service.tasks.list_outbox(user_subject="user-a")

    grant_permissions(ctx.service.user_grants, "user-a", [])
    _, _, denied = request(ctx, "GET", PATH)
    assert denied["canConfigure"] is False
    status, _, paused = request(ctx, "POST", PATH, body=form(denied, enabled=False))
    assert status == 200 and paused["enabled"] is False and paused["nextRunAt"] is None
    assert paused["time"] == "08:30" and paused["telegramEndpointId"] == own
