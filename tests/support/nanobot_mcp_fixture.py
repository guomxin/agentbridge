"""Isolated L1 MCP service for NanoBot host acceptance.

Run with python -m tests.support.nanobot_mcp_fixture --home <new-directory>.
Only synthetic downstream reads are possible. The local SDK runner may read the
two mode-0600 token files. Browser clients receive public evidence only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys

import uvicorn

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.mcp.resource_budget import BoundedMcpProtocol, bounded_mcp_app
from tests.authorization_fixtures import grant_permissions
from tests.support.skill_host_acceptance import (
    ALICE, BOB, SkillHostAcceptance, SyntheticReadService, SyntheticWorker,
)


def private_file(path: Path, contents: bytes):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)


def deny_outgoing_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise PermissionError("This fixture serves incoming loopback requests only")


class NanoBotMcpFixture:
    def __init__(self, home: Path, base: str, port: int, *,
                 trusted_card_origin="http://127.0.0.1:1", service_class=SyntheticReadService, reuse_existing=False,
                 subjects=None):
        home.mkdir(mode=0o700, parents=False, exist_ok=reuse_existing)
        self.home = home
        self.key = home / "session.key"
        if not reuse_existing:
            private_file(self.key, os.urandom(32))
        os.environ["AGENTBRIDGE_SESSION_KEY_FILE"] = str(self.key)
        self.service = service_class(
            home=home / "central",
            base_url="https://oa.invalid",
            trusted_card_base_url=trusted_card_origin,
            worker_factory=lambda session, _adapter: SyntheticWorker(session),
        )
        # Reuse the repository's read-only OA responses; the production service,
        # subject grants, operation ledger, MCP serialization and auth all run.
        self.calls = []
        self.service.adapter.invoke_capability = self.fixture_read
        self.service.adapter.probe_session = self.fixture_probe
        self.identities = McpIdentityTokenStore(self.service.db_path)
        self.subjects = {"a": ALICE, "b": BOB} if subjects is None else dict(subjects)
        if set(self.subjects) != {"a", "b"} or len(set(self.subjects.values())) != 2:
            raise ValueError("Expected two distinct isolated test subjects")
        self.tokens = {}
        for label, subject in self.subjects.items():
            if reuse_existing:
                identity = self.identities.verify((home / f"{label}.token").read_text().strip())
                if not identity or identity["user_subject"] != subject:
                    raise RuntimeError("Original isolated credential is unavailable")
                self.tokens[label] = identity["token_id"]
                continue
            grant_permissions(self.service.user_grants, subject, ["oa.workflow.read"] if label == "a" else [])
            session = self.service.sessions.get_or_create(user_subject=subject, system_id="oa", expected_principal_ref=subject)
            self.service.sessions.activate(session["session_id"], observed_principal_ref=subject)
            self.service.session_states.save(session["session_id"], {"cookies": []})
            token = self.identities.issue(user_subject=subject, expected_principal_ref=subject, ttl_seconds=7200)
            self.tokens[label] = token["token_id"]
            private_file(home / f"{label}.token", token["token"].encode("utf-8"))
        self.meta = {
            "endpoint": f"{base}/mcp",
            "subjects": self.subjects,
            "tokenFiles": {label: str(home / f"{label}.token") for label in self.subjects},
        }
        if not reuse_existing:
            private_file(home / "meta.json", json.dumps(self.meta).encode("utf-8"))
        config = validate_central_mcp_server_config(
            host="127.0.0.1", port=port, public_base_url=base, tls_cert=None, tls_key=None
        )
        mcp = create_central_mcp_server(
            service=self.service, identity_store=self.identities, config=config, auth_card_base_url=trusted_card_origin
        )
        self.app = bounded_mcp_app(mcp, self.identities)

    def fixture_read(self, capability, worker, arguments):
        # This existing fixture checks the permitted capability and synthetic
        # worker identity before returning its two synthetic OA records.
        return SkillHostAcceptance._invoke(self, capability, worker, arguments, expected_subject=self.subjects["a"])

    @staticmethod
    def fixture_probe(worker):
        return {
            "authenticated": True,
            "observed_principal_ref": worker.session["expected_principal_ref"],
            "transport": "synthetic-no-network",
        }

    def close(self):
        for label, token_id in self.tokens.items():
            self.identities.revoke(token_id)
            (self.home / f"{label}.token").unlink(missing_ok=True)
        self.key.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="New, private fixture directory")
    parser.add_argument("--telegram-bindings", action="store_true", help="Seed two existing, verified private Telegram peers")
    parser.add_argument("--capability-samples", action="store_true", help="Assign a sample skill to the first subject")
    args = parser.parse_args()
    home = args.home.resolve()
    os.umask(0o077)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        sys.addaudithook(deny_outgoing_network)
        fixture = NanoBotMcpFixture(home, base, port)
        try:
            if args.capability_samples:
                fixture.service.skills.save("user:" + fixture.subjects["a"],
                    {"oa-work-log":{"profiles":["preview"]}},expected_revision=0,
                    actor="isolated-test",reason="two-subject NanoBot skill catalog check")
            if args.telegram_bindings:
                from tests.support.nanobot_telegram_setup import seed_telegram_peers
                seed_telegram_peers(fixture)
            print(json.dumps(fixture.meta), flush=True)
            config = uvicorn.Config(fixture.app, http=BoundedMcpProtocol, ws="none", access_log=False, log_level="error")
            uvicorn.Server(config).run(sockets=[listener])
        finally:
            fixture.close()


if __name__ == "__main__":
    main()
