"""Real ledger sequences frozen against 3aaa88d, including restart and rollback."""
from contextlib import closing
from itertools import count
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bscli.core import tasks


def run_sequence(module, root, steps):
    """Shared scenario driver; the frozen run supplies the original git module."""
    serial = count(1)
    with patch.object(module, 'uuid4', side_effect=lambda: f'fixture-{next(serial):05d}'), \
            patch.object(module, '_utc_now', return_value='2030-01-01T00:00:00+00:00'):
        path = Path(root) / 'ledger.db'
        store = module.TaskHubStore(path)
        endpoint, _ = store.ensure_endpoint(
            user_subject='alice', token_id='fixture-token', agent_host='fixture-host',
            endpoint_key='telegram:alice', client_type='telegram', external_subject='alice',
            conversation_ref='conversation', capabilities=['direct_status', 'trusted_interaction'],
        )
        task, _ = store.ensure_task(
            user_subject='alice', agent_host='fixture-host', host_task_key='fixture-task',
            origin_endpoint_id=endpoint['endpoint_id'], active_conversation_ref='conversation',
            title='Synthetic task', summary={'retained': 'original'},
        )
        task_id = task['task_id']
        snapshots = []
        for step in steps:
            failure = None
            try:
                kind = step['kind']
                if kind in {'operation', 'plan_operation'}:
                    arguments = dict(task_id=task_id, user_subject='alice', operation={
                        'operation_id': step['id'], 'user_subject': 'alice',
                        'capability_name': 'synthetic.read', 'status': step['state'],
                        'error': {'code': 'SYNTHETIC'} if step['state'] in {'failed', 'unknown'} else None,
                    })
                    if kind == 'plan_operation':
                        store.link_plan_operation(**arguments, plan_id='plan', step_key='step')
                    else:
                        store.link_operation(**arguments)
                elif kind == 'interaction':
                    store.link_interaction(
                        task_id=task_id, user_subject='alice',
                        interaction_record={'interaction_id': step['id'], 'user_subject': 'alice'},
                        interaction={'interactionId': step['id'], 'type': step.get('type', 'business_input'),
                                     'state': step['state']},
                    )
                elif kind == 'plan_event':
                    store.record_plan_event(task_id=task_id, user_subject='alice',
                                            event_type=step['event'], payload={'planId': 'plan'}, causation_ref='plan')
                elif kind == 'terminal':
                    command = step['command']
                    arguments = {'task_id': task_id, 'user_subject': 'alice'}
                    if command in {'complete_task', 'cancel_task'}:
                        arguments['reason'] = 'synthetic'
                    else:
                        arguments['error_code'] = 'SYNTHETIC'
                        if command == 'fail_task': arguments['message'] = 'synthetic failure'
                    getattr(store, command)(**arguments)
                elif kind == 'batch':
                    store.create_batch(
                        parent_task_id=task_id, user_subject='alice', system_id='oa',
                        capability_name='synthetic.batch.prepare', selection_summary={'synthetic': True},
                        failure_policy='stop_on_failure', items=[
                            {'resource_ref': 'private-'+str(i), 'display_summary': {'title': 'item '+str(i)}}
                            for i in range(1, step.get('count', 2)+1)],
                    )
                elif kind == 'batch_activity':
                    store.record_batch_item_activity(parent_task_id=task_id, user_subject='alice',
                        operation_id=step.get('operation'), interaction_id=step.get('interaction'))
                elif kind == 'batch_complete':
                    store.complete_current_batch_item(parent_task_id=task_id, user_subject='alice',
                        operation_id=step['id'], expected_ordinal=step['ordinal'], result_summary={'verified': True})
                elif kind == 'batch_fail':
                    store.fail_current_batch_item(parent_task_id=task_id, user_subject='alice',
                        operation_id=step['id'], expected_ordinal=step['ordinal'], item_state=step['state'],
                        error_code='SYNTHETIC')
                elif kind == 'restart':
                    store = module.TaskHubStore(path)
                elif kind == 'legacy_active':
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute("UPDATE agent_tasks SET status='active', finished_at=NULL WHERE task_id=?", (task_id,))
                elif kind == 'source_tables':
                    from bscli.core.operations import OperationStore
                    from bscli.core.interactions import InteractionStore
                    OperationStore(path)
                    InteractionStore(path)
                elif kind == 'source_operation':
                    from bscli.core import operations
                    with patch.object(operations, 'uuid4', return_value=step['id']), \
                            patch.object(operations, '_utc_now', return_value='2030-01-01T00:00:00+00:00'):
                        ledger = operations.OperationStore(path)
                        record, _ = ledger.create(user_subject='alice', capability_name='synthetic.read',
                            capability_version='1', input_summary={}, request_id='fixture-request')
                        ledger.mark_succeeded(record['operation_id'], {'verified': True})
                elif kind == 'source_credential':
                    from bscli.core import interactions
                    with patch.object(interactions.secrets, 'token_urlsafe', return_value=step['id']):
                        interactions.InteractionStore(path).register(
                            interaction_type='credential', user_subject='alice', system_id='oa',
                            session_id='session', resource_id='resource', title='Synthetic login',
                            message='Synthetic', display={}, resume_spec={'kind': 'session_login'},
                            created_at='2030-01-01T00:00:00+00:00', expires_at='2030-01-02T00:00:00+00:00',
                        )
                else:
                    raise AssertionError('unknown scenario step: '+kind)
            except Exception as error:
                failure = {'type': type(error).__name__, 'message': str(error)}
            current = store.get_task(task_id, user_subject='alice')
            events = store.list_events(task_id=task_id, user_subject='alice', limit=500)
            with closing(sqlite3.connect(path)) as db:
                observations = db.execute('SELECT interaction_id,last_state FROM task_interactions ORDER BY interaction_id').fetchall()
            snapshots.append({
                'error': failure,
                'task': {k: current[k] for k in ('status', 'version', 'current_operation_id', 'current_interaction_id', 'finished_at', 'summary')},
                'events': [{'type': event['event_type'], 'payload': event['payload'], 'cause': event['causation_ref']} for event in events],
                'outboxCount': len(store.list_outbox(user_subject='alice', limit=500)),
                'observations': [list(row) for row in observations],
                'batch': store.get_batch_for_task(parent_task_id=task_id, user_subject='alice'),
            })
        return snapshots


class TaskStateSequenceTests(unittest.TestCase):
    def test_frozen_sequences_preserve_ledger_events_notifications_and_restarts(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/task_state_sequences.json').read_text())
        for case in fixture['cases']:
            with self.subTest(sequence=case['name']), TemporaryDirectory() as root:
                self.assertEqual(run_sequence(tasks, root, case['steps']), case['expected'])

    def test_external_cancel_transaction_rolls_back_task_batch_and_events_together(self):
        with TemporaryDirectory() as root:
            path = Path(root) / 'ledger.db'
            store = tasks.TaskHubStore(path)
            endpoint, _ = store.ensure_endpoint(user_subject='alice', token_id='fixture-token',
                agent_host='fixture-host', endpoint_key='telegram:alice', client_type='telegram',
                external_subject='alice', conversation_ref='conversation')
            task, _ = store.ensure_task(user_subject='alice', agent_host='fixture-host',
                host_task_key='task', origin_endpoint_id=endpoint['endpoint_id'],
                active_conversation_ref='conversation', title='Synthetic')
            store.create_batch(parent_task_id=task['task_id'], user_subject='alice', system_id='oa',
                capability_name='synthetic.batch.prepare', selection_summary={}, failure_policy='stop_on_failure',
                items=[{'resource_ref': 'private', 'display_summary': {'title': 'synthetic'}}])
            def snapshot():
                with closing(sqlite3.connect(path)) as db:
                    return {table: db.execute('SELECT * FROM '+table).fetchall() for table in
                            ('agent_tasks', 'task_batches', 'task_batch_items', 'task_events', 'notification_outbox', 'user_timeline')}
            before = snapshot()
            with closing(sqlite3.connect(path)) as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN IMMEDIATE')
                result = store.cancel_task(task_id=task['task_id'], user_subject='alice', reason='rollback', connection=db)
                self.assertEqual(result['status'], 'canceled')
                self.assertTrue(db.in_transaction)
                self.assertEqual(snapshot(), before)
                db.rollback()
            self.assertEqual(snapshot(), before)


if __name__ == '__main__':
    unittest.main()
