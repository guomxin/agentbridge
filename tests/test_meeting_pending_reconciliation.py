"""Verified meeting receipts retire copies left by the historical entry path.

Current preparation guards prevent creating these duplicates. Only synthetic
legacy source/decision setup bypasses that guard; submission and retirement use
the real runtime after the patch has ended.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

import pytest
from agentbridge.core.business_drafts import BusinessDraftService
from agentbridge.core.oa_pending_reconciliation import reconcile_completed_pending_intents
from tests.support.meeting_reply_acceptance import MeetingReplyAcceptanceFixture, OWNER


def finish(fixture, case, attitude):
    drafts = BusinessDraftService(fixture.service)
    # Existing duplicate cards could already have produced a confirmed winner
    # before this release. Recreate only that old setup, not its execution.
    with patch.object(fixture.service, '_guard_pending_preparation'):
        opened = drafts.open(case['input_id'])
        args = dict(source_submission_id=case['input_id'], csrf_token=opened['csrfToken'], csrf_cookie=opened['csrfToken'])
        saved = drafts.save(**args, expected_revision=opened['state']['revision'], values={'attitude': attitude, 'feedback': ''})
        ready = drafts.preview(**args, expected_revision=saved['revision'])
        receipt = drafts.approve(**args, expected_revision=ready['revision'], preview_id=ready['preview']['previewId'], request_key=uuid4().hex)['receipt']
    fixture.resume(receipt['interactionId'])
    auth = fixture.service.write_authorizations.get(receipt['authorizationId'])
    return fixture.service.operations.get(auth['commit_operation_id'])


@pytest.mark.parametrize('attitude', ['join', 'not_join', 'pending'])
def test_meeting_closes_all_unused_copies_but_preserves_batch(tmp_path, attitude):
    fixture = MeetingReplyAcceptanceFixture(tmp_path)
    try:
        with patch.object(fixture.service, '_guard_pending_preparation'):
            old = [fixture.add_meeting_case() for _ in range(2)]
            batch = fixture.add_meeting_case(batch=True)
            winner = fixture.add_meeting_case()
        operation = finish(fixture, winner, attitude)
        assert operation['status'] == 'succeeded'
        for case in old:
            assert fixture.service.field_submissions.get(case['input_id'])['state'] == 'superseded'
            assert fixture.service.tasks.get_task(case['task_id'], user_subject=OWNER)['status'] == 'superseded'
        assert fixture.service.field_submissions.get(batch['input_id'])['state'] == 'pending'
        assert len(fixture.meeting_writes) == 1
        assert reconcile_completed_pending_intents(fixture.service, operation, task_id=winner['task_id']) == []
    finally:
        fixture.close()


@pytest.mark.parametrize('fault', ['unknown', 'failed', 'unverified', 'meeting', 'reply', 'attitude', 'method'])
def test_inexact_meeting_receipt_never_closes_old_card(tmp_path, fault):
    fixture = MeetingReplyAcceptanceFixture(tmp_path)
    try:
        with patch.object(fixture.service, '_guard_pending_preparation'):
            old = fixture.add_meeting_case()
            winner = fixture.add_meeting_case()
        with patch('agentbridge.core.oa_pending_reconciliation.reconcile_completed_pending_intents', return_value=[]):
            operation = finish(fixture, winner, 'join')
        operation = deepcopy(operation)
        if fault in {'unknown', 'failed'}:
            operation['status'] = fault
        elif fault == 'unverified':
            operation['result']['verified'] = False
        else:
            key = {'meeting': 'meeting_id', 'reply': 'reply_id'}.get(fault, fault)
            operation['result']['verification'][key] = 'mismatch'
        assert reconcile_completed_pending_intents(fixture.service, operation, task_id=winner['task_id']) == []
        assert fixture.service.field_submissions.get(old['input_id'])['state'] == 'pending'
        assert len(fixture.meeting_writes) == 1
    finally:
        fixture.close()


def test_every_supported_pending_type_has_completion_proof():
    from agentbridge.adapters.seeyon_pending_batch import PENDING_BATCH_TYPES
    from agentbridge.core.business_draft_profiles import BY_PREPARE
    from agentbridge.core.write_catalog import _TRUSTED_WRITE_COMMITS
    from agentbridge.core.oa_pending_reconciliation import _completion_kind
    covered = set()
    for capability, (prepare, definition) in _TRUSTED_WRITE_COMMITS.items():
        profile = BY_PREPARE.get(prepare)
        if profile and profile.review_kind in PENDING_BATCH_TYPES and "affair_id" in definition.get("context_fields", ()):
            assert _completion_kind(capability, profile, definition), capability
            covered.add(profile.review_kind)
    assert covered == set(PENDING_BATCH_TYPES)
