import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

from agentbridge.adapters.seeyon_central import (
    SeeyonCentralAdapter,
    SeeyonReadContractMismatch,
    build_central_capability_registry,
)


BASE_URL = "http://oa.example.test/seeyon/main.do?method=main"


class SeeyonCentralWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SeeyonCentralAdapter(base_url=BASE_URL)
        self.worker = FakeWorkflowWorker()

    def test_registry_exposes_reads_and_governed_write_workflows(self):
        registry = build_central_capability_registry()

        names = [spec.name for spec in registry.list(system="oa")]

        self.assertEqual(
            names,
            [
                "oa.addressbook.department.members",
                "oa.addressbook.export",
                "oa.addressbook.group.list",
                "oa.addressbook.group.members",
                "oa.addressbook.organization.tree",
                "oa.addressbook.person.get",
                "oa.addressbook.person.search",
                "oa.addressbook.private_contact.get",
                "oa.addressbook.private_contact.search",
                "oa.attendance_confirmation.confirm",
                "oa.attendance_confirmation.prepare",
                "oa.business_trip.approval.prepare",
                "oa.business_trip.approve",
                "oa.business_trip.prepare",
                "oa.business_trip.save_draft",
                "oa.business_trip.submit",
                "oa.business_trip.submit.prepare",
                "oa.document.certificate.search",
                "oa.efficiency_data.approval.prepare",
                "oa.efficiency_data.approve",
                "oa.flight_application.approval.prepare",
                "oa.flight_application.approve",
                "oa.intellectual_property_declaration.approval.prepare",
                "oa.intellectual_property_declaration.approve",
                "oa.labor_contract_renewal.approval.prepare",
                "oa.labor_contract_renewal.approve",
                "oa.leave.approval.prepare",
                "oa.leave.approve",
                "oa.leave.prepare",
                "oa.leave.save_draft",
                "oa.leave.submit",
                "oa.leave.submit.prepare",
                "oa.meeting.create",
                "oa.meeting.create.prepare",
                "oa.meeting_room.application.cancel",
                "oa.meeting_room.application.cancel.prepare",
                "oa.meeting_room.application.create",
                "oa.meeting_room.application.prepare",
                "oa.meeting_room.availability.list",
                "oa.meeting_room.my_applications.list",
                "oa.missed_punch.approval.batch.prepare",
                "oa.missed_punch.approval.prepare",
                "oa.missed_punch.approve",
                "oa.missed_punch.prepare",
                "oa.missed_punch.save_draft",
                "oa.overtime.approval.prepare",
                "oa.overtime.approve",
                "oa.resignation.approval.prepare",
                "oa.resignation.approve",
                "oa.standard_collaboration.approval.prepare",
                "oa.standard_collaboration.approve",
                "oa.template.list",
                "oa.travel_expense.approval.prepare",
                "oa.travel_expense.approve",
                "oa.weekly_report.acknowledge",
                "oa.weekly_report.acknowledgement.prepare",
                "oa.work_handover.approval.prepare",
                "oa.work_handover.approve",
                "oa.workflow.detail.get",
                "oa.workflow.done.list",
                "oa.workflow.opinions.list",
                "oa.workflow.pending.batch.prepare",
                "oa.workflow.pending.list",
                "oa.workflow.revoke",
                "oa.workflow.revoke.prepare",
                "oa.workflow.sent.list",
                "oa.workflow.tracked.list",
            ],
        )
        effects = {spec.name: spec.effect for spec in registry.list(system="oa")}
        self.assertEqual(effects["oa.document.certificate.search"], "read")
        self.assertEqual(effects["oa.meeting_room.availability.list"], "read")
        self.assertEqual(effects["oa.meeting_room.my_applications.list"], "read")
        self.assertEqual(effects["oa.business_trip.prepare"], "reversible_write")
        self.assertEqual(effects["oa.business_trip.save_draft"], "reversible_write")
        self.assertEqual(effects["oa.business_trip.submit.prepare"], "controlled_write")
        self.assertEqual(effects["oa.business_trip.submit"], "controlled_write")
        self.assertEqual(
            effects["oa.business_trip.approval.prepare"], "controlled_write"
        )
        self.assertEqual(effects["oa.business_trip.approve"], "controlled_write")
        self.assertEqual(effects["oa.attendance_confirmation.prepare"], "controlled_write")
        self.assertEqual(effects["oa.attendance_confirmation.confirm"], "controlled_write")
        self.assertEqual(effects["oa.leave.prepare"], "reversible_write")
        self.assertEqual(effects["oa.leave.save_draft"], "reversible_write")
        self.assertEqual(effects["oa.leave.submit.prepare"], "controlled_write")
        self.assertEqual(effects["oa.leave.submit"], "controlled_write")
        self.assertEqual(effects["oa.leave.approval.prepare"], "controlled_write")
        self.assertEqual(effects["oa.leave.approve"], "controlled_write")
        self.assertEqual(
            effects["oa.missed_punch.approval.batch.prepare"],
            "controlled_write",
        )
        self.assertEqual(effects["oa.labor_contract_renewal.approval.prepare"], "controlled_write")
        self.assertEqual(effects["oa.labor_contract_renewal.approve"], "controlled_write")
        self.assertEqual(
            effects["oa.intellectual_property_declaration.approval.prepare"],
            "controlled_write",
        )
        self.assertEqual(
            effects["oa.intellectual_property_declaration.approve"],
            "controlled_write",
        )
        self.assertEqual(effects["oa.workflow.revoke.prepare"], "controlled_write")
        self.assertEqual(effects["oa.workflow.revoke"], "controlled_write")
        prepare = registry.get("oa.business_trip.prepare")
        self.assertEqual(prepare.version, "0.3.0")
        self.assertEqual(
            set(prepare.input_schema["properties"]),
            {
                "start_time",
                "end_time",
                "travel_mode",
                "origin",
                "destination",
                "reason",
                "has_direct_supervisor",
                "input_submission_id",
            },
        )
        self.assertEqual(effects["oa.missed_punch.prepare"], "reversible_write")
        self.assertEqual(effects["oa.missed_punch.save_draft"], "reversible_write")
        self.assertEqual(effects["oa.missed_punch.approval.prepare"], "controlled_write")
        self.assertEqual(effects["oa.missed_punch.approve"], "controlled_write")
        self.assertEqual(effects["oa.overtime.approval.prepare"], "controlled_write")
        self.assertEqual(effects["oa.overtime.approve"], "controlled_write")
        self.assertEqual(effects["oa.resignation.approval.prepare"], "controlled_write")
        self.assertEqual(effects["oa.resignation.approve"], "controlled_write")
        self.assertEqual(effects["oa.meeting.create.prepare"], "controlled_write")
        self.assertEqual(effects["oa.meeting.create"], "controlled_write")
        for capability_name in (
            "oa.meeting_room.application.prepare",
            "oa.meeting_room.application.create",
            "oa.meeting_room.application.cancel.prepare",
            "oa.meeting_room.application.cancel",
        ):
            self.assertEqual(effects[capability_name], "controlled_write")
        self.assertTrue(
            all(
                effects[name] == "read"
                for name in {
                    "oa.template.list",
                    "oa.workflow.detail.get",
                    "oa.workflow.done.list",
                    "oa.workflow.opinions.list",
                    "oa.workflow.pending.list",
                    "oa.workflow.sent.list",
                    "oa.workflow.tracked.list",
                }
            )
        )

    def test_pending_list_filters_and_removes_internal_transport_fields(self):
        result = self.adapter.invoke_capability(
            "oa.workflow.pending.list",
            self.worker,
            {"keyword": "Quarterly", "limit": 1},
        )

        self.assertEqual(result["collection"], "pending")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["affair_id"], "pending-1")
        self.assertEqual(result["transport"], "central_http_session")
        serialized = json.dumps(result)
        self.assertNotIn("href", serialized)
        self.assertNotIn("raw_text", serialized)
        self.assertNotIn("owner-secret", serialized)

    def test_done_list_uses_the_distinct_done_page_grid(self):
        result = self.adapter.invoke_capability("oa.workflow.done.list", self.worker, {})

        self.assertEqual(result["source"], "history_page_grid")
        self.assertEqual(result["items"][0]["affair_id"], "done-1")
        self.assertEqual(result["items"][0]["status"], "Completed")
        self.assertIn("method=listDone", self.worker.goto_calls[-1])
        self.assertEqual(
            self.worker.page.wait_calls[-1]["managerMethod"],
            "getDoneList",
        )

    def test_done_list_filters_dates_before_limit(self):
        result = self.adapter.invoke_capability(
            "oa.workflow.done.list",
            self.worker,
            {
                "start_date": "2026-07-03",
                "end_date": "2026-07-10",
                "limit": 1,
            },
        )

        self.assertEqual(result["schema_version"], "agentbridge.oa_workflow_list.v2")
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["date_filtered_count"], 2)
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["affair_id"], "done-1")
        self.assertEqual(result["filters"]["start_date"], "2026-07-03")

    def test_done_list_rejects_reversed_date_range(self):
        with self.assertRaisesRegex(ValueError, "start_date cannot be after end_date"):
            self.adapter.invoke_capability(
                "oa.workflow.done.list",
                self.worker,
                {"start_date": "2026-07-11", "end_date": "2026-07-10"},
            )

    def test_sent_list_uses_the_distinct_sent_page_grid(self):
        result = self.adapter.invoke_capability("oa.workflow.sent.list", self.worker, {})

        self.assertEqual(result["collection"], "sent")
        self.assertEqual(result["source"], "history_page_grid")
        self.assertEqual(result["transport"], "central_browser_session")
        self.assertEqual(result["items"][0]["affair_id"], "sent-1")
        self.assertIn("method=listSent", self.worker.goto_calls[-1])
        self.assertEqual(
            self.worker.page.wait_calls[-1]["managerMethod"],
            "getSentList",
        )

    def test_sent_date_range_proves_coverage_from_server_filtered_total(self):
        result = self.adapter.invoke_capability(
            "oa.workflow.sent.list",
            self.worker,
            {"start_date": "2026-07-01", "end_date": "2026-07-08"},
        )

        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(
            result["coverage"]["completionReason"],
            "server_filtered_source_exhausted",
        )
        self.assertEqual(result["coverage"]["dateBasis"], "initiated_at")

    def test_date_filter_with_fallback_display_date_remains_incomplete(self):
        parsed = {
            "source": "history_page_grid",
            "total": 1,
            "page": 1,
            "items": [
                {
                    "affair_id": "done-fallback-date",
                    "title": "Fallback date row",
                    "date": "2026-07-10 10:30",
                    "date_basis": "fallback_display_date",
                }
            ],
        }
        with patch.object(self.adapter, "_fetch_workflow_collection", return_value=parsed):
            result = self.adapter.invoke_capability(
                "oa.workflow.done.list",
                self.worker,
                {"start_date": "2026-07-10", "end_date": "2026-07-10"},
            )

        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(
            result["coverage"]["completionReason"], "date_basis_not_proven"
        )

    def test_unknown_total_before_date_boundary_reports_possible_more_rows(self):
        parsed = {
            "source": "history_page_grid",
            "total": None,
            "page": 1,
            "items": [
                {
                    "affair_id": "done-newer",
                    "title": "Newer completed row",
                    "date": "2026-07-12 10:30",
                    "date_basis": "processed_at",
                }
            ],
        }
        with patch.object(self.adapter, "_fetch_workflow_collection", return_value=parsed):
            result = self.adapter.invoke_capability(
                "oa.workflow.done.list",
                self.worker,
                {"start_date": "2026-07-10", "end_date": "2026-07-10"},
            )

        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertTrue(result["coverage"]["hasMore"])
        self.assertEqual(
            result["coverage"]["completionReason"],
            "loaded_page_does_not_cover_requested_range",
        )

    def test_tracked_list_uses_the_independent_more_track_grid(self):
        result = self.adapter.invoke_capability("oa.workflow.tracked.list", self.worker, {})

        self.assertEqual(result["collection"], "tracked")
        self.assertEqual(result["source"], "tracked_page_grid")
        self.assertEqual(result["total"], 28)
        self.assertEqual(
            [item["affair_id"] for item in result["items"]],
            ["tracked-sent", "tracked-done"],
        )
        self.assertIn("method=main", self.worker.goto_calls[-1])
        self.assertEqual(
            self.worker.page.locator_clicks,
            ["tracking-tab", "more-track"],
        )
        self.assertEqual(
            self.worker.page.frames[0].wait_calls[-1]["gridId"],
            "gridId",
        )

    def test_tracked_detail_preserves_the_row_source_page(self):
        self.adapter.invoke_capability(
            "oa.workflow.detail.get",
            self.worker,
            {
                "collection": "tracked",
                "affair_id": "tracked-done",
            },
        )

        self.assertIn("openFrom=listDone", self.worker.render_calls[-1])

    def test_tracked_identifier_fallback_reconciles_sent_and_done_rows(self):
        self.worker.tracked_id_shell = FakeTrackedIdPage(
            ["sent-fallback", "done-fallback"]
        )
        sent = {
            "items": [
                {
                    "affair_id": "sent-fallback",
                    "title": "Tracked sent fallback",
                    "status": "In progress",
                    "date": "2026-07-10",
                    "category": "",
                    "raw_text": "",
                    "href": "internal",
                }
            ]
        }
        done = {
            "items": [
                {
                    "affair_id": "done-fallback",
                    "title": "Tracked done fallback",
                    "status": "Completed",
                    "date": "2026-07-09",
                    "category": "",
                    "raw_text": "",
                    "href": "internal",
                }
            ]
        }
        with patch.object(
            self.adapter,
            "_read_history_page",
            side_effect=[sent, done],
        ):
            result = self.adapter._read_tracked_page_fallback(self.worker)

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            [item["affair_id"] for item in result["items"]],
            ["sent-fallback", "done-fallback"],
        )
        self.assertEqual(
            [item["open_from"] for item in result["items"]],
            ["listSent", "listDone"],
        )

    def test_detail_merges_same_origin_frame_and_exposes_business_data_only(self):
        result = self.adapter.invoke_capability(
            "oa.workflow.detail.get",
            self.worker,
            {"collection": "done", "affair_id": "done-1", "text_limit": 1000},
        )

        detail = result["detail"]
        self.assertEqual(detail["title"], "Completed request")
        self.assertEqual(detail["fields"], [{"name": "Applicant", "value": "Alice"}])
        self.assertEqual(detail["attachments"], [{"name": "brief.pdf"}])
        self.assertEqual(detail["opinion_count"], 1)
        self.assertIn("Rendered business body", detail["text"])
        serialized = json.dumps(result)
        for forbidden in ("href", "write_hints", "actions", "detail-internal-token"):
            self.assertNotIn(forbidden, serialized)

    def test_authoritative_sent_row_opens_detail_without_home_projection(self):
        row = {
            "affair_id": "sent-new",
            "template_id": "template-1",
            "form_app_id": "form-1",
            "title": "Expanded sent subject",
        }
        with patch(
            "agentbridge.adapters.seeyon_central._load_collection_rows",
            return_value=([row], self.worker.page),
        ):
            rows, page = self.adapter.load_sent_workflow_rows(self.worker)

        self.assertEqual(rows, [row])
        self.assertIs(page, self.worker.page)
        source_item, detail = self.adapter.resolve_sent_workflow_row_detail(
            self.worker,
            source_item=row,
        )
        query = parse_qs(urlparse(self.worker.render_calls[-1]).query)
        self.assertEqual(query["method"], ["summary"])
        self.assertEqual(query["openFrom"], ["listSent"])
        self.assertEqual(query["affairId"], ["sent-new"])
        self.assertEqual(source_item["title"], row["title"])
        self.assertEqual(detail["fields"], [{"name": "Applicant", "value": "Alice"}])

    def test_opinions_returns_bounded_sanitized_items(self):
        result = self.adapter.invoke_capability(
            "oa.workflow.opinions.list",
            self.worker,
            {"collection": "done", "affair_id": "done-1", "limit": 1},
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"], [{"text": "Opinion approved by Alice 2026-07-10 10:30"}])
        self.assertEqual(result["transport"], "central_browser_session")

    def test_invalid_collection_is_rejected_before_browser_navigation(self):
        with self.assertRaisesRegex(ValueError, "collection must be one of"):
            self.adapter.invoke_capability(
                "oa.workflow.detail.get",
                self.worker,
                {"collection": "unknown", "affair_id": "done-1"},
            )

        self.assertEqual(self.worker.goto_calls, [])

    def test_empty_section_shell_is_not_misreported_as_an_empty_collection(self):
        self.worker.section_payload_override = {}

        with self.assertRaisesRegex(SeeyonReadContractMismatch, "missing Data"):
            self.adapter.invoke_capability("oa.workflow.pending.list", self.worker, {})


class FakeWorkflowWorker:
    def __init__(self):
        self.page = FakeWorkflowPage()
        self.goto_calls = []
        self.render_calls = []
        self.last_section_arguments = {}
        self.section_payload_override = None
        self.tracked_id_shell = None
        self._resource_urls = [
            _section_url("pendingSection", entity_id="pending-entity", panel_id="pending-panel"),
            _section_url("sentSection", entity_id="sent-entity", panel_id="sent-panel"),
        ]

    @property
    def page_url(self):
        return self.page.url

    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        if "method=moreTrack" in url and self.tracked_id_shell is not None:
            self.page = self.tracked_id_shell
            return self.page
        self.page.url = url
        return self.page

    def resource_urls(self):
        return list(self._resource_urls)

    def request(self, _method, url, **_kwargs):
        if "/rest/template/myTemplate" in url:
            return {
                "status": 200,
                "url": url,
                "content_type": "application/json",
                "json": {"code": 0, "data": {"templates": []}},
                "text": "",
            }
        arguments = _arguments_from_url(url)
        self.last_section_arguments = arguments
        if arguments.get("sectionBeanId") == "pendingSection":
            projection = _projection("pending-1", "Quarterly report", pending=True)
        elif arguments.get("panelId") == "done-panel":
            projection = _projection("done-1", "Completed request")
        elif arguments.get("panelId") == "tracked-panel":
            projection = _projection("tracked-1", "Tracked request")
        else:
            projection = _projection("sent-1", "Sent request")
        if self.section_payload_override is not None:
            projection = self.section_payload_override
        return {
            "status": 200,
            "url": url,
            "content_type": "application/json",
            "json": projection,
            "text": "",
        }

    def rendered_snapshot(self, url, **_kwargs):
        self.render_calls.append(url)
        return {
            "url": "http://oa.example.test/seeyon/detail?detail-internal-token=secret",
            "title": "Completed request",
            "html": """
                <html><body>
                  <h1>Completed request Alice 2026-07-10</h1>
                  <div class="processLog">Opinion approved by Alice 2026-07-10 10:30</div>
                </body></html>
            """,
            "frames": [
                {
                    "url": "http://oa.example.test/seeyon/cap4",
                    "html": """
                        <html><body>
                          <table><tr><th>Applicant</th><td>Alice</td></tr></table>
                          <div>Rendered business body</div>
                          <a href="/seeyon/fileUpload.do?method=download&fileId=secret">brief.pdf</a>
                          <script>var jsonArrBase = '[{"codes":["ContinueSubmit"]}]';</script>
                        </body></html>
                    """,
                }
            ],
        }


class FakeWorkflowLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs):
        return None

    def click(self, **_kwargs):
        if "moreTrack" in self.selector:
            self.page.locator_clicks.append("more-track")
        else:
            self.page.locator_clicks.append("tracking-tab")


class FakeTrackedFrame:
    def __init__(self):
        self.url = (
            "http://oa.example.test/seeyon/portalAffair/"
            "portalAffairController.do?method=moreTrack"
        )
        self.wait_calls = []

    def wait_for_function(self, _expression, *, arg, timeout):
        self.wait_calls.append({**arg, "timeout": timeout})

    def evaluate(self, _expression, arg):
        if arg.get("gridId") != "gridId":
            raise AssertionError(f"unexpected tracked grid id: {arg.get('gridId')}")
        return {
            "total": 28,
            "page": 1,
            "items": [
                {
                    "affair_id": "tracked-sent",
                    "title": "Tracked sent request",
                    "status": "Alice",
                    "date": "2026-07-02 17:11",
                    "category": "Collaboration (Sent)",
                    "open_from": "listSent",
                },
                {
                    "affair_id": "tracked-done",
                    "title": "Tracked done request",
                    "status": "Completed",
                    "date": "2026-06-01 09:00",
                    "category": "Collaboration (Done)",
                    "open_from": "listDone",
                },
            ],
        }


class FakeTrackedIdPage:
    def __init__(self, affair_ids):
        self.url = (
            "http://oa.example.test/seeyon/portalAffair/"
            "portalAffairController.do?method=moreTrack"
        )
        self.affair_ids = list(affair_ids)
        self.wait_calls = []

    def wait_for_function(self, _expression, *, arg, timeout):
        self.wait_calls.append({**arg, "timeout": timeout})

    def evaluate(self, _expression, arg):
        if arg.get("gridId") != "gridId":
            raise AssertionError(f"unexpected tracked grid id: {arg.get('gridId')}")
        return {
            "total": len(self.affair_ids),
            "page": 1,
            "affair_ids": self.affair_ids,
        }


class FakeWorkflowPage:
    def __init__(self):
        self.url = BASE_URL
        self.wait_calls = []
        self.locator_clicks = []
        self.frames = [FakeTrackedFrame()]
        self.context = FakeWorkflowContext(self)

    def locator(self, selector):
        return FakeWorkflowLocator(self, selector)

    def wait_for_function(self, _expression, *, arg, timeout):
        self.wait_calls.append({**arg, "timeout": timeout})

    def evaluate(self, _expression, arg):
        if arg.get("managerMethod") in {"getSentList", "getDoneList"}:
            collection = "listSent" if arg["managerMethod"] == "getSentList" else "listDone"
            rows = self.evaluate("", {"gridId": collection})["items"]
            value = arg["filters"].get("createDate") or arg["filters"].get("dealDate")
            start, end = value.split("#")
            rows = [row for row in rows if (not start or row["date"][:10] >= start)
                    and (not end or row["date"][:10] <= end)]
            return {"total": len(rows), "pages": 1 if rows else 0,
                    "page": arg["pageNumber"], "size": arg["pageSize"], "items": rows}
        if arg.get("gridId") == "listSent":
            return {
                "total": 215,
                "page": 1,
                "items": [
                    {
                        "affair_id": "sent-1",
                        "title": "Sent request",
                        "status": "In progress",
                        "date": "2026-07-08 09:00",
                        "date_basis": "initiated_at",
                        "is_track": False,
                    },
                    {
                        "affair_id": "shared-tracked",
                        "title": "Shared tracked request",
                        "status": "In progress",
                        "date": "2026-07-01 09:00",
                        "date_basis": "initiated_at",
                        "is_track": True,
                    },
                ],
            }
        if arg.get("gridId") == "listDone":
            return {
                "total": 320,
                "page": 1,
                "items": [
                    {
                        "affair_id": "done-1",
                        "title": "Completed request",
                        "status": "Completed",
                        "date": "2026-07-10 10:30",
                        "date_basis": "processed_at",
                        "is_track": False,
                    },
                    {
                        "affair_id": "shared-tracked",
                        "title": "Shared tracked request",
                        "status": "Completed",
                        "date": "2026-07-01 09:00",
                        "date_basis": "processed_at",
                        "is_track": True,
                    },
                    {
                        "affair_id": "done-tracked",
                        "title": "Done tracked request",
                        "status": "Completed",
                        "date": "2026-07-03 09:00",
                        "date_basis": "processed_at",
                        "is_track": True,
                    },
                ],
            }
        raise AssertionError(f"unexpected grid id: {arg.get('gridId')}")

    def content(self):
        return """
            <div id="section_sent-entity">
              <ul>
                <li id="sectionName_sent-panel" title="\u5df2\u53d1\u4e8b\u9879">\u5df2\u53d1\u4e8b\u9879</li>
                <li id="sectionName_done-panel" title="\u5df2\u529e\u4e8b\u9879">\u5df2\u529e\u4e8b\u9879</li>
                <li id="sectionName_tracked-panel" title="\u8ddf\u8e2a\u4e8b\u9879">\u8ddf\u8e2a\u4e8b\u9879</li>
              </ul>
            </div>
        """


class FakeWorkflowContext:
    def __init__(self, page):
        self.pages = [page]


def _section_url(section_bean_id, *, entity_id, panel_id):
    arguments = {
        "sectionBeanId": section_bean_id,
        "entityId": entity_id,
        "panelId": panel_id,
        "ownerId": "owner-secret",
        "spaceId": "space-current",
    }
    return "http://oa.example.test/seeyon/ajax.do?" + urlencode(
        {
            "method": "ajaxAction",
            "managerName": "sectionManager",
            "managerMethod": "doProjection",
            "arguments": json.dumps(arguments, separators=(",", ":")),
        }
    )


def _arguments_from_url(url):
    raw = parse_qs(urlparse(url).query).get("arguments", ["{}"])[0]
    return json.loads(raw)


def _projection(affair_id, title, *, pending=False):
    cells = [
        {
            "cellContentHTML": title,
            "id": affair_id,
            "linkURL": (
                "/seeyon/collaboration/collaboration.do"
                f"?method=summary&affairId={affair_id}&internal=secret"
            ),
            "className": "ReadDifferFromNotRead" if pending else "",
        },
        {"cellContentHTML": "Alice" if pending else "Alice&nbsp待处理"},
        {"cellContentHTML": "2026-07-10"},
        {"cellContentHTML": "Collaboration"},
    ]
    return {
        "Name": "section",
        "Data": {"dataCount": 1, "pageNo": 1, "rows": [{"cells": cells}]},
    }


if __name__ == "__main__":
    unittest.main()
