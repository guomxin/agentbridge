"""Process death, live lease exclusion and independently invoked cleanup."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agentbridge.database.independent import IndependentDatabase, DatabaseRejected
from agentbridge.database.reports import Reports
from tests.process_helpers import kill_fixture_process

ROOT=Path(__file__).resolve().parents[1]
ARGS={'query_capability':'database.free.read','query_arguments':{'sql':'SELECT 1'},'request_key':'interrupted'}


class ReportRecoveryTests(unittest.TestCase):
    def test_kill_at_each_boundary_converges_without_query_replay(self):
        for stage in ('running','query','before_file','after_file','ready'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                home=Path(tmp); key=home/'key'; key.write_bytes(os.urandom(32))
                key.chmod(0o600)
                env=dict(os.environ,PYTHONPATH=str(ROOT),AGENTBRIDGE_SESSION_KEY_FILE=str(key))
                process=subprocess.Popen([sys.executable,str(ROOT/'tests/fixtures/report_crash.py'),tmp,stage],env=env,
                                         stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+30
                    while not (home/'boundary').exists():
                        if process.poll() is not None:
                            self.fail(process.stderr.read().decode(errors='replace'))
                        if time.monotonic()>deadline: self.fail('report boundary timeout')
                        time.sleep(.02)
                    with patch.dict(os.environ,{'AGENTBRIDGE_SESSION_KEY_FILE':str(key)}):
                        reports=Reports(IndependentDatabase(home))
                        reports.cleanup()  # Independent worker cannot mark live exporter failed.
                        with closing(sqlite3.connect(reports.path)) as db:
                            rid,status=db.execute('SELECT id,status FROM reports').fetchone()
                        self.assertEqual(status,'ready' if stage=='ready' else 'running')
                        kill_fixture_process(process)
                        # Process termination can precede file-system cleanup
                        # has released its OS lease. Observe bounded convergence.
                        deadline = time.monotonic() + 5
                        while True:
                            reports.cleanup()
                            with closing(sqlite3.connect(reports.path)) as db:
                                status=db.execute('SELECT status FROM reports').fetchone()[0]
                            if status != 'running' or time.monotonic() >= deadline:
                                break
                            time.sleep(.02)
                        self.assertEqual(status,'ready' if stage=='ready' else 'failed')
                        self.assertEqual(reports.payloads.path(rid).exists(),stage=='ready')
                        with patch.object(reports.runtime,'_execute',side_effect=AssertionError('must not replay')):
                            if stage=='ready': self.assertEqual(reports.export('a','taihua_primary',ARGS)['report_id'],rid)
                            else:
                                with self.assertRaisesRegex(DatabaseRejected,'REPORT_FAILED'):
                                    reports.export('a','taihua_primary',ARGS)
                finally:
                    kill_fixture_process(process)
                    process.stderr.close()

    def test_orphan_grace_and_cleanup_failure_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=IndependentDatabase(tmp); reports=Reports(runtime)
            reports.payloads.root.mkdir(parents=True)
            old=reports.payloads.root/('a'*64+'.bin'); old.write_bytes(b'opaque')
            new=reports.payloads.root/('b'*64+'.bin'); new.write_bytes(b'opaque')
            unfinished=reports.payloads.root/('c'*32+'.tmp'); unfinished.write_bytes(b'opaque')
            for p in (old,unfinished): os.utime(p,(time.time()-7200,)*2)
            reports.cleanup()
            self.assertFalse(old.exists()); self.assertFalse(unfinished.exists()); self.assertTrue(new.exists())
            with closing(sqlite3.connect(reports.path)) as db,db:
                db.execute('INSERT INTO reports VALUES (?,?,?,?,?,?,?)',('d'*32,'a','s','key','fingerprint','running','2099-01-01'))
            with patch.object(reports.payloads,'delete',side_effect=OSError('fixture permission failure')):
                with self.assertRaises(OSError): reports.cleanup()
            with closing(sqlite3.connect(reports.path)) as db:
                self.assertEqual(db.execute('SELECT status FROM reports').fetchone()[0],'running')
            reports.cleanup()
            with closing(sqlite3.connect(reports.path)) as db:
                self.assertEqual(db.execute('SELECT status FROM reports').fetchone()[0],'failed')
