"""OA meeting declarations retain their frozen contracts and lazy field builders."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bscli.adapters import seeyon_central, seeyon_meeting, seeyon_meeting_room_application
from bscli.core import write_catalog
from bscli.core.planning_policy import planning_descriptor
from bscli.core.user_grants import CAPABILITY_PERMISSIONS, PERMISSIONS
from bscli.core.write_workflow import WriteWorkflowDefinition


CONTRACT_PATH = Path(__file__).parent / "fixtures/oa_meeting_workflows_contract.json"
CONTRACT_BYTES = CONTRACT_PATH.read_bytes()
CONTRACT = json.loads(CONTRACT_BYTES)
WORKFLOWS = CONTRACT["workflows"]
DYNAMIC_WORKFLOWS = [
    item for item in WORKFLOWS if "field_schema_function" in item["definition"]
]
MODULES = {
    "seeyon_meeting": seeyon_meeting,
    "seeyon_meeting_room_application": seeyon_meeting_room_application,
}
SOURCE_SCHEMA_NAMES = {
    "MEETING_CREATE_WORKFLOW": (
        "MEETING_PREPARE_INPUT_SCHEMA",
        "MEETING_CREATE_INPUT_SCHEMA",
        "MEETING_FIELD_CARD_SCHEMA",
    ),
    "MEETING_ROOM_APPLICATION_CREATE_WORKFLOW": (
        "MEETING_ROOM_APPLICATION_PREPARE_INPUT_SCHEMA",
        "MEETING_ROOM_APPLICATION_CREATE_INPUT_SCHEMA",
        "MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA",
    ),
    "MEETING_ROOM_APPLICATION_CANCEL_WORKFLOW": (
        "MEETING_ROOM_APPLICATION_CANCEL_PREPARE_INPUT_SCHEMA",
        "MEETING_ROOM_APPLICATION_CANCEL_INPUT_SCHEMA",
        "MEETING_ROOM_APPLICATION_CANCEL_FIELD_CARD_SCHEMA",
    ),
}
POSITIONAL_FIELDS = (
    "prepare_spec", "commit_spec", "required_scopes", "field_schema",
    "context_fields", "prepare_function", "commit_function", "contract_error",
    "outcome_error", "field_message", "authorization_message",
)


def _declaration(contract):
    return getattr(MODULES[contract["module"]], contract["declaration"])


def _expected_definition(contract):
    expected = deepcopy(contract["definition"])
    expected["context_fields"] = tuple(expected["context_fields"])
    module = MODULES[contract["module"]]
    for field in ("contract_error", "outcome_error"):
        qualified_name = expected[field]
        error = getattr(module, qualified_name.rsplit(".", 1)[1])
        assert f"{error.__module__}.{error.__qualname__}" == qualified_name
        expected[field] = error
    return expected


def test_oa_meeting_fixture_is_the_frozen_pre_refactor_baseline():
    assert CONTRACT["baseline"] == "8567100cbbb3fafb371e8b193a72569f913b4d56"
    assert hashlib.sha256(CONTRACT_BYTES).hexdigest() == (
        "159cd0a178ebe7cb921a6cd61084285238b38911a76b2250249d0b2ad15226e2"
    )


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_meeting_capabilities_and_source_schemas_match_frozen_contract(contract):
    declaration = _declaration(contract)
    module = MODULES[contract["module"]]
    registry = seeyon_central.build_central_capability_registry()
    assert [spec.to_dict() for spec in declaration.capability_specs()] == contract["capabilities"]
    assert [
        registry.get(spec["name"]).to_dict() for spec in contract["capabilities"]
    ] == contract["capabilities"]
    prepare_schema, commit_schema, field_schema = (
        getattr(module, name) for name in SOURCE_SCHEMA_NAMES[contract["declaration"]]
    )
    assert prepare_schema == contract["capabilities"][0]["input_schema"]
    assert commit_schema == contract["capabilities"][1]["input_schema"]
    assert field_schema == contract["definition"]["field_schema"]
    assert declaration.prepare_spec.input_schema is not prepare_schema
    assert declaration.prepare_spec.input_schema["properties"] is not prepare_schema["properties"]
    assert declaration.commit_spec.input_schema["required"] is not commit_schema["required"]
    assert declaration.field_schema["fields"] is not field_schema["fields"]


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_meeting_catalog_preserves_complete_definition_and_key_order(contract):
    declaration = _declaration(contract)
    prepare_name, commit_name = (spec["name"] for spec in contract["capabilities"])
    expected = _expected_definition(contract)
    projected = declaration.legacy_definition()
    registered = write_catalog._TRUSTED_WRITE_DEFINITIONS[prepare_name]
    assert projected == expected
    assert registered == expected
    assert list(projected) == list(contract["definition"])
    assert list(registered) == list(contract["definition"])
    assert write_catalog._TRUSTED_WRITE_COMMITS[commit_name] == (prepare_name, expected)
    assert "preflight_function" not in projected
    assert "preflight_profile" not in projected
    module = MODULES[contract["module"]]
    for field in ("prepare_function", "commit_function", "field_schema_function"):
        expected_name = contract["definition"].get(field)
        expected_handler = getattr(module, expected_name) if expected_name else None
        assert getattr(declaration, field) is expected_handler


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_meeting_scopes_permissions_and_planning_match_frozen_contract(contract):
    declaration = _declaration(contract)
    assert declaration.scope_bindings() == {
        name: frozenset(scopes) for name, scopes in contract["scopes"].items()
    }
    for name, scopes in contract["scopes"].items():
        assert write_catalog.capability_required_scopes(name) == frozenset(scopes)
        assert CAPABILITY_PERMISSIONS[name] == contract["permissions"][name]
        assert planning_descriptor(name) == contract["planning"][name]
    for permission, expected in contract["permission_definitions"].items():
        assert PERMISSIONS[permission] == expected


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_meeting_function_bridges_remain_late_bound(contract):
    definition = write_catalog._TRUSTED_WRITE_DEFINITIONS[contract["capabilities"][0]["name"]]
    module = MODULES[contract["module"]]
    for field in ("prepare_function", "commit_function", "field_schema_function"):
        if field not in contract["definition"]:
            assert field not in definition
            continue
        name = contract["definition"][field]
        assert definition[field] == name
        with patch.object(write_catalog, name) as injected:
            assert write_catalog.resolve_write_function(definition[field]) is injected
        assert write_catalog.resolve_write_function(name) is getattr(module, name)
    assert write_catalog.resolve_write_function("capability_specs") is None


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_meeting_source_and_projection_mutations_are_isolated(contract):
    original = _declaration(contract)
    source_prepare, source_commit = original.capability_specs()
    source_fields = deepcopy(contract["definition"]["field_schema"])
    declaration = replace(
        original, prepare_spec=source_prepare, commit_spec=source_commit,
        field_schema=source_fields,
    )
    first_input = next(iter(source_prepare.input_schema["properties"]))
    source_prepare.input_schema["properties"][first_input]["type"] = "array"
    source_prepare.output_schema["type"] = "array"
    source_commit.input_schema["required"].clear()
    source_commit.output_schema["type"] = "array"
    source_fields["fields"][0]["label"] = "changed source"
    if "constraints" in source_fields:
        source_fields["constraints"][0]["maximum_minutes"] = 1

    projected_prepare, projected_commit = declaration.capability_specs()
    projected_prepare.input_schema["properties"].clear()
    projected_prepare.output_schema["type"] = "string"
    projected_commit.input_schema["properties"]["authorization_id"]["type"] = "integer"
    projected_commit.output_schema["type"] = "string"
    projected_definition = declaration.legacy_definition()
    projected_definition["field_schema"]["fields"][0]["label"] = "changed projection"
    if "constraints" in projected_definition["field_schema"]:
        projected_definition["field_schema"]["constraints"][0]["message"] = "changed constraint"
    projected_definition["context_fields"] = ("other_target",)
    projected_definition["field_schema_function"] = "unregistered_builder"
    declaration.scope_bindings().clear()

    assert [spec.to_dict() for spec in declaration.capability_specs()] == contract["capabilities"]
    assert declaration.legacy_definition() == _expected_definition(contract)
    assert declaration.scope_bindings() == original.scope_bindings()
    for other in WORKFLOWS:
        assert _declaration(other).legacy_definition() == _expected_definition(other)
        prepare_name = other["capabilities"][0]["name"]
        assert write_catalog._TRUSTED_WRITE_DEFINITIONS[prepare_name] == _expected_definition(other)


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_meeting_registries_do_not_share_nested_schema_mutations(contract):
    first = seeyon_central.build_central_capability_registry()
    prepare_name, commit_name = (spec["name"] for spec in contract["capabilities"])
    first.get(prepare_name).input_schema["properties"].clear()
    first.get(commit_name).input_schema["required"].clear()
    second = seeyon_central.build_central_capability_registry()
    assert [second.get(spec["name"]).to_dict() for spec in contract["capabilities"]] == contract["capabilities"]
    assert [spec.to_dict() for spec in _declaration(contract).capability_specs()] == contract["capabilities"]


@pytest.mark.parametrize("contract", DYNAMIC_WORKFLOWS, ids=lambda item: item["declaration"])
def test_dynamic_oa_builders_are_not_executed_by_construction_or_registry(contract):
    builder_name = contract["definition"]["field_schema_function"]
    builder = Mock(side_effect=AssertionError("static declaration executed a live field builder"))
    builder.__name__ = builder_name
    declaration = replace(_declaration(contract), field_schema_function=builder)
    assert declaration.legacy_definition()["field_schema_function"] == builder_name
    assert [spec.to_dict() for spec in declaration.capability_specs()] == contract["capabilities"]
    with patch.object(seeyon_central, contract["declaration"], declaration):
        registry = seeyon_central.build_central_capability_registry()
    assert [registry.get(spec["name"]).to_dict() for spec in contract["capabilities"]] == contract["capabilities"]
    builder.assert_not_called()


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
def test_oa_field_builder_extension_preserves_eleven_positional_arguments(contract):
    original = _declaration(contract)
    arguments = tuple(getattr(original, name) for name in POSITIONAL_FIELDS)
    static = WriteWorkflowDefinition(*arguments)
    expected_static = _expected_definition(contract)
    expected_static.pop("field_schema_function", None)
    assert static.field_schema_function is None
    assert static.legacy_definition() == expected_static
    assert list(static.legacy_definition()) == list(expected_static)
    explicit = WriteWorkflowDefinition(*arguments, field_schema_function=original.field_schema_function)
    assert explicit.legacy_definition() == _expected_definition(contract)
    with pytest.raises(TypeError):
        WriteWorkflowDefinition(*arguments, original.field_schema_function)


@pytest.mark.parametrize("contract", DYNAMIC_WORKFLOWS, ids=lambda item: item["declaration"])
def test_absent_dynamic_field_builder_omits_the_legacy_key(contract):
    declaration = replace(_declaration(contract), field_schema_function=None)
    expected = _expected_definition(contract)
    expected.pop("field_schema_function")
    assert declaration.legacy_definition() == expected
    assert list(declaration.legacy_definition()) == list(expected)


class _UnnamedCallable:
    def __call__(self, *_args, **_kwargs):
        raise AssertionError("constructor must not invoke a field builder")


@pytest.mark.parametrize("contract", WORKFLOWS, ids=lambda item: item["declaration"])
@pytest.mark.parametrize(
    "handler",
    ["build_fields", 7, object(), lambda *_args: {}, _UnnamedCallable()],
    ids=["function-name-string", "integer", "noncallable", "lambda", "unnamed-callable"],
)
def test_invalid_dynamic_field_builders_fail_before_registration(contract, handler):
    with pytest.raises((TypeError, ValueError)):
        replace(_declaration(contract), field_schema_function=handler)
