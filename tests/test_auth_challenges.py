from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agentbridge.core.auth_challenges import (
    AuthChallengeStore,
    ChallengeAccessDenied,
    ChallengeStateError,
)


class AuthChallengeStoreTests(unittest.TestCase):
    def test_challenge_binds_session_contract_and_is_consumed_once(self):
        with TemporaryDirectory() as tmp:
            clock = MutableClock(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc))
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db", clock=clock)

            challenge = store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="seeyon-login-v1:abc",
                nonce="nonce-a",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=300,
            )

            self.assertEqual(challenge["state"], "pending")
            self.assertEqual(challenge["session_id"], "session-a")
            self.assertEqual(challenge["fields"][1]["input_type"], "password")
            self.assertTrue(challenge["card_url"].startswith("http://127.0.0.1:8780/auth/"))

            csrf = store.issue_csrf(challenge["challenge_id"])
            claimed = store.claim(
                challenge["challenge_id"],
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            self.assertEqual(claimed["state"], "processing")

            completed = store.complete(
                challenge["challenge_id"],
                result={"observed_principal_ref": "Alice"},
            )
            self.assertEqual(completed["state"], "succeeded")
            with self.assertRaises(ChallengeStateError):
                store.claim(
                    challenge["challenge_id"],
                    csrf_token=csrf,
                    csrf_cookie=csrf,
                )

    def test_challenge_rejects_csrf_mismatch_and_expires(self):
        with TemporaryDirectory() as tmp:
            clock = MutableClock(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc))
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db", clock=clock)
            challenge = store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="seeyon-login-v1:abc",
                nonce="nonce-a",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=60,
            )
            csrf = store.issue_csrf(challenge["challenge_id"])

            with self.assertRaises(ChallengeAccessDenied):
                store.claim(
                    challenge["challenge_id"],
                    csrf_token=csrf,
                    csrf_cookie="different",
                )

            clock.value += timedelta(seconds=61)
            expired = store.get(challenge["challenge_id"])
            self.assertEqual(expired["state"], "expired")
            with self.assertRaises(ChallengeStateError):
                store.issue_csrf(challenge["challenge_id"])

    def test_new_challenge_supersedes_previous_pending_challenge(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            first = store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-a",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
            )
            second = store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-b",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertEqual(store.get(first["challenge_id"])["state"], "superseded")
            self.assertEqual(store.get(second["challenge_id"])["state"], "pending")

    def test_matching_active_challenge_is_reused_while_pending_or_processing(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")

            first, first_reused = store.create_or_reuse(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-a",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
            )
            pending, pending_reused = store.create_or_reuse(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-b",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertFalse(first_reused)
            self.assertTrue(pending_reused)
            self.assertEqual(pending["challenge_id"], first["challenge_id"])
            self.assertEqual(pending["card_url"], first["card_url"])

            csrf = store.issue_csrf(first["challenge_id"])
            store.claim(
                first["challenge_id"],
                csrf_token=csrf,
                csrf_cookie=csrf,
            )
            processing, processing_reused = store.create_or_reuse(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-c",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
            )

            self.assertTrue(processing_reused)
            self.assertEqual(processing["challenge_id"], first["challenge_id"])
            self.assertEqual(processing["state"], "processing")

    def test_expired_challenge_is_replaced_instead_of_reused(self):
        with TemporaryDirectory() as tmp:
            clock = MutableClock(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc))
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db", clock=clock)
            first, _reused = store.create_or_reuse(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-a",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=60,
            )

            clock.value += timedelta(seconds=61)
            second, reused = store.create_or_reuse(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce-b",
                fields=_login_fields(),
                card_base_url="http://127.0.0.1:8780",
                ttl_seconds=60,
            )

            self.assertFalse(reused)
            self.assertNotEqual(second["challenge_id"], first["challenge_id"])
            self.assertEqual(store.get(first["challenge_id"])["state"], "expired")
            self.assertEqual(second["state"], "pending")


def _login_fields() -> list[dict]:
    return [
        {
            "name": "username",
            "label": "OA account",
            "input_type": "text",
            "autocomplete": "username",
            "required": True,
        },
        {
            "name": "password",
            "label": "Password",
            "input_type": "password",
            "autocomplete": "current-password",
            "required": True,
        },
    ]


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


if __name__ == "__main__":
    unittest.main()
