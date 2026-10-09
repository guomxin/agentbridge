import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.session_secrets import (
    AesGcmSessionStateProtector,
    SESSION_KEY_FILE_ENV,
    SessionSecretError,
    SessionStateAccessDenied,
    SessionStateStore,
)


class AesGcmSessionStateProtectorTests(unittest.TestCase):
    def test_round_trip_encrypts_cookie_and_binds_session_context(self):
        with TemporaryDirectory() as tmp:
            key_path = self._key_file(Path(tmp), b"a" * 32)
            protector = AesGcmSessionStateProtector.from_key_file(key_path)
            plaintext = b'{"cookies":[{"value":"secret-cookie"}]}'

            ciphertext = protector.protect(plaintext, context=b"session-a")

            self.assertTrue(ciphertext.startswith(b"ABSS\x01"))
            self.assertNotIn(b"secret-cookie", ciphertext)
            self.assertEqual(
                protector.unprotect(ciphertext, context=b"session-a"),
                plaintext,
            )
            with self.assertRaises(SessionStateAccessDenied):
                protector.unprotect(ciphertext, context=b"session-b")

    def test_wrong_key_and_tampering_fail_authentication(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = AesGcmSessionStateProtector.from_key_file(
                self._key_file(root, b"a" * 32, name="first.key")
            )
            second = AesGcmSessionStateProtector.from_key_file(
                self._key_file(root, b"b" * 32, name="second.key")
            )
            ciphertext = first.protect(b"secret", context=b"session-a")
            tampered = bytearray(ciphertext)
            tampered[-1] ^= 1

            with self.assertRaises(SessionStateAccessDenied):
                second.unprotect(ciphertext, context=b"session-a")
            with self.assertRaises(SessionStateAccessDenied):
                first.unprotect(bytes(tampered), context=b"session-a")

    def test_invalid_key_size_and_relative_path_are_rejected(self):
        with TemporaryDirectory() as tmp:
            short_key = self._key_file(Path(tmp), b"short")

            with self.assertRaisesRegex(SessionSecretError, "exactly 32 bytes"):
                AesGcmSessionStateProtector.from_key_file(short_key)
            with self.assertRaisesRegex(SessionSecretError, "must be absolute"):
                AesGcmSessionStateProtector.from_key_file("session.key")

    @unittest.skipUnless(os.name == "posix", "POSIX key-file validation")
    def test_default_posix_store_uses_environment_key_and_survives_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_path = self._key_file(root, os.urandom(32), mode=0o440)
            state = {
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": "linux-secret-cookie",
                        "domain": "oa.example.test",
                        "path": "/",
                    }
                ]
            }
            with patch.dict(os.environ, {SESSION_KEY_FILE_ENV: str(key_path)}):
                SessionStateStore(root / "states").save("session-a", state)
                restored = SessionStateStore(root / "states").load("session-a")

            self.assertEqual(restored, state)
            self.assertNotIn(
                b"linux-secret-cookie",
                SessionStateStore(
                    root / "states",
                    protector=AesGcmSessionStateProtector.from_key_file(key_path),
                ).path_for("session-a").read_bytes(),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX key-file validation")
    def test_posix_key_file_rejects_broad_permissions_and_symlinks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            broad = self._key_file(root, os.urandom(32), mode=0o644)
            link = root / "linked.key"
            link.symlink_to(broad)

            with self.assertRaisesRegex(SessionSecretError, "permissions are too broad"):
                AesGcmSessionStateProtector.from_key_file(broad)
            with self.assertRaisesRegex(SessionSecretError, "could not be opened"):
                AesGcmSessionStateProtector.from_key_file(link)

    @unittest.skipUnless(os.name == "posix", "POSIX key-file validation")
    def test_default_posix_store_requires_explicit_key_file(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ):
            os.environ.pop(SESSION_KEY_FILE_ENV, None)

            with self.assertRaisesRegex(SessionSecretError, SESSION_KEY_FILE_ENV):
                SessionStateStore(Path(tmp))

    @staticmethod
    def _key_file(
        root: Path,
        value: bytes,
        *,
        name: str = "session.key",
        mode: int = 0o600,
    ) -> Path:
        path = root / name
        path.write_bytes(value)
        path.chmod(mode)
        return path


if __name__ == "__main__":
    unittest.main()
