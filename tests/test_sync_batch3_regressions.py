"""Regression tests for Batch 3 sync/delivery hardening across NanoBot, Pi, Reference-Host, and Central."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in ("integrations/nanobot-agentbridge", "integrations/pi-agentbridge", "integrations/reference-host"):
    path_str = str(ROOT / extra)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from tests.support.nanobot_telegram_setup import INSTANCE  # noqa: E402
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401,E402
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401,E402
from tests.test_telegram_pairing_revocation import migration_setup  # noqa: E402


def test_ensure_host_task_enforces_migrated_pairing_protocol(mirror, pair_fixture):
    service, links, accounts, owner, _, token = migration_setup(mirror, pair_fixture)
    args = (accounts[owner], INSTANCE, 8001)
    challenge = links.begin(*args, migrate=True)
    proof = links.present(owner, INSTANCE, 8001, 501, challenge["code"], token_id=token, protocol=2)
    assert proof
    links.confirm(*args, challenge["challenge_id"], proof)
    endpoint = links.ensure_endpoint(owner, token, INSTANCE, 8001, 501, protocol=2)

    common = dict(
        user_subject=owner,
        token_id=token,
        agent_host="nanobot",
        host_task_key=str(uuid4()),
        endpoint_key=endpoint["endpointKey"],
        client_type="telegram",
        external_subject="501",
        conversation_ref=endpoint["conversationRef"],
        title="迁移后任务",
        account_id="8001",
        host_instance_id=INSTANCE,
        host_version="0.3.2",
    )
    with pytest.raises(PermissionError):
        service.ensure_host_task(**common)

    created = service.ensure_host_task(**common, pairing_protocol=2)
    assert created["endpoint"]["endpointId"] == endpoint["endpointId"]
    assert created["task"]["originEndpointId"] == endpoint["endpointId"]


def test_bound_driver_sends_nanobot_protocol_v2_and_checks_pairing_generation():
    from reference_host.bound_coordinator import BoundDriver, IMPLEMENTATION
    from reference_host.identity import IdentityConfig, IdentityRegistry

    pair = "a" * 64
    calls = []

    class FakeClient:
        async def call_tool(self, name, args, *, meta=None, recovery_class=None):
            calls.append((name, args, meta))
            if name == "agentbridge_host_identity_profile":
                return SimpleNamespace(is_error=False, payload=lambda: {"telegramIdentityVerified": True})
            if name == "agentbridge_host_telegram_endpoint":
                return SimpleNamespace(
                    is_error=False,
                    payload=lambda: {
                        "endpointId": "ep-tg",
                        "endpointKey": "telegram:8001:501",
                        "conversationRef": "conv-tg",
                        "workspaceEndpointId": "ep-web",
                        "pairingRef": state["pair"],
                    },
                )
            raise AssertionError(name)

    state = {"pair": pair}
    req = {
        "endpoint": "ep-tg",
        "endpointKey": "telegram:8001:501",
        "conversation": "conv-tg",
        "workspaceEndpointId": "ep-web",
        "pairingRef": pair,
    }
    driver = BoundDriver(
        req,
        None,
        identities=IdentityRegistry([
            IdentityConfig(
                label="owner",
                token_env="AGENTBRIDGE_UNUSED_TOKEN",
                endpoint_key="telegram:8001:501",
                external_subject="501",
                conversation_ref="conv-tg",
            )
        ]),
        state=SimpleNamespace(),
        mcp_url="http://127.0.0.1:8781/mcp",
        implementation=IMPLEMENTATION,
        instance_id="nano-office",
    )
    driver._clients["owner"] = FakeClient()

    assert driver.host_context()["io.agentbridge/telegram-pairing-version"] == 2
    assert IMPLEMENTATION.version == "0.3.2"
    asyncio.run(driver.check_telegram())
    assert [name for name, _, _ in calls] == [
        "agentbridge_host_identity_profile",
        "agentbridge_host_telegram_endpoint",
    ]
    assert all(meta.get("io.agentbridge/telegram-pairing-version") == 2 for _, _, meta in calls)

    state["pair"] = "b" * 64
    with pytest.raises(PermissionError, match="pairing generation"):
        asyncio.run(driver.check_telegram())


def test_deployment_token_supports_rolling_versions_and_token_rotation(mirror, pair_fixture):
    from agentbridge.core.mcp_identities import McpIdentityTokenStore
    from agentbridge.workspace import telegram_pair_migration as migration
    from tests.test_workspace_query_projection import register

    service, links, accounts, owner, _, old_token = migration_setup(mirror, pair_fixture)
    # 1. Rolling version 0.3.1 stays accepted even when latest supported version is 0.3.2.
    with service.tasks._connect() as db:
        db.execute("UPDATE workspace_host_instances SET adapter_version='0.3.1' WHERE instance_id=?", (INSTANCE,))
        db.execute("UPDATE agent_host_registrations SET host_version='0.3.1' WHERE user_subject=? AND host_instance_id=?", (owner, INSTANCE))
        assert migration.deployment_token(db, owner, INSTANCE, "8001") == old_token

    # 2. Rotating the provisioned personal token with the exact deployment label must not deadlock
    #    even while client_endpoints.token_id still holds old_token and before any telegram endpoint exists.
    with service.tasks._connect() as db:
        cred_ref = db.execute(
            """SELECT b.credential_ref FROM workspace_host_bindings b
               JOIN workspace_accounts a USING(account_id)
               WHERE a.user_subject=? AND b.instance_id=?""",
            (owner, INSTANCE),
        ).fetchone()[0]
    identities = McpIdentityTokenStore(service.db_path)
    rotated = identities.issue(
        user_subject=owner,
        expected_principal_ref=owner,
        label=f"workspace-nanobot:{INSTANCE}:{cred_ref}",
    )["token_id"]
    identities.revoke(old_token)
    register(service, owner, "nanobot", INSTANCE, rotated)
    with service.tasks._connect() as db:
        assert migration.deployment_token(db, owner, INSTANCE, "8001") == rotated

    # 3. An unrelated personal token without matching label or endpoint must still be rejected.
    unrelated = identities.issue(
        user_subject=owner,
        expected_principal_ref=owner,
        label="unrelated-cli-token",
    )["token_id"]
    identities.revoke(rotated)
    register(service, owner, "nanobot", INSTANCE, unrelated)
    with service.tasks._connect() as db:
        assert migration.deployment_token(db, owner, INSTANCE, "8001") is None


def test_enrollment_senders_receipt_recovery_across_query_media_and_history(tmp_path, monkeypatch):
    from nanobot_agentbridge import telegram_history_receipts, telegram_media_receipts, workspace_query_delivery

    token_file = tmp_path / "personal.token"
    token_file.write_text("synthetic-enrolled-token")
    token_file.chmod(0o600)

    owner = {
        "user_subject": "enrolled-user",
        "endpoint_id": str(uuid4()),
        "endpoint_key": "workspace:nano-office:enrolled-user",
        "session_key": "workspace:nano-office:enrolled-user",
        "credential_ref": "cred-enrolled",
        "tokenFile": str(token_file),
    }
    pair = "c" * 64
    tg_record = {
        **owner,
        "endpoint_id": str(uuid4()),
        "endpoint_key": "telegram:8001:501",
        "session_key": "agentbridge:telegram:enrolled",
        "credential_ref": "tg-" + pair,
        "workspaceEndpointId": owner["endpoint_id"],
        "workspaceEndpointKey": owner["endpoint_key"],
    }
    binding = {k: tg_record[k] for k in workspace_query_delivery.BINDING_FIELDS}
    delivery_id = str(uuid4())
    task_id = str(uuid4())
    received_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    db_path = tmp_path / "coordinated-runs.sqlite3"

    # Seed query claim, media source + manifest, and history manifest under the enrolled user's directory.
    workspace_query_delivery.record_claim(db_path, tg_record["endpoint_id"], delivery_id, 1, task_id, pair, record=tg_record)
    with sqlite3.connect(db_path) as db:
        telegram_media_receipts._initialize(db)
        route = {k: tg_record[k] for k in telegram_media_receipts.SOURCE_FIELDS}
        token_hash = hashlib.sha256(b"synthetic-enrolled-token").hexdigest()
        db.execute(
            "INSERT INTO telegram_media_sources VALUES(?,?,?,?,?,?,?)",
            (owner["user_subject"], "tg:8001:501:42", tg_record["endpoint_id"], received_at, pair, json.dumps(route, sort_keys=True), token_hash),
        )
    media_meta = {
        "messageId": 42,
        "ordinal": 0,
        "fileName": "附件",
        "mimeType": "application/octet-stream",
        "byteSize": 0,
        "sha256": None,
        "receivedAt": received_at,
        "pairingRef": pair,
        "failure": "unsupported",
    }
    telegram_media_receipts._freeze_manifest(db_path, tg_record, "tg:8001:501:42", media_meta)
    history_body = {
        "binding": binding,
        "messageId": 42,
        "role": "user",
        "text": "已开通用户原消息",
        "runId": None,
        "projectionMode": "telegram_received_v2",
        "sourceState": None,
    }
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS telegram_history_manifests(subject TEXT NOT NULL, request_key TEXT NOT NULL, role TEXT NOT NULL, manifest TEXT NOT NULL, PRIMARY KEY(subject,request_key,role))"
        )
        db.execute(
            "INSERT INTO telegram_history_manifests VALUES(?,?,?,?)",
            (owner["user_subject"], "tg:8001:501:42", "user", telegram_history_receipts.manifest(history_body)),
        )

    # Host has EMPTY bindings (simulating Enrollment.pop() at startup) and resolves via enrollment.web_record().
    routes = SimpleNamespace(web={})
    resolved_calls = []

    class FakeEnrollment:
        def __init__(self):
            self.identities = {
                owner["user_subject"]: {
                    "user_subject": owner["user_subject"],
                    "endpoint_key": "telegram:8001:501",
                    "credential_ref": owner["credential_ref"],
                    "tokenFile": str(token_file),
                }
            }

        async def web_record(self, sender):
            resolved_calls.append(sender)
            routes.web[owner["user_subject"]] = owner
            return owner

    host = SimpleNamespace(
        instance="nano-office",
        bindings={},
        telegram_routes=routes,
        enrollment=FakeEnrollment(),
        card_origins=[],
        worker_payload=lambda r, _: {"userDir": str(tmp_path), "endpoint": r["endpoint_id"], "endpointKey": r["endpoint_key"], "hostType": "nanobot"},
    )

    expiry = (datetime.fromisoformat(received_at) + timedelta(seconds=telegram_media_receipts.TTL)).isoformat()
    entry_id = str(uuid4())

    class FakeSession:
        async def call_tool(self, name, args, meta=None):
            if name == "agentbridge_host_workspace_result_view":
                return SimpleNamespace(isError=False, structuredContent={"recorded": True})
            if name == "agentbridge_host_telegram_media_append":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "recorded": True,
                        "mediaReceipt": {
                            "entryId": entry_id,
                            "sourceEndpointId": tg_record["endpoint_id"],
                            "sourceMessageId": 42,
                            "ordinal": 0,
                            "fileName": "附件",
                            "mimeType": "application/octet-stream",
                            "byteSize": 0,
                            "sha256": None,
                            "receivedAt": received_at,
                            "failure": "unsupported",
                            "expiresAt": expiry,
                            "state": "unavailable",
                            "attachmentId": None,
                        },
                    },
                )
            if name == "agentbridge_host_timeline_append":
                return SimpleNamespace(
                    isError=False,
                    structuredContent={
                        "status": "succeeded",
                        "endpoint": {"endpointId": tg_record["endpoint_id"]},
                        "entry": {"entryId": entry_id, "role": "user", "retained": False},
                        "projectionReceipt": {
                            "entryId": entry_id,
                            "messageKey": args["message_key"],
                            "sourceEndpointId": tg_record["endpoint_id"],
                            "role": "user",
                            "textSha256": hashlib.sha256("已开通用户原消息".encode()).hexdigest(),
                        },
                    },
                )
            raise AssertionError(name)

    @asynccontextmanager
    async def opened(*_, **__):
        yield FakeSession(), {}, "L3"

    monkeypatch.setattr("nanobot_agentbridge.session_pool.open_host_session", opened)
    monkeypatch.setattr("nanobot_agentbridge.telegram_media_receipts.open_host_session", opened)

    async def run():
        ack_res = await workspace_query_delivery.acknowledge_receipt(
            host,
            {
                "binding": binding,
                "action": "ack",
                "deliveryId": delivery_id,
                "attempt": 1,
                "sourceTaskId": task_id,
                "pairingRef": pair,
                "outcome": "sent",
                "receipt": 99,
            },
            host_type="nanobot",
        )
        assert ack_res == {"recorded": True}
        assert resolved_calls == ["501"]

        media_res = await telegram_media_receipts.recover_route(host, {"binding": binding, **media_meta}, host_type="nanobot")
        assert media_res["recorded"] is True

        history_res = await telegram_history_receipts.recover_route(host, history_body, host_type="nanobot")
        assert history_res == {"recorded": True}

    asyncio.run(run())


def test_stale_pairing_eviction_on_nanobot_and_pi(monkeypatch):
    from nanobot_agentbridge.telegram_routes import TelegramRoutes
    from pi_agentbridge.telegram_routes import PiTelegramRoutes

    fields = ("user_subject", "endpoint_id", "endpoint_key", "session_key", "credential_ref")
    owner = {
        "user_subject": "alice",
        "endpoint_id": str(uuid4()),
        "endpoint_key": "workspace:office:alice",
        "session_key": "workspace:office:alice",
        "credential_ref": "cred-a",
    }
    host = SimpleNamespace(instance="office", bindings={"w": owner}, worker_payload=lambda r, _: r)
    routes = TelegramRoutes(host, {"telegramPilot": {"botId": 8001, "username": "fixture_bot"}})
    base = routes.record(owner, "501")
    state = {"active": True, "pair": "a" * 64}

    async def call_tool(name, args, meta=None):
        assert name == "agentbridge_host_telegram_endpoint"
        return SimpleNamespace(
            isError=not state["active"],
            structuredContent={
                "endpointId": base["endpoint_id"],
                "endpointKey": base["endpoint_key"],
                "conversationRef": base["session_key"],
                "workspaceEndpointId": owner["endpoint_id"],
                "workspaceEndpointKey": owner["endpoint_key"],
                "generationScoped": True,
                "pairingRef": state["pair"],
            },
        )

    @asynccontextmanager
    async def opened(*_, **__):
        yield SimpleNamespace(call_tool=call_tool), {}, "L3"

    async def verified(*_, **__):
        return state["active"]

    monkeypatch.setitem(
        sys.modules,
        "nanobot_agentbridge.server",
        SimpleNamespace(BINDING_KEYS=fields, binding_key=lambda r: tuple(r[k] for k in fields), identity_denial=lambda _: False),
    )
    monkeypatch.setattr("nanobot_agentbridge.session_pool.open_host_session", opened)
    monkeypatch.setattr("nanobot_agentbridge.telegram_routes.verify_telegram_binding", verified)

    async def check_nanobot():
        first = (await routes.action("telegram-route", {"botId": "8001", "senderId": "501"}))["binding"]
        first_key = tuple(first[k] for k in fields)
        assert routes.get(first_key) is not None
        assert routes.get(first_key)["pairingRef"] == "a" * 64

        state["pair"] = "b" * 64
        second = (await routes.action("telegram-route", {"botId": "8001", "senderId": "501"}))["binding"]
        second_key = tuple(second[k] for k in fields)
        assert routes.get(first_key) is None  # old generation evicted
        assert routes.get(second_key) is not None

        state["active"] = False
        revoked = await routes.action("telegram-route", {"botId": "8001", "senderId": "501"})
        assert revoked == {"binding": None}
        assert routes.get(second_key) is None  # revoked generation evicted

    asyncio.run(check_nanobot())

    # Pi _last_routed must also clear when current() fails.
    pi_host = SimpleNamespace(
        instance="pi-office",
        spec={"telegramPilot": {"botId": 8801, "allowedSenders": {"alice": "501"}}},
        bindings={"w": owner},
    )
    pi_routes = PiTelegramRoutes(pi_host)
    pi_routes._last_routed = {"stale": True}
    bad_session = SimpleNamespace(call_tool=lambda *a, **k: asyncio.sleep(0, result=SimpleNamespace(isError=True, structuredContent={})))
    assert asyncio.run(pi_routes.current(bad_session, {})) is None
    assert pi_routes._last_routed is None


def test_inbound_media_retries_transient_errors_and_accepts_multidot_paths_and_extra_extensions(monkeypatch):
    from nanobot_agentbridge import telegram_media
    from nanobot_agentbridge.telegram import TelegramUnavailable

    monkeypatch.setattr(telegram_media.time, "sleep", lambda *_: None)

    # FILE_PATH allows multi-dot & extensionless Telegram paths, forbids traversal & URLs.
    assert telegram_media.FILE_PATH.fullmatch("documents/my.report.v2.pdf")
    assert telegram_media.FILE_PATH.fullmatch("documents/file_42")
    assert not telegram_media.FILE_PATH.fullmatch("../etc/passwd")
    assert not telegram_media.FILE_PATH.fullmatch("documents/../secret.png")
    assert not telegram_media.FILE_PATH.fullmatch("https://example.invalid/a.png")

    # _select recognizes .jpeg, .yml, .htm with octet-stream or missing mime_type.
    for fname, expected in (("photo.jpeg", "image/jpeg"), ("cfg.yml", "text/yaml"), ("page.htm", "text/html")):
        _, mime = telegram_media._select({"document": {"file_id": "abc", "mime_type": "application/octet-stream", "file_name": fname}})
        assert mime == expected

    # download() retries transient 503 and OSError up to 3 attempts and succeeds.
    payload = b"\x89PNG\r\n\x1a\nvalid"
    attempts = {"getFile": 0, "get": 0}

    class FakeResp:
        def __init__(self, status, body):
            self.status = status
            self._body = body
            self._done = False

        def getheader(self, name):
            return str(len(self._body)) if name.lower() == "content-length" else None

        def read1(self, _n):
            if self._done:
                return b""
            self._done = True
            return self._body

    class FakeConn:
        def request(self, _method, _path):
            attempts["get"] += 1
            if attempts["get"] == 1:
                raise OSError("transient tcp reset")

        def getresponse(self):
            if attempts["get"] == 2:
                return FakeResp(503, b"busy")
            return FakeResp(200, payload)

        def close(self):
            pass

    class FakeAPI:
        _token = "tok"

        def call(self, method, **kwargs):
            attempts["getFile"] += 1
            assert method == "getFile"
            return {"file_id": kwargs["file_id"], "file_path": "documents/my.image.v1", "file_size": len(payload)}

        def _connect(self):
            return FakeConn()

    assert telegram_media.download(FakeAPI(), {"file_id": "img1", "file_size": len(payload)}) == payload
    assert attempts["get"] == 3

    # Deterministic 404 fails immediately with MediaError without retrying.
    calls_404 = {"n": 0}

    class Conn404:
        def request(self, *_):
            calls_404["n"] += 1

        def getresponse(self):
            return FakeResp(404, b"missing")

        def close(self):
            pass

    api404 = FakeAPI()
    api404._connect = lambda: Conn404()
    with pytest.raises(telegram_media.MediaError):
        telegram_media.download(api404, {"file_id": "img404"})
    assert calls_404["n"] == 1


def test_workspace_query_legacy_schema_migration_attempt_advancement_and_duplicate_receipt(tmp_path):
    from nanobot_agentbridge import workspace_query_delivery as wqd
    from nanobot_agentbridge.notifications import TerminalMessages

    # 1. Legacy workspace_query_claims schema (without mode/part_index/part_count) migrates on handle_request.
    db_path = tmp_path / "legacy-claims.sqlite3"
    delivery_id, task_id, endpoint_id, pair = str(uuid4()), str(uuid4()), str(uuid4()), "d" * 64
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE workspace_query_claims(delivery TEXT PRIMARY KEY, endpoint TEXT NOT NULL, task TEXT NOT NULL, pairing TEXT NOT NULL, attempt INTEGER NOT NULL)"
        )
        db.execute("INSERT INTO workspace_query_claims VALUES(?,?,?,?,?)", (delivery_id, endpoint_id, task_id, pair, 1))

    class AckSession:
        async def call_tool(self, name, args, meta=None):
            assert name == "agentbridge_host_workspace_result_view" and args["action"] == "ack"
            return SimpleNamespace(isError=False, structuredContent={"recorded": True})

    res = asyncio.run(
        wqd.handle_request(
            AckSession(),
            {},
            {"endpoint": endpoint_id, "endpointKey": "telegram:8001:501", "hostType": "nanobot"},
            db_path,
            {
                "deliveryId": delivery_id,
                "attempt": 1,
                "sourceTaskId": task_id,
                "pairingRef": pair,
                "action": "ack",
                "outcome": "sent",
                "receipt": 12,
            },
            frozenset(),
        )
    )
    assert res == {"recorded": True}

    # 2. Re-claim with attempt=2 advances attempt/claim_json so drain() marks acknowledged=1 (no 403 spin).
    pilot_db = sqlite3.connect(tmp_path / "pilot.db")
    pilot_db.row_factory = sqlite3.Row
    binding = {"user_subject": "alice", "endpoint_id": endpoint_id, "endpoint_key": "telegram:8001:501", "session_key": "s", "credential_ref": "tg-" + pair}
    acked = []

    def req(route, body, **_):
        acked.append(body)
        return {"recorded": True}

    pilot = SimpleNamespace(
        db=pilot_db,
        gateway=SimpleNamespace(_request=req),
        host_type="nanobot",
        bot_id=8001,
        navigation=SimpleNamespace(remember=lambda *_: None),
        _binding=lambda _chat: (binding, "digest"),
    )
    terminal = TerminalMessages(pilot)
    wq = terminal.workspace_queries
    try:
        dedupe = "result_view:" + task_id
        claim1 = wqd.Claim(501, binding, "digest", delivery_id, 1, task_id, pair)
        with pilot_db:
            pilot_db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,?,'delivered',77)", (8001, 501, dedupe, delivery_id))
            pilot_db.execute(
                "INSERT INTO telegram_workspace_query_attempts VALUES(?,?,?,?,NULL,NULL,?,0,0)",
                (8001, 501, delivery_id, 1, json.dumps(wqd.asdict(claim1))),
            )
        wq.claim(501, binding, "digest", {"kind": "workspace_query_v2", "deliveryId": delivery_id, "attempt": 2, "sourceTaskId": task_id, "pairingRef": pair})
        for _ in range(10):
            if not wq.pending:
                break
            for _, _, fut in list(wq.pending.values()):
                fut.result(timeout=2)
            wq.drain()
        row = pilot_db.execute("SELECT attempt, acknowledged FROM telegram_workspace_query_attempts WHERE delivery=?", (delivery_id,)).fetchone()
        assert tuple(row) == (2, 1)
        assert acked[-1]["attempt"] == 2 and acked[-1]["outcome"] == "sent" and acked[-1]["receipt"] == 77

        # 3. Duplicate sendMessage receipt is treated as delivery_unknown (not delivered).
        other_delivery = str(uuid4())
        other_task = str(uuid4())
        claim2 = wqd.Claim(501, binding, "digest", other_delivery, 1, other_task, pair)
        with pilot_db:
            pilot_db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,?,'beginning',NULL)", (8001, 501, claim2.dedupe_key, other_delivery))
        pilot.api = SimpleNamespace(call=lambda *a, **k: {"message_id": 77, "from": {"id": 8001}, "chat": {"id": 501, "type": "private"}})
        wq._send(claim2, ({"text": "hi"}, time.time() + 60))
        saved = pilot_db.execute("SELECT state, receipt FROM telegram_card_deliveries WHERE dedupe_key=?", (claim2.dedupe_key,)).fetchone()
        assert tuple(saved) == ("delivery_unknown", None)
    finally:
        wq.executor.shutdown()
        pilot_db.close()


def test_telegram_received_v2_uses_none_run_id_even_after_run_admission(tmp_path, monkeypatch):
    from tests.support.nanobot_telegram_loopback import fixture as transport

    with transport(tmp_path) as (state, _, create):
        pilot = create()
        binding, digest = pilot._binding(501)
        with pilot.db:
            pilot.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,'accepted','run-after-intake',NULL)", (8001, 501, 15, binding["user_subject"], digest))
            pilot.db.execute(
                "INSERT INTO telegram_inbound_sources VALUES(?,?,?,?,?,?)",
                (8001, 501, 15, json.dumps(binding), "a" * 64, datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
            )
            pilot.timeline.queue(501, 15, "user", "入站原问题", projection_mode="telegram_received_v2")
        seen = []

        def publish(b, msg_id, role, text, run_id, receipts, **kwargs):
            seen.append((kwargs.get("receipt_only"), run_id))
            return bool(kwargs.get("receipt_only"))

        monkeypatch.setattr(pilot.gateway, "publish_telegram_timeline", publish)
        pilot.timeline.flush()
        assert seen == [(True, None)]
        pilot.db.close()


def test_l2_reception_epoch_and_preflight_cached_binding_restart_retry(tmp_path, monkeypatch):
    from aiohttp import web
    from agentbridge.workspace.gateway import GatewayRequestError
    from nanobot_agentbridge.telegram_reception import preflight, receive, validate
    from pi_agentbridge.server import Host as PiHost
    from tests.support.nanobot_telegram_loopback import fixture as transport, update

    # 1. L2 receive() and validate() succeed with local reception proof without calling L3 media_source.
    token = tmp_path / "l2.token"
    token.write_text("synthetic-l2-token")
    token.chmod(0o600)
    pair = "e" * 64
    floor = int(time.time()) - 5
    record = {
        "user_subject": "alice",
        "endpoint_id": str(uuid4()),
        "endpoint_key": "telegram:8801:501",
        "session_key": "agentbridge:telegram:l2",
        "credential_ref": "tg-" + pair,
        "workspaceEndpointId": str(uuid4()),
        "workspaceEndpointKey": "workspace:pi-office:alice",
        "tokenFile": str(token),
    }
    calls = []

    class L2Session:
        async def call_tool(self, name, args, meta=None):
            calls.append(name)
            assert name == "agentbridge_host_telegram_endpoint"
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "endpointId": record["endpoint_id"],
                    "endpointKey": record["endpoint_key"],
                    "conversationRef": record["session_key"],
                    "pairingRef": pair,
                    "generationScoped": True,
                    "receptionFloor": floor,
                },
            )

    @asynccontextmanager
    async def opened_l2(*_, **__):
        yield L2Session(), {}, "L2"

    monkeypatch.setattr("nanobot_agentbridge.session_pool.open_host_session", opened_l2)
    host = SimpleNamespace(worker_payload=lambda r, _: {"userDir": str(tmp_path), "endpointKey": r["endpoint_key"], "hostType": "pi"})
    stamp_ok = datetime.fromtimestamp(floor + 2, timezone.utc).isoformat()
    stamp_old = datetime.fromtimestamp(floor, timezone.utc).isoformat()

    async def check_l2():
        with pytest.raises(web.HTTPForbidden):
            await receive(host, record, {"messageId": 9, "receivedAt": stamp_old})
        res = await receive(host, record, {"messageId": 9, "receivedAt": stamp_ok})
        assert res["accepted"] is True and res["generationScoped"] is True and res["pairingRef"] == pair
        validated = await validate(host, record, "tg:8801:501:9")
        assert validated == {"messageId": 9, "receivedAt": stamp_ok, "pairingRef": pair}
        assert calls == ["agentbridge_host_telegram_endpoint"] * 3

    asyncio.run(check_l2())

    # 2. Pi Host.action allows telegram-received-source at L2.
    dispatched = []

    async def fake_dispatch(h, rec, act, body):
        dispatched.append((act, rec["endpoint_key"]))
        return {"accepted": True, "generationScoped": True}

    monkeypatch.setattr("pi_agentbridge.channel_delivery.dispatch", fake_dispatch)
    pi_host = SimpleNamespace(
        level="L2",
        coordinator=None,
        telegram=SimpleNamespace(delivery_hint=lambda _k: asyncio.sleep(0, result=record)),
    )
    out = asyncio.run(PiHost.action(pi_host, "telegram-received-source", {"binding": {k: record[k] for k in ("user_subject", "endpoint_id", "endpoint_key", "session_key", "credential_ref")}, "messageId": 9, "receivedAt": stamp_ok}))
    assert out == {"accepted": True, "generationScoped": True}
    assert dispatched == [("telegram-received-source", "telegram:8801:501")]

    # 3. preflight() retries once when cached _paired_bindings hits BINDING_UNAVAILABLE after host restart.
    poller_dir = tmp_path / "poller"
    poller_dir.mkdir()
    with transport(poller_dir) as (state, _, create):
        pilot = create()
        binding, digest = pilot._binding(501)
        pilot._paired_bindings[501] = binding
        attempts = {"n": 0}
        original_req = pilot.gateway._request

        def flaky(route, body, **kw):
            if route == "telegram-received-source":
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise GatewayRequestError("NANOBOT_BINDING_UNAVAILABLE", "host restarted")
                return {"accepted": True, "generationScoped": False}
            return original_req(route, body, **kw)

        monkeypatch.setattr(pilot.gateway, "_request", flaky)
        assert preflight(pilot, update(1, message=21)["message"], 501, 21, binding, digest) is True
        assert attempts["n"] == 2
        pilot.db.close()


def test_pi_cross_endpoint_reference_parses_before_documents_and_handles_wrong_source():
    from pi_agentbridge.central import CentralTools
    from pi_agentbridge.server import Host

    record = {
        "user_subject": "one",
        "endpoint_id": "private",
        "endpoint_key": "telegram:8801:501",
        "session_key": "original",
        "credential_ref": "tg-" + "a" * 64,
    }
    emitted = []
    model_called = []

    async def current(*_):
        return record

    central = SimpleNamespace(level="L3", session=SimpleNamespace(), meta={}, binding_check=None)
    central.require_binding = lambda: CentralTools.require_binding(central)

    @asynccontextmanager
    async def bound(_):
        yield central

    async def invoke_model(*a, **k):
        model_called.append((a, k))
        return {"text": "should not run"}

    host = SimpleNamespace(
        telegram=SimpleNamespace(verified=current, current=current),
        bound_session=bound,
        invoke_model=invoke_model,
        ledger=SimpleNamespace(record_usage=lambda *_: None),
    )

    # 1. Explicit reference with attached document must be blocked before concatenation (never invoke model).
    with pytest.raises(PermissionError, match="text only"):
        asyncio.run(
            Host.execute(
                host,
                record,
                "run-doc",
                [],
                lambda *args: emitted.append(args),
                "继续网页消息 original-0，总结一下。",
                [],
                documents="附件正文内容",
            )
        )
    assert not model_called and not emitted

    # 2. Wrong source (referencing Telegram from Telegram) returns clean refusal without model/tools.
    ans = asyncio.run(
        Host.execute(
            host,
            record,
            "run-wrong",
            [],
            lambda *args: emitted.append(args),
            "继续电报消息 original-0，解释一下。",
            [],
        )
    )
    assert ans == "请在另一端引用这条消息。"
    assert emitted == [("delta", "请在另一端引用这条消息。")]
    assert not model_called


def test_pi_timeline_media_skips_superseded_token_rows_without_hol_blocking(tmp_path):
    from agentbridge.workspace import pi_timeline_media

    db = sqlite3.connect(tmp_path / "pi-media.db")
    db.row_factory = sqlite3.Row
    pi_timeline_media.initialize(db)
    db.executescript(
        """
        CREATE TABLE timeline_attachments (attachment_id TEXT, user_subject TEXT, message_key TEXT, ordinal INTEGER, state TEXT, expires_at TEXT, content_hash TEXT);
        CREATE TABLE agent_host_dispatches (dispatch_id TEXT, agent_host TEXT, user_subject TEXT, message_key TEXT, instance_id TEXT, origin_endpoint_id TEXT, attachment_refs_json TEXT);
        CREATE TABLE user_timeline (entry_id TEXT, user_subject TEXT, source_endpoint_id TEXT, role TEXT, entry_type TEXT, task_id TEXT, dedupe_key TEXT, payload_json TEXT);
        """
    )
    scope = ("alice", "pi-office", "ep-web", "ep-tg", "f" * 64)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    for idx, (did, tok) in enumerate((("d-old", "tok-old"), ("d-new", "tok-new"))):
        att_id = f"att-{idx}"
        msg_key = f"msg-{idx}"
        disp_id = f"disp-{idx}"
        ent_id = f"ent-{idx}"
        db.execute("INSERT INTO timeline_attachments VALUES(?,?,?,?,?,?,?)", (att_id, "alice", msg_key, 0, "ready", future, "h"))
        db.execute("INSERT INTO agent_host_dispatches VALUES(?,?,?,?,?,?,?)", (disp_id, "pi", "alice", msg_key, "pi-office", "ep-web", json.dumps([att_id])))
        db.execute("INSERT INTO user_timeline VALUES(?,?,?,?,?,?,?,?)", (ent_id, "alice", "ep-web", "user", "chat_message", None, "message:" + msg_key, json.dumps({"attachments": [{"attachmentId": att_id}]})))
        db.execute(
            """INSERT INTO pi_workspace_image_deliveries
               (delivery_id,dispatch_id,entry_id,subject,instance,source,destination,pair_proof,attachment_id,ordinal,token_id,endpoint_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, disp_id, ent_id, *scope, att_id, 0, tok, "telegram:8801:501"),
        )
    db.commit()

    claimed = pi_timeline_media._claim(db, scope, token_id="tok-new")
    assert claimed["available"] is True and claimed["deliveryId"] == "d-new"
    old_state = db.execute("SELECT state FROM pi_workspace_image_deliveries WHERE delivery_id='d-old'").fetchone()[0]
    assert old_state == "unavailable"
    db.close()


def test_navigation_redisplay_reclaims_delivery_unknown_card_and_arms_closure(tmp_path, monkeypatch):
    from tests.support.nanobot_telegram_loopback import BOT, fixture, update
    from tests.test_nanobot_telegram_presentation import _interaction

    with fixture(tmp_path) as (state, _, create):
        pilot = create()
        interaction = _interaction()
        card_id = interaction["interactionId"]
        task_id = str(uuid4())
        source_ref = str(uuid4())
        key = "interaction:" + card_id
        with pilot.db:
            pilot.db.execute("INSERT INTO telegram_card_deliveries VALUES(?,?,?,NULL,'delivery_unknown',NULL)", (BOT, 501, key))

        native_source = {"sourceRef": source_ref, "interactionId": card_id, "type": interaction["type"], "taskId": task_id, "runId": None}
        original = pilot.gateway._request

        def nav(action, body, **kw):
            if action != "telegram-task":
                return original(action, body, **kw)
            if body["operation"] == "list":
                return {"candidates": [{"taskId": task_id, "title": "合成审批", "status": "waiting_user"}]}
            return {"taskId": task_id, "state": "waiting_user", "interaction": interaction, "nativeCardSource": native_source}

        monkeypatch.setattr(pilot.gateway, "_request", nav)
        pilot.ingest(update(1, message=31, text="/pending"))
        card_row = pilot.db.execute("SELECT state, receipt FROM telegram_card_deliveries WHERE dedupe_key=?", (key,)).fetchone()
        watch_row = pilot.db.execute("SELECT card_key FROM telegram_native_card_watches WHERE card_key=?", (key,)).fetchone()
        assert card_row[0] == "delivered" and card_row[1] > 0
        assert watch_row is not None and watch_row[0] == key
        pilot.db.close()
