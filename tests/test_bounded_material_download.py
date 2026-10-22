from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
import unittest
from unittest.mock import patch
import zlib

from agentbridge.browser.central import CentralBrowserWorker
from agentbridge.browser.http import CentralHttpWorker
from agentbridge.core.capability_runtime import CapabilityRejected
from tests.test_central_browser import FakePlaywrightController


class Stream(BytesIO):
    def __init__(self, data, *, headers=None):
        super().__init__(data)
        self.status = 200
        self.headers = {"Content-Type": "application/octet-stream", **(headers or {})}
        self.read_sizes = []

    def geturl(self): return "http://oa.example.test/file"

    def read1(self, size=-1):
        self.read_sizes.append(size)
        return super().read1(size)


class BoundedBinaryDownloadTests(unittest.TestCase):
    def test_binary_http_reads_stop_at_limit_plus_one_without_full_body_buffering(self):
        worker = CentralHttpWorker(allowed_origins={"http://oa.example.test"})
        response = Stream(b"x" * 100000)
        with patch.object(worker, "_open", return_value=response), self.assertRaises(CapabilityRejected) as caught:
            worker.request_bytes("GET", "http://oa.example.test/file", max_response_bytes=100, deadline=monotonic() + 1)
        self.assertEqual(caught.exception.code, "RESULT_INCOMPLETE")
        self.assertEqual(response.read_sizes, [101])

    def test_declared_oversize_is_rejected_without_reading_and_deadline_blocks_network(self):
        worker = CentralHttpWorker(allowed_origins={"http://oa.example.test"})
        response = Stream(b"x", headers={"Content-Length": "1000000"})
        with patch.object(worker, "_open", return_value=response), self.assertRaises(CapabilityRejected):
            worker.request_bytes("GET", "http://oa.example.test/file", max_response_bytes=100)
        self.assertEqual(response.read_sizes, [])
        with patch.object(worker, "_open") as network, self.assertRaises(CapabilityRejected) as caught:
            worker.request_bytes("GET", "http://oa.example.test/file", max_response_bytes=100, deadline=monotonic() - 1)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        network.assert_not_called()

    def test_connect_timeout_is_clamped_to_total_deadline(self):
        worker = CentralHttpWorker(allowed_origins={"http://oa.example.test"})
        with patch.object(worker, "_open", return_value=Stream(b"small")) as network:
            self.assertEqual(worker.request_bytes("GET", "http://oa.example.test/file", max_response_bytes=100,
                                                timeout_seconds=20, deadline=monotonic() + .1)["body"], b"small")
        self.assertGreater(network.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(network.call_args.kwargs["timeout"], .1)

    def test_compressed_body_is_bounded_after_decompression_too(self):
        worker = CentralHttpWorker(allowed_origins={"http://oa.example.test"})
        response = Stream(zlib.compress(b"x" * 10000), headers={"Content-Encoding": "deflate"})
        with patch.object(worker, "_open", return_value=response), self.assertRaises(CapabilityRejected) as caught:
            worker.request_bytes("GET", "http://oa.example.test/file", max_response_bytes=100)
        self.assertEqual(caught.exception.code, "RESULT_INCOMPLETE")

    def test_browser_bounded_branch_streams_current_session_cookie_and_restores_rotation(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                observed.append(self.headers.get("Cookie"))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Set-Cookie", "JSESSIONID=rotated; Path=/; HttpOnly; SameSite=Strict")
                self.end_headers(); self.wfile.write(b"synthetic-binary")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with TemporaryDirectory() as home:
                controller = FakePlaywrightController()
                controller.context.cookie_jar = [{"name": "JSESSIONID", "value": "original", "domain": "127.0.0.1",
                    "path": "/", "expires": -1, "secure": False, "httpOnly": True, "sameSite": "Lax"}]
                origin = f"http://127.0.0.1:{server.server_port}"
                with CentralBrowserWorker(profile_path=Path(home) / "profile", allowed_origins={origin},
                                          playwright_starter=lambda: controller) as worker:
                    result = worker.request_bytes("GET", origin + "/file", max_response_bytes=100, deadline=monotonic() + 1)
                    self.assertEqual(result["body"], b"synthetic-binary")
                self.assertEqual(observed, ["JSESSIONID=original"])
                self.assertEqual(controller.context.request.calls, [])
                rotated = controller.context.added_cookies[-1][0]
                self.assertEqual((rotated["value"], rotated["expires"], rotated["httpOnly"], rotated["sameSite"]),
                                 ("rotated", -1, True, "Strict"))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_bounded_browser_branch_rejects_mutation_and_foreign_origin_before_transfer(self):
        with TemporaryDirectory() as home:
            controller = FakePlaywrightController()
            with CentralBrowserWorker(profile_path=Path(home) / "profile", allowed_origins={"http://oa.example.test"},
                                      playwright_starter=lambda: controller) as worker:
                with patch("agentbridge.browser.http.CentralHttpWorker.request_bytes") as request:
                    with self.assertRaises(ValueError):
                        worker.request_bytes("POST", "http://oa.example.test/file", max_response_bytes=100)
                    with self.assertRaises(ValueError):
                        worker.request_bytes("GET", "http://other.test/file", max_response_bytes=100)
                    request.assert_not_called()
