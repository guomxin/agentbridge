from __future__ import annotations

from typing import Iterable


def sent_snapshot(adapter, worker) -> dict:
    rows, _page = adapter.load_sent_workflow_rows(worker)
    return {
        "affair_ids": sorted(
            str(item.get("affair_id") or "")
            for item in rows
            if isinstance(item, dict) and str(item.get("affair_id") or "")
        )
    }


def new_sent_candidates(
    adapter,
    worker,
    *,
    baseline_affair_ids: set[str],
    template_id: str,
    form_app_id: str,
    title_markers: Iterable[str],
) -> list[dict]:
    rows, _page = adapter.load_sent_workflow_rows(worker)
    markers = [str(value).strip() for value in title_markers if str(value).strip()]
    return [
        item
        for item in rows
        if isinstance(item, dict)
        and _matches_authorized_identity(
            item,
            baseline_affair_ids=baseline_affair_ids,
            template_id=template_id,
            form_app_id=form_app_id,
            title_markers=markers,
        )
    ]


def _matches_authorized_identity(
    item: dict,
    *,
    baseline_affair_ids: set[str],
    template_id: str,
    form_app_id: str,
    title_markers: list[str],
) -> bool:
    affair_id = str(item.get("affair_id") or "")
    title = str(item.get("title") or "")
    return all(
        (
            bool(affair_id),
            affair_id not in baseline_affair_ids,
            str(item.get("template_id") or "") == str(template_id),
            str(item.get("form_app_id") or "") == str(form_app_id),
            bool(title_markers),
            all(marker in title for marker in title_markers),
        )
    )
