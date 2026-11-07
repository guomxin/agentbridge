"""Live keyset lists retain active work while keeping historical records reachable."""
import base64
from contextlib import closing
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentbridge.core.central_service import CentralCapabilityService


class SkillPaginationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.a = CentralCapabilityService(home=self.tmp.name, base_url='http://oa.test').skill_authoring
        self.proposal = {'name': '合成方法', 'description': '归纳输入',
            'selection': {'use_when': '归纳', 'not_for': '业务写入', 'output': '摘要'},
            'instructions': '只使用给定文字，缺失内容标为待核对。'}

    def draft(self, owner='alice'):
        self.serial = getattr(self, 'serial', 0) + 1
        return self.a.save(owner, proposal=self.proposal, request_key=str(self.serial))

    def review(self, draft, owner='alice'):
        sample = self.a.test_start(owner, draft_id=draft['draft_id'], expected_revision=1,
            profile='use', prompt='合成材料', request_key='test-' + draft['draft_id'])
        self.a.test_result(owner, test_id=sample['test_id'], output='合成摘要')
        return self.a.submit(owner, draft_id=draft['draft_id'], expected_revision=1,
            request_key='submit-' + draft['draft_id'], reason='合成申请')

    def pages(self, call, *, key, **kwargs):
        found = []; cursor = None
        while True:
            page = call(cursor=cursor, **kwargs)
            found.extend(row[key] for row in page['items'])
            self.assertEqual(page['has_more'], page['next_cursor'] is not None)
            if not page['has_more']:
                return found
            cursor = page['next_cursor']

    def test_active_draft_survives_more_than_100_archived_and_history_has_pages(self):
        active = self.draft()
        archived = []
        with patch('agentbridge.core.skill_authoring.now', return_value='2026-11-07T10:00:00+00:00'):
            for _ in range(105):
                draft = self.draft(); archived.append(draft['draft_id'])
                self.a.archive('alice', draft_id=draft['draft_id'], expected_revision=1)
        other = self.draft('bob')
        self.assertEqual(self.a.list('alice')['items'][0]['draft_id'], active['draft_id'])
        self.assertEqual([r['draft_id'] for r in self.a.list_page('alice')['items']], [active['draft_id']])
        ids = self.pages(lambda **kw: self.a.list_page('alice', **kw), key='draft_id', state='archived', limit=17)
        self.assertEqual(ids, sorted(archived, reverse=True))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(other['draft_id'], ids)
        self.assertEqual(self.a.list_page('nobody')['items'], [])

    def test_live_insert_before_boundary_does_not_duplicate_existing_rows(self):
        with patch('agentbridge.core.skill_authoring.now', return_value='2026-11-07T10:00:00+00:00'):
            original = [self.draft()['draft_id'] for _ in range(5)]
        first = self.a.list_page('alice', limit=2)
        with patch('agentbridge.core.skill_authoring.now', return_value='2026-11-07T11:00:00+00:00'):
            added = self.draft()['draft_id']
        rest = []
        cursor = first['next_cursor']
        while cursor:
            page = self.a.list_page('alice', limit=2, cursor=cursor)
            rest.extend(r['draft_id'] for r in page['items']); cursor = page['next_cursor']
        ids = [r['draft_id'] for r in first['items']] + rest
        self.assertEqual(ids, sorted(original, reverse=True)); self.assertNotIn(added, ids)

    def test_pending_reviews_survive_history_and_owner_filter_applies_to_every_page(self):
        pending = self.review(self.draft())
        other = self.draft('bob')
        history = []
        self.review(other, 'bob')
        self.a.withdraw('bob', request_id=self.a.reviews('bob')['items'][0]['request_id'])
        # Same-time rows require the immutable request ID as the second sort key.
        with closing(self.a.store.connect()) as db, db:
            source = db.execute('SELECT * FROM skill_review_requests WHERE owner_subject=?', ('bob',)).fetchone()
            from uuid import uuid4
            for _ in range(105):
                values = list(source); values[0] = str(uuid4()); history.append(values[0])
                db.execute('INSERT INTO skill_review_requests VALUES (' + ','.join('?' for _ in values) + ')', values)
        self.assertEqual(self.a.reviews()['items'][0]['request_id'], pending['request_id'])
        self.assertEqual([r['request_id'] for r in self.a.review_page()['items']], [pending['request_id']])
        ids = self.pages(self.a.review_page, key='request_id', state='history', limit=19)
        self.assertEqual(len(ids), 106); self.assertEqual(len(set(ids)), 106)
        self.assertEqual(self.a.review_page('alice', state='history')['items'], [])
        self.assertEqual(len(self.pages(lambda **kw:self.a.review_page('bob', **kw), key='request_id', state='history', limit=19)), 106)

    def test_invalid_cursor_state_limit_and_cross_scope_are_rejected(self):
        for _ in range(2): self.draft()
        cursor = self.a.list_page('alice', limit=1)['next_cursor']
        for call in (lambda: self.a.list_page('bob', cursor=cursor),
                     lambda: self.a.list_page('alice', state='archived', cursor=cursor),
                     lambda: self.a.review_page('alice', cursor=cursor)):
            with self.assertRaisesRegex(ValueError, '游标'): call()
        for value in (0, 101, True, '1', 1.5):
            with self.subTest(limit=value), self.assertRaises(ValueError): self.a.list_page('alice', limit=value)
            with self.subTest(limit=value), self.assertRaises(ValueError): self.a.review_page(limit=value)
        for value in ('!', 'a' * 1025, 'W10=', base64.urlsafe_b64encode(json.dumps([['drafts','alice','active'],'bad','bad']).encode()).decode()):
            with self.subTest(cursor=value[:20]), self.assertRaises(ValueError): self.a.list_page('alice', cursor=value)
        for value in ('all', '', None, []):
            with self.subTest(state=value), self.assertRaises(ValueError): self.a.list_page('alice', state=value)
            with self.subTest(state=value), self.assertRaises(ValueError): self.a.review_page(state=value)
