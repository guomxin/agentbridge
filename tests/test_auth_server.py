import http.client
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from agentbridge.auth.action_card import TrustedActionApplication
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.field_card import TrustedFieldApplication
from agentbridge.auth.server import (
    _request_origin_allowed,
    create_auth_http_server,
    validate_auth_server_config,
)
from agentbridge.core.auth_challenges import AuthChallengeStore
from agentbridge.core.field_submissions import FieldSubmissionStore
from agentbridge.core.write_authorizations import WriteAuthorizationStore


class AuthServerConfigTests(unittest.TestCase):
    def test_loopback_server_can_use_http_for_local_poc(self):
        config = validate_auth_server_config(
            host="127.0.0.1",
            port=8780,
            public_base_url=None,
            tls_cert=None,
            tls_key=None,
        )

        self.assertEqual(config.public_base_url, "http://127.0.0.1:8780")
        self.assertFalse(config.secure_cookie)

    def test_non_loopback_server_requires_tls_and_https_public_url(self):
        with self.assertRaisesRegex(ValueError, "requires TLS"):
            validate_auth_server_config(
                host="0.0.0.0",
                port=8780,
                public_base_url="http://auth.example.test:8780",
                tls_cert=None,
                tls_key=None,
            )

        with TemporaryDirectory() as tmp:
            cert = Path(tmp) / "cert.pem"
            key = Path(tmp) / "key.pem"
            with self.assertRaisesRegex(ValueError, "must use HTTPS"):
                validate_auth_server_config(
                    host="0.0.0.0",
                    port=8780,
                    public_base_url="http://auth.example.test:8780",
                    tls_cert=cert,
                    tls_key=key,
                )

    def test_explicit_private_ip_http_is_allowed_for_restricted_poc(self):
        config = validate_auth_server_config(
            host="10.20.30.40",
            port=8780,
            public_base_url="http://10.20.30.40:8780",
            tls_cert=None,
            tls_key=None,
            allow_insecure_private_http=True,
        )

        self.assertEqual(config.public_base_url, "http://10.20.30.40:8780")
        self.assertFalse(config.secure_cookie)
        self.assertTrue(config.insecure_private_http)

    def test_insecure_private_http_rejects_unsafe_or_mismatched_endpoints(self):
        cases = [
            (
                "wildcard bind",
                "0.0.0.0",
                8780,
                "http://10.20.30.40:8780",
                "RFC 1918 or IPv6 ULA",
            ),
            (
                "public address",
                "203.0.113.10",
                8780,
                "http://203.0.113.10:8780",
                "RFC 1918 or IPv6 ULA",
            ),
            (
                "hostname",
                "agentbridge.internal",
                8780,
                "http://agentbridge.internal:8780",
                "literal private IP",
            ),
            (
                "different public IP",
                "10.20.30.40",
                8780,
                "http://10.20.30.41:8780",
                "match the bind IP",
            ),
            (
                "different public port",
                "10.20.30.40",
                8780,
                "http://10.20.30.40:8781",
                "match the bind port",
            ),
        ]

        for name, host, port, public_base_url, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                validate_auth_server_config(
                    host=host,
                    port=port,
                    public_base_url=public_base_url,
                    tls_cert=None,
                    tls_key=None,
                    allow_insecure_private_http=True,
                )

    def test_loopback_origin_aliases_are_treated_as_the_same_trusted_source(self):
        allowed_hosts = {"127.0.0.1", "localhost", "::1"}

        self.assertTrue(
            _request_origin_allowed(
                origin="http://localhost:8780",
                sec_fetch_site="same-origin",
                host_header="127.0.0.1:8780",
                expected_scheme="http",
                allowed_hosts=allowed_hosts,
            )
        )
        self.assertTrue(
            _request_origin_allowed(
                origin="http://[::1]:8780",
                sec_fetch_site="same-origin",
                host_header="localhost:8780",
                expected_scheme="http",
                allowed_hosts=allowed_hosts,
            )
        )

    def test_opaque_origin_requires_same_origin_fetch_metadata(self):
        parameters = {
            "origin": "null",
            "host_header": "127.0.0.1:8780",
            "expected_scheme": "http",
            "allowed_hosts": {"127.0.0.1", "localhost", "::1"},
        }

        self.assertTrue(
            _request_origin_allowed(sec_fetch_site="same-origin", **parameters)
        )
        self.assertFalse(
            _request_origin_allowed(sec_fetch_site="cross-site", **parameters)
        )
        self.assertFalse(_request_origin_allowed(sec_fetch_site=None, **parameters))

    def test_private_http_poc_allows_opaque_origin_without_fetch_metadata(self):
        parameters = {
            "origin": "null",
            "host_header": "10.20.30.40:8780",
            "expected_scheme": "http",
            "allowed_hosts": {"10.20.30.40"},
            "allow_opaque_without_fetch_metadata": True,
        }

        self.assertTrue(_request_origin_allowed(sec_fetch_site=None, **parameters))
        self.assertTrue(
            _request_origin_allowed(sec_fetch_site="same-origin", **parameters)
        )
        self.assertFalse(
            _request_origin_allowed(sec_fetch_site="cross-site", **parameters)
        )

    def test_origin_check_rejects_untrusted_scheme_host_and_port(self):
        parameters = {
            "sec_fetch_site": "same-origin",
            "host_header": "127.0.0.1:8780",
            "expected_scheme": "http",
            "allowed_hosts": {"127.0.0.1", "localhost", "::1"},
        }

        self.assertFalse(
            _request_origin_allowed(
                origin="https://127.0.0.1:8780", **parameters
            )
        )
        self.assertFalse(
            _request_origin_allowed(
                origin="http://evil.example.test:8780", **parameters
            )
        )
        self.assertFalse(
            _request_origin_allowed(
                origin="http://127.0.0.1:9999", **parameters
            )
        )

    def test_http_server_serves_card_and_rejects_cross_origin_post(self):
        with TemporaryDirectory() as tmp:
            store = AuthChallengeStore(Path(tmp) / "agentbridge.db")
            challenge = store.create(
                user_subject="user-a",
                system_id="oa",
                system_name="致远 OA",
                session_id="session-a",
                expected_principal_ref="Alice",
                origin="http://oa.example.test",
                page_fingerprint="fingerprint",
                nonce="nonce",
                fields=[
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
                ],
                card_base_url="http://127.0.0.1:0",
            )
            config = validate_auth_server_config(
                host="127.0.0.1",
                port=0,
                public_base_url="http://127.0.0.1:0",
                tls_cert=None,
                tls_key=None,
            )
            application = TrustedAuthApplication(
                challenge_store=store,
                broker=RejectingBroker(),
            )
            server = create_auth_http_server(config=config, application=application)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", f"/auth/{challenge['challenge_id']}")
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("致远 OA", body)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                connection.close()

                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    port,
                    timeout=5,
                )
                connection.request(
                    "POST",
                    f"/auth/{challenge['challenge_id']}",
                    body="csrf_token=x&username=a&password=b",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://evil.example.test",
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_server_serves_write_authorization_on_separate_route(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            challenge_store = AuthChallengeStore(db_path)
            authorization_store = WriteAuthorizationStore(db_path)
            authorization = authorization_store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                capability_name="oa.business_trip.save_draft",
                capability_version="0.1.0",
                prepare_operation_id="prepare-1",
                plan={"exact_input": {"reason": "Test"}},
                summary={
                    "title": "保存出差申请草稿",
                    "fields": [{"label": "事由", "value": "Test"}],
                },
                card_base_url="http://127.0.0.1:0",
            )
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
                    challenge_store=challenge_store,
                    broker=RejectingBroker(),
                ),
                action_application=TrustedActionApplication(
                    authorization_store=authorization_store,
                ),
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                presentation = authorization_store.create_presentation(
                    authorization["authorization_id"],
                    user_subject="user-a",
                    endpoint_id="telegram-1",
                )
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "GET",
                    f"/authorize/{authorization['authorization_id']}",
                )
                response = connection.getresponse()
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("保存出差申请草稿", html)
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                presentation_path = (
                    f"/authorize/{authorization['authorization_id']}/present/"
                    f"{presentation['presentation_id']}"
                )
                connection.request("GET", presentation_path)
                response = connection.getresponse()
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn(f'action="{presentation_path}"', html)
                self.assertIn(f"Path={presentation_path}", response.getheader("Set-Cookie"))
                connection.close()

                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "POST",
                    f"/authorize/{authorization['authorization_id']}",
                    body="csrf_token=x&decision=approve",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://evil.example.test",
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    authorization_store.get(authorization["authorization_id"])["state"],
                    "pending",
                )
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_http_server_serves_field_input_on_separate_route(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agentbridge.db"
            challenge_store = AuthChallengeStore(db_path)
            submission_store = FieldSubmissionStore(db_path)
            submission = submission_store.create(
                user_subject="user-a",
                system_id="oa",
                session_id="session-a",
                capability_name="oa.business_trip.prepare",
                capability_version="0.2.0",
                create_operation_id="prepare-1",
                form_schema={
                    "schema_version": "test.fields.v1",
                    "title": "填写出差申请",
                    "fields": [
                        {
                            "name": "reason",
                            "label": "事由",
                            "control": "text",
                            "required": True,
                        }
                    ],
                },
                card_base_url="http://127.0.0.1:0",
            )
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
                    challenge_store=challenge_store,
                    broker=RejectingBroker(),
                ),
                field_application=TrustedFieldApplication(
                    submission_store=submission_store,
                ),
            )
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", f"/input/{submission['submission_id']}")
                response = connection.getresponse()
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("填写出差申请", html)
                connection.close()

                presentation = submission_store.create_presentation(
                    submission["submission_id"],
                    user_subject="user-a",
                    endpoint_id="endpoint-telegram",
                )
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    port,
                    timeout=5,
                )
                connection.request(
                    "GET",
                    (
                        f"/input/{submission['submission_id']}/present/"
                        f"{presentation['presentation_id']}"
                    ),
                )
                response = connection.getresponse()
                html = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn(
                    f"/present/{presentation['presentation_id']}",
                    html,
                )
                connection.close()

                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    port,
                    timeout=5,
                )
                connection.request(
                    "POST",
                    f"/input/{submission['submission_id']}",
                    body="csrf_token=x&reason=Test",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://evil.example.test",
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    submission_store.get(submission["submission_id"])["state"],
                    "pending",
                )
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class RejectingBroker:
    def authenticate(self, **_kwargs):
        raise AssertionError("cross-origin request reached credential broker")


if __name__ == "__main__":
    unittest.main()
