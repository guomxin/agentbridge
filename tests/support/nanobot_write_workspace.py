"""Authenticated synthetic Workspace and a native form entry for the controlled-write probe."""

import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit

from agentbridge.admin.application import AdminControlPlane
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from agentbridge.workspace.server import (
    validate_workspace_server_config,
    create_workspace_http_server,
)
from tests.support.business_draft_acceptance import OWNER
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_mcp_fixture import private_file
from tests.support.workspace_host_acceptance import add_fixture_signin
from tests.test_workspace import FakeGateway, PASSWORD


def configure_workspace(root, fixture, instance, host_port, token_map):
    gateway = FakeGateway()
    gateway.diagnostics = lambda: {"configured": True}
    path = root / "routes.json"
    private_file(
        path,
        json.dumps(
            {
                "routes": {
                    instance: {
                        "url": f"http://127.0.0.1:{host_port}",
                        "instanceId": instance,
                        "adapterVersion": "0.3.2",
                        "telegram":{"botId":"8001","username":"fixture_bot"},
                        "tokenFile": str(root / "service.token"),
                        "subjects": {
                            subject: "cred-" + subject for subject in token_map
                        },
                    }
                }
            }
        ).encode(),
    )
    os.environ["AGENTBRIDGE_NANOBOT_ROUTES_FILE"] = str(path)
    control = AdminControlPlane(
        service=fixture.service,
        identity_store=McpIdentityTokenStore(fixture.service.db_path),
        workspace_gateway=gateway,
    )
    actor = control.accounts.create(
        username="writefixture",
        password=secrets.token_urlsafe(36) + "Ab9!",
        role="admin",
        must_change_password=False,
    )
    control.configure_workspace_host(
        actor=actor,
        request_ip="127.0.0.1",
        action="save",
        body={
            "instance_id": instance,
            "route_ref": instance,
            "display_name": "NanoBot 合成写入",
            "state": "enabled",
            "subjects": list(token_map),
            "expected_revision": 0,
            "reason": "合成写入隔离验收",
        },
    )
    with sqlite3.connect(fixture.service.db_path) as db:
        accounts = {
            subject: db.execute(
                "SELECT account_id FROM workspace_accounts WHERE user_subject=?",
                (subject,),
            ).fetchone()[0]
            for subject in token_map
        }
    records = []
    for subject, account in accounts.items():
        directory = control.workspace_hosts.directory
        directory.choices(account)
        binding = control.configure_workspace_host(
            actor=actor,
            request_ip="127.0.0.1",
            action="bind",
            body={
                "instance_id": instance,
                "account_id": account,
                "expected_revision": 1,
                "reason": "合成端点",
            },
        )
        directory.set_default(account, instance, expected_revision=1)
        records.append({k: v for k, v in binding.items() if k != "instance_id"})
    return records, WorkspaceApplication(
        service=fixture.service,
        gateway=gateway,
        host_routes=NanoBotGatewayClient.from_environment(),
    )


def start_workspace(fixture, workspace,controls=None,*,form_capability="oa.leave.prepare"):
    cards, unused_server = fixture.servers()
    unused_server.server_close()
    servers = [cards]
    links = {}
    for label, host, name in (
        ("a", "127.0.0.1", "cardreview"),
        ("b", "localhost", "cardother"),
    ):
        port = free_port()
        origin = f"http://{host}:{port}"
        cfg = validate_workspace_server_config(
            host="127.0.0.1",
            port=port,
            public_base_url=origin,
            tls_cert=None,
            tls_key=None,
        )
        server = create_workspace_http_server(config=cfg, application=workspace)
        add_fixture_signin(server, origin, {name: PASSWORD})

        def entry(base, expected_origin):
            class WriteEntry(base):
                def do_POST(self):
                    if self.path=="/fixture-restart" and controls:
                        return controls.handle(self,expected_origin)
                    return super().do_POST()

                def do_GET(self):
                    if self.path=="/fixture-restart" and controls:
                        return controls.handle(self,expected_origin)
                    if self.path != "/fixture-draft":
                        return super().do_GET()
                    if self.headers.get("Host") != urlsplit(expected_origin).netloc:
                        return self.send_error(403)
                    account = self._authenticate()
                    if account is None:
                        return
                    with sqlite3.connect(fixture.service.db_path) as db:
                        row = db.execute(
                            "SELECT submission_id FROM field_submissions WHERE user_subject=? AND capability_name=? "
                            "ORDER BY rowid DESC LIMIT 1",
                            (account["user_subject"],form_capability),
                        ).fetchone()
                    if account["user_subject"] != OWNER or not row:
                        return self.send_error(404)
                    self.send_response(303)
                    self.send_header(
                        "Location", fixture.card_url + "/flow/input/" + row[0]
                    )
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.end_headers()

            return WriteEntry

        server.RequestHandlerClass = entry(server.RequestHandlerClass, origin)
        links[label] = dict(
            signin=origin + "/fixture-signin", form=origin + "/fixture-draft"
        )
        if controls:
            links[label]["restart"]=origin+"/fixture-restart"
        servers.append(server)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return servers, links
