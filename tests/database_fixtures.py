"""Isolated configured sources; never contacts a business database."""
import json
import sqlite3
from contextlib import closing
from agentbridge.database.sources import Sources,TAIHUA_RELATIONS

def configured_source(home,sid='taihua_primary', *, pack='taihua_logs'):
    sources=Sources(home)
    config={'source_id':sid,'name':sid,'description':'fixture','engine':'postgresql','host':'localhost','port':5432,
        'dbname':'fixture','username':'readonly','sslmode':'verify-full','allowed_relations':TAIHUA_RELATIONS if pack=='taihua_logs' else ['public.devices'],
        'template_pack':pack,'statement_timeout_ms':10000,'export_rows':10000,'export_bytes':10485760,'credential_ref':'fixture'}
    record={'source_id':sid,'revision':1,'state':'enabled','active':config,'draft':config,'draft_revision':1,'preflight':{'status':'passed','revision':1}}
    with closing(sqlite3.connect(sources.path)) as c,c:
        c.execute('INSERT INTO sources VALUES (?,?)',(sid,json.dumps(record)))
    return record
