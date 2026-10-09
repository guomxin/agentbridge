from copy import deepcopy
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentbridge.adapters.seeyon_flight_application import (
    FIELD_LABELS, LEG_FIELDS, FLIGHT_APPLICATION_SNAPSHOT_SCRIPT,
    flight_application_business_snapshot,
)
from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch, PendingActionOutcomeUnknown,
    approve_flight_application, pending_action_profile_for_title,
    preflight_pending_action, prepare_flight_application_approval,
)
from tests.test_seeyon_pending_actions import FakeAdapter, FakeWorker, _fixture, _inputs


def snapshot():
    values = {
        "field0001_id": "测试申请人", "field0002_id": "T001",
        "field0003_id": "研发中心", "field0004_id": "2026-09-23",
        "field0009_id": "",
        "field0013_id": "2026-09-24", "field0014_id": "济南",
        "field0015_id": "榆林", "field0016_id": "项目出差",
    }
    fields = [
        {"id": key, "label": label, "value": values[key],
         "record_id": "leg-1" if key in LEG_FIELDS else ""}
        for key, label in FIELD_LABELS.items()
    ]
    second = [deepcopy(item) for item in fields if item["id"] in LEG_FIELDS]
    for item in second:
        item["record_id"] = "leg-2"
        if item["id"] == "field0014_id":
            item["value"] = "榆林"
        elif item["id"] == "field0015_id":
            item["value"] = "济南"
    fields.extend(second)
    return {"browse_only": True, "fields": fields}


class FlightFrame:
    url = "http://oa.example.test/seeyon/common/cap4/form/index.html"

    def __init__(self, data):
        self.data = data

    def locator(self, selector):
        assert selector == "#field0001_id"
        return SimpleNamespace(count=lambda: 1)

    def evaluate(self, script):
        assert script == FLIGHT_APPLICATION_SNAPSHOT_SCRIPT
        return deepcopy(self.data)


@pytest.fixture
def target(monkeypatch):
    fixture = _fixture("standard_collaboration")
    fixture["source"]["title"] = "【综合】乘坐飞机申请单"
    fixture["detail"]["fields"] = [
        {"name": "申请人", "value": "测试申请人"},
        {"name": "出发时间", "value": "2026-09-24"},
    ]
    fixture["signals"]["identity"].update(
        template_id="-2302881233144493310",
        form_app_id="2631521264809559746",
        form_record_id="record-1",
    )
    worker = FakeWorker(fixture)
    data = snapshot()
    worker.page.frames = [FlightFrame(data)]
    adapter = FakeAdapter(worker)
    adapter.list_workflows = Mock(return_value={"items": [], "coverage": {"status": "complete"}})
    monkeypatch.setattr("agentbridge.adapters.seeyon_pending_actions.time.sleep", lambda _: None)
    return fixture, data, worker, adapter


def test_prepare_freezes_every_leg_and_commit_verifies_complete_readback(target):
    _, _, worker, adapter = target
    prepared = prepare_flight_application_approval(adapter, worker, _inputs())
    summary = prepared["summary"]["fields"]
    assert any(f["label"] == "航段1 · 出发地点" and f["value"] == "济南" for f in summary)
    assert any(f["label"] == "航段2 · 出发地点" and f["value"] == "榆林" for f in summary)
    assert worker.page.commit_payload is None
    boundary = Mock()
    result = approve_flight_application(adapter, worker, prepared["plan"], enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert result["workflow_approved"] is True
    assert result["workflow_profile"] == "flight_application"
    assert worker.page.commit_payload["attitude_code"] == "agree"


def test_preflight_returns_complete_read_only_review_for_opinion_card(target):
    _, _, worker, adapter = target

    result = preflight_pending_action(
        adapter,
        worker,
        {"affair_id": "affair-1"},
        "flight_application",
    )

    fields = result["review"]["fields"]
    assert result["review_fingerprint"].startswith("sha256:")
    assert result["review"]["title"] == "【综合】乘坐飞机申请单"
    assert any(f["label"] == "申请人" and f["value"] == "测试申请人" for f in fields)
    assert any(f["label"] == "航段1 · 到达地点" and f["value"] == "榆林" for f in fields)
    assert any(f["label"] == "航段2 · 出发地点" and f["value"] == "榆林" for f in fields)
    assert not any(f["label"] == "处理意见" for f in fields)


@pytest.mark.parametrize("change", [
    "template", "form", "node", "inconsistent_node", "record", "editable",
    "missing_field", "extra_field", "label", "missing_leg_id", "duplicate_leg_cell",
    "blank_leg_value",
])
def test_changed_contract_stops_before_prepare(target, change):
    fixture, data, worker, adapter = target
    if change in {"template", "form", "record"}:
        key = {"template": "template_id", "form": "form_app_id", "record": "form_record_id"}[change]
        fixture["signals"]["identity"][key] = "other" if change != "record" else ""
    elif change == "node":
        fixture["signals"].update(node_policy="sendoredit", node_policy_name="发起人填写")
    elif change == "inconsistent_node":
        fixture["signals"]["node_policy"] = "sendoredit"
    elif change == "editable":
        data["browse_only"] = False
    elif change == "missing_field":
        data["fields"].pop()
    elif change == "extra_field":
        data["fields"].append({"id": "field9999_id", "label": "未知", "value": "", "record_id": ""})
    elif change == "label":
        data["fields"][0]["label"] = "其他申请人"
    elif change == "missing_leg_id":
        next(f for f in data["fields"] if f["id"] in LEG_FIELDS)["record_id"] = ""
    elif change == "duplicate_leg_cell":
        data["fields"].append(deepcopy(next(f for f in data["fields"] if f["id"] in LEG_FIELDS)))
    else:
        next(f for f in data["fields"] if f["id"] in LEG_FIELDS)["value"] = ""
    with pytest.raises(PendingActionContractMismatch):
        prepare_flight_application_approval(adapter, worker, _inputs())
    assert worker.page.commit_payload is None


@pytest.mark.parametrize("change", ["route", "new_leg", "editable"])
def test_change_after_authorization_stops_before_boundary(target, change):
    _, data, worker, adapter = target
    plan = prepare_flight_application_approval(adapter, worker, _inputs())["plan"]
    if change == "route":
        next(f for f in data["fields"] if f["record_id"] == "leg-1" and f["id"] == "field0015_id")["value"] = "北京"
    elif change == "new_leg":
        extra = [deepcopy(f) for f in data["fields"] if f["record_id"] == "leg-1"]
        for field in extra:
            field["record_id"] = "leg-3"
        data["fields"].extend(extra)
    else:
        data["browse_only"] = False
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch):
        approve_flight_application(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_payload is None


def test_incomplete_pending_readback_cannot_prove_success(target):
    _, _, worker, adapter = target
    plan = prepare_flight_application_approval(adapter, worker, _inputs())["plan"]
    adapter.list_workflows.return_value = {"items": [], "coverage": {"status": "partial"}}
    with pytest.raises(PendingActionOutcomeUnknown):
        approve_flight_application(adapter, worker, plan, enter_commit_boundary=Mock())


def test_title_routes_to_dedicated_profile():
    assert pending_action_profile_for_title("【综合】乘坐飞机申请单")["profile"] == "flight_application"
    assert pending_action_profile_for_title("【综合】乘坐飞机申请单-张三")["profile"] == "flight_application"
    assert pending_action_profile_for_title("转发：【综合】乘坐飞机申请单") is None


def test_actual_cap4_extractor_groups_rows_and_rejects_visible_editor():
    from playwright.sync_api import sync_playwright

    data = snapshot()
    html = []
    for field in data["fields"]:
        value = escape(field["value"])
        html.append(
            f'<tr data-record-id="{field["record_id"]}"><td>'
            f'<div id="{field["id"]}"><section class="is-none">'
            f'<span class="label-margin">{field["label"]}</span>'
            f'<div class="field-content-wrapper">{value}</div>'
            f'</section></div></td></tr>'
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('<table>' + ''.join(html) + '</table>')
            actual = page.evaluate(FLIGHT_APPLICATION_SNAPSHOT_SCRIPT)
            assert actual == data
            page.locator("#field0013_id").first.evaluate(
                "element => element.insertAdjacentHTML('beforeend', '<input value=changed>')"
            )
            assert page.evaluate(FLIGHT_APPLICATION_SNAPSHOT_SCRIPT)["browse_only"] is False
        finally:
            browser.close()
