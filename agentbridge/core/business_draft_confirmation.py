"""OA opinion confirmation reads once, using a separate displayed-material receipt.

These immutable receipts are evidence of what was displayed, never a prepared
plan or permission to write. Only a fresh real prepare can create authorization.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
import secrets

from agentbridge.core.business_content_identity import (
    identity_from_target, principal_binding, validated_identity,
)
from agentbridge.core.business_draft_profiles import profile_for_source
from agentbridge.core.store_transactions import store_transaction


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS business_draft_material_reviews (
            review_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL,
            identity_json TEXT NOT NULL, fingerprint TEXT NOT NULL,
            review_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS business_draft_material_reviews_draft
        ON business_draft_material_reviews(draft_id);
        CREATE TRIGGER IF NOT EXISTS immutable_business_draft_material_review
        BEFORE UPDATE ON business_draft_material_reviews BEGIN
            SELECT RAISE(ABORT, 'business draft material review is immutable');
        END;
    """)


def eligible(source):
    profile = profile_for_source(source)
    schema = source['form_schema']
    return bool(source['system_id'] == 'oa' and profile.view_kind == 'approval'
        and profile.fields == ('opinion',)
        and re.fullmatch(r'sha256:[0-9a-f]{64}', str(schema.get('_agentbridge_review_fingerprint') or ''))
        and validated_identity(profile, schema.get('_agentbridge_content_identity'))
        and schema.get('_agentbridge_content_principal') and isinstance(schema.get('review'), dict))


def latest(connection, draft):
    return connection.execute('SELECT * FROM business_draft_material_reviews WHERE draft_id=? ORDER BY rowid DESC LIMIT 1',
                              (draft['draft_id'],)).fetchone()


def insert(connection, source, draft, *, identity, fingerprint, review, now):
    from agentbridge.core.business_drafts import _json
    connection.execute('INSERT INTO business_draft_material_reviews VALUES (?,?,?,?,?,?,?)',
        (secrets.token_urlsafe(24), draft['draft_id'], _json(identity), fingerprint,
         _json(review), now.isoformat(), source['expires_at']))


def ensure_initial(connection, source, draft, now):
    if eligible(source) and latest(connection, draft) is None and draft['state'] not in {'expired', 'finalized'}:
        insert(connection, source, draft, identity=source['form_schema']['_agentbridge_content_identity'],
               fingerprint=source['form_schema']['_agentbridge_review_fingerprint'],
               review=source['form_schema']['review'], now=now)


def project(connection, source, draft, result, now):
    if not eligible(source):
        return
    review = latest(connection, draft)
    result['ui']['confirmationMode'] = 'on_confirm'
    # Never fall back to an older source snapshot when a displayed receipt expires.
    result['confirmation'] = None
    if review is None or now >= datetime.fromisoformat(review['expires_at']):
        return
    if not result.get('preview'):
        result['ui']['review'] = deepcopy(json.loads(review['review_json']))
        result['ui']['review']['snapshot'] = 'confirmation'
    result['confirmation'] = {'reviewId': review['review_id'], 'expiresAt': review['expires_at']}
    if 'save' in result['allowedActions']:
        result['allowedActions'].append('confirm')


def _review_summary(summary, *, changed=True):
    return {**{key: deepcopy(summary[key]) for key in (
        'kind', 'materialsVersion', 'sections', 'attachments', 'materialPreviews',
        'completeness', 'truncated', 'title', 'effect') if key in summary},
        'fields': [deepcopy(field) for field in summary.get('fields', [])
                   if field.get('label') not in {'处理意见', '审批意见', '意见'}],
        'reviewChanged': changed}


def record_prepared(connection, source, draft, prepared, summary, now):
    """Legacy explicit previews also display a newly checked material version."""
    if not eligible(source):
        return
    profile = profile_for_source(source)
    target = prepared['plan']['target']
    identity = identity_from_target(profile, target)
    if identity != validated_identity(profile, source['form_schema']['_agentbridge_content_identity']):
        return
    reviewed = latest(connection, draft)
    fingerprint = target.get('review_fingerprint')
    if reviewed and fingerprint == reviewed['fingerprint']:
        return
    insert(connection, source, draft, identity=identity, fingerprint=fingerprint,
           review=_review_summary(summary, changed=summary.get('reviewChanged') is True), now=now)


def confirm(app, source_id, *, presentation_id, csrf_token, csrf_cookie,
            expected_revision, review_id, request_key):
    from agentbridge.core.business_drafts import BusinessDraftError, _json
    if not isinstance(request_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', request_key):
        raise BusinessDraftError('DRAFT_REQUEST_KEY_INVALID', '提交请求标识无效。')
    if not isinstance(review_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', review_id):
        raise BusinessDraftError('DRAFT_REVIEW_INVALID', '材料版本无效，请重新打开原页面。', status=409)
    # Bind recovery to the exact explicit request, including the displayed review.
    decision_key = 'confirm_' + hashlib.sha256(_json([request_key, expected_revision, review_id]).encode()).hexdigest()

    def checked(connection):
        source, interaction, task_id, endpoint, draft = app._checked(connection, source_id, presentation_id, csrf_token, csrf_cookie)
        if not eligible(source):
            raise BusinessDraftError('DRAFT_CONFIRM_UNSUPPORTED', '此页面需要使用原核对流程。', status=409)
        session = app.service.sessions.get(source['session_id'])
        if source['form_schema']['_agentbridge_content_principal'] != principal_binding(session):
            raise BusinessDraftError('DRAFT_SESSION_CHANGED', '实际登录账号与原任务不一致，请从原任务重新准备。', status=409)
        receipt = connection.execute('SELECT * FROM business_draft_decisions WHERE draft_id=?', (draft['draft_id'],)).fetchone()
        if receipt:
            if receipt['request_key'] == decision_key and receipt['revision'] == expected_revision:
                return source, interaction, task_id, endpoint, draft, receipt
            app._conflict(connection, draft, presentation_id, endpoint)
        app._editable(connection, draft, source, expected_revision, presentation_id, endpoint)
        return source, interaction, task_id, endpoint, draft, None

    with store_transaction(app.store) as connection:
        source, interaction, task_id, endpoint, draft, receipt = checked(connection)
        if receipt:
            return app._view(connection, draft, presentation_id, endpoint)
        reviewed = latest(connection, draft)
        if not reviewed or reviewed['review_id'] != review_id or app.store.clock() >= datetime.fromisoformat(reviewed['expires_at']):
            raise BusinessDraftError('DRAFT_REVIEW_STALE', '材料版本已变化，请核对当前材料后再确认。', status=409,
                current=app._view(connection, draft, presentation_id, endpoint))
        app._require_complete_review(json.loads(reviewed['review_json']))
        binding = app._authority(connection, source, interaction, task_id)
        values, _selection = app._values(connection, source, draft)
        version = draft['version']
        baseline_identity = json.loads(reviewed['identity_json'])
        baseline_fingerprint = reviewed['fingerprint']

    def validate():
        app._validate_queued_read(source_id, presentation_id, csrf_token, csrf_cookie,
                                 expected_revision, version, binding)
        with app.store._connect() as connection:
            session = app.service.sessions.get(source['session_id'])
            if source['form_schema']['_agentbridge_content_principal'] != principal_binding(session):
                raise BusinessDraftError('DRAFT_SESSION_CHANGED', '实际登录账号与原任务不一致，请从原任务重新准备。', status=409)
            current_review = latest(connection, draft)
            if not current_review or current_review['review_id'] != review_id:
                raise BusinessDraftError('DRAFT_REVIEW_STALE', '材料版本已变化，请重新核对。', status=409)

    try:
        with app._timing(source, task_id, 'draft.confirm'):
            prepared = app._prepare(source, task_id, values, priority=True, validate=validate)
        profile = profile_for_source(source)
        fresh_target = prepared['plan']['target']
        original_identity = validated_identity(profile, source['form_schema']['_agentbridge_content_identity'])
        fresh_identity = identity_from_target(profile, fresh_target)
        if not fresh_identity or fresh_identity != original_identity or fresh_identity != baseline_identity:
            raise BusinessDraftError('DRAFT_TARGET_CHANGED', '办理目标身份已变化，请从原任务重新准备。', status=409)
        fingerprint = fresh_target.get('review_fingerprint')
        if not isinstance(fingerprint, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', fingerprint):
            raise BusinessDraftError('DRAFT_MATERIALS_INCOMPLETE', '无法确认完整材料，请从原任务重新准备。', status=409)
        app._require_complete_review(prepared['summary'])
    except Exception as exc:
        raise app._read_error(exc) from exc

    changed = fingerprint != baseline_fingerprint
    with store_transaction(app.store) as connection:
        source, interaction, task_id, endpoint, current, receipt = checked(connection)
        if receipt:
            return app._view(connection, current, presentation_id, endpoint)
        current_review = latest(connection, current)
        if (current['version'] != version or not current_review or current_review['review_id'] != review_id
                or binding != app._authority(connection, source, interaction, task_id)):
            app._conflict(connection, current, presentation_id, endpoint)
        if changed:
            insert(connection, source, current, identity=fresh_identity, fingerprint=fingerprint,
                   review=_review_summary(prepared['summary']), now=app.store.clock())
            connection.execute("UPDATE business_drafts SET version=version+1,state='editing',preview_id=NULL,updated_at=? WHERE draft_id=?",
                               (app.store.clock().isoformat(), current['draft_id']))
            connection.execute('DELETE FROM business_draft_previews WHERE draft_id=?', (current['draft_id'],))
            view = app._view(connection, app._draft(connection, source_id), presentation_id, endpoint)
        else:
            app._save_preview(connection, source, current, values, binding, prepared)
            current = app._draft(connection, source_id)
            return app._approve_checked(connection, source_id, presentation_id=presentation_id,
                csrf_token=csrf_token, csrf_cookie=csrf_cookie, expected_revision=expected_revision,
                preview_id=current['preview_id'], request_key=decision_key, checked_version=current['version'])
    raise BusinessDraftError('DRAFT_REVIEW_CHANGED', '办理材料已变化，请核对更新后的材料，再明确确认本次操作。',
                             status=409, current=view)
