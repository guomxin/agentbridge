"""Durable fragment recovery never replays a known or uncertain platform send."""
from datetime import timedelta
from unittest.mock import patch

import pytest

from agentbridge.core import actionable_results as actions
from agentbridge.core import workspace_text_delivery as text_delivery
from agentbridge.core.tasks import TaskHubStore
from tests.support.latency_acceptance import OWNER
from tests.test_workspace_query_projection import openclaw  # noqa: F401
from tests.test_workspace_text_delivery import admit, control, text_claim


def send(service, endpoint, token, item, receipt):
    text = control(service, endpoint, token, item, 'issue')['publicText'].split('\n', 1)[1]
    assert control(service, endpoint, token, item, 'begin')['allowed']
    assert control(service, endpoint, token, item, 'ack', outcome='sent', receipt=receipt)['recorded']
    return text


def answer(fixture, endpoint, token, text):
    dispatch = admit(fixture)
    service = fixture.service
    question, = text_claim(service, endpoint, token)
    send(service, endpoint, token, question, 1)
    service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
        message_key='workspace:assistant:' + dispatch['idempotency_key'], text=text)
    return dispatch


def test_middle_unknown_survives_restart_then_late_receipt_releases_only_unsent_parts(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    original = '中文😀<&>*\n' * 1_400 + '最后一片'
    answer(fixture, endpoint, token, original)
    first, = text_claim(service, endpoint, token)
    seen = [send(service, endpoint, token, first, 2)]
    second, = text_claim(service, endpoint, token)
    seen.append(control(service, endpoint, token, second, 'issue')['publicText'].split('\n', 1)[1])
    assert control(service, endpoint, token, second, 'begin')['allowed']
    assert control(service, endpoint, token, second, 'ack', outcome='delivery_unknown')['recorded']
    with service.tasks._connect() as db:
        before = [(r['delivery_id'], r['part_start'], r['part_end'], r['part_hash'])
                  for r in db.execute('SELECT * FROM workspace_text_deliveries ORDER BY rowid')]
        assert db.execute("SELECT count(*) FROM workspace_text_deliveries WHERE state='blocked'").fetchone()[0] > 0
        db.execute('UPDATE workspace_text_deliveries SET due=?', ((actions._now()-timedelta(hours=1)).isoformat(),))
    service.tasks = TaskHubStore(service.db_path, maintain_on_startup=False)
    with patch.object(text_delivery, 'split_text', side_effect=AssertionError('recovery must not repartition')):
        assert not text_claim(service, endpoint, token)
        assert not control(service, endpoint, token, second, 'begin')['allowed']
        assert not control(service, endpoint, token, {**second, 'attemptCount': 2}, 'ack', outcome='sent', receipt=3)['recorded']
        assert control(service, endpoint, token, second, 'ack', outcome='sent', receipt=3)['recorded']
        assert control(service, endpoint, token, second, 'ack', outcome='sent', receipt=3)['recorded']
        assert not control(service, endpoint, token, second, 'ack', outcome='sent', receipt=4)['recorded']
        assert not control(service, endpoint, token, second, 'ack', outcome='delivery_unknown')['recorded']
        while items := text_claim(service, endpoint, token):
            item, = items
            assert item['partIndex'] == len(seen) + 1
            assert item['attemptCount'] == 1
            seen.append(send(service, endpoint, token, item, len(seen) + 2))
    assert ''.join(seen) == original
    with service.tasks._connect() as db:
        after = [(r['delivery_id'], r['part_start'], r['part_end'], r['part_hash'])
                 for r in db.execute('SELECT * FROM workspace_text_deliveries ORDER BY rowid')]
        assert before == after
        assert not db.execute("SELECT 1 FROM workspace_text_deliveries WHERE state!='sent' OR attempt!=1").fetchone()
    assert fixture.audit()['downstreamWriteCount'] == 0


@pytest.mark.parametrize('change', ['revoke_pair', 'source_deleted', 'source_changed'])
def test_late_middle_receipt_after_revocation_or_source_cleanup_cannot_release_content(openclaw, change):
    fixture, token, endpoint = openclaw
    service = fixture.service
    answer(fixture, endpoint, token, '完整公开答复😀' * 2_000)
    item, = text_claim(service, endpoint, token)
    assert control(service, endpoint, token, item, 'issue')['available']
    assert control(service, endpoint, token, item, 'begin')['allowed']
    assert control(service, endpoint, token, item, 'ack', outcome='delivery_unknown')['recorded']
    with service.tasks._connect() as db:
        if change == 'revoke_pair':
            db.execute("UPDATE client_endpoints SET state='revoked' WHERE endpoint_id=?", (endpoint['endpoint_id'],))
        elif change == 'source_deleted':
            db.execute('DELETE FROM user_timeline WHERE entry_id=?', (item['sourceEntryId'],))
        else:
            db.execute("UPDATE user_timeline SET text='变更后的文字' WHERE entry_id=?", (item['sourceEntryId'],))
    # A private, exact-attempt receipt settles only the historic send. It cannot
    # bypass the separate live checks before any later part is issued.
    assert control(service, endpoint, token, item, 'ack', outcome='sent', receipt=9)['recorded']
    assert not control(service, endpoint, token, item, 'issue')['available']
    assert not text_claim(service, endpoint, token)
    with service.tasks._connect() as db:
        assert db.execute("SELECT count(*) FROM workspace_text_deliveries WHERE state='sent'").fetchone()[0] == 2
        assert not db.execute("SELECT 1 FROM workspace_text_deliveries WHERE part_index>1 AND attempt>0").fetchone()


def test_workspace_total_cap_is_atomic_and_does_not_create_a_partial_mirror(openclaw):
    fixture, token, endpoint = openclaw
    service = fixture.service
    dispatch = answer(fixture, endpoint, token, '😀' * 200_000)
    with service.tasks._connect() as db:
        assert db.execute("SELECT SUM(part_end-part_start) FROM workspace_text_deliveries WHERE kind='assistant'").fetchone()[0] == 200_000
        before = db.execute('SELECT COUNT(*) FROM workspace_text_deliveries').fetchone()[0]
    with pytest.raises(ValueError):
        service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=dispatch['origin_endpoint_id'],
            message_key='workspace:assistant:overflow', text='😀' * 200_001)
    with service.tasks._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM workspace_text_deliveries').fetchone()[0] == before
        assert not db.execute("SELECT 1 FROM user_timeline WHERE dedupe_key='message:workspace:assistant:overflow'").fetchone()
