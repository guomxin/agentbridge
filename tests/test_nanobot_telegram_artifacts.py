"""Native Telegram documents travel only from a current, owned task through a private host."""

import asyncio
import base64
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.authorization_fixtures import grant_permissions
from tests.support.file_retention_acceptance import seed_retained_csv
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_document_downloads import _reference
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"))
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.telegram import TelegramAPI, TelegramUnavailable


@pytest.fixture
def origin(mirror):
    service, links, accounts, subject, _, _, _ = mirror
    web = service.workspace.current_host(accounts[subject])
    with service.tasks._connect() as db:
        token = db.execute(
            "SELECT token_id FROM client_endpoints WHERE endpoint_id=?", (web["endpoint_id"],)
        ).fetchone()[0]
    ep = links.ensure_endpoint(subject, token, INSTANCE, 8001, 501)
    task, _ = service.tasks.ensure_task(
        user_subject=subject,
        agent_host="nanobot",
        host_task_key="synthetic-file-request",
        origin_endpoint_id=ep["endpointId"],
        active_conversation_ref=ep["conversationRef"],
        title="导出测试文件",
    )
    grant_permissions(service.user_grants, subject, ["oa.certificate.read", "oa.certificate.download"])
    return SimpleNamespace(
        service=service,
        subject=subject,
        token_id=token,
        ep=ep,
        task=task["task_id"],
        scope={
            "user_subject": subject,
            "agent_host": "nanobot",
            "endpoint_key": ep["endpointKey"],
            "host_instance_id": INSTANCE,
        },
    )


def pdf(origin):
    documents = origin.service.document_downloads
    record = documents.create(
        user_subject=origin.subject,
        system_id="oa",
        session_id="synthetic-session",
        document=_reference(),
        filename="合成.pdf",
        document_type="patent_certificate",
        display_size="",
        card_base_url="https://fixture.invalid",
    )
    body = b"%PDF-test-document"
    documents.claim_for_prepare(record["download_id"], user_subject=origin.subject)
    ready = documents.mark_ready(record["download_id"], body=body, content_type="application/pdf")
    artifact, _ = origin.service.tasks.link_artifact(
        task_id=origin.task,
        user_subject=origin.subject,
        artifact={
            "artifact_type": "certificate_scan",
            "source_ref": record["download_id"],
            "filename": ready["filename"],
            "byte_size": len(body),
            "content_type": ready["content_type"],
            "download_url": ready["card_url"] + "/file",
            "expires_at": ready["expires_at"],
        },
    )
    return artifact, body


def csv_report(origin):
    result = seed_retained_csv(origin.service, user_subject=origin.subject, task_id=origin.task)
    # The shared fixture expires both entries after linking. Restore their exact original lifetime.
    expiry = result["delivery"]["download_expires_at"]
    stored = result["reports"].payloads.load(result["report_id"])
    stored["download_expires_at"] = expiry
    result["reports"].payloads.save(result["report_id"], stored)
    with origin.service.tasks._connect() as db:
        db.execute(
            "UPDATE task_artifacts SET expires_at=? WHERE artifact_id=?", (expiry, result["artifact_id"])
        )
    return result["artifact_id"], base64.b64decode(stored["content_base64"])


def host_gateway(origin, monkeypatch, tmp_path):
    # aiohttp is packaged in the separate NanoBot runtime. Exercise its handler directly.
    monkeypatch.setitem(
        sys.modules,
        "aiohttp",
        SimpleNamespace(web=SimpleNamespace(HTTPForbidden=PermissionError, HTTPBadRequest=ValueError)),
    )
    from nanobot_agentbridge import session_pool, telegram_identity

    request = {
        "instance": INSTANCE,
        "subject": origin.subject,
        "endpoint": origin.ep["endpointId"],
        "endpointKey": origin.ep["endpointKey"],
        "conversation": origin.ep["conversationRef"],
        "workspaceEndpointId": origin.ep["workspaceEndpointId"],
        "userDir": str(tmp_path),
    }
    host = SimpleNamespace(coordinator=True, worker_payload=lambda *_: request)

    class Session:
        async def call_tool(self, name, arguments, *, meta):
            handler = {
                "agentbridge_host_notification_claim": origin.service.claim_host_notifications,
                "agentbridge_host_notification_validate": origin.service.validate_host_notification,
                "agentbridge_host_notification_ack": origin.service.acknowledge_host_notification,
            }[name]
            arguments = dict(arguments)
            if name == "agentbridge_host_notification_claim" and arguments.pop("include_workspace_query_v2", False):
                # The actual MCP adapter derives this value from the authenticated session.
                arguments["query_token_id"] = origin.token_id
            result = handler(host_instance_id=INSTANCE, user_subject=origin.subject, **arguments)
            return SimpleNamespace(structuredContent=result, isError=False)

    @asynccontextmanager
    async def session(*_, **kwargs):
        yield Session(), {}, "L3"

    async def valid(*args, **kwargs):
        return True

    monkeypatch.setattr(session_pool, "open_host_session", session)
    monkeypatch.setattr(telegram_identity, "verify_telegram_binding", valid)

    class Gateway:
        def _request(self, action, payload, **kwargs):
            return asyncio.run(notifications(host, request, action, payload))

    return Gateway()


def test_source_task_multiple_files_central_to_telegram_receipts(origin, monkeypatch, tmp_path):
    artifact, pdf_bytes = pdf(origin)
    csv_id, csv_bytes = csv_report(origin)
    gateway = host_gateway(origin, monkeypatch, tmp_path)
    binding = {"user_subject": origin.subject}
    claimed = gateway._request("telegram-notifications", {"binding": binding})
    assert {event["artifactId"] for event in claimed["deliveries"]} == {artifact["artifact_id"], csv_id}
    assert all(
        set(event) == {"kind", "artifactId", "deliveryId", "attempt"} for event in claimed["deliveries"]
    )
    uploads = []

    def upload(chat, filename, mime, body):
        uploads.append((chat, filename, mime, body))
        return {
            "message_id": len(uploads),
            "from": {"id": 8001},
            "chat": {"type": "private", "id": chat},
            "document": {"file_id": str(uuid4()), "file_size": len(body)},
        }

    def local_request(action, arguments, **kwargs):
        if action == "telegram-notifications":
            return claimed
        return gateway._request(action, arguments, **kwargs)

    pilot = SimpleNamespace(
        gateway=SimpleNamespace(_request=local_request),
        bot_id=8001,
        db=sqlite3.connect(tmp_path / "bot.db"),
        _binding=lambda chat: (binding, "current"),
        api=SimpleNamespace(send_document=upload),
    )
    pilot.db.row_factory = sqlite3.Row
    terminal = TerminalMessages(pilot)
    terminal.poll(501, binding, "current")
    assert {(c, m, b) for c, _, m, b in uploads} == {
        (501, "application/pdf", pdf_bytes),
        (501, "text/csv", csv_bytes),
    }
    assert all(
        origin.service.tasks.get_delivery(e["deliveryId"])["state"] == "acknowledged"
        for e in claimed["deliveries"]
    )
    task = origin.service.tasks.get_task(origin.task, user_subject=origin.subject)
    assert {
        (f["artifactId"], f["state"])
        for f in task["summary"]["artifactDeliveryByChannel"]["telegram"]["files"]
    } == {(artifact["artifact_id"], "attachment_sent"), (csv_id, "attachment_sent")}
    # A repeat/restart must never touch the Telegram network a second time.
    monkeypatch.setattr(gateway, "_request", lambda *a, **k: {"recorded": True})
    TerminalMessages(pilot).poll(501, binding, "current")
    assert len(uploads) == 2
    pilot.db.close()


@pytest.mark.parametrize("boundary", ["grant", "expiry", "route", "wrong_attempt"])
def test_source_file_rechecked_before_send(origin, mirror, boundary):
    artifact, _ = pdf(origin)
    service = origin.service
    # 2f7bd8a7d64d's deployed consumer already includes ordinary Workspace chat.
    old = service.claim_host_notifications(**origin.scope, limit=32, lease_seconds=60, include_workspace_chat=True)
    assert not any(item["deliveryMode"] == "artifact" for item in old["notifications"])
    row = next(d for d in service.tasks.list_outbox(user_subject=origin.subject)
               if d["payload"].get("artifactPairProof"))
    assert (row["state"], row["attempt_count"]) == ("pending", 0)
    items = service.claim_host_notifications(**origin.scope, include_task_files=True)["notifications"]
    row = next(i for i in items if i["deliveryMode"] == "artifact")
    args = {**origin.scope, "delivery_id": row["deliveryId"], "attempt_count": row["attemptCount"]}
    assert (
        service.validate_host_notification(**args, include_artifact=True)["file"]["contentType"]
        == "application/pdf"
    )
    if boundary == "grant":
        grant_permissions(service.user_grants, origin.subject, ["oa.certificate.read"])
    elif boundary == "expiry":
        with service.tasks._connect() as db:
            db.execute(
                "UPDATE task_artifacts SET expires_at='2000-01-01T00:00:00+00:00' WHERE artifact_id=?",
                (artifact["artifact_id"],),
            )
    elif boundary == "route":
        mirror[-1]("8002")
    else:
        args["attempt_count"] += 1
    assert service.validate_host_notification(**args, include_artifact=True).get("allowed") is False


@pytest.mark.parametrize("failure", ["lost", "wrong_chat", "invalid_message", "crash_at_send"])
def test_ambiguous_upload_is_terminal_and_never_replayed(origin, monkeypatch, tmp_path, failure):
    artifact, _ = pdf(origin)
    gateway = host_gateway(origin, monkeypatch, tmp_path)
    (event,) = gateway._request("telegram-notifications", {})["deliveries"]
    from nanobot_agentbridge.telegram_files import deliver_file

    calls = []

    def upload(*args):
        calls.append(args)
        if failure == "lost":
            raise TelegramUnavailable()
        if failure == "crash_at_send":
            raise KeyboardInterrupt()
        return {
            "message_id": 1,
            "from": {"id": 8001},
            "document": {"file_id": "f"},
            "chat": {"id": 502, "type": "private"} if failure == "wrong_chat" else "not-a-receipt",
        }

    pilot = SimpleNamespace(
        db=sqlite3.connect(tmp_path / "bot.db"),
        bot_id=8001,
        api=SimpleNamespace(send_document=upload),
        gateway=gateway,
        _binding=lambda chat: ({}, "current"),
    )
    pilot.db.row_factory = sqlite3.Row
    TerminalMessages(pilot)
    if failure == "crash_at_send":
        with pytest.raises(KeyboardInterrupt):
            deliver_file(pilot, 501, {}, "current", event)
        TerminalMessages(pilot)
    deliver_file(pilot, 501, {}, "current", event)
    assert len(calls) == 1
    assert origin.service.tasks.get_delivery(event["deliveryId"])["state"] == "failed"
    task = origin.service.tasks.get_task(origin.task, user_subject=origin.subject)
    (status,) = task["summary"]["artifactDeliveryByChannel"]["telegram"]["files"]
    assert (
        status["artifactId"] == artifact["artifact_id"] and status["errorCode"] == "TELEGRAM_DELIVERY_UNKNOWN"
    )
    pilot.db.close()


def test_send_document_posts_multipart_bytes(monkeypatch):
    api = TelegramAPI("synthetic")

    def post(method, payload, headers, *, chat):
        assert chat == 501
        assert method == "sendDocument" and method not in api._SAFE_RECONNECT_METHODS
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + headers["Content-Type"] + "\r\n\r\n").encode() + payload
        )
        chat, document = message.iter_parts()
        assert chat.get_payload(decode=True) == b"501"
        assert document.get_filename() == "合成.pdf" and document.get_content_type() == "application/pdf"
        assert document.get_payload(decode=True) == b"%PDF-test"
        return {"ok": True}

    monkeypatch.setattr(api, "_post", post)
    assert api.send_document(501, "合成.pdf", "application/pdf", b"%PDF-test") == {"ok": True}
