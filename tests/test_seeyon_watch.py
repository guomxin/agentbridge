from copy import deepcopy
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from agentbridge.adapters.seeyon_central import SeeyonLoginRequired, SeeyonReadContractMismatch
from agentbridge.adapters.seeyon_watch import read_workflow_state
from agentbridge.browser.read_budget import ReadBudgetExceeded, current_read_deadline, read_deadline_scope


SOURCE = {"affair_id": "10", "summary_id": "20", "process_id": "30", "case_id": "40",
          "title": "业务结算单", "flow_finished": True}
IDENTITY = {**{key: SOURCE[key] for key in ("affair_id", "summary_id", "process_id", "case_id")},
            "finished": False}
DIAGRAM = {
    "process_id": "30", "case_id": "40",
    "process_xml": '<ps><p i="30"><n i="old" t="6" n="已办旧节点"/>'
                   '<n i="now" t="6" n="财务复核"/><n i="parallel" t="6" n="项目核对"/></p></ps>',
    "case_log_xml": '<caseLog><S><F N="old" S="2"/></S><S><F N="old" S="3"/>'
                    '<F N="now" S="2"/><F N="parallel" S="2"/></S></caseLog>',
    "workitem_log_xml": '<caseItemLog><WL N="old" PN="历史经办人" AS="0,26,5"/>'
                        '<WL N="now" PN="当前甲" AS="0"/><WL N="now" PN="当前乙" AS="0,26"/>'
                        '<WL N="parallel" PN="当前丙" AS="0"/></caseItemLog>',
    "nodes": [{"id": "old", "icons": ["#icon_nodestate_monitor-finish_", "#icon_current"]},
              {"id": "now", "icons": ["#icon_nodestate_unread_"]},
              {"id": "parallel", "icons": ["#icon_nodestate_read_"]}],
}


def raw_row(source=None):
    source = source or SOURCE
    return {"affairId": source["affair_id"], "summaryId": source["summary_id"],
            "processId": source["process_id"], "caseId": source["case_id"],
            "subject": source["title"], "flowFinished": source.get("flow_finished")}


def history_page(rows, *, page=1, total=None):
    total = len(rows) if total is None else total
    return {"items": rows, "page": page, "size": 50, "total": total, "pages": (total + 49) // 50}


class FakeLocator:
    def __init__(self, worker, selector):
        self.worker, self.selector = worker, selector

    @property
    def first(self):
        return self

    def locator(self, selector):
        return FakeLocator(self.worker, selector)

    def click(self, **_kwargs):
        self.worker.clicks.append(self.selector)

    def wait_for(self, **_kwargs):
        if self.worker.diagram_timeout:
            raise ReadBudgetExceeded("fixture diagram timed out")


class FakeFrame:
    def __init__(self, worker):
        self.worker = worker
        self.url = "https://oa.example/seeyon/workflow/designer.do?method=showDiagram"

    def evaluate(self, script):
        self.worker.scripts.append(script)
        return deepcopy(self.worker.diagram)


class FakePage:
    def __init__(self, worker):
        self.worker = worker
        self.frames = [FakeFrame(worker)]

    def wait_for_function(self, *_args, **_kwargs):
        if self.worker.wait_timeout and "arg" not in _kwargs:
            raise ReadBudgetExceeded("fixture page timed out")

    def evaluate(self, script, arg=None):
        self.worker.scripts.append(script)
        if arg is not None:
            self.worker.list_calls.append(deepcopy(arg))
            key = arg["pageNumber"]
            payload = self.worker.pages[key]
            if callable(payload):
                payload = payload()
            if isinstance(payload, Exception):
                raise payload
            return deepcopy(payload)
        self.worker.identity_reads += 1
        if self.worker.identity_reads > 1 and self.worker.last_identity is not None:
            return deepcopy(self.worker.last_identity)
        return deepcopy(self.worker.identity)

    def locator(self, selector):
        return FakeLocator(self.worker, selector)

    def frame_locator(self, selector):
        return FakeLocator(self.worker, selector)


class FakeWorker:
    def __init__(self, pages=None, *, identity=None, diagram=None):
        self.pages = pages if pages is not None else {1: history_page([raw_row()])}
        self.identity = deepcopy(IDENTITY if identity is None else identity)
        self.diagram = deepcopy(DIAGRAM if diagram is None else diagram)
        self.last_identity = None
        self.page_url = "about:blank"
        self.page = FakePage(self)
        self.routes, self.clicks, self.list_calls, self.scripts = [], [], [], []
        self.forks = self.identity_reads = 0
        self.closed = self.wait_timeout = self.diagram_timeout = False
        self.redirect = None

    def fork_page(self):
        self.forks += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def goto(self, url, **_kwargs):
        self.routes.append(url)
        self.page_url = self.redirect or url
        return self.page


class SeeyonWatchTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SimpleNamespace(base_url="https://oa.example/seeyon/")

    def read(self, worker=None, *, source=None, collection="done"):
        return read_workflow_state(self.adapter, worker or FakeWorker(),
                                   source=SOURCE if source is None else source, collection=collection)

    def test_parallel_nodes_current_people_and_safety_metadata(self):
        worker = FakeWorker()
        result = self.read(worker)
        self.assertEqual(result["workflow_state"], "in_progress")
        self.assertEqual(result["current_nodes"], [
            {"name": "财务复核", "handlers": ["当前甲", "当前乙"]},
            {"name": "项目核对", "handlers": ["当前丙"]},
        ])
        self.assertEqual(result["current_handlers"], ["当前甲", "当前乙", "当前丙"])
        self.assertEqual(result["completeness"], "complete")
        self.assertEqual(result["unavailable_fields"], [])
        self.assertTrue(result["observed_at"].endswith("+00:00"))
        self.assertEqual(result["target"], {"collection": "done", **{k: v for k, v in SOURCE.items() if k != "flow_finished"}})
        self.assertEqual(result["read_side_effects"], ["may_mark_read"])
        self.assertFalse(result["background_read_safe"])
        self.assertEqual(result["safety_level"], "foreground_only_may_mark_read")
        self.assertEqual(worker.identity_reads, 2)
        self.assertEqual(worker.clicks, ["#workflow_view_li a"])
        self.assertTrue(worker.closed)

    def test_ended_is_only_current_summary_evidence_and_does_not_read_diagram(self):
        worker = FakeWorker(identity={**IDENTITY, "finished": True})
        result = self.read(worker, source={**SOURCE, "flow_finished": False})
        self.assertEqual(result["workflow_state"], "ended")
        self.assertEqual(result["current_nodes"], [])
        self.assertEqual(result["current_handlers"], [])
        self.assertEqual(worker.clicks, [])
        self.assertEqual(worker.identity_reads, 1)
        self.assertNotIn("payment", repr(result))

    def test_missing_finish_flag_stays_unknown_instead_of_ended_or_active(self):
        for identity in ({k: v for k, v in IDENTITY.items() if k != "finished"}, {**IDENTITY, "finished": None}):
            with self.subTest(identity=identity):
                worker = FakeWorker(identity=identity)
                result = self.read(worker)
                self.assertEqual(result["workflow_state"], "unknown")
                self.assertIsNone(result["current_nodes"])
                self.assertIsNone(result["current_handlers"])
                self.assertEqual(result["completeness"], "partial")
                self.assertEqual(set(result["unavailable_fields"]), {"workflow_state", "current_nodes", "current_handlers"})
                self.assertEqual(worker.clicks, [])

    def test_invalid_completion_flag_is_not_coerced_to_a_boolean(self):
        for finished in ("true", "false", 0, 1, [], {}):
            with self.subTest(finished=finished), self.assertRaises(SeeyonReadContractMismatch):
                self.read(FakeWorker(identity={**IDENTITY, "finished": finished}))

    def test_each_stable_identity_is_checked_in_current_list_and_summary(self):
        for field in ("affair_id", "summary_id", "process_id", "case_id"):
            with self.subTest(location="list", field=field):
                row = raw_row({**SOURCE, field: "99"})
                worker = FakeWorker({1: history_page([row])})
                with self.assertRaises(SeeyonReadContractMismatch):
                    self.read(worker)
                self.assertEqual(worker.identity_reads, 0)
            with self.subTest(location="summary", field=field):
                worker = FakeWorker(identity={**IDENTITY, field: "99"})
                with self.assertRaises(SeeyonReadContractMismatch):
                    self.read(worker)
                self.assertEqual(worker.clicks, [])

    def test_same_title_never_substitutes_for_exact_visible_source(self):
        worker = FakeWorker({1: history_page([raw_row({**SOURCE, "affair_id": "99"})])})
        with self.assertRaises(SeeyonReadContractMismatch):
            self.read(worker)
        self.assertEqual(len(worker.routes), 1)
        changed_title = FakeWorker({1: history_page([raw_row({**SOURCE, "title": "另一事项"})])})
        with self.assertRaises(SeeyonReadContractMismatch):
            self.read(changed_title)

    def test_collection_is_revalidated_for_sent_and_no_pending_route_is_used(self):
        worker = FakeWorker()
        self.read(worker, collection="sent")
        self.assertEqual(worker.list_calls[0]["method"], "getSentList")
        queries = [parse_qs(urlparse(route).query) for route in worker.routes]
        self.assertEqual(queries[0], {"method": ["listSent"]})
        self.assertEqual(queries[1]["openFrom"], ["listSent"])
        self.assertTrue(all("pending" not in route.lower() for route in worker.routes))
        for collection in ("pending", "tracked", "", None, []):
            with self.subTest(collection=collection):
                rejected = FakeWorker()
                with self.assertRaises(SeeyonReadContractMismatch):
                    self.read(rejected, collection=collection)
                self.assertEqual(rejected.forks, 0)
                self.assertEqual(rejected.routes, [])

    def test_invalid_sources_and_urls_are_rejected_before_any_browser_read(self):
        variants = [None, {}, {**SOURCE, "url": "https://untrusted.example"},
                    {**SOURCE, "href": "/arbitrary"}, {**SOURCE, "title": " "},
                    {**SOURCE, "title": "x" * 1001}]
        for field in ("affair_id", "summary_id", "process_id", "case_id"):
            variants.extend({**SOURCE, field: value} for value in (None, 10, "10&method=submit", ""))
        for source in variants:
            with self.subTest(source=source):
                worker = FakeWorker()
                with self.assertRaises(SeeyonReadContractMismatch):
                    read_workflow_state(self.adapter, worker, source=source, collection="done")
                self.assertEqual(worker.forks, 0)
                self.assertEqual(worker.routes, [])

    def test_visibility_scan_reaches_later_pages_and_rechecks_anchor(self):
        first = history_page([raw_row({**SOURCE, "affair_id": str(100 + i)}) for i in range(50)], total=51)
        second = history_page([raw_row()], page=2, total=51)
        worker = FakeWorker({1: first, 2: second})
        self.assertEqual(self.read(worker)["workflow_state"], "in_progress")
        self.assertEqual([call["pageNumber"] for call in worker.list_calls], [1, 2, 1])
        calls = 0
        def changing_first():
            nonlocal calls
            calls += 1
            value = deepcopy(first)
            if calls > 1:
                value["items"][0]["affairId"] = "999"
            return value
        changing = FakeWorker({1: changing_first, 2: second})
        with self.assertRaises(SeeyonReadContractMismatch):
            self.read(changing)
        self.assertEqual(changing.identity_reads, 0)

    def test_invalid_or_incomplete_visibility_page_never_opens_summary(self):
        variants = [history_page([]), history_page([raw_row(), raw_row()]),
                    {**history_page([raw_row()]), "total": True},
                    {**history_page([raw_row()]), "page": 2},
                    {**history_page([raw_row()]), "size": 100},
                    {**history_page([raw_row()]), "total": 2},
                    {**history_page([raw_row()]), "pages": 2}]
        for value in variants:
            with self.subTest(value=value):
                worker = FakeWorker({1: value})
                with self.assertRaises(SeeyonReadContractMismatch):
                    self.read(worker)
                self.assertEqual(worker.identity_reads, 0)

    def test_scan_budget_and_timeouts_fail_closed_and_close_reader(self):
        first = history_page([raw_row({**SOURCE, "affair_id": str(100 + i)}) for i in range(50)], total=51)
        worker = FakeWorker({1: first})
        with patch("agentbridge.adapters.seeyon_watch.MAX_SOURCE_PAGES", 1):
            with self.assertRaises(SeeyonReadContractMismatch):
                self.read(worker)
        self.assertTrue(worker.closed)
        self.assertEqual(worker.identity_reads, 0)
        for stage in ("list", "summary", "diagram"):
            with self.subTest(stage=stage):
                worker = FakeWorker({1: ReadBudgetExceeded("fixture list timeout")} if stage == "list" else None)
                worker.wait_timeout = stage == "summary"
                worker.diagram_timeout = stage == "diagram"
                with self.assertRaises(ReadBudgetExceeded):
                    self.read(worker)
                self.assertTrue(worker.closed)

    def test_parent_budget_is_inherited_and_browser_failures_are_sanitized(self):
        worker = FakeWorker()
        def exhausted(*_args, **_kwargs):
            self.assertEqual(current_read_deadline(), 12)
            raise ReadBudgetExceeded("fixture parent budget")
        with patch("agentbridge.adapters.seeyon_watch.time.monotonic", return_value=10):
            with read_deadline_scope(12, inherit=False):
                with patch("agentbridge.adapters.seeyon_watch.read_report_page", side_effect=exhausted):
                    with self.assertRaises(ReadBudgetExceeded):
                        self.read(worker)
        self.assertTrue(worker.closed)
        worker = FakeWorker({1: RuntimeError("private browser route and page content")})
        with self.assertRaises(SeeyonReadContractMismatch) as error:
            self.read(worker)
        self.assertNotIn("private", str(error.exception))
        self.assertTrue(worker.closed)

    def test_missing_diagram_evidence_and_mid_read_identity_change_fail_closed(self):
        for diagram in ({**DIAGRAM, "case_id": "99"}, {**DIAGRAM, "nodes": []},
                        {**DIAGRAM, "workitem_log_xml": "<caseItemLog/>"}):
            with self.subTest(diagram=diagram), self.assertRaises(SeeyonReadContractMismatch):
                self.read(FakeWorker(diagram=diagram))
        for changed in ({**IDENTITY, "affair_id": "99"}, {**IDENTITY, "finished": True}):
            with self.subTest(changed=changed):
                worker = FakeWorker()
                worker.last_identity = changed
                with self.assertRaises(SeeyonReadContractMismatch):
                    self.read(worker)

    def test_login_and_unregistered_redirects_are_not_sources(self):
        for redirect, error in (("https://oa.example/seeyon/login", SeeyonLoginRequired),
                                ("https://other.example/seeyon/collaboration/collaboration.do?method=listDone", SeeyonReadContractMismatch)):
            with self.subTest(redirect=redirect):
                worker = FakeWorker()
                worker.redirect = redirect
                with self.assertRaises(error):
                    self.read(worker)
                self.assertEqual(worker.list_calls, [])

    def test_unregistered_or_ambiguous_diagram_routes_fail_closed(self):
        for route in ("https://untrusted.example/seeyon/workflow/designer.do?method=showDiagram",
                      "https://oa.example/seeyon/workflow/designer.do?method=edit"):
            with self.subTest(route=route):
                worker = FakeWorker()
                worker.page.frames[0].url = route
                with self.assertRaises(SeeyonReadContractMismatch):
                    self.read(worker)
        worker = FakeWorker()
        worker.page.frames.append(FakeFrame(worker))
        with self.assertRaises(SeeyonReadContractMismatch):
            self.read(worker)

    def test_scripts_only_extract_registered_state_not_body_opinions_or_urls(self):
        worker = FakeWorker()
        self.read(worker)
        for script in worker.scripts:
            self.assertNotIn("document.body", script)
            self.assertNotIn("innerText", script)
            self.assertNotIn("opinion", script.lower())
            self.assertNotIn("location.href", script)


if __name__ == "__main__":
    unittest.main()
