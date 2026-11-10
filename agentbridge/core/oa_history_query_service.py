"""Bounded, read-only OA history scans owned by one authenticated session.

Callers hold the existing session/profile lock across this service invocation.
Only a store-issued cursor may continue work; query filters never come from a
continuation request. Source enumeration and live field verification are separate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
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


def history_query_capability_specs() -> list[CapabilitySpec]:
    base = dict(version="0.1.0", output_schema={"type": "object"}, effect="read",
                adapter="seeyon-central", workflow="history-query-v1")
    return [
        CapabilitySpec(name=QUERY_CAPABILITY,
            description="Start a bounded complete-history OA query with frozen date intent and verified workflow fields.",
            input_schema={"type": "object", "additionalProperties": False,
                "properties": {"collection": {"type": "string", "enum": ["done", "sent"]},
                    "temporal": oa_query_temporal_schema(), "keyword": {"type": "string", "maxLength": 200},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
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
              collection, temporal, accepted_at, keyword=None, limit=50):
        if collection not in {"done", "sent"} or type(limit) is not int or not 1 <= limit <= 50:
            raise OaHistoryQueryError("OA_HISTORY_QUERY_INVALID", "查询集合或结果分页大小不合法。")
        if keyword is not None and (not isinstance(keyword, str) or len(keyword) > 200):
            raise OaHistoryQueryError("OA_HISTORY_QUERY_INVALID", "查询关键字不合法。")
        normalized = normalize_oa_query_temporal(temporal, accepted_at=accepted_at,
            default_date_basis="processed_at" if collection == "done" else "initiated_at",
            supported_date_bases=("processed_at", "initiated_at") if collection == "done" else ("initiated_at",))
        binding = dict(user_subject=user_subject, session_id=session_id, principal_ref=principal_ref)
        created = self.store.create(**binding,
            query={"collection": collection, "temporal": normalized, "keyword": (keyword or "").strip(), "limit": limit},
            state_data={"rows": [], "seen_ids": [], "next_page": 1, "metadata": None,
                "first_anchor": None, "last_anchor": None, "scan_complete": False,
                "scanned_count": 0, "details_index": 0, "details_checked": 0,
                "details_failed": 0, "timeouts": 0, "completion_reason": None})
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

    @staticmethod
    def _envelope(record, data):
        details_complete = data["details_index"] == len(data["rows"]) and not data["details_failed"]
        complete = data["scan_complete"] and details_complete and record["state"] == "complete"
        return {"schema_version": "agentbridge.oa_history_query.v1", "query_id": record["query_id"],
            "expires_at":record.get('expires_at'),
            "collection": record["query"]["collection"], "state": "scanning" if record["state"] == "scan" else record["state"],
            "temporal": record["query"]["temporal"],
            "progress": {"source_total": (data["metadata"] or {}).get("total"),
                "scanned_count": data["scanned_count"], "matched_count": len(data["rows"]),
                "details_checked": data["details_checked"], "details_failed": data["details_failed"]},
            "coverage": {"status": "complete" if complete else "partial",
                "completionReason": data["completion_reason"] or "query_in_progress",
                "scan_complete": data["scan_complete"], "details_complete": details_complete}}

    def _results(self, record, data, offset):
        public_rows = [{key: deepcopy(row.get(key)) for key in PUBLIC_FIELDS} for row in data["rows"]]
        if self.target_ref_factory:
            for source, public in zip(data['rows'], public_rows):
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


def _signature(rows):
    stable = [{**{key: row.get(key) for key in ("affair_id", "title", "initiated_at", "processed_at")},
        "source_identity": {key: (row.get("_source") or {}).get(key) for key in ("affair_id", "summary_id", "process_id", "case_id")}}
        for row in rows]
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
