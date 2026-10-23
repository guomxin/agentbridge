"""Synthetic coverage for the verified business-settlement approval contract."""
from copy import deepcopy
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from agentbridge.adapters.seeyon_business_settlement import (
    BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT, EXPENSE_FIELDS, FIELD_LABELS, FORM_APP_ID,
    MASTER_FIELDS, PAYMENT_FIELDS, TEMPLATE_ID, business_settlement_business_snapshot,
    business_settlement_summary_fields,
)
from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch, PendingActionOutcomeUnknown,
    approve_business_settlement, pending_action_profile_for_title,
    preflight_pending_action, prepare_business_settlement_approval,
    prepare_standard_collaboration_approval,
)
from tests.test_seeyon_pending_actions import FakeAdapter, FakePage, FakeWorker, _fixture, _inputs


PREPARE = "oa.business_settlement.approval.prepare"
COMMIT = "oa.business_settlement.approve"
TITLE = "【报销】业务结算单-示例申请人-2026-10-22-报销并付款-155.00-个人账户-示例收款人"
ATTACHMENT_FIELDS = {"field0009_id", "field0017_id"}


def snapshot():
    # No production personal data or account identifiers are used in this fixture.
    values = {
        "field0001_id": "SYNTHETIC-SETTLEMENT-001", "field0002_id": "示例申请人",
        "field0003_id": "SYNTHETIC-EMPLOYEE-001", "field0004_id": "示例发起部门",
        "field0005_id": "2026年10月22日", "field0006_id": "报销并付款",
        "field0007_id": "示例费用部门", "field0032_id": "示例结算实体",
        "field0067_id": "示例项目事项", "field0068_id": "SYNTHETIC-FINANCE-001",
        "field0008_id": "", "field0009_id": "", "field0022_id": "155.00",
        "field0023_id": "壹佰伍拾伍元整", "field0036_id": "示例稽核会计", "field0021_id": "",
        "field0024_id": "个人账户", "field0025_id": "示例收款人",
        "field0026_id": "0000000000000000001", "field0027_id": "示例开户行",
        "field0028_id": "合成测试备注", "field0118_id": "", "field0029_id": "合成历史审批意见",
    }
    fields = [{"id": key, "value": value, "record_id": ""} for key, value in values.items()]
    for record, amount, description, attachment_ids in (
        ("expense-1", "100.00", "合成办公用品", ["101", "102"]),
        ("expense-2", "55.00", "合成运费", ["-103"]),
    ):
        row = {
            "field0013_id": "2026-10-21", "field0010_id": description,
            "field0012_id": "合成费用小类", "field0057_id": "合成费用子类",
            "field0011_id": "合成发票费用名称", "field0015_id": amount,
            "field0016_id": "", "field0017_id": " ".join(f"合成发票-{value}.pdf" for value in attachment_ids),
        }
        fields.extend({"id": key, "value": value, "record_id": record} for key, value in row.items())
        fields[-1].update(attachment_ids=attachment_ids, attachment_count=len(attachment_ids))
    fields.extend({"id": key, "value": "", "record_id": "payment-1"} for key in (
        "field0109_id", "field0110_id", "field0111_id", "field0116_id", "field0112_id", "field0113_id"))
    next(item for item in fields if item["id"] == "field0009_id").update(attachment_ids=[], attachment_count=0)
    return {"browse_only": True, "fields": fields}


class SettlementFrame:
    url = "http://oa.example.test/seeyon/common/cap4/form/index.html"

    def __init__(self, data):
        self.data = data

    def locator(self, selector):
        assert selector == "#field0001_id"
        return SimpleNamespace(count=lambda: 1)

    def evaluate(self, script):
        assert script == BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT
        return deepcopy(self.data)


class SettlementPage(FakePage):
    def __init__(self, fixture, data):
        super().__init__(fixture)
        self.frames = [SettlementFrame(data)]
        self.commit_calls = []

    def evaluate(self, script, argument=None):
        if isinstance(argument, dict) and "opinion" in argument:
            self.commit_calls.append({key: deepcopy(value) for key, value in argument.items() if key != "identity"})
        return super().evaluate(script, argument)


@pytest.fixture
def target():
    fixture = _fixture("standard_collaboration")
    fixture["source"]["title"] = TITLE
    fixture["detail"].update(title=TITLE, fields=[{"name": name, "value": "合成可见材料"}
        for name in ("流水号", "姓名", "结算方式", "应付金额合计", "账户类型", "收款账户名称", "收款账号", "收款账户开户行")])
    fixture["signals"].update(node_policy="报销审批", node_policy_name="报销审批",
                               attitude_codes=["agree", "disagree"])
    fixture["signals"]["identity"].update(template_id=TEMPLATE_ID, form_app_id=FORM_APP_ID,
        form_record_id="settlement-record-1", body_type="20")
    worker, data = FakeWorker(fixture), snapshot()
    worker.page = SettlementPage(fixture, data)
    return fixture, data, worker, FakeAdapter(worker)


def field(data, key, record=""):
    return next(item for item in data["fields"] if item["id"] == key and item["record_id"] == record)


def prepare(adapter, worker):
    return prepare_business_settlement_approval(adapter, worker, _inputs())


def test_title_routes_only_the_dedicated_settlement_contract(target):
    fixture, _, worker, adapter = target
    assert TEMPLATE_ID == "8886783585203274099"
    assert FORM_APP_ID == "-1826745395617707257"
    for title in ("【报销】业务结算单-合成申请", TITLE):
        assert pending_action_profile_for_title(title) == {
            "profile": "business_settlement", "prepare_capability": PREPARE, "commit_capability": COMMIT}
    for title in ("转发：" + TITLE, "【报销】未注册结算单", "【报销】业务结算单", "【报销】业务结算单据"):
        assert pending_action_profile_for_title(title) is None
    assert pending_action_profile_for_title("【报销】差旅费审批报销单-合成申请")["profile"] == "travel_expense"
    with pytest.raises(PendingActionContractMismatch):
        prepare_standard_collaboration_approval(adapter, worker, _inputs())
    fixture["source"]["title"] = "【报销】差旅费审批报销单-合成申请"
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("body_type", ["20", "FORM"])
def test_prepare_freezes_all_45_instances_and_displays_full_amount_account_and_rows(target, body_type):
    fixture, data, worker, adapter = target
    fixture["signals"]["identity"]["body_type"] = body_type
    assert len(FIELD_LABELS) == 37 and len(data["fields"]) == 45
    assert set(MASTER_FIELDS) | set(EXPENSE_FIELDS) | set(PAYMENT_FIELDS) == set(FIELD_LABELS)
    assert len(MASTER_FIELDS) == 23 and len(EXPENSE_FIELDS) == 8 and len(PAYMENT_FIELDS) == 6
    prepared = prepare(adapter, worker)
    plan = prepared["plan"]
    frozen = deepcopy(plan)
    assert plan["action_contract"]["action_kind"] == "approval"
    assert plan["action_contract"]["version"] == "seeyon-business-settlement-approval-v1"
    assert plan["target"]["business_snapshot"] == {"business_fields_browse_only": True, "fields": data["fields"]}
    assert plan["target"]["template_id"] == TEMPLATE_ID
    assert plan["target"]["form_app_id"] == FORM_APP_ID
    assert plan["target"]["form_record_id"] == "settlement-record-1"
    assert plan["exact_input"] == {"opinion": "同意"}
    summary = prepared["summary"]["fields"]
    for item in business_settlement_summary_fields(plan["target"]["business_snapshot"]):
        assert item in summary
    for value in ("155.00", "100.00", "55.00", "0000000000000000001", "示例收款人", "示例开户行",
                  "合成办公用品", "合成运费", "合成发票-101.pdf 合成发票-102.pdf", "合成发票--103.pdf"):
        assert any(item["value"] == value for item in summary), value
    review = preflight_pending_action(adapter, worker, {"affair_id": "affair-1"}, "business_settlement")["review"]
    for row_fields, record_ids in ((EXPENSE_FIELDS, ["expense-1", "expense-2"]), (PAYMENT_FIELDS, ["payment-1"])):
        table = next(section for section in review["sections"]
                     if {column["key"] for column in section.get("columns", [])} == set(row_fields))
        assert table["columns"] == [{"key": key, "label": FIELD_LABELS[key]} for key in row_fields]
        assert [row["id"] for row in table["rows"]] == record_ids
        for row in table["rows"]:
            assert set(row["cells"]) == set(row_fields)
            for key, value in row["cells"].items():
                expected = field(data, key, row["id"])["value"]
                assert value == expected or not expected and value == "（空）"
    field(data, "field0026_id")["value"] = "0000000000000000002"
    field(data, "field0017_id", "expense-1")["attachment_ids"][0] = "999"
    assert plan == frozen
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("account", ["个人账户", "他人/对公账户"])
@pytest.mark.parametrize("payment_option", ["", "一次性结算"])
@pytest.mark.parametrize("difference", ["", "0", "0.00"])
def test_verified_read_only_variants_are_supported(target, account, payment_option, difference):
    _, data, worker, adapter = target
    field(data, "field0024_id")["value"] = account
    field(data, "field0118_id")["value"] = payment_option
    field(data, "field0016_id", "expense-1")["value"] = difference
    assert prepare(adapter, worker)["plan"]["target"]["business_snapshot"]["fields"] == data["fields"]
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("change", [
    "template", "form", "record", "body", "summary", "process", "node", "node_name", "attitude_missing", "attitude_extra",
    "editable", "missing", "unknown", "master_duplicate", "master_record", "applicant_missing", "account_missing",
    "row_id", "row_id_type", "row_id_control", "row_duplicate", "unpaired", "malformed", "field_id_type", "value_type",
    "amount_mismatch", "no_frame", "multiple_frames", "missing_expense", "missing_payment", "row_identity_collision",
    "settlement_method", "account_type", "batch_payment", "unknown_payment_option", "audit_difference", "linked_process",
])
def test_changed_or_incomplete_contract_is_rejected_before_prepare(target, change):
    fixture, data, worker, adapter = target
    if change in {"template", "form", "record", "body", "summary", "process"}:
        key = {"template": "template_id", "form": "form_app_id", "record": "form_record_id", "body": "body_type",
               "summary": "summary_id", "process": "process_id"}[change]
        fixture["signals"]["identity"][key] = "other" if change in {"template", "form", "body"} else ""
    elif change == "node": fixture["signals"]["node_policy"] = "approve"
    elif change == "node_name": fixture["signals"]["node_policy_name"] = "审批"
    elif change == "attitude_missing": fixture["signals"]["attitude_codes"] = ["agree"]
    elif change == "attitude_extra": fixture["signals"]["attitude_codes"].append("unknown")
    elif change == "editable": data["browse_only"] = False
    elif change == "missing": data["fields"].pop()
    elif change == "unknown": data["fields"].append({"id": "field9999_id", "value": "", "record_id": ""})
    elif change == "master_duplicate": data["fields"].append(deepcopy(data["fields"][0]))
    elif change == "master_record": field(data, "field0001_id")["record_id"] = "unexpected-row"
    elif change == "applicant_missing": field(data, "field0002_id")["value"] = " "
    elif change == "account_missing": field(data, "field0026_id")["value"] = ""
    elif change in {"row_id", "row_id_type", "row_id_control"}:
        field(data, "field0010_id", "expense-1")["record_id"] = {"row_id": "", "row_id_type": 123, "row_id_control": "bad\nrow"}[change]
    elif change == "row_duplicate": data["fields"].append(deepcopy(field(data, "field0010_id", "expense-1")))
    elif change == "unpaired": field(data, "field0015_id", "expense-1")["record_id"] = "unpaired-row"
    elif change == "malformed": data["fields"].append(None)
    elif change == "field_id_type": data["fields"][0]["id"] = []
    elif change == "value_type": field(data, "field0022_id")["value"] = 155
    elif change == "amount_mismatch": field(data, "field0015_id", "expense-1")["value"] = "101.00"
    elif change == "no_frame": worker.page.frames = []
    elif change == "multiple_frames": worker.page.frames.append(SettlementFrame(data))
    elif change in {"missing_expense", "missing_payment"}:
        absent = EXPENSE_FIELDS if change == "missing_expense" else PAYMENT_FIELDS
        data["fields"] = [item for item in data["fields"] if item["id"] not in absent]
    elif change == "row_identity_collision":
        for item in data["fields"]:
            if item["id"] in PAYMENT_FIELDS: item["record_id"] = "expense-1"
    elif change == "settlement_method": field(data, "field0006_id")["value"] = "其他"
    elif change == "account_type": field(data, "field0024_id")["value"] = "其他账户"
    elif change == "batch_payment": field(data, "field0118_id")["value"] = "分批付款"
    elif change == "unknown_payment_option": field(data, "field0118_id")["value"] = "未知选项"
    elif change == "audit_difference": field(data, "field0016_id", "expense-1")["value"] = "1.00"
    elif change == "linked_process": field(data, "field0008_id")["value"] = "未验证的关联流程"
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("key", ["field0109_id", "field0110_id", "field0111_id", "field0116_id", "field0112_id", "field0113_id"])
def test_any_existing_payment_value_requires_a_different_contract(target, key):
    _, data, worker, adapter = target
    field(data, key, "payment-1")["value"] = "1.00" if key == "field0110_id" else "合成已填付款信息"
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("key,record", [("field0022_id", ""), ("field0015_id", "expense-1")])
@pytest.mark.parametrize("invalid", ["", "-100", "+100", "NaN", "Infinity", "1e2", "100.001", "1,000.00", "1000000000000", " 100.00"])
def test_amounts_require_finite_bounded_currency_precision(target, key, record, invalid):
    _, data, worker, adapter = target
    field(data, key, record)["value"] = invalid
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


def test_zero_total_is_rejected_and_decimal_addition_reconciles_exactly(target):
    _, data, worker, adapter = target
    for key, record in (("field0022_id", ""), ("field0015_id", "expense-1"), ("field0015_id", "expense-2")):
        field(data, key, record)["value"] = "0.00"
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    field(data, "field0022_id")["value"] = "0.30"
    field(data, "field0015_id", "expense-1")["value"] = "0.10"
    field(data, "field0015_id", "expense-2")["value"] = "0.20"
    assert prepare(adapter, worker)["plan"]["target"]["business_snapshot"]["fields"] == data["fields"]
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("change", ["missing_ids", "missing_count", "bad_ids", "empty_ids", "duplicate_ids", "count_mismatch", "invalid_count", "invalid_id", "empty_name"])
def test_attachment_display_requires_matching_stable_identity(target, change):
    _, data, worker, adapter = target
    item = field(data, "field0017_id", "expense-1")
    if change == "missing_ids": item.pop("attachment_ids")
    elif change == "missing_count": item.pop("attachment_count")
    elif change == "bad_ids": item["attachment_ids"] = "101,102"
    elif change == "empty_ids": item["attachment_ids"] = []
    elif change == "duplicate_ids": item["attachment_ids"] = ["101", "101"]
    elif change == "count_mismatch": item["attachment_count"] = 1
    elif change == "invalid_count": item["attachment_count"] = "2"
    elif change == "invalid_id": item["attachment_ids"] = ["invalid", "102"]
    elif change == "empty_name": item["value"] = ""
    with pytest.raises(PendingActionContractMismatch):
        prepare(adapter, worker)
    assert worker.page.commit_calls == []


@pytest.mark.parametrize("change", ["account", "recipient", "reason", "amounts", "allocation", "attachment_name", "attachment_identity", "master_opinion", "existing_opinion", "node", "editable"])
def test_material_changes_after_authorization_stop_before_commit(target, change):
    fixture, data, worker, adapter = target
    plan = prepare(adapter, worker)["plan"]
    if change in {"account", "recipient", "reason", "master_opinion"}:
        key = {"account": "field0026_id", "recipient": "field0025_id", "reason": "field0028_id", "master_opinion": "field0029_id"}[change]
        field(data, key)["value"] = "合成变更"
    elif change == "amounts":
        field(data, "field0022_id")["value"] = "156.00"
        field(data, "field0015_id", "expense-1")["value"] = "101.00"
    elif change == "allocation":
        field(data, "field0015_id", "expense-1")["value"] = "101.00"
        field(data, "field0015_id", "expense-2")["value"] = "54.00"
    elif change == "attachment_name": field(data, "field0017_id", "expense-1")["value"] = "替换附件.pdf"
    elif change == "attachment_identity": field(data, "field0017_id", "expense-1")["attachment_ids"] = ["901", "902"]
    elif change == "existing_opinion": fixture["detail"]["workflow"][0]["opinion"] = "合成新增历史意见"
    elif change == "node": fixture["signals"]["node_policy"] = "approve"
    elif change == "editable": data["browse_only"] = False
    boundary = Mock()
    with pytest.raises(PendingActionContractMismatch):
        approve_business_settlement(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_not_called()
    assert worker.page.commit_calls == []
    assert worker.readback_opened is False


def test_mock_commit_submits_once_and_verifies_exact_done_identity(target):
    fixture, _, worker, adapter = target
    plan = prepare(adapter, worker)["plan"]
    adapter.list_workflows = Mock(wraps=adapter.list_workflows)
    boundary = Mock()
    result = approve_business_settlement(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert worker.page.commit_calls == [{"affair_id": "affair-1", "opinion": "同意", "attitude_code": "agree", "action_kind": "approval"}]
    assert result["workflow_approved"] is True
    assert result["action_kind"] == "approval"
    assert result["workflow_profile"] == "business_settlement"
    assert result["processed_count"] == 1
    assert result["verification"] == {"confirmed": True, "method": "complete_pending_absence_and_done_identity",
        "reason": "verified", "pending_absence_confirmed": True, "done_identity_confirmed": True}
    assert adapter.list_workflows.call_args_list == [
        call(worker.readback_worker, collection="pending", arguments={"limit": 100}),
        call(worker.readback_worker, collection="done", arguments={"limit": 1000})]
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
    elif change == "done_title": done["items"][0]["title"] = "另一条合成业务结算单"
    elif change == "done_affair": done["items"][0]["affair_id"] = "different-affair"
    else: done["items"][0][change] = "different-identity"
    adapter.list_workflows = Mock(side_effect=lambda *_args, collection, **_kwargs: pending if collection == "pending" else done)
    boundary = Mock()
    with patch("agentbridge.adapters.seeyon_pending_execution.time.monotonic", side_effect=[0, 0, 0, 0, 61]):
        with pytest.raises(PendingActionOutcomeUnknown, match=reason):
            approve_business_settlement(adapter, worker, plan, enter_commit_boundary=boundary)
    boundary.assert_called_once()
    assert len(worker.page.commit_calls) == 1
    assert worker.readback_closed is True


def test_new_permission_does_not_expand_existing_approval_grants(tmp_path):
    from agentbridge.core.business_draft_profiles import BY_PREPARE
    from agentbridge.core.user_grants import CAPABILITY_PERMISSIONS, UserGrants
    from agentbridge.core.write_catalog import capability_required_scopes

    grants = UserGrants(tmp_path / "grants.db")
    existing = ["oa.workflow.read", "oa.travel_expense.approve", "oa.standard_collaboration.approve"]
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
    assert profile.action_kind == "approval"
    assert profile.review_kind == "business_settlement"
    assert profile.commit == COMMIT


def test_offline_cap4_extractor_keeps_rows_selected_choices_and_attachment_identity():
    from playwright.sync_api import sync_playwright

    data = snapshot()
    html = []
    choices = {
        "field0006_id": ("报销并付款", "预付款"),
        "field0024_id": ("个人账户", "他人/对公账户"),
        "field0118_id": ("一次性结算", "分批付款"),
    }
    for item in data["fields"]:
        kind, value = "", escape(item["value"])
        if item["id"] in choices:
            kind = " cap4-radio"
            value = "".join('<div class="cap4-radio__item">'
                + ('<i class="cap4-radio-xuanzhong"></i>' if choice == item["value"] else '')
                + f'<span class="cap4-radio__text">{escape(choice)}</span></div>'
                for choice in choices[item["id"]])
        elif item["id"] in ATTACHMENT_FIELDS:
            kind = " cap4-attach"
            value = " ".join(f'<span class="cap4-attach__att">{escape(name)}</span>'
                              for name in item["value"].split())
            if item["attachment_ids"]:
                value += '<a class="cap4-attach-downall" href="/seeyon/rest/attachment/file/batchDownload/' + ','.join(item["attachment_ids"]) + '"></a>'
        html.append(f'<tr data-record-id="{item["record_id"]}"><td><div id="{item["id"]}">'
            f'<section class="is-none{kind}"><div class="field-content-wrapper">{value}</div>'
            '<textarea style="visibility:hidden">hidden duplicate</textarea></section></div></td></tr>')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content('<table>' + ''.join(html) + '</table>')
            extracted = page.evaluate(BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT)
            assert extracted == data
            assert business_settlement_business_snapshot(SimpleNamespace(frames=[SettlementFrame(extracted)])) == {
                "business_fields_browse_only": True, "fields": data["fields"]}

            page.locator('#field0118_id .cap4-radio__item').evaluate_all(
                "items => items.forEach(item => item.insertAdjacentHTML('afterbegin', '<i class=cap4-radio-xuanzhong></i>'))")
            ambiguous = page.evaluate(BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT)
            with pytest.raises(ValueError):
                business_settlement_business_snapshot(SimpleNamespace(frames=[SettlementFrame(ambiguous)]))
            page.locator('#field0118_id .cap4-radio-xuanzhong').evaluate_all("icons => icons.forEach(icon => icon.remove())")

            page.locator('#field0017_id .cap4-attach-downall').first.evaluate("link => link.removeAttribute('href')")
            missing_identity = page.evaluate(BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT)
            with pytest.raises(ValueError):
                business_settlement_business_snapshot(SimpleNamespace(frames=[SettlementFrame(missing_identity)]))

            page.locator('textarea').first.evaluate("element => element.style.visibility = 'visible'")
            assert page.evaluate(BUSINESS_SETTLEMENT_SNAPSHOT_SCRIPT)["browse_only"] is False
        finally:
            browser.close()
