"""Trusted short trials use the durable dispatch and real authoring receipts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from agentbridge.core.user_grants import UserGrantConflict
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.gateway import OpenClawGatewayClient
from agentbridge.workspace.stores import WorkspaceConflictError
from tests.test_workspace import FakeGateway, _create_account, _service
from tests.test_workspace_acceptance import _image


PROPOSAL = {
    'name': '日志计划核对', 'description': '区分已完成事项与下周计划',
    'selection': {'use_when': '整理工作日志', 'not_for': '直接提交日志', 'output': '完成与计划'},
    'instructions': '仅用输入文字，不把计划描述为已经完成。',
    'profiles': {'preview': {}, 'fill': {}},
}


class WorkspaceSkillTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = _service(self.temp.name)
        self.account = _create_account(self.service, user_subject='user-a', username='alice', endpoint_key='telegram:*:alice')
        self.other = _create_account(self.service, user_subject='user-b', username='bob', endpoint_key='telegram:*:bob')
        self.authoring = self.service.skill_authoring
        self.draft = self.authoring.save('user-a', proposal=PROPOSAL, request_key='create')
        self.trial = {'draft_id': self.draft['draft_id'], 'expected_revision': 1, 'profile': 'preview'}
        self.app = WorkspaceApplication(service=self.service, gateway=FakeGateway())
        self.addCleanup(self.app.close)
        worker = patch.object(self.app, '_ensure_dispatch_worker')
        worker.start()
        self.addCleanup(worker.stop)

    def accept(self, *, account=None, trial=None, key='trial-1', message='下周计划：修复登录问题。', attachments=None):
        self.app.send_chat_stream(account or self.account, message=message, idempotency_key=key,
                                  skill_trial=self.trial if trial is None else trial, attachments=attachments)
        return self.service.workspace.list_host_dispatches(user_subject=(account or self.account)['user_subject'])[0]

    def binding(self, account=None, turn='trial-1'):
        account = account or self.account
        grant = self.service.workspace.issue_gateway_grant(account['account_id'])
        return self.service.redeem_workspace_gateway_grant(user_subject=account['user_subject'], agent_host='openclaw',
            endpoint_key=grant['endpoint_key'], session_key=grant['session_key'], grant=grant['grant'], turn_ref=turn)['binding']

    def sample(self, binding, output='计划：修复登录问题，尚未完成。'):
        data = {key: binding[key] for key in ('draft_id', 'expected_revision', 'profile', 'prompt', 'request_key')}
        self.authoring.guard_workspace_trial('user-a', binding['dispatch_id'], 'test', data)
        sample = self.authoring.test_start('user-a', **data)
        if output is not None:
            self.authoring.test_result('user-a', test_id=sample['test_id'], output=output)
        return sample

    def finish(self, dispatch, *, state='final', text='模型声称：已保存样例。'):
        claimed = self.service.workspace.claim_next_host_dispatch(account_id=self.account['account_id'], claim_owner='test')
        self.assertEqual(claimed['dispatch_id'], dispatch['dispatch_id'])
        self.app._finish_accepted_dispatch(claimed, self.account,
            {'type': 'chat', 'state': state, 'text': text, 'skillTrial': {'status': 'reported', 'test_id': 'forged'}})
        return self.service.workspace.list_host_dispatch_events(dispatch['dispatch_id'])[-1]['payload']

    def test_validated_trial_is_atomic_and_bound_to_prompt_owner_revision_and_profile(self):
        invalid = [
            (self.other, self.trial, KeyError),
            (self.account, {**self.trial, 'expected_revision': 2}, UserGrantConflict),
            (self.account, {**self.trial, 'profile': 'missing'}, ValueError),
            (self.account, {**self.trial, 'expected_revision': True}, ValueError),
            (self.account, {**self.trial, 'prompt': 'spoof'}, ValueError),
        ]
        for account, trial, error in invalid:
            with self.subTest(trial=trial, owner=account['user_subject']), self.assertRaises(error):
                self.accept(account=account, trial=trial)
        for message, attachments in [('x' * 3001, None), ('', None), ('x', [_image()])]:
            with self.subTest(message_length=len(message)), self.assertRaises(ValueError):
                self.accept(message=message, attachments=attachments)
        with sqlite3.connect(self.service.db_path) as db:
            for table in ('agent_host_dispatches', 'user_timeline', 'timeline_attachments'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.authoring.archive('user-a', draft_id=self.draft['draft_id'], expected_revision=1)
        with self.assertRaisesRegex(ValueError, '归档'):
            self.accept()

    def test_retry_reuses_accepted_dispatch_after_revision_changes_but_payload_changes_conflict(self):
        dispatch = self.accept()
        self.authoring.save('user-a', draft_id=self.draft['draft_id'], expected_revision=1,
                            proposal={**PROPOSAL, 'name': '新的名字'}, request_key='edit')
        with ThreadPoolExecutor(max_workers=2) as pool:
            retries = list(pool.map(lambda _: self.accept(), range(2)))
        self.assertTrue(all(item['dispatch_id'] == dispatch['dispatch_id'] for item in retries))
        for trial, message in [({**self.trial, 'profile': 'fill'}, '下周计划：修复登录问题。'),
                               ({**self.trial, 'expected_revision': 2}, '下周计划：修复登录问题。'),
                               (self.trial, '已完成：修复登录问题。')]:
            with self.assertRaisesRegex(WorkspaceConflictError, 'IDEMPOTENCY_PAYLOAD_MISMATCH'):
                self.accept(trial=trial, message=message)
        binding = self.binding()['skillTrial']
        self.assertEqual(binding['expected_revision'], 1)
        self.assertEqual(binding['name'], PROPOSAL['name'])
        with self.assertRaises(UserGrantConflict):
            self.sample(binding)
        self.assertEqual(len(self.service.workspace.list_host_dispatches(user_subject='user-a')), 1)

    def test_gateway_binding_is_account_and_explicit_turn_scoped_and_recovers_test_id(self):
        dispatch = self.accept()
        trial = self.binding()['skillTrial']
        self.assertEqual(trial, {**self.trial, 'name': PROPOSAL['name'], 'prompt': '下周计划：修复登录问题。',
            'request_key': 'workspace-sample:' + dispatch['dispatch_id'], 'dispatch_id': dispatch['dispatch_id']})
        self.assertNotIn('skillTrial', self.binding(self.other))
        self.assertNotIn('skillTrial', self.binding(turn=None))
        self.assertNotIn('skillTrial', self.binding(turn='another-turn'))
        self.assertIsNone(self.service.workspace.skill_trial_for_dispatch(dispatch['dispatch_id'], user_subject='user-b'))
        self.assertIsNone(self.service.workspace.skill_trial_for_dispatch(dispatch['dispatch_id'], user_subject='user-a', account_id=self.other['account_id']))
        sample = self.sample(trial)
        self.assertEqual(self.binding()['skillTrial']['test_id'], sample['test_id'])
        self.authoring.save('user-a', draft_id=self.draft['draft_id'], expected_revision=1,
                            proposal={**PROPOSAL, 'instructions': '新增处理方法'}, request_key='edit')
        self.assertEqual(self.sample(self.binding()['skillTrial']), sample)
        resolved = self.service.resolve_workspace_gateway_session(user_subject='user-a', agent_host='openclaw',
                                                                  session_key=self.account['openclaw_session_key'])
        self.assertNotIn('skillTrial', resolved['binding'])

    def test_terminal_status_requires_matching_reported_receipt_and_excludes_automatic_capture(self):
        self.authoring.preferences('user-a', value={'auto_draft': True}, expected_revision=0)
        self.authoring.workbench.scopes('user-a', scope='workspace:' + self.account['account_id'], enabled=True)
        dispatch = self.accept()
        binding = self.binding()['skillTrial']
        sample = self.sample(binding)
        event = self.finish(dispatch)
        self.assertEqual(event['skillTrial'], {'draft_id': self.draft['draft_id'], 'name': PROPOSAL['name'],
            'revision': 1, 'profile': 'preview', 'dispatch_id': dispatch['dispatch_id'], 'test_id': sample['test_id'], 'status': 'reported'})
        entry = self.service.tasks.get_timeline_message(user_subject='user-a', message_key='workspace:assistant:trial-1')
        self.assertEqual(entry['payload']['skillTrial'], event['skillTrial'])
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM skill_auto_capture').fetchone()[0], 0)
        with patch.object(self.authoring.workbench, 'generate') as generate:
            self.app._capture_skill_method({'dispatch_id': dispatch['dispatch_id'], 'owner_subject': 'user-a', 'assistant_text': '方法'})
            generate.assert_not_called()

    def test_model_claim_or_running_sample_does_not_mark_saved(self):
        for index, started in enumerate((False, True)):
            with self.subTest(started=started):
                key = f'trial-{index}'
                dispatch = self.accept(key=key)
                if started:
                    self.sample(self.binding(turn=key)['skillTrial'], output=None)
                event = self.finish(dispatch)
                self.assertEqual(event['skillTrial']['status'], 'not_reported')
                self.assertNotEqual(event['skillTrial']['test_id'], 'forged')
                self.assertEqual(event['text'], '模型声称：已保存样例。')
                entry = self.service.tasks.get_timeline_message(user_subject='user-a',
                    message_key='workspace:assistant:' + key)
                self.assertEqual(entry['text'], event['text'])
                self.assertEqual(entry['payload']['skillTrial'], event['skillTrial'])

    def test_host_trial_meta_guard_rejects_changed_arguments_and_other_sample_ids(self):
        dispatch = self.accept()
        binding = self.binding()['skillTrial']
        data = {key: binding[key] for key in ('draft_id', 'expected_revision', 'profile', 'prompt', 'request_key')}
        for key, value in [('draft_id', 'other'), ('expected_revision', 2), ('profile', 'fill'),
                           ('prompt', 'another prompt'), ('request_key', 'another request')]:
            with self.subTest(key=key), self.assertRaises(PermissionError):
                self.authoring.guard_workspace_trial('user-a', dispatch['dispatch_id'], 'test', {**data, key: value})
        with self.assertRaises(PermissionError):
            self.authoring.guard_workspace_trial('user-b', dispatch['dispatch_id'], 'test', data)
        with self.assertRaises(PermissionError):
            self.authoring.guard_workspace_trial('user-a', dispatch['dispatch_id'], 'save', {})
        with self.assertRaises(PermissionError):
            self.authoring.guard_workspace_trial('user-a', dispatch['dispatch_id'], 'test_result', {'test_id': 'unknown', 'output': 'x'})
        sample = self.sample(binding, output=None)
        self.authoring.guard_workspace_trial('user-a', dispatch['dispatch_id'], 'test_result', {'test_id': sample['test_id'], 'output': '计划'})

    def test_archive_retains_existing_receipt_and_rejects_a_new_sample(self):
        dispatch = self.accept()
        binding = self.binding()['skillTrial']
        sample = self.sample(binding)
        self.authoring.archive('user-a', draft_id=self.draft['draft_id'], expected_revision=1)
        self.assertEqual(self.accept()['dispatch_id'], dispatch['dispatch_id'])
        self.assertEqual(self.sample(binding), sample)
        with self.assertRaisesRegex(ValueError, '归档'):
            self.authoring.test_start('user-a', draft_id=self.draft['draft_id'], expected_revision=1,
                profile='preview', prompt=binding['prompt'], request_key='new-sample')

    def test_dispatch_worker_passes_required_binding_and_uses_server_status(self):
        dispatch = self.accept()
        claimed = self.service.workspace.claim_next_host_dispatch(account_id=self.account['account_id'], claim_owner='test')
        events = iter([{'type': 'accepted', 'runId': 'native-run', 'status': 'started'},
                       {'type': 'progress', 'kind': 'tool', 'skillTrial': {'status': 'reported', 'test_id': 'forged'}},
                       {'type': 'chat', 'runId': 'native-run', 'state': 'final', 'text': '试用输出'}])
        with patch.object(self.app.gateway, 'send_stream', return_value=events) as send:
            self.app._process_host_dispatch(claimed)
        self.assertIs(send.call_args.kwargs['require_trial_binding'], True)
        event = self.service.workspace.list_host_dispatch_events(dispatch['dispatch_id'])[-1]['payload']
        self.assertEqual(event['skillTrial']['status'], 'not_reported')
        self.assertEqual(event['runId'], 'native-run')
        public = list(self.app._stream_host_dispatch(user_subject='user-a', dispatch_id=dispatch['dispatch_id']))
        self.assertTrue(all('skillTrial' not in event for event in public if event.get('type') != 'chat'))

    def test_reported_receipt_recovers_a_lost_host_final_without_replaying(self):
        dispatch = self.accept()
        self.sample(self.binding()['skillTrial'])
        claimed = self.service.workspace.claim_next_host_dispatch(account_id=self.account['account_id'], claim_owner='test')
        accepted = self.service.workspace.mark_host_dispatch_accepted(dispatch['dispatch_id'],
            claim_token=claimed['claim_token'], run_id='recovered-native-run', status='recovered')
        with patch.object(self.app.gateway, 'send_stream') as send, patch.object(self.app.gateway, 'run_history_evidence', create=True) as history:
            self.app._observe_accepted_dispatch(accepted)
        send.assert_not_called()
        history.assert_not_called()
        event = self.service.workspace.list_host_dispatch_events(dispatch['dispatch_id'])[-1]['payload']
        self.assertEqual(event['skillTrial']['status'], 'reported')
        self.assertTrue(event['recovered'])
        self.assertEqual(event['text'], '计划：修复登录问题，尚未完成。')

    def test_canceled_trial_stream_and_timeline_do_not_claim_saved(self):
        dispatch = self.accept()
        self.app.cancel_chat_dispatch(self.account, dispatch['dispatch_id'])
        events = list(self.app._stream_host_dispatch(user_subject='user-a', dispatch_id=dispatch['dispatch_id']))
        self.assertEqual(events[-1]['skillTrial']['status'], 'not_reported')
        entry = self.service.tasks.get_timeline_message(user_subject='user-a', message_key='workspace:assistant:error:trial-1')
        self.assertEqual(entry['payload']['skillTrial']['status'], 'not_reported')

    def test_receipt_from_another_test_cannot_mark_this_dispatch_reported(self):
        dispatch = self.accept()
        binding = self.binding()['skillTrial']
        other = self.authoring.test_start('user-a', draft_id=self.draft['draft_id'], expected_revision=1,
            profile='fill', prompt='另一个问题', request_key=binding['request_key'])
        self.authoring.test_result('user-a', test_id=other['test_id'], output='已保存另一个模式')
        event = self.finish(dispatch)
        self.assertEqual(event['skillTrial']['status'], 'not_reported')
        self.assertIsNone(event['skillTrial']['test_id'])

    def test_python_gateway_payload_demands_trial_aware_host(self):
        token = Path(self.temp.name) / 'gateway.token'
        token.write_text('test-token')
        token.chmod(0o600)
        client = OpenClawGatewayClient(url='ws://127.0.0.1:18789', token_file=token, state_dir=self.temp.name)
        with patch.object(client, '_stream_payload', return_value=iter([{'type': 'chat', 'state': 'final', 'text': 'x'}])) as stream:
            list(client.send_stream(session_key=self.account['openclaw_session_key'], endpoint_key=self.account['endpoint_key'],
                grant='g' * 32, message='合成样例', idempotency_key='one', require_trial_binding=True))
        self.assertIs(stream.call_args.args[0]['requireTrialBinding'], True)

    def test_dedicated_http_trial_route_is_authenticated_strict_and_never_ordinary_chat(self):
        from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
        from tests.test_workspace import PASSWORD, _cookies, _free_port, _raw_request, _request
        port = _free_port()
        origin = f'http://127.0.0.1:{port}'
        server = create_workspace_http_server(config=validate_workspace_server_config(
            host='127.0.0.1', port=port, public_base_url=origin, tls_cert=None, tls_key=None), application=self.app)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, timeout=2)
        self.addCleanup(server.shutdown)
        path = '/api/skill-trials/send-stream'
        status, headers, _ = _request(port, 'POST', '/api/login', origin=origin,
            body={'username': 'alice', 'password': PASSWORD})
        self.assertEqual(status, 200)
        cookies = _cookies(headers)
        args = {'origin': origin, 'cookies': cookies, 'csrf': cookies['agentbridge_workspace_csrf']}
        payload = {'message': '计划修复登录', 'idempotencyKey': 'http-trial', 'skillTrial': self.trial}
        status, _, _ = _request(port, 'POST', path, origin=origin, body=payload)
        self.assertEqual(status, 401)
        for trial in (None, {}, {**self.trial, 'profile': 'missing'}, {**self.trial, 'expected_revision': 2}):
            with self.subTest(trial=trial):
                status, headers, body = _request(port, 'POST', path, **args, body={**payload, 'skillTrial': trial})
                self.assertIn(status, (400, 409))
                self.assertEqual(headers.get_content_type(), 'application/json')
        with patch.object(self.app, 'send_chat_stream', wraps=self.app.send_chat_stream) as send:
            for ordinary in ('/api/chat/send', '/api/chat/send-stream'):
                status, headers, _ = _request(port, 'POST', ordinary, **args, body=payload)
                self.assertEqual(status, 400)
                self.assertEqual(headers.get_content_type(), 'application/json')
            send.assert_not_called()
        self.assertEqual(self.service.workspace.list_host_dispatches(user_subject='user-a'), [])
        with patch.object(self.app, '_stream_host_dispatch', return_value=iter([
            {'type': 'accepted', 'runId': 'http-trial', 'status': 'started'},
        ])):
            status, headers, wire = _raw_request(port, 'POST', path, **args, body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), 'text/event-stream')
        self.assertIn('event: accepted', wire)
        trial = self.service.workspace.skill_trial_for_turn(account_id=self.account['account_id'],
            user_subject='user-a', turn_ref='http-trial')
        self.assertEqual(trial['prompt'], payload['message'])
        self.assertEqual(trial['profile'], 'preview')
        self.assertEqual(self.app.gateway.calls, [])


class WorkspaceSkillTrialMcpTests(unittest.TestCase):
    def test_authenticated_mcp_meta_guard_applies_before_authoring_dispatch(self):
        from tests.test_skill_transport_contracts import HOST_META, mcp_authoring
        from tests.test_central_mcp import CentralMcpTests

        with mcp_authoring() as (service, token, client):
            account = _create_account(service, user_subject='user-a', username='alice', endpoint_key='telegram:*:alice')
            draft = service.skill_authoring.save('user-a', proposal=PROPOSAL, request_key='create')
            app = WorkspaceApplication(service=service, gateway=FakeGateway())
            try:
                with patch.object(app, '_ensure_dispatch_worker'):
                    app.send_chat_stream(account, message='下周计划：修复登录问题。', idempotency_key='trial',
                        skill_trial={'draft_id': draft['draft_id'], 'expected_revision': 1, 'profile': 'preview'})
                trial = service.workspace.skill_trial_for_turn(account_id=account['account_id'], user_subject='user-a', turn_ref='trial')
                data = {key: trial[key] for key in ('draft_id', 'expected_revision', 'profile', 'prompt', 'request_key')}

                def call(action, data, meta=None):
                    return CentralMcpTests._request(client, 'tools/call', request_id=1, token=token, params={
                        'name': 'agentbridge_skill_authoring', 'arguments': {'action': action, 'data': data},
                        '_meta': {**HOST_META, 'agentbridge/skill-trial': meta or {'dispatchId': trial['dispatch_id']}},
                    }).json()['result']

                for action, arguments in [('test', {**data, 'profile': 'fill'}), ('save', {'proposal': PROPOSAL, 'request_key': 'spoof'})]:
                    self.assertTrue(call(action, arguments)['isError'])
                self.assertTrue(call('test', data, {'dispatchId': trial['dispatch_id'], 'profile': 'preview'})['isError'])
                result = call('test', data)
                self.assertFalse(result['isError'], result)
                test_id = result['structuredContent']['test_id']
                self.assertTrue(call('test_result', {'test_id': 'other', 'output': '保存'})['isError'])
                result = call('test_result', {'test_id': test_id, 'output': '计划：修复登录问题，未完成。'})
                self.assertFalse(result['isError'], result)
                self.assertEqual(result['structuredContent']['status'], 'reported')
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
