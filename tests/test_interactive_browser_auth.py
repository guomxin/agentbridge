from __future__ import annotations

import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlencode

from agentbridge.auth.interactive_browser import TrustedInteractiveBrowserApplication
from agentbridge.core.auth_challenges import AuthChallengeStore


class TrustedInteractiveBrowserTests(unittest.TestCase):
    def test_card_embeds_native_novnc_without_model_side_input_relay(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            challenge = _challenge(store)
            broker = StubRemoteBrowserBroker()
            app = TrustedInteractiveBrowserApplication(
                challenge_store=store,
                broker=broker,
            )

            page = app.get_card(challenge["challenge_id"], secure_cookie=True)

            html = page.body.decode("utf-8")
            self.assertEqual(page.status, 200)
            self.assertIn("启动安全登录", html)
            self.assertIn("<iframe", html)
            self.assertIn("result.remoteUrl", html)
            self.assertIn("登录结果会由 AgentBridge 自动核验", html)
            self.assertNotIn("interactive/frame", html)
            self.assertNotIn("interactive/event", html)
            self.assertNotIn("pointer_stream", html)
            self.assertNotIn("type_text", html)
            self.assertNotIn("name=\"password\"", html)
            self.assertNotIn("temporary-vnc-password", html)
            self.assertIn(
                "frame-src https://10.10.50.213:8781",
                page.headers["Content-Security-Policy"],
            )
            self.assertIn("Secure", page.headers["Set-Cookie"])

    def test_start_claims_csrf_and_returns_private_remote_url_to_card(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            challenge = _challenge(store)
            broker = StubRemoteBrowserBroker()
            app = TrustedInteractiveBrowserApplication(
                challenge_store=store,
                broker=broker,
            )
            page = app.get_card(challenge["challenge_id"], secure_cookie=False)
            html = page.body.decode("utf-8")
            csrf = re.search(r'const csrfToken = "([^"]+)"', html).group(1)
            cookie = page.headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

            response = app.start(
                challenge["challenge_id"],
                body=urlencode({"csrf_token": csrf}).encode("utf-8"),
                content_type="application/x-www-form-urlencoded",
                csrf_cookie=cookie,
            )

            payload = json.loads(response.body)
            self.assertEqual(response.status, 202)
            self.assertEqual(payload["controlToken"], "short-lived-control")
            self.assertTrue(payload["remoteUrl"].startswith(broker.public_origin))
            self.assertIn("&password=", payload["remoteUrl"])
            self.assertEqual(broker.started["challenge_id"], challenge["challenge_id"])
            self.assertEqual(broker.started["csrf_token"], csrf)

    def test_status_requires_the_challenge_control_token(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            challenge = _challenge(store)
            broker = StubRemoteBrowserBroker()
            app = TrustedInteractiveBrowserApplication(
                challenge_store=store,
                broker=broker,
            )

            denied = app.status(
                challenge["challenge_id"],
                control_token="wrong",
            )
            accepted = app.status(
                challenge["challenge_id"],
                control_token="short-lived-control",
            )

            self.assertEqual(denied.status, 403)
            self.assertEqual(accepted.status, 200)
            self.assertEqual(
                json.loads(accepted.body)["verification"],
                "awaiting_login",
            )


class StubRemoteBrowserBroker:
    public_origin = "https://10.10.50.213:8781"

    def __init__(self) -> None:
        self.started = None

    def start(self, **kwargs):
        self.started = kwargs
        return {
            "status": "processing",
            "challengeId": kwargs["challenge_id"],
            "controlToken": "short-lived-control",
            "remoteUrl": (
                f"{self.public_origin}/vnc_lite.html?"
                "path=websockify%3Ftoken%3Dopaque"
                "#agentbridge=1&password=temporary-vnc-password"
            ),
        }

    def status(self, *, challenge_id, control_token):
        from agentbridge.broker.remote_browser import RemoteBrowserAccessDenied

        if control_token != "short-lived-control":
            raise RemoteBrowserAccessDenied("wrong token")
        return {
            "status": "processing",
            "challengeId": challenge_id,
            "verification": "awaiting_login",
        }


def _challenge(store: AuthChallengeStore) -> dict:
    return store.create(
        user_subject="user-a",
        system_id="yuque",
        session_id="session-a",
        origin="https://tc-aiot.yuque.com",
        page_fingerprint="yuque-interactive-login-v1",
        nonce="nonce",
        fields=[],
        card_base_url="https://10.10.50.213:8780",
        system_name="部门信息库",
        expected_principal_ref="辛国茂",
        ttl_seconds=600,
        challenge_type="interactive_browser_login",
    )


if __name__ == "__main__":
    unittest.main()
