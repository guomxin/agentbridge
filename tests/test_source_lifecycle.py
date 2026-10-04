"""Source initialization reuse never turns into cached authorization or secrets."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from bscli.core.data_source_secrets import DataSourceSecretStore
from bscli.core.session_secrets import SESSION_KEY_FILE_ENV, SessionSecretError
from bscli.database.independent import IndependentDatabase, DatabaseRejected
from bscli.database.legacy_source import LegacySourceConfig
from bscli.database.sources import Sources
from tests.database_fixtures import configured_source


class SourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        configured_source(self.home, 'equipment', pack='generic')
        self.runtime = IndependentDatabase(self.home)
        self.runtime.grants.set('a', ['database.free.read'], source_id='equipment')

    def snapshot(self):
        return self.runtime.snapshot('a', 'database.free.read', 'equipment')

    def test_repeated_reads_issue_ddl_only_once_and_read_live_records(self):
        sql = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(sql.append)
            return connection
        with patch('bscli.database.sources.sqlite3.connect', side_effect=traced):
            for _ in range(20):
                self.snapshot()
        ddl = [s for s in sql if s.lstrip().upper().startswith('CREATE TABLE')]
        self.assertEqual(len(ddl), 2)
        self.assertEqual(sum(s.startswith('SELECT record FROM sources WHERE') for s in sql), 40)
        grant, record = self.snapshot()
        record['active']['name'] = 'caller-local edit'
        self.assertEqual(self.snapshot()[1]['active']['name'], 'equipment')
        self.runtime.grants.set('a', [], source_id='equipment')
        with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_CAPABILITY_DENIED'):
            self.snapshot()

    def test_concurrent_first_reads_initialize_once(self):
        barrier = Barrier(8)
        original = Sources._initialize
        def read(_):
            barrier.wait(timeout=5)
            return self.snapshot()[1]['source_id']
        with patch.object(Sources, '_initialize', autospec=True, side_effect=original) as initialize:
            with ThreadPoolExecutor(max_workers=8) as executor:
                self.assertEqual(list(executor.map(read, range(8))), ['equipment'] * 8)
        initialize.assert_called_once()

    def test_failed_initialization_can_retry_and_instances_are_isolated(self):
        original = Sources._initialize
        calls = []
        def initialize(store):
            calls.append(store.home)
            if len(calls) == 1:
                raise sqlite3.OperationalError('fixture initialization failure')
            original(store)
        with patch.object(Sources, '_initialize', autospec=True, side_effect=initialize):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture initialization failure'):
                self.snapshot()
            self.snapshot()
            other = IndependentDatabase(self.home / 'other')
            self.assertEqual(other.catalog('a')['sources'], [])
        self.assertEqual(calls, [self.home, self.home, self.home / 'other'])

    def test_each_handle_rechecks_key_file_and_uses_rotated_key(self):
        key = self.home / 'isolated-key'
        key.write_bytes(b'a' * 32)
        key.chmod(0o600)
        with patch.dict(os.environ, {SESSION_KEY_FILE_ENV: str(key)}):
            self.snapshot()
            key.unlink()
            with self.assertRaises(SessionSecretError):
                self.snapshot()
            key.write_bytes(b'b' * 32)
            key.chmod(0o600)
            DataSourceSecretStore(self.home / 'database' / 'credentials').save('rotated', {'password': 'fixture-only'})
            handle = self.runtime._source_store()
            self.assertEqual(handle.secrets.load('rotated'), {'password': 'fixture-only'})

    def test_late_legacy_import_and_disabled_registration_still_win(self):
        runtime = IndependentDatabase(self.home / 'legacy')
        self.assertEqual(runtime.catalog('a')['sources'], [])
        root = runtime.home / 'analytics'
        root.mkdir(parents=True)
        (root / 'source.json').write_text(json.dumps(asdict(LegacySourceConfig(
            enabled=True, username='fixture', privileges_reviewed=True))))
        DataSourceSecretStore(root / 'credentials').save('taihua_primary:1', {'password': 'fixture-only'})
        runtime.grants.set('a', ['database.logs.query'])
        self.assertEqual(runtime.catalog('a')['sources'][0]['source_id'], 'taihua_primary')
        sources = Sources(runtime.home)
        sources.write('taihua_primary', 'disable', revision=1, actor='test', reason='fixture')
        self.assertEqual(runtime.catalog('a')['sources'], [])
        self.assertEqual(sources.get('taihua_primary')['revision'], 2)
        self.assertEqual(sources.get('taihua_primary')['state'], 'disabled')

    def test_changes_during_fetch_and_at_end_prevent_returning_rows(self):
        for change in ('revoke', 'disable', 'revision'):
            for empty_final in (False, True):
                with self.subTest(change=change, empty_final=empty_final), TemporaryDirectory() as home:
                    configured_source(home, 'equipment', pack='generic')
                    runtime = IndependentDatabase(home)
                    runtime.grants.set('a', ['database.free.read'], source_id='equipment')
                    control = Sources(home)
                    connection = MagicMock()
                    cursor = connection.cursor.return_value.__enter__.return_value
                    cursor.description = [SimpleNamespace(name='name')]
                    def fetch(_count):
                        if change == 'revoke':
                            runtime.grants.set('a', [], source_id='equipment')
                        elif change == 'disable':
                            control.write('equipment', 'disable', revision=1, actor='test', reason='fixture')
                        else:
                            config = {k: v for k, v in control.get('equipment')['active'].items() if k != 'credential_ref'}
                            control.write('equipment', 'save', revision=1, actor='test', reason='fixture', config={**config, 'name': 'changed draft'})
                        return [] if empty_final else [{'name': 'must not be returned'}]
                    cursor.fetchmany.side_effect = fetch
                    with patch('bscli.database.sources.connect') as connect, patch('bscli.database.sources.check_role'):
                        connect.return_value.__enter__.return_value = connection
                        with self.assertRaisesRegex(DatabaseRejected, 'DATABASE_AUTHORIZATION_CHANGED'):
                            runtime.execute('a', 'database.free.read', {'sql': 'SELECT name FROM public.devices'}, 'equipment')
                    cursor.fetchmany.assert_called_once()
