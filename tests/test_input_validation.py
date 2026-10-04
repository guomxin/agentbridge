"""Compatibility evidence captured before the shared validator was introduced."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from bscli.adapters.smartlight import build_smartlight_capability_registry, _normalize_choice
from bscli.core.capability_runtime import CapabilityEngine, _validate_json_object
from bscli.core.operations import OperationStore
from bscli.core.task_plan_validation import PlanValidationError, _validate_partial_input
from bscli.core.transforms import (
    TransformRegistry, TransformSpec, TransformRejected, _validate_schema_value,
)


FIXTURE_BYTES = (Path(__file__).parent / "fixtures/input_validation_contract.json").read_bytes()
CONTRACT = json.loads(FIXTURE_BYTES)


def test_fixture_was_frozen_before_2f_implementation():
    assert CONTRACT["baseline"] == "1ab6dddedea54cc821ba103639d34e1e611f6e7e"
    assert len(CONTRACT["cases"]) == 84
    assert hashlib.sha256(FIXTURE_BYTES).hexdigest() == (
        "8c76dfcbb6927c49a9221658c4fe7b1d2d45460856c79cb2811f2625376c17d2"
    )


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda case: case["id"])
@pytest.mark.parametrize("profile", ["capability", "plan", "transform"])
def test_original_acceptance_errors_precedence_and_mutation_contract(case, profile):
    value, schema, bindings = deepcopy((case["value"], CONTRACT["schemas"][case["schema"]], case["bindings"]))
    original = deepcopy((value, schema, bindings))
    try:
        if profile == "capability":
            _validate_json_object(value, schema)
        elif profile == "plan":
            _validate_partial_input(value, bindings=bindings, schema=schema, step_key="sample")
        else:
            _validate_schema_value(value, schema, path="input", error_code="TRANSFORM_INPUT_INVALID")
        result = {"accepted": True}
    except Exception as exc:
        result = {"accepted": False, "exception": type(exc).__name__, "message": str(exc)}
        for attribute in ("code", "step_key"):
            if hasattr(exc, attribute):
                result[attribute] = getattr(exc, attribute)
    assert result == case["results"][profile]
    assert (value, schema, bindings) == original


def test_invalid_capability_input_never_creates_an_operation_or_calls_handler(tmp_path):
    registry = build_smartlight_capability_registry()
    store = OperationStore(tmp_path / "operations.db")
    engine = CapabilityEngine(registry=registry, operation_store=store)
    handler = Mock()
    engine.register_handler("smartlight.alarm.list", handler)
    with pytest.raises(ValueError, match="capability input 'size' must be integer"):
        engine.invoke(user_subject="alice", capability_name="smartlight.alarm.list",
                      arguments={"size": True}, idempotency_key="invalid")
    assert store.list() == []
    handler.assert_not_called()


@pytest.mark.parametrize("sort_by", ["", " OCCURRED_AT "])
def test_adapter_normalization_and_same_key_reuse_remain_unchanged(tmp_path, sort_by):
    registry = build_smartlight_capability_registry()
    store = OperationStore(tmp_path / "operations.db")
    engine = CapabilityEngine(registry=registry, operation_store=store)
    handler = Mock(side_effect=lambda _context, args: {
        "sort_by": _normalize_choice(args.get("sort_by"), default="occurred_at",
                                     allowed={"occurred_at", "last_activity"}, field_name="sort_by")
    })
    engine.register_handler("smartlight.alarm.list", handler)
    # Existing adapter defaults, whitespace and case normalization must remain
    # reachable through ordinary invocation; plan enum checks stay stricter.
    arguments = {"sort_by": sort_by}
    first = engine.invoke(user_subject="alice", capability_name="smartlight.alarm.list",
                          arguments=arguments, idempotency_key="once")
    second = engine.invoke(user_subject="alice", capability_name="smartlight.alarm.list",
                           arguments=arguments, idempotency_key="once")
    assert first["status"] == "succeeded"
    assert first["result"] == {"sort_by": "occurred_at"}
    assert second["reused"] is True
    assert first["operationId"] == second["operationId"]
    assert len(store.list()) == 1
    assert handler.call_count == 1
    assert arguments == {"sort_by": sort_by}
    with pytest.raises(PlanValidationError, match="不在允许值范围内"):
        _validate_partial_input(arguments, bindings={},
                                schema=registry.get("smartlight.alarm.list").input_schema, step_key="read")


@pytest.mark.parametrize("side", ["input", "output"])
def test_transform_nested_validation_still_surrounds_handler(side):
    registry = TransformRegistry()
    schema = CONTRACT["schemas"]["recursive"]
    good = {"items": [{"name": "ok"}]}
    bad = {"items": [{"name": "ok"}, {"name": "bad"}]}
    handler = Mock(return_value=bad if side == "output" else good)
    registry.register(TransformSpec(
        name="test.validation.v1", description="test", input_schema=schema,
        output_schema=schema, maximum_input_items=100, maximum_output_chars=100,
    ), handler)
    with pytest.raises(TransformRejected) as raised:
        registry.invoke("test.validation.v1", bad if side == "input" else good)
    assert raised.value.code == f"TRANSFORM_{side.upper()}_INVALID"
    assert str(raised.value) == f"{side}.items[1].name is outside the allowed values"
    assert handler.call_count == (0 if side == "input" else 1)
