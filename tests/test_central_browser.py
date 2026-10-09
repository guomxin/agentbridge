import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentbridge.adapters.seeyon_central import (
    SeeyonCentralAdapter,
    SeeyonLoginRequired,
    SeeyonSessionCheckUnavailable,
)
from agentbridge.browser.central import (
    AttachedCentralBrowserWorker,
    CentralBrowserWorker,
    CentralProfileInUseError,
    CentralProfileUnavailableError,
)


class CentralBrowserTests(unittest.TestCase):
    def test_attached_worker_accepts_only_loopback_cdp_and_captures_cookies(self):
        controller = FakePlaywrightController()
        controller.context.cookie_jar = [
            {
                "name": "yuque_session",
                "value": "secret",
                "domain": "tc-aiot.yuque.com",
                "path": "/",
            }
        ]
        worker = AttachedCentralBrowserWorker(
            cdp_endpoint="http://127.0.0.1:9222",
            allowed_origins={"https://tc-aiot.yuque.com"},
            playwright_starter=lambda: controller,
        )

        with worker:
            state = worker.capture_session_state()

        self.assertEqual(state["cookies"][0]["name"], "yuque_session")
        self.assertEqual(controller.chromium.cdp_endpoints, ["http://127.0.0.1:9222"])
        self.assertTrue(controller.browser.closed)
        self.assertTrue(controller.stopped)
        with self.assertRaisesRegex(ValueError, "loopback"):
            AttachedCentralBrowserWorker(
                cdp_endpoint="http://10.10.50.213:9222",
                allowed_origins={"https://tc-aiot.yuque.com"},
            )

    def test_worker_uses_persistent_profile_and_shared_context_request(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                headless=True,
                playwright_starter=lambda: controller,
            )

            with worker:
                response = worker.request("GET", "http://oa.example.test/rest/templates")

            self.assertEqual(response["status"], 200)
            self.assertEqual(response["json"], {"code": 0, "data": {"templates": []}})
            self.assertIsInstance(response["elapsed_ms"], int)
            self.assertGreaterEqual(response["elapsed_ms"], 0)
            launch = controller.chromium.launches[0]
            self.assertEqual(launch["user_data_dir"], str(Path(tmp) / "profile"))
            self.assertTrue(launch["headless"])
            self.assertEqual(controller.context.request.calls[0]["method"], "GET")
            self.assertEqual(controller.context.request.calls[0]["max_redirects"], 0)
            self.assertTrue(controller.stopped)

    def test_worker_reports_an_unwritable_profile_before_launching_browser(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )
            worker._lease.acquire = lambda: (_ for _ in ()).throw(
                PermissionError("profile denied")
            )

            with self.assertRaisesRegex(CentralProfileUnavailableError, "not writable"):
                worker.start()

            self.assertFalse(controller.chromium.launches)

    def test_worker_hardens_profile_permissions_before_launch(self):
        with TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile"
            profile_path.mkdir()
            controller = FakePlaywrightController()
            worker = CentralBrowserWorker(
                profile_path=profile_path,
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with patch.object(Path, "chmod", autospec=True) as chmod:
                with worker:
                    pass

            chmod.assert_called_once_with(profile_path, 0o700)

    def test_worker_parses_json_body_when_server_uses_text_content_type(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            controller.context.request.response = FakePlainTextJsonResponse()
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with worker:
                response = worker.request("GET", "http://oa.example.test/rest/templates")

            self.assertEqual(response["content_type"], "text/plain; charset=utf-8")
            self.assertEqual(response["json"], {"Data": {"rows": []}})

    def test_worker_returns_binary_body_without_decoding_it_as_text(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            controller.context.request.response = FakePdfResponse()
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with worker:
                response = worker.request_bytes(
                    "GET",
                    "http://oa.example.test/seeyon/fileDownload.do",
                )

            self.assertEqual(response["content_type"], "application/pdf")
            self.assertEqual(response["body"], b"%PDF-1.7\ncertificate")
            self.assertEqual(
                controller.context.request.calls[0]["max_redirects"],
                0,
            )
    def test_worker_captures_and_restores_allowed_session_cookies(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            controller.context.cookie_jar = [
                {
                    "name": "JSESSIONID",
                    "value": "secret",
                    "domain": "oa.example.test",
                    "path": "/seeyon",
                }
            ]
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with worker:
                state = worker.capture_session_state()
                controller.context.cookie_jar = []
                worker.restore_session_state(state)

            self.assertEqual(controller.context.added_cookies[0][0]["name"], "JSESSIONID")
            self.assertEqual(controller.context.cookie_requests, [None])

    def test_worker_rejects_request_outside_allowed_origin(self):
        with TemporaryDirectory() as tmp:
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=FakePlaywrightController,
            )

            with worker:
                with self.assertRaisesRegex(ValueError, "origin is not allowed"):
                    worker.request("GET", "https://other.example.test/data")

    def test_worker_returns_only_unique_allowed_resource_urls(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            controller.context.pages[0].resources = [
                "http://oa.example.test/seeyon/section?a=1",
                "https://other.example.test/tracker",
                "http://oa.example.test/seeyon/section?a=1",
            ]
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with worker:
                urls = worker.resource_urls()

            self.assertEqual(urls, ["http://oa.example.test/seeyon/section?a=1"])

    def test_worker_rendered_snapshot_includes_only_same_origin_frames(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            page = controller.context.pages[0]
            page.html = "<html><body>top</body></html>"
            page.frames = [
                page,
                FakeFrame("http://oa.example.test/seeyon/cap4", "<div>business form</div>"),
                FakeFrame("about:blank", "<div>inherited frame</div>"),
                FakeFrame("https://other.example.test/embed", "<div>external</div>"),
            ]
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with worker:
                snapshot = worker.rendered_snapshot(
                    "http://oa.example.test/seeyon/detail",
                    settle_ms=25,
                )

            self.assertEqual(snapshot["html"], "<html><body>top</body></html>")
            self.assertEqual(len(snapshot["frames"]), 2)
            self.assertEqual(page.waits, [25])
            self.assertNotIn("external", str(snapshot))

    def test_forked_page_preserves_primary_page_and_closes_independently(self):
        with TemporaryDirectory() as tmp:
            controller = FakePlaywrightController()
            worker = CentralBrowserWorker(
                profile_path=Path(tmp) / "profile",
                allowed_origins={"http://oa.example.test"},
                playwright_starter=lambda: controller,
            )

            with worker:
                primary_page = worker.goto("http://oa.example.test/seeyon/leave")
                with worker.fork_page() as readback_worker:
                    readback_page = readback_worker.goto(
                        "http://oa.example.test/seeyon/home"
                    )
                    self.assertIs(worker.page, primary_page)
                    self.assertEqual(
                        primary_page.url,
                        "http://oa.example.test/seeyon/leave",
                    )
                    self.assertEqual(
                        readback_page.url,
                        "http://oa.example.test/seeyon/home",
                    )

                self.assertTrue(readback_page.closed)
                self.assertFalse(primary_page.closed)

    def test_worker_prevents_concurrent_use_of_the_same_profile(self):
        with TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile"
            first = CentralBrowserWorker(
                profile_path=profile_path,
                allowed_origins={"http://oa.example.test"},
                playwright_starter=FakePlaywrightController,
            )
            second = CentralBrowserWorker(
                profile_path=profile_path,
                allowed_origins={"http://oa.example.test"},
                playwright_starter=FakePlaywrightController,
            )

            with first:
                with self.assertRaises(CentralProfileInUseError):
                    second.start()

            with second:
                self.assertEqual(second.page_title, "OA")

    def test_seeyon_adapter_parses_template_center_response(self):
        worker = FakeWorker(
            {
                "status": 200,
                "url": "http://oa.example.test/seeyon/rest/template/myTemplate",
                "content_type": "application/json",
                "json": {
                    "code": 0,
                    "data": {
                        "templates": [
                            {
                                "id": "tpl-1",
                                "subject": "HR Request",
                                "categoryName": "HR",
                            }
                        ]
                    },
                },
                "text": "",
            }
        )
        adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")

        result = adapter.list_templates(worker)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["template_id"], "tpl-1")
        self.assertEqual(result["transport"], "central_http_session")

    def test_seeyon_adapter_reports_login_required_for_html_response(self):
        worker = FakeWorker(
            {
                "status": 200,
                "url": "http://oa.example.test/seeyon/login",
                "content_type": "text/html",
                "json": None,
                "text": "<html>login</html>",
            }
        )
        adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")

        with self.assertRaises(SeeyonLoginRequired):
            adapter.list_templates(worker)

    def test_seeyon_adapter_detects_login_form_without_login_url(self):
        worker = FakeWorker(
            {
                "status": 200,
                "url": "http://oa.example.test/seeyon/rest/template/myTemplate",
                "content_type": "text/html; charset=UTF-8",
                "json": None,
                "text": (
                    '<form><input id="login_username" type="text">'
                    '<input id="login_password1" type="password"></form>'
                ),
                "elapsed_ms": 12,
            }
        )
        adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")

        with self.assertRaises(SeeyonLoginRequired):
            adapter.list_templates(worker)

    def test_seeyon_adapter_preserves_session_for_non_login_html(self):
        worker = FakeWorker(
            {
                "status": 200,
                "url": "http://oa.example.test/seeyon/rest/template/myTemplate",
                "content_type": "text/html; charset=UTF-8",
                "json": None,
                "text": "<html><h1>Temporary upstream error</h1><p>internal detail</p></html>",
                "elapsed_ms": 321,
            }
        )
        adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")

        with self.assertRaises(SeeyonSessionCheckUnavailable) as raised:
            adapter.list_templates(worker)

        message = str(raised.exception)
        self.assertIn("HTTP 200", message)
        self.assertIn("content_type=text/html", message)
        self.assertIn("elapsed_ms=321", message)
        self.assertNotIn("internal detail", message)

    def test_seeyon_adapter_preserves_session_for_temporary_http_failure(self):
        worker = FakeWorker(
            {
                "status": 503,
                "url": "http://oa.example.test/seeyon/rest/template/myTemplate",
                "content_type": "text/html",
                "json": None,
                "text": "private upstream body",
                "elapsed_ms": 30000,
            }
        )
        adapter = SeeyonCentralAdapter(base_url="http://oa.example.test/seeyon/main.do?method=main")

        with self.assertRaises(SeeyonSessionCheckUnavailable) as raised:
            adapter.list_templates(worker)

        message = str(raised.exception)
        self.assertIn("HTTP 503", message)
        self.assertIn("elapsed_ms=30000", message)
        self.assertNotIn("private upstream body", message)

    def test_seeyon_authentication_contract_has_fixed_registered_secret_fields(self):
        adapter = SeeyonCentralAdapter(
            base_url="http://oa.example.test/seeyon/main.do?method=main"
        )

        contract = adapter.authentication_contract()

        self.assertEqual(contract["system_id"], "oa")
        self.assertEqual(contract["origin"], "http://oa.example.test")
        self.assertEqual([field["name"] for field in contract["fields"]], ["username", "password"])
        self.assertTrue(contract["page_fingerprint"].startswith("seeyon-form-login-v1:"))

    def test_seeyon_adapter_authenticates_through_real_page_contract(self):
        worker = FakeLoginWorker()
        adapter = SeeyonCentralAdapter(
            base_url="http://oa.example.test/seeyon/main.do?method=main"
        )

        result = adapter.authenticate(
            worker,
            {"username": "alice.login", "password": "secret"},
            timeout_seconds=2,
        )

        self.assertTrue(worker.cleared)
        self.assertEqual(worker.page.login_frame.username.value, "alice.login")
        self.assertEqual(worker.page.login_frame.password.value, "secret")
        self.assertTrue(worker.page.login_frame.submit.clicked)
        self.assertEqual(result["observed_principal_ref"], "Alice")
        self.assertEqual(result["templates"]["count"], 1)

    def test_seeyon_adapter_waits_for_login_iframe_to_render(self):
        worker = FakeLoginWorker(page=DelayedFakeLoginPage(hidden_reads=2))
        adapter = SeeyonCentralAdapter(
            base_url="http://oa.example.test/seeyon/main.do?method=main"
        )

        result = adapter.authenticate(
            worker,
            {"username": "alice.login", "password": "secret"},
            timeout_seconds=2,
        )

        self.assertGreaterEqual(worker.page.frame_reads, 3)
        self.assertEqual(result["observed_principal_ref"], "Alice")


class FakeWorker:
    def __init__(self, response):
        self.response = response

    def request(self, method, url, **_kwargs):
        return {**self.response, "method": method, "requested_url": url}


class FakeLoginWorker:
    def __init__(self, *, page=None):
        self.page = page or FakeLoginPage()
        self.cleared = False

    def clear_session_state(self):
        self.cleared = True

    def goto(self, url, **_kwargs):
        self.page.url = url

    def request(self, method, url, **_kwargs):
        if not self.page.login_frame.submit.clicked:
            return {
                "status": 401,
                "url": url,
                "content_type": "application/json",
                "json": {"code": 401},
                "text": "",
            }
        return {
            "status": 200,
            "url": url,
            "content_type": "application/json",
            "json": {
                "code": 0,
                "data": {
                    "templates": [
                        {"id": "tpl-1", "subject": "Template", "categoryName": "General"}
                    ]
                },
            },
            "text": "",
        }

    @property
    def page_title(self):
        return "致远A8-V5协同管理软件, Alice,您好!" if self.page.login_frame.submit.clicked else "OA login"

    @property
    def page_url(self):
        return self.page.url


class FakeLoginPage:
    def __init__(self):
        self.url = "http://oa.example.test/login"
        self.login_frame = FakeLoginFrame()
        self.frames = [self.login_frame]


class DelayedFakeLoginPage:
    def __init__(self, *, hidden_reads):
        self.url = "http://oa.example.test/login"
        self.login_frame = FakeLoginFrame()
        self.hidden_reads = hidden_reads
        self.frame_reads = 0

    @property
    def frames(self):
        self.frame_reads += 1
        if self.frame_reads <= self.hidden_reads:
            return []
        return [self.login_frame]

    def locator(self, _selector):
        return FakeLoginLocator(visible=False)


class FakeLoginFrame:
    def __init__(self):
        self.username = FakeLoginLocator()
        self.password = FakeLoginLocator()
        self.submit = FakeLoginLocator()

    def locator(self, selector):
        mapping = {
            '#login_username': self.username,
            '#login_password1': self.password,
            '#login_button': self.submit,
        }
        return mapping.get(selector, FakeLoginLocator(visible=False))


class FakeLoginLocator:
    def __init__(self, *, visible=True):
        self.visible = visible
        self.value = None
        self.clicked = False

    def count(self):
        return 1 if self.visible else 0

    def nth(self, _index):
        return self

    def is_visible(self):
        return self.visible

    def fill(self, value):
        self.value = value

    def click(self):
        self.clicked = True

    def press(self, _key):
        self.clicked = True


class FakeResponse:
    status = 200
    url = "http://oa.example.test/rest/templates"

    @property
    def headers(self):
        return {"content-type": "application/json"}

    def text(self):
        return '{"code": 0, "data": {"templates": []}}'

    def json(self):
        return {"code": 0, "data": {"templates": []}}

    def body(self):
        return self.text().encode()


class FakePlainTextJsonResponse(FakeResponse):
    @property
    def headers(self):
        return {"content-type": "text/plain; charset=utf-8"}

    def text(self):
        return '{"Data": {"rows": []}}'


class FakePdfResponse(FakeResponse):
    @property
    def headers(self):
        return {
            "content-type": "application/pdf",
            "content-length": "20",
        }

    def body(self):
        return b"%PDF-1.7\ncertificate"

class FakeRequestContext:
    def __init__(self):
        self.calls = []
        self.response = FakeResponse()

    def fetch(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.resources = []
        self.html = "<html><body>OA</body></html>"
        self.main_frame = self
        self.frames = [self]
        self.waits = []
        self.closed = False

    def goto(self, url, **_kwargs):
        self.url = url

    def title(self):
        return "OA"

    def content(self):
        return self.html

    def evaluate(self, _script):
        return list(self.resources)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def close(self):
        self.closed = True


class FakeFrame:
    def __init__(self, url, html):
        self.url = url
        self.html = html

    def content(self):
        return self.html


class FakeBrowserContext:
    def __init__(self):
        self.request = FakeRequestContext()
        self.pages = [FakePage()]
        self.closed = False
        self.cookie_jar = []
        self.added_cookies = []
        self.cookies_cleared = False
        self.cookie_requests = []

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True

    def cookies(self, urls=None):
        self.cookie_requests.append(urls)
        return [dict(cookie) for cookie in self.cookie_jar]

    def add_cookies(self, cookies):
        self.added_cookies.append(cookies)
        self.cookie_jar.extend(dict(cookie) for cookie in cookies)

    def clear_cookies(self):
        self.cookie_jar = []
        self.cookies_cleared = True


class FakeChromium:
    def __init__(self, controller):
        self.controller = controller
        self.launches = []
        self.cdp_endpoints = []

    def launch_persistent_context(self, user_data_dir, **kwargs):
        self.launches.append({"user_data_dir": user_data_dir, **kwargs})
        return self.controller.context

    def connect_over_cdp(self, endpoint):
        self.cdp_endpoints.append(endpoint)
        return self.controller.browser


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.closed = False

    def close(self):
        self.closed = True


class FakePlaywrightController:
    def __init__(self):
        self.context = FakeBrowserContext()
        self.browser = FakeBrowser(self.context)
        self.chromium = FakeChromium(self)
        self.stopped = False

    def stop(self):
        self.stopped = True


if __name__ == "__main__":
    unittest.main()
