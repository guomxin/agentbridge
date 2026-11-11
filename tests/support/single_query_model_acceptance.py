"""Independent real OpenClaw/model run over the single-query synthetic readers.

The existing host fixture is reused for official provider/auth/runtime operations,
without modifying it or substituting an assistant answer. No production Gateway
is contacted. Oracle/review files stay outside the model workspace.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import time

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.gateway import GatewayRequestError
from tests.support.single_query_acceptance import OWNER, OTHER, PROMPT, ROOT, SingleQueryAcceptanceFixture, private_json
from tests.support.skill_host_acceptance import (
    ALICE, BOB, SkillHostAcceptance, private_text, run_isolated_process,
)

TOOLS = ['agentbridge_identity_status','oa_workflow_history_query','oa_workflow_history_next',
    'agentbridge_operation_get','agentbridge_operation_list']
PROMPTS = {'latest':PROMPT+'。','paraphrase':'查一下我 OA 已发里最新那条还在流转的事项。','unknown':PROMPT+'。'}


def code_identity():
    paths = [ROOT/'tests/support/single_query_model_acceptance.py', ROOT/'scripts/run-single-query-model-acceptance.py',
        ROOT/'tests/support/single_query_acceptance.py',ROOT/'agentbridge/core/oa_history_query_service.py',
        ROOT/'agentbridge/core/oa_history_queries.py',ROOT/'agentbridge/core/central_service.py',
        ROOT/'agentbridge/core/planning_policy.py',ROOT/'agentbridge/mcp/central.py',ROOT/'agentbridge/mcp/oa_history.py',
        ROOT/'agentbridge/workspace/gateway.py',ROOT/'agentbridge/workspace/gateway_client.mjs',
        ROOT/'agentbridge/workspace/gateway_run_guard.mjs']
    paths += list((ROOT/'integrations/openclaw-agentbridge/lib').glob('*.js'))
    paths += list((ROOT/'integrations/openclaw-agentbridge/lib').glob('*.json'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def text_of(message):
    content=message.get('content')
    if isinstance(content,str):return content.strip()
    return '\n'.join(part['text'].strip() for part in content or [] if isinstance(part,dict)
        and part.get('type') in {'text','output_text'} and isinstance(part.get('text'),str) and part['text'].strip()).strip()


def run_history(history,run_id):
    messages=history.get('messages',[]);anchor=None
    for index,message in enumerate(messages):
        if message.get('role')=='user' and (message.get('runId')==run_id or message.get('idempotencyKey')==run_id+':user'):
            anchor=index
    if anchor is None:return [],None,[]
    current=[];calls={};final=None
    for message in messages[anchor+1:]:
        if message.get('role')=='user':break
        if message.get('runId') not in {None,'',run_id}:continue
        current.append(message)
        for part in message.get('content',[]) if isinstance(message.get('content'),list) else []:
            if isinstance(part,dict) and part.get('type') in {'toolCall','tool_use'}:
                key=part.get('id') or part.get('toolCallId')
                if key:calls[key]={'id':key,'name':part.get('name'),'arguments':deepcopy(part.get('arguments',part.get('input')))}
        if message.get('role') in {'tool','toolResult'}:final=None
        elif message.get('role')=='assistant' and message.get('stopReason',message.get('stop_reason')) in {'stop','end_turn'} and text_of(message):final=message
    return current,final,list(calls.values())


class SingleQueryModelAcceptance(SkillHostAcceptance):
    def __init__(self,home,case='latest'):
        if case not in PROMPTS:raise ValueError('Unknown synthetic case')
        self.fixture=SingleQueryAcceptanceFixture(home,seed=False)
        self.home,self.service,self.accounts=self.fixture.home,self.fixture.service,self.fixture.accounts
        for name in ('workspace','evidence','openclaw/agents/main/agent'):(self.home/name).mkdir(parents=True,mode=0o700,exist_ok=True)
        self.previous_key=self.fixture.previous_key
        self.candidate=None;self.app=None;self.gateway=None;self.servers=[];self.log_levels={}
        self.identities=McpIdentityTokenStore(self.service.db_path)
        owner_token=self.identities.issue(user_subject=OWNER,expected_principal_ref=OWNER,ttl_seconds=14400)['token']
        other_token=self.identities.issue(user_subject=OTHER,expected_principal_ref=OTHER,ttl_seconds=14400)['token']
        self.tokens={ALICE:owner_token,BOB:other_token}
        private_json(self.home/'synthetic-mcp-identities.json',self.tokens)
        private_json(self.home/'skill-host-acceptance.json',{'fixture':'skill-host-acceptance-v1',
            'candidate':str(ROOT),'consumer':'single-query-real-host-v1'})
        self.case=case;self.code_start=code_identity()
        self.report={'fixture':'single-query-real-host-v1','case':case,'prompt':PROMPTS[case],
            'model_attempted':False,'semantic_review':'pending; blank review is not a pass',
            'code_start':self.code_start,'evidence_boundary':{'host':'actual isolated Workspace grant -> bind -> chat.send -> same-socket history',
                'central':'real MCP/auth/permission/session/query/operation/task stores','source':'only read_page/read_nodes are synthetic',
                'realOaCalls':0,'externalMessages':0}}
        original=self.fixture.source.read_page
        selected='unknown' if case=='unknown' else 'latest'
        def read_page(*args,**kwargs):
            self.fixture.source.local.case=selected
            self.fixture.source.local.caller='model'
            return original(*args,**kwargs)
        self.service.oa_history_query_service.read_page=read_page
        save_source=self.fixture.source.save
        def save_source_metrics():
            save_source()
            path=self.home/'source-reads.json';value=json.loads(path.read_text())
            value['modelStarted']=self.report['model_attempted'];private_json(path,value)
        self.fixture.source.save=save_source_metrics

    def configure(self,**kwargs):
        super().configure(**kwargs)
        path=self.home/'openclaw.json';config=json.loads(path.read_text())
        config['tools']['allow']=TOOLS
        bindings=config['plugins']['entries']['agentbridge-interactions']['config']['identityBindings']
        bindings[0]['senderId']=OWNER;bindings[1]['senderId']=OTHER
        private_json(path,config)

    def prepare_auth(self,*,node,sdk,source_config,source_agent_dir):
        path=self.home/'auth-transfer-input.json'
        private_json(path,{'sdkPath':str(sdk),'sourceConfig':str(source_config),'sourceAgentDir':str(source_agent_dir),
            'targetAgentDir':str(self.home/'openclaw/agents/main/agent'),'fixtureHome':str(self.home)})
        code,stdout,_=run_isolated_process([str(node),str(Path(__file__).with_name('single_query_model_auth.mjs')),str(path)],
            env=self.env,cwd=self.home/'workspace',timeout=30)
        if code!=0:raise RuntimeError('Unexpired model access token unavailable; official source profiles were not modified')
        self.report['auth']=json.loads(stdout.strip().splitlines()[-1])

    def run(self,*,node,openclaw,timeout=180):
        if not 1<=timeout<=180:raise ValueError('Model budget must be 1..180 seconds')
        account=self.accounts[OWNER];session=account['openclaw_session_key']
        code,stdout,stderr=run_isolated_process([str(node),str(openclaw),'gateway','call','sessions.reset',
            '--params',json.dumps({'key':session,'reason':'new'}),'--json','--timeout','30000'],
            env=self.env,cwd=self.home/'workspace',timeout=40)
        private_text(self.home/'evidence/reset.stderr.txt',stderr)
        try:reset=json.loads(stdout)
        except ValueError:reset={}
        if code!=0 or reset.get('ok') is not True:raise RuntimeError('Isolated session reset failed before dispatch')
        prompt=PROMPTS[self.case];private_text(self.home/'evidence/prompt.txt',prompt)
        grant=self.service.workspace.issue_gateway_grant(account['account_id'])
        events=[];error_code=None;started=time.monotonic();self.report['model_attempted']=True
        try:
            for event in self.gateway_client.send_stream(session_key=session,endpoint_key=grant['endpoint_key'],
                grant=grant['grant'],message=prompt,idempotency_key=secrets.token_hex(16),timeout_seconds=timeout,retry_before_accept=False):events.append(event)
        except GatewayRequestError as error:error_code=error.code
        elapsed=time.monotonic()-started
        private_json(self.home/'evidence/events.json',events)
        history=self.gateway_client.call('chat.history',{'sessionKey':session,'limit':200,'maxChars':200000})
        private_json(self.home/'evidence/history.json',history)
        finals=[event for event in events if event.get('type')=='chat' and event.get('state')=='final']
        final=finals[-1] if finals else {};run_id=final.get('runId') or ''
        current,normal,calls=run_history(history,run_id)
        private_json(self.home/'evidence/run-history.json',current)
        private_json(self.home/'evidence/model-tool-calls.json',calls)
        output=final.get('text','');private_text(self.home/'evidence'/(self.case+'.answer.txt'),output)
        receipt=final.get('agentbridgeFinalHistory') or {}
        expected_partial=self.case=='unknown'
        central=self.service.query_calls
        query_ids={entry.get('query_id') for entry in central if entry.get('query_id')}
        returned=[entry for entry in central if entry.get('selection')]
        last=returned[-1] if returned else {}
        selection=last.get('selection') or {}
        source=self.fixture.source.calls
        private_json(self.home/'evidence/manual-review.json',{'reviewer':None,'review_kind':None,'verdict':None,
            'note':'Fill only after sentence-by-sentence review; Codex review is not independent human review.',
            'claims':[{'text':line,'evidence':[],'verdict':None,'review_note':''} for line in output.splitlines() if line.strip()],
            'target_correct':None,'unsupported_claims':[],'complete_final_answer':None})
        checks={'no_transport_error':error_code is None,'final_received':bool(output.strip()),
            'normal_stop':normal is not None,
            'same_run_same_socket_history':receipt.get('schemaVersion')=='agentbridge.gateway-final.v1'
                and receipt.get('source')=='local-same-socket-history' and receipt.get('sessionKey')==session
                and receipt.get('runId')==run_id and receipt.get('observedRunId')==run_id,
            'full_final_matches_history':normal is not None and text_of(normal)==output.strip(),
            'provider_model_recorded':normal is not None and bool(normal.get('provider')) and bool(normal.get('model')),
            'one_logical_query':len(query_ids)==1,
            'selection_requested':any(entry.get('arguments',{}).get('selection')=={'kind':'latest','count':1,'workflow_state':'in_progress'} for entry in central),
            'no_rejected_central_calls':bool(central) and all(entry['status']=='succeeded' for entry in central),
            'selection_outcome':selection.get('selection_complete') is (not expected_partial)
                and selection.get('selected_count')==(0 if expected_partial else 1)
                and (not expected_partial or selection.get('reason')=='selection_state_unverified'),
            'no_real_business_effects':all(self.fixture.audit()[key]==0 for key in ('businessDecisions','authorizations','commits','realOaCalls')),
            'source_identity_unchanged':self.code_start==code_identity()}
        self.report.update(elapsed_seconds=round(elapsed,3),error_code=error_code,run_id=run_id,
            model=normal.get('model') if normal else None,provider=normal.get('provider') if normal else None,
            normal_stop=normal.get('stopReason',normal.get('stop_reason')) if normal else None,
            final_chars=len(output),checks=checks,automatic_transport_status='passed' if all(checks.values()) else 'failed',
            model_tool_count=len(calls),model_tools=[call.get('name') for call in calls],
            central_call_count=len(central),central_calls=central,
            source_page_calls=sum(row['kind']=='page' for row in source),source_detail_calls=sum(row['kind']=='detail' for row in source),
            source_rows_including_anchors=sum(row['returned_rows'] for row in source),
            source_unique_rows=len({identity for row in source for identity in row.get('row_ids',[])}),
            result_delivery_pages=[{'query_id':entry.get('query_id'),'delivery':entry.get('delivery'),
                'returned_count':entry.get('returned_count')} for entry in central if entry.get('returned_count')],
            answer_file=str(self.home/'evidence'/(self.case+'.answer.txt')),audit=self.host_audit(),code_end=code_identity())
        self.save_report();return self.report

    def host_audit(self):
        raw=self.fixture.audit()
        return {**{key:raw[key] for key in ('businessDecisions','authorizations','commits','realOaCalls')},
            'modelStarted':self.report['model_attempted'],'isolatedGatewayStarted':self.gateway is not None,
            'productionGatewayContacted':False,'externalMessages':0}

    def close(self):
        try:super().close()
        finally:
            self.fixture.close()
            private_json(self.home/'business-audit.json',self.host_audit())
