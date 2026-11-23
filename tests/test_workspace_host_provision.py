import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agentbridge.admin.application import AdminControlPlane
from agentbridge.admin.server import (
    create_admin_http_server,
    validate_admin_server_config,
)
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.host_instances import HostInstanceConflict
from tests.support.interaction_acceptance import free_port
from tests.test_admin_control_plane import PASSWORD, _cookies, _request
from tests.test_workspace import _create_account


class AdminHostProvisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.routes_file = self.home / "routes.json"
        key = self.home / "session.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        env = patch.dict(
            os.environ,
            {
                "AGENTBRIDGE_NANOBOT_ROUTES_FILE": str(self.routes_file),
                "AGENTBRIDGE_SESSION_KEY_FILE": str(key),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.routes = {
            "office-ref": {
                "url": "http://127.0.0.1:1",
                "instanceId": "office",
                "adapterVersion": "0.1.0",
                "tokenFile": str(self.home / "private.token"),
                "subjects": {"user-a": "credential-a"},
            }
        }
        self.write_routes()
        self.service = CentralCapabilityService(
            home=self.home, base_url="https://oa.invalid"
        )
        control = AdminControlPlane(
            service=self.service,
            identity_store=McpIdentityTokenStore(self.service.db_path),
        )
        self.control = control
        self.admin = control.accounts.create(
            username="operator",
            password=PASSWORD,
            role="admin",
            must_change_password=False,
        )
        self.auditor = control.accounts.create(
            username="reader",
            password=PASSWORD,
            role="auditor",
            must_change_password=False,
        )
        self.account = _create_account(
            self.service,
            user_subject="user-a",
            username="pilota",
            endpoint_key="telegram:a",
        )
        self.config = dict(
            instance_id="office",
            route_ref="office-ref",
            display_name="办公 NanoBot",
            state="enabled",
            subjects=["user-a"],
            expected_revision=0,
            reason="开通合成用户",
        )
        self.binding = dict(
            instance_id="office",
            account_id=self.account["account_id"],
            expected_revision=1,
            reason="开通独立主体",
        )

    def write_routes(self):
        self.routes_file.write_text(json.dumps({"routes": self.routes}))
        self.routes_file.chmod(0o600)

    def manage(self, action, body, actor=None):
        return self.control.configure_workspace_host(
            actor=actor or self.admin, request_ip="127.0.0.1", body=body, action=action
        )

    def test_http_authorization_origin_revision_and_no_secret_file_projections(self):
        port = free_port()
        origin = f"http://127.0.0.1:{port}"
        server = create_admin_http_server(
            config=validate_admin_server_config(
                host="127.0.0.1",
                port=port,
                public_base_url=origin,
                tls_cert=None,
                tls_key=None,
            ),
            control_plane=self.control,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def login(name):
            status, headers, _ = _request(
                port,
                "POST",
                "/api/login",
                body={"username": name, "password": PASSWORD},
                origin=origin,
            )
            self.assertEqual(status, 200)
            return _cookies(headers)

        admin = login("operator")
        auditor = login("reader")
        path = "/api/workspace-hosts"
        self.assertEqual(_request(port, "GET", path)[0], 401)
        self.assertEqual(_request(port, "GET", path, cookies=auditor)[0], 403)

        def post(data, cookies, csrf=True):
            return _request(
                port,
                "POST",
                path,
                body=data,
                cookies=cookies,
                origin=origin,
                csrf=cookies["agentbridge_admin_csrf"] if csrf else None,
            )

        self.assertEqual(post(self.config, admin, False)[0], 401)
        self.assertEqual(post(self.config, auditor)[0], 403)
        self.assertEqual(post(self.config, admin)[0], 200)
        self.assertEqual(post(self.config, admin)[0], 409)
        self.assertEqual(
            post({**self.config, "url": "http://arbitrary.invalid"}, admin)[0], 400
        )
        status, _, page = _request(port, "GET", path, cookies=admin)
        self.assertEqual(status, 200)
        self.assertIn("office-ref", str(page))
        self.assertNotIn("private.token", str(page))
        self.assertNotIn("credential-a", str(page))

    def test_subject_and_stable_endpoint_binding_atomic_audited_revocation(self):
        self.manage("save", self.config)
        with self.assertRaises(PermissionError):
            self.manage("bind", self.binding, self.auditor)
        with self.assertRaises(HostInstanceConflict):
            self.manage("bind", {**self.binding, "expected_revision": 0})
        with patch.object(
            self.control.audit, "append", side_effect=RuntimeError("audit write failed")
        ):
            with self.assertRaises(RuntimeError):
                self.manage("bind", self.binding)
        with self.service.tasks._connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM client_endpoints WHERE agent_host='nanobot'"
                ).fetchone()[0],
                0,
            )
        result = self.manage("bind", self.binding)
        self.assertEqual(
            set(result),
            {
                "user_subject",
                "instance_id",
                "endpoint_id",
                "endpoint_key",
                "session_key",
                "credential_ref",
            },
        )
        self.assertEqual(result, self.manage("bind", self.binding))
        with self.service.tasks._connect() as db:
            db.execute(
                "UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?",
                (result["endpoint_id"],),
            )
        with self.assertRaises(PermissionError):
            self.manage("bind", self.binding)
        self.routes_file.chmod(0o644)
        self.assertTrue(self.control.workspace_host_catalog(self.admin)["route_error"])
        self.assertEqual(
            self.manage(
                "save", {**self.config, "state": "revoked", "expected_revision": 1}
            )["state"],
            "revoked",
        )

    def test_separate_manifest_per_subject_and_instance_required_for_provision(self):
        self.manage("save", self.config)
        other = _create_account(
            self.service,
            user_subject="user-b",
            username="pilotb",
            endpoint_key="telegram:b",
        )
        with self.assertRaises(PermissionError):
            self.manage("bind", {**self.binding, "account_id": other["account_id"]})
        self.manage("save", {**self.config, "expected_revision": 1, "subjects": None})
        with self.assertRaises(PermissionError):
            self.manage(
                "bind",
                {
                    **self.binding,
                    "expected_revision": 2,
                    "account_id": other["account_id"],
                },
            )
        for fields in ({"instance_id": "another"}, {"route_ref": "unknown"}):
            with self.assertRaises(ValueError):
                self.manage("save", {**self.config, **fields})
        self.routes["office-ref"]["subjects"]["user-b"] = "credential-a"
        self.write_routes()
        with self.assertRaises(ValueError):
            self.manage("bind", {**self.binding, "expected_revision": 2})

    def test_probe_reports_checked_time_and_real_unavailability(self):
        self.manage("save", self.config)
        result = self.manage("probe", {"instance_id": "office"})
        self.assertFalse(result["available"])
        self.assertTrue(result["checked_at"])
        self.assertNotIn("private.token", str(result))


if __name__ == "__main__":
    unittest.main()
