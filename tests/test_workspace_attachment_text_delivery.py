"""A resource's visibility and lifecycle cannot swallow its ordinary public text."""
import base64
import json
from uuid import uuid4

import pytest

from agentbridge.core import workspace_text_delivery
from agentbridge.core import workspace_query_delivery
from agentbridge.core.endpoint_instances import endpoint_instances
from agentbridge.workspace.telegram_pilot_links import mirror_binding
from tests.support.latency_acceptance import OWNER
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_workspace_chat_mirror import mirror, pair_fixture  # noqa: F401
from tests.test_pi_workspace_images import pi, paired  # noqa: F401
from tests.test_workspace_query_projection import openclaw, register  # noqa: F401
from tests.test_workspace_text_delivery import control, text_claim


def admit(fixture):
    service = fixture.service
    dispatch, _ = service.workspace.accept_chat_request(account_id=fixture.account['account_id'], user_subject=OWNER,
        message='请比较这两张图片。', idempotency_key=uuid4().hex,
        attachments=[{'fileName': f'{i}.png', 'mimeType': 'image/png',
            'content': base64.b64encode(b'\x89PNG\r\n\x1a\n' + bytes([i])).decode()} for i in range(2)],
        tasks=service.tasks, attachment_store=service.timeline_attachments, media_base_url='https://fixture.invalid')
    claimed = service.workspace.claim_next_host_dispatch(account_id=fixture.account['account_id'], claim_owner='fixture')
    service.workspace.mark_host_dispatch_accepted(dispatch['dispatch_id'], claim_token=claimed['claim_token'],
        run_id=dispatch['idempotency_key'])
    return dispatch, claimed['claim_token']


def collect(service, endpoint, token):
    contents = []
    while items := text_claim(service, endpoint, token):
        item, = items
        contents.append(control(service, endpoint, token, item, 'issue')['publicText'])
        assert control(service, endpoint, token, item, 'begin')['allowed']
        assert control(service, endpoint, token, item, 'ack', outcome='sent', receipt=len(contents))['recorded']
    return contents


@pytest.mark.parametrize('expired', [False, True])
def test_attachment_question_and_final_answer_deliver_without_forwarding_resource_urls(openclaw, expired):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch, _ = admit(fixture)
    if expired:
        with service.tasks._connect() as db:
            db.execute("UPDATE timeline_attachments SET expires_at='2000-01-01T00:00:00Z'")
    service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:' + dispatch['idempotency_key'], text='两张图展示了不同的合成内容。')
    assert collect(service, endpoint, token) == ['工作台提问\n请比较这两张图片。', '工作台回复\n两张图展示了不同的合成内容。']
    with service.tasks._connect() as db:
        assert db.execute("SELECT selection FROM workspace_query_destinations").fetchone()[0] == 'paired'
        assert not db.execute("SELECT 1 FROM notification_outbox WHERE state='pending' AND payload_type='timeline_message'").fetchone()
        assert db.execute('SELECT count(*) FROM timeline_attachments').fetchone()[0] == 2
        assert not db.execute('SELECT 1 FROM operations').fetchone()


@pytest.mark.parametrize('state', ['failed', 'stopped', 'result_unknown'])
def test_attachment_request_public_terminal_keeps_original_question_pin(openclaw, state):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch, claim_token = admit(fixture)
    service.workspace.finish_host_dispatch(dispatch['dispatch_id'], claim_token=claim_token,
        state=state, event={'state': 'error', 'text': 'private material must not leak'})
    assert collect(service, endpoint, token) == [
        '工作台提问\n请比较这两张图片。', '工作台状态\n' + workspace_text_delivery.STATUS[state]]


def test_attachment_admission_without_pair_never_retargets_after_pairing(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    with service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoint['endpoint_id'],))
    dispatch, _ = admit(fixture)
    with service.tasks._connect() as db:
        db.execute("UPDATE client_endpoints SET state='active' WHERE endpoint_id=?", (endpoint['endpoint_id'],))
    service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:' + dispatch['idempotency_key'], text='旧图片问题的公开回答。')
    assert not text_claim(service, endpoint, token)
    with service.tasks._connect() as db:
        assert db.execute('SELECT selection FROM workspace_query_destinations').fetchone()[0] == 'unpaired'


@pytest.mark.parametrize('host', ['nanobot','pi'])
def test_python_host_image_caption_and_answer_use_original_independent_text_pin(request, host):
    if host == 'pi':
        fixture = request.getfixturevalue('pi')
        pair = request.getfixturevalue('paired')
        service, subject, account, instance = fixture.service, 'user-a', fixture.accounts['a']['account_id'], 'pi-office'
        endpoint = pair['endpoint_key']
    else:
        service, _, accounts, subject, _, _, _ = request.getfixturevalue('mirror')
        account, instance, endpoint = accounts[subject], INSTANCE, 'telegram:8001:501'
    # The old Pi fixture's FakeGateway only minted a placeholder web credential.
    # Register the same real synthetic personal identity that its private route uses.
    with service.tasks._connect() as db:
        web = mirror_binding(db, subject, instance, endpoint_instances(db, subject))
        endpoint_row = dict(db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?', (web['endpoint_id'],)).fetchone())
        token = web['token_id']
    if host == 'pi':
        token = pair['token_id']
        service.tasks.ensure_endpoint(user_subject=subject, token_id=token, agent_host=host,
            **{k:endpoint_row[k] for k in ('endpoint_key','client_type','external_subject','conversation_ref','account_id')},
            route=json.loads(endpoint_row['route_json']), capabilities=json.loads(endpoint_row['capabilities_json']))
    register(service, subject, host, instance, token)
    dispatch, _ = service.workspace.accept_chat_request(account_id=account, user_subject=subject,
        message='说明这张图。', idempotency_key=uuid4().hex,
        attachments=[{'fileName':'one.png','mimeType':'image/png',
                      'content':base64.b64encode(b'\x89PNG\r\n\x1a\na').decode()}],
        tasks=service.tasks, attachment_store=service.timeline_attachments,
        media_base_url='https://fixture.invalid', instance_id=instance)
    with service.tasks._connect() as db:
        pins = [tuple(row) for row in db.execute('SELECT host,instance,selection FROM workspace_query_destinations')]
        assert pins == [(host,instance,'paired')]
    claim = service.workspace.claim_next_host_dispatch(account_id=account, claim_owner='fixture')
    service.workspace.mark_host_dispatch_accepted(dispatch['dispatch_id'], claim_token=claim['claim_token'],
                                                run_id=dispatch['idempotency_key'])
    with service.tasks._connect() as db:
        db.execute("UPDATE timeline_attachments SET expires_at='2000-01-01T00:00:00Z'")
    service.tasks.append_workspace_response(user_subject=subject, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:'+dispatch['idempotency_key'], text='公开的图像说明。')
    contents = []
    while items := workspace_text_delivery.claim(service, subject, host, instance, endpoint, token, 10, 30):
        item, = items
        scope = dict(subject=subject,host=host,instance=instance,endpoint_key=endpoint,token_id=token,
            pairing_ref=item['pairingRef'],delivery_id=item['deliveryId'],attempt_count=item['attemptCount'])
        contents.append(workspace_query_delivery.handle(service, **scope, action='issue')['publicText'])
        assert workspace_query_delivery.handle(service, **scope, action='begin')['allowed']
        assert workspace_query_delivery.handle(service, **scope, action='ack',outcome='sent',receipt=len(contents))['recorded']
    assert contents == ['工作台提问\n说明这张图。','工作台回复\n公开的图像说明。']
