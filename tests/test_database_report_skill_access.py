"""Exercise old encrypted report delivery through authenticated MCP and Workspace."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from unittest.mock import patch
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.database.independent import DatabaseRejected, IndependentDatabase
from agentbridge.database.reports import Reports
from agentbridge.mcp.central import TASK_CONTEXT_META_KEY, create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.workspace.application import WorkspaceApplication, WorkspaceArtifactError
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.database_fixtures import configured_source
from tests.test_workspace import PASSWORD, _cookies, _create_account, _free_port, _raw_request, _request, _service


def link_report(service, subject, task_id, source_id, delivery, filename):
    rid = delivery['report_id']
    artifact, _ = service.tasks.link_artifact(task_id=task_id, user_subject=subject, artifact={
        'artifact_type': 'database_csv', 'source_ref': f'{rid}:{task_id}', 'filename': filename,
        'content_type': 'text/csv', 'byte_size': delivery['byte_size'],
        'download_url': f'/api/database/reports/{source_id}/{rid}/download',
        'expires_at': delivery['download_expires_at'],
    })
    return artifact['artifact_id']


def seed_report_skill_access(service, *, user_subject, allowed_task_id, denied_task_id, source_id='report_skill_fixture'):
    """Seed an isolated service with two retained files and a query-limited skill for both tasks."""
    configured_source(service.home, source_id)
    runtime = IndependentDatabase(service.home)
    runtime.grants.set(user_subject, ['database.logs.analyze', 'database.free.read', 'database.report.export'],
                       source_id=source_id)
    reports = Reports(runtime)
    queries = {
        'allowed': ('database.logs.analyze', {'start_date': '2026-10-01', 'end_date_exclusive': '2026-10-08'},
                    allowed_task_id, '合成日志统计.csv'),
        'denied': ('database.free.read', {'sql': 'SELECT id FROM analysis.work_logs'},
                   denied_task_id, '合成历史自由查询.csv'),
    }
    files = {}
    for label, (capability, arguments, task_id, filename) in queries.items():
        rows = {'columns': ['metric'], 'rows': [{'metric': label}], 'queried_at': datetime.now(timezone.utc).isoformat(),
                'sql_sha256': 'synthetic-report'}
        with patch.object(runtime, '_execute', return_value=rows):
            exported = reports.export(user_subject, source_id, {'query_capability': capability, 'query_arguments': arguments,
                                                               'request_key': task_id})
        delivery = reports.download(user_subject, source_id, {'report_id': exported['report_id']})
        artifact_id = link_report(service, user_subject, task_id, source_id, delivery, filename)
        files[label] = {'task_id': task_id, 'artifact_id': artifact_id, 'report_id': exported['report_id']}

    author = service.skill_authoring
    draft = author.save(user_subject, proposal={'name': '合成受限报告', 'description': '仅导出日志统计',
        'selection': {'use_when': '日志统计', 'not_for': '自由查询', 'output': 'CSV'}, 'instructions': '使用日志统计。',
        'profiles': {'use': {'database': {'all': ['database.logs.analyze']}, 'execution': {
            'effects': ['read'], 'allow_batch': False, 'capabilities': ['database.logs.analyze', 'database.report.export']}}}},
        request_key='report-skill-fixture')
    sample = author.test_start(user_subject, draft_id=draft['draft_id'], expected_revision=1, profile='use',
                               prompt='合成核验', request_key='report-test')
    author.test_result(user_subject, test_id=sample['test_id'], output='合成核验')
    request = author.submit(user_subject, draft_id=draft['draft_id'], expected_revision=1, request_key='report-submit',
                            reason='合成核验')
    author.decide(actor={'username': 'admin', 'role': 'admin'}, request_id=request['request_id'], decision='approve',
                  reason='合成核验', reviewed_tests=True, manual_quality_reason='限定日志统计')
    binding = service.skills.bind(user_subject, draft['skill_id'], 'use', source_id)
    for task_id in (allowed_task_id, denied_task_id):
        service.skills.attach(user_subject, task_id, binding)
    return {**files, 'binding_id': binding, 'source_id': source_id}


@pytest.fixture
def retained(tmp_path):
    service = _service(str(tmp_path))
    account = _create_account(service, user_subject='report-reader', username='report-reader', endpoint_key='telegram:*:report-reader')
    def new_task():
        task, _ = service.tasks.ensure_task(user_subject=account['user_subject'], agent_host='openclaw',
            host_task_key=str(uuid4()), origin_endpoint_id=account['endpoint_id'],
            active_conversation_ref=account['openclaw_session_key'], title='合成报告权限验收')
        return task['task_id']
    sample = seed_report_skill_access(service, user_subject=account['user_subject'],
                                      allowed_task_id=new_task(), denied_task_id=new_task())
    return service, account, sample, new_task


def test_mcp_checks_saved_query_before_renewing_and_delivering(retained):
    service, account, sample, _ = retained
    subject = account['user_subject']
    source = sample['source_id']
    reports = Reports(IndependentDatabase(service.home))
    store = McpIdentityTokenStore(service.db_path)
    user = store.issue(user_subject=subject, expected_principal_ref='synthetic')
    profile = json.loads(Path('schemas/agent-host/v1/test-vectors.json').read_text())['profiles'][0]['value']
    service.negotiate_host(user_subject=subject, token_id=user['token_id'], profile=profile)
    host = {'version': '1', 'agentHost': 'reference-host', 'hostInstanceId': profile['hostInstanceId'], 'hostVersion': '0.1.0'}
    config = validate_central_mcp_server_config(host='127.0.0.1', port=8790, public_base_url='http://testserver',
                                               tls_cert=None, tls_key=None)
    server = create_central_mcp_server(service=service, identity_store=store, config=config, auth_card_base_url='http://127.0.0.1:8780')
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18',
               'Authorization': 'Bearer ' + user['token']}
    with TestClient(server.streamable_http_app()) as client:
        def download(file, *, bound=False, task_id=None, extra=None):
            meta = {'io.agentbridge/host-context': host}
            if bound:
                meta['agentbridge/skill'] = {'bindingId': sample['binding_id']}
            if task_id:
                meta[TASK_CONTEXT_META_KEY] = {'taskId': task_id}
            return client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'database_execute', 'arguments': {'source_id': source, 'capability': 'database.report.download',
                    'arguments': {'report_id': file['report_id'], **(extra or {})}}, '_meta': meta}}).json()['result']

        denied = sample['denied']
        before = reports.payloads.load(denied['report_id'])
        for context in ({'bound': True}, {'task_id': denied['task_id']}):
            result = download(denied, **context)
            assert result['isError']
            assert '不在本次业务助手模式的执行范围内' in result['content'][0]['text']
            assert reports.payloads.load(denied['report_id']) == before
        spoof = download(denied, bound=True, extra={'query_capability': 'database.logs.analyze'})
        assert spoof['structuredContent']['code'] == 'INVALID_DATABASE_ARGUMENTS'
        assert reports.payloads.load(denied['report_id']) == before
        for file, context in ((sample['allowed'], {'bound': True}), (denied, {})):
            response = download(file, **context)['structuredContent']
            assert response['status'] == 'succeeded'
            assert base64.b64decode(response['result']['file']['content_base64'])

        renew = Reports._download
        def revoke_after_renewal(store, owner, sid, arguments):
            response = renew(store, owner, sid, arguments)
            config = service.skills.config('user:' + owner)
            service.skills.save('user:' + owner, {}, expected_revision=config['revision'], actor='test', reason='fixture')
            return response
        with patch.object(Reports, '_download', revoke_after_renewal):
            revoked = download(sample['allowed'], bound=True)
            assert revoked['isError'] and 'structuredContent' not in revoked


def test_workspace_checks_task_cards_and_direct_report_links(retained):
    service, account, sample, new_task = retained
    subject, source = account['user_subject'], sample['source_id']
    reports = Reports(IndependentDatabase(service.home))
    app = WorkspaceApplication(service=service)
    try:
        denied, allowed = sample['denied'], sample['allowed']
        before = reports.payloads.load(denied['report_id'])
        card, = app.task_detail(account, denied['task_id'])['artifacts']
        assert not card['can_reissue'] and not card['download_url']
        with pytest.raises(WorkspaceArtifactError):
            app.reissue_artifact(account, task_id=denied['task_id'], artifact_id=denied['artifact_id'])
        with pytest.raises(WorkspaceArtifactError):
            app.database_report(account, source, denied['report_id'])
        assert reports.payloads.load(denied['report_id']) == before

        allowed_detail = app.reissue_artifact(account, task_id=allowed['task_id'], artifact_id=allowed['artifact_id'])
        assert allowed_detail['artifacts'][0]['state'] == 'ready'
        assert b'allowed' in app.database_report(account, source, allowed['report_id'])['body']

        personal_query = {'columns': ['metric'], 'rows': [{'metric': 'personal'}], 'queried_at': 'fixture',
                          'sql_sha256': 'fixture'}
        with patch.object(reports.runtime, '_execute', return_value=personal_query):
            standalone = reports.export(subject, source, {'query_capability': 'database.free.read',
                'query_arguments': {'sql': 'SELECT 1 AS metric'}, 'request_key': 'personal-no-task'})
        reports.download(subject, source, {'report_id': standalone['report_id']})
        assert b'personal' in app.database_report(account, source, standalone['report_id'])['body']

        another_task = new_task()
        original = reports.download(subject, source, {'report_id': denied['report_id']})
        other_artifact = link_report(service, subject, another_task, source, original, '合成个人报告.csv')
        other_card, = app.task_detail(account, another_task)['artifacts']
        assert other_card['download_url'].endswith('?artifact_id=' + other_artifact)
        assert b'denied' in app.database_report(account, source, denied['report_id'], artifact_id=other_artifact)['body']
        for artifact_id in (None, denied['artifact_id'], allowed['artifact_id'], str(uuid4())):
            with pytest.raises(WorkspaceArtifactError):
                app.database_report(account, source, denied['report_id'], artifact_id=artifact_id)

        wrong_owner = {**account, 'user_subject': 'another-reader'}
        with pytest.raises(WorkspaceArtifactError):
            app.database_report(wrong_owner, source, denied['report_id'], artifact_id=other_artifact)
        with pytest.raises(WorkspaceArtifactError):
            app.database_report(account, 'another_source', denied['report_id'], artifact_id=other_artifact)
        value = reports.payloads.load(allowed['report_id'])
        value['download_expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        reports.payloads.save(allowed['report_id'], value)
        with pytest.raises(WorkspaceArtifactError, match='文件不可下载'):
            app.database_report(account, source, allowed['report_id'])
        reports.runtime.grants.set(subject, ['database.logs.analyze'], source_id=source)
        with pytest.raises(WorkspaceArtifactError):
            app.reissue_artifact(account, task_id=allowed['task_id'], artifact_id=allowed['artifact_id'])
    finally:
        app.close()


def test_workspace_http_uses_the_saved_artifact_binding(retained):
    service, account, sample, new_task = retained
    subject, source, denied = account['user_subject'], sample['source_id'], sample['denied']
    rid = denied['report_id']
    reports = Reports(IndependentDatabase(service.home))
    # This second, unrestricted task legitimately has a different card for the same personal file.
    other_artifact = link_report(service, subject, new_task(), source, reports.download(subject, source, {'report_id': rid}),
                                 '合成个人报告.csv')
    port = _free_port()
    origin = f'http://127.0.0.1:{port}'
    app = WorkspaceApplication(service=service)
    server = create_workspace_http_server(config=validate_workspace_server_config(host='127.0.0.1', port=port,
        public_base_url=origin, tls_cert=None, tls_key=None), application=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, _ = _request(port, 'POST', '/api/login', origin=origin,
                                      body={'username': account['username'], 'password': PASSWORD})
        assert status == 200
        path = f'/api/database/reports/{source}/{rid}/download'
        for suffix in ('', '?task_id=' + denied['task_id'], '?artifact_id=' + denied['artifact_id']):
            status, _, body = _raw_request(port, 'GET', path + suffix, cookies=_cookies(headers))
            assert status == 409 and 'DATABASE_REPORT_DENIED' in body
        status, _, body = _raw_request(port, 'GET', path + '?artifact_id=' + other_artifact, cookies=_cookies(headers))
        assert status == 200 and 'denied' in body
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
        app.close()


def test_report_expiring_during_authorization_cannot_renew_or_deliver(retained):
    service, account, sample, _ = retained
    file = sample['allowed']
    reports = Reports(IndependentDatabase(service.home))
    stored = reports.payloads.load(file['report_id'])
    expires = datetime.fromisoformat(stored['expires_at'])
    with patch('agentbridge.database.reports.datetime') as clock:
        clock.now.side_effect = [expires - timedelta(seconds=1), expires]
        with pytest.raises(DatabaseRejected, match='DATABASE_REPORT_UNAVAILABLE'):
            reports.download(account['user_subject'], sample['source_id'], {'report_id': file['report_id']})
    assert reports.payloads.load(file['report_id']) == stored
