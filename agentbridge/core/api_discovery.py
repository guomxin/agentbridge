from __future__ import annotations

from collections import defaultdict
from typing import Any
from urllib.parse import urlparse


API_RESOURCE_TYPES = {"fetch", "xmlhttprequest", "beacon"}
STATIC_EXTENSIONS = {
    ".css",
    ".gif",
    ".ico",
    ".jpg",
    ".jpeg",
    ".js",
    ".map",
    ".png",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
}


def extract_api_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in _iter_records(snapshot):
        method = (record.get("method") or "GET").upper()
        url = record.get("url") or record.get("name")
        if not url:
            continue
        parsed = urlparse(str(url))
        path = parsed.path or str(url)
        if parsed.query:
            path = f"{path}?{parsed.query}"
        if _is_static_path(path):
            continue

        key = (method, parsed.geturl() if parsed.scheme else path)
        item = grouped.setdefault(
            key,
            {
                "method": method,
                "url": parsed.geturl() if parsed.scheme else str(url),
                "origin": f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "",
                "path": path,
                "count": 0,
                "kinds": set(),
                "statuses": set(),
                "sample_request_body": None,
            },
        )
        item["count"] += 1
        if record.get("kind"):
            item["kinds"].add(record["kind"])
        if record.get("initiatorType"):
            item["kinds"].add(record["initiatorType"])
        if record.get("status") is not None:
            item["statuses"].add(record["status"])
        if item["sample_request_body"] is None and record.get("requestBody") is not None:
            item["sample_request_body"] = record["requestBody"]

    candidates = []
    for item in grouped.values():
        normalized = dict(item)
        normalized["kinds"] = sorted(item["kinds"])
        normalized["statuses"] = sorted(item["statuses"])
        normalized["score"] = _score_candidate(normalized)
        candidates.append(normalized)
    return sorted(candidates, key=lambda item: (-item["score"], item["path"], item["method"]))


def inspect_api_response(replay: dict[str, Any]) -> dict[str, Any]:
    payload = replay.get("json")
    result: dict[str, Any] = {
        "status": replay.get("status"),
        "ok": replay.get("ok"),
        "url": replay.get("url", ""),
        "content_type": replay.get("contentType") or replay.get("content_type") or "",
        "response_type": "json" if payload is not None else "text",
        "json_keys": [],
        "data_keys": [],
        "data_shape": "",
        "item_count": None,
        "sample_fields": [],
    }
    if payload is None:
        result["text_length"] = len(str(replay.get("text") or ""))
        return result

    if isinstance(payload, list):
        result["data_shape"] = "json[]"
        result["item_count"] = len(payload)
        result["sample_fields"] = _sample_fields(payload[0] if payload else None)
        return result

    if not isinstance(payload, dict):
        result["data_shape"] = type(payload).__name__
        return result

    result["json_keys"] = sorted(payload.keys())
    data = payload.get("Data")
    if isinstance(data, dict):
        result["data_keys"] = sorted(data.keys())
        if isinstance(data.get("items"), list):
            items = data["items"]
            result["data_shape"] = "Data.items[]"
            result["item_count"] = len(items)
            result["sample_fields"] = _sample_fields(items[0] if items else None)
        elif isinstance(data.get("rows"), list):
            rows = data["rows"]
            first_row = rows[0] if rows else None
            cells = first_row.get("cells") if isinstance(first_row, dict) else None
            if isinstance(cells, list):
                result["data_shape"] = "Data.rows[].cells[]"
                result["sample_fields"] = _sample_fields(cells[0] if cells else None)
            else:
                result["data_shape"] = "Data.rows[]"
                result["sample_fields"] = _sample_fields(first_row)
            result["item_count"] = len(rows)
        else:
            result["data_shape"] = "Data{}"
    else:
        result["data_shape"] = "json{}"
        result["sample_fields"] = _sample_fields(payload)
    return result


def _sample_fields(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(value.keys())
    return []


def _iter_records(snapshot: dict[str, Any]):
    for record in snapshot.get("records") or []:
        yield record
    for record in snapshot.get("resources") or []:
        if record.get("initiatorType") in API_RESOURCE_TYPES:
            yield {
                "kind": record.get("initiatorType"),
                "method": "GET",
                "url": record.get("name"),
            }


def _is_static_path(path: str) -> bool:
    lower = path.lower().split("?", 1)[0]
    return any(lower.endswith(ext) for ext in STATIC_EXTENSIONS)


def _score_candidate(item: dict[str, Any]) -> int:
    score = 0
    if item["method"] != "GET":
        score += 5
    if "/rest/" in item["path"] or "/api/" in item["path"]:
        score += 4
    if item["sample_request_body"]:
        score += 3
    score += min(item["count"], 3)
    return score
