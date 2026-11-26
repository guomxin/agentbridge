"""Compare the three real host adapters from the Workspace gateway event boundary."""

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
import tomllib

from agentbridge.workspace.gateway import OpenClawGatewayClient
from agentbridge.workspace.nanobot_gateway import NanoBotGatewayClient, PiGatewayClient
from tests.support.interaction_acceptance import free_port
from tests.support.nanobot_sdk_probe import MODEL, QUESTION, model_server
from tests.support.pi_sdk_probe import host_fixture, private
from tests.support.pi_workspace_acceptance import central_fixture

ROOT = Path(__file__).resolve().parents[2]
FACTS = ("采购申请", "验收申请")
DENIED = "本人未获待办查询权限。"


class ReadModel:
    """One deterministic response policy across SDKs; tool names and results still come from each host."""

    def __init__(self):
        self.failures = []
        self.requests = 0
        self.denials = []

    def reply(self, data):
        self.requests += 1
        last = data["messages"][-1]
        if last["role"] == "tool":
            result = str(last["content"])
            if all(title in result for title in FACTS):
                return "本人有采购申请和验收申请。", None
            if "permission" in result.lower() or "无权" in result or "未获" in result:
                self.denials.append("central_permission_denied")
                return DENIED, None
            raise ValueError("unrecognized_tool_response")
        names = [t["function"]["name"] for t in data.get("tools", ())]
        pending = next(
            (
                n
                for n in names
                if n in ("oa_workflow_pending_list", "mcp_ab_oa_workflow_pending_list")
            ),
            None,
        )
        if pending is None:
            self.denials.append("not_in_model_tools")
            return DENIED, None
        return None, {
            "id": "read_" + secrets.token_hex(6),
            "type": "function",
            "function": {"name": pending, "arguments": '{"limit":2}'},
        }


@contextmanager
def provider(model):
    server = model_server(model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def daemon(command, *, env, cwd, log):
    with log.open("wb") as out:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=out, stderr=out, start_new_session=True
        )
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@contextmanager
def openclaw_fixture(home, central, meta, args, model):
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    port = free_port()
    token_file = private(home / "gateway.token", secrets.token_urlsafe(40))
    env = {
        "PATH": str(args.node.parent) + ":/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "OPENCLAW_HOME": str(home),
        "OPENCLAW_STATE_DIR": str(home / "state"),
        "OPENCLAW_CONFIG_PATH": str(home / "config.json"),
        "OPENCLAW_DISABLE_BONJOUR": "1",
        "OPENCLAW_GATEWAY_PORT": str(port),
    }
    identities = []
    accounts = {}
    for label, subject in meta["subjects"].items():
        token_env = "AGENTBRIDGE_TEST_" + label.upper()
        env[token_env] = (central.home / (label + ".token")).read_text().strip()
        identities.append(
            {"channel": "telegram", "senderId": subject, "tokenEnv": token_env}
        )
        with central.service.workspace._connect() as db:
            accounts[label] = dict(
                db.execute(
                    "SELECT * FROM workspace_accounts WHERE user_subject=?", (subject,)
                ).fetchone()
            )
    plugin = "agentbridge-interactions"
    with provider(model) as url:
        private(
            home / "config.json",
            {
                "gateway": {
                    "mode": "local",
                    "bind": "loopback",
                    "port": port,
                    "auth": {"mode": "token", "token": Path(token_file).read_text()},
                    "controlUi": {"enabled": False},
                },
                "discovery": {"mdns": {"mode": "off"}},
                "browser": {"enabled": False},
                "tools": {"allow": [plugin], "elevated": {"enabled": False}},
                "skills": {"allowBundled": []},
                "agents": {
                    "defaults": {
                        "workspace": str(workspace),
                        "model": {"primary": "fixture/read", "fallbacks": []},
                        "skipBootstrap": True,
                    },
                    "list": [{"id": "main", "default": True, "skills": []}],
                },
                "models": {
                    "providers": {
                        "fixture": {
                            "baseUrl": url,
                            "apiKey": "fixture-only",
                            "api": "openai-completions",
                            "models": [
                                {
                                    "id": "read",
                                    "name": "read",
                                    "contextWindow": 64000,
                                    "maxTokens": 5000,
                                }
                            ],
                        }
                    }
                },
                "plugins": {
                    "allow": [plugin],
                    "load": {
                        "paths": [str(ROOT / "integrations/openclaw-agentbridge")]
                    },
                    "slots": {"memory": "none"},
                    "entries": {
                        plugin: {
                            "enabled": True,
                            "hooks": {"allowConversationAccess": True},
                            "config": {
                                "mcpUrl": meta["endpoint"],
                                "identityBindings": identities,
                                "autoPoll": False,
                                "syncTimeline": False,
                                "allowedCardOrigins": ["http://127.0.0.1:1"],
                                "toolScope": "oa_pending",
                            },
                        }
                    },
                },
            },
        )
        client = OpenClawGatewayClient(
            url=f"ws://127.0.0.1:{port}",
            token_file=token_file,
            state_dir=home / "gateway-client",
            node_executable=str(args.node),
        )
        with daemon(
            [args.node, args.openclaw, "gateway", "run"],
            env=env,
            cwd=workspace,
            log=home / "gateway.log",
        ) as process:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Isolated OpenClaw Gateway exited")
                try:
                    client.call("health", timeout_seconds=3)
                    break
                except RuntimeError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Isolated OpenClaw Gateway did not become ready")

            def send(label, key):
                account = accounts[label]
                grant = central.service.workspace.issue_gateway_grant(
                    account["account_id"]
                )
                return client.send_stream(
                    session_key=account["openclaw_session_key"],
                    endpoint_key=grant["endpoint_key"],
                    grant=grant["grant"],
                    message=QUESTION,
                    idempotency_key=key,
                    timeout_seconds=65,
                    retry_before_accept=False,
                )

            yield send


@contextmanager
def nanobot_fixture(home, central, meta, python, model):
    home.mkdir()
    instance, port = "nanobot-comparison", free_port()
    service_token = private(home / "server.token", secrets.token_urlsafe(40))
    names = {}
    with provider(model) as url:
        api = {
            "preferredApi": "chat_completions",
            "supportedApis": ["chat_completions"],
        }
        config = private(
            home / "model.json",
            {
                "providers": {
                    "custom": {"apiKey": "fixture-only", "apiBase": url, "api": api}
                },
                "agents": {
                    "defaults": {
                        "provider": "custom",
                        "model": MODEL,
                        "api": api,
                        "maxToolIterations": 4,
                    }
                },
            },
        )
        specs = []
        for label, subject in meta["subjects"].items():
            names[label] = {
                "user_subject": subject,
                "endpoint_id": label + "-web",
                "endpoint_key": "workspace:" + label,
                "session_key": label + "-comparison",
                "credential_ref": "nano-" + label,
            }
            specs.append(
                {
                    **names[label],
                    "tokenFile": str(central.home / (label + ".token")),
                    "modelConfigFile": config,
                }
            )
        cfg = private(
            home / "host.json",
            {
                "instanceId": instance,
                "listenPort": port,
                "stateDir": str(home / "state"),
                "serviceTokenFile": service_token,
                "mcpUrl": meta["endpoint"],
                "bindings": specs,
            },
        )
        client = NanoBotGatewayClient(
            url=f"http://127.0.0.1:{port}",
            instance_id=instance,
            token_file=Path(service_token),
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(ROOT / "integrations/nanobot-agentbridge"),
        }
        with daemon(
            [python, "-m", "nanobot_agentbridge.server", "--config", cfg],
            env=env,
            cwd=home,
            log=home / "host.log",
        ) as process:
            for _ in range(160):
                if process.poll() is not None:
                    raise RuntimeError("Isolated NanoBot exited")
                if client.readiness()["available"]:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Isolated NanoBot did not become ready")
            yield lambda label, key: client.send_dispatch(
                {"idempotency_key": key}, names[label], QUESTION, [], None
            )


def observation(central, name, label, round_no, send, model):
    owner = central.subjects[label]
    with central.service.workspace._connect() as db:
        highest = db.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM operations"
        ).fetchone()[0]
    start = time.monotonic()
    wall = datetime.now(timezone.utc)
    calls, requests, denials = len(central.calls), model.requests, len(model.denials)
    result = {
        "host": name,
        "subject": label,
        "round": round_no,
        "acceptMs": None,
        "firstTextMs": None,
        "businessFinalMs": None,
        "finalMs": None,
    }
    answer = None
    for event in send(label, secrets.token_hex(14)):
        elapsed = round((time.monotonic() - start) * 1000)
        if event.get("type") == "accepted":
            result["acceptMs"] = elapsed
        if (
            event.get("type") == "chat"
            and event.get("text")
            and event["state"] in ("delta", "final")
            and result["firstTextMs"] is None
        ):
            result["firstTextMs"] = elapsed
        if event.get("state") == "final":
            result["finalMs"] = elapsed
            answer = event["text"]
    if not answer or not all(
        result[key] is not None for key in ("acceptMs", "firstTextMs", "finalMs")
    ):
        raise RuntimeError(f"{name} {label} incomplete Workspace stream")
    reads = central.calls[calls:]
    with central.service.workspace._connect() as db:
        ops = list(
            db.execute(
                """SELECT status,finished_at FROM operations WHERE rowid>? AND user_subject=?
              AND capability_name='oa.workflow.pending.list' ORDER BY rowid""",
                (highest, owner),
            )
        )
    result.update(
        modelCalls=model.requests - requests,
        downstreamReads=len(reads),
        denial=model.denials[denials:],
    )
    if label == "a":
        assert (
            all(fact in answer for fact in FACTS)
            and len(reads) == 1
            and reads[0]["owner"] == owner
        ), result
        assert len(ops) == 1 and ops[0]["status"] == "succeeded", (
            result,
            [op["status"] for op in ops],
        )
        result["businessFinalMs"] = round(
            (datetime.fromisoformat(ops[0]["finished_at"]) - wall).total_seconds()
            * 1000
        )
    else:
        assert answer == DENIED and not reads and not ops and result["denial"], result
    return result


def comparison(args):
    os.umask(0o077)
    with (
        tempfile.TemporaryDirectory(prefix="three-host-read-") as temporary,
        ExitStack() as stack,
    ):
        root = Path(temporary)
        port = free_port()
        token = private(root / "pi.token", secrets.token_urlsafe(40))
        current = []
        meta, bindings = stack.enter_context(
            central_fixture(
                root,
                host_url=f"http://127.0.0.1:{port}",
                token_file=token,
                configure=current.append,
            )
        )
        central = current[0]
        models = {name: ReadModel() for name in ("openclaw", "nanobot", "pi")}
        openclaw = stack.enter_context(
            openclaw_fixture(root / "openclaw", central, meta, args, models["openclaw"])
        )
        nanobot = stack.enter_context(
            nanobot_fixture(
                root / "nanobot", central, meta, args.nanobot_python, models["nanobot"]
            )
        )
        stack.enter_context(
            host_fixture(
                root,
                args.node,
                args.pi_sdk,
                meta,
                bindings,
                port=port,
                token_file=token,
                engine_python=str(args.pi_python),
                model=models["pi"],
            )
        )
        pi = PiGatewayClient(
            url=f"http://127.0.0.1:{port}",
            instance_id="pi-office",
            adapter_version="0.1.4",
            token_file=Path(token),
        )
        probes = {
            "openclaw": openclaw,
            "nanobot": nanobot,
            "pi": lambda label, key: pi.send_dispatch(
                {"idempotency_key": key}, bindings[label], QUESTION, [], None
            ),
        }
        rows = []
        names = list(probes)
        for n in range(4):
            for name in names[n % 3 :] + names[: n % 3]:
                for label in ("a", "b"):
                    row = observation(
                        central, name, label, n, probes[name], models[name]
                    )
                    rows.append(row)
        assert not any(model.failures for model in models.values())
        return {
            "question": QUESTION,
            "coldMeaning": "first request per account after all host services were ready",
            "units": "milliseconds from Workspace gateway invocation",
            "crossChannelMs": None,
            "syntheticModel": True,
            "syntheticOA": True,
            "businessWrites": 0,
            "openclaw": json.loads(
                (args.openclaw.resolve().parent / "package.json").read_text()
            )["version"],
            "openclawPlugin": json.loads(
                (ROOT / "integrations/openclaw-agentbridge/package.json").read_text()
            )["version"],
            "nanobot": subprocess.check_output(
                [
                    args.nanobot_python,
                    "-c",
                    "import importlib.metadata;print(importlib.metadata.version('nanobot-ai'))",
                ],
                text=True,
            ).strip(),
            "pi": json.loads((args.pi_sdk / "package.json").read_text())["version"],
            "piAdapter": pi.readiness()["adapterVersion"],
            "nanoAdapter": tomllib.loads(
                (ROOT / "integrations/nanobot-agentbridge/pyproject.toml").read_text()
            )["project"]["version"],
            "rows": rows,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("node", "openclaw", "nanobot-python", "pi-sdk", "pi-python"):
        parser.add_argument("--" + arg, type=Path, required=True)
    print(json.dumps(comparison(parser.parse_args()), ensure_ascii=False))
