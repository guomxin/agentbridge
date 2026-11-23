"""Isolated browser fixture for a real Workspace, NanoBot host and central credential form."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
from urllib.parse import urlsplit

from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_credential_probe import CredentialModel, SUBJECT
from tests.support.nanobot_mcp_fixture import private_file
from tests.support.nanobot_sdk_probe import MODEL, model_server

SYNTHETIC_FIELD = "nanobot-fixture-only"


class CredentialWorkspace:
    def __init__(self, root, nanobot_python, cert=None, key=None):
        if bool(cert) != bool(key):
            raise ValueError("Supply both isolated TLS certificate and key")
        self.root = root
        self.python = nanobot_python
        self.stack = ExitStack()
        self.broker_port = free_port()
        self.broker = f"http://127.0.0.1:{self.broker_port}"
        self.tls = (cert, key)
        self.https_port = free_port() if cert else None
        self.trusted_origin = (
            f"https://localhost:{self.https_port}"
            if cert
            else "https://synthetic-credentials.invalid"
        )

    def start(self, mcp, bindings, alias, port):
        self.service = mcp.service
        for subject in mcp.subjects.values():
            session = self.service.sessions.find(user_subject=subject, system_id="oa")
            self.service.sessions.mark_expired(session["session_id"])

        def launch(server):
            self.stack.callback(server.server_close)
            self.stack.callback(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()

        auth_app = TrustedAuthApplication(
            challenge_store=self.service.challenges, broker=self
        )
        config = validate_auth_server_config(
            host="127.0.0.1",
            port=self.broker_port,
            public_base_url=self.broker,
            tls_cert=None,
            tls_key=None,
        )
        launch(create_auth_http_server(config=config, application=auth_app))
        if self.https_port:
            config = validate_auth_server_config(
                host="127.0.0.1",
                port=self.https_port,
                public_base_url=self.trusted_origin,
                tls_cert=self.tls[0],
                tls_key=self.tls[1],
            )
            launch(create_auth_http_server(config=config, application=auth_app))
        self.model = CredentialModel()
        model_server_instance = model_server(self.model)
        launch(model_server_instance)
        model_config = self.root / "model.json"
        private_file(
            model_config,
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": MODEL,
                            "provider": "custom",
                            "maxToolIterations": 5,
                        }
                    },
                    "providers": {
                        "custom": {
                            "apiBase": f"http://127.0.0.1:{model_server_instance.server_port}/v1",
                            "apiKey": "synthetic",
                        }
                    },
                }
            ).encode(),
        )
        spec = self.root / "sdk-host.json"
        subjects = {subject: label for label, subject in mcp.subjects.items()}
        private_file(
            spec,
            json.dumps(
                {
                    "instanceId": alias,
                    "listenPort": port,
                    "serviceTokenFile": str(self.root / "service.token"),
                    "stateDir": str(self.root / "host-state"),
                    "mcpUrl": mcp.meta["endpoint"],
                    "trustedCardOrigins": [self.trusted_origin],
                    "bindings": [
                        {
                            **b,
                            "modelConfigFile": str(model_config),
                            "tokenFile": mcp.meta["tokenFiles"][
                                subjects[b["user_subject"]]
                            ],
                        }
                        for b in bindings
                    ],
                }
            ).encode(),
        )
        bootstrap = (
            "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
            "runpy.run_module('nanobot_agentbridge.server',run_name='__main__')"
        )
        child = subprocess.Popen(
            [
                str(self.python),
                "-I",
                "-c",
                bootstrap,
                str(
                    Path(__file__).resolve().parents[2]
                    / "integrations/nanobot-agentbridge"
                ),
                "--config",
                str(spec),
            ],
            env={
                "HOME": str(self.root),
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(self.root),
                "LANG": "en_US.UTF-8",
            },
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop():
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

        self.stack.callback(stop)

    def authenticate(self, *, challenge_id, csrf_token, csrf_cookie, credentials):
        # Only fixed, deliberately public demo field values count as fixture evidence.
        ok = credentials and all(v == SYNTHETIC_FIELD for v in credentials.values())
        credentials.clear()
        record = self.service.challenges.get(challenge_id)
        if not ok or record["user_subject"] != SUBJECT:
            raise PermissionError("Unrecognized fixture account or demo fields")
        self.service.challenges.claim(
            challenge_id, csrf_token=csrf_token, csrf_cookie=csrf_cookie
        )
        self.service.sessions.activate(
            record["session_id"], observed_principal_ref=SUBJECT
        )
        self.service.challenges.complete(
            challenge_id, result={"observed_principal_ref": SUBJECT}
        )
        return {"status": "succeeded", "challengeId": challenge_id}

    def add_entry(self, server, origin):
        fixture = self
        base = server.RequestHandlerClass

        class CredentialEntry(base):
            def do_GET(self):
                if self.path != "/fixture-credential":
                    return super().do_GET()
                if self.headers.get("Host") != urlsplit(origin).netloc:
                    return self.send_error(403)
                account = self._authenticate()
                if account is None:
                    return
                with sqlite3.connect(fixture.service.db_path) as db:
                    row = db.execute(
                        "SELECT challenge_id FROM auth_challenges WHERE user_subject=? AND state='pending' "
                        "ORDER BY rowid DESC LIMIT 1",
                        (account["user_subject"],),
                    ).fetchone()
                if not row or account["user_subject"] != SUBJECT:
                    return self.send_error(404)
                self.send_response(303)
                self.send_header("Location", fixture.broker + "/auth/" + row[0])
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def log_message(self, *args):
                pass

        server.RequestHandlerClass = CredentialEntry

    def close(self):
        self.stack.close()
