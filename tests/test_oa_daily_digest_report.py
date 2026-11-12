"""The daily report reads owned watch evidence without refreshing an OA session."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.oa_daily_digest_report import collect_oa_daily_digest
from tests.test_business_watch_service import context, counts, snapshot, source


def _watch(ctx, subject, number, start, change):
    store = ctx.watches.store
    store.clock = lambda: start
    watch, _ = store.create(
        user_subject=subject,
        principal_ref=principal_binding(ctx.sessions[subject]),
        system_instance=ctx.watches.system_instance,
        source={**source(number), "collection": "sent"},
        title=f"关注样例 {number}",
        rules=["nodes", "handlers", "ended"],
        interval_seconds=3600,
        expires_at=(start + timedelta(days=2)).isoformat(),
        endpoints=[],
        allow_mark_read=True,
        include_private=True,
    )
    initial = store.apply_observation(
        watch["watch_id"], user_subject=subject, expected_version=watch["version"],
        snapshot={**snapshot(name="初审"), "observed_at": start.isoformat()},
    )["watch"]
    store.clock = lambda: change
    store.apply_observation(
        watch["watch_id"], user_subject=subject, expected_version=initial["version"],
        snapshot={**snapshot(name="复核"), "observed_at": change.isoformat()},
    )


def test_report_reads_owned_changes_without_opening_details_or_refreshing_activity(context):
    ctx = context
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    _watch(ctx, "user-a", 1, since, now - timedelta(minutes=5))
    _watch(ctx, "user-b", 2, since, now - timedelta(minutes=4))
    previous_counts = counts(ctx)
    session_id = ctx.sessions["user-a"]["session_id"]
    activity = ctx.service.sessions.get(session_id).get("last_user_activity_at")

    with patch.object(ctx.service.adapter, "list_workflows", return_value={
        "collection": "pending",
        "items": [{"title": "待办样例"}],
        "coverage": {"status": "complete", "observedAt": now.isoformat()},
    }) as read_pending:
        report = collect_oa_daily_digest(ctx.service, user_subject="user-a", now=now, since=since, trial=True)

    read_pending.assert_called_once_with(ctx.workers[0], collection="pending", arguments={"limit": 100})
    assert "OA 每日简报（测试）" in report.text
    assert "当前 1 项" in report.text and "待办样例" in report.text
    assert "记录到 1 次变化" in report.text and "复核" in report.text
    assert "关注样例 1" in report.text and "关注样例 2" not in report.text
    assert report.watch_coverage_complete
    assert counts(ctx) == previous_counts
    assert ctx.service.sessions.get(session_id).get("last_user_activity_at") == activity
    assert ctx.workers[0].closed and not ctx.workers[0].captured


def test_report_does_not_render_cached_watches_on_live_oa_identity_mismatch(context):
    ctx = context
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1)
    _watch(ctx, "user-a", 1, since, now - timedelta(minutes=5))
    ctx.probe.side_effect = lambda _: {"authenticated": True, "observed_principal_ref": "Bob"}
    with patch.object(ctx.service.adapter, "list_workflows") as read_pending:
        report = collect_oa_daily_digest(ctx.service, user_subject="user-a", now=now, since=since, trial=False)

    read_pending.assert_not_called()
    assert "本次无法确认 OA 账号身份" in report.text
    assert "关注样例 1" not in report.text
    assert not report.watch_coverage_complete


def test_report_bounds_unicode_payload_and_describes_partial_pending_coverage(context):
    ctx = context
    now = datetime.now(timezone.utc)
    items = [{"title": f"{index} " + "😀" * 120 + " https://example.test/sensitive"} for index in range(100)]
    with patch.object(ctx.service.adapter, "list_workflows", return_value={
        "collection": "pending",
        "items": items,
        "coverage": {"status": "partial", "hasMore": True},
        "total": 101,
    }):
        report = collect_oa_daily_digest(
            ctx.service, user_subject="user-a", now=now, since=now - timedelta(days=1), trial=False,
        )

    assert "已读到 100 项，OA 显示共 101 项，覆盖不完整" in report.text
    assert "另有 " in report.text and "项未在此列出" in report.text
    assert "https://" not in report.text
    assert len(report.text.encode("utf-16-le")) // 2 <= 3500
    assert report.watch_coverage_complete
