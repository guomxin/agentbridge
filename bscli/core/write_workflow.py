"""Typed write declarations projected into the existing runtime catalogs.

This module owns no execution, persistence, or authorization transaction.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Protocol

from bscli.core.capability import CapabilitySpec


PrepareHandler = Callable[[object, object, dict], dict]


class CommitHandler(Protocol):
    def __call__(
        self, adapter: object, worker: object, plan: dict, *,
        enter_commit_boundary: Callable[[], None],
    ) -> dict: ...


def _binding_name(handler: object) -> str:
    name = getattr(handler, "__name__", None)
    if not callable(handler) or not isinstance(name, str) or not name.isidentifier():
        raise ValueError("write binding must be a named callable")
    return name


@dataclass(frozen=True)
class WriteWorkflowDefinition:
    """Static paired capabilities with independent copies at consumer boundaries.

    Frozen fields prevent reassignment, not mutation of nested schema dictionaries.
    Consumers must use the projection methods instead of modifying declaration data.
    """

    prepare_spec: CapabilitySpec
    commit_spec: CapabilitySpec
    required_scopes: frozenset[str]
    field_schema: dict | None
    context_fields: tuple[str, ...]
    prepare_function: PrepareHandler
    commit_function: CommitHandler
    contract_error: type[Exception]
    outcome_error: type[Exception]
    field_message: str
    authorization_message: str

    def __post_init__(self) -> None:
        if not isinstance(self.prepare_spec, CapabilitySpec) or not isinstance(self.commit_spec, CapabilitySpec):
            raise TypeError("write capabilities must be CapabilitySpec values")
        if self.prepare_spec.name == self.commit_spec.name:
            raise ValueError("prepare and commit capabilities must be distinct")
        if (self.prepare_spec.system != self.commit_spec.system
                or self.prepare_spec.adapter != self.commit_spec.adapter):
            raise ValueError("write capabilities must share a system and adapter")
        if any(spec.effect not in {"controlled_write", "reversible_write"}
               for spec in (self.prepare_spec, self.commit_spec)):
            raise ValueError("write capabilities must declare a write effect")
        if (not isinstance(self.required_scopes, frozenset) or not self.required_scopes
                or any(not isinstance(scope, str) or not scope.strip() or scope != scope.strip()
                       for scope in self.required_scopes)):
            raise ValueError("write workflow requires nonempty immutable scope names")
        if self.field_schema is not None and not isinstance(self.field_schema, dict):
            raise TypeError("write field schema must be an object or None")
        if (not isinstance(self.context_fields, tuple)
                or any(not isinstance(name, str) or not name for name in self.context_fields)
                or len(set(self.context_fields)) != len(self.context_fields)
                or any(name not in self.prepare_spec.input_schema.get("properties", {})
                       for name in self.context_fields)):
            raise ValueError("write context fields must be unique declared prepare inputs")
        _binding_name(self.prepare_function)
        _binding_name(self.commit_function)
        for error in (self.contract_error, self.outcome_error):
            if not isinstance(error, type) or not issubclass(error, Exception):
                raise TypeError("write errors must be Exception classes")
        if any(not isinstance(message, str) or not message.strip()
               for message in (self.field_message, self.authorization_message)):
            raise ValueError("write workflow messages must be nonempty")
        # Adapter schema constants stay source-compatible; consumers cannot mutate
        # them through a descriptor or one of its legacy projections.
        object.__setattr__(self, "prepare_spec", deepcopy(self.prepare_spec))
        object.__setattr__(self, "commit_spec", deepcopy(self.commit_spec))
        object.__setattr__(self, "field_schema", deepcopy(self.field_schema))

    def capability_specs(self) -> tuple[CapabilitySpec, CapabilitySpec]:
        return deepcopy((self.prepare_spec, self.commit_spec))

    def legacy_definition(self) -> dict:
        """Keep the executor's field names and late-bound function-name bridge."""
        return {
            "commit_capability": self.commit_spec.name,
            "field_schema": deepcopy(self.field_schema),
            "context_fields": self.context_fields,
            "prepare_function": _binding_name(self.prepare_function),
            "commit_function": _binding_name(self.commit_function),
            "contract_error": self.contract_error,
            "outcome_error": self.outcome_error,
            "field_message": self.field_message,
            "authorization_message": self.authorization_message,
        }

    def scope_bindings(self) -> dict[str, frozenset[str]]:
        return {self.prepare_spec.name: self.required_scopes,
                self.commit_spec.name: self.required_scopes}
