"""Real Python->Node gateway bridge against a clock-accelerated fake socket."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from agentbridge.workspace.gateway import GatewayRequestError, OpenClawGatewayClient


SESSION = "agent:main:agentbridge-workspace:direct:observation-fixture"
RUN = "observation-fixture-run"


class Bridge(OpenClawGatewayClient):
    def __init__(self, directory, *, history):
        directory = Path(directory)
        token = directory / "synthetic.token"
        token.write_text("g" * 48)
        token.chmod(0o600)
        super().__init__(url="ws://fake-gateway.local", token_file=token, state_dir=directory,
                         retry_sleep=lambda _: None)
        self.history = history
        self.history_calls = 0
        self.stream_payloads = []

    def _stream_payload(self, payload):
        self.stream_payloads.append(dict(payload))
        yield from super()._stream_payload(payload)

    def call(self, method, params=None, *, timeout_seconds=30):
        if method == "chat.history":
            self.history_calls += 1
            return deepcopy(self.history)
        return super().call(method, params, timeout_seconds=timeout_seconds)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    fake = Path(__file__).parent / "support/fake_gateway_websocket.mjs"
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("NODE_OPTIONS", "--import=" + fake.resolve().as_uri())
    monkeypatch.setenv("AB_GATEWAY_FAKE_SCENARIO", "timeout_abort_race_with_tool")
    monkeypatch.setenv("AB_GATEWAY_FAKE_TRACE", str(trace))
    monkeypatch.setenv("AB_GATEWAY_INITIAL_RUN_ID", RUN)
    return tmp_path, trace


def requests(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()]


def send(client, **kwargs):
    return list(client.send_stream(session_key=SESSION, endpoint_key="workspace:observation-fixture",
        grant="a" * 48, message="Synthetic bounded read", idempotency_key=RUN,
        timeout_seconds=10, retry_before_accept=False, **kwargs))


def history(*, session=SESSION, run=RUN):
    return {"sessionKey": session, "messages": [
        {"role": "user", "idempotencyKey": run + ":user", "runId": run},
        {"role": "tool", "content": "synthetic tool receipt", "runId": run},
        {"role": "assistant", "content": "authoritative late final", "runId": run, "stopReason": "stop"}]}


def test_real_bridge_flag_prevents_abort_and_recovers_same_run_late_final(fixture):
    directory, trace = fixture
    client = Bridge(directory, history=history())
    events = send(client, abort_accepted_on_close=False)
    assert events[-1]["state"] == "final" and events[-1]["recovered"] is True
    assert events[-1]["runId"] == RUN and events[-1]["text"] == "authoritative late final"
    assert client.history_calls == 1
    assert client.stream_payloads[0]["abortAcceptedOnClose"] is False
    assert client.stream_payloads[0]["timeoutMs"] == 10000  # Execution limit was not raised.
    assert sum(row["method"] == "chat.send" for row in requests(trace)) == 1
    assert not any(row["method"] == "chat.abort" and row["runId"] for row in requests(trace))


def test_unknown_after_observation_timeout_never_replays_or_aborts(fixture):
    directory, trace = fixture
    client = Bridge(directory, history={"messages": []})
    with pytest.raises(GatewayRequestError) as caught:
        send(client, abort_accepted_on_close=False)
    assert caught.value.code == "GATEWAY_RUN_OBSERVATION_TIMEOUT"
    assert caught.value.details["safeToRetry"] is False and caught.value.details["abortRequested"] is False
    assert caught.value.details["hadToolActivity"] is True and client.history_calls >= 1
    assert sum(row["method"] == "chat.send" for row in requests(trace)) == 1
    assert not any(row["method"] == "chat.abort" and row["runId"] for row in requests(trace))


@pytest.mark.parametrize("payload", [history(run="another-run"), history(session="another-workspace-account")])
def test_recovery_never_inherits_another_run_or_workspace_identity(fixture, payload):
    directory, trace = fixture
    client = Bridge(directory, history=payload)
    with pytest.raises(GatewayRequestError) as caught:
        send(client, abort_accepted_on_close=False)
    assert caught.value.code == "GATEWAY_RUN_OBSERVATION_TIMEOUT" and caught.value.details["safeToRetry"] is False
    assert sum(row["method"] == "chat.send" for row in requests(trace)) == 1
    assert not any(row["method"] == "chat.abort" and row["runId"] for row in requests(trace))


def test_default_abort_policy_remains_compatible(fixture):
    directory, trace = fixture
    client = Bridge(directory, history={"messages": []})
    with pytest.raises(GatewayRequestError) as caught:
        send(client)
    assert caught.value.code == "GATEWAY_RUN_TIMEOUT_ABORTED"
    assert caught.value.details["aborted"] is True
    assert client.stream_payloads[0]["abortAcceptedOnClose"] is True
    assert any(row["method"] == "chat.abort" and row["runId"] == RUN for row in requests(trace))


def test_explicit_cancel_is_not_disabled_by_durable_observation(fixture):
    directory, trace = fixture
    client = Bridge(directory, history={"messages": []})
    with pytest.raises(GatewayRequestError):
        send(client, abort_accepted_on_close=False)
    assert not any(row["method"] == "chat.abort" and row["runId"] for row in requests(trace))
    client.abort_chat(session_key=SESSION, run_id=RUN)
    assert sum(row["method"] == "chat.abort" and row["runId"] == RUN for row in requests(trace)) == 1


def test_connection_loss_after_final_candidate_stays_read_only(fixture, monkeypatch):
    directory, trace = fixture
    monkeypatch.setenv("AB_GATEWAY_FAKE_SCENARIO", "final_close")
    client = Bridge(directory, history=history())
    events = send(client, abort_accepted_on_close=False)
    assert events[-1]["text"] == "authoritative late final" and events[-1]["runId"] == RUN
    assert sum(row["method"] == "chat.send" for row in requests(trace)) == 1
    assert not any(row["method"] == "chat.abort" and row["runId"] for row in requests(trace))
