"""Read-only, shared evidence rules for processing an OA pending affair.

An absent row is meaningful only in an exhausted pending collection. Even then,
the same affair must be present in the user's done collection with the frozen
workflow identity. A partial done collection may prove presence, never absence.
The caller owns the independent readback worker and polling/retry policy.
"""

from __future__ import annotations

from agentbridge.browser.read_budget import read_deadline_scope, check_read_deadline


VERIFICATION_METHOD = "complete_pending_absence_and_done_identity"

_STABLE_IDENTITY_FIELDS = (
    "summary_id",
    "process_id",
    "template_id",
    "form_app_id",
    "form_record_id",
)


def read_pending_done(adapter, readback_worker, target: dict, *, deadline: float | None = None) -> dict:
    """One bounded done observation, shared by readback and recovery assessment."""
    with read_deadline_scope(deadline):
        reader = getattr(adapter, "read_pending_done", None)
        if callable(reader):
            result = reader(readback_worker, target=target)
        else:
            # Legacy/test adapters may already expose a complete or identity-rich
            # done snapshot. Production Seeyon always uses the controlled scan.
            result = adapter.list_workflows(readback_worker, collection="done", arguments={"limit": 1000})
        check_read_deadline()
        return result


def verify_pending_action(
    adapter, readback_worker, target: dict, *, pending_snapshot=None,
    done_snapshot=None, deadline: float | None = None,
) -> dict:
    with read_deadline_scope(deadline):
        result = _verify_pending_action(adapter, readback_worker, target,
                                        pending_snapshot=pending_snapshot, done_snapshot=done_snapshot)
        check_read_deadline()
        return result


def _verify_pending_action(adapter, readback_worker, target: dict, *, pending_snapshot, done_snapshot) -> dict:
    """Return one observation; read failures propagate without asserting success.

    The adapter may supplement an incomplete home section with the verified
    all-pending grid. If either path cannot prove complete coverage (including
    more than 100 rows), fail closed. Raising the limit is not completeness.
    """
    affair_id = _text(target.get("affair_id"))
    if not affair_id:
        raise ValueError("A frozen affair_id is required for pending verification.")
    if not all(_text(target.get(name)) for name in ("summary_id", "process_id")):
        return _observation("frozen_identity_incomplete")

    pending = pending_snapshot if pending_snapshot is not None else adapter.list_workflows(
        readback_worker, collection="pending", arguments={"limit": 100}
    )
    check_read_deadline()
    rows = _rows(pending)
    if rows is None:
        return _observation("pending_response_invalid")
    if any(_text(row.get("affair_id")) == affair_id for row in rows):
        return _observation("still_pending")
    coverage = pending.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("status") != "complete":
        return _observation("pending_coverage_incomplete")
    if coverage.get("hasMore") is True or not _consistent_complete_collection(pending, rows):
        return _observation("pending_coverage_inconsistent")

    done = done_snapshot if done_snapshot is not None else read_pending_done(adapter, readback_worker, target)
    check_read_deadline()
    done_rows = _rows(done)
    if done_rows is None:
        return _observation("done_response_invalid", pending_absence_confirmed=True)
    evidence = done.get("query_evidence")
    if isinstance(evidence, dict) and evidence.get("completionReason") not in {
        "server_target_found", "server_source_exhausted", "server_filtered_source_exhausted",
    }:
        return _observation("done_scan_unproven", pending_absence_confirmed=True)
    matches = [row for row in done_rows if _text(row.get("affair_id")) == affair_id]
    if not matches:
        return _observation("done_affair_not_found", pending_absence_confirmed=True)
    if len(matches) != 1:
        return _observation("done_affair_ambiguous", pending_absence_confirmed=True)
    row = matches[0]
    if _title_mismatch(target, row):
        return _observation("done_title_mismatch", pending_absence_confirmed=True)

    identity = _row_identity(row)
    if not all(_text(identity.get(name)) for name in ("summary_id", "process_id")):
        resolve_row = getattr(adapter, "resolve_done_workflow_row_detail", None)
        if callable(resolve_row):
            source, _detail = resolve_row(readback_worker, source_item=row)
        else:
            source, _detail = adapter.resolve_workflow_detail(
                readback_worker, collection="done", affair_id=affair_id
            )
        check_read_deadline()
        if not isinstance(source, dict) or _text(source.get("affair_id")) != affair_id:
            return _observation("done_detail_affair_mismatch", pending_absence_confirmed=True)
        if _title_mismatch(target, source):
            return _observation("done_title_mismatch", pending_absence_confirmed=True)
        signals = readback_worker.page.evaluate(_DONE_IDENTITY_SCRIPT, affair_id)
        check_read_deadline()
        if not isinstance(signals, dict) or not all((
            signals.get("affair_matches") is True,
            signals.get("page_path") == "/seeyon/collaboration/collaboration.do",
            isinstance(signals.get("identity"), dict),
        )):
            return _observation("done_detail_identity_unavailable", pending_absence_confirmed=True)
        identity = signals["identity"]

    for name in ("summary_id", "process_id"):
        if _text(identity.get(name)) != _text(target.get(name)):
            return _observation("done_identity_mismatch", pending_absence_confirmed=True)
    # These optional identity fields are not exposed by every done page. When
    # exposed, they must not contradict the frozen target.
    for name in _STABLE_IDENTITY_FIELDS[2:]:
        actual = _text(identity.get(name))
        if actual and actual != _text(target.get(name)):
            return _observation("done_identity_mismatch", pending_absence_confirmed=True)
    return _observation(
        "verified", confirmed=True, pending_absence_confirmed=True,
        done_identity_confirmed=True,
    )


def _observation(reason: str, *, confirmed: bool = False, **evidence) -> dict:
    return {
        "confirmed": confirmed,
        "method": VERIFICATION_METHOD,
        "reason": reason,
        **evidence,
    }


def _rows(value) -> list[dict] | None:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return None
    rows = value["items"]
    if any(not isinstance(row, dict) or not _text(row.get("affair_id")) for row in rows):
        return None
    return rows


def _consistent_complete_collection(value: dict, rows: list[dict]) -> bool:
    ids = [_text(row.get("affair_id")) for row in rows]
    if len(set(ids)) != len(ids):
        return False
    for name in ("total", "count", "matched_count", "source_count"):
        count = value.get(name)
        if count is not None and (
            not isinstance(count, int) or isinstance(count, bool) or count != len(rows)
        ):
            return False
    return True


def _row_identity(row: dict) -> dict:
    identity = row.get("identity")
    return identity if isinstance(identity, dict) else row


def _text(value) -> str:
    return str(value or "").strip()


def _title_mismatch(target: dict, row: dict) -> bool:
    expected = " ".join(_text(target.get("title")).split())
    actual = " ".join(_text(row.get("title")).split())
    return bool(expected and expected != actual)


_DONE_IDENTITY_SCRIPT = r"""
(expectedAffairId) => {
  const read = (names) => {
    for (const name of names) {
      const element = document.querySelector(`#${name}`)
        || document.querySelector(`[name='${name}']`);
      const value = String(element?.value || window[name] || '').trim();
      if (value) return value;
    }
    return '';
  };
  const pageAffairId = read(['affairId'])
    || new URLSearchParams(location.search).get('affairId') || '';
  return {
    affair_matches: String(pageAffairId) === String(expectedAffairId),
    page_path: location.pathname,
    identity: {
      summary_id: read(['summaryId']),
      process_id: read(['processId']),
      template_id: read(['templeteId', 'templateId']),
      form_app_id: read(['formAppId']),
      form_record_id: read(['formRecordid', 'formRecordId']),
    },
  };
}
"""
