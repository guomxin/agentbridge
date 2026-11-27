"""Telegram intake projection can arrive after its original completed task."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from agentbridge.core.actionable_results import _now
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.authorization_fixtures import grant_permissions
from tests.test_pi_workspace_images import pi, paired  # noqa: F401


def test_pi_late_telegram_question_does_not_hide_original_oa_card_on_current_or_refresh(pi, paired):
    service, app, account = pi.service, pi.app, pi.accounts['a']
    grant_permissions(service.user_grants, 'user-a', ['oa.workflow.read','oa.attendance_confirmation.approve'])
    service.skills.save('user:user-a', {'oa-pending':{'profiles':['single','batch']}},
                        expected_revision=0, actor='test', reason='synthetic original result visibility')
    endpoint = service.tasks.endpoint_for_key(user_subject='user-a',agent_host='pi',endpoint_key=paired['endpoint_key'])
    start = _now() - timedelta(seconds=10)
    for number in range(45):
        with patch('agentbridge.core.tasks._utc_now', return_value=(start-timedelta(seconds=number+1)).isoformat()):
            service.tasks.append_timeline_message(user_subject='user-a',source_endpoint_id=endpoint['endpoint_id'],
                message_key=f'old-{number}',role='assistant',text='较早的合成历史。')
    with patch('agentbridge.core.tasks._utc_now', return_value=start.isoformat()):
        task,_ = service.tasks.ensure_task(user_subject='user-a',agent_host='pi',host_task_key=uuid4().hex,
            origin_endpoint_id=endpoint['endpoint_id'],active_conversation_ref=endpoint['conversation_ref'],title='原私聊待办查询')
    operation,_ = service.operations.create(user_subject='user-a',capability_name='oa.workflow.pending.list',
                                            capability_version='1',input_summary={})
    original = [{'affair_id':f'late-original-{i}','title':f'【HR】月度考勤确认单-合成{i}',
                 'category':'协同','sender':f'合成{i}','date':'2026-11-27'} for i in range(2)]
    operation = service.operations.mark_succeeded(operation['operation_id'],
        {'items':original,'count':2,'coverage':{'status':'complete','hasMore':False}})
    service.tasks.link_operation(task_id=task['task_id'],user_subject='user-a',operation=operation)
    for offset, role, text in [(2,'user','查询我的 OA 待办'),(3,'assistant','已读取本人的待办。')]:
        with patch('agentbridge.core.tasks._utc_now', return_value=(start+timedelta(seconds=offset)).isoformat()):
            service.tasks.append_timeline_message(user_subject='user-a',source_endpoint_id=endpoint['endpoint_id'],
                message_key='late-'+role,role=role,text=text)
    with patch.object(service,'invoke',side_effect=AssertionError('History must not re-query or prepare')):
        for _ in range(2):
            current = app.current(account,history_limit=32)
            assert [card['task']['task_id'] for card in current['cards']] == [task['task_id']]
            view = current['cards'][0]['actionableResults']
            assert sum(len(group['targets']) for group in view) == 2
            assert view[0]['sourceTaskId'] == task['task_id'] and view[0]['operationId'] == operation['operation_id']
            assert [target['title'] for group in view for target in group['targets']] == [item['title'] for item in original]
            assert current['timeline']['items'][-2]['role'] == 'user'
            assert current['timeline']['items'][-1]['role'] == 'assistant'
            assert not app.current(pi.accounts['b'],history_limit=32)['cards']
        seen = {entry['entry_id'] for entry in current['timeline']['items']} | {task['task_id']}
        cursor = current['history']['nextCursor']
        assert cursor
        page = app.history_page(account,before_created_at=cursor['createdAt'],before_kind=cursor['kind'],before_id=cursor['id'])
        later = {entry['entry_id'] for entry in page['timeline']['items']} | {card['task']['task_id'] for card in page['cards']}
        assert not seen & later and len(seen | later) == 48
        assert not page['hasMore']
        TelegramPilotLinks(service).revoke(account['account_id'],'pi-office','8801')
        assert not app.current(account,history_limit=32)['cards']
    with service.tasks._connect() as db:
        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM business_draft_decisions').fetchone()
        assert not db.execute('SELECT 1 FROM write_authorizations').fetchone()
