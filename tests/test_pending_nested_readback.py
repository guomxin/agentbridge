"""Exercise full pending verification through the real independent page worker."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agentbridge.adapters.seeyon_central import SeeyonCentralAdapter, SeeyonLoginRequired
from agentbridge.adapters.seeyon_pending_verification import verify_pending_action
from agentbridge.browser.central import CentralBrowserPageWorker, CentralBrowserWorker
from agentbridge.browser.read_budget import ReadBudgetExceeded
from tests.test_central_browser import FakePage, FakePlaywrightController
from tests.test_seeyon_pending_query import BASE, FRAME, MORE, PARAMS, payload, rows


TARGET = {"affair_id": "900", "title": "合成已办事项", "summary_id": "901",
          "process_id": "902", "template_id": "903", "form_app_id": "904",
          "form_record_id": "905"}


class PendingMorePage(FakePage):
    def __init__(self, response):
        super().__init__()
        self.frame = Mock()
        self.frame.url = FRAME
        self.frame.evaluate.side_effect = [
            {"managerName": "pendingManager", "managerMethod": "getMoreList4SectionContion",
             "params": deepcopy(PARAMS)},
            response,
        ]
        self.wait_for_selector = Mock()

    def locator(self, selector):
        assert selector == "#pendingMainIframe"
        return SimpleNamespace(element_handle=lambda: SimpleNamespace(content_frame=lambda: self.frame))


@pytest.fixture
def nested_readback(tmp_path, monkeypatch):
    controller = FakePlaywrightController()
    created = []
    response = [payload(rows(11))]

    def allocate():
        page = PendingMorePage(response[0])
        controller.context.pages.append(page)
        created.append(page)
        return page

    monkeypatch.setattr(controller.context, "new_page", allocate)
    adapter = SeeyonCentralAdapter(base_url=BASE)
    home = {"source": "section_api", "count": 9, "total": 11, "page": 0,
            "items": [{"affair_id": str(index), "title": f"合成待办 {index}", "category": "协同"}
                      for index in range(1, 10)]}
    monkeypatch.setattr(adapter, "list_templates", Mock())
    monkeypatch.setattr(adapter, "_discover_pending_section_url", Mock(return_value=BASE + "ajax.do"))
    monkeypatch.setattr("agentbridge.adapters.seeyon_central.parse_pending_projection",
                        lambda _projection, **_kw: deepcopy(home))
    done = Mock(return_value={"items": [deepcopy(TARGET)],
                             "query_evidence": {"completionReason": "server_target_found"}})
    monkeypatch.setattr(adapter, "read_pending_done", done)
    with CentralBrowserWorker(profile_path=tmp_path / "profile", allowed_origins={"https://oa.example.test"},
                              playwright_starter=lambda: controller) as worker:
        monkeypatch.setattr(worker, "request", Mock(return_value={"status": 200, "url": BASE + "ajax.do",
            "json": {"Data": {"moreButton": {"link": MORE}}}}))
        action = worker.goto(BASE + "action")
        with worker.fork_page() as readback:
            assert isinstance(readback, CentralBrowserPageWorker)
            readback.goto(BASE + "readback")
            yield SimpleNamespace(adapter=adapter, worker=worker, action=action, readback=readback,
                                  response=response, home=home, created=created, done=done)


def test_real_readback_worker_completes_pending_absence_and_exact_done_identity(nested_readback):
    run = nested_readback
    with run.adapter.capability_scope(run.readback):
        scope = run.adapter._scope_for(run.readback)
        result = verify_pending_action(run.adapter, run.readback, TARGET)
        assert run.adapter._scope_for(run.readback) is scope
    assert result["confirmed"] is True
    assert result["pending_absence_confirmed"] is True
    assert result["done_identity_confirmed"] is True
    run.done.assert_called_once_with(run.readback, target=TARGET)
    assert len(run.created) == 2
    assert run.created[-1].url == MORE
    assert run.created[-1].frame.evaluate.call_count == 2
    assert run.created[-1].closed
    assert run.action.url == BASE + "action" and not run.action.closed
    assert run.readback.page_url == BASE + "readback" and not run.readback.page.closed


def test_real_readback_worker_keeps_invalid_full_coverage_unconfirmed(nested_readback):
    run = nested_readback
    run.response[0] = {**payload(rows(11)), "total": None}
    result = verify_pending_action(run.adapter, run.readback, TARGET)
    assert result["confirmed"] is False
    assert result["reason"] == "pending_coverage_incomplete"
    run.done.assert_not_called()
    assert len(run.created) == 2 and run.created[-1].closed
    assert not run.action.closed and not run.readback.page.closed


@pytest.mark.parametrize("error", [ReadBudgetExceeded("read budget"), SeeyonLoginRequired("login lost")])
def test_real_readback_nested_failure_closes_only_full_reader(nested_readback, error):
    run = nested_readback

    run.response[0] = error
    if isinstance(error, SeeyonLoginRequired):
        # The all-pending reader translates its observed redirect into the
        # adapter's authentication signal instead of asserting an empty list.
        original = run.worker._context.new_page

        def logged_out_page():
            page = original()
            page.goto = lambda *_args, **_kwargs: setattr(page, "url", BASE + "login/index.jsp")
            return page

        run.worker._context.new_page = logged_out_page
    with pytest.raises(type(error)):
        verify_pending_action(run.adapter, run.readback, TARGET)
    run.done.assert_not_called()
    assert len(run.created) == 2 and run.created[-1].closed
    assert not run.action.closed and not run.readback.page.closed
