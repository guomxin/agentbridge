from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4


class CentralProfileInUseError(RuntimeError):
    pass


class CentralProfileUnavailableError(RuntimeError):
    pass


class CentralBrowserWorker:
    def __init__(
        self,
        *,
        profile_path: Path | str,
        allowed_origins: set[str],
        headless: bool = True,
        executable_path: str | None = None,
        playwright_starter: Callable[[], Any] | None = None,
    ) -> None:
        self.profile_path = Path(profile_path)
        self.allowed_origins = {_normalize_origin(origin) for origin in allowed_origins}
        self.headless = headless
        self.executable_path = executable_path
        self._playwright_starter = playwright_starter or _start_playwright
        self._controller = None
        self._context = None
        self._http_state: dict[str, Any] = {}
        self._lease = _ProfileLease(self.profile_path / ".agentbridge-browser-lease.json")

    def __enter__(self) -> CentralBrowserWorker:
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def start(self) -> CentralBrowserWorker:
        if self._context is not None:
            return self
        try:
            self.profile_path.mkdir(parents=True, exist_ok=True)
            self.profile_path.chmod(0o700)
            self._lease.acquire()
        except OSError as exc:
            raise CentralProfileUnavailableError(
                "central browser profile is not writable or cannot be restricted to the service account"
            ) from exc
        try:
            self._controller = self._playwright_starter()
            launch_options: dict[str, Any] = {"headless": self.headless}
            if self.executable_path:
                launch_options["executable_path"] = self.executable_path
            self._context = self._controller.chromium.launch_persistent_context(
                str(self.profile_path),
                **launch_options,
            )
        except Exception:
            if self._controller is not None:
                self._controller.stop()
            self._controller = None
            self._lease.release()
            raise
        return self

    def close(self) -> None:
        context, self._context = self._context, None
        controller, self._controller = self._controller, None
        try:
            if context is not None:
                context.close()
        finally:
            try:
                if controller is not None:
                    controller.stop()
            finally:
                self._lease.release()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_seconds: float = 30,
    ) -> dict:
        self._require_started()
        self._validate_url(url)
        started_at = time.monotonic()
        options: dict[str, Any] = {
            "method": method.upper(),
            "headers": headers or {},
            "timeout": max(timeout_seconds, 0.1) * 1000,
            "max_redirects": 0,
        }
        if body is not None:
            options["data"] = body
        response = self._context.request.fetch(url, **options)
        try:
            self._validate_url(response.url)
            response_headers = response.headers
            if callable(response_headers):
                response_headers = response_headers()
            content_type = str((response_headers or {}).get("content-type") or "")
            text = response.text()
            payload = None
            if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
                try:
                    payload = json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    payload = None
            return {
                "status": response.status,
                "url": response.url,
                "content_type": content_type,
                "json": payload,
                "text": text,
                "elapsed_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            }
        finally:
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                dispose()

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_seconds: float = 30,
    ) -> dict:
        self._require_started()
        self._validate_url(url)
        started_at = time.monotonic()
        options: dict[str, Any] = {
            "method": method.upper(),
            "headers": headers or {},
            "timeout": max(timeout_seconds, 0.1) * 1000,
            "max_redirects": 0,
        }
        if body is not None:
            options["data"] = body
        response = self._context.request.fetch(url, **options)
        try:
            self._validate_url(response.url)
            response_headers = response.headers
            if callable(response_headers):
                response_headers = response_headers()
            normalized_headers = {
                str(name).lower(): str(value)
                for name, value in (response_headers or {}).items()
            }
            return {
                "status": response.status,
                "url": response.url,
                "content_type": normalized_headers.get("content-type", ""),
                "content_length": normalized_headers.get("content-length"),
                "location": normalized_headers.get("location"),
                "body": response.body(),
                "elapsed_ms": max(
                    0,
                    round((time.monotonic() - started_at) * 1000),
                ),
            }
        finally:
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                dispose()
    def goto(self, url: str, *, timeout_seconds: float = 30):
        self._require_started()
        self._validate_url(url)
        page = self.page
        page.goto(url, wait_until="domcontentloaded", timeout=max(timeout_seconds, 0.1) * 1000)
        self._validate_url(page.url)
        return page

    def fork_page(self) -> CentralBrowserPageWorker:
        """Create an isolated page that shares this worker's authenticated context."""
        self._require_started()
        return CentralBrowserPageWorker(self, self._context.new_page())

    def resource_urls(self) -> list[str]:
        self._require_started()
        values = self.page.evaluate(
            "() => performance.getEntriesByType('resource').map((entry) => entry.name)"
        )
        if not isinstance(values, list):
            return []
        urls = []
        seen = set()
        for value in values:
            if not isinstance(value, str) or value in seen:
                continue
            try:
                self._validate_url(value)
            except ValueError:
                continue
            seen.add(value)
            urls.append(value)
        return urls

    def rendered_snapshot(
        self,
        url: str,
        *,
        settle_ms: int = 1500,
        include_frames: bool = True,
        timeout_seconds: float = 30,
    ) -> dict:
        page = self.goto(url, timeout_seconds=timeout_seconds)
        if settle_ms > 0:
            page.wait_for_timeout(settle_ms)
        snapshot = {
            "url": page.url,
            "title": page.title(),
            "html": page.content(),
            "frames": [],
        }
        if not include_frames:
            return snapshot
        main_frame = getattr(page, "main_frame", None)
        for frame in list(getattr(page, "frames", []) or []):
            if frame is main_frame:
                continue
            frame_url = str(getattr(frame, "url", "") or "")
            if frame_url and frame_url != "about:blank":
                try:
                    self._validate_url(frame_url)
                except ValueError:
                    continue
            try:
                frame_html = frame.content()
            except Exception:
                continue
            if frame_html:
                snapshot["frames"].append({"url": frame_url, "html": frame_html})
        return snapshot

    @property
    def page(self):
        self._require_started()
        if self._context.pages:
            return self._context.pages[0]
        return self._context.new_page()

    @property
    def page_title(self) -> str:
        return self.page.title()

    @property
    def page_url(self) -> str:
        return self.page.url

    def capture_session_state(self) -> dict:
        self._require_started()
        cookies = self._context.cookies()
        if any(not self._cookie_is_allowed(cookie) for cookie in cookies):
            raise ValueError("central browser produced a disallowed cookie")
        return {"cookies": cookies, "http": deepcopy(self._http_state)}

    def restore_session_state(self, state: dict) -> None:
        self._require_started()
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies, list):
            raise ValueError("central browser session state must contain cookies")
        for cookie in cookies:
            if not isinstance(cookie, dict) or not self._cookie_is_allowed(cookie):
                raise ValueError("central browser session state contains a disallowed cookie")
        http_state = state.get("http", {})
        if not isinstance(http_state, dict):
            raise ValueError("central browser session state contains invalid HTTP state")
        self._http_state = deepcopy(http_state)
        if cookies:
            self._context.add_cookies(cookies)

    def clear_session_state(self) -> None:
        self._require_started()
        self._context.clear_cookies()
        self._http_state = {}

    def get_http_state(self) -> dict:
        return deepcopy(self._http_state)

    def set_http_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise TypeError("HTTP session state must be an object")
        self._http_state = deepcopy(state)

    def _require_started(self) -> None:
        if self._context is None:
            raise RuntimeError("central browser worker is not started")

    def _validate_url(self, url: str) -> None:
        origin = _origin_from_url(url)
        if origin not in self.allowed_origins:
            raise ValueError(f"request origin is not allowed: {origin}")

    def _cookie_is_allowed(self, cookie: dict) -> bool:
        cookie_url = cookie.get("url")
        if isinstance(cookie_url, str) and cookie_url:
            try:
                return _origin_from_url(cookie_url) in self.allowed_origins
            except ValueError:
                return False
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            return False
        allowed_hosts = {urlparse(origin).hostname or "" for origin in self.allowed_origins}
        return any(host == domain or host.endswith(f".{domain}") for host in allowed_hosts)


class AttachedCentralBrowserWorker:
    """Attach to a directly launched Chromium instance for session verification."""

    def __init__(
        self,
        *,
        cdp_endpoint: str,
        allowed_origins: set[str],
        playwright_starter: Callable[[], Any] | None = None,
    ) -> None:
        parsed = urlparse(cdp_endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or not parsed.port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CDP endpoint must be an explicit loopback HTTP endpoint")
        self.cdp_endpoint = cdp_endpoint.rstrip("/")
        self.allowed_origins = {
            _normalize_origin(origin) for origin in allowed_origins
        }
        self._playwright_starter = playwright_starter or _start_playwright
        self._controller = None
        self._browser = None
        self._context = None
        self._http_state: dict[str, Any] = {}

    def __enter__(self) -> AttachedCentralBrowserWorker:
        return self.start()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def start(self) -> AttachedCentralBrowserWorker:
        if self._context is not None:
            return self
        try:
            self._controller = self._playwright_starter()
            self._browser = self._controller.chromium.connect_over_cdp(
                self.cdp_endpoint
            )
            contexts = list(self._browser.contexts)
            if len(contexts) != 1:
                raise RuntimeError(
                    "direct Chromium must expose exactly one browser context"
                )
            self._context = contexts[0]
        except Exception:
            if self._controller is not None:
                self._controller.stop()
            self._controller = None
            self._browser = None
            self._context = None
            raise
        return self

    def close(self) -> None:
        browser, self._browser = self._browser, None
        controller, self._controller = self._controller, None
        self._context = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if controller is not None:
                controller.stop()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_seconds: float = 30,
    ) -> dict:
        self._require_started()
        self._validate_url(url)
        started_at = time.monotonic()
        options: dict[str, Any] = {
            "method": method.upper(),
            "headers": headers or {},
            "timeout": max(timeout_seconds, 0.1) * 1000,
            "max_redirects": 0,
        }
        if body is not None:
            options["data"] = body
        response = self._context.request.fetch(url, **options)
        try:
            self._validate_url(response.url)
            response_headers = response.headers
            if callable(response_headers):
                response_headers = response_headers()
            content_type = str((response_headers or {}).get("content-type") or "")
            text_value = response.text()
            payload = None
            if "json" in content_type.lower() or text_value.lstrip().startswith(("{", "[")):
                try:
                    payload = json.loads(text_value)
                except (TypeError, json.JSONDecodeError):
                    payload = None
            return {
                "status": response.status,
                "url": response.url,
                "content_type": content_type,
                "json": payload,
                "text": text_value,
                "elapsed_ms": max(
                    0,
                    round((time.monotonic() - started_at) * 1000),
                ),
            }
        finally:
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                dispose()

    @property
    def page(self):
        self._require_started()
        pages = list(self._context.pages)
        if not pages:
            raise RuntimeError("direct Chromium has no interactive page")
        return pages[0]

    @property
    def page_url(self) -> str:
        return self.page.url

    def capture_session_state(self) -> dict:
        self._require_started()
        cookies = self._context.cookies()
        if any(not self._cookie_is_allowed(cookie) for cookie in cookies):
            raise ValueError("direct Chromium produced a disallowed cookie")
        return {"cookies": cookies, "http": deepcopy(self._http_state)}

    def restore_session_state(self, state: dict) -> None:
        self._require_started()
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if not isinstance(cookies, list):
            raise ValueError("direct Chromium session state must contain cookies")
        for cookie in cookies:
            if not isinstance(cookie, dict) or not self._cookie_is_allowed(cookie):
                raise ValueError("direct Chromium session state contains a disallowed cookie")
        http_state = state.get("http", {})
        if not isinstance(http_state, dict):
            raise ValueError("direct Chromium session state contains invalid HTTP state")
        self._http_state = deepcopy(http_state)
        if cookies:
            self._context.add_cookies(cookies)

    def clear_session_state(self) -> None:
        self._require_started()
        self._context.clear_cookies()
        self._http_state = {}

    def get_http_state(self) -> dict:
        return deepcopy(self._http_state)

    def set_http_state(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise TypeError("HTTP session state must be an object")
        self._http_state = deepcopy(state)

    def _require_started(self) -> None:
        if self._context is None:
            raise RuntimeError("attached central browser worker is not started")

    def _validate_url(self, url: str) -> None:
        origin = _origin_from_url(url)
        if origin not in self.allowed_origins:
            raise ValueError(f"request origin is not allowed: {origin}")

    def _cookie_is_allowed(self, cookie: dict) -> bool:
        cookie_url = cookie.get("url")
        if isinstance(cookie_url, str) and cookie_url:
            try:
                return _origin_from_url(cookie_url) in self.allowed_origins
            except ValueError:
                return False
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            return False
        allowed_hosts = {
            urlparse(origin).hostname or "" for origin in self.allowed_origins
        }
        return any(
            host == domain or host.endswith(f".{domain}")
            for host in allowed_hosts
        )


class CentralBrowserPageWorker:
    """A worker-shaped view bound to one page in a central browser context."""

    def __init__(self, owner: CentralBrowserWorker, page: Any) -> None:
        self._owner = owner
        self._page = page
        self._closed = False

    def __enter__(self) -> CentralBrowserPageWorker:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._page, "close", None)
        if callable(close):
            close()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout_seconds: float = 30,
    ) -> dict:
        return self._owner.request(
            method,
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
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
        return self._owner.request_bytes(
            method,
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )
    def goto(self, url: str, *, timeout_seconds: float = 30):
        self._owner._require_started()
        self._owner._validate_url(url)
        self._page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=max(timeout_seconds, 0.1) * 1000,
        )
        self._owner._validate_url(self._page.url)
        return self._page

    def resource_urls(self) -> list[str]:
        self._owner._require_started()
        values = self._page.evaluate(
            "() => performance.getEntriesByType('resource').map((entry) => entry.name)"
        )
        if not isinstance(values, list):
            return []
        urls = []
        seen = set()
        for value in values:
            if not isinstance(value, str) or value in seen:
                continue
            try:
                self._owner._validate_url(value)
            except ValueError:
                continue
            seen.add(value)
            urls.append(value)
        return urls

    def rendered_snapshot(
        self,
        url: str,
        *,
        settle_ms: int = 1500,
        include_frames: bool = True,
        timeout_seconds: float = 30,
    ) -> dict:
        page = self.goto(url, timeout_seconds=timeout_seconds)
        if settle_ms > 0:
            page.wait_for_timeout(settle_ms)
        snapshot = {
            "url": page.url,
            "title": page.title(),
            "html": page.content(),
            "frames": [],
        }
        if not include_frames:
            return snapshot
        main_frame = getattr(page, "main_frame", None)
        for frame in list(getattr(page, "frames", []) or []):
            if frame is main_frame:
                continue
            frame_url = str(getattr(frame, "url", "") or "")
            if frame_url and frame_url != "about:blank":
                try:
                    self._owner._validate_url(frame_url)
                except ValueError:
                    continue
            try:
                frame_html = frame.content()
            except Exception:
                continue
            if frame_html:
                snapshot["frames"].append({"url": frame_url, "html": frame_html})
        return snapshot

    @property
    def page(self):
        self._owner._require_started()
        return self._page

    @property
    def page_title(self) -> str:
        return self.page.title()

    @property
    def page_url(self) -> str:
        return self.page.url


def _start_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("browser URL must include an http(s) origin")
    if parsed.username or parsed.password:
        raise ValueError("browser URL must not contain credentials")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _normalize_origin(origin: str) -> str:
    parsed = urlparse(origin)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(f"allowed origin must not include a path: {origin}")
    return _origin_from_url(origin)


class _ProfileLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lease_id = str(uuid4())
        self.acquired = False

    def acquire(self) -> None:
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "lease_id": self.lease_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=True,
        ).encode("utf-8")
        for _attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                owner = self._read_owner()
                owner_pid = owner.get("pid") if isinstance(owner, dict) else None
                if isinstance(owner_pid, int) and not _pid_is_running(owner_pid):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                owner_text = str(owner_pid) if owner_pid is not None else "unknown"
                raise CentralProfileInUseError(
                    f"central browser profile is already leased by process {owner_text}"
                )
            else:
                try:
                    os.write(descriptor, payload)
                finally:
                    os.close(descriptor)
                self.acquired = True
                return
        raise CentralProfileInUseError("central browser profile lease could not be acquired")

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        owner = self._read_owner()
        if isinstance(owner, dict) and owner.get("lease_id") != self.lease_id:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _read_owner(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
