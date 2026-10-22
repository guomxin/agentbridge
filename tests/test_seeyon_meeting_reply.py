from copy import deepcopy
from unittest.mock import Mock
import pytest
from agentbridge.adapters.seeyon_meeting_reply import (
    PREPARE, COMMIT, ATTITUDES, MeetingReplyContractMismatch, MeetingReplyOutcomeUnknown,
    prepare_meeting_reply, commit_meeting_reply, preflight_meeting_reply, meeting_target,
)


def sample():
    return {'meetingVo': {'id':'22','title':'合成会议','beginDate':'2026-10-23 08:45',
            'endDate':'2026-10-23 12:00','meetPlace':'合成会议室','bodyType':'10',
            'state':10,'roomState':1,'content':'<p>议题</p>','attachments':[],
            'createUserName':'合成发起人','updateDateStr':'2026-10-22 08:24:01'},
            'meetingAuth':{'showReply':True,'showReplyAttitude':True},
            'myReply':{'id':'33','userId':'44','userName':'合成用户','meetingId':'22',
                       'feedbackFlag':-100,'feedback':None,'attachmentJSON':'[]'}}


class Adapter:
    base_url='https://oa.example.test/seeyon/main.do?method=main'
    origin='https://oa.example.test'
    def __init__(self):
        self.row={'affair_id':'-11','title':'合成会议','category':'会议',
                  'href':self.origin+'/seeyon/meeting.do?method=view&meetingId=22'}
        self.total=1
    def _fetch_workflow_collection(self,worker,collection):
        return {'items':[deepcopy(self.row)],'total':self.total,'count':1}


@pytest.fixture
def rig(monkeypatch):
    from agentbridge.adapters import seeyon_meeting_reply as mod
    adapter,worker=Adapter(),object()
    view=sample(); writes=[]
    def ajax(worker,adapter,method,args):
        if method=='reply':
            writes.append(deepcopy(args[0]))
            view['myReply'].update(feedbackFlag=int(args[0]['feedbackFlag']),feedback=args[0]['feedback'])
            return None  # Empty success payload is not proof; verify meetingView.
        assert method=='meetingView'
        return deepcopy(view)
    monkeypatch.setattr(mod,'_ajax',ajax)
    return adapter,worker,view,writes


def prep(rig,**kw):
    a,w,*_=rig
    return prepare_meeting_reply(a,w,{'affair_id':'-11','attitude':'join','feedback':'会前准备',**kw})['plan']


def test_preflight_no_write_and_no_default_decision(rig):
    a,w,v,writes=rig
    result=preflight_meeting_reply(a,w,{'affair_id':'-11'})
    assert result['matched'] and writes==[]
    from agentbridge.adapters.seeyon_meeting_reply import SCHEMA
    assert 'value' not in SCHEMA['fields'][0]
    assert any(x['value']=='合成会议' for x in result['review']['fields'])


@pytest.mark.parametrize('attitude',ATTITUDES)
def test_one_authorized_reply_and_exact_verification(rig,attitude):
    a,w,v,writes=rig; plan=prep(rig,attitude=attitude)
    boundary=Mock()
    result=commit_meeting_reply(a,w,plan,enter_commit_boundary=boundary)
    boundary.assert_called_once(); assert len(writes)==1
    assert result['verification']['confirmed'] and result['verification']['attitude']==attitude
    with pytest.raises(MeetingReplyContractMismatch):
        commit_meeting_reply(a,w,plan,enter_commit_boundary=boundary)
    assert len(writes)==1


@pytest.mark.parametrize('section,key,value',[
    ('meetingAuth','showReply',False),('meetingAuth','showReplyAttitude',False),
    ('meetingVo','id','99'),('meetingVo','state',30),('meetingVo','roomState',0),
    ('meetingVo','bodyType','20'),('myReply','meetingId','99'),('myReply','id',''),
    ('myReply','userId',''),('myReply','feedbackFlag',1),('myReply','feedbackFlag',True),
    ('myReply','attachmentJSON','[{"id":"1"}]')])
def test_unsupported_contract_never_writes(rig,section,key,value):
    a,w,v,writes=rig;v[section][key]=value
    with pytest.raises(MeetingReplyContractMismatch):prep(rig)
    assert writes==[]


@pytest.mark.parametrize('section,key,value',[
    ('meetingVo','content','材料变了'),('meetingVo','beginDate','2026-10-23 09:00'),
    ('meetingVo','meetPlace','另一地点'),('myReply','userId','55'),('myReply','id','55')])
def test_change_after_confirmation_stops_before_boundary(rig,section,key,value):
    a,w,v,writes=rig;plan=prep(rig);v[section][key]=value;boundary=Mock()
    with pytest.raises(MeetingReplyContractMismatch):commit_meeting_reply(a,w,plan,enter_commit_boundary=boundary)
    boundary.assert_not_called();assert writes==[]


@pytest.mark.parametrize('suffix',['&proxyId=55','&meetingId=99'])
def test_proxy_and_ambiguous_url_rejected(rig,suffix):
    a,w,_,writes=rig;a.row['href']+=suffix
    with pytest.raises(MeetingReplyContractMismatch):prep(rig)
    assert writes==[]


def test_incomplete_pending_and_foreign_url_rejected(rig):
    a,w,_,writes=rig;a.total=2
    with pytest.raises(MeetingReplyContractMismatch):prep(rig)
    a.total=1;a.row['href']=a.row['href'].replace(a.origin,'https://evil.test')
    with pytest.raises(MeetingReplyContractMismatch):prep(rig)
    assert writes==[]


@pytest.mark.parametrize('field,value',[('feedbackFlag',0),('feedback','other'),('userId','55'),('id','55'),('meetingId','99')])
def test_mismatched_authoritative_reply_is_unknown_without_retry(rig,monkeypatch,field,value):
    from agentbridge.adapters import seeyon_meeting_reply as mod
    a,w,v,writes=rig;plan=prep(rig);original=mod._ajax
    def ajax(worker,adapter,method,args):
        result=original(worker,adapter,method,args)
        if method=='meetingView' and writes:result['myReply'][field]=value
        return result
    monkeypatch.setattr(mod,'_ajax',ajax)
    with pytest.raises(MeetingReplyOutcomeUnknown):commit_meeting_reply(a,w,plan,enter_commit_boundary=Mock())
    assert len(writes)==1


def test_lost_submit_response_unknown_and_never_retried(rig,monkeypatch):
    from agentbridge.adapters import seeyon_meeting_reply as mod
    a,w,v,writes=rig;plan=prep(rig);original=mod._ajax
    def ajax(worker,adapter,method,args):
        result=original(worker,adapter,method,args)
        if method=='reply':raise TimeoutError()
        return result
    monkeypatch.setattr(mod,'_ajax',ajax)
    with pytest.raises(MeetingReplyOutcomeUnknown):commit_meeting_reply(a,w,plan,enter_commit_boundary=Mock())
    assert len(writes)==1


@pytest.mark.parametrize('status,text,payload',[(200,'',None),(200,'null',None),(200,'true',True)])
def test_reply_transport_allows_empty_ack_only_for_authoritative_readback(status,text,payload):
    from agentbridge.adapters.seeyon_meeting import _ajax
    worker=Mock();worker.request.return_value={'status':status,'text':text,'json':payload}
    assert _ajax(worker,Adapter(),'reply',[{}])==payload


@pytest.mark.parametrize('status,text,payload',[(500,'',None),(200,'<html>error</html>',None),(200,'{"code":1,"message":"error"}',{'code':1,'message':'error'})])
def test_reply_transport_does_not_accept_http_or_application_errors(status,text,payload):
    from agentbridge.adapters.seeyon_meeting import _ajax,MeetingContractMismatch
    worker=Mock();worker.request.return_value={'status':status,'text':text,'json':payload}
    with pytest.raises(MeetingContractMismatch):_ajax(worker,Adapter(),'reply',[{}])
