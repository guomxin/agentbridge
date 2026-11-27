"""Original Pi image receipts survive restart/unpairing without another send."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from starlette.exceptions import HTTPException

from agentbridge.workspace.pi_timeline_media import delivery
from agentbridge.workspace.telegram_pilot_links import TelegramPilotLinks
from tests.test_pi_workspace_images import PNG, paired, pi, send  # noqa: F401


@pytest.fixture
def receipt_host(pi, paired, tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1] / 'integrations'
    monkeypatch.syspath_prepend(str(root / 'nanobot-agentbridge'))
    monkeypatch.syspath_prepend(str(root / 'pi-agentbridge'))
    token = tmp_path / 'personal-token'
    token.write_text('synthetic-personal-credential-' + 'a' * 32)
    token.chmod(0o600)
    user = tmp_path / 'user'
    user.mkdir(mode=0o700)
    web = dict(user_subject='user-a', endpoint_id='workspace-a', endpoint_key='workspace:pi-office:a',
               session_key='workspace-session', credential_ref='web-a', tokenFile=str(token))
    record = dict(user_subject='user-a', endpoint_id='telegram-a', endpoint_key=paired['endpoint_key'],
                  session_key='telegram-session', credential_ref='tg-' + paired['pairing_ref'],
                  workspaceEndpointId=web['endpoint_id'], workspaceEndpointKey=web['endpoint_key'],
                  tokenFile=str(token))
    calls = []

    async def call_tool(name, args, **kwargs):
        assert name == 'agentbridge_host_telegram_workspace_media'
        calls.append(dict(args))
        value = delivery(pi.service, **{**paired, **args})
        return SimpleNamespace(isError=False, structuredContent=value)

    @asynccontextmanager
    async def borrow(original, *, lane):
        assert original is web and lane == 'workspace-image-receipt'
        yield SimpleNamespace(level=host.level, meta={}, session=SimpleNamespace(call_tool=call_tool))

    @asynccontextmanager
    async def checkout(original, *, lane):
        assert original is record and lane == 'workspace-media'

        async def check_identity():
            return SimpleNamespace(level=host.level, meta={}, session=SimpleNamespace(call_tool=call_tool))

        yield check_identity

    async def current(*args, **kwargs):
        assert host.pair_available, 'Historical receipt must not look up the current pairing'
        host.current_lookups += 1
        return record

    host = SimpleNamespace(instance='pi-office', bindings={'web': web}, level='L3',
        worker_payload=lambda route,*_: {'userDir':str(user), 'endpointKey':route['endpoint_key']},
        control_sessions=SimpleNamespace(borrow=borrow,checkout=checkout),
        telegram=SimpleNamespace(current=current), record=record, calls=calls, token=token,
        coordinator=None, commands=None, cards=SimpleNamespace(origins=[]), pair_available=True, current_lookups=0)
    return host


def frozen_body(host, claim, **extra):
    from pi_agentbridge.media_receipts import BINDING_FIELDS
    return dict(instance=host.instance, action='ack', binding={k:host.record[k] for k in BINDING_FIELDS},
        delivery_id=claim['deliveryId'], attempt=claim['attempt'],
        pairingRef=host.record['credential_ref'][3:], receipt=901, **extra)


def test_lost_image_ack_restarts_and_settles_after_unpair_without_bot_or_pair_lookup(
    pi, paired, receipt_host, tmp_path
):
    from nanobot_agentbridge.pi_workspace_images import poll, recover_receipts
    from pi_agentbridge.channel_delivery import dispatch
    from pi_agentbridge.server import Host

    host = receipt_host
    send(pi, 'a', 'image-ack-loss', [PNG + b'a', PNG + b'b'])
    sent = []
    binding = frozen_body(host, {'deliveryId':'unused', 'attempt':1})['binding']
    digest = hashlib.sha256(json.dumps([host.instance,8801,501,binding],
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    lost = True

    def request(action, body, **kwargs):
        nonlocal lost
        assert action == 'telegram-workspace-media'
        if body['action'] == 'ack':
            if lost:
                raise RuntimeError('Synthetic response loss before original central ack')
            return asyncio.run(Host.action(host, action, {'instance':host.instance, **body}))
        return asyncio.run(dispatch(host, host.record, action, {'instance':host.instance, **body}))

    def upload(chat, filename, mime, data):
        sent.append((chat, filename, mime, data))
        return {'message_id':901, 'from':{'id':8801}, 'chat':{'id':chat,'type':'private'},
                'document':{'file_id':'synthetic-image','file_size':len(data)}}

    def channel():
        db = sqlite3.connect(tmp_path / 'channel.sqlite')
        db.row_factory = sqlite3.Row
        return SimpleNamespace(instance=host.instance, bot_id=8801, db=db,
            gateway=SimpleNamespace(_request=request), api=SimpleNamespace(send_document=upload),
            _binding=lambda *_: (binding,digest))

    pilot = channel()
    with pytest.raises(RuntimeError, match='Synthetic response loss'):
        poll(pilot, 501, binding, digest)
    assert len(sent) == 1
    assert pilot.db.execute('SELECT receipt,acknowledged FROM pi_workspace_image_receipts').fetchone()[:] == (901,0)
    pilot.db.close()
    TelegramPilotLinks(pi.service).revoke(pi.accounts['a']['account_id'], 'pi-office', '8801')
    assert host.current_lookups == 3
    host.pair_available = False
    restarted = channel()
    restarted.api = restarted._binding = None  # Recovery has no access to a sender or current binding.
    with restarted.db:
        restarted.db.execute('UPDATE pi_workspace_image_receipts SET next_ack=0')
    lost = False
    recover_receipts(restarted)
    recover_receipts(restarted)
    assert len(sent) == 1 and [call['action'] for call in host.calls] == ['claim','read','begin','ack']
    assert host.current_lookups == 3
    assert restarted.db.execute('SELECT acknowledged FROM pi_workspace_image_receipts').fetchone()[0] == 1
    with pi.service.workspace._connect() as db:
        assert [tuple(r) for r in db.execute('SELECT state,receipt,attempt FROM pi_workspace_image_deliveries ORDER BY ordinal')] == [
            ('sent',901,1), ('pending',None,0)]
    restarted.db.close()


def test_private_receipt_route_rejects_wrong_frozen_identity_and_only_settles_original_attempt(
    pi, paired, receipt_host
):
    from pi_agentbridge.media_receipts import record_claim
    from pi_agentbridge.server import Host

    host = receipt_host
    send(pi, 'a', 'private-image-ack', [PNG + b'a'])
    claim = delivery(pi.service, **paired, action='claim')
    record_claim(host, host.record, claim)
    target = dict(delivery_id=claim['deliveryId'], attempt=claim['attempt'])
    assert delivery(pi.service, **paired, action='begin', **target)['available']
    TelegramPilotLinks(pi.service).revoke(pi.accounts['a']['account_id'], 'pi-office', '8801')
    body = frozen_body(host, claim)
    for replacement in (
        {'instance':'wrong'}, {'attempt':claim['attempt']+1}, {'pairingRef':'0'*64},
        {'delivery_id':'00000000-0000-0000-0000-000000000000'}, {'receipt':True},
        {'binding':{**body['binding'],'user_subject':'user-b'}},
        {'binding':{**body['binding'],'endpoint_key':'telegram:8801:502'}},
        {'binding':{**body['binding'],'credential_ref':'tg-'+'0'*64}},
        {'tokenFile':str(host.token)}, {'action':'read'},
    ):
        from pi_agentbridge.media_receipts import acknowledge
        with pytest.raises(HTTPException) as error:
            asyncio.run(acknowledge(host, {**body, **replacement}))
        assert error.value.status_code == 403
    assert host.calls == []
    host.level = 'L2'
    with pytest.raises(HTTPException):
        asyncio.run(Host.action(host, 'telegram-workspace-media', body))
    host.level = 'L3'
    original = host.token.read_text()
    host.token.write_text('rotated-personal-credential-' + 'b'*32)
    with pytest.raises(HTTPException):
        asyncio.run(Host.action(host, 'telegram-workspace-media', body))
    assert host.calls == []
    host.token.write_text(original)
    assert asyncio.run(Host.action(host, 'telegram-workspace-media', {**body,'receipt':None})) == {'recorded':True}
    assert asyncio.run(Host.action(host, 'telegram-workspace-media', body)) == {'recorded':True}
    assert asyncio.run(Host.action(host, 'telegram-workspace-media', body)) == {'recorded':True}
    assert asyncio.run(Host.action(host, 'telegram-workspace-media', {**body,'receipt':902})) == {'recorded':False}
    assert all(call['action'] == 'ack' for call in host.calls)


def test_claim_pin_cannot_be_replaced_by_another_route_or_old_attempt(pi, paired, receipt_host):
    from pi_agentbridge.media_receipts import record_claim

    host = receipt_host
    send(pi, 'a', 'image-claim-pin', [PNG + b'a'])
    claim = delivery(pi.service, **paired, action='claim')
    record_claim(host, host.record, claim)
    record_claim(host, host.record, claim)
    for record, item in (({**host.record,'endpoint_key':'telegram:8801:502'},claim),
                         (host.record,{**claim,'ordinal':1}), (host.record,{**claim,'attempt':0})):
        with pytest.raises(HTTPException):
            record_claim(host, record, item)
    record_claim(host, host.record, {**claim,'attempt':2})
    with pytest.raises(HTTPException):
        record_claim(host, host.record, claim)
