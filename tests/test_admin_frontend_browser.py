"""Isolated browser acceptance; opt in with AGENTBRIDGE_BROWSER_TESTS=1."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import threading

import pytest

from agentbridge.admin.application import AdminControlPlane
from agentbridge.admin.server import create_admin_http_server, validate_admin_server_config
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from tests.database_fixtures import configured_source


pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTBRIDGE_BROWSER_TESTS") != "1",
    reason="opt-in isolated Chromium acceptance",
)
PASSWORD = "BrowserFixture!Only2026"


@pytest.fixture
def console(tmp_path):
    service = CentralCapabilityService(
        home=tmp_path,
        base_url="http://127.0.0.1:1/seeyon",
        taihua_base_url="http://127.0.0.1:1",
        smartlight_base_url="https://127.0.0.1:1/smartlight",
        yuque_base_url="https://fixture.yuque.com",
        yuque_organization_id=1,
    )
    configured_source(tmp_path)
    control = AdminControlPlane(service=service, identity_store=McpIdentityTokenStore(service.db_path))
    for role in ("admin", "auditor"):
        control.accounts.create(username=role, password=PASSWORD, role=role, must_change_password=False)
    for system in ("oa", "taihua", "yuque", "smartlight"):
        session = service.sessions.get_or_create(user_subject="fixture-user", system_id=system)
        service.sessions.mark_awaiting_login(session["session_id"])
    control.identity_store.issue(
        user_subject="fixture-user", expected_principal_ref="fixture-user",
        label="浏览器测试", ttl_seconds=86400,
    )
    trace, _ = service.runtime_governance.ensure_trace(
        user_subject="fixture-user", request_id="browser-fixture", host_type="reference-host",
        request_kind="write", system_id="oa", capability_name="oa.meeting_room.cancel",
    )
    span = service.runtime_governance.start_span(
        trace_id=trace["trace_id"], stage="commit", side_effect_boundary="B4_COMMIT_ATTEMPTED",
    )
    service.runtime_governance.finish_span(span["span_id"], status="unknown", error_code="RESULT_UNKNOWN")
    first = service.runtime_governance.upsert_incident(
        rule_id="write_outcome_unknown", severity="P2", symptom_code="RESULT_UNKNOWN",
        actionability="current", title="写入结果未确认", trace_id=trace["trace_id"],
        user_subject="fixture-user", system_id="oa",
        recommended_action="到权威业务系统核对实际结果，禁止自动再次提交。",
    )
    second = service.runtime_governance.upsert_incident(
        rule_id="delivery_wait", severity="P2", symptom_code="DELIVERY_PENDING",
        actionability="current", title="投递等待确认", user_subject="fixture-user",
        object_type="delivery", object_id="fixture-delivery",
    )
    historical = service.runtime_governance.upsert_incident(
        rule_id="historical_fixture", severity="P2", symptom_code="RESULT_UNKNOWN",
        actionability="manual_reconciliation", title="历史开发测试事项", object_type="operation", object_id="fixture-old",
    )
    with service.runtime_governance._connect() as connection:
        connection.execute("UPDATE runtime_incidents SET first_seen_at = '2026-01-01T00:00:00+00:00' WHERE incident_id = ?", (historical["incident_id"],))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    config = validate_admin_server_config(host="127.0.0.1", port=port, public_base_url=origin, tls_cert=None, tls_key=None)
    server = create_admin_http_server(config=config, control_plane=control)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield origin, first, second
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_database_grant_editor_persists_and_auditor_cannot_edit(console):
    browser_api = pytest.importorskip("playwright.sync_api")
    expect = browser_api.expect
    origin, _, _ = console
    output = Path("output/admin-database-grants-20260912")
    output.mkdir(parents=True, exist_ok=True)
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for role in ("admin", "auditor"):
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator('[name="username"]').fill(role)
                page.locator('[name="password"]').fill(PASSWORD)
                page.locator('#login-form button[type="submit"]').click()
                page.locator('#nav [data-view="users"]').click()
                page.get_by_role("button", name="fixture-user用户操作").click()
                page.locator('[data-database-grants="fixture-user"]').click()
                expect(page.locator("#modal-title")).to_have_text("fixture-user · 选择数据源")
                page.locator("#modal-submit").click()
                expect(page.locator("#modal-title")).to_have_text("fixture-user · taihua_primary")
                choices = page.locator('#modal input[name="database_capability"]')
                expect(choices).to_have_count(8)
                if role == "admin":
                    expect(page.locator('#modal input:checked')).to_have_count(0)
                    for choice in choices.all():
                        choice.check()
                    page.locator('#modal [name="reason"]').fill("isolated fixture: all eight capabilities")
                    page.locator('#modal-submit').click()
                    expect(page.locator("#modal")).not_to_be_visible()
                    expect(page.locator("#content")).to_contain_text("8 项；按源配置")
                    page.get_by_role("button", name="fixture-user用户操作").click()
                    page.locator('[data-database-grants="fixture-user"]').click()
                    page.locator("#modal-submit").click()
                else:
                    expect(page.locator('#modal input:disabled')).to_have_count(8)
                    expect(page.locator('#modal-submit')).to_have_text("关闭")
                expect(page.locator('#modal input:checked')).to_have_count(8)
                page.screenshot(path=str(output / f"{role}-desktop.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("#modal").evaluate("e => e.scrollWidth <= e.clientWidth")
                expect(page.locator("#modal-submit")).to_be_in_viewport()
                expect(page.locator("#modal-title")).to_be_in_viewport()
                page.screenshot(path=str(output / f"{role}-mobile.png"))
                assert errors == []
                context.close()
        finally:
            browser.close()


def test_skill_configuration_is_scoped_and_auditor_is_readonly(console):
    browser_api = pytest.importorskip("playwright.sync_api")
    expect = browser_api.expect
    origin, _, _ = console
    output = Path("output/business-skills-browser")
    output.mkdir(parents=True, exist_ok=True)
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for role in ("admin", "auditor"):
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin)
                page.locator('[name="username"]').fill(role)
                page.locator('[name="password"]').fill(PASSWORD)
                page.locator('#login-form button[type="submit"]').click()
                page.locator('#nav [data-view="users"]').click()
                page.get_by_role("button", name="fixture-user用户操作").click()
                page.locator('[data-skill-user="fixture-user"]').click()
                expect(page.locator('#modal')).to_be_visible()
                expect(page.locator('#modal input[name="skill"]')).to_have_count(5)
                if role == "admin":
                    page.locator('#modal input[name="skill"][value="oa-work-log"]').check()
                    page.locator('#modal [name="profiles:oa-work-log"][value="fill"]').uncheck()
                    page.locator('#modal [name="reason"]').fill("isolated Skill fixture: preview only")
                    page.locator('#modal-submit').click()
                    expect(page.locator('#modal')).not_to_be_visible()
                    page.get_by_role("button", name="fixture-user用户操作").click()
                    page.locator('[data-skill-user="fixture-user"]').click()
                    expect(page.locator('#modal')).to_be_visible()
                expect(page.locator('#modal input[name="skill"]:checked')).to_have_count(0)
                assert page.locator('#modal input[name="skill"][value="oa-work-log"]').evaluate("e => e.indeterminate")
                expect(page.locator('#modal input[name^="source:"]')).to_have_count(0)
                expect(page.locator('#modal [name="profiles:oa-work-log"][value="fill"]')).not_to_be_checked()
                if role == "auditor":
                    expect(page.locator('#modal input[name="skill"]:disabled')).to_have_count(5)
                page.screenshot(path=str(output / f"{role}-desktop.png"))
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator('#modal').evaluate("e => e.scrollWidth <= e.clientWidth")
                page.screenshot(path=str(output / f"{role}-mobile.png"))
                assert not errors
                context.close()
        finally:
            browser.close()


def test_console_views_interactions_and_layout(console):
    browser_api = pytest.importorskip("playwright.sync_api")
    expect = browser_api.expect
    origin, first, second = console
    output = Path("output/admin-redesign-20260906")
    output.mkdir(parents=True, exist_ok=True)
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1600, "height": 1050})
        page = context.new_page()
        errors = []
        writes = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)

        def ready():
            expect(page.locator("#content")).to_have_attribute("aria-busy", "false")
            assert "读取失败" not in page.locator("#content").inner_text()

        def navigate(view):
            if page.viewport_size["width"] <= 760:
                page.locator("#navigation-toggle").click()
            page.locator(f'#nav [data-view="{view}"]').click()
            ready()

        def layout():
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "page overflow"
            page.wait_for_function("Array.from(document.images).every(i => i.complete)")
            assert page.locator("img").evaluate_all("images => images.every(i => i.complete && i.naturalWidth > 0)"), "broken icons"

        page.goto(origin)
        expect(page.locator("#login-form")).to_be_visible()
        page.screenshot(path=str(output / "登录-桌面.png"))
        page.locator('[name="username"]').fill("admin")
        page.locator('[name="password"]').fill(PASSWORD)
        page.locator('#login-form button[type="submit"]').click()
        expect(page.locator(".architecture")).to_be_visible()
        ready()
        layout()
        page.screenshot(path=str(output / "总览-桌面.png"), full_page=True)

        for view in ("users", "sessions", "capabilities", "operations", "interactions", "traces", "incidents", "coordination", "runtime", "audit"):
            navigate(view)
            layout()
            page.screenshot(path=str(output / f"{view}-桌面.png"))

        navigate("incidents")
        expect(page.locator('select[data-incident-category]')).to_have_value("current")
        expect(page.get_by_role("button", name="历史开发测试事项")).not_to_be_visible()
        page.locator('select[data-incident-category]').select_option("historical")
        expect(page.get_by_role("button", name="历史开发测试事项")).to_be_visible()
        page.get_by_role("button", name="历史开发测试事项").click()
        expect(page.locator("#incident-detail")).to_contain_text("累计检测次数（非业务次数）")
        page.locator('select[data-incident-category]').select_option("current")
        page.locator(f'[data-incident-detail="{first["incident_id"]}"]').click()
        expect(page.locator("#incident-evidence")).to_contain_text("B4")
        expect(page.locator("#incident-evidence")).not_to_contain_text("已核验")
        page.screenshot(path=str(output / "事件治理-桌面.png"), full_page=True)
        page.locator('[data-incident-action="resolve"]').click()
        expect(page.locator("#modal")).to_be_visible()
        expect(page.locator('#modal [name="reason"]')).to_have_attribute("required", "")
        page.locator("#modal-cancel").click()
        page.locator('[data-filter-search]').fill("没有这个事件")
        expect(page.locator("#incident-detail")).to_contain_text("选择筛选结果")
        expect(page.locator("[data-filter-empty]")).to_be_visible()
        page.locator('[data-filter-search]').fill("")
        for tab in ("observations", "slo", "signals", "recoveries", "events"):
            page.locator(f'[data-governance-tab="{tab}"]').click()
            expect(page.locator(f'[data-governance-panel="{tab}"]')).to_be_visible()
        page.locator('[data-governance-tab="events"]').press("ArrowRight")
        expect(page.locator('[data-governance-tab="observations"]')).to_be_focused()

        navigate("databases")
        page.get_by_role("button", name="taihua_primary taihua_primary数据源操作").click()
        expect(page.locator("#row-action-menu .row-menu-item")).to_have_count(4)
        page.locator('[data-source-edit="taihua_primary"]').click()
        expect(page.locator("#modal-title")).to_contain_text("taihua_primary")
        page.locator("#modal-cancel").click()
        navigate("sessions")
        page.get_by_role("button", name="fixture-user oa会话操作").click()
        expect(page.locator("#row-action-menu .row-menu-item")).to_have_count(3)
        page.locator("[data-session-history]").click()
        expect(page.locator("#modal-title")).to_have_text("会话状态历史")
        page.locator("#modal-cancel").click()
        navigate("users")
        expect(page.get_by_role("button", name="fixture-user用户操作")).to_be_visible()
        expect(page.locator('[data-user-grants="fixture-user"]')).to_have_count(0)
        page.get_by_role("button", name="fixture-user用户操作").click()
        expect(page.locator("#row-action-menu")).to_be_visible()
        expect(page.locator('[data-user-grants="fixture-user"]')).to_be_visible()
        expect(page.locator('[data-pause-user="fixture-user"]')).to_be_visible()
        page.locator('[data-user-grants="fixture-user"]').click()
        expect(page.locator("#modal-title")).to_have_text("fixture-user · 业务能力")
        page.locator("#modal-cancel").click()
        expect(page.locator("#row-action-menu")).not_to_be_visible()
        expect(page.get_by_role("columnheader", name="历史 Scope")).to_have_count(0)
        page.locator('[data-filter-status="active"] [data-row-actions]').click()
        expect(page.locator('[data-renew-token]')).to_be_visible()
        page.locator('[data-renew-token]').click()
        expect(page.locator("#modal-title")).to_have_text("管理员续期")
        page.locator("#modal-cancel").click()
        page.get_by_role("button", name="fixture-user用户操作").click()
        page.locator("#row-action-menu .row-menu-item").first.press("Escape")
        expect(page.locator("#row-action-menu")).not_to_be_visible()
        expect(page.get_by_role("button", name="fixture-user用户操作")).to_have_attribute("aria-expanded", "false")
        page.locator("[data-issue-token]").click()
        expect(page.locator('input[name="scope"]')).to_have_count(0)
        expect(page.locator("#modal-body")).to_contain_text("新用户默认无业务能力")
        expect(page.locator("#modal-title")).to_have_text("签发 MCP Token")
        page.locator("#modal-cancel").click()
        navigate("capabilities")
        page.locator("[data-global-pause]").click()
        expect(page.locator("#modal-title")).to_have_text("全局暂停所有写入")
        page.locator("#modal-cancel").click()

        # Delayed responses must never replace the page chosen after them.
        page.route("**/api/overview", lambda route: (page.wait_for_timeout(350), route.continue_()))
        page.locator('[data-view="overview"]').click()
        page.locator('[data-view="users"]').click()
        ready()
        page.wait_for_timeout(500)
        expect(page.locator("#content")).to_contain_text("身份绑定")
        page.unroute("**/api/overview")
        page.route("**/api/operations?*", lambda route: route.fulfill(status=503, json={"error": {"message": "暂不可用"}}))
        page.locator('[data-view="operations"]').click()
        expect(page.locator("#content")).to_contain_text("读取失败")
        expect(page.locator("#refresh-button")).to_be_enabled()
        assert errors == ["Failed to load resource: the server responded with a status of 503 (Service Unavailable)"]
        errors.clear()
        page.unroute("**/api/operations?*")
        page.locator('#content [data-open-view="operations"]').click()
        ready()

        for width in (390, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            for view in ("overview", "incidents", "users", "sessions", "coordination", "runtime"):
                navigate(view)
                layout()
                page.screenshot(path=str(output / f"{view}-{width}.png"), full_page=True)
                if view == "users" and width == 390:
                    page.get_by_role("button", name="fixture-user用户操作").click()
                    assert page.locator("#row-action-menu").evaluate(
                        "e => { const r = e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight; }"
                    )
                    page.screenshot(path=str(output / "users-mobile-menu.png"))
                    page.locator("#row-action-menu .row-menu-item").first.press("Escape")
        assert writes == [origin + "/api/login"], "read-only acceptance issued a management write"
        assert not errors

        page.set_viewport_size({"width": 1600, "height": 1050})
        context.clear_cookies()
        page.locator("#refresh-button").click()
        expect(page.locator("#login-form")).to_be_visible()
        assert errors and all(message == "Failed to load resource: the server responded with a status of 401 (Unauthorized)" for message in errors)
        errors.clear()
        page.locator('[name="username"]').fill("auditor")
        page.locator('[name="password"]').fill(PASSWORD)
        page.locator('#login-form button[type="submit"]').click()
        expect(page.locator(".architecture")).to_be_visible()
        navigate("incidents")
        page.locator(f'[data-incident-detail="{first["incident_id"]}"]').click()
        expect(page.locator("[data-incident-action]")).to_have_count(0)
        navigate("users")
        expect(page.locator("[data-issue-token]")).to_have_count(0)
        page.get_by_role("button", name="fixture-user用户操作").click()
        expect(page.locator('[data-user-grants="fixture-user"]')).to_contain_text("查看业务能力")
        expect(page.locator("[data-pause-user]")).to_have_count(0)
        expect(page.locator("[data-renew-token]")).to_have_count(0)
        assert not errors
        browser.close()
