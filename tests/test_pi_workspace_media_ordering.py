"""Old/new Pi queues preserve the original order across an ownership handoff."""
from datetime import timedelta
import time

import pytest

from agentbridge.core import actionable_results as actions, workspace_media_delivery as media
from agentbridge.workspace.pi_timeline_media import delivery
from tests.test_pi_workspace_images import PNG, paired, pi, send  # noqa: F401
from tests.test_workspace_query_projection import register


@pytest.fixture
def group(pi, paired):
    register(pi.service, paired['subject'], 'pi', paired['instance'], paired['token_id'])
    send(pi, 'a', 'ordered-originals', [PNG + b'first', PNG + b'second'])
    scope = {k: paired[k] for k in ('subject', 'instance', 'endpoint_key', 'token_id')}
    scope['host'] = 'pi'
    return pi.service, paired, scope


def claim(group):
    service, _, scope = group
    return media.claim(service, scope['subject'], scope['host'], scope['instance'],
        scope['endpoint_key'], scope['token_id'], limit=4, lease=60)


def control(group, item, action, **extra):
    service, _, scope = group
    return media.handle(service, **scope, pairing_ref=item['pairingRef'],
        delivery_id=item['deliveryId'], attempt_count=item['attemptCount'], action=action, **extra)


def old(group, action, item=None, **extra):
    service, pair, _ = group
    lease = {} if item is None else {'delivery_id': item['deliveryId'], 'attempt': item['attempt']}
    return delivery(service, **pair, action=action, **lease, **extra)


def finish_new(group, item, receipt):
    assert control(group, item, 'issue')['available']
    assert control(group, item, 'begin')['allowed']
    assert control(group, item, 'ack', outcome='sent', receipt=receipt)['recorded']


def test_first_v2_claim_transfers_the_whole_unstarted_old_group(group):
    first, = claim(group)
    assert first['itemIndex'] == 1
    assert old(group, 'claim') == {'available': False}
    with group[0].tasks._connect() as db:
        assert [tuple(r) for r in db.execute('SELECT state,attempt FROM pi_workspace_image_deliveries ORDER BY ordinal')] == [
            ('superseded', 0), ('superseded', 0)]
    finish_new(group, first, 101)
    second, = claim(group)
    assert second['itemIndex'] == 2 and second['deliveryId'] != first['deliveryId']
    assert old(group, 'claim') == {'available': False}
    finish_new(group, second, 102)
    assert not claim(group) and not old(group, 'claim')['available']


@pytest.mark.parametrize('state', ['claimed', 'sending', 'sent', 'unknown'])
def test_any_started_old_item_retains_the_whole_old_group(group, state):
    first = old(group, 'claim')
    assert first['ordinal'] == 0
    if state != 'claimed':
        assert old(group, 'begin', first)['available']
    if state in {'sent', 'unknown'}:
        assert old(group, 'ack', first, receipt=101 if state == 'sent' else None)['recorded']
    assert not claim(group)
    if state == 'claimed':
        assert old(group, 'begin', first)['available']
    if state in {'claimed', 'sending'}:
        assert old(group, 'ack', first, receipt=101)['recorded']
    assert not claim(group)
    second = old(group, 'claim')
    assert second['ordinal'] == 1
    assert old(group, 'read', second)['available']
    assert old(group, 'begin', second)['available']
    assert old(group, 'ack', second, receipt=102)['recorded']
    assert not claim(group) and not old(group, 'claim')['available']
    with group[0].tasks._connect() as db:
        assert not db.execute('SELECT 1 FROM workspace_media_deliveries WHERE attempt>0').fetchone()


@pytest.mark.parametrize('started', [False, True])
def test_preexisting_half_handoff_blocks_old_second_claim_and_begin(group, started):
    first, = claim(group)
    # Reproduce a persisted state from the older per-item handoff, including a
    # legacy lease acquired before the fix but not its physical send permission.
    with group[0].tasks._connect() as db:
        db.execute('''UPDATE pi_workspace_image_deliveries SET state=?,attempt=?,lease_until=? WHERE ordinal=1''',
            ('claimed' if started else 'pending', int(started), time.time() + 60))
        row = db.execute('SELECT delivery_id FROM pi_workspace_image_deliveries WHERE ordinal=1').fetchone()
    second = {'deliveryId': row[0], 'attempt': 1}
    if started:
        assert not old(group, 'read', second)['available']
        assert not old(group, 'begin', second)['available']
    else:
        assert not old(group, 'claim')['available']
    finish_new(group, first, 101)
    if not started:
        second = old(group, 'claim')
    assert old(group, 'read', second)['available']
    assert old(group, 'begin', second)['available']
    assert old(group, 'ack', second, receipt=102)['recorded']
    assert not claim(group)


@pytest.mark.parametrize('leased', [False, True])
def test_preexisting_v2_second_waits_for_old_first_at_claim_and_begin(group, leased):
    first = old(group, 'claim')
    assert old(group, 'begin', first)['available']
    with group[0].tasks._connect() as db:
        db.execute("UPDATE workspace_media_deliveries SET state='legacy' WHERE ordinal=0")
        db.execute("UPDATE pi_workspace_image_deliveries SET state='superseded' WHERE ordinal=1")
        if leased:
            db.execute("UPDATE workspace_media_deliveries SET state='leased',attempt=1,issued=1,due=? WHERE ordinal=1",
                ((actions._now() + timedelta(seconds=60)).isoformat(),))
        row = db.execute('SELECT delivery_id FROM workspace_media_deliveries WHERE ordinal=1').fetchone()
    second = {'deliveryId': row[0], 'attemptCount': 1, 'pairingRef': group[1]['pairing_ref']}
    if leased:
        assert not control(group, second, 'issue')['available']
        assert not control(group, second, 'begin')['allowed']
    else:
        assert not claim(group)
    assert old(group, 'ack', first, receipt=101)['recorded']
    if not leased:
        second, = claim(group)
    finish_new(group, second, 102)
    assert not old(group, 'claim')['available'] and not claim(group)


@pytest.mark.parametrize('absence', ['table', 'rows'])
def test_old_only_storage_keeps_original_legacy_order(group, absence):
    with group[0].tasks._connect() as db:
        db.execute('DROP TABLE workspace_media_deliveries' if absence == 'table' else 'DELETE FROM workspace_media_deliveries')
    for ordinal in range(2):
        item = old(group, 'claim')
        assert item['ordinal'] == ordinal
        assert old(group, 'read', item)['available'] and old(group, 'begin', item)['available']
        assert old(group, 'ack', item, receipt=101 + ordinal)['recorded']
    assert not old(group, 'claim')['available']


def test_unknown_v2_predecessor_never_replays_and_releases_old_successor(group):
    first, = claim(group)
    with group[0].tasks._connect() as db:
        db.execute("UPDATE pi_workspace_image_deliveries SET state='pending' WHERE ordinal=1")
    assert control(group, first, 'issue')['available']
    assert control(group, first, 'begin')['allowed']
    assert not old(group, 'claim')['available']
    assert control(group, first, 'ack', outcome='delivery_unknown')['recorded']
    second = old(group, 'claim')
    assert second['ordinal'] == 1
    assert old(group, 'begin', second)['available']
    assert old(group, 'ack', second, receipt=102)['recorded']
    assert not claim(group)
    assert not control(group, first, 'begin')['allowed']
    assert control(group, first, 'ack', outcome='sent', receipt=101)['recorded']
    assert not claim(group)
