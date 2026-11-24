"""Unit tests for NanoBot Telegram rich card, result view, status, and Markdown rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge")
)

from nanobot_agentbridge.telegram_presentation import (  # noqa: E402
    ACTION_LABELS,
    BUSINESS_VIEWS,
    EXTERNAL_BROWSER_LABEL,
    format_ordinary_telegram_message,
    interaction_can_open,
    interaction_entry_label,
    normalize_interaction,
    normalize_result_view,
    render_interaction_message,
    render_result_view_message,
    render_status_message,
)

ORIGIN = "https://bridge.example.test"
ALLOWED = frozenset({ORIGIN})
SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas/agent-host/v1/interaction-presentation.schema.json"
)


def _future(minutes: int = 10) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _past(minutes: int = 5) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _interaction(
    *,
    interaction_id: str = "interaction-1234567890abcdef",
    kind: str = "business_input",
    state: str = "pending",
    url: str | None = None,
    expires_at: str | None = None,
    display: dict | None = None,
    title: str = "请核对审批材料",
    message: str = "打开安全页面核对后提交。",
) -> dict:
    return {
        "schemaVersion": "agentbridge.interaction.v1",
        "interactionId": interaction_id,
        "type": kind,
        "state": state,
        "title": title,
        "message": message,
        "presentation": {
            "owner": "agentbridge",
            "url": url if url is not None else f"{ORIGIN}/flow/input/token-abcdef0123456789",
            "modelMustNotCollectValues": True,
            "flowVersion": "agentbridge.trusted-flow.v1",
        },
        "display": display or {},
        "expiresAt": expires_at if expires_at is not None else _future(),
    }


def _result_view(
    *,
    source_task_id: str = "task-1234567890abcdef",
    endpoint_id: str = "endpoint-tg-1",
    token: str = "A" * 43,
    expires_at: str | None = None,
    source_expires_at: str | None = None,
    selection_available: bool = True,
) -> dict:
    return {
        "schemaVersion": "agentbridge.result-view.v1",
        "sourceTaskId": source_task_id,
        "expiresAt": expires_at if expires_at is not None else _future(30),
        "selectionAvailable": selection_available,
        "sourceExpiresAt": source_expires_at if source_expires_at is not None else _future(10),
        "presentation": {
            "owner": "agentbridge",
            "endpointId": endpoint_id,
            "url": f"{ORIGIN}/results/{token}",
            "modelMustNotCollectValues": True,
        },
    }


def test_business_views_match_closed_schema_and_ignore_arbitrary_payload_labels():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    bview_def = (
        schema["properties"]["display"]["properties"]["businessView"]["properties"]["profileId"]["enum"]
    )
    assert set(BUSINESS_VIEWS.keys()) == set(bview_def)
    assert len(BUSINESS_VIEWS) == 29

    for profile_id, (view_kind, expected_label) in BUSINESS_VIEWS.items():
        card = _interaction(
            kind="business_input",
            display={
                "businessView": {
                    "schemaVersion": "agentbridge.business-view.v1",
                    "profileId": profile_id,
                    "profileVersion": "1",
                    "viewKind": view_kind,
                    "entryLabel": "恶意覆盖按钮文案",
                    "submitLabel": "一键自动同意",
                }
            },
        )
        normalized = normalize_interaction(card, ALLOWED)
        assert normalized is not None
        label = interaction_entry_label(normalized)
        assert label == expected_label
        assert "恶意覆盖" not in label
        rendered = render_interaction_message(normalized, ALLOWED)
        assert rendered is not None
        assert "恶意覆盖" not in rendered["text"]
        buttons = rendered["reply_markup"]["inline_keyboard"][0]
        assert buttons[0]["text"] == expected_label
        assert buttons[0]["web_app"] == {"url": card["presentation"]["url"]}
        assert buttons[1] == {"text": EXTERNAL_BROWSER_LABEL, "url": card["presentation"]["url"]}


def test_action_labels_and_batch_progress_labels():
    auth = _interaction(
        kind="execution_authorization",
        url=f"{ORIGIN}/flow/authorize/token-abcdef0123456789",
        display={
            "systemName": "OA 审批",
            "actionView": {
                "schemaVersion": "agentbridge.action-view.v1",
                "capabilityId": "oa.leave.approve",
            },
            "batch": {
                "schemaVersion": "agentbridge.batch-view.v1",
                "totalCount": 4,
                "currentIndex": 2,
                "succeededCount": 1,
                "state": "waiting_user",
            },
        },
    )
    rendered = render_interaction_message(auth, ALLOWED)
    assert rendered is not None
    assert rendered["parse_mode"] == "HTML"
    assert "批次第 2/4 项，已核验完成 1/4 项" in rendered["text"]
    assert "返回 Workspace" not in rendered["text"]
    primary = rendered["reply_markup"]["inline_keyboard"][0][0]
    assert primary["text"] == "第 2/4 项 · 核对请假材料"
    assert primary["web_app"]["url"] == auth["presentation"]["url"]
    assert ACTION_LABELS["oa.attendance_confirmation.confirm"] == "核对月度考勤"
    assert ACTION_LABELS["smartlight.alarm.dispose"] == "核对 RTU 告警处置"


@pytest.mark.parametrize(
    "kind,state,expected_snippet",
    [
        ("business_input", "processing", "处理中"),
        ("business_input", "completed", "办理内容已接收"),
        ("execution_authorization", "completed", "已授权，业务结果待核验"),
        ("credential", "completed", "登录已完成"),
        ("business_input", "declined", "已拒绝"),
        ("business_input", "expired", "已过期"),
        ("business_input", "failed", "失败"),
        ("business_input", "superseded", "已替换"),
    ],
)
def test_non_pending_interactions_emit_zero_buttons_and_zero_urls(kind, state, expected_snippet):
    card = _interaction(kind=kind, state=state)
    rendered = render_interaction_message(card, ALLOWED)
    assert rendered is not None
    assert "reply_markup" not in rendered
    assert ORIGIN not in rendered["text"]
    assert "/flow/" not in rendered["text"]
    assert expected_snippet in rendered["text"]
    assert not interaction_can_open(card)


def test_expired_timestamp_and_stopped_batch_never_emit_buttons():
    expired = _interaction(expires_at=_past())
    rendered = render_interaction_message(expired, ALLOWED)
    assert rendered is not None
    assert "reply_markup" not in rendered
    assert ORIGIN not in rendered["text"]
    assert not interaction_can_open(expired)

    for stopped_state in ("outcome_unknown", "failed", "canceled", "partially_succeeded", "expired"):
        stopped = _interaction(
            display={
                "batch": {
                    "schemaVersion": "agentbridge.batch-view.v1",
                    "totalCount": 3,
                    "currentIndex": 2,
                    "succeededCount": 1,
                    "state": stopped_state,
                }
            }
        )
        assert not interaction_can_open(stopped)
        out = render_interaction_message(stopped, ALLOWED)
        assert out is not None
        assert "reply_markup" not in out
        assert ORIGIN not in out["text"]


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://bridge.example.test/flow/input/abc",
        "https://evil.example.test/flow/input/abc",
        "https://user:pass@bridge.example.test/flow/input/abc",
        "https://bridge.example.test/flow/input/abc#frag",
        "https://bridge.example.test/flow/input/abc with space",
        "javascript:alert(1)",
    ],
)
def test_untrusted_interaction_urls_rejected(bad_url):
    assert normalize_interaction(_interaction(url=bad_url), ALLOWED) is None
    assert render_interaction_message(_interaction(url=bad_url), ALLOWED) is None


def test_html_escaping_in_interaction_and_status_cards():
    card = _interaction(
        title="<script>alert(1)</script> & 请假",
        message='内容 <b>raw</b> "quote"',
        display={"systemName": "OA <test>"},
    )
    rendered = render_interaction_message(card, ALLOWED)
    assert rendered is not None
    assert "<script>" not in rendered["text"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered["text"]
    assert "&lt;b&gt;raw&lt;/b&gt;" in rendered["text"]
    assert "OA &lt;test&gt;" in rendered["text"]

    unknown = render_status_message("<img src=x> 结果未知", tone="warning", unknown=True)
    assert unknown["parse_mode"] == "HTML"
    assert "reply_markup" not in unknown
    assert "<img" not in unknown["text"]
    assert "请先核对业务系统，不要重复提交" in unknown["text"]


def test_result_view_normalization_and_rendering():
    raw = _result_view()
    view = normalize_result_view(
        raw,
        source_task_id="task-1234567890abcdef",
        endpoint_id="endpoint-tg-1",
        allowed_origins=ALLOWED,
    )
    assert view is not None
    rendered = render_result_view_message(view, summary="已查询，发现 3 项 OA 待办。")
    assert rendered["parse_mode"] == "HTML"
    assert "OA 待办 · 选择与办理" in rendered["text"]
    assert "不会提交业务" in rendered["text"]
    buttons = rendered["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["text"] == "选择并办理 OA 待办"
    assert buttons[0]["web_app"] == {"url": view["url"]}
    assert buttons[1] == {"text": EXTERNAL_BROWSER_LABEL, "url": view["url"]}

    expired_source = normalize_result_view(
        _result_view(source_expires_at=_past()),
        source_task_id="task-1234567890abcdef",
        endpoint_id="endpoint-tg-1",
        allowed_origins=ALLOWED,
    )
    assert expired_source is not None
    rendered_expired = render_result_view_message(expired_source)
    assert rendered_expired["reply_markup"]["inline_keyboard"][0][0]["text"] == "查看办理进度"
    assert "可选时效已结束" in rendered_expired["text"]

    # Rejection boundaries
    assert normalize_result_view(raw, source_task_id="other-task", endpoint_id="endpoint-tg-1", allowed_origins=ALLOWED) is None
    assert normalize_result_view(raw, source_task_id="task-1234567890abcdef", endpoint_id="other-ep", allowed_origins=ALLOWED) is None
    assert normalize_result_view(_result_view(token="too-short"), source_task_id="task-1234567890abcdef", endpoint_id="endpoint-tg-1", allowed_origins=ALLOWED) is None
    assert normalize_result_view(_result_view(expires_at=_future(90)), source_task_id="task-1234567890abcdef", endpoint_id="endpoint-tg-1", allowed_origins=ALLOWED) is None
    bad_query = _result_view()
    bad_query["presentation"]["url"] += "?x=1"
    assert normalize_result_view(bad_query, source_task_id="task-1234567890abcdef", endpoint_id="endpoint-tg-1", allowed_origins=ALLOWED) is None


def test_ordinary_markdown_formatting_preserves_plain_text_and_escapes_html():
    plain = format_ordinary_telegram_message("合成结果 alpha")
    assert plain == {"text": "合成结果 alpha"}
    assert "parse_mode" not in plain

    md = format_ordinary_telegram_message(
        "# 汇总\n**共 2 项**，命令 `oa.list` <script>x</script>\n```python\na < b\n```"
    )
    assert md["parse_mode"] == "HTML"
    assert "<b>汇总</b>" in md["text"]
    assert "<b>共 2 项</b>" in md["text"]
    assert "<code>oa.list</code>" in md["text"]
    assert "&lt;script&gt;x&lt;/script&gt;" in md["text"]
    assert "<pre><code>a &lt; b</code></pre>" in md["text"]

    huge = "**" + ("测" * 4092) + "**"
    fallback = format_ordinary_telegram_message(huge)
    assert fallback == {"text": huge}
    assert "parse_mode" not in fallback
