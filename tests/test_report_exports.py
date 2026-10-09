from __future__ import annotations

from tests.authorization_fixtures import authorized_service, grant_permissions
from agentbridge.core.user_grants import UserGrants
import csv
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.auth.document_download import TrustedDocumentDownloadApplication
from agentbridge.core.document_downloads import DocumentDownloadStore
from agentbridge.adapters.seeyon_addressbook import ADDRESSBOOK_EXPORT_CAPABILITY
from agentbridge.adapters.smartlight import SMARTLIGHT_REPORT_EXPORT_CAPABILITY
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.report_exports import (
    ADDRESSBOOK_REPORT_CONTENT_TYPE,
    ADDRESSBOOK_REPORT_DOCUMENT_TYPE,
    SMARTLIGHT_REPORT_CONTENT_TYPE,
    SMARTLIGHT_REPORT_DOCUMENT_TYPE,
    addressbook_report_recipe,
    render_addressbook_report_csv,
    render_smartlight_report_csv,
    smartlight_report_filename,
    smartlight_report_recipe,
)


class SmartlightReportExportTests(unittest.TestCase):
    def test_addressbook_csv_uses_the_same_safe_utf8_contract(self):
        body = render_addressbook_report_csv(
            {
                "columns": [
                    {"key": "name", "label": "姓名"},
                    {"key": "phone", "label": "手机号码"},
                ],
                "rows": [
                    {"name": "张三", "phone": "******"},
                    {"name": "=公式", "phone": "+123"},
                ],
            }
        )

        rows = list(csv.reader(StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["姓名", "手机号码"])
        self.assertEqual(rows[1], ["张三", "******"])
        self.assertEqual(rows[2], ["'=公式", "'+123"])

    def test_csv_uses_utf8_bom_and_blocks_spreadsheet_formulas(self):
        body = render_smartlight_report_csv(
            {
                "columns": [
                    {"key": "name", "label": "名称"},
                    {"key": "value", "label": "内容"},
                ],
                "rows": [
                    {"name": "测试", "value": "=HYPERLINK(\"https://bad\")"},
                    {"name": "普通", "value": {"状态": "正常"}},
                ],
            }
        )

        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["名称", "内容"])
        self.assertEqual(rows[1][1], "'=HYPERLINK(\"https://bad\")")
        self.assertEqual(rows[2][1], '{"状态":"正常"}')

    def test_filename_is_safe_and_uses_business_time(self):
        filename = smartlight_report_filename(
            {"filenameStem": "照明/告警:分析"},
            now=datetime(2026, 8, 12, 4, 5, 6, tzinfo=timezone.utc),
        )

        self.assertEqual(filename, "照明_告警_分析_20260812_120506.csv")

    def test_document_store_accepts_ready_csv_report(self):
        with TemporaryDirectory() as tmp:
            store = DocumentDownloadStore(Path(tmp) / "agentbridge.db")
            grant_permissions(UserGrants(store.db_path), "user-a", ['smartlight.report.export', 'smartlight.alarm.read'])
            record = store.create(
                user_subject="user-a",
                system_id="smartlight",
                session_id="session-a",
                document=smartlight_report_recipe(
                    report_type="alarm_analysis",
                    arguments={"report_type": "alarm_analysis", "last_days": 30},
                ),
                filename="照明告警.csv",
                document_type=SMARTLIGHT_REPORT_DOCUMENT_TYPE,
                display_size="1 KB",
                card_base_url="https://10.10.50.213:8780",
            )
            store.claim_for_prepare(record["download_id"], user_subject="user-a")
            ready = store.mark_ready(
                record["download_id"],
                body=b"\xef\xbb\xbftest\r\n",
                content_type=SMARTLIGHT_REPORT_CONTENT_TYPE,
            )

            self.assertEqual(ready["state"], "ready")
            self.assertEqual(ready["content_type"], "text/csv")
            self.assertEqual(
                store.ready_payload(
                    record["download_id"],
                    user_subject="user-a",
                )["body"],
                b"\xef\xbb\xbftest\r\n",
            )

            app = TrustedDocumentDownloadApplication(
                download_store=store,
                fetcher=lambda _record: {},
            )
            response = app.get_file(record["download_id"])
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], "text/csv")
            self.assertIn("filename=agentbridge-report.csv", response.headers["Content-Disposition"])
            self.assertIn("%E7%85%A7%E6%98%8E%E5%91%8A%E8%AD%A6.csv", response.headers["Content-Disposition"])
            self.assertTrue(response.body.startswith(b"\xef\xbb\xbf"))

    def test_central_service_links_report_and_reissues_expired_artifact(self):
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://oa.example.test/seeyon/",
                smartlight_base_url="http://smartlight.example.test/smartlight/",
                smartlight_allow_insecure_http=True,
                trusted_card_base_url="https://10.10.50.213:8780",
            )
            adapter = FakeReportAdapter()
            service._adapters_by_system["smartlight"] = adapter
            service._worker_factories_by_system["smartlight"] = (
                lambda _session, _adapter: FakeReportWorker()
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="smartlight",
                expected_principal_ref="Tester",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Tester",
            )
            service.session_states.save(
                session["session_id"],
                {"cookies": [], "http": {}},
            )
            endpoint, _ = service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id="token-a",
                agent_host="openclaw",
                endpoint_key="web:user-a",
                client_type="web",
                external_subject="user-a",
                conversation_ref="agent:main:web:user-a",
            )
            task, _ = service.tasks.ensure_task(
                user_subject="user-a",
                agent_host="openclaw",
                host_task_key="run|smartlight-report",
                origin_endpoint_id=endpoint["endpoint_id"],
                active_conversation_ref=endpoint["conversation_ref"],
                title="导出照明系统 CSV 报告",
            )

            operation = service.invoke(
                user_subject="user-a",
                capability_name=SMARTLIGHT_REPORT_EXPORT_CAPABILITY,
                arguments={"report_type": "alarm_analysis", "last_days": 30},
                task_id=task["task_id"],
            )

            self.assertEqual(operation["status"], "succeeded", operation)
            delivery = operation["result"]
            self.assertEqual(delivery["file"]["contentType"], "text/csv")
            expires_at = datetime.fromisoformat(delivery["file"]["expiresAt"])
            remaining_seconds = (expires_at - datetime.now(timezone.utc)).total_seconds()
            self.assertGreater(remaining_seconds, 1_740)
            self.assertLessEqual(remaining_seconds, 1_800)
            artifact_id = delivery["file"]["artifactId"]
            payload = service.document_downloads.ready_payload(
                delivery["file"]["downloadId"],
                user_subject="user-a",
            )
            self.assertIn("告警内容", payload["body"].decode("utf-8-sig"))
            with service.tasks._connect() as connection:
                connection.execute(
                    "UPDATE task_artifacts SET state = 'expired' WHERE artifact_id = ?",
                    (artifact_id,),
                )

            reissued = service.reissue_document_download(
                user_subject="user-a",
                task_id=task["task_id"],
                artifact_id=artifact_id,
            )

            self.assertEqual(reissued["status"], "succeeded", reissued)
            self.assertEqual(reissued["file"]["artifactId"], artifact_id)
            self.assertNotEqual(
                reissued["file"]["downloadId"],
                delivery["file"]["downloadId"],
            )
            self.assertEqual(adapter.calls, 2)
            denied = service.reissue_document_download(
                user_subject="user-b",
                task_id=task["task_id"],
                artifact_id=artifact_id,
            )
            self.assertEqual(denied["error"]["code"], "DOWNLOAD_NOT_FOUND")

    def test_central_service_materializes_and_reissues_addressbook_export(self):
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=tmp,
                base_url="http://oa.example.test/seeyon/",
                trusted_card_base_url="https://10.10.50.213:8780",
            )
            adapter = FakeAddressbookReportAdapter()
            service._adapters_by_system["oa"] = adapter
            service._worker_factories_by_system["oa"] = (
                lambda _session, _adapter: FakeReportWorker()
            )
            session = service.sessions.get_or_create(
                user_subject="user-a",
                system_id="oa",
                expected_principal_ref="Tester",
            )
            session = service.sessions.activate(
                session["session_id"],
                observed_principal_ref="Tester",
            )
            service.session_states.save(session["session_id"], {"cookies": [], "http": {}})
            endpoint, _ = service.tasks.ensure_endpoint(
                user_subject="user-a",
                token_id="token-a",
                agent_host="openclaw",
                endpoint_key="web:user-a",
                client_type="web",
                external_subject="user-a",
                conversation_ref="agent:main:web:user-a",
            )
            task, _ = service.tasks.ensure_task(
                user_subject="user-a",
                agent_host="openclaw",
                host_task_key="run|addressbook-report",
                origin_endpoint_id=endpoint["endpoint_id"],
                active_conversation_ref=endpoint["conversation_ref"],
                title="导出 OA 通讯录",
            )

            operation = service.invoke(
                user_subject="user-a",
                capability_name=ADDRESSBOOK_EXPORT_CAPABILITY,
                arguments={"source": "person_search", "query": "张三"},
                task_id=task["task_id"],
            )

            self.assertEqual(operation["status"], "succeeded", operation)
            delivery = operation["result"]
            self.assertEqual(delivery["file"]["contentType"], ADDRESSBOOK_REPORT_CONTENT_TYPE)
            payload = service.document_downloads.ready_payload(
                delivery["file"]["downloadId"], user_subject="user-a"
            )
            self.assertIn("手机号码", payload["body"].decode("utf-8-sig"))
            artifact_id = delivery["file"]["artifactId"]
            with service.tasks._connect() as connection:
                connection.execute(
                    "UPDATE task_artifacts SET state = 'expired' WHERE artifact_id = ?",
                    (artifact_id,),
                )

            reissued = service.reissue_document_download(
                user_subject="user-a",
                task_id=task["task_id"],
                artifact_id=artifact_id,
            )

            self.assertEqual(reissued["status"], "succeeded", reissued)
            self.assertEqual(reissued["file"]["artifactId"], artifact_id)
            self.assertEqual(adapter.calls, 2)

    def test_addressbook_report_recipe_is_stored_as_read_only_regeneration_input(self):
        recipe = addressbook_report_recipe(
            arguments={"source": "department_members", "department_id": "42"}
        )
        self.assertEqual(recipe["kind"], "oa_addressbook_report.v1")
        self.assertEqual(recipe["arguments"]["department_id"], "42")
        self.assertEqual(ADDRESSBOOK_REPORT_DOCUMENT_TYPE, "oa_addressbook_csv_report")


class FakeReportWorker:
    def __init__(self) -> None:
        self.state = {}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def restore_session_state(self, state: dict) -> None:
        self.state = dict(state)

    def capture_session_state(self) -> dict:
        return dict(self.state)


class FakeReportAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_capability(self, capability_name: str, _worker, arguments: dict) -> dict:
        if capability_name != SMARTLIGHT_REPORT_EXPORT_CAPABILITY:
            raise KeyError(capability_name)
        self.calls += 1
        return {
            "reportType": arguments["report_type"],
            "reportTitle": "照明RTU告警分析",
            "filenameStem": "照明RTU告警分析",
            "columns": [
                {"key": "time", "label": "时间"},
                {"key": "message", "label": "告警内容"},
            ],
            "rows": [
                {"time": "2026-08-12 12:00:00", "message": "测试告警"},
            ],
            "metadata": {
                "downstreamTotal": 1,
                "exportedCount": 1,
                "truncated": False,
            },
        }


class FakeAddressbookReportAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def invoke_capability(self, capability_name: str, _worker, arguments: dict) -> dict:
        if capability_name != ADDRESSBOOK_EXPORT_CAPABILITY:
            raise KeyError(capability_name)
        self.calls += 1
        return {
            "reportType": arguments["source"],
            "reportTitle": "OA组织通讯录查询结果",
            "filenameStem": "OA组织通讯录查询结果",
            "columns": [
                {"key": "name", "label": "姓名"},
                {"key": "phone", "label": "手机号码"},
            ],
            "rows": [{"name": "张三", "phone": "******"}],
            "metadata": {"rowCount": 1, "maskedValuesPreserved": True},
        }


if __name__ == "__main__":
    unittest.main()
