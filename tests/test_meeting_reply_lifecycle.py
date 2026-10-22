import sqlite3
import pytest
from agentbridge.core.business_drafts import BusinessDraftService,BusinessDraftError
from tests.support.meeting_reply_acceptance import MeetingReplyAcceptanceFixture,OWNER


@pytest.mark.parametrize('batch',[False,True])
@pytest.mark.parametrize('attitude',['join','not_join','pending'])
def test_trusted_choice_single_authorization_exact_commit_and_replay(tmp_path,batch,attitude):
    fixture=MeetingReplyAcceptanceFixture(tmp_path)
    try:
        case=fixture.add_meeting_case(batch=batch);drafts=BusinessDraftService(fixture.service)
        opened=drafts.open(case['input_id']);state=opened['state'];token=opened['csrfToken']
        args=dict(source_submission_id=case['input_id'],csrf_token=token,csrf_cookie=token)
        assert state['ui']['profileId']=='oa.meeting.reply'
        assert not state['values'].get('attitude')
        assert fixture.meeting_writes==[]
        with pytest.raises(BusinessDraftError):drafts.preview(**args,expected_revision=state['revision'])
        saved=drafts.save(**args,expected_revision=state['revision'],values={'attitude':attitude,'feedback':'合成说明'})
        ready=drafts.preview(**args,expected_revision=saved['revision'])
        result=drafts.approve(**args,expected_revision=ready['revision'],preview_id=ready['preview']['previewId'],request_key='synthetic-one-decision')
        assert fixture.meeting_writes==[]
        interaction=result['receipt']['interactionId']
        fixture.resume(interaction);fixture.resume(interaction)
        assert len(fixture.meeting_writes)==1
        with sqlite3.connect(fixture.service.db_path) as db:
            assert db.execute('SELECT COUNT(*) FROM business_draft_decisions').fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM write_authorizations WHERE state='consumed'").fetchone()[0]==1
        assert fixture.service.tasks.get_task(case['task_id'],user_subject=OWNER)['status']=='succeeded'
    finally:fixture.close()


@pytest.mark.parametrize('fault',['lost_response','changed_material','permission_revoked'])
def test_no_duplicate_write_after_failure_or_unknown(tmp_path,fault):
    from tests.authorization_fixtures import grant_permissions
    fixture=MeetingReplyAcceptanceFixture(tmp_path)
    try:
        case=fixture.add_meeting_case();drafts=BusinessDraftService(fixture.service)
        opened=drafts.open(case['input_id']);token=opened['csrfToken']
        args=dict(source_submission_id=case['input_id'],csrf_token=token,csrf_cookie=token)
        saved=drafts.save(**args,expected_revision=opened['state']['revision'],values={'attitude':'join','feedback':''})
        ready=drafts.preview(**args,expected_revision=saved['revision'])
        result=drafts.approve(**args,expected_revision=ready['revision'],preview_id=ready['preview']['previewId'],request_key='synthetic-fault-decision')
        if fault=='changed_material':fixture.meeting['meetingVo']['content']='changed'
        elif fault=='permission_revoked':
            existing=fixture.service.user_grants.get(OWNER)
            grant_permissions(fixture.service.user_grants,OWNER,set(existing['permissions'])-{'oa.meeting.reply'})
        else:fixture.reply_fault=fault
        interaction=result['receipt']['interactionId']
        if fault=='permission_revoked':
            for _ in range(2):
                with pytest.raises(PermissionError):fixture.resume(interaction)
        else:
            fixture.resume(interaction);fixture.resume(interaction)
        assert len(fixture.meeting_writes)==(1 if fault=='lost_response' else 0)
        status=fixture.service.tasks.get_task(case['task_id'],user_subject=OWNER)['status']
        assert status=='outcome_unknown' if fault=='lost_response' else status!='succeeded'
    finally:fixture.close()
