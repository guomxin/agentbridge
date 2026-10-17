"""Invocation cache boundaries and semantic readiness, without a live OA account."""
from contextvars import Context
import json
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter, SeeyonLoginRequired
from agentbridge.adapters import seeyon_documents as documents
from agentbridge.browser.central import (
    CentralBrowserWorker, CentralBrowserPageWorker,
    _DETAIL_STATE_SCRIPT, _wait_for_detail_snapshot,
)
from tests.test_seeyon_central_workflows import BASE_URL, FakeWorkflowWorker


class ObservedWorkflowWorker(FakeWorkflowWorker):
    def __init__(self):
        super().__init__()
        self.requests = []

    @property
    def page_title(self):
        return "OA - Alice"

    def request(self, method, url, **kwargs):
        self.requests.append(url)
        return super().request(method, url, **kwargs)


class SeeyonInvocationScopeTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SeeyonCentralAdapter(base_url=BASE_URL)
        self.worker = ObservedWorkflowWorker()

    def test_probe_and_route_reused_but_pending_and_materials_are_read_again(self):
        with self.adapter.capability_scope(self.worker):
            self.adapter.probe_business_draft_session(self.worker)
            self.adapter._render_workflow_detail(self.worker, collection="pending", affair_id="pending-1")
            self.adapter._render_workflow_detail(self.worker, collection="pending", affair_id="pending-1")
        self.assertEqual(sum("/rest/template/" in url for url in self.worker.requests), 1)
        self.assertEqual(len(self.worker.requests), 3)  # one proof, two complete pending reads
        self.assertEqual(self.worker.goto_calls, [BASE_URL])
        self.assertEqual(len(self.worker.render_calls), 2)

    def test_scope_exit_and_exception_discard_proof_and_route(self):
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "abort"):
                with self.adapter.capability_scope(self.worker):
                    self.adapter._fetch_workflow_collection(self.worker, "pending")
                    raise RuntimeError("abort")
        self.assertEqual(len(self.worker.requests), 4)
        self.assertEqual(self.worker.goto_calls, [BASE_URL, BASE_URL])

    def test_pending_coverage_changes_are_not_cached_even_inside_scope(self):
        with self.adapter.capability_scope(self.worker):
            self.adapter._fetch_workflow_collection(self.worker, "pending")
            self.worker.section_payload_override = {}
            with self.assertRaisesRegex(RuntimeError, "missing Data"):
                self.adapter._fetch_workflow_collection(self.worker, "pending")
        self.assertEqual(len(self.worker.requests), 3)

    def test_account_and_context_boundaries_do_not_share_cached_proof(self):
        other = ObservedWorkflowWorker()
        with self.adapter.capability_scope(self.worker):
            first = self.adapter.list_templates(self.worker)
            first["transport"] = "mutated"
            self.assertEqual(self.adapter.list_templates(self.worker)["transport"], "central_http_session")
            self.adapter.list_templates(other)
            Context().run(self.adapter.list_templates, self.worker)
            with self.adapter.capability_scope(other):
                self.adapter.list_templates(other)
            self.adapter.list_templates(self.worker)
        self.assertEqual(len(self.worker.requests), 2)
        self.assertEqual(len(other.requests), 2)

    def test_unsuccessful_probe_is_never_cached(self):
        original = self.worker.request
        calls = 0

        def expired_once(method, url, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"status": 401, "url": url}
            return original(method, url, **kwargs)

        self.worker.request = expired_once
        with self.adapter.capability_scope(self.worker):
            with self.assertRaises(SeeyonLoginRequired):
                self.adapter.probe_session(self.worker)
            self.adapter.probe_session(self.worker)
        self.assertEqual(calls, 2)


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now


class DetailFrame:
    def __init__(self, page, *, blank=False, busy=False):
        self.page, self.blank, self.busy = page, blank, busy
        self.url = "about:blank" if blank else "http://oa.example.test/seeyon/cap4"

    def evaluate(self, script):
        return {"ready": not self.busy and self.page.clock.now >= self.page.ready_after,
                "placeholder": self.blank, "material": not self.blank,
                "text": "" if self.blank else "Target material"}

    def content(self):
        if self.blank:
            return "<html><body></body></html>"
        # A late material change must restart the short stability window.
        return "<table>" + ("new" if self.page.clock.now >= self.page.change_after else "old") + "</table>"


class DetailPage:
    def __init__(self, clock, *, ready_after=0, change_after=0, wrong_target=False):
        self.clock, self.ready_after, self.change_after = clock, ready_after, change_after
        self.url = "http://oa.example.test/seeyon/detail?affairId=" + ("other" if wrong_target else "target")
        self.main_frame = self
        self.frames = [self, DetailFrame(self)]
        self.waits = []
        self.context = SimpleNamespace(pages=[self])

    def evaluate(self, script):
        return {"ready": True, "material": False, "text": "Target"}

    def title(self):
        return "Target"

    def content(self):
        return "<h1>Target</h1>"

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock.now += milliseconds / 1000


class SeeyonDetailReadinessTests(unittest.TestCase):
    def test_title_navigation_race_resets_stability_and_recovers(self):
        page = DetailPage(Clock())
        def title():
            if .15 <= page.clock.now < .225:
                raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
            return "Target"
        page.title = title
        self.assertEqual(self.snapshot(page)["title"], "Target")
        self.assertGreaterEqual(page.clock.now, .525)

    def test_closed_page_title_is_not_retried(self):
        page = DetailPage(Clock())
        page.title = lambda: (_ for _ in ()).throw(RuntimeError("Target page has been closed"))
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.snapshot(page)
        self.assertEqual(page.waits, [])

    def snapshot(self, page, *, timeout=1):
        with patch("agentbridge.browser.central.time.monotonic", side_effect=page.clock.monotonic):
            return _wait_for_detail_snapshot(
                page, lambda url: None,
                {"kind": "oa_workflow_detail", "affair_id": "target", "title": "Target"},
                include_frames=True, timeout_seconds=timeout,
            )

    def test_delayed_frame_and_late_material_change_restart_stability(self):
        page = DetailPage(Clock(), ready_after=.15, change_after=.375)
        snapshot = self.snapshot(page)
        self.assertGreaterEqual(page.clock.now, .675)
        self.assertIn("new", snapshot["frames"][0]["html"])
        self.assertTrue(all(wait == 75 for wait in page.waits))

    def test_empty_explicit_placeholder_is_captured_and_fingerprinted(self):
        page = DetailPage(Clock())
        page.frames.append(DetailFrame(page, blank=True))
        snapshot = self.snapshot(page)
        self.assertEqual(len(snapshot["frames"]), 2)
        self.assertEqual(snapshot["frames"][1]["url"], "about:blank")
        self.assertGreaterEqual(page.clock.now, .3)

    def test_wrong_target_busy_or_unreadable_frame_never_returns_partial_materials(self):
        for kind in ("wrong_target", "busy", "unreadable", "stale_frame"):
            with self.subTest(kind=kind):
                page = DetailPage(Clock(), wrong_target=kind == "wrong_target")
                if kind == "busy":
                    page.frames[1].busy = True
                elif kind == "unreadable":
                    page.frames[1].content = lambda: (_ for _ in ()).throw(RuntimeError("detached"))
                elif kind == "stale_frame":
                    page.frames[1].url += "?affairId=other"
                with self.assertRaises(TimeoutError):
                    self.snapshot(page, timeout=.5)

    def test_navigation_and_readiness_share_one_timeout_budget_on_primary_and_fork(self):
        for worker_type in (CentralBrowserWorker, CentralBrowserPageWorker):
            with self.subTest(worker=worker_type.__name__):
                page = DetailPage(Clock())
                worker = object.__new__(worker_type)
                worker._validate_url = lambda _: None
                worker._owner = SimpleNamespace(_validate_url=lambda _: None)

                def slow_navigation(*args, **kwargs):
                    page.clock.now += .85
                    return page

                worker.goto = slow_navigation
                with patch("agentbridge.browser.central.time.monotonic", side_effect=page.clock.monotonic):
                    with self.assertRaises(TimeoutError):
                        worker.rendered_snapshot(
                            page.url, timeout_seconds=1,
                            readiness={"kind": "oa_workflow_detail", "affair_id": "target", "title": "Target"},
                        )
                self.assertLess(page.clock.now, 1.08)


@unittest.skipUnless(shutil.which("node"), "Node is needed to evaluate the DOM predicate unit fixtures")
class ReadinessJavaScriptContractTests(unittest.TestCase):
    def run_js(self, script):
        completed = subprocess.run(["node", "--input-type=module", "-e", script],
                                   check=True, text=True, capture_output=True)
        return json.loads(completed.stdout)

    def test_detail_predicate_rejects_stale_document_busy_and_pending_blank_frame(self):
        script = """
        const predicate = eval(%s);
        let declared = '', current = 'http://oa.example.test/cap4', busy = false;
        let text = 'Target material', loading = false;
        const cell = {tagName:'TD', innerText:'filled'};
        global.window = {frameElement:{getAttribute:()=>declared},
          parent:{location:{href:'http://oa.example.test/detail'}}, location:{get href(){return current}}};
        global.document = {body:{get innerText(){return text}}, get readyState(){return loading?'loading':'complete'},
          querySelectorAll:(s)=>s==='tr'?[{children:[cell,cell]}]:s.startsWith('[aria-busy')&&busy?[{getClientRects:()=>[1]}]:[],
          querySelector:()=>null};
        const results=[];
        results.push(predicate());
        declared='/new-document'; results.push(predicate());
        declared=''; busy=true; results.push(predicate());
        busy=false; current='about:blank'; text=''; results.push(predicate());
        declared='/not-yet-loaded'; results.push(predicate());
        declared=''; loading=true; results.push(predicate());
        console.log(JSON.stringify(results));
        """ % json.dumps(_DETAIL_STATE_SCRIPT)
        results = self.run_js(script)
        self.assertTrue(results[0]["ready"])
        self.assertFalse(results[1]["ready"])
        self.assertFalse(results[2]["ready"])
        self.assertTrue(results[3]["ready"])
        self.assertTrue(results[3]["placeholder"])
        self.assertFalse(results[4]["ready"])
        self.assertFalse(results[5]["ready"])


class CertificateMenuReadinessTests(unittest.TestCase):
    def test_each_menu_waits_for_its_observed_entry_and_clicks_once(self):
        page = DetailPage(Clock())
        worker = SimpleNamespace(goto=lambda *a, **kw: page)
        frame = object()
        clicked, state = [], {"available_after": .15}

        def available(*_):
            return object() if page.clock.now >= state["available_after"] else None

        def click(pages, label):
            if not available():
                return False
            self.assertGreaterEqual(page.clock.now, state["available_after"])
            clicked.append(label)
            state["available_after"] = page.clock.now + .15
            return True

        def search(*_):
            if not available():
                return False
            self.assertGreaterEqual(page.clock.now, state["available_after"])
            state["available_after"] = page.clock.now + .15
            return True

        with (patch.object(documents, "_has_login_form", return_value=False),
              patch.object(documents, "_find_exact_visible_text", side_effect=available),
              patch.object(documents, "_find_app_search", side_effect=available),
              patch.object(documents, "_click_exact_visible_text", side_effect=click),
              patch.object(documents, "_search_all_apps", side_effect=search),
              patch.object(documents, "_find_document_frame", side_effect=lambda _: frame if available() else None),
              patch.object(documents.time, "monotonic", side_effect=page.clock.monotonic)):
            result = documents._open_certificate_category(
                worker, base_url=BASE_URL, category_label="category",
            )
        self.assertIs(result, frame)
        self.assertEqual(clicked, [documents._ALL_APPS_LABEL, documents._DOCUMENT_CENTER_LABEL,
                                  documents._UNIT_DOCUMENTS_LABEL, documents._INTELLECTUAL_PROPERTY_LABEL,
                                  documents._GROUP_CERTIFICATES_LABEL, "category"])
        self.assertEqual(page.waits.count(900), 1)  # result completion contract is not known yet
        self.assertGreater(page.waits.count(75), 0)

    def test_login_and_exhausted_navigation_budget_do_not_click_or_start_new_timeout(self):
        for expired in (False, True):
            page = DetailPage(Clock())

            def navigate(*args, **kwargs):
                page.clock.now = 59.99
                return page

            worker = SimpleNamespace(goto=navigate)
            with (patch.object(documents, "_has_login_form", return_value=expired),
                  patch.object(documents, "_find_exact_visible_text", return_value=None),
                  patch.object(documents, "_click_exact_visible_text", return_value=False) as click,
                  patch.object(documents.time, "monotonic", side_effect=page.clock.monotonic)):
                error = documents.AdapterLoginRequired if expired else documents.SeeyonDocumentContractMismatch
                with self.assertRaises(error):
                    documents._open_certificate_category(worker, base_url=BASE_URL, category_label="category")
            self.assertLess(page.clock.now, 60.08)
            if expired:
                click.assert_not_called()

    def test_detached_login_frame_retries_but_expiry_and_nontransient_errors_do_not(self):
        for error in (RuntimeError("Locator.count: Frame was detached"),
                      RuntimeError("Target page, context or browser has been closed"),
                      ValueError("invalid selector")):
            with self.subTest(error=str(error)):
                page = DetailPage(Clock())
                with (patch.object(documents, "_has_login_form", side_effect=[error, False]),
                      patch.object(documents.time, "monotonic", side_effect=page.clock.monotonic)):
                    if "detached" in str(error):
                        self.assertEqual(documents._wait_for_ui(page, lambda: "ready", "missing"), "ready")
                        self.assertEqual(page.waits, [75])
                    else:
                        with self.assertRaises(type(error)):
                            documents._wait_for_ui(page, lambda: "ready", "missing")
                        self.assertEqual(page.waits, [])

    def test_persistent_navigation_failure_is_bounded(self):
        page = DetailPage(Clock())
        with (patch.object(documents, "_has_login_form", side_effect=RuntimeError("Frame was detached")),
              patch.object(documents.time, "monotonic", side_effect=page.clock.monotonic)):
            with self.assertRaises(documents.SeeyonDocumentContractMismatch):
                documents._wait_for_ui(page, lambda: True, "missing", timeout_seconds=.2)
        self.assertLess(page.clock.now, .28)

    def test_menu_detachment_and_frame_replacement_during_settle(self):
        page = DetailPage(Clock())
        worker = SimpleNamespace(goto=lambda *a, **kw: page)
        old, new = object(), object()
        calls = []
        def click(pages, label):
            calls.append(label)
            if len(calls) == 1:
                raise RuntimeError("Locator.count: Frame was detached")
            return True
        with (patch.object(documents, "_has_login_form", return_value=False),
              patch.object(documents, "_click_exact_visible_text", side_effect=click),
              patch.object(documents, "_search_all_apps", return_value=True),
              patch.object(documents, "_find_document_frame", side_effect=[old, new]),
              patch.object(documents.time, "monotonic", side_effect=page.clock.monotonic)):
            self.assertIs(documents._open_certificate_category(worker, base_url=BASE_URL,
                                                               category_label="category"), new)
        self.assertEqual(calls[:2], [documents._ALL_APPS_LABEL] * 2)
        self.assertEqual(page.waits, [75, 900])

    def test_identical_and_empty_query_results_keep_existing_result_path(self):
        for rows in ([], [{"resource_id": "id", "source_id": "source", "filename": "same.pdf"}]):
            page = DetailPage(Clock())
            control = SimpleNamespace(count=lambda: 1, first=None, fill=lambda *a, **kw: None,
                                      click=lambda **kw: None)
            control.first = control
            frame = SimpleNamespace(locator=lambda _: control, evaluate=lambda _: rows)
            worker = SimpleNamespace(page=page)
            first = documents._search_current_folder(worker, frame=frame, query="same")
            second = documents._search_current_folder(worker, frame=frame, query="same")
            self.assertEqual(first, rows)
            self.assertEqual(second, rows)
            self.assertEqual(page.waits, [900, 900])

