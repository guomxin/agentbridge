from tests.support.pending_execution_clock import single_readback_attempt
"""Synthetic coverage for the reward form's read-only inform-node contract."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from agentbridge.adapters.seeyon_intellectual_property_reward import (
    FIELD_LABELS, FORM_APP_ID, INTELLECTUAL_PROPERTY_REWARD_SNAPSHOT_SCRIPT,
    MASTER_FIELDS, ROW_FIELDS, TEMPLATE_ID,
)
from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch, PendingActionOutcomeUnknown,
    acknowledge_intellectual_property_reward, pending_action_profile_for_title,
    preflight_pending_action, prepare_intellectual_property_reward_acknowledgement,
    prepare_standard_collaboration_approval,
)
from tests.test_seeyon_pending_actions import FakeAdapter, FakePage, FakeWorker, _fixture, _inputs


PREPARE = "oa.intellectual_property_reward.acknowledgement.prepare"
COMMIT = "oa.intellectual_property_reward.acknowledge"


def snapshot():
    # All names and amounts are invented; the form identifiers are structural.
    values = {
        "field0001_id": "合成申请人", "field0002_id": "SYNTHETIC-001",
        "field0003_id": "合成部门", "field0004_id": "2026年10月22日",
        "field0005_id": "合成知识产权奖项", "field0006_id": "1000.00",
        "field0007_id": "合成业务负责人", "field0008_id": "合成奖励事由，供完整只读材料验收。",
        "field0009_id": "合成团队负责人", "field0010_id": "合成团队部门",
        "field0013_id": "合成成员已确认", "field0014_id": "合成历史审批意见",
        "field0015_id": "合成提示",
    }
    fields = [{"id": key, "value": value, "record_id": ""} for key, value in values.items()]
    for record, name, amount in (("allocation-1", "合成人员甲", "600.00"),
                                 ("allocation-2", "合成人员乙", "400.00")):
        fields.extend([{"id": "field0011_id", "value": name, "record_id": record},
                       {"id": "field0012_id", "value": amount, "record_id": record}])
    return {"browse_only": True, "fields": fields}


class RewardFrame:
    url = "http://oa.example.test/seeyon/common/cap4/form/index.html"

    def __init__(self, data):
        self.data = data

    def locator(self, selector):
        assert selector == "#field0001_id"
        return SimpleNamespace(count=lambda: 1)

    def evaluate(self, script):
        assert script == INTELLECTUAL_PROPERTY_REWARD_SNAPSHOT_SCRIPT
        return deepcopy(self.data)


class RewardPage(FakePage):
    def __init__(self, fixture, data):
        super().__init__(fixture)
        self.frames = [RewardFrame(data)]
        self.commit_calls = []

    def evaluate(self, script, argument=None):
        if isinstance(argument, dict) and "opinion" in argument:
            self.commit_calls.append({key: deepcopy(value) for key, value in argument.items() if key != "identity"})
        return super().evaluate(script, argument)


@pytest.fixture
def target():
    fixture = _fixture("standard_collaboration")
    fixture["source"]["title"] = "【知识产权】知识产权奖励审批单-合成申请"
    fixture["detail"].update(title=fixture["source"]["title"], fields=[
        {"name": name, "value": "合成可见材料"}
        for name in ("姓名", "奖项名称", "奖励事由", "团队成员确认")
    ])
    fixture["signals"].update(node_policy="inform", node_policy_name="知会", attitude_codes=[])
    fixture["signals"]["identity"].update(template_id=TEMPLATE_ID, form_app_id=FORM_APP_ID,
                                          form_record_id="reward-record-1", body_type="20")
    worker = FakeWorker(fixture)
    data = snapshot()
    worker.page = RewardPage(fixture, data)
    return fixture, data, worker, FakeAdapter(worker)


def field(data, key, record=""):
    return next(item for item in data["fields"] if item["id"] == key and item["record_id"] == record)


def prepare(adapter, worker):
    return prepare_intellectual_property_reward_acknowledgement(adapter, worker, _inputs(opinion="已阅"))


def test_specific_reward_title_routes_separately_from_declaration_and_generic_approval(target):
    fixture, _, worker, adapter = target
    assert TEMPLATE_ID == "8783733253983913697"
    assert FORM_APP_ID == "-7252470787007887384"
    for title in ("【知识产权】知识产权奖励审批单", fixture["source"]["title"]):
        route = pending_action_profile_for_title(title)
        assert route == {"profile": "intellectual_property_reward", "prepare_capability": PREPARE,
                         "commit_capability": COMMIT}
    declaration = "【知识产权】知识产权申报审批单-合成软件著作权"
    assert pending_action_profile_for_title(declaration)["profile"] == "intellectual_property_declaration"
    assert pending_action_profile_for_title("【知识产权】未注册奖励表") is None
    with pytest.raises(PendingActionContractMismatch, match="not a registered"):
        prepare_standard_collaboration_approval(adapter, worker, _inputs())
    fixture["source"]["title"] = declaration
    with pytest.raises(PendingActionContractMismatch, match="not a registered"):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("body_type", ["20", "FORM"])
def test_prepare_freezes_all_fifteen_fields_and_displays_both_allocations_without_writing(target, body_type):
    fixture, data, worker, adapter = target
    fixture["signals"]["identity"]["body_type"] = body_type
    assert set(FIELD_LABELS) == {f"field{number:04d}_id" for number in range(1, 16)}
    assert ROW_FIELDS == ("field0011_id", "field0012_id")
    prepared = prepare(adapter, worker)
    plan = prepared["plan"]
    frozen = deepcopy(plan)
    assert plan["action_contract"]["action_kind"] == "acknowledgement"
    assert plan["business_intent"] == "acknowledge_intellectual_property_reward"
    assert plan["target"]["business_snapshot"] == {
        "business_fields_browse_only": True, "fields": data["fields"]}
    assert plan["target"]["template_id"] == TEMPLATE_ID
    assert plan["target"]["form_app_id"] == FORM_APP_ID
    assert plan["target"]["form_record_id"] == "reward-record-1"
    assert plan["exact_input"] == {"opinion": "已阅"}
    summary = {item["label"]: item["value"] for item in prepared["summary"]["fields"]}
    assert summary["申请人"] == "合成申请人"
    assert summary["奖励金额"] == "1000.00"
    assert summary["奖励事由"] == field(data, "field0008_id")["value"]
    review = preflight_pending_action(adapter, worker, {"affair_id": "affair-1"},
                                     "intellectual_property_reward")["review"]
    facts = next(section for section in review["sections"] if section["id"] == "reward-information")
    assert facts["fields"] == [{"label": FIELD_LABELS[item["id"]], "value": item["value"]}
                               for item in data["fields"] if item["id"] in MASTER_FIELDS]
    table = next(section for section in review["sections"] if section["id"] == "reward-allocations")
    assert table["columns"] == [{"key": "field0011_id", "label": "奖励人姓名"},
                                {"key": "field0012_id", "label": "奖励金额明细"}]
    assert table["rows"] == [
        {"id": "allocation-1", "cells": {"field0011_id": "合成人员甲", "field0012_id": "600.00"}},
        {"id": "allocation-2", "cells": {"field0011_id": "合成人员乙", "field0012_id": "400.00"}},
    ]
    field(data, "field0008_id")["value"] = "稍后的外部变更"
    assert plan == frozen
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("change", [
    "template", "form", "record", "body", "summary", "process", "node", "node_name", "attitude",
    "editable", "missing", "unknown", "master_duplicate", "master_record", "applicant_missing",
    "reason_missing", "row_id", "row_id_type", "row_id_control", "row_duplicate", "unpaired",
    "recipient_missing", "malformed", "field_id_type", "value_type", "amount_mismatch", "no_frame", "multiple_frames",
])
def test_changed_or_incomplete_contract_is_rejected_before_prepare(target, change):
    fixture, data, worker, adapter = target
    identity = fixture["signals"]["identity"]
    if change in {"template", "form", "record", "body", "summary", "process"}:
        key = {"template": "template_id", "form": "form_app_id", "record": "form_record_id",
               "body": "body_type", "summary": "summary_id", "process": "process_id"}[change]
        identity[key] = "other" if change in {"template", "form", "body"} else ""
    elif change == "node": fixture["signals"]["node_policy"] = "approve"
    elif change == "node_name": fixture["signals"]["node_policy_name"] = "审批"
    elif change == "attitude": fixture["signals"]["attitude_codes"] = ["agree"]
    elif change == "editable": data["browse_only"] = False
    elif change == "missing": data["fields"].pop()
    elif change == "unknown": data["fields"].append({"id": "field9999_id", "value": "", "record_id": ""})
    elif change == "master_duplicate": data["fields"].append(deepcopy(data["fields"][0]))
    elif change == "master_record": field(data, "field0001_id")["record_id"] = "unexpected-row"
    elif change == "applicant_missing": field(data, "field0001_id")["value"] = " "
    elif change == "reason_missing": field(data, "field0008_id")["value"] = ""
    elif change in {"row_id", "row_id_type", "row_id_control"}:
        field(data, "field0011_id", "allocation-1")["record_id"] = {
            "row_id": "", "row_id_type": 123, "row_id_control": "bad\nrow"}[change]
    elif change == "row_duplicate": data["fields"].append(deepcopy(field(data, "field0011_id", "allocation-1")))
    elif change == "unpaired": field(data, "field0012_id", "allocation-1")["record_id"] = "unpaired-row"
    elif change == "recipient_missing": field(data, "field0011_id", "allocation-1")["value"] = " "
    elif change == "malformed": data["fields"].append(None)
    elif change == "field_id_type": data["fields"][0]["id"] = []
    elif change == "value_type": field(data, "field0006_id")["value"] = 1000
    elif change == "amount_mismatch": field(data, "field0012_id", "allocation-1")["value"] = "601.00"
    elif change == "no_frame": worker.page.frames = []
    elif change == "multiple_frames": worker.page.frames.append(RewardFrame(data))
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("invalid", ["", "0", "-1000", "+1000", "NaN", "Infinity", "-Infinity",
                                      "1e3", "1000.001", "1,000.00", "1000000000000", " 1000.00"])
def test_total_requires_positive_finite_bounded_currency_precision(target, invalid):
    _, data, worker, adapter = target
    field(data, "field0006_id")["value"] = invalid
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("invalid", ["", "-600", "+600", "NaN", "Infinity", "-Infinity",
                                      "6e2", "600.001", "600,00", "1000000000000", " 600.00"])
def test_allocation_requires_finite_nonnegative_currency_precision(target, invalid):
    _, data, worker, adapter = target
    field(data, "field0012_id", "allocation-1")["value"] = invalid
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


def test_decimal_allocation_reconciles_exactly_without_binary_rounding(target):
    _, data, worker, adapter = target
    field(data, "field0006_id")["value"] = "0.30"
    field(data, "field0012_id", "allocation-1")["value"] = "0.10"
    field(data, "field0012_id", "allocation-2")["value"] = "0.20"
    assert prepare(adapter, worker)["plan"]["target"]["business_snapshot"]["fields"] == data["fields"]
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("change", ["applicant", "reason", "recipient", "amounts", "allocation", "master_opinion",
                                     "existing_opinion", "node", "editable"])
def test_post_authorization_material_or_opinion_changes_stop_before_commit(target, change):
    fixture, data, worker, adapter = target
    plan = prepare(adapter, worker)["plan"]
    if change in {"applicant", "reason", "master_opinion"}:
        key = {"applicant": "field0001_id", "reason": "field0008_id", "master_opinion": "field0014_id"}[change]
        field(data, key)["value"] = "合成变更"
    elif change == "recipient": field(data, "field0011_id", "allocation-1")["value"] = "合成人员丙"
    elif change == "amounts":
        field(data, "field0006_id")["value"] = "1100.00"
        field(data, "field0012_id", "allocation-1")["value"] = "700.00"
    elif change == "allocation":
        field(data, "field0012_id", "allocation-1")["value"] = "500.00"
        field(data, "field0012_id", "allocation-2")["value"] = "500.00"
    elif change == "existing_opinion": fixture["detail"]["workflow"][0]["opinion"] = "合成新增历史意见"
    elif change == "node": fixture["signals"]["node_policy"] = "approve"
    elif change == "editable": data["browse_only"] = False
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch):
        acknowledge_intellectual_property_reward(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_calls == []
    assert worker.readback_opened is False


def test_mock_commit_submits_acknowledgement_once_and_verifies_exact_done_identity(target):
    fixture, _, worker, adapter = target
    plan = prepare(adapter, worker)["plan"]
    adapter.list_workflows = Mock(wraps=adapter.list_workflows)
    boundary = Mock()
    result = acknowledge_intellectual_property_reward(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert worker.page.commit_calls == [{"affair_id": "affair-1", "opinion": "已阅",
                                         "attitude_code": "", "action_kind": "acknowledgement"}]
    assert result["workflow_acknowledged"] is True
    assert result["action_kind"] == "acknowledgement"
    assert result["workflow_profile"] == "intellectual_property_reward"
    assert result["processed_count"] == 1
    assert "workflow_approved" not in result
    assert result["verification"] == {"confirmed": True, "method": "complete_pending_absence_and_done_identity",
                                       "reason": "verified", "pending_absence_confirmed": True,
                                       "done_identity_confirmed": True}
    assert adapter.list_workflows.call_args_list == [
        call(worker.readback_worker, collection="pending", arguments={"limit": 100}),
        call(worker.readback_worker, collection="done", arguments={"limit": 1000}),
    ]
    assert result["target"] == {"affair_id": "affair-1", "title": fixture["source"]["title"]}
    assert worker.readback_closed is True


@pytest.mark.parametrize("change,reason", [
    ("partial_pending", "pending_coverage_incomplete"), ("still_pending", "still_pending"),
    ("missing_done", "done_affair_not_found"), ("duplicate_done", "done_affair_ambiguous"),
    ("done_title", "done_title_mismatch"), ("done_affair", "done_affair_not_found"),
    ("summary_id", "done_identity_mismatch"), ("process_id", "done_identity_mismatch"),
    ("template_id", "done_identity_mismatch"), ("form_app_id", "done_identity_mismatch"),
    ("form_record_id", "done_identity_mismatch"),
])
def test_incomplete_or_wrong_readback_is_unknown_without_second_submission(target, change, reason):
    fixture, _, worker, adapter = target
    plan = prepare(adapter, worker)["plan"]
    pending = {"items": [], "coverage": {"status": "complete", "hasMore": False}}
    done = {"items": [{**fixture["source"], **fixture["signals"]["identity"]}]}
    if change == "partial_pending": pending["coverage"]["status"] = "partial"
    elif change == "still_pending": pending["items"] = [fixture["source"]]
    elif change == "missing_done": done["items"] = []
    elif change == "duplicate_done": done["items"].append(deepcopy(done["items"][0]))
    elif change == "done_title": done["items"][0]["title"] = "另一条合成奖励单"
    elif change == "done_affair": done["items"][0]["affair_id"] = "different-affair"
    else: done["items"][0][change] = "different-identity"
    adapter.list_workflows = Mock(side_effect=lambda *_args, collection, **_kwargs: pending if collection == "pending" else done)
    boundary = Mock()
    with single_readback_attempt():
        with pytest.raises(PendingActionOutcomeUnknown, match=reason):
            acknowledge_intellectual_property_reward(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert len(worker.page.commit_calls) == 1
    assert worker.readback_closed is True
    expected_collections = ["pending"] if change in {"partial_pending", "still_pending"} else ["pending", "done"]
    assert [entry.kwargs["collection"] for entry in adapter.list_workflows.call_args_list] == expected_collections


def test_new_permission_is_independent_and_does_not_expand_existing_grants(tmp_path):
    from agentbridge.core.business_draft_profiles import BY_PREPARE
    from agentbridge.core.user_grants import CAPABILITY_PERMISSIONS, UserGrants
    from agentbridge.core.write_catalog import capability_required_scopes

    grants = UserGrants(tmp_path / "grants.db")
    existing = ["oa.workflow.read", "oa.intellectual_property_declaration.approve",
                "oa.quarterly_performance.acknowledge", "oa.standard_collaboration.approve"]
    grants.save("reader", existing, expected_revision=0, actor="test", reason="Existing permissions only")
    for capability in (PREPARE, COMMIT):
        assert CAPABILITY_PERMISSIONS[capability] == COMMIT
        with pytest.raises(PermissionError):
            grants.require_capability("reader", capability)
        assert capability_required_scopes(capability) == frozenset({"oa:write:approval"})
    assert grants.get("reader")["permissions"] == sorted(existing)
    grants.save("reader", [*existing, COMMIT], expected_revision=1, actor="test", reason="Explicit new permission")
    grants.require_capability("reader", PREPARE)
    grants.require_capability("reader", COMMIT)
    profile = BY_PREPARE[PREPARE]
    assert profile.action_kind == "acknowledgement"
    assert profile.review_kind == "intellectual_property_reward"
    assert profile.commit == COMMIT
