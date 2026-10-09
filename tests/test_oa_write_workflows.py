"""Keep the complete OA write surface equal to the pre-migration contract."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from importlib import import_module
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agentbridge.adapters import seeyon_central
from agentbridge.adapters.seeyon_write_workflows import (
    OA_WRITE_DECLARATIONS,
    oa_write_capability_specs_by_name,
)
from agentbridge.core import write_catalog
from agentbridge.core.planning_policy import planning_descriptor
from agentbridge.core.user_grants import CAPABILITY_PERMISSIONS, PERMISSIONS
from agentbridge.core.write_workflow import WritePrepareAliasDefinition, WriteWorkflowDefinition


CONTRACT_BYTES = (Path(__file__).parent / "fixtures/oa_write_workflows_contract.json").read_bytes()
# Keep the signed-off bytes/hash frozen; adapt only the retired Python exception namespace.
CONTRACT = json.loads(CONTRACT_BYTES.replace(b'"bscli.adapters.', b'"agentbridge.adapters.'))
DEFINITIONS = dict(CONTRACT["definitions"])
SPECS = {spec["name"]: spec for spec in CONTRACT["capabilities"]}
SCOPES = {name: frozenset(scopes) for name, scopes in CONTRACT["scopes"]}
MEETING_PREPARES = {
    "oa.meeting.create.prepare",
    "oa.meeting_room.application.prepare",
    "oa.meeting_room.application.cancel.prepare",
}
NEW_PREPARES = [name for name in DEFINITIONS if name not in MEETING_PREPARES]
PREFLIGHT_PREPARES = [name for name in NEW_PREPARES if "preflight_function" in DEFINITIONS[name]]
ALIAS_PREPARES = (
    "oa.missed_punch.approval.batch.prepare",
    "oa.workflow.pending.batch.prepare",
)
CANONICAL_PREPARE = "oa.missed_punch.approval.prepare"
SHARED_COMMIT = "oa.missed_punch.approve"
PAIR_PREPARES = [name for name in NEW_PREPARES if name not in ALIAS_PREPARES]
POSITIONAL_FIELDS = (
    "prepare_spec", "commit_spec", "required_scopes", "field_schema",
    "context_fields", "prepare_function", "commit_function", "contract_error",
    "outcome_error", "field_message", "authorization_message",
)
LEAF_MODULES = (
    "seeyon_business_trip", "seeyon_business_trip_submit", "seeyon_leave",
    "seeyon_leave_submit", "seeyon_meeting", "seeyon_meeting_room_application",
    "seeyon_missed_punch", "seeyon_pending_actions", "seeyon_workflow_revoke",
    "taihua", "smartlight",
)


def _normalized_definition(definition):
    normalized = deepcopy(definition)
    normalized["context_fields"] = list(normalized["context_fields"])
    for field in ("contract_error", "outcome_error"):
        error = normalized[field]
        normalized[field] = f"{error.__module__}.{error.__qualname__}"
    return normalized


def _change_schema(spec):
    first_property = next(iter(spec.input_schema["properties"].values()))
    first_property["type"] = "array"
    if "required" in spec.input_schema:
        spec.input_schema["required"].clear()
    spec.output_schema["type"] = "array"


def test_oa_write_fixture_remains_the_frozen_5020bf6_contract():
    assert CONTRACT["baseline"].startswith("5020bf6")
    assert hashlib.sha256(CONTRACT_BYTES).hexdigest() == (
        "5815e56c49564f20a8b67a0acb588a84c0937b6a8a958f53d7725ccd69606d3e"
    )
    assert len(SPECS) == 48
    assert len(DEFINITIONS) == 25
    assert len(CONTRACT["commits"]) == 23
    assert len(PREFLIGHT_PREPARES) == 13


def test_full_registry_catalog_and_scope_insertion_orders_are_unchanged():
    registry = seeyon_central.build_central_capability_registry()
    # list() sorts capabilities and would hide movement across read/write blocks.
    assert list(registry._specs) == CONTRACT["registry_order"]
    assert list(write_catalog._TRUSTED_WRITE_DEFINITIONS) == CONTRACT["catalog_order"]
    assert list(write_catalog._CAPABILITY_SCOPES) == CONTRACT["scope_order"]
    assert [
        [commit, prepare]
        for commit, (prepare, _definition) in write_catalog._TRUSTED_WRITE_COMMITS.items()
        if commit.startswith("oa.")
    ] == CONTRACT["commits"]


def test_all_oa_write_specs_preserve_versions_schemas_and_registration_order():
    registry = seeyon_central.build_central_capability_registry()
    assert [
        spec.to_dict() for spec in registry._specs.values() if spec.effect != "read"
    ] == CONTRACT["capabilities"]


@pytest.mark.parametrize("prepare_name", list(DEFINITIONS))
def test_each_oa_catalog_definition_and_reverse_binding_match_old_contract(prepare_name):
    expected = DEFINITIONS[prepare_name]
    actual = write_catalog._TRUSTED_WRITE_DEFINITIONS[prepare_name]
    assert _normalized_definition(actual) == expected
    assert list(actual) == list(expected)
    commit = expected["commit_capability"]
    canonical_prepare, canonical_definition = write_catalog._TRUSTED_WRITE_COMMITS[commit]
    assert canonical_prepare == dict(CONTRACT["commits"])[commit]
    assert canonical_definition is write_catalog._TRUSTED_WRITE_DEFINITIONS[canonical_prepare]


def test_all_oa_scopes_permissions_and_planning_remain_unchanged():
    assert [
        [name, sorted(scopes)] for name, scopes in write_catalog._CAPABILITY_SCOPES.items()
        if name.startswith("oa.")
    ] == CONTRACT["scopes"]
    for name in SPECS:
        assert write_catalog.capability_required_scopes(name) == SCOPES[name]
        # Mixed pending batches resolve permissions per selected item at runtime.
        assert CAPABILITY_PERMISSIONS.get(name) == CONTRACT["capability_permissions"][name]
        assert planning_descriptor(name) == CONTRACT["planning"][name]
    for permission, expected in CONTRACT["permissions"].items():
        assert PERMISSIONS[permission] == expected


def test_original_leaf_aliases_and_all_sixty_function_bridges_remain_late_bound():
    assert sorted(write_catalog.WRITE_FUNCTION_NAMES) == CONTRACT["function_names"]
    modules = [import_module(f"agentbridge.adapters.{name}") for name in LEAF_MODULES]
    for name in CONTRACT["function_names"]:
        originals = [
            getattr(module, name) for module in modules
            if name in vars(module) and getattr(getattr(module, name), "__module__", None) == module.__name__
        ]
        assert len(originals) == 1, name
        assert getattr(write_catalog, name) is originals[0]
        assert write_catalog.resolve_write_function(name) is originals[0]
        with patch.object(write_catalog, name) as injected:
            assert write_catalog.resolve_write_function(name) is injected
        assert write_catalog.resolve_write_function(name) is originals[0]
    assert write_catalog.resolve_write_function("oa_write_capability_specs_by_name") is None
    assert write_catalog.resolve_write_function("__import__") is None


@pytest.mark.parametrize("prepare_name", NEW_PREPARES)
def test_new_declarations_project_the_frozen_pair_or_alias_contract(prepare_name):
    declaration = OA_WRITE_DECLARATIONS[prepare_name]
    assert _normalized_definition(declaration.legacy_definition()) == DEFINITIONS[prepare_name]
    assert list(declaration.legacy_definition()) == list(DEFINITIONS[prepare_name])
    names = [prepare_name]
    if prepare_name not in ALIAS_PREPARES:
        names.append(DEFINITIONS[prepare_name]["commit_capability"])
    assert [spec.to_dict() for spec in declaration.capability_specs()] == [SPECS[name] for name in names]
    assert declaration.scope_bindings() == {name: SCOPES[name] for name in names}
    expected_canonical = CANONICAL_PREPARE if prepare_name in ALIAS_PREPARES else prepare_name
    assert declaration.canonical_prepare_name == expected_canonical


def test_unified_declarations_cover_exactly_twenty_pairs_and_two_prepare_aliases():
    assert list(OA_WRITE_DECLARATIONS) == NEW_PREPARES
    assert len(PAIR_PREPARES) == 20
    specs = oa_write_capability_specs_by_name()
    expected_names = {
        name for prepare in NEW_PREPARES
        for name in (prepare, DEFINITIONS[prepare]["commit_capability"])
    }
    assert set(specs) == expected_names
    assert len(specs) == 42
    for name, spec in specs.items():
        assert spec.to_dict() == SPECS[name]
        _change_schema(spec)
    fresh = oa_write_capability_specs_by_name()
    assert {name: spec.to_dict() for name, spec in fresh.items()} == {
        name: SPECS[name] for name in expected_names
    }


@pytest.mark.parametrize("prepare_name", NEW_PREPARES)
def test_oa_source_and_repeated_projection_schemas_are_independent(prepare_name):
    source = deepcopy(OA_WRITE_DECLARATIONS[prepare_name])
    declaration = replace(source)
    _change_schema(source.prepare_spec)
    if isinstance(source, WritePrepareAliasDefinition):
        _change_schema(source.canonical_workflow.commit_spec)
        source.canonical_workflow.field_schema["fields"][0]["label"] = "changed canonical source"
    else:
        _change_schema(source.commit_spec)
        source.field_schema["fields"][0]["label"] = "changed source"
    for spec in declaration.capability_specs():
        _change_schema(spec)
    projection = declaration.legacy_definition()
    projection["field_schema"]["fields"][0]["label"] = "changed projection"
    if projection["field_schema"].get("constraints"):
        projection["field_schema"]["constraints"][0]["message"] = "changed constraint"
    projection["context_fields"] = ("other_target",)
    projection["preflight_profile"] = "other_profile"
    declaration.scope_bindings().clear()
    assert _normalized_definition(declaration.legacy_definition()) == DEFINITIONS[prepare_name]
    for spec in declaration.capability_specs():
        assert spec.to_dict() == SPECS[spec.name]
    for other in NEW_PREPARES:
        assert _normalized_definition(OA_WRITE_DECLARATIONS[other].legacy_definition()) == DEFINITIONS[other]


@pytest.mark.parametrize("prepare_name", ALIAS_PREPARES)
def test_batch_alias_registers_only_its_prepare_and_keeps_the_single_item_commit(prepare_name):
    alias = OA_WRITE_DECLARATIONS[prepare_name]
    canonical = OA_WRITE_DECLARATIONS[CANONICAL_PREPARE]
    assert isinstance(alias, WritePrepareAliasDefinition)
    assert alias.canonical_prepare_name == CANONICAL_PREPARE
    assert [spec.name for spec in alias.capability_specs()] == [prepare_name]
    assert alias.scope_bindings() == {prepare_name: SCOPES[prepare_name]}
    assert alias.commit_spec == canonical.commit_spec
    assert alias.required_scopes == canonical.required_scopes
    assert alias.context_fields == ("batch_id", "affair_id")
    assert canonical.context_fields == ("affair_id",)
    definition = alias.legacy_definition()
    assert definition["field_schema_function"] == "build_missed_punch_approval_batch_field_schema"
    assert definition["prepare_function"] == "prepare_missed_punch_approval"
    assert definition["commit_function"] == "approve_missed_punch_request"
    assert write_catalog._TRUSTED_WRITE_COMMITS[SHARED_COMMIT][0] == CANONICAL_PREPARE


@pytest.mark.parametrize("prepare_name", ALIAS_PREPARES)
def test_alias_canonical_snapshot_does_not_share_mutable_schema_state(prepare_name):
    source = deepcopy(OA_WRITE_DECLARATIONS[CANONICAL_PREPARE])
    alias = replace(OA_WRITE_DECLARATIONS[prepare_name], canonical_workflow=source)
    _change_schema(source.prepare_spec)
    _change_schema(source.commit_spec)
    source.field_schema["fields"][0]["label"] = "external mutation"
    assert alias.canonical_workflow.prepare_spec.to_dict() == SPECS[CANONICAL_PREPARE]
    assert alias.commit_spec.to_dict() == SPECS[SHARED_COMMIT]
    assert _normalized_definition(alias.legacy_definition()) == DEFINITIONS[prepare_name]
    # The public canonical snapshot and delegated projection are isolated too.
    alias.canonical_workflow.field_schema["fields"][0]["label"] = "snapshot mutation"
    assert _normalized_definition(alias.legacy_definition()) == DEFINITIONS[prepare_name]


def _named_preflight(_adapter, _worker, _arguments, _profile):
    raise AssertionError("constructing a declaration must not run its preflight")


class _UnnamedCallable:
    def __call__(self, *_arguments):
        raise AssertionError("constructing a declaration must not invoke callbacks")


@pytest.mark.parametrize("changes", [
    {"preflight_function": None},
    {"preflight_profile": None},
    {"preflight_profile": ""},
    {"preflight_profile": " \t"},
    {"preflight_profile": 3},
    {"preflight_function": "preflight_pending_action"},
    {"preflight_function": 3},
    {"preflight_function": object()},
    {"preflight_function": lambda *_args: {}},
    {"preflight_function": _UnnamedCallable()},
])
def test_invalid_preflight_function_profile_pairs_fail_before_registration(changes):
    with pytest.raises((TypeError, ValueError)):
        replace(OA_WRITE_DECLARATIONS[PREFLIGHT_PREPARES[0]], **changes)


def test_preflight_absence_omits_both_keys_and_presence_appends_them_in_old_order():
    original = OA_WRITE_DECLARATIONS[PREFLIGHT_PREPARES[0]]
    absent = replace(original, preflight_function=None, preflight_profile=None)
    expected = deepcopy(DEFINITIONS[original.prepare_spec.name])
    expected.pop("preflight_function")
    expected.pop("preflight_profile")
    assert _normalized_definition(absent.legacy_definition()) == expected
    assert list(absent.legacy_definition()) == list(expected)
    present = replace(absent, preflight_function=_named_preflight, preflight_profile="test_profile")
    assert list(present.legacy_definition())[-3:] == [
        "authorization_message", "preflight_function", "preflight_profile",
    ]
    assert present.legacy_definition()["preflight_function"] == "_named_preflight"


@pytest.mark.parametrize("prepare_name", PREFLIGHT_PREPARES)
def test_preflight_is_never_executed_by_construction_projection_or_registry(prepare_name):
    spy = Mock(side_effect=AssertionError("registry executed a business preflight"))
    spy.__name__ = "preflight_pending_action"
    declaration = replace(OA_WRITE_DECLARATIONS[prepare_name], preflight_function=spy)
    assert declaration.legacy_definition()["preflight_profile"] == DEFINITIONS[prepare_name]["preflight_profile"]
    with patch.dict(OA_WRITE_DECLARATIONS, {prepare_name: declaration}):
        specs = oa_write_capability_specs_by_name()
        registry = seeyon_central.build_central_capability_registry()
    assert specs[prepare_name].to_dict() == SPECS[prepare_name]
    assert registry.get(prepare_name).to_dict() == SPECS[prepare_name]
    spy.assert_not_called()


def test_preflight_extension_keeps_the_original_eleven_positional_arguments():
    for prepare_name in PAIR_PREPARES:
        original = OA_WRITE_DECLARATIONS[prepare_name]
        arguments = tuple(getattr(original, name) for name in POSITIONAL_FIELDS)
        positional = WriteWorkflowDefinition(*arguments)
        expected = deepcopy(DEFINITIONS[prepare_name])
        expected.pop("preflight_function", None)
        expected.pop("preflight_profile", None)
        assert _normalized_definition(positional.legacy_definition()) == expected
        explicit = WriteWorkflowDefinition(
            *arguments, preflight_function=original.preflight_function,
            preflight_profile=original.preflight_profile,
        )
        assert _normalized_definition(explicit.legacy_definition()) == DEFINITIONS[prepare_name]
        with pytest.raises(TypeError):
            WriteWorkflowDefinition(*arguments, _named_preflight)


@pytest.mark.parametrize("invalid", [
    "noncanonical", "alias_canonical", "same_prepare", "commit_as_prepare",
    "different_system", "different_adapter", "different_effect", "missing_context",
    "duplicate_context", "empty_field_message", "empty_authorization", "lambda_builder",
])
def test_invalid_prepare_aliases_fail_before_registration(invalid):
    alias = OA_WRITE_DECLARATIONS[ALIAS_PREPARES[0]]
    canonical = OA_WRITE_DECLARATIONS[CANONICAL_PREPARE]
    changes = {
        "noncanonical": {"canonical_workflow": None},
        "alias_canonical": {"canonical_workflow": alias},
        "same_prepare": {"prepare_spec": canonical.prepare_spec},
        "commit_as_prepare": {"prepare_spec": canonical.commit_spec},
        "different_system": {"prepare_spec": replace(alias.prepare_spec, name="other.batch.prepare")},
        "different_adapter": {"prepare_spec": replace(alias.prepare_spec, adapter="other-adapter")},
        "different_effect": {"prepare_spec": replace(alias.prepare_spec, effect="reversible_write")},
        "missing_context": {"context_fields": ("unknown_target",)},
        "duplicate_context": {"context_fields": ("affair_id", "affair_id")},
        "empty_field_message": {"field_message": ""},
        "empty_authorization": {"authorization_message": ""},
        "lambda_builder": {"field_schema_function": lambda *_args: {}},
    }[invalid]
    with pytest.raises((TypeError, ValueError)):
        replace(alias, **changes)


@pytest.mark.parametrize("prepare_name", ALIAS_PREPARES)
def test_prepare_alias_constructor_preserves_arguments_and_lazy_dynamic_builder(prepare_name):
    original = OA_WRITE_DECLARATIONS[prepare_name]
    arguments = (
        original.prepare_spec, original.canonical_workflow, original.context_fields,
        original.field_message, original.authorization_message,
    )
    static = WritePrepareAliasDefinition(*arguments)
    assert "field_schema_function" not in static.legacy_definition()
    spy = Mock(side_effect=AssertionError("alias construction executed its field builder"))
    spy.__name__ = "build_missed_punch_approval_batch_field_schema"
    dynamic = WritePrepareAliasDefinition(*arguments, field_schema_function=spy)
    assert _normalized_definition(dynamic.legacy_definition()) == DEFINITIONS[prepare_name]
    assert [spec.to_dict() for spec in dynamic.capability_specs()] == [SPECS[prepare_name]]
    spy.assert_not_called()
    with pytest.raises(TypeError):
        WritePrepareAliasDefinition(*arguments, original.field_schema_function)
