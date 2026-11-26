"""Both signed-in Workspace users see and can submit only their currently paired Pi destination."""

from types import SimpleNamespace
import threading

import pytest

from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.authorization_fixtures import grant_permissions
from tests.test_pi_workspace import pi  # noqa: F401 -- pytest fixture
from tests.test_pi_workspace_images import paired  # noqa: F401 -- pytest fixture
from tests.test_workspace import PASSWORD, _cookies, _free_port, _request
from tests.test_workspace_daily_digest import form
from tests.test_workspace_watches import seed, request


@pytest.fixture
def server(pi, paired):
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    http = create_workspace_http_server(
        config=validate_workspace_server_config(
            host="127.0.0.1", port=port, public_base_url=origin, tls_cert=None, tls_key=None
        ),
        application=pi.app,
    )
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    ctx = SimpleNamespace(
        service=pi.service, app=pi.app, accounts=pi.accounts, port=port, origin=origin, cookies={}
    )
    try:
        for account in pi.accounts.values():
            subject = account["user_subject"]
            grant_permissions(pi.service.user_grants, subject, ["oa.workflow.read", "oa.workflow.watch"])
            session = pi.service.sessions.get_or_create(
                user_subject=subject, system_id="oa", expected_principal_ref=subject
            )
            pi.service.sessions.activate(session["session_id"], observed_principal_ref=subject)
            pi.service.session_states.save(session["session_id"], {"cookies": [], "synthetic": subject})
            status, headers, _ = _request(
                port,
                "POST",
                "/api/login",
                origin=origin,
                body={"username": account["username"], "password": PASSWORD},
            )
            assert status == 200
            ctx.cookies[subject] = _cookies(headers)
        yield ctx
    finally:
        http.shutdown()
        http.server_close()
        thread.join(5)


def test_current_pi_recipient_and_ownership_across_digest_and_watch_forms(server):
    ctx = server
    digest = "/api/oa-daily-digest"
    options = "/api/business-watches/options"

    def read(path, subject):
        status, _, response = request(ctx, "GET", path, subject=subject)
        assert status == 200
        return response

    first = read(digest, "user-a")
    second = read(digest, "user-b")
    assert len(first["telegramOptions"]) == 1 and second["telegramOptions"] == []
    target = first["telegramOptions"][0]["id"]
    assert [e["endpoint_id"] for e in read(options, "user-a")["endpoints"]] == [target]
    assert read(options, "user-b")["endpoints"] == []
    saved = request(
        ctx, "POST", digest, body=form(first, enabled=True, workspace=True, telegramEndpointId=target)
    )
    assert saved[0] == 200 and read(digest, "user-a")["telegramEndpointId"] == target
    assert (
        request(
            ctx, "POST", digest, subject="user-b", body=form(second, enabled=True, telegramEndpointId=target)
        )[0]
        == 409
    )
    # Each owner edits their own real watch; trying another person's watch would only test its id guard.
    for subject, expected in [("user-a", 200), ("user-b", 403)]:
        watch = seed(ctx, subject=subject)
        path = "/api/business-watches/" + watch["watch_id"] + "/update"
        result = request(
            ctx,
            "POST",
            path,
            subject=subject,
            body={"action": "configure", "expected_version": watch["version"], "endpoint_ids": [target]},
        )
        assert result[0] == expected, result[2]
        if expected == 200:
            assert read("/api/business-watches/" + watch["watch_id"], subject)["watch"]["endpoint_ids"] == [
                target
            ]
    TelegramPilotLinks(ctx.service).revoke(ctx.accounts["a"]["account_id"], "pi-office", "8801")
    assert read(options, "user-a")["endpoints"] == [] and read(digest, "user-a")["telegramOptions"] == []
    current = read(digest, "user-a")
    assert request(ctx, "POST", digest, body=form(current, enabled=True, telegramEndpointId=target))[0] == 409
    another = seed(ctx, subject="user-a", number=3)
    status, _, error = request(
        ctx,
        "POST",
        "/api/business-watches/" + another["watch_id"] + "/update",
        body={"action": "configure", "expected_version": another["version"], "endpoint_ids": [target]},
    )
    assert status == 403 and error["error"]["code"] == "WATCH_ENDPOINT_DENIED"


def test_watch_options_identify_generic_pi_private_chat_among_other_telegram_destinations(server):
    ctx = server
    path = "/api/business-watches/options"
    target = request(ctx, "GET", path)[2]["endpoints"][0]["endpoint_id"]
    other, _ = ctx.service.tasks.ensure_endpoint(
        user_subject="user-a", token_id="old-host-test-token", agent_host="openclaw",
        endpoint_key="telegram:default:777", client_type="telegram", external_subject="777",
        conversation_ref="agent:main:telegram:direct:777", label="办公手机",
    )
    other_id = other["endpoint_id"]
    for alias in (None, "Telegram", "Telegram 私聊", "办公手机"):
        with ctx.service.tasks._connect() as db:
            db.execute("UPDATE client_endpoints SET label=? WHERE endpoint_id=?", (alias, target))
        status, _, options = request(ctx, "GET", path)
        assert status == 200
        labels = {e["endpoint_id"]: e["label"] for e in options["endpoints"]}
        if alias == "办公手机":
            assert labels == {endpoint: "Telegram · 办公手机 · " + endpoint[:8] for endpoint in (target, other_id)}
        else:
            assert labels == {target: "Pi · Telegram 私聊", other_id: "Telegram · 办公手机"}
    assert request(ctx, "GET", path, subject="user-b")[2]["endpoints"] == []


def test_owned_history_preview_and_download_keep_original_bytes_and_expiry(server, pi):
    import httpx
    from tests.support.pi_workspace_images_probe import solid_png
    from tests.test_pi_workspace_images import send

    original = solid_png((21, 95, 190))
    send(pi, "b", "preview-a-real-image", [original])
    with server.service.workspace._connect() as db:
        image = db.execute(
            "SELECT attachment_id FROM timeline_attachments WHERE user_subject='user-b'"
        ).fetchone()[0]
    path = "/api/timeline/attachments/" + image + "/download"
    with httpx.Client(base_url=server.origin, trust_env=False) as client:
        assert client.get(path, params={"preview": "1"}).status_code == 401
        client.cookies.update(server.cookies["user-a"])
        assert client.get(path, params={"preview": "1"}).status_code == 403
        client.cookies.update(server.cookies["user-b"])
        for params, expected in (({}, "attachment"), ({"preview": "1"}, "inline")):
            response = client.get(path, params=params)
            assert response.status_code == 200 and response.content == original
            assert response.headers["content-type"] == "image/png"
            assert response.headers["content-disposition"].split(";")[0] == expected
            assert "no-store" in response.headers["cache-control"]
        with server.service.workspace._connect() as db:
            db.execute(
                "UPDATE timeline_attachments SET expires_at='2000-01-01T00:00:00Z' WHERE attachment_id=?",
                (image,),
            )
        expired = client.get(path, params={"preview": "1"})
        assert expired.status_code == 409 and expired.json()["error"]["code"] == "TIMELINE_ATTACHMENT_EXPIRED"
