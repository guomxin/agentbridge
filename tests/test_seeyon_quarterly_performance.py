from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch, PendingActionOutcomeUnknown,
    acknowledge_quarterly_performance, prepare_quarterly_performance_acknowledgement,
    prepare_standard_collaboration_approval, pending_action_profile_for_title,
)
from agentbridge.adapters.seeyon_quarterly_performance import (
    TEMPLATE_ID, FORM_APP_ID, FIELD_LABELS, ROW_FIELDS, QUARTERLY_PERFORMANCE_SNAPSHOT_SCRIPT,
)
from tests.test_seeyon_pending_actions import FakeAdapter, FakeWorker, _fixture, _inputs


def snapshot():
    values = {"field0001_id": "测试考评人", "field0002_id": "测试部门", "field0003_id": "2026年10月19日",
              "field0015_id": "10%", "field0016_id": "20%", "field0006_id": "测试员工",
              "field0007_id": "测试岗位", "field0008_id": "测试审批人", "field0010_id": "B", "field0022_id": "C"}
    return {"browse_only": True, "fields": [
        {"id": key, "value": values.get(key, ""), "record_id": "employee-1" if key in ROW_FIELDS else ""}
        for key in FIELD_LABELS]}


class PerformanceFrame:
    url = "http://oa.example.test/seeyon/common/cap4/form/index.html"

    def __init__(self, data):
        self.data = data

    def locator(self, selector):
        assert selector == "#field0001_id"
        return SimpleNamespace(count=lambda: 1)

    def evaluate(self, script):
        assert script == QUARTERLY_PERFORMANCE_SNAPSHOT_SCRIPT
        return deepcopy(self.data)


@pytest.fixture
def target():
    fixture = _fixture("standard_collaboration")
    fixture["source"]["title"] = "【绩效】部门季度绩效考核汇总-测试部门"
    fixture["detail"]["fields"] = [{"name": "等级名称", "value": "等级规则"}, {"name": "姓名", "value": "员工明细"}]
    fixture["signals"].update(node_policy="inform", node_policy_name="知会", attitude_codes=[])
    fixture["signals"]["identity"].update(template_id=TEMPLATE_ID, form_app_id=FORM_APP_ID,
                                          form_record_id="form-record-1", body_type="20")
    worker = FakeWorker(fixture)
    data = snapshot()
    worker.page.frames = [PerformanceFrame(data)]
    return fixture, data, worker, FakeAdapter(worker)


def test_specific_routing_and_no_generic_approval_fallback(target):
    fixture, _, worker, adapter = target
    for prefix in ("", "(自动发起)"):
        assert pending_action_profile_for_title(prefix + fixture["source"]["title"])["profile"] == "quarterly_performance"
    assert pending_action_profile_for_title("【绩效】未注册绩效表-测试部门") is None
    with pytest.raises(PendingActionContractMismatch, match="not a registered"):
        prepare_standard_collaboration_approval(adapter, worker, _inputs())


def test_prepare_freezes_and_displays_every_employee_without_writing(target):
    _, data, worker, adapter = target
    second = [dict(f, record_id="employee-2") for f in data["fields"] if f["id"] in ROW_FIELDS]
    data["fields"].extend(second)
    prepared = prepare_quarterly_performance_acknowledgement(adapter, worker, _inputs())
    plan = prepared["plan"]
    assert plan["action_contract"]["action_kind"] == "acknowledgement"
    assert plan["business_intent"] == "acknowledge_quarterly_performance"
    assert plan["target"]["business_snapshot"]["fields"] == data["fields"]
    from agentbridge.adapters.seeyon_pending_actions import preflight_pending_action
    review = preflight_pending_action(adapter, worker, {"affair_id":"affair-1"}, "quarterly_performance")["review"]
    table = next(s for s in review["sections"] if s["id"] == "performance-employees")
    assert len(table["rows"]) == 2
    assert len(table["columns"]) == 10
    assert table["rows"][0]["cells"]["field0022_id"] == "C"
    assert worker.page.commit_payload is None


@pytest.mark.parametrize("change", ["template", "form", "record", "body", "node", "node_name", "attitude",
    "editable", "extra", "missing", "duplicate", "row_id", "partial_row", "rating", "unknown_rating", "identity", "master_duplicate", "malformed"])
def test_contract_changes_are_blocked_before_prepare(target, change):
    fixture, data, worker, adapter = target
    if change in {"template", "form", "record", "body"}:
        key = {"template":"template_id", "form":"form_app_id", "record":"form_record_id", "body":"body_type"}[change]
        fixture["signals"]["identity"][key] = "" if change == "record" else "other"
    elif change == "node": fixture["signals"]["node_policy"] = "approve"
    elif change == "node_name": fixture["signals"]["node_policy_name"] = "审批"
    elif change == "attitude": fixture["signals"]["attitude_codes"] = ["agree"]
    elif change == "editable": data["browse_only"] = False
    elif change == "extra": data["fields"].append({"id":"field9999_id", "value":"", "record_id":""})
    elif change == "missing": data["fields"].pop()
    elif change in {"duplicate", "master_duplicate"}: data["fields"].append(deepcopy(data["fields"][-1 if change == "duplicate" else 0]))
    elif change == "row_id": next(f for f in data["fields"] if f["id"] in ROW_FIELDS)["record_id"] = ""
    elif change == "partial_row": data["fields"].append(dict(data["fields"][-1], record_id="employee-2"))
    elif change in {"rating", "unknown_rating"}: next(f for f in data["fields"] if f["id"] == "field0022_id")["value"] = "" if change == "rating" else "E"
    elif change == "identity": next(f for f in data["fields"] if f["id"] == "field0006_id")["value"] = ""
    else: data["fields"].append(None)
    with pytest.raises(PendingActionContractMismatch):
        prepare_quarterly_performance_acknowledgement(adapter, worker, _inputs())
    assert worker.page.commit_payload is None


@pytest.mark.parametrize("change", ["rating", "opinion", "employee", "new_row", "node", "editable"])
def test_post_authorization_changes_stop_before_commit_boundary(target, change):
    fixture, data, worker, adapter = target
    plan = prepare_quarterly_performance_acknowledgement(adapter, worker, _inputs())["plan"]
    if change == "editable": data["browse_only"] = False
    elif change == "node": fixture["signals"]["node_policy"] = "approve"
    elif change == "new_row": data["fields"].extend([dict(f, record_id="employee-2") for f in data["fields"] if f["id"] in ROW_FIELDS])
    else:
        key = {"rating":"field0022_id", "opinion":"field0023_id", "employee":"field0006_id"}[change]
        next(f for f in data["fields"] if f["id"] == key)["value"] = "A" if change == "rating" else "changed"
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch):
        acknowledge_quarterly_performance(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_payload is None


def test_mock_commit_is_acknowledgement_once_and_verified(target):
    _, _, worker, adapter = target
    plan = prepare_quarterly_performance_acknowledgement(adapter, worker, _inputs())["plan"]
    boundary = Mock()
    result = acknowledge_quarterly_performance(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert result["workflow_acknowledged"] is True
    assert "workflow_approved" not in result
    assert worker.page.commit_payload["attitude_code"] == ""


def test_incomplete_readback_is_unknown(target):
    _, _, worker, adapter = target
    plan = prepare_quarterly_performance_acknowledgement(adapter, worker, _inputs())["plan"]
    adapter.list_workflows = Mock(return_value={"items": [], "coverage": {"status":"partial"}})
    with patch("agentbridge.adapters.seeyon_pending_execution.time.monotonic", side_effect=[0,0,61]):
        with pytest.raises(PendingActionOutcomeUnknown, match="pending_coverage_incomplete"):
            acknowledge_quarterly_performance(adapter, worker, plan, enter_commit_boundary=Mock())
    adapter.list_workflows.assert_called_once()


def test_permission_is_independent_and_does_not_expand_existing_grants(tmp_path):
    from agentbridge.core.user_grants import UserGrants
    from agentbridge.core.business_draft_profiles import BY_PREPARE
    from agentbridge.core.write_catalog import capability_required_scopes
    prepare = "oa.quarterly_performance.acknowledgement.prepare"
    commit = "oa.quarterly_performance.acknowledge"
    grants = UserGrants(tmp_path / "grants.db")
    grants.save("reader", ["oa.workflow.read", "oa.standard_collaboration.approve", "oa.weekly_report.approve"],
                expected_revision=0, actor="test", reason="Existing permissions only")
    with pytest.raises(PermissionError):
        grants.require_capability("reader", prepare)
    with pytest.raises(PermissionError):
        grants.require_capability("reader", commit)
    grants.save("reader", ["oa.workflow.read", commit], expected_revision=1, actor="test", reason="Explicit new permission")
    grants.require_capability("reader", prepare)
    grants.require_capability("reader", commit)
    profile = BY_PREPARE[prepare]
    assert profile.action_kind == "acknowledgement"
    assert profile.commit == commit
    assert capability_required_scopes(prepare) == frozenset({"oa:write:approval"})
