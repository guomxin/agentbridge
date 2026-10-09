from __future__ import annotations

from copy import deepcopy
from http.cookiejar import Cookie, CookieJar
import json
import time
import zlib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener


class CentralHttpWorker:
    """Origin-restricted HTTP worker with encrypted caller-owned session state."""

    def __init__(self, *, allowed_origins: set[str]) -> None:
        self.allowed_origins = {_normalize_origin(origin) for origin in allowed_origins}
        self._cookie_jar = CookieJar()
        self._open = build_opener(
            HTTPCookieProcessor(self._cookie_jar),
            _RejectRedirectHandler(),
        ).open
        self._state: dict[str, Any] = {"cookies": [], "http": {}}

    def __enter__(self) -> CentralHttpWorker:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_seconds: float = 30,
        max_response_bytes: int | None = None,
        deadline: float | None = None,
        cancellation=None,
    ) -> dict:
        return self._request(
            method,
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
            raw=False,
            max_response_bytes=max_response_bytes,
            deadline=deadline,
            cancellation=cancellation,
        )

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_seconds: float = 30,
    ) -> dict:
        return self._request(
            method,
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
            raw=True,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        body: Any,
        timeout_seconds: float,
        raw: bool,
        max_response_bytes: int | None = None,
        deadline: float | None = None,
        cancellation=None,
    ) -> dict:
        self._validate_url(url)
        request_headers = dict(headers or {})
        encoded_body = None
        if isinstance(body, bytes):
            encoded_body = body
        elif isinstance(body, str):
            encoded_body = body.encode("utf-8")
        elif body is not None:
            encoded_body = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json;charset=utf-8")
        request_headers.setdefault("Accept", "application/json")
        started_at = time.monotonic()
        request = Request(
            url,
            data=encoded_body,
            headers=request_headers,
            method=method.upper(),
        )
        if max_response_bytes is not None:
            request_headers.setdefault("Accept-Encoding", "identity")
            request.add_header("Accept-Encoding", "identity")
            from agentbridge.core.capability_runtime import CapabilityRejected
            def reject(code):
                raise CapabilityRejected(code, "HTTP 读取超出预算、已中断或响应格式无效。")
            if cancellation is not None and cancellation.is_set():
                reject("INTERRUPTED")
            if deadline is not None and time.monotonic() >= deadline:
                reject("BUDGET_EXCEEDED")
        effective_deadline = min(deadline or float("inf"), started_at + timeout_seconds)
        def read_content(response):
            if max_response_bytes is None:
                return response.read()
            from agentbridge.core.capability_runtime import CapabilityRejected
            def reject(code):
                raise CapabilityRejected(code, "HTTP 读取超出预算、已中断或响应格式无效。")
            content = bytearray()
            while True:
                if cancellation is not None and cancellation.is_set():
                    reject("INTERRUPTED")
                if time.monotonic() >= effective_deadline:
                    reject("BUDGET_EXCEEDED")
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(max(0.001, effective_deadline - time.monotonic()))
                # read1 avoids waiting to fill a large buffer on slow streams.
                reader = getattr(response, "read1", response.read)
                chunk = reader(min(65536, max_response_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > max_response_bytes:
                    reject("RESULT_INCOMPLETE")
            encoding = str(response.headers.get("Content-Encoding") or "identity").lower()
            if encoding == "identity":
                return bytes(content)
            if encoding not in {"gzip", "deflate"}:
                reject("DATA_CONTRACT_CHANGED")
            try:
                decoder = zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS)
                decoded = decoder.decompress(bytes(content), max_response_bytes + 1)
                if len(decoded) > max_response_bytes or decoder.unconsumed_tail:
                    reject("RESULT_INCOMPLETE")
                if not decoder.eof or decoder.unused_data:
                    reject("DATA_CONTRACT_CHANGED")
                return decoded
            except zlib.error:
                reject("DATA_CONTRACT_CHANGED")
        try:
            response = self._open(request, timeout=max(timeout_seconds, 0.1))
        except HTTPError as exc:
            with exc:
                return self._response(
                    status=exc.code,
                    url=exc.geturl(),
                    headers=exc.headers,
                    content=read_content(exc),
                    started_at=started_at,
                    raw=raw,
                )
        except (TimeoutError, URLError) as exc:
            raise ConnectionError(f"downstream HTTP request failed: {exc}") from exc
        with response:
            return self._response(
                status=response.status,
                url=response.geturl(),
                headers=response.headers,
                content=read_content(response),
                started_at=started_at,
                raw=raw,
            )

    def capture_session_state(self) -> dict:
        cookies = [self._serialize_cookie(cookie) for cookie in self._cookie_jar]
        if any(not self._cookie_is_allowed(cookie) for cookie in cookies):
            raise ValueError("central HTTP worker produced a disallowed cookie")
        return {"cookies": cookies, "http": deepcopy(self._state["http"])}

    def restore_session_state(self, state: dict) -> None:
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError("central HTTP session state is invalid")
        http_state = state.get("http")
        if not isinstance(http_state, dict):
            raise ValueError("central HTTP session state must contain HTTP state")
        self._cookie_jar.clear()
        for cookie in state["cookies"]:
            if not isinstance(cookie, dict) or not self._cookie_is_allowed(cookie):
                raise ValueError("central HTTP session state contains a disallowed cookie")
            self._cookie_jar.set_cookie(self._deserialize_cookie(cookie))
        self._state = {"cookies": [], "http": deepcopy(http_state)}

    def clear_session_state(self) -> None:
        self._cookie_jar.clear()
        self._state = {"cookies": [], "http": {}}

    def get_http_state(self) -> dict:
        return deepcopy(self._state["http"])

    def set_http_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise TypeError("HTTP session state must be an object")
        self._state = {"cookies": [], "http": deepcopy(state)}

    def _response(
        self,
        *,
        status: int,
        url: str,
        headers,
        content: bytes,
        started_at: float,
        raw: bool,
    ) -> dict:
        self._validate_url(url)
        content_type = str(headers.get("Content-Type") or "")
        location = str(headers.get("Location") or "") or None
        if raw:
            return {
                "status": int(status),
                "url": url,
                "content_type": content_type,
                "content_length": headers.get("Content-Length"),
                "location": location,
                "body": bytes(content),
                "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            }
        text = content.decode("utf-8", errors="replace")
        payload = None
        if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
        return {
            "status": int(status),
            "url": url,
            "content_type": content_type,
            "location": location,
            "json": payload,
            "text": text,
            "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
        }

    @staticmethod
    def _serialize_cookie(cookie: Cookie) -> dict:
        return {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": bool(cookie.secure),
            "expires": cookie.expires,
            "discard": bool(cookie.discard),
            "rest": dict(cookie._rest),
        }

    @staticmethod
    def _deserialize_cookie(value: dict) -> Cookie:
        domain = str(value.get("domain") or "")
        path = str(value.get("path") or "/")
        return Cookie(
            version=0,
            name=str(value.get("name") or ""),
            value=str(value.get("value") or ""),
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=bool(domain),
            domain_initial_dot=domain.startswith("."),
            path=path,
            path_specified=True,
            secure=bool(value.get("secure")),
            expires=value.get("expires"),
            discard=bool(value.get("discard", value.get("expires") is None)),
            comment=None,
            comment_url=None,
            rest=dict(value.get("rest") or {}),
            rfc2109=False,
        )

    def _cookie_is_allowed(self, cookie: dict) -> bool:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            return False
        allowed_hosts = {
            urlparse(origin).hostname or "" for origin in self.allowed_origins
        }
        return any(host == domain or host.endswith(f".{domain}") for host in allowed_hosts)

    def _validate_url(self, url: str) -> None:
        origin = _origin_from_url(url)
        if origin not in self.allowed_origins:
            raise ValueError(f"request origin is not allowed: {origin}")


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, _request, _file_pointer, _code, _message, _headers, _url):
        return None

def _normalize_origin(value: str) -> str:
    return _origin_from_url(value)


def _origin_from_url(value: str) -> str:
    parsed = urlparse(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HTTP worker URL must use http(s) with a host")
    if parsed.username or parsed.password:
        raise ValueError("HTTP worker URL must not contain credentials")
    default_port = 80 if parsed.scheme == "http" else 443
    port = parsed.port or default_port
    netloc = parsed.hostname.lower()
    if port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{parsed.scheme.lower()}://{netloc}"
