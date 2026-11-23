"""Private controls for the isolated NanoBot crash acceptance process."""
import html
import json
import os
import secrets
import signal
import sqlite3
import threading
from urllib.parse import parse_qs,urlsplit

from tests.support.business_draft_acceptance import OWNER


class RestartControls:
    def __init__(self,fixture,model,process,start_host,gateway):
        self.fixture=fixture
        self.model=model
        self.process=process
        self.start_host=start_host
        self.gateway=gateway
        self.nonce=secrets.token_urlsafe(32)
        self.state="就绪"
        self.scenario=""
        self.lock=threading.Lock()
        self.bounces=0

    def act(self,action):
        with self.lock:
            if action=="start" and self.process().poll() is not None:
                self.start_host()
                self.state="已重新启动"
                self.bounces+=1
                return
            if action not in {"verified","unknown"} or self.process().poll() is not None:
                raise ValueError
            if self.state=="已布置":
                raise ValueError
            self.scenario=action
            self.fixture.matrix["fault"]="lost_reply" if action=="unknown" else None
            self.state="已布置"
            def crash():
                with self.lock:
                    proc=self.process()
                    # The worker/sidecar inherit this isolated test host's new process group.
                    if os.getpgid(proc.pid)!=proc.pid:
                        raise RuntimeError("Unsafe synthetic process group")
                    os.killpg(proc.pid,signal.SIGKILL)
                    proc.wait(timeout=8)
                    self.state="原宿主与 worker 已停止"
            self.model.before_final=crash

    def page(self):
        state=html.escape(self.state)
        alive=self.gateway.readiness()["available"]
        effects=len(self.fixture.matrix["effects"])
        with sqlite3.connect(self.fixture.service.db_path) as db:
            counts=dict(db.execute("SELECT status,COUNT(*) FROM operations WHERE capability_name='oa.leave.save_draft' GROUP BY status"))
        text=(f"状态 {state}。宿主端口{'在线' if alive else '离线'}。实际重启 {self.bounces} 次。"
              f"合成账本 {effects} 笔。中央核验成功 {counts.get('succeeded',0)} 笔，未知 {counts.get('outcome_unknown',0)+counts.get('unknown',0)} 笔。")
        buttons="".join('<button type="submit" name="action" value="'+action+'">'+value+'</button>'
                        for action,value in (("verified","下次在权威核验后停止"),("unknown","下次在写后丢回包时停止"),("start","重启原宿主")))
        return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>隔离重启验收</title><h1>隔离重启验收</h1><p>'
                +text+'</p><form method="post"><input type="hidden" name="nonce" value="'+self.nonce+'">'+buttons+'</form>').encode()

    def handle(self,handler,origin):
        if handler.headers.get("Host")!=urlsplit(origin).netloc:
            return handler.send_error(403)
        account=handler._authenticate()
        if account is None:
            return
        if account["user_subject"]!=OWNER:
            return handler.send_error(403)
        if handler.command=="POST":
            if handler.headers.get("Origin")!=origin or handler.headers.get_content_type()!="application/x-www-form-urlencoded":
                return handler.send_error(403)
            size=int(handler.headers.get("Content-Length","0"))
            if not 0<size<512:
                return handler.send_error(400)
            fields=parse_qs(handler.rfile.read(size).decode())
            if fields.get("nonce")!=[self.nonce]:
                return handler.send_error(403)
            try:
                self.act(fields["action"][0])
            except (KeyError,ValueError):
                return handler.send_error(409)
            handler.send_response(303)
            handler.send_header("Location","/fixture-restart")
            handler.send_header("Cache-Control","no-store")
            handler.end_headers()
            return
        body=self.page()
        handler.send_response(200)
        handler.send_header("Content-Type","text/html; charset=utf-8")
        handler.send_header("Cache-Control","no-store")
        handler.send_header("Content-Security-Policy","default-src 'none'; form-action 'self'; frame-ancestors 'none'")
        handler.send_header("Content-Length",str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
