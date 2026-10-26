#!/usr/bin/env python3
"""Isolated Workspace credential failure/renewal acceptance. No real OA IO."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import threading
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.support.latency_acceptance import LatencyAcceptanceFixture
from tests.support.full_business_acceptance import OWNER
from tests.support.interaction_acceptance import write_metadata
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.gateway import GatewayRequestError

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--home',type=Path,required=True)
args=parser.parse_args()
if args.home.exists(): raise SystemExit('Use a fresh acceptance home')
f=LatencyAcceptanceFixture(args.home,seed=False)
f.seed_history(messages=160,tasks=0)
clock=[datetime.now(timezone.utc)-timedelta(seconds=400)]
store=McpIdentityTokenStore(f.service.tasks.db_path,clock=lambda:clock[0])
token=store.issue(user_subject=OWNER,expected_principal_ref=OWNER,ttl_seconds=300)
with f.service.tasks._connect() as db:
    db.execute('INSERT INTO agent_host_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
       (str(uuid4()),OWNER,token['token_id'],'synthetic-credential-host','openclaw','test','{}','L0','test',clock[0].isoformat(),clock[0].isoformat()))
original=f.app.gateway
class Gateway:
    def call(self,method,params=None,**kwargs):
        if method=='agentbridge.task.wake':
            raise GatewayRequestError('FORBIDDEN','Synthetic expired credential')
        return original.call(method,params,**kwargs)
f.app.gateway=Gateway()
servers=f.servers()
for server in servers: threading.Thread(target=server.serve_forever,daemon=True).start()
metadata=f.public_metadata()
write_metadata(f.home/'public.json',metadata)
print(json.dumps({'workspace_url':f.workspace_url,'home':str(f.home),'username':'cardreview','password':metadata['synthetic_password']}),flush=True)
revision=0
try:
    while not f.stop_event.wait(.2):
        signal=f.home/'phase'
        phase=signal.read_text().strip() if signal.exists() else ''
        if phase in {'expiring','valid'} and phase != locals().get('previous_phase'):
            clock[0]=datetime.now(timezone.utc)
            days=2 if phase=='expiring' else 30
            store.renew(token['token_id'],new_expires_at=(clock[0]+timedelta(days=days)).isoformat(),
                expected_revision=revision,request_id=phase,actor='synthetic-admin',reason='isolated acceptance')
            revision+=1
            previous_phase=phase
            if phase=='valid':
                threading.Thread(target=f.coordinate,daemon=True).start()
            print(json.dumps({'phase':phase,'at':clock[0].isoformat()}),flush=True)
except KeyboardInterrupt:
    pass
finally:
    f.stop_event.set()
    for server in servers: server.shutdown();server.server_close()
    f.close()
