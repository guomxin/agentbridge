"""Old online routes are retired; query/file isolation lives in test_database_multisource."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from starlette.testclient import TestClient
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server,validate_central_mcp_server_config

class RetirementTests(unittest.TestCase):
    def test_existing_token_authenticates_but_all_old_tools_are_absent_and_denied(self):
        with tempfile.TemporaryDirectory() as root:
            service=CentralCapabilityService(home=root,base_url='http://127.0.0.1:1')
            identities=McpIdentityTokenStore(service.db_path)
            identity=identities.issue(user_subject='a',expected_principal_ref='unused')
            with closing(sqlite3.connect(service.db_path)) as c,c:
                c.execute('UPDATE mcp_identity_tokens SET scopes_json=? WHERE token_id=?',
                    (json.dumps(['taihua:read','taihua:analytics:read','taihua:analytics:export']),identity['token_id']))
            self.assertIsNotNone(identities.verify(identity['token']))
            server=create_central_mcp_server(service=service,identity_store=identities,
                config=validate_central_mcp_server_config(host='127.0.0.1',port=8790,public_base_url='http://testserver',tls_cert=None,tls_key=None),
                auth_card_base_url='http://127.0.0.1:8780')
            headers={'Accept':'application/json, text/event-stream','Authorization':'Bearer '+identity['token'],'MCP-Protocol-Version':'2025-06-18'}
            with TestClient(server.streamable_http_app()) as client:
                def request(method,params):
                    return client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':'test','method':method,'params':params}).json()['result']
                names={x['name'] for x in request('tools/list',{})['tools']}
                self.assertFalse(any(n.startswith('taihua_analytics_') for n in names))
                self.assertIn('taihua_work_log_my_list',names)
                for name in ('personal_summary','result_get','report_export','report_download'):
                    self.assertTrue(request('tools/call',{'name':'taihua_analytics_'+name,'arguments':{}})['isError'])
                result=request('tools/call',{'name':'database_execute','arguments':{'source_id':'taihua_primary','capability':'database.free.read','arguments':{'sql':'select 1'}}})['structuredContent']
                self.assertEqual(result['code'],'DATABASE_CAPABILITY_DENIED')
            catalog=service.planning_catalog(granted_scopes=['taihua:read','taihua:analytics:read','taihua:analytics:export'])
            self.assertNotIn('taihua.analytics.personal.summary',json.dumps(catalog))
            self.assertNotIn('taihua_personal_compare',json.dumps(catalog))

    def test_old_workspace_download_does_not_load_old_payload(self):
        from unittest.mock import MagicMock
        from agentbridge.workspace.application import WorkspaceApplication,WorkspaceArtifactError
        obj=MagicMock()
        with self.assertRaises(WorkspaceArtifactError):
            WorkspaceApplication.analytics_report(obj,{'user_subject':'a'},'a'*32)
        self.assertEqual(obj.service.mock_calls, [])

    def test_old_runtime_and_cli_scopes_are_removed_without_touching_history(self):
        import argparse
        import importlib.util
        from pathlib import Path
        from agentbridge.cli.main import build_parser
        from agentbridge.core.sessions import SessionRegistry
        pending = [build_parser()]
        while pending:
            parser = pending.pop()
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    pending.extend(action.choices.values())
                elif action.choices is not None:
                    self.assertFalse(any(str(x).startswith('taihua:analytics:') for x in action.choices))
        self.assertFalse(hasattr(CentralCapabilityService, 'analytics_runtime'))
        self.assertFalse(hasattr(SessionRegistry, 'bind_analytics_principal'))
        for module in ('admin', 'central', 'comparison', 'results', 'reports', 'taihua_personal', 'visibility'):
            try:
                spec = importlib.util.find_spec('agentbridge.analytics.' + module)
            except ModuleNotFoundError:
                spec = None
            self.assertIsNone(spec)
        with tempfile.TemporaryDirectory() as home:
            history = Path(home) / 'analytics' / 'results' / 'preserved.bin'
            history.parent.mkdir(parents=True)
            history.write_bytes(b'historical encrypted payload')
            service = CentralCapabilityService(home=home, base_url='http://127.0.0.1:1')
            result = service.invoke(user_subject='a', capability_name='taihua.analytics.report.export', arguments={})
            self.assertEqual(result['error']['code'], 'CAPABILITY_RETIRED')
            self.assertEqual(history.read_bytes(), b'historical encrypted payload')
            with self.assertRaisesRegex(ValueError, 'retired'):
                service.tasks.link_artifact(task_id='unused', user_subject='a', artifact={
                    'artifact_type': 'taihua_personal_csv', 'source_ref': 'a' * 32,
                    'filename': 'old.csv', 'content_type': 'text/csv', 'byte_size': 10,
                    'expires_at': '2099-01-01T00:00:00+00:00',
                    'download_url': '/api/analytics/reports/' + 'a' * 32 + '/download',
                })
