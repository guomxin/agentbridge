"""Network-free meeting transport with actual Central/draft/commit machinery."""
from copy import deepcopy
import json
from urllib.parse import parse_qs,urlparse
from uuid import uuid4

from tests.authorization_fixtures import grant_permissions
from tests.support.business_view_acceptance import BusinessViewAcceptanceFixture, OWNER
from tests.test_seeyon_meeting_reply import sample


class MeetingReplyAcceptanceFixture(BusinessViewAcceptanceFixture):
    def __init__(self,home):
        super().__init__(home,seed=False)
        self.meeting=sample();self.meeting_writes=[];self.reply_fault=None
        self.row={'affair_id':'-11','title':'合成会议','category':'会议','sender':'合成发起人','date':'2026-10-23',
                  'href':self.service.adapter.origin+'/seeyon/meeting.do?method=view&meetingId=22'}
        existing=self.service.user_grants.get(OWNER)
        grant_permissions(self.service.user_grants,OWNER,set(existing['permissions'])|{'oa.meeting.reply','oa.workflow.read'})
        original=self.service.adapter._fetch_workflow_collection
        def collection(worker,name,**kwargs):
            if name!='pending':return original(worker,name,**kwargs)
            rows=[deepcopy(self.row)] if self.meeting['myReply']['feedbackFlag']==-100 else []
            return {'items':rows,'count':len(rows),'total':len(rows),'page':0}
        self.service.adapter._fetch_workflow_collection=collection
        request=self.oa.request
        def meeting_request(method,url,*,body=None,**kwargs):
            query=parse_qs(urlparse(url).query)
            if query.get('managerName')!=['meetingAjaxManager']:
                return request(method,url,body=body,**kwargs)
            args=parse_qs(body);name=args['managerMethod'][0];values=json.loads(args['arguments'][0])
            if name=='meetingView': payload=deepcopy(self.meeting)
            elif name=='reply':
                self.meeting_writes.append(values[0]);self.meeting['myReply'].update(
                    feedbackFlag=int(values[0]['feedbackFlag']),feedback=values[0]['feedback'])
                if self.reply_fault=='lost_response':raise TimeoutError('synthetic reply response lost')
                payload=None
            else:raise AssertionError('Unexpected meeting write/read')
            return {'status':200,'json':payload,'text':json.dumps(payload),'url':url}
        self.oa.request=meeting_request

    def add_meeting_case(self,*,batch=False):
        task=self.service.ensure_host_task(user_subject=OWNER,token_id='fixture-token',agent_host='openclaw',
            host_task_key='meeting:'+uuid4().hex,endpoint_key=self.account['endpoint_key'],client_type='web',
            external_subject=self.account['account_id'],conversation_ref=self.account['openclaw_session_key'],
            title='合成会议回复验收')['task']['taskId']
        capability='oa.workflow.pending.batch.prepare' if batch else 'oa.meeting.reply.prepare'
        arguments={'affair_ids':['-11']} if batch else {'affair_id':'-11'}
        result=self.service.invoke(user_subject=OWNER,capability_name=capability,arguments=arguments,
                                  task_id=task,idempotency_key=uuid4().hex)
        assert result['status']=='requires_user_action',result
        return {'task_id':task,'input_id':result['nextAction']['inputSubmissionId'],
                'input_interaction':result['interaction']['interactionId']}
