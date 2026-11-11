"""Real HTTP and saved-query acceptance; no model or production source access."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.support.single_query_acceptance import (
    CASES, OWNER, OTHER, PROMPT, SingleQueryAcceptanceFixture,
)
from tests.test_workspace import _cookies, _request


class CookieJar(dict):
    def __repr__(self): return '<synthetic Workspace cookies>'


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENTBRIDGE_SESSION_KEY_FILE',str(tmp_path/'unused-parent.key'))
    value=SingleQueryAcceptanceFixture(tmp_path/'fixture')
    value.start()
    try:yield value
    finally:value.close()


def login(fixture,subject=OWNER):
    status,headers,_=_request(urlsplit(fixture.url).port,'POST','/api/login',origin=fixture.url,
        body={'username':fixture.accounts[subject]['username'],'password':json.loads((fixture.home/'login.json').read_text())['password']})
    assert status==200
    return CookieJar(_cookies(headers))


def http(fixture,method,path,*,cookies,body=None,csrf=True,origin=True):
    return _request(urlsplit(fixture.url).port,method,path,cookies=cookies,body=body,
        csrf=cookies['agentbridge_workspace_csrf'] if method=='POST' and csrf else None,
        origin=fixture.url if method=='POST' and origin else None)


def projection(fixture,case,cookies):
    return http(fixture,'GET','/api/tasks/'+fixture.tasks[case]['task_id'],cookies=cookies)


def test_source_data_is_blind_and_latest_is_global_not_source_page_order(fixture):
    rows=fixture.source.rows['latest']
    assert len(rows)==224 and sum(row['workflow_state']=='in_progress' for row in rows)==29
    newest=sorted(rows,key=lambda row:row['initiated_at'],reverse=True)
    assert all(row['workflow_state']=='ended' for row in newest[:5])
    assert newest[5]['affair_id']=='100005' and newest[5]['workflow_state']=='in_progress'
    assert rows[0]['initiated_at']<rows[-1]['initiated_at']
    assert all('target' not in row['title'] and '目标' not in row['title'] for row in rows)
    assert PROMPT=='查我最近一条还没结束的OA已发事项'
    assert not (fixture.home/'central'/'oracle.json').exists()
    assert all(not any(key in row for key in ('expected','oracle','answer')) for row in rows)
    assert fixture.audit()=={'businessDecisions':0,'authorizations':0,'commits':0,
        'realOaCalls':0,'modelStarted':False,'coordinatorStarted':False}


def test_real_http_projects_one_selected_item_from_all_source_rows_and_refresh_reads_no_source(fixture):
    cookies=login(fixture);before=len(fixture.source.calls)
    status,_,detail=projection(fixture,'latest',cookies)
    assert status==200
    query=detail['oaHistoryQuery']
    assert query['state']=='complete' and len(query['items'])==1 and query['items'][0]['affair_id']=='100005'
    assert query['selection']['selected_count']==1 and query['selection']['selection_complete'] is True
    assert query['selection']['scope']=='all_matching_history' and query['progress']['matched_count']==224
    assert query['coverage']['details_scope']=='selection_candidates' and query['progress']['details_checked']==1
    assert query['items'][0]['current_nodes'][0]['name']=='合成财务复核' and query['items'][0]['target_ref']
    assert not query['has_more'] and query['next_cursor'] is None
    assert projection(fixture,'latest',cookies)[2]['oaHistoryQuery']==query and len(fixture.source.calls)==before
    calls=[call for call in fixture.source.calls if call['case']=='latest']
    assert sum(call['kind']=='detail' for call in calls)==1
    pages=[call for call in calls if call['kind']=='page']
    assert len(pages)==7 and len({identity for call in pages for identity in call['row_ids']})==224
    assert sum(call['returned_rows'] for call in pages)==298


def test_empty_all_ended_unknown_and_failure_keep_their_actual_completeness(fixture):
    cookies=login(fixture)
    for case in ('empty','all-ended'):
        status,_,detail=projection(fixture,case,cookies);query=detail['oaHistoryQuery']
        assert status==200 and query['state']=='complete' and query['items']==[]
        assert query['selection']['selected_count']==0 and query['selection']['selection_complete'] is True
    unknown=projection(fixture,'unknown',cookies)[2]['oaHistoryQuery']
    assert unknown['state']=='partial' and unknown['items']==[]
    assert unknown['selection']['selection_complete'] is False and unknown['selection']['reason']=='selection_state_unverified'
    failed=projection(fixture,'failure',cookies)[2]['oaHistoryQuery']
    assert failed['state']=='partial' and failed['selection']['selection_complete'] is False
    assert not failed['coverage']['scan_complete'] and failed['items']==[]


def test_authenticated_csrf_paging_replays_exact_receipts_without_source_reads_and_delivers_selected_set(fixture):
    cookies=login(fixture);task_id=fixture.tasks['continue']['task_id'];path='/api/tasks/'+task_id+'/oa-history/next'
    current=projection(fixture,'continue',cookies)[2]['oaHistoryQuery']
    assert current['has_more'] and current['state']=='details'
    cursor=current['next_cursor'];before=len(fixture.source.calls)
    assert http(fixture,'POST',path,cookies=cookies,body={'cursor':cursor},csrf=False)[0]==401
    assert http(fixture,'POST',path,cookies=cookies,body={'cursor':cursor},origin=False)[0]==403
    assert len(fixture.source.calls)==before
    status,_,response=http(fixture,'POST',path,cookies=cookies,body={'cursor':cursor})
    assert status==200;current=response['oaHistoryQuery'];after=len(fixture.source.calls)
    replay=http(fixture,'POST',path,cookies=cookies,body={'cursor':cursor})
    assert replay[0]==200 and replay[2]['oaHistoryQuery']==current and len(fixture.source.calls)==after
    delivered=list(current['items']);pages=0
    while current['has_more']:
        pages+=1;assert pages<=10
        status,_,response=http(fixture,'POST',path,cookies=cookies,body={'cursor':current['next_cursor']})
        assert status==200;current=response['oaHistoryQuery'];delivered.extend(current['items'])
    assert current['selection']['selection_complete'] is True
    assert len(delivered)==12 and len({row['affair_id'] for row in delivered})==12
    assert [row['affair_id'] for row in delivered]==[str(105000+rank) for rank in range(5,17)]
    assert sum(c['kind']=='detail' for c in fixture.source.calls if c['case']=='continue')==12


def test_permission_session_and_route_boundaries_block_io_and_clear_history_projection(fixture):
    owner=login(fixture);other=login(fixture,OTHER);before=len(fixture.source.calls)
    assert projection(fixture,'latest',other)[0]==404
    assert _request(urlsplit(fixture.url).port,'GET','/api/tasks/'+fixture.tasks['latest']['task_id'])[0]==401
    assert http(fixture,'POST','/api/chat/send',cookies=owner,body={'message':PROMPT})[0]==403
    assert http(fixture,'POST','/api/business-watches',cookies=owner,body={})[0]==403
    (fixture.home/'expire-primary-session').write_text('');fixture.controls()
    status,_,detail=projection(fixture,'latest',owner)
    assert status==200 and detail['oaHistoryQuery']['state']=='unavailable' and detail['oaHistoryQuery']['items']==[]
    (fixture.home/'restore-primary-session').write_text('');fixture.controls()
    (fixture.home/'revoke-primary-read').write_text('');fixture.controls()
    assert projection(fixture,'latest',owner)[0]==403
    assert len(fixture.source.calls)==before
    with pytest.raises(PermissionError):
        fixture.service.invoke(user_subject=OWNER,capability_name='oa.workflow.approval.commit',arguments={})
    assert len(fixture.source.calls)==before and fixture.audit()['commits']==0


def test_authoritative_run_extraction_keeps_the_complete_normal_final_and_actual_tool_ids():
    from tests.support.single_query_model_acceptance import run_history,text_of
    text='完整答复\n'+'合成节点与证据。'*300
    history={'messages':[{'role':'assistant','stopReason':'stop','content':'旧回复'},
        {'role':'user','runId':'run-one','content':'短句'},
        {'role':'assistant','content':[{'type':'toolCall','id':'call-one','name':'oa_workflow_history_query','arguments':{'collection':'sent'}}]},
        {'role':'toolResult','toolCallId':'call-one','content':'合成工具结果'},
        {'role':'assistant','runId':'run-one','stopReason':'stop','provider':'openai','model':'synthetic',
            'content':[{'type':'text','text':text}]},
        {'role':'user','runId':'run-two','content':'另一个问题'},
        {'role':'assistant','stopReason':'stop','content':'不属于本轮'}]}
    current,final,calls=run_history(history,'run-one')
    assert text_of(final)==text and len(calls)==1 and calls[0]['id']=='call-one'
    assert len(current)==3
    history['messages'].insert(4,{'role':'toolResult','toolCallId':'call-two','content':'新工具活动'})
    history['messages'][5]['stopReason']='length'
    assert run_history(history,'run-one')[1] is None


def test_model_auth_copy_uses_only_unexpired_access_and_never_refreshes_or_changes_source(tmp_path):
    import shutil
    import subprocess
    import time
    node=shutil.which('node');assert node
    home=tmp_path/'private';home.mkdir(mode=0o700)
    source=tmp_path/'source-agent';source.mkdir()
    target=home/'openclaw/agents/main/agent';target.mkdir(parents=True)
    (home/'skill-host-acceptance.json').write_text(json.dumps({'fixture':'skill-host-acceptance-v1','consumer':'single-query-real-host-v1'}))
    config=tmp_path/'source-config.json';config.write_text(json.dumps({'auth':{'profiles':{'openai:synthetic':{'provider':'openai','mode':'oauth'}}}}))
    profile=source/'profile.json';credential={'type':'oauth','provider':'openai','access':'synthetic-private-access-'+'x'*24,
        'refresh':'synthetic-private-refresh','expires':int(time.time()*1000)+3600000,'email':'synthetic-private-email'}
    profile.write_text(json.dumps(credential));original=profile.read_bytes()
    sdk=tmp_path/'sdk.mjs';sdk.write_text("""import fs from 'node:fs';import path from 'node:path';
export function findPersistedAuthProfileCredential({agentDir}){return JSON.parse(fs.readFileSync(path.join(agentDir,'profile.json'),'utf8'));}
export function upsertAuthProfile(value){fs.writeFileSync(path.join(value.agentDir,'copied.json'),JSON.stringify(value));}
export function resolveApiKeyForProfile(){throw Error('Resolver must never be called');}
""")
    payload=home/'input.json';payload.write_text(json.dumps({'sdkPath':str(sdk),'sourceConfig':str(config),
        'sourceAgentDir':str(source),'targetAgentDir':str(target),'fixtureHome':str(home)}))
    helper=Path(__file__).parent/'support/single_query_model_auth.mjs'
    result=subprocess.run([node,str(helper),str(payload)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0 and 'synthetic-private-' not in result.stdout+result.stderr
    copied=json.loads((target/'copied.json').read_text())
    assert copied['credential']=={'type':'token','provider':'openai','token':credential['access'],
        'expires':credential['expires'],'copyToAgents':False}
    assert profile.read_bytes()==original and json.loads(result.stdout)['sourceModified'] is False
    (target/'copied.json').unlink();credential['expires']=0;profile.write_text(json.dumps(credential));expired=profile.read_bytes()
    result=subprocess.run([node,str(helper),str(payload)],capture_output=True,text=True,timeout=10)
    assert result.returncode==1 and not (target/'copied.json').exists() and profile.read_bytes()==expired
    assert 'synthetic-private-' not in result.stdout+result.stderr
