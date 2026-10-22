"""Bounded binary transfer using an already verified browser account's cookies.

Playwright APIResponse buffers a complete body before exposing it. Explicitly
bounded GETs therefore use the existing origin-restricted streaming HTTP worker.
Cookies remain in memory, and only the contacted origin's cookie updates are
mapped back to the browser context. Ordinary browser requests are unchanged.
"""
from copy import deepcopy

from agentbridge.browser.http import CentralHttpWorker, _origin_from_url


def bounded_browser_download(worker, method, url, *, headers=None, body=None,
                             timeout_seconds, max_response_bytes, deadline=None):
    if method.upper() != "GET" or body is not None:
        raise ValueError("bounded browser downloads support bodyless GET only")
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("bounded browser download requires a positive byte limit")
    worker._require_started()
    worker._validate_url(url)
    with CentralHttpWorker(allowed_origins={_origin_from_url(url)}) as transfer:
        before = worker.capture_session_state()
        originals = {(item["name"], item["domain"], item.get("path", "/")): item
                     for item in before["cookies"] if transfer._cookie_is_allowed(item)}
        cookies = []
        for item in originals.values():
            value = deepcopy(item)
            if value.get("expires", -1) is None or value.get("expires", -1) <= 0:
                value["expires"], value["discard"] = None, True
            value["rest"] = {**value.get("rest", {}),
                             **({"HttpOnly": None} if value.get("httpOnly") else {}),
                             **({"SameSite": value["sameSite"]} if value.get("sameSite") else {})}
            cookies.append(value)
        transfer.restore_session_state({"cookies": cookies, "http": before.get("http", {})})
        try:
            return transfer.request_bytes("GET", url, headers=headers, timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes, deadline=deadline)
        finally:
            updated = transfer.capture_session_state()["cookies"]
            updates, seen = [], set()
            for item in updated:
                key = (item["name"], item["domain"], item.get("path", "/")); seen.add(key)
                original = originals.get(key, {})
                rest = {str(name).lower(): value for name, value in item.get("rest", {}).items()}
                same_site = rest.get("samesite", original.get("sameSite", "Lax"))
                same_site = {"lax": "Lax", "strict": "Strict", "none": "None"}.get(str(same_site).lower(), "Lax")
                updates.append({"name": item["name"], "value": item["value"], "domain": item["domain"],
                    "path": item.get("path") or "/", "secure": bool(item.get("secure")),
                    "expires": item["expires"] if item.get("expires") is not None else -1,
                    "httpOnly": "httponly" in rest or bool(original.get("httpOnly")), "sameSite": same_site})
            for key, original in originals.items():
                if key not in seen:
                    updates.append({"name": original["name"], "value": "", "domain": original["domain"],
                                    "path": original.get("path") or "/", "expires": 0})
            if updates:
                worker._context.add_cookies(updates)
