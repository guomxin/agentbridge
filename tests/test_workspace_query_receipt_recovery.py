"""Revoked pairing may settle an exact old receipt, but cannot issue or send again."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
from nanobot_agentbridge.workspace_query_delivery import BINDING_FIELDS, acknowledge_receipt, record_claim


@pytest.mark.parametrize("host_type", ["nanobot", "pi"])
@pytest.mark.parametrize("mode", ["workspace_query_v2", "workspace_text_v2"])
def test_only_exact_original_binding_and_credential_can_settle_after_revocation(tmp_path, monkeypatch, host_type, mode):
    web = pytest.importorskip("aiohttp.web")
    token = tmp_path / "owner.token"
    token.write_text("synthetic-original-owner-credential")
    token.chmod(0o600)
    owner = {"user_subject": "first", "endpoint_id": str(uuid4()), "endpoint_key": "workspace:test:first",
             "session_key": "workspace-session", "credential_ref": "web-first", "tokenFile": str(token)}
    original = {**owner, "endpoint_id": str(uuid4()), "endpoint_key": "telegram:8001:501",
                "session_key": "original-private-conversation", "credential_ref": "tg-" + "a" * 64,
                "workspaceEndpointId": owner["endpoint_id"], "workspaceEndpointKey": owner["endpoint_key"]}
    delivery, task, pair = str(uuid4()), str(uuid4()), "a" * 64
    path = tmp_path / "coordinated-runs.sqlite3"
    record_claim(path, original["endpoint_id"], delivery, 1, task, pair, record=original, mode=mode,
                 part_index=1, part_count=2 if mode == "workspace_text_v2" else 1)
    called = []
    async def call(name, args, meta):
        assert name == "agentbridge_host_workspace_result_view"
        assert args == {"agent_host": host_type, "action": "ack", "endpoint_key": original["endpoint_key"],
                        "pairing_ref": pair, "delivery_id": delivery, "attempt_count": 1, "outcome": "sent", "receipt": 9}
        called.append(args)
        return SimpleNamespace(isError=False, structuredContent={"recorded": True})
    session = SimpleNamespace(call_tool=call)
    def payload(record, run):
        return {"subject": record["user_subject"], "endpoint": record["endpoint_id"],
                "endpointKey": record["endpoint_key"], "conversation": record["session_key"],
                "userDir": str(tmp_path), "tokenFile": record["tokenFile"], "hostType": host_type}
    host = SimpleNamespace(bindings={tuple(owner[k] for k in BINDING_FIELDS): owner}, worker_payload=payload)
    @asynccontextmanager
    async def nano_session(actual_host, request, *, lane):
        assert actual_host is host and request["endpoint"] == owner["endpoint_id"]
        assert lane == "workspace-query-receipt"
        yield session, {}, "L3"
    @asynccontextmanager
    async def pi_session(record, *, lane):
        assert record == owner and lane == "workspace-query-receipt"
        yield SimpleNamespace(session=session, meta={}, level="L3")
    monkeypatch.setattr("nanobot_agentbridge.session_pool.open_host_session", nano_session)
    host.control_sessions = SimpleNamespace(borrow=pi_session)
    body = {"binding": {k: original[k] for k in BINDING_FIELDS}, "deliveryId": delivery, "sourceTaskId": task,
            "pairingRef": pair, "attempt": 1, "action": "ack", "outcome": "sent", "receipt": 9}
    if mode == "workspace_text_v2":
        body.pop("sourceTaskId")
        body.update(sourceEntryId=task, partIndex=1, partCount=2)
    def settle(value):
        return asyncio.run(acknowledge_receipt(host, value, host_type=host_type))
    # No Telegram route resolver exists: neither a restart nor revocation may
    # force this receipt back through pairing, issue or begin.
    assert settle(body) == {"recorded": True}
    for changed in ({"action": "issue"}, {"action": "begin"}, {"attempt": 2},
                    {"sourceTaskId": str(uuid4())}, {"pairingRef": "b" * 64},
                    {"binding": {**body["binding"], "credential_ref": "tg-new-generation"}},
                    {"binding": {**body["binding"], "user_subject": "second"}}):
        with pytest.raises(web.HTTPForbidden):
            settle({**body, **changed})
    token.write_text("synthetic-rotated-owner-credential")
    with pytest.raises(web.HTTPForbidden):
        settle(body)
    assert len(called) == 1
