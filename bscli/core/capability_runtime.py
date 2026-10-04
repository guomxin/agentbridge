from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from bscli.core.capability import CapabilityRegistry, CapabilitySpec
from bscli.core.operations import OperationStore
from bscli.core.input_validation import (
    ValidationProfile, matches_json_type, object_input_issue,
)


@dataclass(frozen=True)
class CapabilityContext:
    user_subject: str
    request_id: str
    operation_id: str
    spec: CapabilitySpec
    trace_id: str | None = None
    task_id: str | None = None


class RequiresUserAction(RuntimeError):
    def __init__(self, code: str, message: str, *, next_action: dict) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_action = next_action


class OutcomeUnknown(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CapabilityRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


CapabilityHandler = Callable[[CapabilityContext, dict], Any]


class CapabilityEngine:
    def __init__(self, *, registry: CapabilityRegistry, operation_store: OperationStore) -> None:
        self.registry = registry
        self.operation_store = operation_store
        self._handlers: dict[str, CapabilityHandler] = {}

    def register_handler(self, capability_name: str, handler: CapabilityHandler) -> None:
        self.registry.get(capability_name)
        self._handlers[capability_name] = handler

    def invoke(
        self,
        *,
        user_subject: str,
        capability_name: str,
        arguments: dict,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        spec = self.registry.get(capability_name)
        _validate_json_object(arguments, spec.input_schema)
        effective_request_id = request_id or str(uuid4())
        operation, reused = self.operation_store.create(
            user_subject=user_subject,
            capability_name=spec.name,
            capability_version=spec.version,
            input_summary=_operation_input_summary(arguments, effect=spec.effect, capability_name=spec.name),
            input_identity=arguments,
            idempotency_key=idempotency_key,
            request_id=effective_request_id,
        )
        if reused:
            return _operation_response(operation, reused=True)

        handler = self._handlers.get(capability_name)
        if handler is None:
            operation = self.operation_store.mark_failed(
                operation["operation_id"],
                code="HANDLER_NOT_REGISTERED",
                message=f"No handler is registered for {capability_name}.",
            )
            return _operation_response(operation, reused=False)

        self.operation_store.mark_running(operation["operation_id"])
        context = CapabilityContext(
            user_subject=user_subject,
            request_id=operation["request_id"],
            operation_id=operation["operation_id"],
            spec=spec,
            trace_id=trace_id,
            task_id=task_id,
        )
        try:
            result = handler(context, arguments)
        except RequiresUserAction as exc:
            operation = self.operation_store.mark_requires_user_action(
                operation["operation_id"],
                code=exc.code,
                message=exc.message,
                next_action=exc.next_action,
            )
        except OutcomeUnknown as exc:
            operation = self.operation_store.mark_unknown(
                operation["operation_id"],
                code=exc.code,
                message=exc.message,
            )
        except CapabilityRejected as exc:
            operation = self.operation_store.mark_failed(
                operation["operation_id"],
                code=exc.code,
                message=exc.message,
            )
        except Exception as exc:
            operation = self.operation_store.mark_failed(
                operation["operation_id"],
                code="CAPABILITY_EXECUTION_FAILED",
                message=str(exc) or exc.__class__.__name__,
            )
        else:
            operation = self.operation_store.mark_succeeded(operation["operation_id"], result)
        return _operation_response(operation, reused=False)


def _operation_response(operation: dict, *, reused: bool) -> dict:
    next_action = operation.get("next_action")
    interaction = (
        next_action.get("interaction")
        if isinstance(next_action, dict)
        and isinstance(next_action.get("interaction"), dict)
        else None
    )
    return {
        "protocolVersion": "0.1",
        "requestId": operation["request_id"],
        "operationId": operation["operation_id"],
        "status": operation["status"],
        "result": operation.get("result"),
        "error": operation.get("error"),
        "evidenceRefs": [],
        "nextAction": next_action,
        "interaction": interaction,
        "reused": reused,
    }


def _validate_json_object(value: Any, schema: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError("capability input must be a JSON object")
    if schema.get("type") not in (None, "object"):
        raise ValueError("only object capability input schemas are supported")
    issue = object_input_issue(value, schema, profile=ValidationProfile.CAPABILITY)
    if issue is None:
        return
    if issue.kind == "missing":
        raise ValueError(f"missing required capability input: {', '.join(issue.fields)}")
    if issue.kind == "unexpected":
        raise ValueError(f"unexpected capability input: {', '.join(issue.fields)}")
    raise ValueError(f"capability input {issue.path!r} must be {issue.expected}")


def _matches_json_type(value: Any, expected: str | list[str]) -> bool:
    return matches_json_type(value, expected, profile=ValidationProfile.CAPABILITY)


def _operation_input_summary(arguments: dict, *, effect: str, capability_name: str = "") -> dict:
    if effect == "read":
        return arguments
    summary = {}
    for name, value in arguments.items():
        if isinstance(value, str):
            summary[name] = {
                "redacted": True,
                "present": bool(value),
                "length": len(value),
            }
        elif isinstance(value, (bool, int, float)) or value is None:
            summary[name] = value
        elif isinstance(value, list):
            summary[name] = {"redacted": True, "item_count": len(value)}
        elif isinstance(value, dict):
            summary[name] = {"redacted": True, "field_count": len(value)}
        else:
            summary[name] = {"redacted": True, "type": type(value).__name__}
    return summary
