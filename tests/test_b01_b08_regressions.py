"""Synthetic coverage for the first-refactor B01–B08 review; no business data."""
import json
from unittest.mock import patch

import pytest

from bscli.adapters.taihua import TaihuaCentralAdapter, TaihuaSessionCheckUnavailable
from bscli.adapters.seeyon_central import SeeyonCentralAdapter
from bscli.adapters.yuque import YuqueCentralAdapter
from bscli.adapters.yuque_content import redact_sensitive_text, _redact_nested_strings
from bscli.workspace.gateway import OpenClawGatewayClient, GatewayRequestError, _history_evidence_for_run
from tests.test_yuque_adapter import FakeYuqueWorker


def test_my_logs_continuation_and_empty_vs_invalid():
    adapter = TaihuaCentralAdapter(base_url='https://logs.example.test')
    rows = [{'id': i + 1, 'logDate': '2026-10-01', 'hours': 1} for i in range(101)]
    args = {'start_date': '2026-10-01', 'end_date': '2026-10-01'}
    with patch.object(adapter, '_authorized_json', return_value=rows):
        first = adapter.list_my_logs(None, args)
        last = adapter.list_my_logs(None, {**args, 'offset': first['nextOffset']})
    assert first['total'] == 101 and first['returnedCount'] == 100
    assert first['truncated'] and first['hasMore'] and not first['coverage']['complete']
    assert last['count'] == 1 and last['nextOffset'] is None and not last['hasMore']
    assert len({row['id'] for row in first['items'] + last['items']}) == 101
    assert sum(row['hours'] for row in first['items'] + last['items']) == 101
    with patch.object(adapter, '_authorized_json', return_value=[]):
        empty = adapter.list_my_logs(None, args)
    assert empty['count'] == 0 and empty['coverage']['complete']
    for bad in [None, {}, {'id': 1}, {'id': 1, 'logDate': '2026-02-30'}]:
        with patch.object(adapter, '_authorized_json', return_value=[*rows, bad]):
            with pytest.raises(TaihuaSessionCheckUnavailable):
                adapter.list_my_logs(None, args)
    with patch.object(adapter, '_authorized_json') as fetch:
        with pytest.raises(ValueError):
            adapter.list_my_logs(None, {'start_date': '2026-02-30'})
        fetch.assert_not_called()
    with patch.object(adapter, '_authorized_json', side_effect=RuntimeError('transport failure')):
        with pytest.raises(RuntimeError, match='transport failure'):
            adapter.list_my_logs(None, args)


def test_oa_body_continuation_checks_revision():
    adapter = SeeyonCentralAdapter(base_url='https://oa.example.test/seeyon/')
    text = 'A' * 12000 + 'TAIL'
    args = {'collection': 'done', 'affair_id': '42', 'text_limit': 12000}
    with patch.object(adapter, '_render_workflow_detail', return_value=({'affair_id': '42'}, {'text': text})):
        first = adapter.get_workflow_detail(None, arguments=args)['detail']
        second = adapter.get_workflow_detail(None, arguments={**args, 'text_offset': first['nextTextOffset'],
                                            'expected_text_revision': first['textRevision']})['detail']
        assert first['textTruncated'] and first['textLength'] == 12004
        assert first['returnedTextLength'] == 12000
        assert first['text'] + second['text'] == text
        assert second['nextTextOffset'] is None
        with pytest.raises(ValueError, match='changed'):
            adapter.get_workflow_detail(None, arguments={**args, 'expected_text_revision': 'old'})


@pytest.mark.parametrize('source,secret', [
    ('帐密：synthetic-value', 'synthetic-value'),
    ('账密=synthetic-value', 'synthetic-value'),
    ('"API_KEY": "synthetic spaced secret"', 'synthetic spaced secret'),
    ("DB_PASSWORD='synthetic secret'", 'synthetic secret'),
    ('postgresql://demo:synthetic-value@server.example:5432/app', 'synthetic-value'),
    ('密码：x', 'x'),
    ('| 地址 | 密码 |\n| --- | --- |\n| server.example:8080 | synthetic-value |', 'synthetic-value'),
    ('| 字段 | 值 |\n| --- | --- |\n| 帐密 | synthetic-value |', 'synthetic-value'),
])
def test_known_secret_formats(source, secret):
    text, categories = redact_sensitive_text(source)
    assert secret not in text
    assert categories
    if 'server.example' in source:
        assert 'server.example' in text


def test_nested_redaction_preserves_locations():
    result, categories = _redact_nested_strings({'title': 'Deployment', 'API_KEY': 'synthetic-value',
        'images': [{'ocrText': '账密: synthetic-ocr'}],
        'links': [{'url': 'redis://demo:synthetic-url@server.example:6379'}]})
    assert 'synthetic-' not in json.dumps(result)
    assert result['title'] == 'Deployment' and 'server.example' in str(result)
    assert categories


def test_yuque_scope_links_and_metadata_redaction():
    adapter = YuqueCentralAdapter(base_url='https://tc-aiot.yuque.com', organization_id=20020375)
    result = adapter.read_document(FakeYuqueWorker(), {'book': '共享文档', 'document': '对接设备清单'})
    assert result['searchScope']['kind'] == 'organization_public_area'
    assert result['searchScope']['includesPrivateSpaces'] is False
    assert result['document']['sourceUrl'].startswith('https://tc-aiot.yuque.com/')
    assert adapter._document_url({'url': '//evil.example/doc'}, {}) is None
    assert adapter._document_url({'url': '/area/book/doc'}, {}) == 'https://tc-aiot.yuque.com/area/book/doc'
    raw = {'id': 1, 'slug': 'doc', 'title': '账密: synthetic-title', 'description': 'API_KEY=synthetic-description'}
    with patch.object(adapter, '_public_area', return_value=({'login': 'area'}, [raw])):
        books = adapter.list_public_books(None)
    assert 'synthetic-' not in str(books)


def history(*messages, session='session-123456789'):
    return {'sessionKey': session, 'messages': [{'role': 'user', 'idempotencyKey': 'run:user'}, *messages]}


def final(text, **kwargs):
    return {'role': 'assistant', 'content': text, 'stopReason': 'stop', **kwargs}


def test_history_requires_final_and_request_identity():
    assert not _history_evidence_for_run(history({'role': 'assistant', 'content': 'preamble'}), 'run')['final_text']
    assert not _history_evidence_for_run(history(final('other', runId='other')), 'run')['final_text']
    assert not _history_evidence_for_run(history(final('other'), session='other'), 'run', session_key='session-123456789')['final_text']
    assert not _history_evidence_for_run(history(final('preamble'), {'role': 'toolResult', 'content': 'done'}), 'run')['final_text']
    assert not _history_evidence_for_run(history({'role': 'user', 'content': 'new'}, final('other')), 'run')['final_text']
    repeated = 'Repeat\nRepeat'
    assert _history_evidence_for_run(history(final(repeated)), 'run')['final_text'] == repeated


def test_stream_final_replaced_with_same_run_history(tmp_path):
    client = OpenClawGatewayClient(url='ws://localhost:1', token_file=tmp_path/'token', state_dir=tmp_path,
                                  retry_sleep=lambda _: None)
    events = [{'type': 'accepted', 'runId': 'run'}, {'type': 'chat', 'runId': 'run', 'state': 'delta', 'text': 'abandoned'},
              {'type': 'chat', 'runId': 'run', 'state': 'final', 'text': 'abandoned\nAnswer\nAnswer'}]
    with patch.object(client, '_stream_payload', return_value=iter(events)), patch.object(client, 'call',
            side_effect=[history({'role': 'assistant', 'content': 'preamble'}), history(final('Answer\nAnswer'))]) as call:
        result = list(client.send_stream(session_key='session-123456789', message='read', idempotency_key='run',
                                             endpoint_key='workspace', grant='g' * 48))
    assert result[-1]['text'] == 'Answer\nAnswer' and result[-1]['replace'] is True
    assert call.call_count == 2


def test_missing_final_history_does_not_replay_or_abort(tmp_path):
    client = OpenClawGatewayClient(url='ws://localhost:1', token_file=tmp_path/'token', state_dir=tmp_path,
                                  retry_sleep=lambda _: None)
    events = [{'type': 'accepted', 'runId': 'run'}, {'type': 'chat', 'runId': 'run', 'state': 'final', 'text': 'untrusted'}]
    with patch.object(client, '_stream_payload', return_value=iter(events)) as send, patch.object(client, 'call', return_value=history()), patch.object(client, 'abort_chat') as abort:
        with pytest.raises(GatewayRequestError, match='awaiting authoritative'):
            list(client.send_stream(session_key='session-123456789', message='read', idempotency_key='run',
                                         endpoint_key='workspace', grant='g' * 48))
    assert send.call_count == 1
    abort.assert_not_called()


def test_sheet_secret_columns_remain_redacted_on_later_pages():
    import zlib
    from bscli.adapters.yuque_content import _render_lake_sheet
    rows = [{'name': 'Synthetic', 'data': {
        '0': {'0': {'v': '地址'}, '1': {'v': '密码'}},
        '1': {'0': {'v': 'server.example:5432'}, '1': {'v': 'synthetic-first'}},
        '2': {'0': {'v': 'server.example:6379'}, '1': {'v': 'synthetic-second'}},
    }}]
    content = json.dumps({'sheet': zlib.compress(json.dumps(rows).encode()).decode('latin1')})
    result = _render_lake_sheet(content, row_offset=2, max_rows=1)
    assert 'synthetic-second' not in result['text']
    assert 'server.example:6379' in result['text']
    assert result['structure']['sheets'][0]['redactedCells'] == 1


def test_search_can_continue_second_page_and_returns_same_origin_location():
    adapter = YuqueCentralAdapter(base_url='https://example.yuque.com', organization_id=1)
    book = {'id': '1', 'slug': 'shared', 'name': 'Shared', 'publicAreaLogin': 'area'}
    calls = []
    def request(worker, method, path):
        from urllib.parse import urlparse, parse_qs
        page = int(parse_qs(urlparse(path).query)['p'][0])
        calls.append(page)
        return {'data': {'totalHits': 2, 'hits': [{'id': page, 'slug': f'doc-{page}',
            'title': 'Target' if page == 2 else 'Candidate', 'type': 'Doc', 'book_id': '1',
            'url': f'/area/shared/doc-{page}'}]}}
    with patch.object(adapter, '_public_area', return_value=({'login': 'area'}, [book])), patch.object(adapter, '_request_json', side_effect=request):
        first = adapter.search_documents(None, {'query': 'server.example', 'limit': 1})
        second = adapter.search_documents(None, {'query': 'server.example', 'limit': 1, 'page': 2})
    assert calls == [1, 2] and first['hasMore'] and not second['hasMore']
    assert second['items'][0]['sourceUrl'] == 'https://example.yuque.com/area/shared/doc-2'


def test_quoted_secret_with_escaped_quote_is_fully_redacted():
    text, _ = redact_sensitive_text(r'API_KEY="synthetic\"tail-value"')
    assert 'synthetic' not in text and 'tail-value' not in text
