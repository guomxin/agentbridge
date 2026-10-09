from __future__ import annotations

import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from urllib.parse import urlencode

from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.interactive_browser import TrustedInteractiveBrowserApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.auth_challenges import AuthChallengeStore


class InteractiveBrowserServerTests(unittest.TestCase):
    def test_http_server_routes_remote_browser_card_start_and_status(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            challenge = store.create(
                user_subject="wechat:user-b",
                system_id="yuque",
                system_name="部门信息库",
                session_id="session-yuque",
                expected_principal_ref="辛国茂",
                origin="https://tc-aiot.yuque.com",
                page_fingerprint="yuque-interactive-login-v1",
                nonce=None,
                fields=[],
                card_base_url="http://127.0.0.1:0",
                challenge_type="interactive_browser_login",
            )
            broker = FakeInteractiveBroker()
            config = validate_auth_server_config(
                host="127.0.0.1",
                port=0,
                public_base_url="http://127.0.0.1:0",
                tls_cert=None,
                tls_key=None,
            )
            server = create_auth_http_server(
                config=config,
                application=TrustedAuthApplication(
                    challenge_store=store,
                    broker=RejectingCredentialBroker(),
                ),
                interactive_application=TrustedInteractiveBrowserApplication(
                    challenge_store=store,
                    broker=broker,
                ),
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", f"/auth/{challenge['challenge_id']}")
                response = connection.getresponse()
                html = response.read().decode("utf-8")
                cookie = response.getheader("Set-Cookie")
                self.assertEqual(response.status, 200)
                self.assertIn("启动安全登录", html)
                self.assertIn(
                    "frame-src https://10.10.50.213:8781",
                    response.getheader("Content-Security-Policy"),
                )
                self.assertIsNotNone(cookie)
                csrf = cookie.split("agentbridge_csrf=", 1)[1].split(";", 1)[0]
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                body = urlencode({"csrf_token": csrf})
                connection.request(
                    "POST",
                    f"/auth/{challenge['challenge_id']}/interactive/start",
                    body=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Cookie": f"agentbridge_csrf={csrf}",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 202)
                self.assertEqual(payload["controlToken"], "control-token")
                self.assertIn("vnc_lite.html", payload["remoteUrl"])
                self.assertEqual(broker.started, challenge["challenge_id"])
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "GET",
                    f"/auth/{challenge['challenge_id']}/interactive/status",
                    headers={"X-AgentBridge-Control-Token": "control-token"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read().decode("utf-8"))["verification"],
                    "awaiting_login",
                )
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class FakeInteractiveBroker:
    public_origin = "https://10.10.50.213:8781"

    def __init__(self) -> None:
        self.started = None

    def start(self, *, challenge_id, csrf_token, csrf_cookie):
        if csrf_token != csrf_cookie:
            raise AssertionError("CSRF values differ")
        self.started = challenge_id
        return {
            "status": "processing",
            "challengeId": challenge_id,
            "controlToken": "control-token",
            "remoteUrl": (
                f"{self.public_origin}/vnc_lite.html?"
                "path=websockify%3Ftoken%3Dopaque#agentbridge=1&password=temporary"
            ),
        }

    def status(self, *, challenge_id, control_token):
        if challenge_id != self.started or control_token != "control-token":
            raise AssertionError("interactive status binding mismatch")
        return {
            "status": "processing",
            "challengeId": challenge_id,
            "verification": "awaiting_login",
        }


class RejectingCredentialBroker:
    def authenticate(self, **_kwargs):
        raise AssertionError("interactive request reached credential broker")


if __name__ == "__main__":
    unittest.main()
