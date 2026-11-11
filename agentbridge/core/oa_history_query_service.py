"""Bounded, read-only OA history scans owned by one authenticated session.

Callers hold the existing session/profile lock across this service invocation.
Only a store-issued cursor may continue work; query filters never come from a
continuation request. Source enumeration and live field verification are separate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import time

from agentbridge.adapters.base import AdapterLoginRequired, AdapterSessionCheckUnavailable
from agentbridge.browser.read_budget import ReadBudgetExceeded, read_deadline_scope
from agentbridge.core.capability import CapabilitySpec
from agentbridge.core.oa_history_queries import OaHistoryQueryError, result_page
from agentbridge.core.oa_query_temporal import normalize_oa_query_temporal, oa_query_temporal_schema


QUERY_CAPABILITY = "oa.workflow.history.query"
NEXT_CAPABILITY = "oa.workflow.history.next"
PUBLIC_FIELDS = ("affair_id", "title", "category", "initiated_at", "processed_at", "workflow_state", "workflow_state_text",
                 "current_handlers", "current_nodes", "observed_at", "unavailable_fields")


def history_selection_schema():
    return {"type": "object", "additionalProperties": False,
        "properties": {"kind": {"type": "string", "enum": ["latest"]},
            "count": {"type": "integer", "minimum": 1, "maximum": 50},
            "workflow_state": {"type": "string", "enum": ["all", "in_progress", "ended"]}},
        "required": ["kind", "count", "workflow_state"]}


def _selection(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "count", "workflow_state"}
            or value.get("kind") != "latest" or type(value.get("count")) is not int
            or not 1 <= value["count"] <= 50
            or not isinstance(value.get("workflow_state"), str)
            or value["workflow_state"] not in {"all", "in_progress", "ended"}):
        raise OaHistoryQueryError("OA_HISTORY_QUERY_INVALID", "最新事项选择条件不合法。")
    return deepcopy(value)


def history_query_capability_specs() -> list[CapabilitySpec]:
    base = dict(version="0.1.0", output_schema={"type": "object"}, effect="read",
                adapter="seeyon-central", workflow="history-query-v1")
    return [
        CapabilitySpec(name=QUERY_CAPABILITY,
            description="Start a bounded OA history query with frozen dates; optional latest selection orders all matching source rows by dateBasis and verifies only required candidates.",
            input_schema={"type": "object", "additionalProperties": False,
                "properties": {"collection": {"type": "string", "enum": ["done", "sent"]},
                    "temporal": oa_query_temporal_schema(), "keyword": {"type": "string", "maxLength": 200},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "selection": history_selection_schema()},
                "required": ["collection", "temporal"]}, **base),
        CapabilitySpec(name=NEXT_CAPABILITY,
            description="Continue only the original owned OA history query through its opaque cursor.",
            input_schema={"type": "object", "additionalProperties": False,
                "properties": {"cursor": {"type": "string", "minLength": 1, "maxLength": 128}},
                "required": ["cursor"]}, **base),
    ]


@dataclass(frozen=True)
class QueryLimits:
    call_rows: int = 1000
    call_details: int = 5
    call_seconds: float = 20
    total_rows: int = 10000
    total_details: int = 500
    total_seconds: float = 300


class _Pause(Exception):
    pass


class _Partial(Exception):
    pass


class OaHistoryQueryService:
    def __init__(self, store, *, read_page=None, read_nodes=None, monotonic=time.monotonic,
                 limits: QueryLimits | None = None, target_ref_factory=None):
        self.store, self.read_page, self.read_nodes = store, read_page, read_nodes
        self.monotonic, self.limits = monotonic, limits or QueryLimits()
        self.target_ref_factory = target_ref_factory

    def query(self, *, user_subject, session_id, principal_ref, adapter, worker,
              collection, temporal, accepted_at, keyword=None, limit=50, selection=None):
        if collection not in {"done", "sent"} or type(limit) is not int or not 1 <= limit <= 50:
            raise OaHistoryQueryError("OA_HISTORY_QUERY_INVALID", "查询集合或结果分页大小不合法。")
        if keyword is not None and (not isinstance(keyword, str) or len(keyword) > 200):
            raise OaHistoryQueryError("OA_HISTORY_QUERY_INVALID", "查询关键字不合法。")
        normalized = normalize_oa_query_temporal(temporal, accepted_at=accepted_at,
            default_date_basis="processed_at" if collection == "done" else "initiated_at",
            supported_date_bases=("processed_at", "initiated_at") if collection == "done" else ("initiated_at",))
        query = {"collection": collection, "temporal": normalized, "keyword": (keyword or "").strip(), "limit": limit}
        data = {"rows": [], "seen_ids": [], "next_page": 1, "metadata": None,
            "first_anchor": None, "last_anchor": None, "scan_complete": False,
            "scanned_count": 0, "details_index": 0, "details_checked": 0,
            "details_failed": 0, "timeouts": 0, "completion_reason": None}
        if selection is not None:
            query["selection"] = _selection(selection)
            data["selection"] = {"selected_indices": [], "evaluated_indices": [], "complete": False}
        binding = dict(user_subject=user_subject, session_id=session_id, principal_ref=principal_ref)
        created = self.store.create(**binding, query=query, state_data=data)
        return self.next(cursor=created["cursor"], adapter=adapter, worker=worker, **binding)

    def next(self, *, cursor, user_subject, session_id, principal_ref, adapter, worker):
        binding = dict(user_subject=user_subject, session_id=session_id, principal_ref=principal_ref)
        resolved = self.store.resolve(cursor, **binding)
        if resolved["cached_response"] is not None:
            return resolved["cached_response"]
        record = resolved["record"]
        data = deepcopy(record["state_data"])
        if resolved["stage"] == "results":
            response, offset = self._results(record, data, resolved["delivery_offset"])
            return self.store.advance(cursor, **binding, expected_version=record["version"],
                state=record["state"], state_data=data, response=response,
                next_stage="results" if offset is not None else None, next_offset=offset or 0)

        started = self.monotonic()
        budget = {"source": 0, "details": 0,
            "deadline": started + min(self.limits.call_seconds,
                max(0, self.limits.total_seconds - record["execution_seconds"]))}
        state = record["state"]
        try:
            with read_deadline_scope(budget["deadline"]):
                if state == "scan":
                    self._scan(record, data, budget, adapter, worker)
                    state = "details"
                if state == "details":
                    if "selection" in record["query"]:
                        self._selection_details(record, data, budget, adapter, worker)
                    else:
                        self._details(record, data, budget, adapter, worker)
                    state = "complete" if not data["details_failed"] else "partial"
                    data["completion_reason"] = "complete" if state == "complete" else "detail_fields_unavailable"
        except _Pause:
            state = "details" if data["scan_complete"] else "scan"
        except _Partial as exc:
            state = "partial"
            data["completion_reason"] = str(exc)
        except (ReadBudgetExceeded, TimeoutError):
            data["timeouts"] += 1
            state = "details" if data["scan_complete"] else "scan"
            if data["timeouts"] >= 3:
                state, data["completion_reason"] = "partial", "source_timeout"

        elapsed = max(0, self.monotonic() - started)
        if record["execution_seconds"] + elapsed >= self.limits.total_seconds:
            state, data["completion_reason"] = "partial", "execution_time_budget"
        if state in {"complete", "partial"}:
            # Remaining rows are retained, explicitly unverified, never omitted.
            if "selection" not in record["query"]:
                for row in data["rows"][data["details_index"]:]:
                    if row.get("current_nodes") is None:
                        _unavailable(row, "current_nodes")
            response, offset = self._results({**record, "state": state}, data, 0)
            next_stage, next_offset = ("results", offset) if offset is not None else (None, 0)
        else:
            response = {**self._envelope({**record, "state": state}, data),
                "items": [], "has_more": True, "next_cursor": None}
            next_stage, next_offset = state, 0
        return self.store.advance(cursor, **binding, expected_version=record["version"], state=state,
            state_data=data, response=response, next_stage=next_stage, next_offset=next_offset,
            scanned_delta=budget["source"], detail_delta=budget["details"], elapsed_seconds=elapsed)

    def _time(self, record, budget):
        if self.monotonic() >= budget["deadline"]:
            if record["execution_seconds"] >= self.limits.total_seconds:
                raise _Partial("execution_time_budget")
            raise _Pause()

    def _page(self, record, data, budget, adapter, worker, page_number):
        self._time(record, budget)
        meta = data["metadata"]
        expected = min(50, max(0, meta["total"] - (page_number - 1) * 50)) if meta else 50
        if record["scanned_count"] + budget["source"] + expected > self.limits.total_rows:
            raise _Partial("source_row_budget")
        if budget["source"] + expected > self.limits.call_rows:
            raise _Pause()
        try:
            reader = self.read_page
            if reader is None:
                from agentbridge.adapters.seeyon_history_report import read_report_page
                reader = read_report_page
            page = reader(adapter, worker, collection=record["query"]["collection"],
                page_number=page_number, temporal=record["query"]["temporal"])
        except (AdapterLoginRequired, AdapterSessionCheckUnavailable, ReadBudgetExceeded, TimeoutError):
            raise
        except Exception as exc:
            # Browser/JS deadlines use their own exception classes. An elapsed
            # per-call budget pauses the saved page, rather than terminating a
            # query which can safely continue in the next bounded invocation.
            if self.monotonic() >= budget["deadline"]:
                raise _Pause() from exc
            raise _Partial("source_read_failed") from exc
        rows = page.get("items") if isinstance(page, dict) else None
        if not isinstance(rows, list) or len(rows) > 50:
            raise _Partial("source_metadata_invalid")
        budget["source"] += len(rows)
        fields = ("total", "pages", "page", "size")
        if any(type(page.get(key)) is not int or page[key] < 0 for key in fields):
            raise _Partial("source_metadata_invalid")
        total, pages = page["total"], page["pages"]
        if (page["page"] != page_number or page["size"] != 50
                or (total and pages != (total + 49) // 50) or (not total and pages not in {0, 1})
                or len(rows) != min(50, max(0, total - (page_number - 1) * 50))
                or any(not isinstance(row, dict) or not row.get("affair_id") or not row.get("title") for row in rows)):
            raise _Partial("source_metadata_invalid")
        current = {"total": total, "pages": pages, "size": 50}
        if meta is not None and meta != current:
            raise _Partial("source_changed")
        data["metadata"] = current
        data["timeouts"] = 0
        return rows

    def _anchors(self, record, data, budget, adapter, worker):
        anchors = {anchor["page"]: anchor for anchor in (data["first_anchor"], data["last_anchor"]) if anchor}
        for number, anchor in anchors.items():
            rows = self._page(record, data, budget, adapter, worker, number)
            if _signature(rows) != anchor["signature"]:
                raise _Partial("source_changed")

    def _scan(self, record, data, budget, adapter, worker):
        verified_on_entry = data["next_page"] > 1
        added_page = False
        if data["next_page"] > 1:
            self._anchors(record, data, budget, adapter, worker)
        seen = set(data["seen_ids"])
        while data["metadata"] is None or data["next_page"] <= data["metadata"]["pages"]:
            page_number = data["next_page"]
            rows = self._page(record, data, budget, adapter, worker, page_number)
            ids = [str(row["affair_id"]) for row in rows]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise _Partial("source_duplicate_ids")
            matching = []
            for row in rows:
                if self._matches(record["query"], row):
                    matching.append(deepcopy(row))
            seen.update(ids)
            data["seen_ids"] = list(seen)
            data["scanned_count"] += len(rows)
            data["rows"].extend(matching)
            anchor = {"page": page_number, "signature": _signature(rows)}
            data["first_anchor"] = data["first_anchor"] or anchor
            data["last_anchor"] = anchor
            data["next_page"] += 1
            added_page = True
        if added_page or not verified_on_entry:
            self._anchors(record, data, budget, adapter, worker)
        if data["scanned_count"] != data["metadata"]["total"]:
            raise _Partial("source_count_mismatch")
        data["scan_complete"] = True

    @staticmethod
    def _matches(query, row):
        temporal = query["temporal"]
        bounds = temporal["absoluteRange"]
        if bounds["start"] or bounds["end"]:
            if "selection" in query and _field_unavailable(row, temporal["dateBasis"]):
                raise _Partial("selection_date_unverified")
            value = row.get(temporal["dateBasis"])
            try:
                day = date.fromisoformat(str(value or "")[:10]).isoformat()
            except ValueError as exc:
                raise _Partial("source_date_unverified") from exc
            if (bounds["start"] and day < bounds["start"]) or (bounds["end"] and day > bounds["end"]):
                # Done/initiated_at must scan the entire source; the other two
                # date contracts are also checked rather than trusting filters.
                if query["collection"] == "done" and temporal["dateBasis"] == "initiated_at":
                    return False
                raise _Partial("source_date_filter_mismatch")
        needle = query["keyword"].casefold()
        keyword_fields = row.get("_keyword_fields") or {key: row.get(key) for key in PUBLIC_FIELDS}
        return not needle or needle in " ".join(str(value or "") for value in keyword_fields.values()).casefold()

    def _details(self, record, data, budget, adapter, worker):
        while data["details_index"] < len(data["rows"]):
            self._time(record, budget)
            row = data["rows"][data["details_index"]]
            if row.get("current_nodes") is None:
                if record["detail_count"] + budget["details"] >= self.limits.total_details:
                    raise _Partial("detail_item_budget")
                if budget["details"] >= self.limits.call_details:
                    raise _Pause()
                budget["details"] += 1
                try:
                    reader = self.read_nodes
                    if reader is None:
                        from agentbridge.adapters.seeyon_history_report import read_current_nodes
                        reader = read_current_nodes
                    detail = reader(adapter, worker, collection=record["query"]["collection"], row=deepcopy(row))
                    nodes = detail.get("current_nodes") if isinstance(detail, dict) else None
                    if not isinstance(nodes, list) or any(not isinstance(node, dict) or
                            not isinstance(node.get("name"), str) or not isinstance(node.get("handlers"), list) for node in nodes):
                        raise ValueError("Unverified workflow nodes")
                    for key in ("current_nodes", "current_handlers", "workflow_state", "workflow_state_text", "observed_at"):
                        if key in detail:
                            row[key] = deepcopy(detail[key])
                    row["unavailable_fields"] = deepcopy(detail.get("unavailable_fields", [
                        field for field in row.get("unavailable_fields", []) if field not in detail]))
                except (AdapterLoginRequired, AdapterSessionCheckUnavailable):
                    raise
                except ReadBudgetExceeded as exc:
                    raise _Pause() from exc
                except Exception:
                    if self.monotonic() >= budget["deadline"]:
                        raise _Pause()
                    _unavailable(row, "current_nodes")
            for field in ("initiated_at", "workflow_state", "observed_at"):
                if not row.get(field):
                    _unavailable(row, field)
            data["details_checked"] += 1
            data["details_failed"] += bool(row.get("unavailable_fields"))
            data["details_index"] += 1

    def _selection_details(self, record, data, budget, adapter, worker):
        """Prove a latest prefix without inspecting unrelated older details.

        List order is not an ordering contract. All matched source rows have
        already been enumerated and anchored before this method runs. Unknown
        states may only be excluded after exact detail verification; a missing
        timestamp can therefore block the proof even when a dated row exists.
        """
        selection = record["query"]["selection"]
        progress = data["selection"]
        progress["selected_indices"] = []
        progress["complete"] = False
        wanted = selection["workflow_state"]
        basis = record["query"]["temporal"]["dateBasis"]
        ordered = []
        for index, row in enumerate(data["rows"]):
            self._time(record, budget)
            if not _possibly_selected(row, wanted):
                continue
            stamp = None if _field_unavailable(row, basis) else _selection_time(row.get(basis))
            if stamp is None:
                if _workflow_state(row) == "unknown" and wanted != "all":
                    self._selection_verify(record, data, budget, adapter, worker, index)
                    if not _possibly_selected(row, wanted):
                        continue
                raise _Partial("selection_date_unverified")
            ordered.append((stamp, index))
        ordered.sort(key=lambda item: item[0], reverse=True)
        position = 0
        while position < len(ordered):
            stamp = ordered[position][0]
            group = []
            while position < len(ordered) and ordered[position][0] == stamp:
                index = ordered[position][1]
                row = data["rows"][index]
                self._time(record, budget)
                self._selection_verify(record, data, budget, adapter, worker, index)
                if wanted == "all" or _workflow_state(row) == wanted:
                    if row.get("unavailable_fields"):
                        raise _Partial("selection_detail_unverified")
                    group.append(index)
                    if len(progress["selected_indices"]) + len(group) > selection["count"]:
                        # A verified excess already proves the boundary tie;
                        # older or further equal-time details cannot undo it.
                        raise _Partial("selection_tie")
                position += 1
            selected = progress["selected_indices"]
            selected.extend(group)
            if len(selected) == selection["count"]:
                progress["complete"] = True
                return
        progress["complete"] = True

    def _selection_verify(self, record, data, budget, adapter, worker, index):
        progress = data["selection"]
        if index in progress["evaluated_indices"]:
            return
        row = data["rows"][index]
        unknown = _workflow_state(row) == "unknown"
        if unknown or not _verified_selection_fields(row):
            self._time(record, budget)
            if record["detail_count"] + budget["details"] >= self.limits.total_details:
                raise _Partial("detail_item_budget")
            if budget["details"] >= self.limits.call_details:
                raise _Pause()
            budget["details"] += 1
            try:
                reader = self.read_nodes
                if reader is None:
                    from agentbridge.adapters.seeyon_history_report import read_current_nodes
                    reader = read_current_nodes
                detail = reader(adapter, worker, collection=record["query"]["collection"], row=deepcopy(row))
                if not isinstance(detail, dict) or not _verified_selection_fields(detail):
                    raise ValueError("Unverified workflow state")
                unavailable = detail.get("unavailable_fields", [
                    field for field in row.get("unavailable_fields", []) if field not in detail])
                if not isinstance(unavailable, list) or any(not isinstance(field, str) for field in unavailable):
                    raise ValueError("Unverified workflow fields")
                for key in ("current_nodes", "current_handlers", "workflow_state", "workflow_state_text", "observed_at"):
                    if key in detail:
                        row[key] = deepcopy(detail[key])
                row["unavailable_fields"] = deepcopy(unavailable)
            except (AdapterLoginRequired, AdapterSessionCheckUnavailable):
                raise
            except ReadBudgetExceeded as exc:
                raise _Pause() from exc
            except Exception as exc:
                if self.monotonic() >= budget["deadline"]:
                    raise _Pause() from exc
                _unavailable(row, "current_nodes")
                data["details_checked"] += 1
                data["details_failed"] += 1
                progress["evaluated_indices"].append(index)
                raise _Partial("selection_state_unverified" if unknown else "selection_detail_unverified") from exc
        for field in ("workflow_state", "observed_at"):
            if not row.get(field):
                _unavailable(row, field)
        data["details_checked"] += 1
        data["details_failed"] += bool(row.get("unavailable_fields")) and _possibly_selected(
            row, record["query"]["selection"]["workflow_state"])
        progress["evaluated_indices"].append(index)

    @staticmethod
    def _envelope(record, data):
        selected = record["query"].get("selection")
        details_complete = (data["selection"]["complete"] if selected else data["details_index"] == len(data["rows"])) and not data["details_failed"]
        complete = data["scan_complete"] and details_complete and record["state"] == "complete"
        envelope = {"schema_version": "agentbridge.oa_history_query.v1", "query_id": record["query_id"],
            "expires_at":record.get('expires_at'),
            "collection": record["query"]["collection"], "state": "scanning" if record["state"] == "scan" else record["state"],
            "temporal": record["query"]["temporal"],
            "progress": {"source_total": (data["metadata"] or {}).get("total"),
                "scanned_count": data["scanned_count"], "matched_count": len(data["rows"]),
                "details_checked": data["details_checked"], "details_failed": data["details_failed"]},
            "coverage": {"status": "complete" if complete else "partial",
                "completionReason": data["completion_reason"] or "query_in_progress",
                "scan_complete": data["scan_complete"], "details_complete": details_complete}}
        if selected:
            envelope["coverage"]["details_scope"] = "selection_candidates"
            envelope["selection"] = {**selected, "date_basis": record["query"]["temporal"]["dateBasis"],
                "order": "desc", "scope": "all_matching_history",
                "selected_count": len(data["selection"]["selected_indices"]),
                "selection_complete": data["selection"]["complete"],
                "reason": data["completion_reason"] or "query_in_progress"}
        return envelope

    def _results(self, record, data, offset):
        rows = ([data["rows"][index] for index in data["selection"]["selected_indices"]]
            if "selection" in record["query"] else data["rows"])
        public_rows = [{key: deepcopy(row.get(key)) for key in PUBLIC_FIELDS} for row in rows]
        if self.target_ref_factory:
            for source, public in zip(rows, public_rows):
                reference = self.target_ref_factory(record, source)
                if reference:
                    public['target_ref'] = reference
        response = result_page(public_rows, offset=offset, limit=record["query"]["limit"],
            envelope={**self._envelope(record, data), "delivery": {"offset": offset, "complete": False}})
        next_offset = response.pop("next_offset")
        # Query completeness describes retained facts, not a single delivery page.
        response["delivery"]["complete"] = not response["has_more"]
        return response, next_offset


def _unavailable(row, field):
    row.setdefault("unavailable_fields", [])
    if field not in row["unavailable_fields"]:
        row["unavailable_fields"].append(field)


def _workflow_state(row):
    value = row.get("workflow_state")
    return value if (not _field_unavailable(row, "workflow_state") and isinstance(value, str)
        and value in {"ended", "in_progress"}) else "unknown"


def _field_unavailable(row, field):
    fields = row.get("unavailable_fields", [])
    return not isinstance(fields, list) or field in fields


def _verified_selection_fields(row):
    state = _workflow_state(row)
    nodes, handlers = row.get("current_nodes"), row.get("current_handlers")
    if (state == "unknown" or any(_field_unavailable(row, field)
            for field in ("current_nodes", "current_handlers", "observed_at"))
            or _selection_time(row.get("observed_at")) is None
            or not isinstance(nodes, list) or not isinstance(handlers, list)
            or any(not isinstance(person, str) or not person for person in handlers)
            or (state == "ended" and (nodes or handlers)) or (state == "in_progress" and not nodes)):
        return False
    people = set()
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get("name"), str) or not node["name"]
                or not isinstance(node.get("handlers"), list) or not node["handlers"]
                or any(not isinstance(person, str) or not person for person in node["handlers"])):
            return False
        people.update(node["handlers"])
    return people == set(handlers)


def _possibly_selected(row, wanted):
    return wanted == "all" or _workflow_state(row) in {wanted, "unknown"}


def _selection_time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _signature(rows):
    stable = [{**{key: row.get(key) for key in ("affair_id", "title", "initiated_at", "processed_at")},
        "source_identity": {key: (row.get("_source") or {}).get(key) for key in ("affair_id", "summary_id", "process_id", "case_id")}}
        for row in rows]
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
