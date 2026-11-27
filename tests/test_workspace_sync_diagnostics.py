"""Diagnostics distinguish deliberate omissions and platform receipts without bodies."""
from datetime import timedelta
import json
import sqlite3

from agentbridge.core import actionable_results as actions, quick_queries
from agentbridge.core.workspace_sync_diagnostics import snapshot
from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_text_delivery import admit, control, text_claim


def report(service):
    return next(row['workspace_sync'] for row in service.tasks.runtime_diagnostics()['users']
                if row['user_subject'] == OWNER)


def states(value, mode='workspace_text_v2'):
    return {row['state']: row for row in value['ledgers'][mode]['states']}


def test_diagnostics_observe_new_ledgers_and_late_receipts_without_claiming_legacy_ack_delivery(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    admit(fixture)
    first = report(service)
    assert first['admissions'] == [{'host': 'openclaw', 'selection': 'paired', 'count': 1}]
    assert states(first)['pending']['confirmed_receipts'] == 0
    assert first['text_projection']['decisions'] == [
        {'host': 'openclaw', 'kind': 'user', 'decision': 'enqueued', 'count': 1}]
    assert first['text_projection']['paired_questions_without_decision'] == 0
    item, = text_claim(service, endpoint, token)
    assert control(service, endpoint, token, item, 'issue')['available']
    assert control(service, endpoint, token, item, 'begin')['allowed']
    with service.tasks._connect() as db:
        db.execute('UPDATE workspace_text_deliveries SET due=?', ((actions._now()-timedelta(seconds=1)).isoformat(),))
    sending = states(report(service))['sending']
    assert sending['overdue_sending'] == 1 and sending['confirmed_receipts'] == 0
    assert sending['oldest_outstanding_due_at']
    assert control(service, endpoint, token, item, 'ack', outcome='delivery_unknown')['recorded']
    assert states(report(service))['unknown']['confirmed_receipts'] == 0
    assert control(service, endpoint, token, item, 'ack', outcome='sent', receipt=1)['recorded']
    assert states(report(service))['sent']['confirmed_receipts'] == 1

    query = quick_queries.queue_query(service, fixture.account, {'requestKey': 'diagnostic-query-unique'})
    response = actions.execute_preparation(service, OWNER, query['taskId'], query['commandKey'], runtime_context={
        'endpointId': fixture.account['endpoint_id'], 'hostType': 'openclaw', 'hostInstanceId': 'openclaw-gateway'})
    assert response['status'] == 'succeeded'
    final = report(service)
    assert states(final, 'workspace_query_v2')['pending']['count'] == 1
    encoded = json.dumps(final)
    for secret in (token, endpoint['endpoint_key'], endpoint['conversation_ref'], item['pairingRef'],
                   item['deliveryId'], '请列出我的待办记录', 'presentation', 'token_id'):
        assert secret not in encoded
    with service.tasks._connect() as db:
        assert snapshot(db, 'another-user', actions._now().isoformat())['admissions'] == []
        assert not snapshot(db, 'another-user', actions._now().isoformat())['ledgers']['workspace_text_v2']['states']


def test_diagnostics_separate_filtered_text_and_missing_observation_and_do_not_backfill(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch = admit(fixture)
    service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:' + dispatch['idempotency_key'], text='公开前缀 /results/private-synthetic-value')
    with service.tasks._connect() as db:
        db.execute("DELETE FROM workspace_text_projection_decisions WHERE kind='user'")
        before = db.execute('SELECT COUNT(*) FROM workspace_text_projection_decisions').fetchone()[0]
    value = report(service)
    assert value['text_projection']['paired_questions_without_decision'] == 1
    assert value['text_projection']['decisions'] == [
        {'host': 'openclaw', 'kind': 'assistant', 'decision': 'not_public', 'count': 1}]
    assert 'private-synthetic-value' not in json.dumps(value)
    assert value == report(service)
    with service.tasks._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workspace_text_projection_decisions').fetchone()[0] == before


def test_diagnostic_handles_database_without_workspace_sync_schema():
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        assert snapshot(db, OWNER, actions._now().isoformat()) == {'available': False}
        db.execute('CREATE TABLE workspace_query_destinations(question_id TEXT, subject TEXT)')
        assert snapshot(db, OWNER, actions._now().isoformat()) == {
            'available': False, 'reason': 'legacy_destination_schema'}
