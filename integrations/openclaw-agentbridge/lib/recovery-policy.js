// Pure admission decisions. Callers retain ownership of classification, leases,
// idempotency, polling, and delivery; these functions perform no retries or I/O.
export function transportRecoveryStrategy(callClass) {
  if (callClass === "read") return "bounded_retry";
  if (callClass === "prepare") return "bounded_retry_with_stable_idempotency_key";
  return "query_operation_then_stop_if_unknown";
}

export function transportRetryAllowed({
  retryable, attempt, maximumAttempts, canceled = false,
  delayMs = 0, remainingMs = null,
}) {
  return retryable === true && canceled !== true && attempt < maximumAttempts &&
    (remainingMs === null || delayMs < remainingMs);
}

export function interactionResumeAllowed(interaction) {
  return interaction?.state === "completed" && interaction.resume?.ready === true &&
    interaction.resume?.completed !== true;
}

export function resumeClaimAllowed({ claimed = false, taskTerminal = false } = {}) {
  return claimed !== true && taskTerminal !== true;
}
