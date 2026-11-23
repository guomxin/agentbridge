"""Exercise the NanoBot service's L2 card, poll and resume with an isolated central database.

Use the repository interpreter for the driver and pass the separately installed NanoBot interpreter.
The broker only acknowledges a synthetic account; it has no network or business credentials.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations" / "nanobot-agentbridge"))
SUBJECT = "skill-host-alice"
ORIGIN = "https://synthetic-credentials.invalid"


def parse():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nanobot-python", type=Path)
    parser.add_argument("--client", type=Path)
    parser.add_argument("--initial-read", action="store_true")
    parser.add_argument("--expire", action="store_true")
    return parser.parse_args()


class CredentialModel:
    def __init__(self, initial_read=False, expire=False):
        self.initial_read = initial_read
        self.expire = expire
        self.failures = []
        self.calls = 0

    def reply(self, body):
        payload = json.dumps(body, ensure_ascii=False)
        if ORIGIN in payload or "Bearer " in payload:
            raise ValueError("private_material_sent_to_model")
        tools = [m for m in body["messages"] if m.get("role") == "tool"]
        self.calls += 1
        pending = "mcp_ab_oa_workflow_pending_list"
        names = {v["function"]["name"] for v in body["tools"]}
        if pending not in names:
            return "此账户无待办读取权限。", None
        if self.expire and tools:
            assert "过期" in str(tools[-1]["content"]) or "关闭" in str(
                tools[-1]["content"]
            )
            return "凭证卡已过期，请重新发起登录。", None
        if not tools and self.initial_read:
            return None, {
                "id": "initial-read",
                "type": "function",
                "function": {"name": pending, "arguments": '{"limit":3}'},
            }
        if not tools:
            assert "mcp_ab_oa_session_login" in {
                v["function"]["name"] for v in body["tools"]
            }
            return None, {
                "id": "login1",
                "type": "function",
                "function": {"name": "mcp_ab_oa_session_login", "arguments": "{}"},
            }
        if tools[-1].get("name") == pending or "采购申请" in str(tools[-1]["content"]):
            if not all(
                title in str(tools[-1]["content"]) for title in ("采购申请", "验收申请")
            ):
                raise ValueError("original_read_failed")
            return "已核验本次登录。您有采购申请、验收申请两项待办。", None
        if "凭证卡已由中央核验并续办" not in str(tools[-1]["content"]):
            raise ValueError("credential_resume_not_verified")
        return None, {
            "id": "read1",
            "type": "function",
            "function": {"name": pending, "arguments": '{"limit":3}'},
        }


async def client(spec):
    from aiohttp import web
    from agentbridge.workspace.gateway import GatewayRequestError
    from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
    from nanobot_agentbridge.server import create_app
    from tests.support.nanobot_sdk_probe import MODEL, model_server

    root = spec.parent
    binding = json.loads(spec.read_text())
    model = CredentialModel(binding["initial_read"], binding["expire"])
    httpd = model_server(model)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    private = root / "host-token"
    private.write_text(secrets.token_urlsafe(32))
    private.chmod(0o600)
    config = root / "model.json"
    config.write_text(
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
                        "apiBase": f"http://127.0.0.1:{httpd.server_port}/v1",
                        "apiKey": "synthetic",
                    }
                },
            }
        )
    )
    config.chmod(0o600)
    instance = "credential-host-l2"
    actual = binding["binding"]
    other = binding["other"]
    app = create_app(
        {
            "instanceId": instance,
            "serviceTokenFile": str(private),
            "mcpUrl": binding["url"],
            "trustedCardOrigins": [ORIGIN],
            "stateDir": str(root / "host"),
            "bindings": [
                {
                    **b,
                    "modelConfigFile": str(config),
                    "tokenFile": str(root / "central" / f"{who}.token"),
                }
                for b, who in [(actual, "a"), (other, "b")]
            ],
        }
    )
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    gateway = NanoBotGatewayClient(
        url="http://127.0.0.1:" + str(runner.addresses[0][1]),
        token_file=private,
        instance_id=instance,
    )

    def perform():
        dispatch = {"idempotency_key": "synthetic-credential-only"}
        private_url = ""
        for event in gateway.send_dispatch(
            dispatch, actual, "请查询我的办公待办，未登录时先办理登录。", [], None
        ):
            if event["type"] == "accepted":
                run = event["runId"]
            elif (
                event.get("kind") == "private_interaction"
                and event.get("status") == "waiting_private"
            ):
                assert not private_url
                view = gateway.private_card(dispatch, actual, run)
                assert view["available"] and view["kind"] == "credential"
                private_url = view["url"]
                assert private_url.startswith(ORIGIN + "/")
                assert gateway.private_card(dispatch, actual, run)["url"] == private_url
                try:
                    permitted = gateway.private_card(dispatch, other, run)
                except GatewayRequestError:
                    permitted = {"available": False}
                assert not permitted["available"]
                (root / "user-opened-synthetic-card").touch()
            assert private_url not in json.dumps(event) if private_url else True
        assert event.get("state") == "final" and event["text"] == (
            "凭证卡已过期，请重新发起登录。"
            if binding["expire"]
            else "已核验本次登录。您有采购申请、验收申请两项待办。"
        )
        assert not gateway.private_card(dispatch, actual, run)["available"]
        assert (
            model.calls in ({2} if binding["expire"] else {2, 3})
            and not model.failures
            and private_url
        )
        for file in (root / "host").rglob("*"):
            if file.is_file():
                assert private_url.encode() not in file.read_bytes()
        return {
            "profile": "nanobot 0.2.0 / L2",
            "card": "real central credential envelope",
            "transport": "MCP and SDK",
            "synthetic_broker": True,
            "presentation_refresh_same_card": True,
            "cross_subject_denied": True,
            "model_and_host_storage_no_url": True,
            "original_run_resumed": not binding["expire"],
            "expired_without_resume": binding["expire"],
            "initial_read": binding["initial_read"],
            "original_pending_query_verified": not binding["expire"],
        }

    try:
        print(json.dumps(await asyncio.to_thread(perform)))
    finally:
        await runner.cleanup()
        httpd.shutdown()
        httpd.server_close()


def credential_service():
    from agentbridge.core.central_service import CentralCapabilityService
    from tests.support.skill_host_acceptance import SyntheticReadService

    class CredentialService(SyntheticReadService):
        verified_resumes = 0

        def resume_interaction(self, *, user_subject, interaction_id, idempotency_key):
            entry = self.interactions.get(interaction_id, user_subject=user_subject)
            assert (
                entry["interaction_type"] == "credential"
                and entry["resume_spec"]["kind"] == "session_ready"
            )
            self.verified_resumes += 1
            return CentralCapabilityService.resume_interaction(
                self,
                user_subject=user_subject,
                interaction_id=interaction_id,
                idempotency_key=idempotency_key,
            )

    return CredentialService


def driver(nanobot_python, initial_read=False, expire=False):
    import uvicorn
    from agentbridge.mcp.resource_budget import BoundedMcpProtocol
    from tests.support.nanobot_mcp_fixture import NanoBotMcpFixture

    with tempfile.TemporaryDirectory(
        prefix="agentbridge-nanobot-real-credential-"
    ) as directory:
        root = Path(directory)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(32)
        port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        fixture = NanoBotMcpFixture(
            root / "central",
            base,
            port,
            trusted_card_origin=ORIGIN,
            service_class=credential_service(),
        )
        fixture.service.verified_resumes = 0
        uv = uvicorn.Server(
            uvicorn.Config(
                fixture.app,
                http=BoundedMcpProtocol,
                ws="none",
                access_log=False,
                log_level="error",
            )
        )
        thread = threading.Thread(
            target=uv.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        meta = {"url": base + "/mcp", "initial_read": initial_read, "expire": expire}
        for label, subject in fixture.subjects.items():
            session = fixture.service.sessions.find(
                user_subject=subject, system_id="oa"
            )
            fixture.service.sessions.mark_expired(session["session_id"])
            endpoint_key = "workspace-L2-" + label
            ep, _ = fixture.service.tasks.ensure_endpoint(
                user_subject=subject,
                token_id=fixture.tokens[label],
                agent_host="nanobot",
                endpoint_key=endpoint_key,
                client_type="web",
                external_subject=subject,
                conversation_ref="session-" + label,
            )
            meta["binding" if label == "a" else "other"] = {
                "user_subject": subject,
                "endpoint_id": ep["endpoint_id"],
                "endpoint_key": endpoint_key,
                "session_key": "session-" + label,
                "credential_ref": "credential-" + label,
            }
        spec = root / "client.json"
        spec.write_text(json.dumps(meta))
        proc = subprocess.Popen(
            [
                str(nanobot_python),
                "-I",
                str(Path(__file__).resolve()),
                "--client",
                str(spec),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 35
            while not (root / "user-opened-synthetic-card").exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Synthetic credential card was not presented")
                time.sleep(0.12)
            import sqlite3

            with sqlite3.connect(fixture.service.db_path) as db:
                cid, sid = db.execute(
                    "SELECT challenge_id, session_id FROM auth_challenges WHERE user_subject=? AND state='pending'",
                    (SUBJECT,),
                ).fetchone()
            store = fixture.service.challenges
            if expire:
                with sqlite3.connect(fixture.service.db_path) as db:
                    db.execute(
                        "UPDATE auth_challenges SET expires_at='2000-01-01T00:00:00Z' WHERE challenge_id=?",
                        (cid,),
                    )
            else:
                csrf = store.issue_csrf(cid)
                store.claim(cid, csrf_token=csrf, csrf_cookie=csrf)
                fixture.service.sessions.activate(sid, observed_principal_ref=SUBJECT)
                store.complete(cid, result={"observed_principal_ref": SUBJECT})
            stdout, stderr = proc.communicate(timeout=30)
            assert proc.returncode == 0 and fixture.service.verified_resumes == (
                0 if expire else 1
            )
            print(stdout.strip())
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            uv.should_exit = True
            thread.join(6)
            fixture.close()


if __name__ == "__main__":
    args = parse()
    if args.client:
        asyncio.run(client(args.client))
    else:
        driver(args.nanobot_python, args.initial_read, args.expire)
