"""Exercise the actual SDK registry with isolated watch receipts and no real OA or Telegram."""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "integrations/nanobot-agentbridge"))
from nanobot_agentbridge.runtime import CentralTools
from nanobot_agentbridge.watch_tools import CREATE, UPDATE, WatchTool


async def main():
    watch_id, endpoint = str(uuid4()), str(uuid4())
    record = {"watch_id":watch_id,"state":"active","version":3,"endpoint_ids":["existing-channel"]}
    calls = []
    class Session:
        async def call_tool(self, name, args, meta=None):
            calls.append((name,args.copy()))
            if name == "agentbridge_host_identity_profile":
                value={"identity":{"userSubject":"alice"},"host":{"hostInstanceId":"office"},"telegramIdentityVerified":True}
            else:
                value={"watch":record}
            return SimpleNamespace(isError=False, structuredContent=value)
    request={"runId":str(uuid4()),"subject":"alice","endpoint":endpoint,"instance":"office","endpointKey":"telegram:8001:501"}
    context=SimpleNamespace(session=Session(),host_meta={"agentHost":"nanobot","hostInstanceId":"office"})
    def tool(name, skill=None):
        keys=("target_ref","allow_mark_read") if name == CREATE else ("watch_id","expected_version","action")
        definition=SimpleNamespace(name=name,description="Synthetic watch",inputSchema={"type":"object",
            "properties":{k:{} for k in (*keys,"endpoint_ids","idempotency_key")}})
        return WatchTool(definition,request,context,skill)
    create, update=tool(CREATE),tool(UPDATE)
    registry=CentralTools()
    registry.register(create)
    registry.register(update)
    assert set(registry.tool_names)=={"mcp_ab_"+CREATE,"mcp_ab_"+UPDATE}
    assert not {"idempotency_key","endpoint_ids"} & create.parameters["properties"].keys()
    await create.execute(target_ref="owned", allow_mark_read=False)
    await create.execute(target_ref="owned", allow_mark_read=True,endpoint_ids=["foreign"])
    await tool(CREATE,SimpleNamespace(selection="fixed",pending=False)).execute(target_ref="owned",allow_mark_read=True)
    assert not calls
    await create.execute(target_ref="owned",allow_mark_read=True)
    assert "endpoint_ids" not in calls[-1][1]
    await create.execute(target_ref="owned",allow_mark_read=True,notify_telegram=True)
    first=calls[-1][1].copy()
    await create.execute(target_ref="owned",allow_mark_read=True,notify_telegram=True)
    assert calls[-1][1]==first and first["endpoint_ids"]==[endpoint]
    await update.execute(watch_id=watch_id,expected_version=3,action="configure",notify_telegram=True)
    assert calls[-1][1]["endpoint_ids"]==["existing-channel",endpoint] and "allow_mark_read" not in calls[-1][1]
    before=len(calls)
    await update.execute(watch_id=watch_id,expected_version=2,action="configure",notify_telegram=False)
    assert not any(n==UPDATE for n,_ in calls[before:])
    await update.execute(watch_id=watch_id,expected_version=3,action="stop")
    assert calls[-1] == (UPDATE,{"watch_id":watch_id,"expected_version":3,"action":"stop"})
    print("NanoBot SDK watch registry, explicit consent, verified destination, idempotence, CAS and stop passed.")


if __name__=="__main__":
    asyncio.run(main())
