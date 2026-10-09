import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentbridge.cli.main import build_parser, main
from agentbridge.core.sessions import SessionRegistry


class CentralMcpCliTests(unittest.TestCase):
    def test_token_issue_rejects_legacy_scope_option(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            build_parser().parse_args(["mcp", "token", "issue", "--user-subject", "user-a", "--scope", "oa:read"])

    def test_identity_token_issue_shows_secret_once_and_binds_session(self):
        with TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()) as issued_stdout:
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "token",
                        "issue",
                        "--user-subject",
                        "user-a",
                        "--expected-principal",
                        "Alice",
                        "--label",
                        "desktop",
                        "--ttl-hours",
                        "12",
                    ]
                )
            issued = json.loads(issued_stdout.getvalue())

            with redirect_stdout(io.StringIO()) as list_stdout:
                list_exit = main(
                    ["--home", tmp, "mcp", "token", "list", "--user-subject", "user-a"]
                )
            listed = json.loads(list_stdout.getvalue())
            profiles_exists = (Path(tmp) / "profiles").exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(list_exit, 0)
        self.assertTrue(issued["bearerToken"].startswith("abmcp_"))
        self.assertEqual(issued["identityToken"]["expectedPrincipalRef"], "Alice")
        self.assertEqual(
            issued["identityToken"]["scopes"], ["agentbridge:connect"],
        )
        self.assertEqual(listed["count"], 1)
        self.assertNotIn("bearerToken", listed)
        self.assertNotIn(issued["bearerToken"], json.dumps(listed))
        self.assertFalse(profiles_exists)

    def test_taihua_token_only_binds_taihua_session_and_adds_base_read_scope(self):
        with TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()) as issued_stdout:
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "token",
                        "issue",
                        "--user-subject",
                        "user-a",
                        "--system-principal",
                        "taihua=Alice",
                    ]
                )
            issued = json.loads(issued_stdout.getvalue())
            sessions = SessionRegistry(
                Path(tmp) / "agentbridge.db",
                Path(tmp) / "profiles",
            )
            has_taihua_session = (
                sessions.find(user_subject="user-a", system_id="taihua") is not None
            )
            has_oa_session = sessions.find(user_subject="user-a", system_id="oa") is not None

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            issued["identityToken"]["scopes"],
            ["agentbridge:connect"],
        )
        self.assertTrue(has_taihua_session)
        self.assertFalse(has_oa_session)

    def test_smartlight_token_only_binds_smartlight_session(self):
        with TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()) as issued_stdout:
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "token",
                        "issue",
                        "--user-subject",
                        "user-a",
                        "--system-principal",
                        "smartlight=无为",
                    ]
                )
            issued = json.loads(issued_stdout.getvalue())
            sessions = SessionRegistry(
                Path(tmp) / "agentbridge.db",
                Path(tmp) / "profiles",
            )
            has_smartlight_session = (
                sessions.find(user_subject="user-a", system_id="smartlight")
                is not None
            )
            has_oa_session = (
                sessions.find(user_subject="user-a", system_id="oa") is not None
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(issued["identityToken"]["scopes"], ["agentbridge:connect"])
        self.assertEqual(
            issued["identityToken"]["principalBindings"],
            {"smartlight": "无为"},
        )
        self.assertTrue(has_smartlight_session)
        self.assertFalse(has_oa_session)

    def test_identity_token_can_be_revoked(self):
        with TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()) as issued_stdout:
                main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "token",
                        "issue",
                        "--user-subject",
                        "user-a",
                        "--expected-principal",
                        "Alice",
                    ]
                )
            token_id = json.loads(issued_stdout.getvalue())["identityToken"]["tokenId"]

            with redirect_stdout(io.StringIO()) as revoked_stdout:
                exit_code = main(
                    ["--home", tmp, "mcp", "token", "revoke", token_id]
                )

        revoked = json.loads(revoked_stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["identityToken"]["state"], "revoked")

    def test_central_server_starts_mcp_and_auth_card_in_one_runtime(self):
        with TemporaryDirectory() as tmp:
            with (
                patch("agentbridge.cli.main.serve_central_mcp") as serve,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "central-serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8790",
                        "--auth-host",
                        "127.0.0.1",
                        "--auth-port",
                        "8780",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mcpUrl"], "http://127.0.0.1:8790/mcp")
        self.assertEqual(payload["authCardBaseUrl"], "http://127.0.0.1:8780")
        self.assertEqual(payload["authentication"], "bearer_identity_token")
        self.assertFalse(payload["sessionKeepalive"]["enabled"])
        self.assertEqual(payload["sessionKeepalive"]["intervalSeconds"], 0)
        serve.assert_called_once()
        self.assertEqual(serve.call_args.kwargs["keepalive_interval_seconds"], 0)
        self.assertEqual(
            serve.call_args.kwargs["keepalive_activity_lease_seconds"],
            604_800,
        )

    def test_central_server_starts_workspace_with_gateway_bff(self):
        with TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "gateway.token"
            token_file.write_text("gateway-token-value", encoding="utf-8")
            with (
                patch("agentbridge.cli.main.serve_central_mcp") as serve,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "central-serve",
                        "--workspace-host",
                        "127.0.0.1",
                        "--workspace-port",
                        "8783",
                        "--workspace-gateway-url",
                        "ws://127.0.0.1:18789",
                        "--workspace-gateway-token-file",
                        str(token_file),
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["workspaceBaseUrl"],
            "http://127.0.0.1:8783",
        )
        self.assertTrue(payload["workspaceGatewayConfigured"])
        self.assertIsNotNone(serve.call_args.kwargs["workspace_config"])
        self.assertIsNotNone(serve.call_args.kwargs["workspace_gateway"])

    def test_central_server_rejects_incomplete_or_orphaned_workspace_gateway(self):
        cases = [
            (
                ["--workspace-gateway-url", "ws://127.0.0.1:18789"],
                "configured together",
            ),
            (
                [
                    "--workspace-gateway-url",
                    "ws://127.0.0.1:18789",
                    "--workspace-gateway-token-file",
                    "gateway.token",
                ],
                "requires --workspace-port",
            ),
            (
                ["--workspace-port", "8790"],
                "different ports",
            ),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments), TemporaryDirectory() as tmp:
                with redirect_stdout(io.StringIO()) as stdout:
                    exit_code = main(
                        [
                            "--home",
                            tmp,
                            "mcp",
                            "central-serve",
                            *arguments,
                        ]
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    payload["error"]["code"],
                    "CENTRAL_MCP_CONFIG_INVALID",
                )
                self.assertIn(message, payload["error"]["message"])

    def test_central_server_enables_bounded_session_keepalive(self):
        with TemporaryDirectory() as tmp:
            with (
                patch("agentbridge.cli.main.serve_central_mcp") as serve,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "central-serve",
                        "--session-keepalive-interval",
                        "1200",
                        "--session-keepalive-lease",
                        "28800",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["sessionKeepalive"],
            {
                "enabled": True,
                "intervalSeconds": 1200.0,
                "activityLeaseSeconds": 28800.0,
            },
        )
        self.assertEqual(serve.call_args.kwargs["keepalive_interval_seconds"], 1200.0)
        self.assertEqual(
            serve.call_args.kwargs["keepalive_activity_lease_seconds"],
            28800.0,
        )

    def test_central_server_rejects_uncontrolled_keepalive_settings(self):
        cases = [
            (["--session-keepalive-interval", "45"], "must be 0 or between"),
            (
                [
                    "--session-keepalive-interval",
                    "1200",
                    "--session-keepalive-lease",
                    "600",
                ],
                "cannot be shorter",
            ),
        ]
        for arguments, expected_message in cases:
            with self.subTest(arguments=arguments), TemporaryDirectory() as tmp:
                with redirect_stdout(io.StringIO()) as stdout:
                    exit_code = main(
                        ["--home", tmp, "mcp", "central-serve", *arguments]
                    )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 2)
                self.assertEqual(
                    payload["error"]["code"],
                    "CENTRAL_MCP_CONFIG_INVALID",
                )
                self.assertIn(expected_message, payload["error"]["message"])

    def test_central_server_rejects_same_mcp_and_auth_port(self):
        with TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as stdout:
            exit_code = main(
                [
                    "--home",
                    tmp,
                    "mcp",
                    "central-serve",
                    "--port",
                    "8790",
                    "--auth-port",
                    "8790",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["error"]["code"], "CENTRAL_MCP_CONFIG_INVALID")

    def test_central_server_allows_explicit_private_http_and_warns(self):
        with TemporaryDirectory() as tmp:
            with (
                patch("agentbridge.cli.main.serve_central_mcp") as serve,
                redirect_stdout(io.StringIO()) as stdout,
                redirect_stderr(io.StringIO()) as stderr,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "mcp",
                        "central-serve",
                        "--host",
                        "10.20.30.40",
                        "--port",
                        "8790",
                        "--public-base-url",
                        "http://10.20.30.40:8790",
                        "--auth-host",
                        "10.20.30.40",
                        "--auth-port",
                        "8780",
                        "--auth-public-base-url",
                        "http://10.20.30.40:8780",
                        "--allow-insecure-private-http",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["mcpUrl"], "http://10.20.30.40:8790/mcp")
        self.assertEqual(payload["authCardBaseUrl"], "http://10.20.30.40:8780")
        self.assertTrue(payload["insecurePrivateHttp"])
        self.assertIn("without TLS", payload["securityWarning"])
        self.assertIn("never expose", payload["securityWarning"])
        self.assertIn("WARNING", stderr.getvalue())
        serve.assert_called_once()

    def test_standalone_auth_server_allows_explicit_private_http_and_warns(self):
        with TemporaryDirectory() as tmp:
            with (
                patch("agentbridge.cli.main.serve_auth_cards") as serve,
                redirect_stdout(io.StringIO()) as stdout,
                redirect_stderr(io.StringIO()) as stderr,
            ):
                exit_code = main(
                    [
                        "--home",
                        tmp,
                        "auth",
                        "serve",
                        "--host",
                        "10.20.30.40",
                        "--port",
                        "8780",
                        "--public-base-url",
                        "http://10.20.30.40:8780",
                        "--allow-insecure-private-http",
                    ]
                )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["publicBaseUrl"], "http://10.20.30.40:8780")
        self.assertTrue(payload["insecurePrivateHttp"])
        self.assertIn("without TLS", payload["securityWarning"])
        self.assertIn("WARNING", stderr.getvalue())
        serve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
