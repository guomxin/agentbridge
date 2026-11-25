"""Deliver the original Pi task's synthetic PDF and CSV through its own private consumer."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

from tests.authorization_fixtures import grant_permissions
from tests.support.pi_workspace_images_probe import fixture
from tests.test_nanobot_telegram_artifacts import csv_report, pdf


def exercise(root, node, sdk, engine):
    with fixture(root, node, sdk, engine) as (meta, _, gateway, service, _, bot, _, errors):
        subject = meta["subjects"]["a"]
        binding = gateway.resolve_telegram_sender(8801, 501)
        assert binding["user_subject"] == subject and binding["endpoint_key"] == "telegram:8801:501"
        grant_permissions(service.user_grants, subject, ["oa.certificate.read","oa.certificate.download"])
        uploads = []
        def send(chat, name, mime, body):
            assert chat == 501 and mime in {"application/pdf","text/csv"}
            uploads.append((mime,hashlib.sha256(body).hexdigest()))
            return {"message_id":100+len(uploads),"chat":{"type":"private","id":chat},"from":{"id":8801},
                    "document":{"file_id":"fixture-"+str(len(uploads)),"file_size":len(body)}}
        bot.send_document = send
        task, _ = service.tasks.ensure_task(
            user_subject=subject, agent_host="pi", host_task_key="pi-private-files",
            origin_endpoint_id=binding["endpoint_id"], active_conversation_ref=binding["session_key"],
            title="合成文件交付")
        origin = SimpleNamespace(service=service,subject=subject,task=task["task_id"])
        first, pdf_bytes = pdf(origin)
        second, csv_bytes = csv_report(origin)
        expected = {first["artifact_id"], second}
        deadline = time.monotonic()+50
        while time.monotonic() < deadline:
            record = service.tasks.get_task(origin.task,user_subject=subject)
            files = record["summary"].get("artifactDeliveryByChannel",{}).get("telegram",{}).get("files",[])
            if {f["artifactId"] for f in files if f["state"] == "attachment_sent"} == expected:
                break
            if errors:
                raise AssertionError("Pi file consumer stopped: "+str(errors))
            time.sleep(0.2)
        else:
            raise AssertionError("Pi file delivery did not finish: "+str([f.get("state") for f in files]))
        digests = lambda value: hashlib.sha256(value).hexdigest()
        assert sorted(uploads) == sorted([("application/pdf",digests(pdf_bytes)),("text/csv",digests(csv_bytes))])
        assert record["agent_host"] == "pi" and record["origin_endpoint_id"] == binding["endpoint_id"]
        rows = [r for r in service.tasks.list_outbox(user_subject=subject)
                if r["task_id"] == origin.task and r["payload"].get("artifactPairProof")]
        assert len(rows) == 2 and all(r["state"] == "acknowledged" and r["attempt_count"] == 1 for r in rows)
        return {"agentHost":"pi","originalTask":True,"privateEndpoint":True,"types":["application/pdf","text/csv"],
                "originalBytesMatched":2,"acknowledged":2,"deliveryAttempts":[1,1],"syntheticTelegram":True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("node","sdk-dir","engine-python"):
        parser.add_argument("--"+arg,type=Path,required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pi-file-qualification-") as tmp:
        print(json.dumps(exercise(Path(tmp),args.node,args.sdk_dir,args.engine_python)))
