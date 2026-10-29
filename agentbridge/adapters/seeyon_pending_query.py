"""Read the verified all-pending grid without changing the active action page."""
from __future__ import annotations

from copy import deepcopy
import re
import time
from urllib.parse import parse_qs, urljoin, urlparse

from agentbridge.adapters.seeyon_home import _join_app_url
from agentbridge.browser.read_budget import (
    ReadBudgetExceeded, check_read_deadline, current_read_deadline,
    remaining_ms, remaining_seconds,
)

PENDING_QUERY_PAGE_SIZE = 50
PENDING_QUERY_MAX_ROWS = 100
PENDING_QUERY_TIMEOUT_SECONDS = 15


class PendingQueryContractMismatch(ValueError):
    pass


class PendingQueryLoginRequired(ValueError):
    pass


_PENDING_GRID_READY_SCRIPT = """() => Boolean(window.grid?.p?.managerName === 'pendingManager'
  && window.grid.p.managerMethod === 'getMoreList4SectionContion'
  && typeof window.pendingManager === 'function' && typeof window.CallerResponder === 'function')"""
_PENDING_GRID_CONTEXT_SCRIPT = """() => ({managerName:window.grid.p.managerName,
  managerMethod:window.grid.p.managerMethod, params:JSON.parse(JSON.stringify(window.grid.p.params))})"""
_PENDING_QUERY_PAGE_SCRIPT = r"""
async ({params, pageNumber, pageSize, timeoutMs}) => {
  const grid = window.grid?.p;
  if (grid?.managerName !== 'pendingManager' || grid.managerMethod !== 'getMoreList4SectionContion'
      || typeof window.pendingManager !== 'function' || typeof window.CallerResponder !== 'function'
      || JSON.stringify(grid.params) !== JSON.stringify(params)) {
    throw new Error('OA pending query context changed');
  }
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('OA pending query timed out')), timeoutMs);
    const responder = new window.CallerResponder();
    responder.success = value => { clearTimeout(timer); resolve(value); };
    responder.error = () => { clearTimeout(timer); reject(new Error('OA pending query failed')); };
    try {
      new window.pendingManager().getMoreList4SectionContion(
        {page:pageNumber, size:pageSize, sortField:grid.sortname, sortOrder:grid.sortorder},
        params, responder);
    } catch (error) { clearTimeout(timer); reject(error); }
  });
}
"""


def _origin(url):
    value = urlparse(str(url or ""))
    return value.scheme, value.netloc


def _login(url):
    value = urlparse(str(url or ""))
    return 'login' in value.path.lower()


def _route(url, base_url, method, *, frame=False):
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query, keep_blank_values=True)
    expected = urlparse(urljoin(base_url, 'collaboration/pending.do')).path
    if (_origin(url) != _origin(base_url) or parsed.path != expected
            or query.get('method') != [method] or parsed.username or parsed.password
            or (frame and query.get('sectionName') != ['pendingSection'])):
        raise PendingQueryContractMismatch('The OA all-pending route is unavailable or changed.')
    return url


def _params(value):
    expected = {'aiSortValue', 'sectionName', 'isTrack', 'bigTitleIndex', 'fragmentId',
                'state', 'rowStr', 'ordinal'}
    if (not isinstance(value, dict) or set(value) != expected
            or value['aiSortValue'] is not None or value['sectionName'] != 'pendingSection'
            or value['isTrack'] is not False or type(value['state']) is not int or value['state'] != 3
            or value['rowStr'] != 'subject,receiveTime,sendUser,category'
            or not isinstance(value['fragmentId'], str)
            or not re.fullmatch(r'-?\d{1,20}', value['fragmentId'])
            or any(not isinstance(value[k], str) or not re.fullmatch(r'\d{1,3}', value[k])
                   for k in ('ordinal', 'bigTitleIndex'))):
        raise PendingQueryContractMismatch('The OA all-pending query scope is unavailable or changed.')
    return deepcopy(value)


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _item(row, base_url):
    if not isinstance(row, dict):
        return None
    key, title, link = row.get('id'), row.get('subject'), row.get('link')
    category = row.get('categoryLabel')
    if (not isinstance(key, str) or not re.fullmatch(r'-?\d{1,20}', key)
            or not isinstance(title, str) or not title.strip() or len(title) > 10000
            or not isinstance(category, str) or not category.strip()
            or not isinstance(link, str) or not link.strip()):
        return None
    href = _join_app_url(base_url, link)
    parsed = urlparse(href)
    if (_origin(href) != _origin(base_url) or parsed.username or parsed.password
            or parsed.fragment or parsed.scheme not in {'http', 'https'}):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if category.strip().casefold() in {'协同', 'collaboration'}:
        expected = urlparse(urljoin(base_url, 'collaboration/collaboration.do')).path
        if (parsed.path != expected or query.get('method') != ['summary']
                or query.get('affairId') != [key] or query.get('openFrom') != ['listPending']):
            return None
    elif category.strip() == '会议':
        expected = urlparse(urljoin(base_url, 'meeting.do')).path
        meeting = query.get('meetingId')
        if (parsed.path != expected or query.get('method') != ['view']
                or not meeting or len(meeting) != 1 or not re.fullmatch(r'-?\d{1,20}', meeting[0])):
            return None
    else:
        # Unknown categories remain visible facts, but must not acquire a
        # navigable route or one of the registered collaboration workflows.
        href = ''
    return {'index': 0, 'affair_id': key, 'title': title.strip(),
            'sender': str(row.get('createMemberName') or ''),
            'date': str(row.get('receiveTime') or ''),
            'category': category.strip(),
            'read': row.get('readState') == 1, 'href': href,
            'raw_text': '', 'date_basis': 'collection_display_date'}


def _signature(payload):
    return (payload.get('total'), payload.get('pages'), payload.get('page'), payload.get('size'),
            payload.get('data'))


def scan_pending_pages(frame, *, params, base_url, deadline=None):
    """Validate every page and recheck the first-page anchor after traversal."""
    params = _params(params)
    deadlines = [time.monotonic() + PENDING_QUERY_TIMEOUT_SECONDS]
    deadlines.extend(d for d in (deadline, current_read_deadline()) if d is not None)
    local_deadline = min(deadlines)
    items, seen = [], set()
    total = pages = None
    first = None
    pages_fetched = 0
    reason = 'pending_query_scan_budget_reached'
    for page_number in range(1, PENDING_QUERY_MAX_ROWS // PENDING_QUERY_PAGE_SIZE + 1):
        check_read_deadline()
        remaining = local_deadline - time.monotonic()
        if remaining <= 0:
            reason = 'pending_query_time_budget_reached'
            break
        try:
            payload = frame.evaluate(_PENDING_QUERY_PAGE_SCRIPT, {
                'params': params, 'pageNumber': page_number, 'pageSize': PENDING_QUERY_PAGE_SIZE,
                'timeoutMs': max(1, min(8000, int(remaining * 1000))),
            })
        except ReadBudgetExceeded:
            raise
        except Exception as exc:
            check_read_deadline()
            if not pages_fetched:
                raise PendingQueryContractMismatch('The OA all-pending read failed.') from exc
            reason = 'pending_query_page_failed'
            break
        check_read_deadline()
        if time.monotonic() >= local_deadline:
            reason = 'pending_query_time_budget_reached'
            break
        if not isinstance(payload, dict):
            reason = 'pending_query_metadata_invalid'
            break
        current_total, current_pages = _number(payload.get('total')), _number(payload.get('pages'))
        size, page = _number(payload.get('size')), _number(payload.get('page'))
        rows = payload.get('data')
        if (current_total is None or current_pages is None or page != page_number
                or size != PENDING_QUERY_PAGE_SIZE or not isinstance(rows, list)
                or current_pages != (current_total + size - 1) // size
                and not (current_total == 0 and current_pages == 1)):
            reason = 'pending_query_metadata_invalid'
            break
        if total is None:
            total, pages = current_total, current_pages
        elif (total, pages) != (current_total, current_pages):
            reason = 'pending_query_changed_during_pagination'
            break
        if len(rows) != min(size, max(0, total - (page_number - 1) * size)):
            reason = 'pending_query_page_size_mismatch'
            break
        pages_fetched += 1
        invalid = None
        for row in rows:
            item = _item(row, base_url)
            if item is None:
                invalid = 'pending_query_row_invalid'
                continue
            if item['affair_id'] in seen:
                invalid = 'pending_query_duplicate_rows'
                continue
            seen.add(item['affair_id'])
            item['index'] = len(items)
            items.append(item)
        if invalid:
            reason = invalid
            break
        if page_number == 1:
            first = deepcopy(payload)
        if len(items) == total and page_number >= pages:
            if page_number > 1:
                remaining = local_deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'pending_query_time_budget_reached'
                    break
                try:
                    anchor = frame.evaluate(_PENDING_QUERY_PAGE_SCRIPT, {
                        'params': params, 'pageNumber': 1, 'pageSize': PENDING_QUERY_PAGE_SIZE,
                        'timeoutMs': max(1, min(8000, int(remaining * 1000))),
                    })
                except ReadBudgetExceeded:
                    raise
                except Exception:
                    check_read_deadline()
                    reason = 'pending_query_page_failed'
                    break
                check_read_deadline()
                if time.monotonic() >= local_deadline:
                    reason = 'pending_query_time_budget_reached'
                    break
                if not isinstance(anchor, dict) or _signature(anchor) != _signature(first):
                    reason = 'pending_query_changed_during_pagination'
                    break
            reason = 'pending_source_exhausted'
            break
    return {'source': 'pending_more_query', 'count': len(items), 'items': items,
            'total': total, 'page': 1, 'query_evidence': {
                'complete': reason == 'pending_source_exhausted',
                'completionReason': reason, 'pagesFetched': pages_fetched,
                'sourceQueryTotal': total, 'sourceQueryPages': pages,
                'scanBudget': PENDING_QUERY_MAX_ROWS, 'serverFilterApplied': False}}


def read_pending_more(worker, *, base_url, projection):
    """Read only the observed all-pending route and its fixed manager contract."""
    deadline = time.monotonic() + PENDING_QUERY_TIMEOUT_SECONDS
    inherited = current_read_deadline()
    if inherited is not None:
        deadline = min(deadline, inherited)
    try:
        link = projection['Data']['moreButton']['link']
    except (KeyError, TypeError) as exc:
        raise PendingQueryContractMismatch('The OA all-pending link is unavailable.') from exc
    if not isinstance(link, str):
        raise PendingQueryContractMismatch('The OA all-pending link is unavailable.')
    more_url = _route(_join_app_url(base_url, link), base_url, 'morePendingCenter')
    try:
        page = worker.goto(more_url, timeout_seconds=remaining_seconds(15, deadline=deadline))
        check_read_deadline()
        if _login(worker.page_url):
            raise PendingQueryLoginRequired('The central OA session expired while opening all pending.')
        _route(worker.page_url, base_url, 'morePendingCenter')
        page.wait_for_selector('#pendingMainIframe', timeout=remaining_ms(8000, deadline=deadline))
        element = page.locator('#pendingMainIframe').element_handle()
        frame = element.content_frame() if element is not None else None
        if frame is None:
            raise PendingQueryContractMismatch('The OA all-pending frame is unavailable.')
        if _login(frame.url):
            raise PendingQueryLoginRequired('The central OA session expired while opening all pending.')
        # A newly created iframe may still be about:blank. Wait for its known
        # grid, then validate its actual route before reading or invoking it.
        frame.wait_for_function(_PENDING_GRID_READY_SCRIPT, timeout=remaining_ms(8000, deadline=deadline))
        _route(frame.url, base_url, 'morePendingAll', frame=True)
        context = frame.evaluate(_PENDING_GRID_CONTEXT_SCRIPT)
        if (not isinstance(context, dict) or context.get('managerName') != 'pendingManager'
                or context.get('managerMethod') != 'getMoreList4SectionContion'):
            raise PendingQueryContractMismatch('The OA all-pending grid is unavailable.')
        result = scan_pending_pages(frame, params=context.get('params'), base_url=base_url, deadline=deadline)
        if _login(worker.page_url) or _login(frame.url):
            raise PendingQueryLoginRequired('The central OA session expired while reading all pending.')
        _route(worker.page_url, base_url, 'morePendingCenter')
        _route(frame.url, base_url, 'morePendingAll', frame=True)
        return result
    except (ReadBudgetExceeded, PendingQueryLoginRequired):
        raise
    except Exception as exc:
        check_read_deadline()
        if _login(worker.page_url) or 'frame' in locals() and frame is not None and _login(frame.url):
            raise PendingQueryLoginRequired('The central OA session expired while reading all pending.') from exc
        raise PendingQueryContractMismatch('The OA all-pending grid is unavailable.') from exc
