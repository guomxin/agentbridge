"""End-to-end loopback transport check against an existing synthetic central MCP fixture."""

import argparse
import asyncio
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations" / "nanobot-agentbridge"))
sys.path.insert(0, str(ROOT))
from nanobot_agentbridge.server import create_app

from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient
from tests.support.nanobot_sdk_probe import MODEL, QUESTION, Scenario, model_server


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(data)


async def main(args):
    with tempfile.TemporaryDirectory(
        prefix="agentbridge-nanobot-transport-"
    ) as temporary:
        root = Path(temporary)
        service_file = root / "service.token"
        write_private(service_file, secrets.token_urlsafe(32))
        specs = []
        servers = []
        scenarios = []
        bindings = []
        runner = None
        try:
            for subject, label in [("skill-host-alice", "a"), ("skill-host-bob", "b")]:
                token_file = Path(args.token_dir) / f"{label}.token"
                scenario = Scenario(
                    subject, label == "a", token_file.read_text().strip()
                )
                if args.image_samples:
                    from tests.support.nanobot_image_probe import ImageScenario
                    scenario = ImageScenario(token_file.read_text().strip())
                if args.capability_samples:
                    from tests.support.nanobot_capability_probe import CatalogScenario

                    scenario = CatalogScenario(
                        label == "a", token_file.read_text().strip()
                    )
                provider = model_server(scenario)
                threading.Thread(target=provider.serve_forever, daemon=True).start()
                servers.append(provider)
                scenarios.append(scenario)
                config = root / f"{label}.json"
                api = {
                    "preferredApi": "chat_completions",
                    "supportedApis": ["chat_completions"],
                }
                write_private(
                    config,
                    json.dumps(
                        {
                            "providers": {
                                "custom": {
                                    "apiKey": "synthetic-only",
                                    "apiBase": f"http://127.0.0.1:{provider.server_port}/v1",
                                    "api": api,
                                }
                            },
                            "agents": {
                                "defaults": {
                                    "provider": "custom",
                                    "model": MODEL,
                                    "api": api,
                                    "maxToolIterations": 4,
                                }
                            },
                        }
                    ),
                )
                binding = {
                    "user_subject": subject,
                    "endpoint_id": label + "-endpoint-id",
                    "endpoint_key": label + "-endpoint",
                    "session_key": label + "-conversation",
                    "credential_ref": label + "-credential",
                }
                bindings.append(binding)
                if args.telegram or args.telegram_console:
                    sender = 501 if label == "a" else 502
                    binding["endpoint_key"] = f"telegram:8001:{sender}"
                specs.append(
                    {
                        **binding,
                        "tokenFile": str(token_file),
                        "modelConfigFile": str(config),
                    }
                )
            # Provision an intentional token/subject mismatch. The central host identity check must reject it.
            wrong = {
                **bindings[0],
                "endpoint_id": "wrong-id",
                "credential_ref": "wrong-credential",
            }
            specs.append({**specs[0], **wrong, "tokenFile": specs[1]["tokenFile"]})
            instance = "nanobot-transport-acceptance"
            app = create_app(
                {
                    "instanceId": instance,
                    "serviceTokenFile": str(service_file),
                    "mcpUrl": args.mcp_url,
                    "stateDir": str(root / "state"),
                    "bindings": specs,
                }
            )
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            client = NanoBotGatewayClient(
                url=f"http://127.0.0.1:{port}",
                instance_id=instance,
                token_file=service_file,
            )
            assert (await asyncio.to_thread(client.readiness))["available"]

            def normal(index):
                binding = bindings[index]
                dispatch = {"idempotency_key": "synthetic-original-" + str(index)}
                items = list(
                    client.send_dispatch(dispatch, binding, QUESTION, [], None)
                )
                assert items[0]["type"] == "accepted" and items[-1]["state"] == "final"
                answer = items[-1]["text"]
                assert "采购申请" in answer if index == 0 else "未获" in answer
                assert len([i for i in items if i.get("state") == "delta"]) >= 1
                # Simulates the central side having lost its first accept acknowledgement.
                evidence = client.dispatch_evidence(
                    dispatch, binding, dispatch["idempotency_key"]
                )
                assert (
                    evidence["observed_run_id"] == items[0]["runId"]
                    and evidence["final_text"] == answer
                )
                replay = list(
                    client.send_dispatch(dispatch, binding, QUESTION, [], None)
                )
                assert replay[0]["runId"] == items[0]["runId"]
                for bad in (
                    dict(binding, endpoint_key="wrong"),
                    dict(binding, credential_ref="mismatch"),
                ):
                    try:
                        client.dispatch_evidence(dispatch, bad, items[0]["runId"])
                    except GatewayRequestError as error:
                        assert error.code == "NANOBOT_BINDING_UNAVAILABLE"
                    else:
                        raise AssertionError("Cross-bound run observation was allowed")
                try:
                    list(
                        client.send_dispatch(dispatch, binding, "另一个问题", [], None)
                    )
                except GatewayRequestError as error:
                    assert error.code == "NANOBOT_DISPATCH_CONFLICT"
                else:
                    raise AssertionError("Changed payload reused accepted key")
                return {
                    "user": binding["user_subject"],
                    "answer": answer,
                    "real_deltas": len(items) - 2,
                    "lost_ack_lookup": True,
                    "idempotent_replay": True,
                    "changed_payload_rejected": True,
                }

            if args.image_samples:
                from tests.support.nanobot_image_probe import verify_images
                print(json.dumps(await verify_images(client,bindings,scenarios,root)))
                return
            if args.capability_samples:
                from tests.support.nanobot_capability_probe import capability_checks

                print(
                    json.dumps(
                        await capability_checks(
                            client, bindings, scenarios, specs, instance, args.mcp_url
                        ),
                        ensure_ascii=False,
                    )
                )
                return
            if args.telegram or args.telegram_console:
                if args.telegram_console:
                    from tests.support.nanobot_telegram_console import run_console

                    await run_console(
                        root / "telegram-console", client, bindings, QUESTION, scenarios
                    )
                    return
                from tests.support.nanobot_telegram_loopback import exercise_real_host

                pilot_home = root / "telegram"
                pilot_home.mkdir(mode=0o700)
                receipt = await asyncio.to_thread(
                    exercise_real_host, pilot_home, client, bindings, QUESTION
                )
                assert [scenario.tool_requested for scenario in scenarios] == [1, 1]
                print(json.dumps(receipt, ensure_ascii=False))
                return
            results = await asyncio.gather(
                *(asyncio.to_thread(normal, index) for index in range(2))
            )
            assert [s.tool_requested for s in scenarios] == [1, 1]
            dispatch = {"idempotency_key": "mismatched-central-bearer"}
            try:
                await asyncio.to_thread(
                    lambda: list(
                        client.send_dispatch(dispatch, wrong, QUESTION, [], None)
                    )
                )
            except GatewayRequestError as error:
                assert error.details.get("stage") == "observe"
            else:
                raise AssertionError("Mismatched credential was accepted by the SDK")
            evidence = await asyncio.to_thread(
                client.dispatch_evidence, dispatch, wrong, dispatch["idempotency_key"]
            )
            assert evidence["state"] == "unknown"
            assert [s.tool_requested for s in scenarios] == [1, 1]
            print(
                json.dumps(
                    {
                        "transport": "real authenticated NanoBot loopback service",
                        "provider": "synthetic local HTTP",
                        "downstream": "synthetic OA through central MCP",
                        "users": results,
                        "wrong_central_token_blocked_before_model": True,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            if runner:
                await runner.cleanup()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--token-dir", required=True)
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--telegram-console", action="store_true")
    parser.add_argument("--capability-samples", action="store_true")
    parser.add_argument("--image-samples", action="store_true")
    asyncio.run(main(parser.parse_args()))
