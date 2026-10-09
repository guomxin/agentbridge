"""Typed write declarations projected into the existing runtime catalogs.

This module owns no execution, persistence, or authorization transaction.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol

from agentbridge.core.capability import CapabilitySpec


PrepareHandler = Callable[[object, object, dict], dict]
FieldSchemaHandler = Callable[[object, object, dict], dict]
PreflightHandler = Callable[[object, object, dict, str], dict]


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
    field_message: str | None
    authorization_message: str
    field_schema_function: FieldSchemaHandler | None = field(default=None, kw_only=True)
    preflight_function: PreflightHandler | None = field(default=None, kw_only=True)
    preflight_profile: str | None = field(default=None, kw_only=True)

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
        if self.field_schema_function is not None:
            _binding_name(self.field_schema_function)
        if (self.preflight_function is None) != (self.preflight_profile is None):
            raise ValueError("write preflight function and profile must be paired")
        if self.preflight_function is not None:
            _binding_name(self.preflight_function)
            if not isinstance(self.preflight_profile, str) or not self.preflight_profile.strip():
                raise ValueError("write preflight profile must be nonempty")
        for error in (self.contract_error, self.outcome_error):
            if not isinstance(error, type) or not issubclass(error, Exception):
                raise TypeError("write errors must be Exception classes")
        messages = [self.authorization_message]
        if self.field_schema is not None or self.field_message is not None:
            messages.append(self.field_message)
        if any(not isinstance(message, str) or not message.strip()
               for message in messages):
            raise ValueError("write workflow messages must be nonempty")
        # Adapter schema constants stay source-compatible; consumers cannot mutate
        # them through a descriptor or one of its legacy projections.
        object.__setattr__(self, "prepare_spec", deepcopy(self.prepare_spec))
        object.__setattr__(self, "commit_spec", deepcopy(self.commit_spec))
        object.__setattr__(self, "field_schema", deepcopy(self.field_schema))

    @property
    def canonical_prepare_name(self) -> str:
        return self.prepare_spec.name

    def capability_specs(self) -> tuple[CapabilitySpec, CapabilitySpec]:
        return deepcopy((self.prepare_spec, self.commit_spec))

    def legacy_definition(self) -> dict:
        """Keep the executor's field names and late-bound function-name bridge."""
        return {
            "commit_capability": self.commit_spec.name,
            "field_schema": deepcopy(self.field_schema),
            **({"field_schema_function": _binding_name(self.field_schema_function)}
               if self.field_schema_function is not None else {}),
            "context_fields": self.context_fields,
            "prepare_function": _binding_name(self.prepare_function),
            "commit_function": _binding_name(self.commit_function),
            "contract_error": self.contract_error,
            "outcome_error": self.outcome_error,
            **({"field_message": self.field_message} if self.field_message is not None else {}),
            "authorization_message": self.authorization_message,
            **({"preflight_function": _binding_name(self.preflight_function),
                "preflight_profile": self.preflight_profile}
               if self.preflight_function is not None else {}),
        }

    def scope_bindings(self) -> dict[str, frozenset[str]]:
        return {self.prepare_spec.name: self.required_scopes,
                self.commit_spec.name: self.required_scopes}


@dataclass(frozen=True)
class WritePrepareAliasDefinition:
    """An additional prepare entry sharing a canonical workflow's commit.

    An alias registers only its prepare capability. Its legacy definition retains
    the shared commit binding without changing that commit's canonical reverse route.
    """

    prepare_spec: CapabilitySpec
    canonical_workflow: WriteWorkflowDefinition
    context_fields: tuple[str, ...]
    field_message: str | None
    authorization_message: str
    field_schema_function: FieldSchemaHandler | None = field(default=None, kw_only=True)
    _workflow: WriteWorkflowDefinition = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.prepare_spec, CapabilitySpec):
            raise TypeError("write alias prepare must be a CapabilitySpec value")
        if not isinstance(self.canonical_workflow, WriteWorkflowDefinition):
            raise TypeError("write alias must reference a canonical workflow")
        if self.prepare_spec.name == self.canonical_workflow.prepare_spec.name:
            raise ValueError("write alias prepare must differ from its canonical prepare")
        if self.prepare_spec.effect != self.canonical_workflow.prepare_spec.effect:
            raise ValueError("write alias must preserve its canonical prepare effect")
        canonical = deepcopy(self.canonical_workflow)
        workflow = replace(
            canonical,
            prepare_spec=self.prepare_spec,
            context_fields=self.context_fields,
            field_message=self.field_message,
            authorization_message=self.authorization_message,
            field_schema_function=self.field_schema_function,
        )
        object.__setattr__(self, "prepare_spec", workflow.prepare_spec)
        object.__setattr__(self, "canonical_workflow", canonical)
        object.__setattr__(self, "_workflow", workflow)

    @property
    def canonical_prepare_name(self) -> str:
        return self.canonical_workflow.prepare_spec.name

    @property
    def commit_spec(self) -> CapabilitySpec:
        return self._workflow.commit_spec

    @property
    def required_scopes(self) -> frozenset[str]:
        return self._workflow.required_scopes

    def capability_specs(self) -> tuple[CapabilitySpec]:
        return (deepcopy(self.prepare_spec),)

    def legacy_definition(self) -> dict:
        return self._workflow.legacy_definition()

    def scope_bindings(self) -> dict[str, frozenset[str]]:
        return {self.prepare_spec.name: self.required_scopes}
