from __future__ import annotations

from datetime import date
import time


HISTORY_QUERY_PAGE_SIZE = 50
HISTORY_QUERY_MAX_ROWS = 1000
HISTORY_QUERY_TIMEOUT_SECONDS = 20

# Filter names and returned date columns are different in Seeyon's own grid.
_CONTRACTS = {
    "done": ("getDoneList", "dealDate", "dealTime", "processed_at"),
    "sent": ("getSentList", "createDate", "startDate", "initiated_at"),
}

_HISTORY_QUERY_PAGE_SCRIPT = r"""
async ({managerMethod, filters, pageNumber, pageSize, dateField, dateBasis, timeoutMs}) => {
  if (typeof window.colManager !== 'function' || typeof window.CallerResponder !== 'function') {
    throw new Error('OA history query contract unavailable');
  }
  const manager = new window.colManager();
  if (typeof manager[managerMethod] !== 'function') {
    throw new Error('OA history query method unavailable');
  }
  const payload = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('OA history query timed out')), timeoutMs);
    const responder = new window.CallerResponder();
    responder.success = value => { clearTimeout(timer); resolve(value); };
    responder.error = () => { clearTimeout(timer); reject(new Error('OA history query failed')); };
    try {
      manager[managerMethod]({page: pageNumber, size: pageSize}, filters, responder);
    } catch (error) { clearTimeout(timer); reject(error); }
  });
  if (!payload || !Array.isArray(payload.data)) {
    throw new Error('OA history query returned invalid rows');
  }
  const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
  return {
    total: payload.total, pages: payload.pages, page: payload.page, size: payload.size,
    items: payload.data.map(row => ({
      affair_id: clean(row.affairId), title: clean(row.subject),
      date: clean(row[dateField]), date_basis: dateBasis,
      status: clean(row.currentNodesInfo), sender: clean(row.startMemberName),
      category: clean(row.category), is_track: row.isTrack === true,
    })),
  };
}
"""


def read_filtered_history(
    page, *, collection: str, start_date: date | None, end_date: date | None
) -> dict:
    manager_method, filter_field, date_field, date_basis = _CONTRACTS[collection]
    filters = {
        filter_field: f"{start_date.isoformat() if start_date else ''}#"
        f"{end_date.isoformat() if end_date else ''}",
        "dumpData": "false",
    }
    if collection == "done":
        filters.update({"deduplication": "false", "aiProcessing": "false"})
    items: list[dict] = []
    seen: set[str] = set()
    total = pages = None
    pages_fetched = 0
    reason = "server_query_scan_budget_reached"
    deadline = time.monotonic() + HISTORY_QUERY_TIMEOUT_SECONDS
    for page_number in range(1, HISTORY_QUERY_MAX_ROWS // HISTORY_QUERY_PAGE_SIZE + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "server_query_time_budget_reached"
            break
        try:
            payload = page.evaluate(
                _HISTORY_QUERY_PAGE_SCRIPT,
                {
                    "managerMethod": manager_method,
                    "filters": filters,
                    "pageNumber": page_number,
                    "pageSize": HISTORY_QUERY_PAGE_SIZE,
                    "dateField": date_field,
                    "dateBasis": date_basis,
                    "timeoutMs": max(1, min(8000, int(remaining * 1000))),
                },
            )
        except Exception:
            if not pages_fetched:
                raise
            reason = "server_query_page_failed"
            break
        if not isinstance(payload, dict):
            reason = "server_query_metadata_invalid"
            break
        current_total = _nonnegative_integer(payload.get("total"))
        current_pages = _nonnegative_integer(payload.get("pages"))
        current_page = _nonnegative_integer(payload.get("page"))
        size = _nonnegative_integer(payload.get("size"))
        rows = payload.get("items")
        if (
            current_total is None or current_pages is None
            or current_page != page_number or size != HISTORY_QUERY_PAGE_SIZE
            or not isinstance(rows, list)
            or (current_total == 0 and (current_pages not in {0, 1} or rows))
            or (current_total > 0 and current_pages != (current_total + size - 1) // size)
        ):
            reason = "server_query_metadata_invalid"
            break
        if total is None:
            total, pages = current_total, current_pages
        elif (total, pages) != (current_total, current_pages):
            reason = "server_query_changed_during_pagination"
            break
        expected_count = min(size, max(0, total - (page_number - 1) * size))
        if len(rows) != expected_count:
            reason = "server_query_page_size_mismatch"
            break
        pages_fetched += 1
        invalid_reason = None
        for row in rows:
            if not isinstance(row, dict) or not row.get("affair_id") or not row.get("title"):
                invalid_reason = "server_query_row_invalid"
                continue
            affair_id = str(row["affair_id"])
            if affair_id in seen:
                invalid_reason = "server_query_duplicate_rows"
                continue
            try:
                row_date = date.fromisoformat(str(row.get("date") or "")[:10])
            except ValueError:
                invalid_reason = "server_query_date_unproven"
                continue
            if row.get("date_basis") != date_basis:
                invalid_reason = "server_query_date_unproven"
                continue
            if (start_date and row_date < start_date) or (end_date and row_date > end_date):
                invalid_reason = "server_query_filter_not_honored"
                continue
            seen.add(affair_id)
            items.append(row)
        if invalid_reason:
            reason = invalid_reason
            break
        if len(items) == total and page_number >= pages:
            reason = "server_filtered_source_exhausted"
            break
    return {
        "items": items,
        "total": total,
        "page": 1,
        "query_evidence": {
            "serverFilterApplied": pages_fetched > 0,
            "sourceQueryTotal": total,
            "sourceQueryPages": pages,
            "pagesFetched": pages_fetched,
            "scanBudget": HISTORY_QUERY_MAX_ROWS,
            "completionReason": reason,
            "complete": reason == "server_filtered_source_exhausted",
        },
    }


def _nonnegative_integer(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
