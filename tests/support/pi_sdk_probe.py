"""Real Pi 1.6.0, central MCP and two principals; loopback model and synthetic read-only OA.

--serve keeps the fixture up for a Workspace client. The emitted path refers to
owner-only fixture metadata; credentials themselves are never printed.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

import httpx

from tests.support.nanobot_sdk_probe import model_server

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "integrations/pi-agentbridge"


def private(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        stream.write(value if isinstance(value, str) else json.dumps(value))
    return str(path)


class Model:
    def __init__(self):
        self.failures = []
        self.model_inputs = 0
        self.business_calls = 0
        self.builtins = set()
        self.history_checks = 0

    def reply(self, data):
        self.model_inputs += 1
        names = {tool["function"]["name"] for tool in data.get("tools") or []}
        self.builtins |= names - {"agentbridge_server_profile", "oa_session_status", "oa_workflow_pending_list"}
        if self.builtins:
            raise ValueError("unexpected_native_or_control_tool")
        messages = data["messages"]
        if not names:
            if any(m.get("role") == "tool" for m in messages):
                raise ValueError("sample_received_business_tools")
            return "独立样例。", None
        if messages[-1].get("role") == "tool":
            if not all(s in str(messages[-1]["content"]) for s in ("采购申请", "验收申请")):
                raise ValueError("missing_checked_central_read")
            return "本人有采购申请和验收申请。", None
        if "oa_workflow_pending_list" not in names:
            assert "采购申请和验收申请" not in str(messages)
            return "本人未获待办查询权限。", None
        if "只回复" in str(messages[-1]["content"]):
            assert any(m["role"] == "assistant" and "采购申请和验收申请" in str(m["content"]) for m in messages[:-1])
            self.history_checks += 1
            return "只读对话。", None
        self.business_calls += 1
        return None, {"id": "call_" + uuid4().hex, "type": "function",
                      "function": {"name": "oa_workflow_pending_list", "arguments": '{"limit":3}'}}


@contextmanager
def host_fixture(directory, node, sdk, meta, accounts, *, port, token_file, model=None, options=None, engine_python=None):
    model = Model() if model is None else model
    fake = model_server(model)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    children = []
    try:
        service = str(token_file)
        model_auth = private(directory / "model-auth.json", "{}")
        models = private(directory / "models.json", {"providers": {"test-pi": {
            "api": "openai-completions", "baseUrl": f"http://127.0.0.1:{fake.server_port}/v1", "apiKey": "fixture",
            "models": [{"id": "pi-fixture", "input": ["text", "image"], "contextWindow": 64000, "maxTokens": 8000}],
        }}})
        spec = {"instanceId": "pi-office", "listenPort": port, "serviceTokenFile": service,
                "stateDir": str(directory / "pi-state"), "nodePath": str(node), "sdkDir": str(sdk),
                "mcpUrl": meta["endpoint"], "model": {"provider": "test-pi", "id": "pi-fixture", "authFile": model_auth, "modelsFile": models},
                "bindings": [{**record, "tokenFile": meta["tokenFiles"][label]} for label, record in accounts.items()],
                **(options or {})}
        config = private(directory / "host.json", spec)
        python = engine_python or os.getenv("AGENTBRIDGE_PI_TEST_PYTHON") or sys.executable
        pythonpath = os.pathsep.join(str(ROOT / path) for path in (
            "integrations/pi-agentbridge", "integrations/nanobot-agentbridge", "integrations/reference-host", "."))
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=url, trust_env=False, timeout=110,
                          headers={"Authorization": "Bearer " + Path(service).read_text()}) as client:
            def post(action, **values):
                return client.post("/v1/" + action, json={"instance": "pi-office", **values})
            def start_host():
                host = subprocess.Popen([python, "-m", "pi_agentbridge.server", "--config", config],
                                    env={**os.environ, "PYTHONPATH": pythonpath}, cwd=PACKAGE, start_new_session=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                children.append(host)
                for _ in range(250):
                    if host.poll() is not None:
                        raise RuntimeError("Pi fixture host exited before readiness")
                    try:
                        if post("ready").json().get("available"):
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError("Pi fixture did not become ready")
            def crash_host():
                active = children[-1]
                assert active.poll() is None and os.getpgid(active.pid) == active.pid
                os.killpg(active.pid, signal.SIGKILL)
                active.wait(timeout=8)
            start_host()
            post.start_host = start_host
            post.crash_host = crash_host
            info = private(directory / "fixture.json", {**meta, "url": url, "tokenFile": service, "instanceId": "pi-office",
                "mcpUrl": meta["endpoint"], "subjects": meta["subjects"],
                "tokenFiles": meta["tokenFiles"], "bindings": accounts, "config": config, "sdk": str(sdk)})
            yield post, model, accounts, Path(info)
    finally:
        for child in reversed(children):
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        fake.shutdown()
        fake.server_close()
        thread.join()


@contextmanager
def pi_fixture(directory, node, sdk):
    from tests.support.pi_workspace_acceptance import central_fixture

    token = private(directory / "route.token", secrets.token_urlsafe(40))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with central_fixture(directory, host_url=f"http://127.0.0.1:{port}", token_file=token) as (meta, accounts):
        with host_fixture(directory, node, sdk, meta, accounts, port=port, token_file=token) as running:
            yield running


def verify(post, model, accounts):
    assert post("ready").json()["hostType"] == "pi"
    runs = {}
    for label, binding in accounts.items():
        key = "pi-sdk-" + label
        payload = {"binding": binding, "requestKey": key}
        accepted = post("accept", **payload, message="查本人待办")
        assert accepted.status_code == 200, accepted.status_code
        run = accepted.json()["runId"]
        assert post("accept", **payload, message="查本人待办").json()["runId"] == run
        assert post("accept", **payload, message="不同请求").status_code == 409
        for _ in range(360):
            row = post("lookup", **payload).json()
            if row["state"] != "accepted":
                break
            time.sleep(.1)
        assert row["state"] == "completed", (label, row["state"], model.failures)
        assert ("采购申请" if label == "a" else "未获") in row["answer"]
        events = post("events", **payload, runId=run, after=0).json()["events"]
        assert any(e["kind"] == "delta" for e in events) and events[-1]["kind"] == "completed"
        assert post("events", **payload, runId=run, after=events[-1]["sequence"]).json()["events"] == []
        runs[label] = run
    assert post("events", binding=accounts["b"], requestKey="pi-sdk-a", runId=runs["a"]).status_code == 403
    again = {"binding": accounts["a"], "requestKey": "second-distinct-request"}
    assert post("accept", **again, message="请接着前一轮，只回复一句。").status_code == 200
    for _ in range(200):
        second = post("lookup", **again).json()
        if second["state"] != "accepted":
            break
        time.sleep(.1)
    assert (second["state"], second["answer"], model.history_checks) == ("completed", "只读对话。", 1), (second["state"],model.history_checks)
    assert post("skill-model", binding=accounts["a"], system="文本试用", prompt="模型样例").json()["execution"] == "tool_free_completion"
    assert not model.failures and not model.builtins and model.business_calls == 1
    return {"passed": True, "sdk": "1.6.0", "adapter": "0.1.6", "subjects": 2, "businessReads": model.business_calls,
            "syntheticModel": True, "syntheticDownstream": True}


def verify_readiness(post, accounts, files):
    paths = {label: Path(path) for label, path in files.items()}
    originals = {label: path.read_bytes() for label, path in paths.items()}
    try:
        paths["a"].write_text(secrets.token_urlsafe(40))
        post.crash_host()
        post.start_host()
        partial = post("ready").json()
        assert (partial["available"], partial["verifiedBindings"], partial["configuredBindings"], partial["centralConnection"]) == (True, 1, 2, "partial")
        assert post("accept", binding=accounts["a"], requestKey="withdrawn", message="本人只读").status_code == 403
        assert post("skill-model", binding=accounts["b"], system="隔离评测", prompt="仅回复一句").status_code == 200
        paths["b"].write_text(secrets.token_urlsafe(40))
        assert post("accept", binding=accounts["b"], requestKey="withdrawn", message="本人只读").status_code == 403
        none = post("ready").json()
        assert (none["available"], none["verifiedBindings"], none["centralConnection"], none["acceptedLevel"], none["capabilities"]) == (False, 0, "unavailable", None, [])
        for label, path in paths.items():
            path.write_bytes(originals[label])
            assert post("skill-model", binding=accounts[label], system="隔离评测", prompt="仅回复一句").status_code == 200
        restored = post("ready").json()
        assert restored["available"] and restored["verifiedBindings"] == 2 and restored["centralConnection"] == "verified"
        return {"oneRevokedAvailable": True, "bothRevokedUnavailable": True, "restoredBindings": 2}
    finally:
        for label, path in paths.items():
            path.write_bytes(originals[label])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", required=True, type=Path)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pi-sdk-probe-") as tmp:
        with pi_fixture(Path(tmp), args.node.resolve(), args.sdk_dir.resolve()) as (post, model, accounts, info):
            if args.serve:
                print(json.dumps({"fixtureFile": str(info)}), flush=True)
                threading.Event().wait()
            else:
                result = verify(post, model, accounts)
                result.update(verify_readiness(post, accounts, json.loads(info.read_bytes())["tokenFiles"]))
                print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
