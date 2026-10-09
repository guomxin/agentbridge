"""Typed declarations must retain the frozen Taihua and Smartlight contracts."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentbridge.adapters import smartlight, taihua
from agentbridge.core import write_catalog
from agentbridge.core.planning_policy import planning_descriptor
from agentbridge.core.user_grants import CAPABILITY_PERMISSIONS, PERMISSIONS
from agentbridge.core.write_workflow import WriteWorkflowDefinition


WORKFLOW = taihua.TAIHUA_WORK_LOG_CREATE_WORKFLOW
CONTRACT = json.loads((Path(__file__).parent / "fixtures/taihua_work_log_contract.json").read_text(encoding="utf-8"))
SMARTLIGHT_CONTRACT = json.loads((Path(__file__).parent / "fixtures/smartlight_alarm_remark_contract.json").read_text(encoding="utf-8"))
SMARTLIGHT_ACTION_CONTRACT = json.loads((Path(__file__).parent / "fixtures/smartlight_alarm_actions_contract.json").read_text(encoding="utf-8"))
ACTION_CONTRACTS = SMARTLIGHT_ACTION_CONTRACT["workflows"]
DECLARATIONS = [
    (taihua, "TAIHUA_WORK_LOG_CREATE_WORKFLOW"),
    (smartlight, "SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW"),
    *[(smartlight, item["declaration"]) for item in ACTION_CONTRACTS],
]


def _action_definition(contract):
    expected = deepcopy(contract["definition"])
    expected["context_fields"] = tuple(expected["context_fields"])
    for field in ("contract_error", "outcome_error"):
        qualified_name = expected[field]
        error = getattr(smartlight, qualified_name.rsplit(".", 1)[1])
        assert f"{error.__module__}.{error.__qualname__}" == qualified_name
        expected[field] = error
    return expected


def test_taihua_capabilities_and_field_schema_match_pre_refactor_contract():
    registry = taihua.build_taihua_capability_registry()
    assert [registry.get(spec["name"]).to_dict() for spec in CONTRACT["capabilities"]] == CONTRACT["capabilities"]
    assert [spec.to_dict() for spec in WORKFLOW.capability_specs()] == CONTRACT["capabilities"]
    assert WORKFLOW.legacy_definition()["field_schema"] == CONTRACT["field_schema"]
    assert "required" not in registry.get("taihua.work_log.create.prepare").input_schema


def test_taihua_legacy_catalog_and_scopes_preserve_runtime_binding_contract():
    expected = {
        "commit_capability": "taihua.work_log.create",
        "field_schema": CONTRACT["field_schema"],
        "context_fields": (),
        "prepare_function": "prepare_taihua_work_log_create",
        "commit_function": "commit_taihua_work_log_create",
        "contract_error": taihua.TaihuaWorkLogContractMismatch,
        "outcome_error": taihua.TaihuaWorkLogOutcomeUnknown,
        "field_message": "工作日志字段必须在可信字段卡中核对。",
        "authorization_message": "工作日志提交计划需要在可信授权卡中确认。",
    }
    assert write_catalog._TRUSTED_WRITE_DEFINITIONS["taihua.work_log.create.prepare"] == expected
    assert write_catalog._TRUSTED_WRITE_COMMITS["taihua.work_log.create"] == (
        "taihua.work_log.create.prepare", expected,
    )
    for name in ("taihua.work_log.create.prepare", "taihua.work_log.create"):
        assert write_catalog.capability_required_scopes(name) == frozenset({"taihua:write:worklog"})
        assert CAPABILITY_PERMISSIONS[name] == "taihua.work_log.create"
    assert WORKFLOW.prepare_function is taihua.prepare_taihua_work_log_create
    assert WORKFLOW.commit_function is taihua.commit_taihua_work_log_create


@pytest.mark.parametrize("adapter, declaration_name", DECLARATIONS)
def test_legacy_function_bridge_remains_late_bound_for_fault_injection(adapter, declaration_name):
    declaration = getattr(adapter, declaration_name)
    definition = declaration.legacy_definition()
    for field in ("prepare_function", "commit_function"):
        name = definition[field]
        with patch.object(write_catalog, name) as injected:
            assert write_catalog.resolve_write_function(name) is injected
        assert write_catalog.resolve_write_function(name) is getattr(adapter, name)
    assert write_catalog.resolve_write_function("legacy_definition") is None


def test_permissions_and_planning_keep_prepare_public_and_commit_private():
    permission = PERMISSIONS["taihua.work_log.create"]
    assert permission["tools"] == ["taihua_work_log_create_prepare"]
    assert permission["private_tools"] == ["taihua_work_log_create"]
    assert frozenset(permission["legacy_scopes"]) == WORKFLOW.required_scopes
    assert planning_descriptor("taihua.work_log.create.prepare") == {
        "schemaVersion": "agentbridge.planning-descriptor.v1",
        "capabilityName": "taihua.work_log.create.prepare",
        "mcpToolName": "taihua_work_log_create_prepare",
        "roles": ["write_sink"],
        "inputProvenance": {
            "content": "user_or_bound_transform", "hours": "user_decision",
            "log_date": "user_constraint", "project": "user_decision",
        },
    }
    assert planning_descriptor("taihua.work_log.create") is None


def test_schema_sources_and_repeated_projections_are_isolated():
    source_prepare, source_commit = WORKFLOW.capability_specs()
    source_fields = deepcopy(CONTRACT["field_schema"])
    declaration = replace(WORKFLOW, prepare_spec=source_prepare, commit_spec=source_commit, field_schema=source_fields)
    source_prepare.input_schema["properties"]["content"]["type"] = "array"
    source_commit.output_schema["type"] = "array"
    source_fields["fields"][0]["label"] = "changed outside"

    first_prepare, first_commit = declaration.capability_specs()
    first_prepare.input_schema["properties"]["hours"]["type"] = "string"
    first_prepare.output_schema["type"] = "array"
    first_commit.input_schema["required"].clear()
    first_commit.output_schema["type"] = "array"
    first_definition = declaration.legacy_definition()
    first_definition["field_schema"]["fields"][0]["label"] = "changed projection"
    first_scopes = declaration.scope_bindings()
    first_scopes.clear()

    assert [spec.to_dict() for spec in declaration.capability_specs()] == CONTRACT["capabilities"]
    assert declaration.legacy_definition()["field_schema"] == CONTRACT["field_schema"]
    assert set(declaration.scope_bindings()) == {"taihua.work_log.create.prepare", "taihua.work_log.create"}
    assert taihua.TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA == CONTRACT["field_schema"]
    assert write_catalog._TRUSTED_WRITE_DEFINITIONS["taihua.work_log.create.prepare"]["field_schema"] == CONTRACT["field_schema"]


@pytest.mark.parametrize("changes", [
    {"prepare_spec": None},
    {"commit_spec": WORKFLOW.prepare_spec},
    {"commit_spec": replace(WORKFLOW.commit_spec, name="other.work_log.create")},
    {"commit_spec": replace(WORKFLOW.commit_spec, adapter="other-adapter")},
    {"prepare_spec": replace(WORKFLOW.prepare_spec, effect="read")},
    {"required_scopes": frozenset()},
    {"required_scopes": {"taihua:write:worklog"}},
    {"required_scopes": frozenset({" "})},
    {"required_scopes": frozenset({3})},
    {"field_schema": []},
    {"context_fields": ("missing",)},
    {"context_fields": ("content", "content")},
    {"prepare_function": None},
    {"commit_function": lambda *args, **kwargs: {}},
    {"contract_error": RuntimeError("an instance")},
    {"outcome_error": BaseException},
    {"field_message": ""},
    {"authorization_message": None},
])
def test_invalid_write_declarations_fail_before_runtime_registration(changes):
    with pytest.raises((TypeError, ValueError)):
        replace(WORKFLOW, **changes)


def test_smartlight_capabilities_and_field_card_match_frozen_contract():
    declaration = smartlight.SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW
    registry = smartlight.build_smartlight_capability_registry()
    assert [registry.get(spec["name"]).to_dict() for spec in SMARTLIGHT_CONTRACT["capabilities"]] == SMARTLIGHT_CONTRACT["capabilities"]
    assert [spec.to_dict() for spec in declaration.capability_specs()] == SMARTLIGHT_CONTRACT["capabilities"]
    assert declaration.legacy_definition()["field_schema"] == SMARTLIGHT_CONTRACT["field_schema"]
    assert declaration.context_fields == ("alarm_id",)
    assert declaration.prepare_spec.input_schema["required"] == ["alarm_id"]
    assert declaration.prepare_spec.effect == declaration.commit_spec.effect == "reversible_write"
    assert declaration.prepare_function is smartlight.prepare_smartlight_alarm_remark_update
    assert declaration.commit_function is smartlight.commit_smartlight_alarm_remark_update


def test_smartlight_catalog_scopes_permissions_and_planning_match_frozen_contract():
    declaration = smartlight.SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW
    expected = deepcopy(SMARTLIGHT_CONTRACT["definition"])
    expected["context_fields"] = tuple(expected["context_fields"])
    for name, error in (
        ("contract_error", smartlight.SmartlightAlarmRemarkContractMismatch),
        ("outcome_error", smartlight.SmartlightAlarmRemarkOutcomeUnknown),
    ):
        assert f"{error.__module__}.{error.__qualname__}" == expected[name]
        expected[name] = error
    assert declaration.legacy_definition() == expected
    assert write_catalog._TRUSTED_WRITE_DEFINITIONS["smartlight.alarm.remark.update.prepare"] == expected
    assert write_catalog._TRUSTED_WRITE_COMMITS["smartlight.alarm.remark.update"] == (
        "smartlight.alarm.remark.update.prepare", expected,
    )
    assert declaration.scope_bindings() == {
        name: frozenset(scopes) for name, scopes in SMARTLIGHT_CONTRACT["scopes"].items()
    }
    for name, scopes in SMARTLIGHT_CONTRACT["scopes"].items():
        assert write_catalog.capability_required_scopes(name) == frozenset(scopes)
        assert CAPABILITY_PERMISSIONS[name] == SMARTLIGHT_CONTRACT["permissions"][name]
        assert planning_descriptor(name) == SMARTLIGHT_CONTRACT["planning"][name]
    assert PERMISSIONS["smartlight.alarm.remark.write"] == SMARTLIGHT_CONTRACT["permission"]


def test_smartlight_schema_projections_cannot_mutate_target_context_or_other_workflow():
    declaration = smartlight.SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW
    source_prepare, source_commit = declaration.capability_specs()
    source_fields = deepcopy(SMARTLIGHT_CONTRACT["field_schema"])
    isolated = replace(
        declaration, prepare_spec=source_prepare, commit_spec=source_commit,
        field_schema=source_fields,
    )
    source_prepare.input_schema["required"].clear()
    source_commit.input_schema["properties"]["authorization_id"]["type"] = "array"
    source_fields["fields"][0]["required"] = True
    projected_prepare, projected_commit = isolated.capability_specs()
    projected_prepare.input_schema["required"].clear()
    projected_commit.input_schema["required"].clear()
    isolated.legacy_definition()["field_schema"]["fields"][0]["max_length"] = 0
    isolated.scope_bindings().clear()

    assert [spec.to_dict() for spec in isolated.capability_specs()] == SMARTLIGHT_CONTRACT["capabilities"]
    assert isolated.legacy_definition()["field_schema"] == SMARTLIGHT_CONTRACT["field_schema"]
    assert isolated.context_fields == ("alarm_id",)
    assert isolated.scope_bindings() == {
        name: frozenset(scopes) for name, scopes in SMARTLIGHT_CONTRACT["scopes"].items()
    }
    assert smartlight.SMARTLIGHT_ALARM_REMARK_FIELD_CARD_SCHEMA == SMARTLIGHT_CONTRACT["field_schema"]
    assert write_catalog._TRUSTED_WRITE_DEFINITIONS["smartlight.alarm.remark.update.prepare"]["field_schema"] == SMARTLIGHT_CONTRACT["field_schema"]
    assert [spec.to_dict() for spec in WORKFLOW.capability_specs()] == CONTRACT["capabilities"]
    assert WORKFLOW.legacy_definition()["field_schema"] == CONTRACT["field_schema"]


@pytest.mark.parametrize("context_fields", [("missing_alarm",), ("alarm_id", "alarm_id"), ["alarm_id"]])
def test_smartlight_invalid_context_declarations_are_rejected(context_fields):
    with pytest.raises(ValueError):
        replace(smartlight.SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW, context_fields=context_fields)


@pytest.mark.parametrize("contract", ACTION_CONTRACTS, ids=lambda item: item["declaration"])
def test_smartlight_action_capabilities_and_no_field_card_match_frozen_contract(contract):
    declaration = getattr(smartlight, contract["declaration"])
    registry = smartlight.build_smartlight_capability_registry()
    assert [registry.get(spec["name"]).to_dict() for spec in contract["capabilities"]] == contract["capabilities"]
    assert [spec.to_dict() for spec in declaration.capability_specs()] == contract["capabilities"]
    assert declaration.context_fields == ("alarm_id",)
    assert declaration.field_schema is None
    assert declaration.field_message is None
    definition = declaration.legacy_definition()
    assert "field_schema" in definition and definition["field_schema"] is None
    assert "field_message" not in definition
    assert definition == _action_definition(contract)
    assert list(definition) == list(contract["definition"])
    assert declaration.prepare_function is getattr(smartlight, contract["definition"]["prepare_function"])
    assert declaration.commit_function is getattr(smartlight, contract["definition"]["commit_function"])


@pytest.mark.parametrize("contract", ACTION_CONTRACTS, ids=lambda item: item["declaration"])
def test_smartlight_action_catalog_permissions_scopes_and_planning_keep_frozen_contract(contract):
    declaration = getattr(smartlight, contract["declaration"])
    prepare_name, commit_name = [spec["name"] for spec in contract["capabilities"]]
    expected = _action_definition(contract)
    assert write_catalog._TRUSTED_WRITE_DEFINITIONS[prepare_name] == expected
    assert write_catalog._TRUSTED_WRITE_COMMITS[commit_name] == (prepare_name, expected)
    assert "field_message" not in write_catalog._TRUSTED_WRITE_DEFINITIONS[prepare_name]
    assert declaration.scope_bindings() == {
        name: frozenset(scopes) for name, scopes in contract["scopes"].items()
    }
    for name, scopes in contract["scopes"].items():
        assert write_catalog.capability_required_scopes(name) == frozenset(scopes)
        assert CAPABILITY_PERMISSIONS[name] == contract["permissions"][name]
        assert planning_descriptor(name) == contract["planning"][name]
    assert PERMISSIONS[contract["permission"]["id"]] == contract["permission"]


@pytest.mark.parametrize("contract", ACTION_CONTRACTS, ids=lambda item: item["declaration"])
def test_smartlight_action_source_and_projection_mutations_are_isolated(contract):
    declaration = getattr(smartlight, contract["declaration"])
    source_prepare, source_commit = declaration.capability_specs()
    isolated = replace(declaration, prepare_spec=source_prepare, commit_spec=source_commit)
    source_prepare.input_schema["properties"]["alarm_id"]["type"] = "array"
    source_prepare.input_schema["required"].clear()
    source_commit.input_schema["properties"]["authorization_id"]["type"] = "array"
    source_commit.output_schema["type"] = "array"

    projected_prepare, projected_commit = isolated.capability_specs()
    projected_prepare.input_schema["required"].clear()
    projected_prepare.output_schema["type"] = "array"
    projected_commit.input_schema["properties"]["authorization_id"]["type"] = "array"
    projected_commit.input_schema["required"].clear()
    projected_definition = isolated.legacy_definition()
    projected_definition["field_schema"] = {"fields": []}
    projected_definition["field_message"] = "projection only"
    projected_definition["context_fields"] = ()
    isolated.scope_bindings().clear()

    assert [spec.to_dict() for spec in isolated.capability_specs()] == contract["capabilities"]
    assert isolated.legacy_definition() == _action_definition(contract)
    assert isolated.scope_bindings() == {
        name: frozenset(scopes) for name, scopes in contract["scopes"].items()
    }
    for other_contract in ACTION_CONTRACTS:
        other = getattr(smartlight, other_contract["declaration"])
        assert [spec.to_dict() for spec in other.capability_specs()] == other_contract["capabilities"]
        assert other.legacy_definition() == _action_definition(other_contract)
        prepare_name = other_contract["capabilities"][0]["name"]
        assert write_catalog._TRUSTED_WRITE_DEFINITIONS[prepare_name] == _action_definition(other_contract)
    assert smartlight.SMARTLIGHT_ALARM_ACTION_PREPARE_INPUT_SCHEMA == contract["capabilities"][0]["input_schema"]
    assert smartlight.SMARTLIGHT_ALARM_ACTION_INPUT_SCHEMA == contract["capabilities"][1]["input_schema"]
    assert [spec.to_dict() for spec in WORKFLOW.capability_specs()] == CONTRACT["capabilities"]
    assert WORKFLOW.legacy_definition()["field_schema"] == CONTRACT["field_schema"]
    remark = smartlight.SMARTLIGHT_ALARM_REMARK_UPDATE_WORKFLOW
    assert [spec.to_dict() for spec in remark.capability_specs()] == SMARTLIGHT_CONTRACT["capabilities"]
    assert remark.legacy_definition()["field_schema"] == SMARTLIGHT_CONTRACT["field_schema"]


@pytest.mark.parametrize("adapter, declaration_name", DECLARATIONS)
def test_write_declaration_positional_arguments_retain_message_order(adapter, declaration_name):
    declaration = getattr(adapter, declaration_name)
    positional = WriteWorkflowDefinition(
        declaration.prepare_spec, declaration.commit_spec, declaration.required_scopes,
        declaration.field_schema, declaration.context_fields, declaration.prepare_function,
        declaration.commit_function, declaration.contract_error, declaration.outcome_error,
        declaration.field_message, declaration.authorization_message,
    )
    assert positional.legacy_definition() == declaration.legacy_definition()
    assert positional.field_message == declaration.field_message
    assert positional.authorization_message == declaration.authorization_message


@pytest.mark.parametrize("adapter, declaration_name", DECLARATIONS[:2])
@pytest.mark.parametrize("field_message", [None, "", " \t", 7])
def test_field_card_declarations_require_a_nonempty_field_message(adapter, declaration_name, field_message):
    with pytest.raises(ValueError):
        replace(getattr(adapter, declaration_name), field_message=field_message)


def test_empty_schema_object_is_not_treated_as_absent_field_card():
    with pytest.raises(ValueError):
        replace(WORKFLOW, field_schema={}, field_message=None)


def test_no_field_card_can_preserve_an_explicit_nonempty_hint():
    declaration = getattr(smartlight, ACTION_CONTRACTS[0]["declaration"])
    explicit = replace(declaration, field_message="Explicit legacy field hint.")
    definition = explicit.legacy_definition()
    assert definition["field_schema"] is None
    assert definition["field_message"] == "Explicit legacy field hint."
    assert definition["authorization_message"] == declaration.authorization_message
    assert list(definition)[-2:] == ["field_message", "authorization_message"]
    assert "field_message" not in declaration.legacy_definition()


@pytest.mark.parametrize("contract", ACTION_CONTRACTS, ids=lambda item: item["declaration"])
@pytest.mark.parametrize("field_message", ["", " \t", 7])
def test_no_field_card_declarations_reject_invalid_explicit_field_messages(contract, field_message):
    with pytest.raises(ValueError):
        replace(getattr(smartlight, contract["declaration"]), field_message=field_message)


@pytest.mark.parametrize("adapter, declaration_name", DECLARATIONS)
@pytest.mark.parametrize("authorization_message", [None, "", " \t", 7])
def test_every_write_declaration_requires_nonempty_authorization_message(adapter, declaration_name, authorization_message):
    with pytest.raises(ValueError):
        replace(getattr(adapter, declaration_name), authorization_message=authorization_message)
