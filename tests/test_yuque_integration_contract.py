from __future__ import annotations

from tests.authorization_fixtures import authorized_service
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.auth.interactive_browser import _challenge_ttl_seconds
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.auth_challenges import AuthChallengeStore


class YuqueIntegrationContractTests(unittest.TestCase):
    def test_only_interactive_browser_challenges_may_omit_fields(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            common = {
                "user_subject": "wechat:user-b",
                "system_id": "yuque",
                "session_id": "session-yuque",
                "origin": "https://tc-aiot.yuque.com",
                "page_fingerprint": "yuque-interactive-login-v1",
                "nonce": None,
                "fields": [],
                "card_base_url": "https://10.10.50.213:8780",
            }

            with self.assertRaisesRegex(ValueError, "fields are required"):
                store.create(**common)

            challenge = store.create(
                **common,
                challenge_type="interactive_browser_login",
            )

            self.assertEqual(challenge["challenge_type"], "interactive_browser_login")
            self.assertEqual(challenge["fields"], [])

    def test_yuque_login_challenge_and_resume_scope_are_system_specific(self):
        with TemporaryDirectory() as tmp:
            service = authorized_service(
                home=Path(tmp),
                base_url="http://oa.example.test/seeyon/main.do?method=main",
                yuque_base_url="https://tc-aiot.yuque.com",
                yuque_organization_id=20020375,
                trusted_card_base_url="https://10.10.50.213:8780",
            )

            response = service.start_login(
                user_subject="wechat:user-b",
                expected_principal_ref="辛国茂",
                card_base_url="https://10.10.50.213:8780",
                system_id="yuque",
            )
            challenge = service.challenges.get(response["challenge"]["challengeId"])

            self.assertEqual(response["status"], "requires_user_action")
            self.assertEqual(challenge["system_id"], "yuque")
            self.assertEqual(challenge["challenge_type"], "interactive_browser_login")
            self.assertEqual(challenge["fields"], [])
            self.assertEqual(
                service.interaction_required_scopes(
                    user_subject="wechat:user-b",
                    interaction_id=response["interaction"]["interactionId"],
                ),
                frozenset({"yuque:read"}),
            )

    def test_interactive_card_cookie_ttl_uses_challenge_duration(self):
        now = datetime.now(timezone.utc)
        challenge = {
            "expires_at": (now + timedelta(seconds=420)).isoformat(),
        }

        self.assertEqual(_challenge_ttl_seconds(challenge), 420)
        self.assertEqual(
            _challenge_ttl_seconds(
                {"expires_at": (now + timedelta(seconds=1200)).isoformat()}
            ),
            900,
        )


if __name__ == "__main__":
    unittest.main()
