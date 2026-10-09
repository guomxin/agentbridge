from __future__ import annotations

import unittest
from unittest.mock import patch

from agentbridge.browser.http import CentralHttpWorker, _RejectRedirectHandler


class CentralHttpWorkerTests(unittest.TestCase):
    def test_state_round_trip_is_copy_safe_and_cookie_compatible(self):
        worker = CentralHttpWorker(allowed_origins={"http://10.10.50.101"})
        worker.set_http_state(
            {
                "authorization": "Bearer access",
                "refresh_token": "refresh",
            }
        )

        captured = worker.capture_session_state()
        captured["http"]["authorization"] = "changed"

        self.assertEqual(
            worker.get_http_state()["authorization"],
            "Bearer access",
        )
        self.assertEqual(worker.capture_session_state()["cookies"], [])

        restored = CentralHttpWorker(
            allowed_origins={"http://10.10.50.101"}
        )
        restored.restore_session_state(worker.capture_session_state())
        self.assertEqual(restored.get_http_state(), worker.get_http_state())

    def test_request_blocks_unregistered_origins_before_network_access(self):
        worker = CentralHttpWorker(allowed_origins={"http://10.10.50.101"})

        with patch.object(worker, "_open") as open_request:
            with self.assertRaisesRegex(ValueError, "origin is not allowed"):
                worker.request("GET", "http://10.10.50.102/api/users/principal")

        open_request.assert_not_called()

    def test_http_redirect_handler_prevents_automatic_follow(self):
        handler = _RejectRedirectHandler()

        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "http://other/")
        )
    def test_redirect_to_unregistered_origin_is_rejected(self):
        worker = CentralHttpWorker(allowed_origins={"http://10.10.50.101"})
        response = FakeResponse(
            status=200,
            url="http://10.10.50.102/redirected",
            content=b'{"ok":true}',
            content_type="application/json",
        )

        with patch.object(worker, "_open", return_value=response):
            with self.assertRaisesRegex(ValueError, "origin is not allowed"):
                worker.request("GET", "http://10.10.50.101/api/test")


class FakeResponse:
    def __init__(self, *, status, url, content, content_type):
        self.status = status
        self._url = url
        self._content = content
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return None

    def geturl(self):
        return self._url

    def read(self):
        return self._content


if __name__ == "__main__":
    unittest.main()
