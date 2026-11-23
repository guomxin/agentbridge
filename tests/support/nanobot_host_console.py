"""Loopback-only interactive acceptance console for the NanoBot SDK and run ledger.

Start the separate nanobot_mcp_fixture first. Provide its meta.json, the
dedicated NanoBot Python interpreter, and an empty private output directory.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
from contextlib import closing
import sys
from urllib.parse import urlsplit
from uuid import UUID

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
import uvicorn

from tests.support.nanobot_mcp_fixture import private_file

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "integrations/nanobot-agentbridge"))
from nanobot_agentbridge.runs import RunCoordinator


class HostConsole:
    def __init__(self, home: Path, metadata: Path, sdk_python: Path, origin: str):
        self.origin = origin
        self.home = home
        self.sdk_python = sdk_python
        self.meta = json.loads(metadata.read_text())
        self.central_ledger = metadata.parent / "central" / "agentbridge.db"
        uri = urlsplit(self.meta["endpoint"])
        if uri.scheme != "http" or uri.hostname != "127.0.0.1" or not uri.port or uri.path != "/mcp":
            raise ValueError("Use the isolated loopback MCP fixture")
        self.sessions = {}
        self.hold = asyncio.Event()
        self.ledger_dir = home / "adapter"
        self.coordinator = RunCoordinator(self.ledger_dir)
        self.restart_lock = asyncio.Lock()

    def binding(self, request):
        label = self.sessions.get(request.cookies.get("lab_user"))
        if label is None:
            raise PermissionError("Select a synthetic account")
        subject = self.meta["subjects"][label]
        return label, {"subject": subject, "endpoint": f"test-web:{label}", "conversation": f"nanobot:web:{label}"}

    async def execute(self, label, run_id, emit, hold):
        # Unique per SDK invocation, even if a regression executes the same run
        # twice. An independent counter survives adapter restarts.
        run_home = self.home / "sdk" / run_id
        run_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        invocation = run_home / secrets.token_hex(8)
        invocation.mkdir(mode=0o700)
        runner = ROOT / "tests/support/nanobot_sdk_probe.py"
        command = [str(self.sdk_python), "-I", str(runner), "--jsonl", "--user", self.meta["subjects"][label],
                   "--expected", "yes" if label == "a" else "no", "--mcp-url", self.meta["endpoint"],
                   "--token-file", self.meta["tokenFiles"][label], "--data-dir", str(invocation)]
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=invocation, env={"PATH":"/usr/bin:/bin", "LANG":"en_US.UTF-8", "HOME":str(invocation),
                                 "TMPDIR":str(invocation), "NO_PROXY":"*"}, start_new_session=True)
        try:
            receipt = None
            async with asyncio.timeout(110):
                async for line in proc.stdout:
                    event = json.loads(line)
                    if event.get("type") == "text.delta":
                        emit(event["delta"])
                    elif event.get("type") == "receipt":
                        receipt = event["receipt"]
                status = await proc.wait()
            if status or not receipt or not receipt.get("ok") or receipt.get("user") != self.meta["subjects"][label]:
                raise RuntimeError("Isolated SDK probe did not complete")
            allowed = ("pending_read_granted", "reopened_history", "pending_rows_verified", "forced_read_denied",
                       "model_requests_checked_for_bearer")
            summary = {key: receipt[key] for key in allowed if type(receipt.get(key)) in {bool, int}}
            card = receipt.get("synthetic_private_card", {})
            summary["synthetic_private_card"] = {key: card[key] for key in
                ("delivered_to_bound_endpoint", "model_and_disk_clean") if type(card.get(key)) is bool}
            summary["events"] = [name for name in receipt.get("events", []) if name in
                                 {"text.delta", "tool.started", "tool.completed", "run.completed"}]
            private_file(run_home / "receipt.json", json.dumps(summary).encode())
            if hold:
                # Inject the loss only after the actual SDK and MCP completed.
                # An adapter restart from here must not replay the request.
                await self.hold.wait()
            return receipt["answer"]
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()

    async def dispatch(self, request: Request):
        if request.headers.get("host") != self.origin.removeprefix("http://"):
            return JSONResponse({"error":"invalid host"}, status_code=403)
        if request.method == "POST" and request.headers.get("origin") != self.origin:
            return JSONResponse({"error":"same-origin POST required"}, status_code=403)
        path = request.url.path
        if path == "/" and request.method == "GET":
            return HTMLResponse(Path(__file__).with_suffix(".html").read_text(), headers={
                "Cache-Control":"no-store", "X-Frame-Options":"DENY", "Referrer-Policy":"no-referrer"})
        try:
            if path == "/login" and request.method == "POST":
                data = await request.json()
                if data.get("account") not in self.meta["subjects"]:
                    raise ValueError("unknown synthetic account")
                sid = secrets.token_urlsafe(32)
                self.sessions.pop(request.cookies.get("lab_user"), None)
                self.sessions[sid] = data["account"]
                reply = JSONResponse({"account":data["account"]})
                reply.set_cookie("lab_user", sid, httponly=True, samesite="strict")
                return reply
            label, binding = self.binding(request)
            if path == "/me" and request.method == "GET":
                return JSONResponse({"account":label})
            if path == "/run" and request.method == "POST":
                data = await request.json()
                request_key = str(UUID(data["requestKey"]))
                hold = data.get("hold", False)
                if type(hold) is not bool:
                    raise ValueError("invalid hold")
                digest = hashlib.sha256(json.dumps(["query", hold]).encode()).hexdigest()
                async with self.restart_lock:
                    run = self.coordinator.accept(**binding, request_key=request_key, payload_digest=digest,
                        execute=lambda run_id, emit: self.execute(label, run_id, emit, hold))
                if data.get("dropReceipt") is True:
                    return JSONResponse({"error":"simulated lost acceptance receipt"}, status_code=503)
                return JSONResponse(run)
            if path == "/restart" and request.method == "POST":
                async with self.restart_lock:
                    await self.coordinator.close()
                    self.coordinator = RunCoordinator(self.ledger_dir)
                return JSONResponse({"restarted":True})
            if path == "/run" and request.method == "GET":
                run_id = str(UUID(request.query_params["id"]))
                cursor = max(0, int(request.query_params.get("after", 0)))
                result = self.coordinator.snapshot(run_id, **binding)
                result["events"] = self.coordinator.events(run_id, after=cursor, **binding)
                directory = self.home / "sdk" / run_id
                result["sdkStarts"] = sum(item.is_dir() for item in directory.iterdir()) if directory.exists() else 0
                file = directory / "receipt.json"
                result["sdkEvidence"] = json.loads(file.read_text()) if file.exists() else None
                with closing(sqlite3.connect(self.central_ledger.as_uri() + "?mode=ro", uri=True)) as db:
                    result["subjectPendingOperations"] = db.execute(
                        "SELECT COUNT(*) FROM operations WHERE user_subject=? AND capability_name=?",
                        (binding["subject"], "oa.workflow.pending.list")).fetchone()[0]
                return JSONResponse(result, headers={"Cache-Control":"no-store"})
            return JSONResponse({"error":"not found"}, status_code=404)
        except PermissionError:
            return JSONResponse({"error":"select a synthetic account"}, status_code=401)
        except KeyError:
            return JSONResponse({"error":"not found for this account"}, status_code=404)
        except (ValueError, TypeError):
            return JSONResponse({"error":"invalid request or conflicting idempotency key"}, status_code=400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--mcp-meta", required=True, type=Path)
    parser.add_argument("--sdk-python", required=True, type=Path)
    options = parser.parse_args()
    os.umask(0o077)
    home = options.home.resolve()
    home.mkdir(mode=0o700, parents=False, exist_ok=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        console = HostConsole(home, options.mcp_meta, options.sdk_python, origin)

        @asynccontextmanager
        async def lifespan(app):
            yield
            await console.coordinator.close()

        server = Starlette(routes=[Route("/{path:path}", console.dispatch, methods=["GET","POST"])], lifespan=lifespan)
        print(json.dumps({"page":origin, "home":str(home)}), flush=True)
        uvicorn.Server(uvicorn.Config(server, ws="none", access_log=False, log_level="error")).run(sockets=[listener])


if __name__ == "__main__":
    main()
