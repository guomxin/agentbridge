"""Public upload keeps the Web identity separate from the registered private Bot."""
import base64
from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace
import threading
from unittest.mock import patch
from uuid import uuid4

import pytest

from agentbridge.core import workspace_media_delivery as media, workspace_query_delivery as query
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.support.nanobot_telegram_setup import INSTANCE
from tests.test_nanobot_workspace_chat_mirror import mirror, pair_fixture  # noqa: F401
from tests.test_pi_workspace_images import pi, paired  # noqa: F401
from tests.test_workspace import FakeGateway, PASSWORD, _cookies, _free_port, _raw_request, _request
from tests.test_workspace_query_projection import register

PNG = b'\x89PNG\r\n\x1a\noriginal-public-upload'
CAPTION = '原件说明与公开答复分别送达。'


@pytest.fixture(params=['nanobot', 'pi'])
def upload(request):
    host = request.param
    if host == 'pi':
        f = request.getfixturevalue('pi')
        pair = request.getfixturevalue('paired')
        service, app, instance = f.service, f.app, 'pi-office'
        accounts = f.accounts
        token, endpoint_key = pair['token_id'], pair['endpoint_key']
        close = False
    else:
        service, links, account_ids, subject, other, _, deploy = request.getfixturevalue('mirror')
        with service.workspace._connect() as db:
            accounts = {label: dict(db.execute('SELECT * FROM workspace_accounts WHERE account_id=?',
                (account_ids[owner],)).fetchone()) for label, owner in [('a', subject), ('b', other)]}
        fixture = request.getfixturevalue('pair_fixture')[0]
        token, instance = fixture.tokens['a'], INSTANCE
        endpoint_key = links.ensure_endpoint(subject, token, instance, '8001', '501')['endpointKey']
        app = WorkspaceApplication(service=service, gateway=FakeGateway())
        close = True
    account = accounts['a']
    owner = account['user_subject']
    source = service.workspace.current_host(account['account_id'])['endpoint_id']
    # The public Web binding is not a host MCP credential. The Pi provisioning
    # fixture already has this separation; Nano's old peer fixture did not.
    if host == 'nanobot':
        with service.tasks._connect() as db:
            row = dict(db.execute('SELECT * FROM client_endpoints WHERE endpoint_id=?', (source,)).fetchone())
        service.tasks.ensure_endpoint(user_subject=owner, token_id='workspace:' + account['account_id'],
            agent_host=host, **{k: row[k] for k in ('endpoint_key', 'client_type', 'external_subject',
                'conversation_ref', 'account_id')}, route=json.loads(row['route_json']),
            capabilities=json.loads(row['capabilities_json']))
    register(service, owner, host, instance, token)
    endpoint = service.tasks.endpoint_for_key(user_subject=owner, agent_host=host, endpoint_key=endpoint_key)
    with service.tasks._connect() as db:
        web_token = db.execute('SELECT token_id FROM client_endpoints WHERE endpoint_id=?', (source,)).fetchone()[0]
        assert web_token != token
        assert not query.registered(db, owner, host, (host, instance), web_token)
        assert query.registered(db, owner, host, (host, instance), token)
    try:
        yield SimpleNamespace(service=service, app=app, accounts=accounts, owner=owner, host=host,
            instance=instance, token=token, source=source, endpoint=endpoint)
    finally:
        if close:
            app.close()


@contextmanager
def browser(upload):
    port = _free_port()
    origin = f'http://127.0.0.1:{port}'
    server = create_workspace_http_server(config=validate_workspace_server_config(host='127.0.0.1',
        port=port, public_base_url=origin, tls_cert=None, tls_key=None), application=upload.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only suppress host execution and the wait stream; public authentication,
    # request validation, atomic admission, source/target selection all run.
    with patch.object(upload.app, '_ensure_dispatch_worker'), patch.object(upload.app, '_stream_host_dispatch',
            return_value=iter(())):
        try:
            def send(label='a', key=None):
                account = upload.accounts[label]
                status, headers, _ = _request(port, 'POST', '/api/login', origin=origin,
                    body={'username': account['username'], 'password': PASSWORD})
                assert status == 200
                cookies = _cookies(headers)
                body = {'message': CAPTION, 'idempotencyKey': key or uuid4().hex,
                    'instanceId': upload.instance,
                    'expectedRevision': upload.app.hosts.choices(account['account_id'])['revision'],
                    'attachments': [{'fileName': 'original.png', 'mimeType': 'image/png',
                        'content': base64.b64encode(PNG).decode()}]}
                status, _, _ = _raw_request(port, 'POST', '/api/chat/send-stream', origin=origin,
                    cookies=cookies, csrf=cookies['agentbridge_workspace_csrf'], body=body)
                return status
            yield send
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def scope(f):
    return dict(subject=f.owner, host=f.host, instance=f.instance,
        token_id=f.token, endpoint_key=f.endpoint['endpoint_key'])


def claim(f):
    return f.service.claim_host_notifications(user_subject=f.owner, agent_host=f.host,
        host_instance_id=f.instance, endpoint_key=f.endpoint['endpoint_key'],
        include_workspace_chat=True, media_token_id=f.token, query_token_id=f.token)['notifications']


def control(f, item, action, **extra):
    handler = media.handle if item['deliveryMode'] == 'workspace_media_v2' else query.handle
    return handler(f.service, **scope(f), pairing_ref=item['pairingRef'], delivery_id=item['deliveryId'],
        attempt_count=item['attemptCount'], action=action, **extra)


def answer(f):
    dispatch = f.service.workspace.claim_next_host_dispatch(account_id=f.accounts['a']['account_id'], claim_owner='test')
    f.service.workspace.mark_host_dispatch_accepted(dispatch['dispatch_id'], claim_token=dispatch['claim_token'],
        run_id='public-image-answer')
    f.service.tasks.append_workspace_response(user_subject=f.owner, source_endpoint_id=f.source,
        message_key='workspace:assistant:public-image-answer', text='已收到原图。')


def test_public_upload_freezes_registered_target_and_each_original_receipt(upload):
    f = upload
    with browser(f) as send:
        assert send(key='same-original') == send(key='same-original') == 200
        assert send('b') == 200
    answer(f)
    seen = []
    while items := claim(f):
        for item in items:
            issued = control(f, item, 'issue')
            assert issued['available']
            if item['deliveryMode'] == 'workspace_media_v2':
                assert base64.b64decode(issued['resource']['contentBase64']) == PNG
                assert issued['resource']['sha256'] == hashlib.sha256(PNG).hexdigest()
            else:
                assert issued['publicText'] in ('工作台提问\n' + CAPTION, '工作台回复\n已收到原图。')
            assert control(f, item, 'begin')['allowed']
            assert not control(f, item, 'begin')['allowed']
            assert control(f, item, 'ack', outcome='sent', receipt=100 + len(seen))['recorded']
            seen.append(item['deliveryMode'])
    assert sorted(seen) == ['workspace_media_v2', 'workspace_text_v2', 'workspace_text_v2']
    with f.service.tasks._connect() as db:
        pins = db.execute('SELECT * FROM workspace_query_destinations').fetchall()
        own, = [p for p in pins if p['subject'] == f.owner]
        assert (own['selection'], own['source'], own['endpoint_id'], own['token_id']) == (
            'paired', f.source, f.endpoint['endpoint_id'], f.token)
        assert all(p['selection'] == 'unpaired' for p in pins if p['subject'] != f.owner)
        assert not db.execute('SELECT 1 FROM operations').fetchone()


@pytest.mark.parametrize('invalid', ['unregistered_token', 'foreign_token', 'revoked_target'])
def test_media_never_treats_web_identity_as_target_authorization(upload, invalid):
    f = upload
    with f.service.tasks._connect() as db:
        if invalid == 'revoked_target':
            db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (f.endpoint['endpoint_id'],))
        else:
            owner = f.accounts['b']['user_subject'] if invalid == 'foreign_token' else f.owner
    if invalid != 'revoked_target':
        token = McpIdentityTokenStore(f.service.db_path).issue(user_subject=owner, expected_principal_ref=owner,
            label='unregistered-media-token')['token_id']
        with f.service.tasks._connect() as db:
            db.execute('UPDATE client_endpoints SET token_id=? WHERE endpoint_id=?', (token, f.endpoint['endpoint_id']))
    with browser(f) as send:
        assert send() == 200
    with f.service.tasks._connect() as db:
        assert db.execute('SELECT selection FROM workspace_query_destinations').fetchone()[0] == 'unpaired'
        assert not db.execute('SELECT 1 FROM workspace_media_deliveries').fetchone()
        assert not db.execute('SELECT 1 FROM workspace_text_deliveries').fetchone()


def test_original_source_binding_rechecked_before_send(upload):
    f = upload
    with browser(f) as send:
        assert send() == 200
    items = claim(f)
    assert {i['deliveryMode'] for i in items} == {'workspace_text_v2', 'workspace_media_v2'}
    for item in items:
        assert control(f, item, 'issue')['available']
    with f.service.tasks._connect() as db:
        db.execute("UPDATE workspace_host_bindings SET credential_ref='changed-source-binding' WHERE endpoint_id=?", (f.source,))
    for item in items:
        assert not control(f, item, 'begin')['allowed']


def test_old_unpaired_upload_never_becomes_new_delivery(upload):
    f = upload
    with f.service.tasks._connect() as db:
        db.execute('DELETE FROM agent_host_registrations WHERE user_subject=? AND agent_host=?', (f.owner, f.host))
    with browser(f) as send:
        assert send(key='before-registration') == 200
        register(f.service, f.owner, f.host, f.instance, f.token)
        assert send(key='before-registration') == 200
    answer(f)
    assert not claim(f)
    with f.service.tasks._connect() as db:
        assert not db.execute('SELECT 1 FROM workspace_media_deliveries').fetchone()
        assert not db.execute('SELECT 1 FROM workspace_text_deliveries').fetchone()
