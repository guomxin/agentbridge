from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import time

from agentbridge.workspace.gateway import (
    GatewayRequestError, OpenClawGatewayClient, _history_evidence_for_run,
    _local_final_history_receipt,
)


class GatewayHistoryTests(unittest.TestCase):
    def test_shared_authoritative_history_vectors(self):
        vectors = json.loads((Path(__file__).parent / "fixtures/workspace_gateway_history_vectors.json").read_text())
        for vector in vectors:
            with self.subTest(vector["name"]):
                self.assertEqual(_history_evidence_for_run(vector["payload"], vector["runId"],
                    session_key=vector["sessionKey"]), vector["expected"])

    def test_only_exact_local_receipt_skips_independent_history_lookup(self):
        session = "agent:main:agentbridge-workspace:direct:test-account"
        receipt = {"schemaVersion": "agentbridge.gateway-final.v1", "source": "local-same-socket-history",
                   "sessionKey": session, "runId": "run-current", "observedRunId": "run-current"}
        for override in [None, {"schemaVersion": "future"}, {"source": "remote"},
                         {"sessionKey": "other"}, {"runId": "other"}, {"observedRunId": "other"}]:
            with self.subTest(override=override), TemporaryDirectory() as directory:
                client = HistoryClient(directory, {**receipt, **(override or {})})
                events = list(client.send_stream(session_key=session, endpoint_key="workspace:test",
                    grant="g" * 48, message="read only", idempotency_key="run-current"))
                self.assertEqual(events[-1]["text"], "local persisted" if override is None else "fallback persisted")
                self.assertEqual(client.history_calls, 0 if override is None else 1)
                self.assertEqual(client.stream_calls, 1)
                if override is None:
                    self.assertIsNotNone(client.diagnostics()["last_success_at"])

    def test_pending_authority_never_replays_or_aborts_the_completed_run(self):
        with TemporaryDirectory() as directory:
            client = HistoryClient(directory, None, history={"messages": []})
            with self.assertRaises(GatewayRequestError) as caught:
                list(client.send_stream(session_key="agent:main:agentbridge-workspace:direct:test-account",
                    endpoint_key="workspace:test", grant="g" * 48, message="read only", idempotency_key="run-current"))
            self.assertEqual(caught.exception.code, "GATEWAY_FINAL_HISTORY_PENDING")
            self.assertFalse(caught.exception.details["safeToRetry"])
            self.assertEqual(client.stream_calls, 1)
            self.assertEqual(client.aborts, 0)

    def test_final_reconciliation_obeys_remaining_run_deadline(self):
        with TemporaryDirectory() as directory:
            client = HistoryClient(directory, None)
            result, _ = client._reconcile_accepted_run(
                session_key="session", run_id="run-current", deadline=time.monotonic() + 0.5)
            self.assertIsNone(result)
            self.assertEqual(client.history_calls, 0)

    def test_empty_text_cannot_use_a_receipt(self):
        receipt = {"schemaVersion": "agentbridge.gateway-final.v1", "source": "local-same-socket-history",
                   "sessionKey": "session", "runId": "run-current", "observedRunId": "run-current"}
        self.assertFalse(_local_final_history_receipt({"text": " ", "agentbridgeFinalHistory": receipt}, "session", "run-current"))


class HistoryClient(OpenClawGatewayClient):
    def __init__(self, directory, receipt, *, history=None):
        super().__init__(url="ws://fixture.invalid", token_file=Path(directory) / "unused-token",
                         state_dir=directory, retry_sleep=lambda _delay: None)
        self.receipt = receipt
        self.history = history if history is not None else {"messages": [
            {"role": "user", "idempotencyKey": "run-current:user"},
            {"role": "assistant", "content": "fallback persisted", "stopReason": "stop"}]}
        self.history_calls = self.stream_calls = self.aborts = 0

    def _stream_payload(self, payload):
        self.stream_calls += 1
        yield {"type": "accepted", "runId": "run-current"}
        yield {"type": "chat", "runId": "run-current", "state": "final", "text": "local persisted",
               "agentbridgeFinalHistory": self.receipt}

    def call(self, method, params=None, *, timeout_seconds=30):
        self.history_calls += 1
        return self.history

    def abort_chat(self, **kwargs):
        self.aborts += 1
