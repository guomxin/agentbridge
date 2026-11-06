import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.database.independent import IndependentDatabase, main
from agentbridge.database.sources import Sources
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from tests.database_fixtures import configured_source


@pytest.fixture
def database_client(tmp_path):
    for source in ('taihua_primary', 'team_logs', 'private_logs'):
        configured_source(tmp_path, source)
    runtime = IndependentDatabase(tmp_path)
    store = McpIdentityTokenStore(tmp_path / 'tokens.db')
    tokens = {subject: store.issue(user_subject=subject, expected_principal_ref='unused')['token']
              for subject in ('reader', 'other')}
    service = MagicMock(home=tmp_path)
    server = create_central_mcp_server(
        service=service, identity_store=store,
        config=validate_central_mcp_server_config(host='127.0.0.1', port=8790, public_base_url='http://testserver',
                                                 tls_cert=None, tls_key=None),
        auth_card_base_url='http://127.0.0.1:8780',
    )
    with TestClient(server.streamable_http_app()) as client:
        def call(name, arguments, *, subject='reader'):
            headers = {'Accept': 'application/json, text/event-stream', 'Authorization': 'Bearer ' + tokens[subject],
                       'MCP-Protocol-Version': '2025-06-18'}
            response = client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments},
            })
            return response.json()['result']
        yield runtime, service, call


def test_query_selection_counts_visible_sources_before_matching_the_capability(database_client):
    runtime, _, call = database_client
    query = {'capability': 'database.schema', 'arguments': {}}
    runtime.grants.set('other', ['database.schema'], source_id='private_logs')

    with patch('agentbridge.database.sources.connect') as connect:
        roles = dict(readonly=True, repeatable=True, default_readonly=True, privileged=False,
                     writable=False, sequence_update=False, sequence_usage=False, temp=False)
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = roles
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.description = []
        cursor.fetchmany.return_value = []

        assert call('database_execute', query)['structuredContent']['code'] == 'DATABASE_CAPABILITY_DENIED'
        runtime.grants.set('reader', ['database.schema'], source_id='team_logs')
        unique = call('database_capabilities', {})['structuredContent']
        assert unique['selection_required'] is False
        assert [source['source_id'] for source in unique['sources']] == ['team_logs']
        assert call('database_execute', query)['structuredContent']['source_id'] == 'team_logs'
        assert connect.call_count == 1

        runtime.grants.set('reader', ['database.logs.analyze'], source_id='taihua_primary')
        catalog = call('database_capabilities', {})['structuredContent']
        assert catalog['selection_required'] is True
        assert {source['source_id'] for source in catalog['sources']} == {'taihua_primary', 'team_logs'}
        ambiguous = call('database_execute', query)['structuredContent']
        assert ambiguous['code'] == 'DATABASE_SOURCE_SELECTION_REQUIRED'
        assert ambiguous['recovery']['action'] == 'select_source'
        assert ambiguous['recovery']['retry_same_request'] is False
        assert 'private_logs' not in json.dumps(ambiguous)
        for source in ('taihua_primary', 'private_logs', 'unknown'):
            denied = call('database_execute', {**query, 'source_id': source})['structuredContent']
            assert denied['code'] == 'DATABASE_CAPABILITY_DENIED'
        assert connect.call_count == 1
        assert call('database_execute', {**query, 'source_id': 'team_logs'})['structuredContent']['source_id'] == 'team_logs'

        Sources(runtime.home).write('team_logs', 'disable', revision=1, actor='admin', reason='fixture')
        assert call('database_execute', query)['structuredContent']['code'] == 'DATABASE_CAPABILITY_DENIED'
        assert connect.call_count == 2


def test_report_uses_the_selected_source_and_owner(database_client):
    runtime, _, call = database_client
    runtime.grants.set('reader', ['database.free.read', 'database.report.export'], source_id='team_logs')
    export = {'capability': 'database.report.export', 'arguments': {
        'query_capability': 'database.free.read', 'query_arguments': {'sql': 'SELECT id FROM analysis.work_logs'},
        'request_key': 'fixture-report',
    }}
    rows = {'columns': ['id'], 'rows': [{'id': 1}], 'queried_at': '2026-11-06T00:00:00Z', 'sql_sha256': 'fixture'}
    with patch.object(IndependentDatabase, '_execute', return_value=rows) as execute:
        report = call('database_execute', export)['structuredContent']
        assert report['source_id'] == 'team_logs'
        download = {'capability': 'database.report.download', 'arguments': {'report_id': report['report_id']}}
        assert call('database_execute', download)['structuredContent']['result']['source_id'] == 'team_logs'
        runtime.grants.set('other', ['database.free.read', 'database.report.export'], source_id='team_logs')
        other = call('database_execute', download, subject='other')['structuredContent']
        assert other['code'] == 'DATABASE_REPORT_UNAVAILABLE'
        runtime.grants.set('reader', ['database.schema'], source_id='taihua_primary')
        for operation in (download, export):
            assert call('database_execute', operation)['structuredContent']['code'] == 'DATABASE_SOURCE_SELECTION_REQUIRED'
            wrong = call('database_execute', {**operation, 'source_id': 'taihua_primary'})['structuredContent']
            assert wrong['code'] == 'DATABASE_CAPABILITY_DENIED'
        runtime.grants.set('reader', ['database.free.read', 'database.report.export'], source_id='taihua_primary')
        wrong = call('database_execute', {**download, 'source_id': 'taihua_primary'})['structuredContent']
        assert wrong['code'] == 'DATABASE_REPORT_UNAVAILABLE'
        right = call('database_execute', {**download, 'source_id': 'team_logs'})['structuredContent']
        assert right['result']['report_id'] == report['report_id']
        assert execute.call_count == 1


def test_inferred_source_is_checked_against_the_skill_before_execution(database_client):
    runtime, service, call = database_client
    runtime.grants.set('reader', ['database.schema'], source_id='team_logs')

    def check_source(subject, task_id, *, capability, source_id):
        if source_id is not None and source_id != 'taihua_primary':
            raise PermissionError('source outside task binding')

    service.validate_skill_task.side_effect = check_source
    with patch('agentbridge.database.sources.connect') as connect:
        response = call('database_execute', {'capability': 'database.schema', 'arguments': {}})
        assert response['isError']
        assert 'source outside task binding' in response['content'][0]['text']
        connect.assert_not_called()


def test_cli_returns_recoverable_selection_without_executing_on_the_legacy_source(tmp_path, monkeypatch, capsys):
    runtime = IndependentDatabase(tmp_path)
    for source in ('taihua_primary', 'team_logs'):
        configured_source(tmp_path, source)
        runtime.grants.set('reader', ['database.schema'], source_id=source)
    monkeypatch.setattr(sys, 'argv', ['database', '--home', str(tmp_path), '--subject', 'reader',
                                     'execute', '--capability', 'database.schema'])
    with patch('agentbridge.database.sources.connect') as connect, pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['code'] == 'DATABASE_SOURCE_SELECTION_REQUIRED'
    assert result['recovery']['action'] == 'select_source'
    connect.assert_not_called()
