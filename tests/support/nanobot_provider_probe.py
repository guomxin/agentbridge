"""A real SDK/provider protocol fault must never become public assistant prose."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"integrations/nanobot-agentbridge"))


class ErrorEndpoint(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body=json.dumps({"error":{"message":"synthetic-provider-diagnostic-marker","type":"invalid_request_error"}}).encode()
        self.send_response(400)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def run():
    from nanobot.config.schema import Config
    from nanobot.config.loader import set_config_path
    from nanobot_agentbridge.runtime import Conversation, run_readonly
    class Session:
        async def call_tool(self,*args,**kwargs):
            return SimpleNamespace(isError=False,structuredContent={"agentToolAccess":{"allowedToolNames":[]}})
        async def list_tools(self,*args,**kwargs):
            return SimpleNamespace(tools=[],nextCursor=None)
    with tempfile.TemporaryDirectory(prefix="nanobot-error-privacy-") as tmp:
        directory=Path(tmp)
        os.environ["HOME"]=str(directory)
        cfg=directory/"config.json"
        set_config_path(cfg)
        httpd=ThreadingHTTPServer(("127.0.0.1",0),ErrorEndpoint)
        threading.Thread(target=httpd.serve_forever,daemon=True).start()
        config=Config.model_validate({"agents":{"defaults":{"model":"synthetic","provider":"custom","workspace":str(directory/"workspace")}},
            "providers":{"custom":{"apiBase":f"http://127.0.0.1:{httpd.server_port}/v1","apiKey":"synthetic"}}})
        config.bind_source_path(cfg)
        deltas=[]
        async def emit(delta):
            deltas.append(delta)
        try:
            await run_readonly(config,Session(),Conversation("alice","endpoint","chat"),"你好。",emit)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Provider failure was accepted as an answer")
        finally:
            httpd.shutdown();httpd.server_close()
        assert not deltas
        assert not any(b"synthetic-provider-diagnostic-marker" in p.read_bytes() for p in directory.rglob("*") if p.is_file())
        print(json.dumps({"http_status":400,"real_sdk":True,"no_success":True,"no_public_delta":True,"no_diagnostic_in_history":True}))


if __name__=="__main__":
    os.umask(0o077)
    asyncio.run(run())
