"""Run the OA digest scheduler and watch-change delivery through one paired Pi consumer."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agentbridge.core import oa_daily_digest
from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.oa_daily_digest_report import DigestReport
from agentbridge.workspace.gateway import GatewayRequestError
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.support.pi_delivery_failure_probe import wait_for
from tests.support.pi_workspace_images_probe import fixture


def exercise(root,node,sdk,engine):
    with fixture(root,node,sdk,engine,notification_settings=True) as (meta,accounts,gateway,service,_,bot,_,errors):
        owner, other = (meta["subjects"][name] for name in ("a","b"))
        binding = gateway.resolve_telegram_sender(8801,501)
        assert binding["user_subject"] == owner
        try:
            gateway.resolve_telegram_sender(8801,502)
        except GatewayRequestError as denied:
            assert denied.code == "PI_BINDING_UNAVAILABLE"
        else:
            raise AssertionError("Second sender obtained a Pi route")
        replies = []
        validating, polled_after_revoke = threading.Event(), threading.Event()
        original = bot.call
        def reply(method,**body):
            if method=="getUpdates" and validating.is_set():
                polled_after_revoke.set()
            if method != "sendMessage":
                return original(method,**body)
            assert body["chat_id"] == 501
            replies.append(body["text"])
            return {"message_id":100+len(replies),"from":{"id":8801},"chat":{"type":"private","id":501}}
        bot.call = reply
        settings = service.oa_digest_settings
        account = accounts[owner]
        assert settings.get(accounts[other])["telegramOptions"] == []
        now = datetime.now(timezone.utc)
        settings.clock = lambda: now - timedelta(minutes=2)
        form = {"enabled":True,"time":now.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M"),
                "workspace":False,"telegramEndpointId":binding["endpoint_id"],"expectedVersion":0}
        settings.update(account,form)
        settings.clock = lambda: now
        with patch.object(oa_daily_digest,"collect_oa_daily_digest",return_value=DigestReport("Pi合成OA日报",True)):
            assert settings.run_due()["published"] == 1
            assert settings.run_due()["claimed"] == 0
            wait_for(lambda: len(replies)==1,errors)
            digest = next(r for r in service.tasks.list_outbox(user_subject=owner) if r["endpoint_id"]==binding["endpoint_id"])
            wait_for(lambda: service.tasks.get_delivery(digest["delivery_id"])["state"]=="acknowledged",errors)
        assert "Pi合成OA日报" in replies[0] and not service.tasks.list_outbox(user_subject=other)
        store=service.business_watch.store
        session=service.sessions.find(user_subject=owner,system_id="oa")
        watch,_ = store.create(user_subject=owner,principal_ref=principal_binding(session),
            system_instance=service.business_watch.system_instance,endpoints=[binding["endpoint_id"]],allow_mark_read=True,include_private=True,
            title="Pi实测关注",source={"collection":"done","affair_id":"871001","summary_id":"871002","process_id":"871003","case_id":"871004"})
        def change(node):
            nonlocal watch
            watch=store.apply_observation(watch["watch_id"],user_subject=owner,expected_version=watch["version"],
                snapshot={"workflow_state":"in_progress","current_nodes":[{"name":node,"handlers":["甲"]}],
                          "current_handlers":["甲"],"unavailable_fields":[],"observed_at":datetime.now(timezone.utc).isoformat()},
                include_private=True)["watch"]
        change("初审")
        change("Pi复核")
        wait_for(lambda: len(replies)==2,errors)
        assert "Pi复核" in replies[-1]
        watches = [r for r in service.tasks.list_outbox(user_subject=owner) if r["delivery_id"]!=digest["delivery_id"] and r["endpoint_id"]==binding["endpoint_id"]]
        assert len(watches)==1
        wait_for(lambda:service.tasks.get_delivery(watches[0]["delivery_id"])["state"]=="acknowledged",errors)
        assert not gateway._request("telegram-notifications",{"binding":binding}).get("deliveries")
        # Revoke the actual pair after claim but before fresh watch content is requested.
        raw=gateway._request
        def intercept(action,payload,**kw):
            if action=="telegram-watch-validate":
                assert TelegramPilotLinks(service).revoke(account["account_id"],"pi-office","8801")["state"]=="unlinked"
                validating.set()
            return raw(action,payload,**kw)
        gateway._request=intercept
        change("撤权后不应可见")
        wait_for(validating.is_set,errors)
        wait_for(polled_after_revoke.is_set,errors)
        assert gateway.resolve_telegram_sender(8801,501) is None
        assert not service.tasks.list_outbox(user_subject=other) and len(replies)==2
        return {"host":"pi","digestScheduled":1,"repeatScheduleClaims":0,"digestAcknowledged":1,"watchAcknowledged":1,
                "postRevocationWatchSends":0,"otherSubjectNotifications":0,"syntheticTelegram":True}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ("node","sdk-dir","engine-python"):
        parser.add_argument("--"+key,type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pi-notification-qualification-") as tmp:
        print(json.dumps(exercise(Path(tmp),args.node,args.sdk_dir,args.engine_python)))
