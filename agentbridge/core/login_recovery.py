"""Original-task read recovery. The caller owns the shared task lock and stores."""

from agentbridge.core.tasks import ACTIVE_TASK_STATUSES
from agentbridge.core.login_continuation import read_continuation_message


def resume_login_read(self, *, user_subject: str, record: dict) -> dict | None:
    task_id = self.tasks.task_id_for_interaction(
        record["interaction_id"], user_subject=user_subject,
    )
    if not task_id or self.tasks.get_batch_for_task(
        parent_task_id=task_id, user_subject=user_subject,
    ):
        return None
    with self._task_call_lock(task_id):
        task = self.tasks.get_task(task_id, user_subject=user_subject)
        if task["status"] not in ACTIVE_TASK_STATUSES:
            return {
                "status": "ignored", "taskId": task_id,
                "taskStatus": task["status"],
                "nextAction": {"type": "original_request_completed"},
            }
        if task.get("current_interaction_id") != record["interaction_id"]:
            return None
        preceding = self.tasks.operation_before_interaction(
            task_id=task_id, user_subject=user_subject,
            interaction_id=record["interaction_id"],
        )
        if preceding is None:
            return None
        original = self.operations.get(preceding["operation_id"])
        spec = self.registry.get(original["capability_name"])
        key = f"login-read:{record['interaction_id']}:{original['operation_id']}"
        current_id = task.get("current_operation_id")
        current = self.operations.get(current_id) if current_id else {}
        if (
            spec.effect != "read" or spec.system != record["system_id"]
            or original["status"] != "requires_user_action"
            or (original.get("error") or {}).get("code") != "LOGIN_REQUIRED"
            or (current_id != original["operation_id"] and current.get("idempotency_key") != key)
        ):
            return None
        # The ledger holds exact read arguments, unlike redacted write inputs.
        try:
            response = self.invoke(
                user_subject=user_subject, capability_name=spec.name,
                arguments=original["input_summary"], idempotency_key=key,
                task_id=task_id, host_type="interaction_resume",
                host_run_id=record["interaction_id"],
            )
        except (ValueError, KeyError):
            response = {"status": "failed", "error": {"code": "LOGIN_READ_INPUT_INVALID"}}
        if response["status"] in {"pending", "running"}:
            # A previous runtime may have stopped during the read. Never claim success.
            response = {**response, "status": "failed", "error": {"code": "LOGIN_READ_INTERRUPTED"}}
        message = read_continuation_message(
            self.challenges.get(self._credential_challenge_id(record))["system_name"], spec.name, response,
        )
        # Persist feedback before closing the task; retrying after a crash is deduplicated.
        self.tasks.append_timeline_message(
            user_subject=user_subject, source_endpoint_id=task["origin_endpoint_id"],
            task_id=task_id, message_key=key, role="assistant", text=message,
            payload={"operationId": response.get("operationId"), "continuation": "login_read"},
            notify_source=True,
        )
        if response.get("operationId"):
            self.observe_host_task(
                user_subject=user_subject, task_id=task_id,
                operation_ids=[response["operationId"]],
            )
        if response["status"] != "succeeded":
            self.fail_host_task(
                user_subject=user_subject, task_id=task_id,
                error_code=(response.get("error") or {}).get("code") or "LOGIN_READ_FAILED",
                message=message, causation_ref=key,
            )
        return {
            **response, "interaction": None,
            "resumedFromInteractionId": record["interaction_id"], "taskId": task_id,
            "taskStatus": self.tasks.get_task(task_id, user_subject=user_subject)["status"],
            "nextAction": {"type": "original_request_completed"},
        }
