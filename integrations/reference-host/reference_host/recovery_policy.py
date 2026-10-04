"""Independent pure host recovery decisions; no central implementation imports."""
from __future__ import annotations

from typing import Any, Mapping


def transport_recovery_strategy(call_class: str) -> str:
    if call_class == "read":
        return "bounded_retry"
    if call_class == "prepare":
        return "bounded_retry_with_stable_idempotency_key"
    return "query_operation_then_stop_if_unknown"


def transport_retry_allowed(
    *, retryable: bool, attempt: int, maximum_attempts: int,
    canceled: bool = False, delay_ms: float = 0, remaining_ms: float | None = None,
) -> bool:
    return (retryable is True and canceled is not True and attempt < maximum_attempts
            and (remaining_ms is None or delay_ms < remaining_ms))


def interaction_resume_allowed(interaction: Mapping[str, Any]) -> bool:
    resume = interaction.get("resume")
    resume = resume if isinstance(resume, Mapping) else {}
    return (interaction.get("state") == "completed" and resume.get("ready") is True
            and resume.get("completed") is not True)


def resume_claim_allowed(*, claimed: bool = False, task_terminal: bool = False) -> bool:
    return claimed is not True and task_terminal is not True
