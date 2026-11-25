"""A Pi file batch receives one positive receipt and one HTTP 429, then loses its route before another upload."""
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace

from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.authorization_fixtures import grant_permissions
from tests.support.pi_workspace_images_probe import fixture
from tests.test_nanobot_telegram_artifacts import csv_report, pdf
from nanobot_agentbridge.telegram import TelegramAPI


def wait_for(check, errors, deadline=30):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        assert not errors, errors
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("Expected isolated Pi delivery observation did not occur")


def exercise(root,node,sdk,engine):
    with fixture(root,node,sdk,engine) as (meta,accounts,gateway,service,_,bot,_,errors):
        subject = meta["subjects"]["a"]
        binding = gateway.resolve_telegram_sender(8801,501)
        grant_permissions(service.user_grants,subject,["oa.certificate.read","oa.certificate.download"])
        def origin(key):
            task,_ = service.tasks.ensure_task(user_subject=subject,agent_host="pi",host_task_key=key,
                origin_endpoint_id=binding["endpoint_id"],active_conversation_ref=binding["session_key"],title="测试文件组合")
            return SimpleNamespace(service=service,subject=subject,task=task["task_id"])
        def states(task):
            record = service.tasks.get_task(task.task,user_subject=subject)
            return {f["artifactId"]:f for f in record["summary"].get("artifactDeliveryByChannel",{}).get("telegram",{}).get("files",[])}
        requests = []
        class RateLimited:
            def request(self,method,path,payload,headers):
                assert method=="POST" and path.endswith("/sendDocument") and b"text/csv" in payload
                requests.append("csv")
            def getresponse(self):
                return SimpleNamespace(status=429,read=lambda size:b'{"ok":false,"error_code":429,"parameters":{"retry_after":17}}')
            def close(self):
                pass
        api = TelegramAPI("synthetic",RateLimited,private_sender=lambda peer: peer==501)
        def upload(chat,name,mime,body):
            assert chat==501
            if mime=="text/csv":
                return api.send_document(chat,name,mime,body)
            assert mime=="application/pdf"
            requests.append("pdf")
            return {"message_id":101,"chat":{"type":"private","id":501},"from":{"id":8801},
                    "document":{"file_id":"synthetic","file_size":len(body)}}
        bot.send_document=upload
        task=origin("partial-pi-batch")
        first,_ = pdf(task)
        second,_ = csv_report(task)
        wait_for(lambda: states(task).get(first["artifact_id"],{}).get("state")=="attachment_sent"
                 and states(task).get(second,{}).get("errorCode")=="TELEGRAM_DELIVERY_UNKNOWN",errors)
        def claims():
            return gateway._request("telegram-notifications",{"binding":binding}).get("deliveries",[])
        for _ in range(2):
            assert not any(d.get("artifactId") in {first["artifact_id"],second} for d in claims())
        assert sorted(requests)==["csv","pdf"]
        with sqlite3.connect(root/"telegram.sqlite") as db:
            assert db.execute("SELECT state FROM telegram_artifact_deliveries WHERE artifact=?",(second,)).fetchone()==("delivery_unknown",)

        # Revoke after the actual authorized response supplied original file bytes and before the consumer sends.
        seen, next_poll = threading.Event(), threading.Event()
        call=bot.call
        def observe_poll(method,**args):
            if method=="getUpdates" and seen.is_set():
                next_poll.set()
            return call(method,**args)
        bot.call=observe_poll
        raw = gateway._request
        def intercept(action,arguments,**options):
            reply=raw(action,arguments,**options)
            if action=="telegram-artifact-file" and arguments.get("withBody") and reply.get("allowed"):
                links=TelegramPilotLinks(service)
                assert links.revoke(accounts[subject]["account_id"],"pi-office","8801")["state"]=="unlinked"
                seen.set()
            return reply
        gateway._request=intercept
        revoked=origin("revoked-pi-file")
        blocked,_=pdf(revoked)
        wait_for(seen.is_set,errors)
        wait_for(next_poll.is_set,errors)
        assert gateway.resolve_telegram_sender(8801,501) is None
        scope={"user_subject":subject,"agent_host":"pi","host_instance_id":"pi-office","endpoint_key":"telegram:8801:501"}
        row=next(r for r in service.tasks.list_outbox(user_subject=subject)
                 if r["task_id"]==revoked.task and r["payload"].get("artifactPairProof"))
        result=service.validate_host_notification(**scope,delivery_id=row["delivery_id"],attempt_count=row["attempt_count"],include_artifact=True)
        assert result.get("allowed") is False
        with sqlite3.connect(root/"telegram.sqlite") as db:
            assert not db.execute("SELECT 1 FROM telegram_artifact_deliveries WHERE artifact=?",(blocked["artifact_id"],)).fetchone()
        assert sorted(requests)==["csv","pdf"]
        return {"agentHost":"pi","partial":{"sent":1,"http429":1,"unknown":1,"reclaims":0},
                "postRevocationUploads":0,"postRevocationFileReadsAllowed":False,"syntheticTelegram":True}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("node","sdk-dir","engine-python"):
        parser.add_argument("--"+key,type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pi-telegram-negative-") as tmp:
        print(json.dumps(exercise(Path(tmp),args.node,args.sdk_dir,args.engine_python)))
