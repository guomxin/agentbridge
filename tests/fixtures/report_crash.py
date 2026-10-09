"""Real report exporter killed at durable boundaries; synthetic read-only query."""
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

from agentbridge.database.independent import IndependentDatabase
from agentbridge.database.reports import Reports
import agentbridge.database.reports as report_module
from tests.database_fixtures import configured_source

home, stage = sys.argv[1:]
configured_source(home)
runtime=IndependentDatabase(home)
runtime.grants.set('a',['database.free.read','database.report.export'])
reports=Reports(runtime)

def stop():
    Path(home,'boundary').write_text(stage)
    # Parent kills this process, proving OS lease release rather than cleanup in finally.
    import time
    while True: time.sleep(1)

def execute(*args, **kwargs):
    if stage=='query': stop()
    return {'columns':['name'],'rows':[{'name':'=fixture'}],
            'queried_at':datetime.now(timezone.utc).isoformat(),'sql_sha256':'fixture'}

runtime._execute=execute
if stage=='running':
    class BeforeQueryBudget:
        def acquire(self, **kwargs): stop()
    report_module._SLOTS=BeforeQueryBudget()
save=reports.payloads.save
def save_then_stop(*args):
    if stage=='before_file': stop()
    save(*args)
    if stage=='after_file': stop()
reports.payloads.save=save_then_stop
reports.export('a','taihua_primary',{'query_capability':'database.free.read',
               'query_arguments':{'sql':'SELECT 1'},'request_key':'interrupted'})
if stage=='ready': stop()
