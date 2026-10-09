from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch, PendingActionOutcomeUnknown,
    approve_work_handover, prepare_work_handover_approval,
    pending_action_profile_for_title,
)
from agentbridge.adapters.seeyon_work_handover import (
    FIELD_LABELS, WORK_FIELDS, FINANCE_FIELDS, WORK_HANDOVER_SNAPSHOT_SCRIPT,
    work_handover_business_snapshot,
)
from tests.test_seeyon_pending_actions import FakeWorker, FakeAdapter, _fixture, _inputs


def snapshot():
    fields = []
    for key in FIELD_LABELS:
        fields.append({"id": key, "value": "",
                       "record_id": "work-1" if key in WORK_FIELDS else "finance-1" if key in FINANCE_FIELDS else ""})
    values = {"field0001_id": "测试移交人", "field0002_id": "测试部门",
              "field0006_id": "2026-09-08", "field0005_id": "测试监交人",
              "field0008_id": "离职", "field0040_id": "测试交接事项",
              "field0049_id": "已完成", "field0052_id": "测试接收人",
              "field0053_id": "2026-09-07", "field0055_id": "确认"}
    for field in fields:
        field["value"] = values.get(field["id"], "")
    return {"browse_only": True, "fields": fields}


class HandoverFrame:
    url = "http://oa.example.test/seeyon/common/cap4/form/index.html"

    def __init__(self, data):
        self.data = data

    def locator(self, selector):
        assert selector == "#field0001_id"
        return SimpleNamespace(count=lambda: 1)

    def evaluate(self, script):
        assert script == WORK_HANDOVER_SNAPSHOT_SCRIPT
        return deepcopy(self.data)


@pytest.fixture
def target(monkeypatch):
    fixture = _fixture("standard_collaboration")
    fixture["source"]["title"] = "(自动发起)【HR】工作交接单-测试移交人-离职"
    fixture["detail"]["fields"] = [{"name": name, "value": name} for name in ("移交人", "交接类别", "工作事项", "交接内容")]
    fixture["signals"]["identity"].update(template_id="-1377138047593329703", form_app_id="5247761788960957082", form_record_id="record-1")
    worker = FakeWorker(fixture)
    data = snapshot()
    worker.page.frames = [HandoverFrame(data)]
    adapter = FakeAdapter(worker)
    adapter.list_workflows = Mock(return_value={"items": [], "coverage": {"status": "complete"}})
    monkeypatch.setattr("agentbridge.adapters.seeyon_pending_actions.time.sleep", lambda _: None)
    return fixture, data, worker, adapter


def test_prepare_and_commit_show_and_freeze_all_rows(target):
    _, data, worker, adapter = target
    second = [deepcopy(f) for f in data["fields"] if f["id"] in WORK_FIELDS]
    for field in second:
        field["record_id"] = "work-2"
    second[0]["value"] = "第二项交接事项"
    data["fields"].extend(second)
    prepared = prepare_work_handover_approval(adapter, worker, _inputs())
    summary = prepared["summary"]["fields"]
    assert any(f["label"] == "工作明细2 · 工作事项" and f["value"] == "第二项交接事项" for f in summary)
    assert any(f["label"] == "交接类别" and f["value"] == "离职" for f in summary)
    assert any(f["label"] == "财务明细1 · 财务交接金额" and f["value"] == "（空）" for f in summary)
    assert worker.page.commit_payload is None
    boundary = Mock()
    result = approve_work_handover(adapter, worker, prepared["plan"], enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert result["workflow_approved"] is True
    assert result["workflow_profile"] == "work_handover"
    assert worker.page.commit_payload["attitude_code"] == "agree"


@pytest.mark.parametrize("change", ["template", "form", "node", "inconsistent_node", "record", "editable", "category", "missing_field", "extra_field", "missing_row_id", "duplicate_cell"])
def test_unsupported_contract_stops_before_prepare(target, change):
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
    elif change == "category":
        next(f for f in data["fields"] if f["id"] == "field0008_id")["value"] = "离职 其他"
    elif change == "missing_field":
        data["fields"].pop()
    elif change == "extra_field":
        data["fields"].append({"id": "field9999_id", "value": "", "record_id": ""})
    elif change == "missing_row_id":
        next(f for f in data["fields"] if f["id"] in WORK_FIELDS)["record_id"] = ""
    else:
        data["fields"].append(deepcopy(next(f for f in data["fields"] if f["id"] in WORK_FIELDS)))
    with pytest.raises(PendingActionContractMismatch):
        prepare_work_handover_approval(adapter, worker, _inputs())
    assert worker.page.commit_payload is None


@pytest.mark.parametrize("change", ["confirmation", "attachment", "amount", "new_row", "editable"])
def test_changes_after_authorization_stop_before_boundary(target, change):
    _, data, worker, adapter = target
    plan = prepare_work_handover_approval(adapter, worker, _inputs())["plan"]
    if change == "editable":
        data["browse_only"] = False
    elif change == "new_row":
        added = [deepcopy(f) for f in data["fields"] if f["id"] in WORK_FIELDS]
        for field in added:
            field["record_id"] = "work-2"
        data["fields"].extend(added)
    else:
        key = {"confirmation": "field0055_id", "attachment": "field0051_id", "amount": "field0061_id"}[change]
        next(f for f in data["fields"] if f["id"] == key)["value"] = "changed"
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch):
        approve_work_handover(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_payload is None


def test_incomplete_readback_is_unknown_not_success(target):
    _, _, worker, adapter = target
    plan = prepare_work_handover_approval(adapter, worker, _inputs())["plan"]
    adapter.list_workflows.return_value = {"items": [], "coverage": {"status": "partial"}}
    with pytest.raises(PendingActionOutcomeUnknown):
        approve_work_handover(adapter, worker, plan, enter_commit_boundary=Mock())
    adapter.list_workflows.assert_called_once()


def test_title_routing_is_dedicated_and_bounded():
    for prefix in ("", "(自动发起)"):
        assert pending_action_profile_for_title(prefix + "【HR】工作交接单-测试-离职")["profile"] == "work_handover"
    assert pending_action_profile_for_title("转发：(自动发起)【HR】工作交接单-测试") is None
    assert pending_action_profile_for_title("【HR】离职申请单-测试")["profile"] == "resignation"


def test_cap4_dom_preserves_multiple_rows_and_rejects_visible_editors():
    # Execute the actual extractor against an offline DOM, including CAP4's hidden textarea.
    from playwright.sync_api import sync_playwright
    from html import escape
    data = snapshot()
    html = []
    for field in data["fields"]:
        value = escape(field["value"])
        if field["id"] == "field0008_id":
            value = '<div class="cap4-radio__item"><i class="cap4-radio-xuanzhong"></i><span class="cap4-radio__text">离职</span></div><div class="cap4-radio__item">其他</div>'
        html.append(f'<tr data-record-id="{field["record_id"]}"><td><div id="{field["id"]}"><section class="is-none"><div class="field-content-wrapper">{value}</div><textarea style="visibility:hidden">ignored duplicate</textarea></section></div></td></tr>')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('<table>' + ''.join(html) + '</table>')
            actual = page.evaluate(WORK_HANDOVER_SNAPSHOT_SCRIPT)
            assert actual == data
            page.eval_on_selector('textarea', "element => element.style.visibility = 'visible'")
            assert page.evaluate(WORK_HANDOVER_SNAPSHOT_SCRIPT)["browse_only"] is False
        finally:
            browser.close()
