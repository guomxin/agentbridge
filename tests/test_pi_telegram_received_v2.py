"""Exercise Pi's real Delivery wrapper, not a fake host that already exposes a DB."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

from tests.test_nanobot_telegram_text_v2 import timeline_host  # noqa: F401


def test_pi_delivery_persists_received_fact_without_model_admission(timeline_host, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'integrations/pi-agentbridge'))
    from pi_agentbridge.channel_delivery import dispatch
    original, record, state = timeline_host
    state.update(entry=None,host_type='pi')
    host = SimpleNamespace(instance=original.instance,coordinator=None,commands=None,cards=SimpleNamespace(origins=[]),
        ledger=SimpleNamespace(db=original.runs._db,lookup=lambda *_:None),worker_payload=original.worker_payload)
    body = {'messageId':9,'role':'user','text':'合成问题','runId':None,'deliveredMessageIds':[],
            'projectionMode':'telegram_received_v2'}
    state['verified'] = False
    with pytest.raises(HTTPException) as error:
        asyncio.run(dispatch(host,record,'telegram-timeline',body))
    assert error.value.status_code == 403 and not state['calls']
    state['verified'] = True
    for _ in range(2):
        assert asyncio.run(dispatch(host,record,'telegram-timeline',body)) == {'recorded':True}
    assert host.ledger.db.execute('SELECT count(*) FROM telegram_received_sources').fetchone()[0] == 1
    for change in ({'text':'篡改同消息正文'},):
        with pytest.raises(HTTPException) as error:
            asyncio.run(dispatch(host,record,'telegram-timeline',{**body,**change}))
        assert error.value.status_code == 403
    assert len(state['calls']) == 2 and all(call[1]['projection_v2'] for call in state['calls'])
