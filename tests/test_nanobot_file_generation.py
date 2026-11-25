"""Exercise the four Telegram file tools against authenticated local MCP, task and artifact stores."""

import asyncio
import base64
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from tests.authorization_fixtures import grant_permissions
from tests.support.nanobot_telegram_setup import INSTANCE
from tests import test_central_mcp
from tests.test_document_downloads import _reference
from tests.test_nanobot_telegram_artifacts import origin  # noqa: F401
from tests.test_nanobot_telegram_pairing import pair_fixture  # noqa: F401
from tests.test_nanobot_telegram_rich_flow import _register_nanobot
from tests.test_nanobot_workspace_chat_mirror import mirror  # noqa: F401

from nanobot_agentbridge.file_generation import TaskFiles, SEARCH, SINGLE, BATCH, parameters


@pytest.fixture
def file_session(origin, pair_fixture, monkeypatch):
    from tests.support import skill_host_acceptance

    fixture, _, _ = pair_fixture
    service = origin.service
    register = _register_nanobot(service, origin.subject, fixture.tokens["a"], INSTANCE)
    lease = service.acquire_host_coordinator_lease(
        user_subject=origin.subject, task_id=origin.task, registration=register, lease_seconds=600
    )
    meta = {
        "io.agentbridge/host-context": {
            "version": "1",
            "agentHost": "nanobot",
            "hostInstanceId": INSTANCE,
            "hostVersion": "0.3.0",
        },
        "io.agentbridge/task": {
            "taskId": origin.task,
            "coordinatorLeaseVersion": str(lease["version"]),
            "endpointId": origin.ep["endpointId"],
            "conversationRef": origin.ep["conversationRef"],
            "hostRunId": "isolated-file-generation",
        },
    }
    grant_permissions(
        service.user_grants,
        origin.subject,
        [
            "oa.certificate.read",
            "oa.certificate.download",
            "oa.addressbook.read",
            "oa.addressbook.export",
            "smartlight.report.export",
            "smartlight.alarm.read",
        ],
    )
    reads = {"oa.document.certificate.search", "oa.addressbook.export", "smartlight.report.export"}
    monkeypatch.setattr(skill_host_acceptance, "READS", reads)
    session = {"user_subject": origin.subject, "system_id": "oa", "session_id": "isolated-read"}

    def synthetic_read(*, capability_name, arguments, task_id, **kwargs):
        assert task_id == origin.task
        if capability_name == "oa.document.certificate.search":
            return service._materialize_document_downloads(
                session=session,
                result={
                    "count": 2,
                    "ambiguous_queries": [],
                    "items": [
                        {
                            "filename": f"测试{i}.pdf",
                            "title": f"测试{i}",
                            "query": "测试",
                            "document_type": "patent_certificate",
                            "_download_reference": {**_reference(), "source_id": f"file-{i}"},
                        }
                        for i in (1, 2)
                    ],
                },
            )
        assert capability_name in reads
        report = {
            "reportType": arguments.get("source") or arguments["report_type"],
            "reportTitle": "隔离导出",
            "columns": [{"key": "private_value", "label": "合成列"}],
            "rows": [{"private_value": "文件专有合成数据"}],
        }
        materialize = (
            service._materialize_addressbook_report
            if capability_name.startswith("oa.")
            else service._materialize_smartlight_report
        )
        return materialize(
            session={**session, "system_id": capability_name.split(".")[0]},
            arguments=arguments,
            report=report,
            task_id=task_id,
        )

    monkeypatch.setattr(service, "_runtime_for_system", lambda _: (object(), object()))
    monkeypatch.setattr(service, "_invoke_adapter", synthetic_read)
    pdf = {"body": b"%PDF-private-test-file", "content_type": "application/pdf"}
    monkeypatch.setattr(service, "fetch_document_download", lambda _: pdf)
    monkeypatch.setattr(service, "fetch_document_downloads", lambda records: [pdf for _ in records])
    with TestClient(fixture.app, base_url="http://127.0.0.1:7191") as client:
        token = (fixture.home / "a.token").read_text()

        class Session:
            async def call_tool(self, name, arguments, meta):
                res = test_central_mcp.CentralMcpTests._request(
                    client,
                    "tools/call",
                    request_id=str(uuid4()),
                    token=token,
                    params={"name": name, "arguments": arguments, "_meta": meta},
                )
                assert res.status_code == 200
                data = res.json()["result"]
                return SimpleNamespace(
                    structuredContent=data.get("structuredContent"),
                    isError=data.get("isError", False),
                    meta=data.get("_meta"),
                )

        async def context():
            return meta

        yield (
            Session(),
            context,
            SimpleNamespace(endpoint=origin.ep["endpointId"], conversation=origin.ep["conversationRef"]),
        )


def model_call(tools, context, name, arguments, *, skill=None):
    session, business_context, binding = context
    return asyncio.run(
        tools.execute(
            name, arguments, session=session, business_context=business_context, skill=skill, binding=binding
        )
    )


@pytest.mark.parametrize(
    "tool,arguments,expected",
    [
        (SINGLE, {}, 1),
        (BATCH, {}, 2),
        ("oa_addressbook_export", {"source": "person_search", "query": "设备"}, 1),
        ("smartlight_report_export", {"report_type": "alarm_analysis"}, 1),
    ],
)
def test_four_private_entries_link_original_task_and_deliver_only_prepared_bytes(
    origin, file_session, tool, arguments, expected
):
    tools = TaskFiles()
    if tool in {SINGLE, BATCH}:
        private, public = model_call(tools, file_session, SEARCH, {"name": "测试"})
        assert public["status"] == "succeeded" and len(public["items"]) == 2
        presentation = json.dumps(public, ensure_ascii=False)
        assert "/download/" not in presentation
        for file in private.structuredContent["result"]["items"]:
            assert file["download_id"] not in presentation
        choices = [item["selection"] for item in public["items"]]
        arguments = {"selection": choices[0]} if tool == SINGLE else {"selections": choices}
    _, result = model_call(tools, file_session, tool, arguments)
    assert result["status"] == "prepared" and result["preparedCount"] == expected
    assert set(result) == {"status", "preparedCount", "requestedCount", "meaning"}
    assert model_call(tools, file_session, tool, arguments)[1] == result
    assert (
        len(origin.service.tasks.list_artifacts(task_id=origin.task, user_subject=origin.subject)) == expected
    )
    events = [
        row
        for row in origin.service.claim_host_notifications(**origin.scope, include_task_files=True)["notifications"]
        if row["deliveryMode"] == "artifact"
    ]
    assert len(events) == expected
    for row in events:
        auth = origin.service.validate_host_notification(
            **origin.scope,
            delivery_id=row["deliveryId"],
            attempt_count=row["attemptCount"],
            include_artifact=True,
        )
        assert auth["allowed"]
        data = base64.b64decode(auth["file"]["contentBase64"])
        if tool in {SINGLE, BATCH}:
            assert data == b"%PDF-private-test-file"
        else:
            assert "文件专有合成数据" in data.decode("utf-8-sig")


def test_certificate_selection_requires_original_search_current_pair_grants_and_lease(origin, file_session):
    tools = TaskFiles()
    central, public = model_call(tools, file_session, SEARCH, {"name": "测试"})
    raw = central.structuredContent["result"]["items"][0]["download_id"]
    ref = public["items"][0]["selection"]
    with pytest.raises(KeyError):
        model_call(TaskFiles(), file_session, SINGLE, {"selection": ref})
    with pytest.raises(ValueError):
        model_call(tools, file_session, SINGLE, {"download_id": raw})
    session, context, _ = file_session
    current = asyncio.run(context())
    foreign = origin.service.document_downloads.create(
        user_subject=origin.subject,
        system_id="oa",
        session_id="other-session",
        document=_reference(),
        filename="不可混入.pdf",
        document_type="patent_certificate",
        display_size="",
        card_base_url="https://fixture.invalid",
    )
    forged = asyncio.run(session.call_tool(BATCH, {"download_ids": [raw, foreign["download_id"]]}, current))
    assert forged.isError
    older = {
        **current,
        "io.agentbridge/task": {**current["io.agentbridge/task"], "coordinatorLeaseVersion": "99"},
    }
    assert asyncio.run(session.call_tool(SINGLE, {"download_id": raw}, older)).isError
    grant_permissions(origin.service.user_grants, origin.subject, ["oa.certificate.read"])
    assert model_call(tools, file_session, SINGLE, {"selection": ref})[1] == {"status": "unconfirmed"}
    assert origin.service.tasks.list_artifacts(task_id=origin.task, user_subject=origin.subject) == []


def test_export_permissions_by_scope_and_file_fields_never_reach_model(origin, file_session):
    tools = TaskFiles()
    assert model_call(tools, file_session, "oa_addressbook_export", {"source": "private_contacts"})[1] == {
        "status": "unconfirmed"
    }
    assert model_call(
        tools,
        file_session,
        "smartlight_report_export",
        {"report_type": "asset_inventory", "asset_type": "rtu"},
    )[1] == {"status": "unconfirmed"}
    assert origin.service.tasks.list_artifacts(task_id=origin.task, user_subject=origin.subject) == []
    with pytest.raises(ValueError):
        model_call(
            tools,
            file_session,
            "oa_addressbook_export",
            {"source": "person_search", "idempotency_key": "injected"},
        )
    assert set(parameters(BATCH, {})["properties"]) == {"selections"}
    assert (
        "idempotency_key"
        not in parameters("smartlight_report_export", {"properties": {"task_id": {}, "idempotency_key": {}}})[
            "properties"
        ]
    )
