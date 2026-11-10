"""Exact, bounded workflow observations from a governed OA session.

The caller owns authorization and supplies a server-held history source, never a
model-supplied URL. A fresh done/sent list proves visibility in the current
session before the four identities are checked again on the summary page.
Opening that page may mark the affair read: this adapter does not certify it as
safe for unattended observation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import time
from urllib.parse import urlencode, urljoin, urlparse

from agentbridge.adapters.seeyon_central import (
    SeeyonLoginRequired, SeeyonReadContractMismatch,
)
from agentbridge.adapters.seeyon_history_report import (
    PAGE_SIZE, _DIAGRAM_SCRIPT, _id, _same_route, _text,
    parse_current_nodes, read_report_page,
)
from agentbridge.browser.read_budget import (
    ReadBudgetExceeded, check_read_deadline, read_deadline_scope, remaining_ms, remaining_seconds,
)

IDENTITY_FIELDS = ("affair_id", "summary_id", "process_id", "case_id")
MAX_SOURCE_PAGES = 20
READ_TIMEOUT_SECONDS = 30
_SOURCE_FIELDS = {*IDENTITY_FIELDS, "title", "flow_finished"}
_STATE_FIELDS = ("workflow_state", "current_nodes", "current_handlers")

_DETAIL_IDENTITY_SCRIPT = r"""
() => ({
  affair_id:String(window.affairId||''),
  summary_id:String(window.summaryId||''),
  process_id:String(window._summaryProcessId||''),
  case_id:String(window._summaryCaseId||''),
  finished:window.isFinish===true||window.isFinish==='true' ? true :
    window.isFinish===false||window.isFinish==='false' ? false : null
})
"""


def _source_identity(source, collection):
    if not isinstance(collection, str) or collection not in {"done", "sent"}:
        raise SeeyonReadContractMismatch("Workflow observation requires done or sent")
    if not isinstance(source, dict) or set(source) - _SOURCE_FIELDS:
        raise SeeyonReadContractMismatch("Invalid workflow observation source")
    identity = {field: _id(source.get(field)) for field in IDENTITY_FIELDS}
    title = source.get("title")
    if not isinstance(title, str) or not title or len(title) > 1000 or not _text(title):
        raise SeeyonReadContractMismatch("Workflow observation source has no valid title")
    identity["title"] = _text(title, 1000)
    return identity


def _signature(result, page_number, expected_metadata=None):
    """Validate pagination before treating the exact source as current evidence."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise SeeyonReadContractMismatch("Invalid workflow source page")
    total, pages = result.get("total"), result.get("pages")
    if (type(total) is not int or total < 0 or type(pages) is not int or pages < 0
            or type(result.get("page")) is not int or result["page"] != page_number
            or type(result.get("size")) is not int or result["size"] != PAGE_SIZE
            or (total == 0 and pages not in {0, 1})
            or (total > 0 and pages != (total + PAGE_SIZE - 1) // PAGE_SIZE)
            or len(result["items"]) != min(PAGE_SIZE, max(0, total - (page_number - 1) * PAGE_SIZE))
            or (expected_metadata is not None and (total, pages) != expected_metadata)):
        raise SeeyonReadContractMismatch("Workflow source pagination changed or is incomplete")
    rows = []
    for row in result["items"]:
        if not isinstance(row, dict) or not isinstance(row.get("_source"), dict):
            raise SeeyonReadContractMismatch("Invalid workflow source row")
        source = row["_source"]
        rows.append(tuple(source.get(field) for field in (*IDENTITY_FIELDS, "title")))
    return total, pages, tuple(rows)


def _visible_source(adapter, worker, source, collection):
    temporal = {"dateBasis": "processed_at" if collection == "done" else "initiated_at",
                "absoluteRange": {"start": None, "end": None}}
    metadata = anchor = None
    seen = set()
    for page_number in range(1, MAX_SOURCE_PAGES + 1):
        check_read_deadline()
        result = read_report_page(adapter, worker, collection, page_number, temporal)
        signature = _signature(result, page_number, metadata)
        if metadata is None:
            metadata, anchor = signature[:2], signature
        match = None
        for row in result["items"]:
            candidate = row["_source"]
            affair_id = _id(candidate.get("affair_id"))
            if affair_id in seen:
                raise SeeyonReadContractMismatch("Duplicate workflow source identity")
            seen.add(affair_id)
            if affair_id != source["affair_id"]:
                continue
            if any(_id(candidate.get(field)) != source[field] for field in IDENTITY_FIELDS):
                raise SeeyonReadContractMismatch("Workflow source identity changed")
            if candidate.get("title") != source["title"]:
                raise SeeyonReadContractMismatch("Workflow source title changed")
            match = candidate
        if match is not None:
            # A later page can move under concurrent inserts/removals even when
            # totals remain equal. Recheck the first ordering anchor as well.
            if page_number > 1:
                current = read_report_page(adapter, worker, collection, 1, temporal)
                if _signature(current, 1, metadata) != anchor:
                    raise SeeyonReadContractMismatch("Workflow source changed during pagination")
            return
        if page_number >= result["pages"]:
            raise SeeyonReadContractMismatch("Workflow source is not visible in the current collection")
    raise SeeyonReadContractMismatch("Workflow source visibility exceeds the bounded scan")


def _check_route(worker, expected):
    check_read_deadline()
    if "login" in worker.page_url.lower():
        raise SeeyonLoginRequired("OA session expired while observing the workflow")
    if not _same_route(worker.page_url, expected):
        raise SeeyonReadContractMismatch("Workflow observation route changed")


@contextmanager
def _read_errors(worker):
    try:
        yield
    except (SeeyonReadContractMismatch, SeeyonLoginRequired, ReadBudgetExceeded):
        raise
    except Exception as exc:
        check_read_deadline()
        try:
            url = worker.page_url
        except Exception:
            url = ""
        if isinstance(url, str) and "login" in url.lower():
            raise SeeyonLoginRequired("OA session expired while observing the workflow") from exc
        # Browser exceptions may contain page content or route parameters.
        raise SeeyonReadContractMismatch("Workflow state could not be verified") from exc


def _detail_identity(page, source):
    identity = page.evaluate(_DETAIL_IDENTITY_SCRIPT)
    check_read_deadline()
    if not isinstance(identity, dict) or any(identity.get(field) != source[field] for field in IDENTITY_FIELDS):
        raise SeeyonReadContractMismatch("Workflow identity changed or access is unavailable")
    if identity.get("finished") is not None and type(identity["finished"]) is not bool:
        raise SeeyonReadContractMismatch("Invalid workflow completion evidence")
    return identity.get("finished")


def _snapshot(source, collection, state, nodes, handlers):
    unavailable = list(_STATE_FIELDS) if state == "unknown" else []
    return {
        "target": {"collection": collection, **source},
        "workflow_state": state,
        "current_nodes": nodes,
        "current_handlers": handlers,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "unavailable_fields": unavailable,
        "completeness": "partial" if unavailable else "complete",
        "read_side_effects": ["may_mark_read"],
        "background_read_safe": False,
        "safety_level": "foreground_only_may_mark_read",
    }


def read_workflow_state(adapter, worker, *, source: dict, collection: str) -> dict:
    """Observe one current workflow; authorization remains the caller's duty.

    The source's old ``flow_finished`` is deliberately ignored. Every read proves
    current visibility and all four identities. Unknown/missing evidence never
    means completion; a completed workflow says nothing about payment.
    """
    source = _source_identity(source, collection)
    if not callable(getattr(worker, "fork_page", None)):
        raise SeeyonReadContractMismatch("Workflow observation requires an isolated reader")
    with read_deadline_scope(time.monotonic() + READ_TIMEOUT_SECONDS), worker.fork_page() as reader, _read_errors(reader):
        _visible_source(adapter, reader, source, collection)
        route = urljoin(adapter.base_url, "collaboration/collaboration.do?" + urlencode({
            "method": "summary", "openFrom": "listDone" if collection == "done" else "listSent",
            "affairId": source["affair_id"], "showTab": "true",
        }))
        page = reader.goto(route, timeout_seconds=remaining_seconds(12))
        _check_route(reader, route)
        page.wait_for_function(
            "() => typeof window.summaryId!=='undefined' && typeof window.refreshWorkflow==='function'",
            timeout=remaining_ms(8000),
        )
        _check_route(reader, route)
        finished = _detail_identity(page, source)
        _check_route(reader, route)
        if finished is None:
            return _snapshot(source, collection, "unknown", None, None)
        if finished:
            return _snapshot(source, collection, "ended", [], [])
        page.locator("#workflow_view_li a").click(timeout=remaining_ms(5000))
        frame = page.frame_locator("#iframeright")
        frame.locator("g.wf_node[node_id]").first.wait_for(state="attached", timeout=remaining_ms(8000))
        diagram_route = urljoin(adapter.base_url, "workflow/designer.do?method=showDiagram")
        candidates = [frame for frame in page.frames if urlparse(frame.url).path == urlparse(diagram_route).path]
        if len(candidates) != 1 or not _same_route(candidates[0].url, diagram_route):
            raise SeeyonReadContractMismatch("Workflow diagram route is not authoritative")
        diagram = candidates[0].evaluate(_DIAGRAM_SCRIPT)
        check_read_deadline()
        if not _same_route(candidates[0].url, diagram_route):
            raise SeeyonReadContractMismatch("Workflow diagram route changed during the read")
        nodes = parse_current_nodes(diagram, source)
        _check_route(reader, route)
        if _detail_identity(page, source) is not False:
            raise SeeyonReadContractMismatch("Workflow state changed during the read")
        _check_route(reader, route)
        handlers = list(dict.fromkeys(person for node in nodes for person in node["handlers"]))
        return _snapshot(source, collection, "in_progress", nodes, handlers)
