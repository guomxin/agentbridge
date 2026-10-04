"""The typed declaration must retain the existing Taihua write contract."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bscli.adapters import taihua
from bscli.core import write_catalog
from bscli.core.planning_policy import planning_descriptor
from bscli.core.user_grants import CAPABILITY_PERMISSIONS, PERMISSIONS


WORKFLOW = taihua.TAIHUA_WORK_LOG_CREATE_WORKFLOW
CONTRACT = json.loads((Path(__file__).parent / "fixtures/taihua_work_log_contract.json").read_text(encoding="utf-8"))


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


def test_legacy_function_bridge_remains_late_bound_for_fault_injection():
    definition = WORKFLOW.legacy_definition()
    for field in ("prepare_function", "commit_function"):
        name = definition[field]
        with patch.object(write_catalog, name) as injected:
            assert write_catalog.resolve_write_function(name) is injected
        assert write_catalog.resolve_write_function(name) is getattr(taihua, name)
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
