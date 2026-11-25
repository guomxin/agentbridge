import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from . import SDK_VERSION


def private_file(filename):
    path = Path(filename)
    if not path.is_absolute():
        raise ValueError("Expected absolute private file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Owner-only material required")
        value = stream.read(1_048_577)
        if len(value) > 1_048_576:
            raise ValueError("Invalid private configuration size")
        return value


def private_dir(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Expected absolute private directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Owner-only directory required")
    return path


class PiRuntime:
    def __init__(self, spec, state):
        self.state = state
        self.node = Path(spec["nodePath"])
        self.sdk = Path(spec["sdkDir"])
        if not self.node.is_absolute() or not self.sdk.is_absolute() or not os.access(self.node, os.X_OK):
            raise ValueError("Expected installed Pi SDK and Node")
        package = json.loads((self.sdk / "package.json").read_text())
        if (package.get("name"), package.get("version")) != ("@earendil-works/pi-coding-agent", SDK_VERSION):
            raise ValueError("Unexpected Pi SDK")
        self.model = {k: spec["model"][k] for k in ("provider", "id", "authFile")}
        if spec["model"].get("modelsFile"):
            self.model["modelsFile"] = spec["model"]["modelsFile"]
        for k in ("authFile", "modelsFile"):
            if k in self.model:
                private_file(self.model[k])
        self.images = False
        self.ready = False

    async def invoke(self, mode, *, subject="readiness", run=None, central=None, history=(), emit=None,
                     message="", images=(), system=None):
        run = run or str(uuid4())
        directory = private_dir(self.state / "workers" / hashlib.sha256(subject.encode()).hexdigest())
        agent_dir = private_dir(directory / "agent")
        env = {"HOME": str(directory), "PI_CODING_AGENT_DIR": str(agent_dir), "TMPDIR": str(private_dir(directory / "tmp")),
               "LANG": "en_US.UTF-8", "PATH": "/usr/bin:/bin"}
        payload = {"runId": run, "mode": mode, "model": self.model, "system": system, "message": message,
                   "history": history, "images": images, "tools": list(central.definitions.values()) if central else []}
        worker = Path(__file__).with_name("worker.mjs")
        proc = await asyncio.create_subprocess_exec(str(self.node), str(worker), str(self.sdk),
                cwd=directory, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=2_097_152)
        try:
            async with asyncio.timeout(100 if mode == "completion" else 10 if mode == "ready" else 240):
                async def write(value):
                    proc.stdin.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")
                    await proc.stdin.drain()
                await write(payload)
                final, total, tool_ids = None, 0, set()
                async for line in proc.stdout:
                    event = json.loads(line)
                    if event.get("runId") != run or final is not None:
                        raise RuntimeError("Invalid Pi worker frame")
                    kind = event.get("kind")
                    if kind == "ready" and mode == "ready":
                        final = event
                    elif kind == "final" and mode != "ready" and isinstance(event.get("text"), str) and len(event["text"]) <= 200_000:
                        final = event
                    elif kind == "delta" and mode == "chat" and isinstance(event.get("text"), str):
                        total += len(event["text"])
                        if total > 200_000:
                            raise RuntimeError("Pi output limit reached")
                        if not central or not central.governed_result and not (central.repair and central.repair.source):
                            emit("delta", event["text"])
                    elif kind == "tool" and mode == "chat" and central:
                        call_id, name = event["callId"], event["name"]
                        if not isinstance(call_id, str) or not 0 < len(call_id) < 256 or call_id in tool_ids or len(tool_ids) >= 64:
                            raise RuntimeError("Invalid Pi tool event")
                        tool_ids.add(call_id)
                        if name in central.definitions:
                            emit("tool_started", name)
                        text = await central.call(name, event["arguments"])
                        await central.require_binding()
                        if name in central.definitions:
                            emit("tool_finished", name)
                        await write({"runId": run, "kind": "result", "callId": call_id, "text": text,
                                     "visibleTools": central.visible_tools()})
                    else:
                        raise RuntimeError("Pi worker failed")
                if await proc.wait() or final is None:
                    raise RuntimeError("Pi worker did not return a final result")
                return final
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            proc.stdin.close()

    async def check(self):
        checked = await self.invoke("ready")
        self.images, self.ready = checked.get("images") is True, True
