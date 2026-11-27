"""Outbound-only candidate acceptance. Never calls getUpdates or changes production state.

Mock and live transports execute the same private v2 handler and durable consumer.
Live mode requires a private, expiring scope and the official fixed-candidate
Full/wheel verifier. OA and host execution remain synthetic, as reported.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from tests.support.workspace_query_acceptance import Fixture, INSTANCES, ROOT

sys.path.insert(0, str(ROOT / "integrations/nanobot-agentbridge"))
from agentbridge.core import workspace_query_delivery as central_delivery
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.result_view import TrustedResultViewApplication
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from nanobot_agentbridge.notifications import TerminalMessages, notifications
from nanobot_agentbridge.private_files import private_text
from nanobot_agentbridge.telegram import TelegramAPI, TelegramUnavailable


class LocalResults:
    """Keep the original signed ticket and production page in this fixture only.

    TLS mode preserves the production HTTPS buttons exactly. An explicitly
    separate mapping mode uses loopback ordinary URL buttons, which does not
    exercise Telegram's HTTPS Mini App container.
    """
    def __init__(self, fixture, *, tls_cert=None, tls_key=None):
        from tests.support.interaction_acceptance import free_port
        if bool(tls_cert) != bool(tls_key):
            raise ValueError("TLS card server needs both certificate and private key")
        self.mapping = not tls_cert
        self.issuer = fixture.trusted_card_origin
        port = free_port() if self.mapping else urlsplit(self.issuer).port
        self.origin = f"http://localhost:{port}" if self.mapping else self.issuer
        self.application = TrustedResultViewApplication(service=fixture.service)
        self.server = create_auth_http_server(config=validate_auth_server_config(host="127.0.0.1", port=port,
            public_base_url=self.origin, tls_cert=tls_cert, tls_key=tls_key),
            application=TrustedAuthApplication(challenge_store=fixture.service.challenges, broker=None),
            result_application=self.application)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def map(self, params):
        if not self.mapping:
            # The candidate renderer's HTTPS web_app and external URL buttons
            # are passed to Telegram byte-for-byte without a fixture rewrite.
            return params
        if "reply_markup" not in params:
            return params
        rows = params["reply_markup"].get("inline_keyboard")
        if not isinstance(rows, list):
            raise ValueError("Expected fixture result-card buttons")
        mapped = []
        for row in rows:
            buttons = []
            for button in row:
                raw = button.get("url") or button.get("web_app", {}).get("url")
                target = urlsplit(raw) if isinstance(raw, str) else None
                if (target is None or target.scheme != "https" or "https://" + target.netloc != self.issuer
                        or target.query or target.fragment or not re.fullmatch(r"/results/[A-Za-z0-9_-]{43}", target.path)):
                    raise ValueError("Only this fixture's exact result ticket can map to loopback")
                self.application.backend.read(target.path.removeprefix("/results/"))
                buttons.append({"text": button["text"], "url": self.origin + target.path})
            mapped.append(buttons)
        return {**params, "reply_markup": {"inline_keyboard": mapped}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def live_scope(filename, *, host):
    """Validate before loading any Keychain item or making a Telegram request."""
    scope_path = Path(filename).absolute()
    raw = private_text(scope_path)
    scope = json.loads(raw)
    if (scope.get("schema") != "agentbridge.workspace-outbound-acceptance.v1" or scope.get("hostType") != host
            or scope.get("source") != str(ROOT) or type(scope.get("expiresEpoch")) not in (int, float)
            or not time.time() < scope["expiresEpoch"] <= time.time() + 3600
            or any(type(scope.get(k)) is not int or not 0 < scope[k] < 2**53 for k in ("botId", "senderId"))):
        raise ValueError("Expected one current private candidate/channel scope")
    profile_path = Path(scope["profile"]["path"])
    if digest(profile_path) != scope["profile"]["sha256"]:
        raise ValueError("Reviewed runtime profile changed")
    profile = json.loads(private_text(profile_path))
    channel = profile["telegram"]
    if channel["botId"] != scope["botId"] or scope.get("userSubject") not in profile["subjects"]:
        raise ValueError("Channel is not owned by the reviewed subject")
    if host == "pi" and channel.get("allowedSenders") != {scope["userSubject"]: str(scope["senderId"])}:
        raise ValueError("Pi sender differs from the reviewed one-person channel")
    metadata = ROOT / (".gitrepo" if (ROOT / ".gitrepo").exists() else ".git")
    commit = subprocess.check_output(["git", f"--git-dir={metadata}", f"--work-tree={ROOT}", "rev-parse", "HEAD"],
                                     cwd=ROOT, text=True).strip()
    if commit != scope.get("candidateCommit"):
        raise ValueError("Candidate commit changed")
    verifier = [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/agentbridge_artifact.py"), "verify", "--root", str(ROOT)]
    artifact = json.loads(subprocess.check_output(verifier, cwd=ROOT, text=True))
    if artifact["sha256"] != scope.get("candidateWheelSha256"):
        raise ValueError("Candidate wheel differs from the reviewed scope")
    scope_hash = hashlib.sha256(raw.encode()).hexdigest()
    def authorized():
        return (time.time() < scope["expiresEpoch"] and hashlib.sha256(private_text(scope_path).encode()).hexdigest() == scope_hash
                and digest(profile_path) == scope["profile"]["sha256"])
    return scope, channel, authorized


class OutboundOnly:
    def __init__(self, api, *, bot, sender, authorized=lambda: True, local_results=None):
        self.api, self.bot, self.sender, self.authorized = api, bot, sender, authorized
        self.local_results = local_results
        self.receipts = []

    def call(self, method, **params):
        if method not in {"getMe", "getWebhookInfo", "sendMessage"}:
            raise ValueError("Outbound fixture never reads or acknowledges Telegram updates")
        if not self.authorized() or (method == "sendMessage" and params.get("chat_id") != self.sender):
            raise TelegramUnavailable("Original outbound scope is no longer authorized")
        if method == "sendMessage" and self.local_results:
            params = self.local_results.map(params)
        response = self.api.call(method, **params)
        if method == "sendMessage":
            self.receipts.append({"receipt": response.get("message_id"), "card": "reply_markup" in params,
                "textSha256": hashlib.sha256(params["text"].encode()).hexdigest(),
                "textLength": len(params["text"]), "sentAt": time.time()})
        return response


class MockBot:
    def __init__(self, bot, sender):
        self.bot, self.sender, self.count = bot, sender, 0
        self.messages = []

    def call(self, method, **params):
        if method == "getMe":
            return {"is_bot": True, "id": self.bot}
        if method == "getWebhookInfo":
            return {"url": ""}
        if method != "sendMessage" or params.get("chat_id") != self.sender:
            raise ValueError("Unexpected mock transport action")
        self.count += 1
        self.messages.append(params)
        return {"message_id": self.count, "chat": {"id": self.sender, "type": "private"}, "from": {"id": self.bot}}


class Gateway:
    """Synthetic transport only; the private claim and presentation handler is unchanged."""
    def __init__(self, fixture, host, binding, record):
        self.fixture, self.host_type, self.binding, self.record = fixture, host, binding, record
        self.directory = fixture.home / (host + "-private-control")
        self.directory.mkdir(mode=0o700)
        self.req = {"hostType": host, "endpoint": binding["endpoint_id"], "endpointKey": binding["endpoint_key"],
            "subject": fixture.owner, "instance": INSTANCES[host], "conversation": binding["session_key"],
            "pairingRef": binding["credential_ref"].removeprefix("tg-"), "userDir": str(self.directory)}

    def _request(self, route, body, **options):
        if body.get("binding") != self.binding:
            raise PermissionError("Synthetic transport requires its original private binding")
        scope = {"user_subject": self.fixture.owner, "agent_host": self.host_type,
                 "host_instance_id": INSTANCES[self.host_type], "endpoint_key": self.binding["endpoint_key"]}
        if route not in {"telegram-notifications", "telegram-workspace-result-view"}:
            raise ValueError("Unsupported outbound fixture route")
        async def call(name, args, meta):
            if name == "agentbridge_host_notification_claim":
                if args.get("include_workspace_query_v2") is not True:
                    raise ValueError("Candidate did not opt into v2 delivery")
                response = self.fixture.service.claim_host_notifications(**scope,
                    query_token_id=self.fixture.tokens[self.host_type, self.fixture.owner])
                # This acceptance channel is deliberately scoped to v2 query
                # and text messages. Legacy consumers retain separate coverage.
                response["notifications"] = [item for item in response["notifications"]
                    if item.get("deliveryMode") in {"workspace_query_v2", "workspace_text_v2"}]
            elif name == "agentbridge_host_workspace_result_view":
                response = central_delivery.handle(self.fixture.service, subject=self.fixture.owner,
                    instance=INSTANCES[self.host_type], host=args.pop("agent_host"),
                    token_id=self.fixture.tokens[self.host_type, self.fixture.owner], **args)
            else:
                raise ValueError("Unsupported private fixture control")
            return SimpleNamespace(isError=False, structuredContent=response)
        @asynccontextmanager
        async def paired_session(req):
            yield SimpleNamespace(call_tool=call), {}, "L3"
        host = SimpleNamespace(coordinator=True, worker_payload=lambda record, run: self.req,
                               card_origins=frozenset({self.fixture.trusted_card_origin}))
        return asyncio.run(notifications(host, self.record, route, body, paired_session=paired_session))


class Channel:
    def __init__(self, fixture, host, api):
        bot, sender = (int(v) for v in fixture.channel_identities[host])
        links = TelegramPilotLinks(fixture.service)
        endpoint = links.ensure_endpoint(fixture.owner, fixture.tokens[host, fixture.owner], INSTANCES[host], bot, sender)
        self.binding = {"user_subject": fixture.owner, "endpoint_id": endpoint["endpointId"], "endpoint_key": endpoint["endpointKey"],
                        "session_key": endpoint["conversationRef"], "credential_ref": "tg-" + endpoint["pairingRef"]}
        record = {**self.binding, "workspaceEndpointId": endpoint["workspaceEndpointId"],
                  "workspaceEndpointKey": endpoint["workspaceEndpointKey"], "tokenFile": fixture.token_files[host, fixture.owner]}
        self.digest = hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()
        self.db = sqlite3.connect(fixture.home / (host + "-outbound.sqlite3"))
        self.db.row_factory = sqlite3.Row
        def current(chat):
            fresh = links.ensure_endpoint(fixture.owner, fixture.tokens[host, fixture.owner], INSTANCES[host], bot, sender)
            return (self.binding, self.digest) if fresh["pairingRef"] == endpoint["pairingRef"] else (None, None)
        self.pilot = SimpleNamespace(db=self.db, bot_id=bot, api=api, host_type=host, _binding=current,
            gateway=Gateway(fixture, host, self.binding, record), navigation=SimpleNamespace(remember=lambda *args: None))
        self.terminal, self.sender = TerminalMessages(self.pilot), sender

    def poll(self):
        self.terminal.poll(self.sender, self.binding, self.digest)
        self.terminal.flush_deferred()

    def report(self):
        return [dict(row) for row in self.db.execute("""SELECT d.dedupe_key,d.state,d.receipt,a.acknowledged
            FROM telegram_card_deliveries d JOIN telegram_workspace_query_attempts a
            ON d.bot=a.bot AND d.chat=a.chat AND d.delivery=a.delivery ORDER BY d.rowid""")]

    def close(self):
        self.terminal.workspace_queries.executor.shutdown()
        if self.terminal.workspace_results:
            self.terminal.workspace_results.executor.shutdown()
        self.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("nanobot", "pi"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-scope", type=Path)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--loopback-card-map", action="store_true",
                        help="Extra fixture-only mapping check; not original HTTPS card click acceptance")
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--self-check", action="store_true", help="Queue two synthetic reads in mock mode only")
    args = parser.parse_args()
    if not 5 <= args.seconds <= 480 or args.output.exists():
        parser.error("Use a new output directory and a 5..480 second window")
    if (bool(args.tls_cert) != bool(args.tls_key) or (args.loopback_card_map and args.tls_cert)
            or (args.live_scope and not args.loopback_card_map and not args.tls_cert) or (args.live_scope and args.self_check)):
        parser.error("Live original cards require trusted local TLS; mapping must be explicitly selected")
    os.umask(0o077)
    scope = None
    if args.live_scope:
        scope, credential, authorized = live_scope(args.live_scope, host=args.host)
        mode = "loopback_mapped" if args.loopback_card_map else "https_original"
        if scope.get("cardMode") != mode:
            raise ValueError("Card transport differs from the reviewed scope")
        if args.tls_cert and (digest(args.tls_cert) != scope.get("tlsCertificateSha256")
                             or digest(args.tls_key) != scope.get("tlsKeySha256")):
            raise ValueError("Card TLS files differ from the reviewed scope")
        from nanobot_agentbridge.telegram_credentials import bot_token
        bot, sender = scope["botId"], scope["senderId"]
        raw_api = TelegramAPI(bot_token({"botCredential": credential["botCredential"]}), private_sender=lambda chat: chat == sender and authorized())
    else:
        bot, sender, authorized = (8001 if args.host == "nanobot" else 8801), 501, lambda: True
        raw_api = MockBot(bot, sender)
    api = OutboundOnly(raw_api, bot=bot, sender=sender, authorized=authorized)
    me, webhook = api.call("getMe"), api.call("getWebhookInfo")
    if me.get("is_bot") is not True or me.get("id") != bot or webhook.get("url"):
        raise ValueError("Telegram identity or webhook differs from the reviewed private Bot")
    args.output.mkdir(mode=0o700)
    from tests.support.interaction_acceptance import free_port
    origin = f"https://localhost:{free_port()}" if args.tls_cert else "https://synthetic.invalid"
    fixture = Fixture(args.output / "fixture", deliveries=False,
        channel_identities={args.host: (str(bot), str(sender))}, trusted_card_origin=origin)
    cards = LocalResults(fixture, tls_cert=args.tls_cert, tls_key=args.tls_key)
    api.local_results = cards
    channel = Channel(fixture, args.host, api)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    account = fixture.accounts[fixture.owner]
    choices = fixture.service.workspace.hosts.choices(account["account_id"])
    selected = fixture.service.workspace.hosts.set_default(account["account_id"], INSTANCES[args.host], expected_revision=choices["revision"])
    public = {"transport": "live_outbound_only" if scope else "mock_bot_api", "getUpdatesCalls": 0,
        "versions": central_delivery.VERSIONS, "signin": fixture.signin,
        "host": args.host, "syntheticHostAndOa": True, "cardOrigin": cards.origin,
        "cardPresentation": "loopback mapping; original HTTPS clicks untested" if cards.mapping else "original HTTPS buttons unchanged",
        "cardArrivalObserved": False, "originalCardClickObserved": False, "mappedCardClickObserved": False,
        "hostControlTransport": "synthetic direct private-handler session; SDK qualification is separate",
        "candidateCommit": scope["candidateCommit"] if scope else None}
    print(json.dumps(public, ensure_ascii=False), flush=True)
    try:
        if args.self_check:
            fixture.app.send_chat_stream(account, message="请列出本次合成待办并说明数量", idempotency_key=uuid4().hex,
                instance_id=INSTANCES[args.host], expected_revision=selected["revision"])
            fixture.app.query_oa_pending(account, {"requestKey": uuid4().hex, "instanceId": INSTANCES[args.host],
                "expectedRevision": selected["revision"], "userMessage": "查询OA待办"})
        deadline = time.monotonic() + args.seconds
        while not stopped.wait(.1) and time.monotonic() < deadline:
            channel.poll()
            report = {**public, "receipts": list(api.receipts), "ledger": channel.report()}
            (args.output / "outbound-receipts.json").write_text(json.dumps(report, ensure_ascii=False))
            if args.self_check and len(report["ledger"]) == 5 and all(row["acknowledged"] for row in report["ledger"]):
                assert len(api.receipts) == 5 and sum(row["card"] for row in api.receipts) == 2
                assert all(row["state"] == "delivered" for row in report["ledger"])
                print(json.dumps({"selfCheck": "passed", "queryCards": 2, "publicTexts": 3, "getUpdatesCalls": 0}), flush=True)
                break
        else:
            if args.self_check:
                raise RuntimeError("Mock consumer did not acknowledge all five original deliveries")
    finally:
        channel.close()
        cards.close()
        fixture.close()
        if isinstance(raw_api, TelegramAPI):
            raw_api.close()


if __name__ == "__main__":
    main()
