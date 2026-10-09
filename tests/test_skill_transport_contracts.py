"""Skill contracts exercised through the authenticated MCP and Workspace routes."""
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
import tests.test_central_mcp as mcp_support
import tests.test_workspace as workspace_support


PROPOSAL = {
    'name': '周报方法', 'description': '归纳用户提供的合成文字',
    'selection': {'use_when': '整理周报', 'not_for': '业务执行', 'output': '完成和计划'},
    'instructions': '按依据区分完成与计划，没有输入时追问。',
}
HOST_META = {'io.agentbridge/host-context': {
    'version': '1', 'agentHost': 'openclaw',
    'hostInstanceId': 'openclaw-gateway', 'hostVersion': '0.4.65',
}}


def mcp_call(client, token, action, data, **extra_arguments):
    return mcp_support.CentralMcpTests._request(
        client, 'tools/call', request_id=1, token=token,
        params={
            'name': 'agentbridge_skill_authoring',
            'arguments': {'action': action, 'data': data, **extra_arguments},
            '_meta': HOST_META,
        },
    )


@contextmanager
def mcp_authoring():
    with mcp_support.CentralMcpFixture() as (host, store, token, client):
        service = CentralCapabilityService(home=Path(store.db_path).parent, base_url='http://oa.test')
        host.skill_authoring = service.skill_authoring
        yield service, token, client


@contextmanager
def workspace_authoring():
    with TemporaryDirectory() as home:
        service = CentralCapabilityService(home=home, base_url='http://oa.test')
        workspace_support._create_account(
            service, user_subject='user-a', username='alice', endpoint_key='telegram:*:alice',
        )
        application = WorkspaceApplication(service=service, gateway=None)
        port = workspace_support._free_port()
        origin = f'http://127.0.0.1:{port}'
        server = create_workspace_http_server(
            config=validate_workspace_server_config(
                host='127.0.0.1', port=port, public_base_url=origin,
                tls_cert=None, tls_key=None,
            ),
            application=application,
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            status, headers, _ = workspace_support._request(
                port, 'POST', '/api/login', origin=origin,
                body={'username': 'alice', 'password': workspace_support.PASSWORD},
            )
            if status != 200:
                raise AssertionError(f'test login failed: {status}')
            cookies = workspace_support._cookies(headers)

            def post(action, data, **extra):
                return workspace_support._request(
                    port, 'POST', '/api/skill-drafts', origin=origin,
                    cookies=cookies, csrf=cookies['agentbridge_workspace_csrf'],
                    body={'action': action, 'data': data, **extra},
                )

            yield service, post, port, cookies, origin
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SkillMcpTransportTests(unittest.TestCase):
    def test_complete_authoring_tool_matches_pre_refactor_http_catalog(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'skill_authoring_mcp_tool.json').read_text())
        with mcp_support.CentralMcpFixture() as (_service, _store, token, client):
            response = mcp_support.CentralMcpTests._request(client, 'tools/list', request_id=1, token=token)
        self.assertEqual(response.status_code, 200)
        tool = next(item for item in response.json()['result']['tools'] if item['name'] == 'agentbridge_skill_authoring')
        self.assertEqual(tool, fixture['tool'])

    def test_mcp_rejects_extra_private_and_non_strict_field_values_before_dispatch(self):
        cases = [
            ('list', {'owner': 'user-b'}),
            ('list', {'user_subject': 'user-b'}),
            ('save', {'proposal': PROPOSAL, 'request_key': 'private', '_job': {}}),
            ('scopes', {'scope': 'chat', 'enabled': 1}),
            ('generate', {'material': '合成文字', 'request_key': 'strict', 'automatic': 'false'}),
            ('discover', {'limit': '12'}),
            ('save', {'proposal': {**PROPOSAL, 'name': 7}, 'request_key': 'nested'}),
        ]
        with mcp_support.CentralMcpFixture() as (service, _store, token, client):
            for action, data in cases:
                with self.subTest(action=action, data=data):
                    response = mcp_call(client, token, action, data)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()['result']['isError'])
            service.skill_authoring.dispatch.assert_not_called()

    def test_mcp_uses_token_identity_and_excludes_none_before_domain_dispatch(self):
        with mcp_authoring() as (service, token, client):
            authoring = service.skill_authoring
            with patch.object(authoring, 'dispatch', wraps=authoring.dispatch) as dispatch:
                response = mcp_call(
                    client, token, 'save',
                    {'proposal': PROPOSAL, 'request_key': 'identity', 'draft_id': None, 'expected_revision': None},
                    user_subject='user-b',
                )
                result = response.json()['result']
                self.assertFalse(result['isError'], result)
                self.assertEqual(dispatch.call_args.args, ('user-a', 'save', {
                    'proposal': PROPOSAL, 'request_key': 'identity',
                }))
            saved = result['structuredContent']
            self.assertEqual(authoring.list('user-b')['items'], [])
            self.assertEqual(authoring.list('user-a')['items'][0]['draft_id'], saved['draft_id'])
            response = mcp_call(client, token, 'decide', {})
            self.assertTrue(response.json()['result']['isError'])
            self.assertIn('草稿操作无效', str(response.json()['result']['content']))


class SkillWorkspaceTransportTests(unittest.TestCase):
    def test_workspace_uses_session_identity_and_enforces_authentication_csrf(self):
        with workspace_authoring() as (service, post, port, cookies, origin):
            data = {'proposal': PROPOSAL, 'request_key': 'workspace-identity'}
            self.assertEqual(workspace_support._request(port, 'GET', '/api/skill-drafts')[0], 401)
            self.assertEqual(workspace_support._request(
                port, 'POST', '/api/skill-drafts', origin=origin, cookies=cookies,
                body={'action': 'save', 'data': data},
            )[0], 401)
            status, _, saved = post('save', data, user_subject='user-b', owner='user-b')
            self.assertEqual(status, 200)
            authoring = service.skill_authoring
            self.assertEqual(authoring.list('user-b')['items'], [])
            self.assertEqual(authoring.list('user-a')['items'][0]['draft_id'], saved['draft_id'])
            other = authoring.save('user-b', proposal=PROPOSAL, request_key='other')
            self.assertEqual(post('get', {'draft_id': other['draft_id']})[0], 404)
            for action, values in [
                ('save', {**data, 'owner': 'user-b'}),
                ('save', {**data, '_job': {}}),
                ('decide', {}),
            ]:
                with self.subTest(action=action, values=values):
                    status, _, body = post(action, values)
                    self.assertEqual(status, 400)
                    self.assertEqual(body['error']['code'], 'INVALID_REQUEST')

    def test_workspace_preserves_raw_nulls_while_mcp_omits_them(self):
        data = {'material': '整理合成文字方法', 'request_key': 'null-automatic', 'automatic': None}
        with workspace_authoring() as (service, post, _port, _cookies, _origin):
            authoring = service.skill_authoring
            with patch.object(authoring, 'dispatch', wraps=authoring.dispatch) as dispatch:
                status, _, body = post('generate', data)
                self.assertEqual(status, 400)
                self.assertEqual(body['error']['message'], '自动标识无效')
                self.assertEqual(dispatch.call_args.args, ('user-a', 'generate', data))
            self.assertEqual(authoring.workbench.jobs('user-a')['items'], [])
        with mcp_authoring() as (service, token, client):
            response = mcp_call(client, token, 'generate', data)
            result = response.json()['result']
            self.assertFalse(result['isError'], result)
            self.assertFalse(result['structuredContent']['automatic'])
            self.assertEqual(result['structuredContent']['state'], 'queued')
            self.assertFalse(service.skill_authoring.workbench.status()['worker_configured'])

    def test_both_transports_preserve_domain_and_action_validation(self):
        cases = [
            ('generate', {'material': ' ', 'request_key': 'empty'}, '提炼材料不能为空且不能超过16000字符'),
            ('scopes', {'scope': 'chat'}, '范围开关必须是布尔值'),
            ('get', {}, '草稿操作参数无效'),
            ('list', {'draft_id': 'unrelated'}, '草稿操作参数无效'),
        ]
        with workspace_authoring() as (_service, post, _port, _cookies, _origin):
            for action, data, message in cases:
                with self.subTest(transport='workspace', action=action):
                    status, _, body = post(action, data)
                    self.assertEqual(status, 400)
                    self.assertEqual(body['error']['message'], message)
        with mcp_authoring() as (_service, token, client):
            for action, data, message in cases:
                with self.subTest(transport='mcp', action=action):
                    response = mcp_call(client, token, action, data)
                    result = response.json()['result']
                    self.assertTrue(result['isError'])
                    self.assertIn(message, str(result['content']))


if __name__ == '__main__':
    unittest.main()
