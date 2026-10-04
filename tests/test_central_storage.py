"""Storage construction and explicit maintenance against real temporary ledgers."""
from contextlib import ExitStack
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bscli.core.central_service import CentralCapabilityService
from bscli.core.session_secrets import AesGcmSessionStateProtector, SessionStateStore
from bscli.core.sessions import SessionRegistry
from bscli.core.tasks import TaskHubStore
from bscli.core.user_grants import PERMISSIONS


class CentralStorageTests(unittest.TestCase):
    def test_frozen_constructor_attributes_schema_and_restart_contract(self):
        baseline = json.loads((Path(__file__).parent / 'fixtures/central_storage_contract.json').read_text())
        self.assertEqual(str(inspect.signature(CentralCapabilityService)), baseline['constructor'])
        with TemporaryDirectory() as home:
            service = CentralCapabilityService(home=home, base_url='https://oa.example.test')
            service.user_grants.save('user-a', [next(iter(PERMISSIONS))], expected_revision=0,
                                     actor='test', reason='restart fixture')
            grant = service.user_grants.get('user-a')
            for instance in (service, CentralCapabilityService(home=home, base_url='https://oa.example.test')):
                self.assertEqual(instance.user_grants.get('user-a'), grant)
                for name, type_name in baseline['stores'].items():
                    obj = getattr(instance, name)
                    self.assertEqual(f'{type(obj).__module__}.{type(obj).__name__}', type_name)
                    if hasattr(obj, 'db_path'):
                        self.assertEqual(obj.db_path, Path(home) / 'agentbridge.db')
                with sqlite3.connect(instance.db_path) as db:
                    schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
                self.assertEqual([[r[0], r[1]] for r in schema], baseline['schemaObjects'])
                self.assertEqual(hashlib.sha256(json.dumps(schema, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(), baseline['schemaSha256'])

    def test_injected_secret_store_release_identity_and_home_isolation(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            secrets = SessionStateStore(root / 'injected', protector=AesGcmSessionStateProtector(b'x' * 32))
            with patch.dict(os.environ, {'AGENTBRIDGE_RELEASE_ID': 'storage-test'}):
                a = CentralCapabilityService(home=root / 'a', base_url='https://oa.example.test', session_state_store=secrets)
                b = CentralCapabilityService(home=root / 'b', base_url='https://oa.example.test')
            self.assertIs(a.session_states, secrets)
            self.assertEqual(a.runtime_governance.release_id, 'storage-test')
            self.assertEqual(a.sessions.profile_root, root / 'a' / 'profiles')
            a.sessions.get_or_create(user_subject='only-a', system_id='oa')
            self.assertIsNone(b.sessions.find(user_subject='only-a', system_id='oa'))
            self.assertIs(a.skill_authoring.store, a.skills)
            self.assertIs(a.task_plan_runtime.plans, a.task_plans)

    def test_maintenance_failure_aborts_central_construction(self):
        with TemporaryDirectory() as home:
            with patch.object(TaskHubStore, '_repair_terminal_task_statuses', side_effect=RuntimeError('repair failed')):
                with self.assertRaisesRegex(RuntimeError, 'repair failed'):
                    CentralCapabilityService(home=home, base_url='https://oa.example.test')
            with sqlite3.connect(Path(home) / 'agentbridge.db') as db:
                names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'operations', 'interactions', 'agent_tasks'} <= names)
            self.assertNotIn('task_plans', names)
            # A fresh startup can recover; a failed builder leaves no cached success.
            CentralCapabilityService(home=home, base_url='https://oa.example.test')


class StartupMaintenanceTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'agentbridge.db'
        self.store = TaskHubStore(self.db)
        endpoint, _ = self.store.ensure_endpoint(user_subject='user-a', token_id='token-a',
            agent_host='test', endpoint_key='a', client_type='telegram',
            external_subject='a', conversation_ref='a')
        def task(key, title):
            return self.store.ensure_task(user_subject='user-a', agent_host='test', host_task_key=key,
                origin_endpoint_id=endpoint['endpoint_id'], active_conversation_ref='a', title=title)[0]
        self.certificate = task('certificate', 'Prepare and Deliver One OA Certificate Scan')
        self.store.link_artifact(task_id=self.certificate['task_id'], user_subject='user-a', artifact={
            'artifact_type': 'certificate_scan', 'source_ref': 'legacy', 'filename': 'certificate.pdf',
            'content_type': 'application/pdf', 'byte_size': 10,
            'download_url': 'https://example.test/download/legacy/file', 'expires_at': '2099-01-01T00:00:00+00:00'})
        self.orphan = task('orphan', 'Old task shell')
        self.waiting = task('waiting', 'Input task')
        self.store.link_interaction(task_id=self.waiting['task_id'], user_subject='user-a',
            interaction_record={'interaction_id': 'fields-a', 'user_subject': 'user-a'},
            interaction={'interactionId': 'fields-a', 'type': 'business_input', 'state': 'pending'})
        with self.store._connect() as db:
            db.execute("UPDATE client_endpoints SET client_type='web'")
            db.execute("UPDATE agent_tasks SET created_at='2000-01-01T00:00:00+00:00', updated_at='2000-01-01T00:00:00+00:00' WHERE task_id=?", (self.orphan['task_id'],))
            db.execute('UPDATE task_interactions SET last_state=NULL, last_observed_at=NULL')

    def snapshot(self):
        with sqlite3.connect(self.db) as db:
            return {table: db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall() for table in
                    ('agent_tasks', 'task_interactions', 'task_events', 'task_subscriptions', 'notification_outbox')}

    def test_schema_only_open_preserves_rows_and_explicit_maintenance_is_idempotent(self):
        before = self.snapshot()
        reopened = TaskHubStore(self.db, maintain_on_startup=False)
        self.assertEqual(self.snapshot(), before)
        reopened.run_startup_maintenance()
        self.assertEqual(reopened.get_task(self.certificate['task_id'], user_subject='user-a')['status'], 'succeeded')
        self.assertEqual(reopened.get_task(self.orphan['task_id'], user_subject='user-a')['status'], 'expired')
        self.assertEqual(reopened.get_task(self.waiting['task_id'], user_subject='user-a')['status'], 'waiting_user')
        with reopened._connect() as db:
            self.assertEqual(db.execute('SELECT last_state FROM task_interactions').fetchone()[0], 'pending')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox WHERE state='pending'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM task_subscriptions WHERE state='active'").fetchone()[0], 0)
        after = self.snapshot()
        reopened.run_startup_maintenance()
        self.assertEqual(self.snapshot(), after)

    def test_all_repairs_share_one_connection_and_rollback_on_late_failure(self):
        before = self.snapshot()
        methods = ('_backfill_task_interaction_observations', '_repair_terminal_task_statuses',
                   '_expire_orphan_task_shells', '_reconcile_pull_based_deliveries')
        connections = []
        def wrapped(name, original):
            def call(connection):
                connections.append(connection)
                result = original(connection)
                if name == methods[-1]:
                    self.assertTrue(connection.in_transaction)
                    self.assertEqual(connection.execute('SELECT status FROM agent_tasks WHERE task_id=?', (self.orphan['task_id'],)).fetchone()[0], 'expired')
                    raise RuntimeError('late maintenance failure')
                return result
            return call
        with ExitStack() as stack:
            for name in methods:
                stack.enter_context(patch.object(self.store, name, side_effect=wrapped(name, getattr(self.store, name))))
            with self.assertRaisesRegex(RuntimeError, 'late maintenance failure'):
                self.store.run_startup_maintenance()
        self.assertEqual(len(connections), 4)
        self.assertTrue(all(c is connections[0] for c in connections))
        self.assertEqual(self.snapshot(), before)
        self.store.run_startup_maintenance()
        self.assertNotEqual(self.snapshot(), before)

    def sessions(self):
        registry = SessionRegistry(self.db, self.root / 'profiles')
        for subject, state in [('active', 'active'), ('expired', 'expired')]:
            item = registry.get_or_create(user_subject=subject, system_id='oa')
            with registry._connect() as db:
                db.execute('UPDATE sessions SET state=?, last_user_activity_at=NULL, expired_at=NULL WHERE session_id=?', (state, item['session_id']))
        return registry

    def test_session_schema_only_then_backfill_without_login_events(self):
        registry = self.sessions()
        schema_only = SessionRegistry(self.db, registry.profile_root, maintain_on_startup=False)
        self.assertIsNone(schema_only.find(user_subject='active', system_id='oa')['last_user_activity_at'])
        with registry._connect() as db:
            events = db.execute('SELECT * FROM session_events').fetchall()
        schema_only.run_startup_maintenance()
        for subject, field in [('active', 'last_user_activity_at'), ('expired', 'expired_at')]:
            record = registry.find(user_subject=subject, system_id='oa')
            self.assertEqual(record[field], record['updated_at'])
        schema_only.run_startup_maintenance()
        with registry._connect() as db:
            self.assertEqual(db.execute('SELECT * FROM session_events').fetchall(), events)

    def test_session_backfills_rollback_together(self):
        registry = self.sessions()
        with registry._connect() as db:
            db.execute("CREATE TRIGGER fail_expired_backfill BEFORE UPDATE OF expired_at ON sessions WHEN OLD.state='expired' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture failure'):
            registry.run_startup_maintenance()
        self.assertIsNone(registry.find(user_subject='active', system_id='oa')['last_user_activity_at'])
        self.assertIsNone(registry.find(user_subject='expired', system_id='oa')['expired_at'])
