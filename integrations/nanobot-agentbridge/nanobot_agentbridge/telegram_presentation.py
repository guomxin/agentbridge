"""Render AgentBridge trusted interactions, result views, status cards, and Markdown for Telegram."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
import re
from urllib.parse import urlsplit

INTERACTION_SCHEMA_VERSION = "agentbridge.interaction.v1"
RESULT_VIEW_SCHEMA_VERSION = "agentbridge.result-view.v1"
FLOW_VERSION = "agentbridge.trusted-flow.v1"
EXTERNAL_BROWSER_LABEL = "浏览器打开"

INTERACTION_TYPES = frozenset({"credential", "business_input", "execution_authorization"})
INTERACTION_STATES = frozenset({
    "pending", "processing", "completed", "declined", "expired", "failed", "superseded",
})
BATCH_STATES = frozenset({
    "running", "waiting_user", "succeeded", "partially_succeeded",
    "failed", "outcome_unknown", "canceled", "expired",
})
STATE_LABELS = {
    "pending": "等待操作",
    "processing": "处理中",
    "completed": "交互已完成，业务结果待核验",
    "declined": "已拒绝",
    "expired": "已过期",
    "failed": "失败",
    "superseded": "已替换",
}

# Closed static mapping matching schemas/agent-host/v1/interaction-presentation.schema.json
# and OpenClaw interaction-business-view.js. Never render arbitrary labels from payloads.
BUSINESS_VIEWS: dict[str, tuple[str, str]] = {
    "oa.meeting.reply": ("form", "核对会议邀请"),
    "taihua.work_log.create": ("work_log", "填写工作日志"),
    "oa.meeting_room.application.create": ("meeting_room", "选择会议室"),
    "oa.meeting.create": ("meeting_room", "安排会议"),
    "oa.meeting_room.application.cancel": ("form", "核对撤销申请"),
    "oa.workflow.revoke": ("form", "核对流程撤销"),
    "oa.business_trip.save_draft": ("form", "填写出差草稿"),
    "oa.business_trip.submit": ("form", "填写出差申请"),
    "oa.leave.save_draft": ("form", "填写请假草稿"),
    "oa.leave.submit": ("form", "填写请假申请"),
    "oa.missed_punch.save_draft": ("form", "填写补签草稿"),
    "smartlight.alarm.remark.update": ("form", "编辑告警备注"),
    "oa.missed_punch.approval": ("approval", "核对补签材料"),
    "oa.efficiency_data.approval": ("approval", "核对效能数据"),
    "oa.travel_expense.approval": ("approval", "核对差旅报销"),
    "oa.business_trip.approval": ("approval", "核对出差材料"),
    "oa.business_settlement.approval": ("approval", "核对业务结算单"),
    "oa.labor_contract_renewal.approval": ("approval", "核对合同续签"),
    "oa.intellectual_property_declaration.approval": ("approval", "核对知识产权材料"),
    "oa.intellectual_property_reward.acknowledgement": ("approval", "核对知识产权奖励并阅办"),
    "oa.overtime.approval": ("approval", "核对加班材料"),
    "oa.leave.approval": ("approval", "核对请假材料"),
    "oa.resignation.approval": ("approval", "核对离职材料"),
    "oa.work_handover.approval": ("approval", "核对交接材料"),
    "oa.flight_application.approval": ("approval", "核对乘机申请"),
    "oa.standard_collaboration.approval": ("approval", "核对协同事项"),
    "oa.attendance_confirmation": ("approval", "核对月度考勤"),
    "oa.weekly_report.acknowledgement": ("approval", "核对周报"),
    "oa.quarterly_performance.acknowledgement": ("approval", "核对季度绩效"),
}

ACTION_LABELS: dict[str, str] = {}
for _profile_id, (_kind, _label) in BUSINESS_VIEWS.items():
    _cap = (
        "oa.attendance_confirmation.confirm"
        if _profile_id == "oa.attendance_confirmation"
        else re.sub(r"\.acknowledgement$", ".acknowledge", re.sub(r"\.approval$", ".approve", _profile_id))
    )
    ACTION_LABELS[_cap] = re.sub(r"^(?:填写|编辑|选择|安排)", "核对", _label)
ACTION_LABELS.update({
    "smartlight.alarm.work_area.submit": "核对工区提交",
    "smartlight.alarm.work_area.revoke": "核对工区撤回",
    "smartlight.alarm.dispose": "核对 RTU 告警处置",
})

_DEFAULT_ENTRY_LABELS = {
    "credential": "安全登录",
    "business_input": "填写信息",
    "execution_authorization": "核对并确认",
}


def _safe_str(value: object, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _trusted_https_url(raw: object, allowed_origins: frozenset[str]) -> str | None:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        return None
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in raw):
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    origin = f"https://{parsed.netloc}"
    if origin not in allowed_origins:
        return None
    return raw


def _is_expired(expires_at: object, *, now: datetime | None = None) -> bool:
    if not isinstance(expires_at, str) or not expires_at.strip():
        return False
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return parsed <= current


def batch_view_for_interaction(interaction: dict) -> dict | None:
    display = interaction.get("display")
    if not isinstance(display, dict):
        return None
    batch = display.get("batch")
    if not isinstance(batch, dict):
        return None
    total = batch.get("totalCount")
    current = batch.get("currentIndex")
    succeeded = batch.get("succeededCount")
    state = batch.get("state")
    if (
        batch.get("schemaVersion") != "agentbridge.batch-view.v1"
        or type(total) is not int or not 1 <= total <= 1000
        or type(current) is not int or not 1 <= current <= total
        or type(succeeded) is not int or not 0 <= succeeded <= total
        or state not in BATCH_STATES
    ):
        return None
    return {
        "totalCount": total,
        "currentIndex": current,
        "succeededCount": succeeded,
        "state": state,
    }


def business_view_for_interaction(interaction: dict) -> dict | None:
    if interaction.get("type") != "business_input":
        return None
    display = interaction.get("display")
    if not isinstance(display, dict):
        return None
    view = display.get("businessView")
    if (
        not isinstance(view, dict)
        or view.get("schemaVersion") != "agentbridge.business-view.v1"
        or view.get("profileVersion") != "1"
    ):
        return None
    profile_id = view.get("profileId")
    known = BUSINESS_VIEWS.get(profile_id) if isinstance(profile_id, str) else None
    if not known or known[0] != view.get("viewKind"):
        return None
    return {"profileId": profile_id, "viewKind": known[0], "entryLabel": known[1]}


def interaction_entry_label(interaction: dict) -> str:
    display = interaction.get("display") if isinstance(interaction.get("display"), dict) else {}
    action = display.get("actionView")
    action_label = None
    if (
        interaction.get("type") == "execution_authorization"
        and isinstance(action, dict)
        and action.get("schemaVersion") == "agentbridge.action-view.v1"
        and isinstance(action.get("capabilityId"), str)
    ):
        action_label = ACTION_LABELS.get(action["capabilityId"])
    bview = business_view_for_interaction(interaction)
    label = (
        (bview["entryLabel"] if bview else None)
        or action_label
        or _DEFAULT_ENTRY_LABELS.get(str(interaction.get("type")), "继续处理")
    )
    batch = batch_view_for_interaction(interaction)
    return f"第 {batch['currentIndex']}/{batch['totalCount']} 项 · {label}" if batch else label


def interaction_can_open(interaction: dict, *, now: datetime | None = None) -> bool:
    if interaction.get("state") != "pending" or _is_expired(interaction.get("expiresAt"), now=now):
        return False
    display = interaction.get("display")
    if isinstance(display, dict) and display.get("batch") is not None:
        batch = batch_view_for_interaction(interaction)
        return bool(batch and batch["state"] in {"running", "waiting_user"})
    return True


def normalize_interaction(raw: object, allowed_origins: frozenset[str]) -> dict | None:
    if not isinstance(raw, dict) or raw.get("schemaVersion") != INTERACTION_SCHEMA_VERSION:
        return None
    interaction_id = _safe_str(raw.get("interactionId"), 128)
    kind = _safe_str(raw.get("type"), 64)
    state = _safe_str(raw.get("state"), 64)
    pres = raw.get("presentation")
    if (
        len(interaction_id) < 16
        or kind not in INTERACTION_TYPES
        or state not in INTERACTION_STATES
        or not isinstance(pres, dict)
        or pres.get("owner") != "agentbridge"
        or pres.get("modelMustNotCollectValues") is not True
    ):
        return None
    url = _trusted_https_url(pres.get("url"), allowed_origins)
    if state in {"pending", "processing"} and not url:
        return None
    flow_version = None
    if pres.get("flowVersion") == FLOW_VERSION and url:
        if re.match(r"^/flow/(?:input|authorize)/", urlsplit(url).path):
            flow_version = FLOW_VERSION
    display = dict(raw["display"]) if isinstance(raw.get("display"), dict) else {}
    return {
        "schemaVersion": INTERACTION_SCHEMA_VERSION,
        "interactionId": interaction_id,
        "type": kind,
        "state": state,
        "title": _safe_str(raw.get("title"), 200) or "AgentBridge",
        "message": _safe_str(raw.get("message"), 1000),
        "presentation": {
            "owner": "agentbridge",
            "url": url,
            "modelMustNotCollectValues": True,
            **({"flowVersion": flow_version} if flow_version else {}),
        },
        "display": display,
        "expiresAt": _safe_str(raw.get("expiresAt"), 80) or None,
    }


def _context_text(interaction: dict) -> str:
    display = interaction["display"]
    parts = ["AgentBridge 可信交互"]
    if isinstance(display.get("systemName"), str) and display["systemName"].strip():
        parts.append(display["systemName"].strip()[:100])
    if type(display.get("fieldCount")) is int:
        parts.append(f"{display['fieldCount']} 个字段")
    batch = batch_view_for_interaction(interaction)
    if batch:
        parts.append(
            f"批次第 {batch['currentIndex']}/{batch['totalCount']} 项，已核验完成 {batch['succeededCount']}/{batch['totalCount']} 项"
        )
    completed_labels = {
        "credential": "登录已完成",
        "business_input": "办理内容已接收",
        "execution_authorization": "已授权，业务结果待核验",
    }
    state = interaction["state"]
    state_label = (
        completed_labels.get(interaction["type"], STATE_LABELS["completed"])
        if state == "completed"
        else STATE_LABELS.get(state, state)
    )
    parts.append(f"状态：{state_label}")
    return " · ".join(parts)


def _guidance_text(interaction: dict) -> str:
    display = interaction["display"]
    batch = batch_view_for_interaction(interaction)
    if display.get("batch") is not None and not batch:
        return "批次状态暂不可确认，请返回原任务核对最新状态。"
    if batch and batch["state"] not in {"running", "waiting_user"}:
        if batch["state"] == "outcome_unknown":
            return "本批次已停止，当前项业务结果未知；请先核对原任务，不会自动重试或继续下一项。"
        if batch["state"] == "succeeded":
            return "本批次各项已由业务系统核验完成，此入口不再办理。"
        return "本批次已停止，后续事项未继续执行，请返回原任务核对原因。"
    state = interaction["state"]
    if state == "completed":
        return (
            "登录完成后将继续原任务，请以原任务的业务核验结果为准。"
            if interaction["type"] == "credential"
            else "安全页面已接收本次处理；业务成功须以原任务的核验结果为准，请勿重复提交。"
        )
    if state == "processing":
        return "安全页面正在处理，请等待原任务更新，勿重复提交。"
    if state == "expired":
        return "此入口已失效，请返回原任务核对状态并重新准备。"
    if state == "superseded":
        return "请使用原任务中的最新卡片，此入口不再办理。"
    if state == "declined":
        return "本次交互已取消，系统不会据此继续执行。"
    if state == "failed":
        return "请先核对原任务的失败原因和业务结果，勿直接重复提交。"
    if interaction["type"] == "credential":
        return "凭据仅在安全页面填写，登录后继续原任务。"
    bview = business_view_for_interaction(interaction)
    if bview:
        guidance = (
            "在办理页核对本项材料、填写意见并最终确认；打开页面不会提交本项处理。"
            if bview["viewKind"] == "approval"
            else "在同一办理页填写、预览并最终确认；打开页面不会提交业务。"
        )
        return f"{guidance}本项核验成功后才进入下一项，每项单独确认。" if batch else guidance
    if interaction["type"] == "execution_authorization":
        return "请在安全页面核对目标、材料和执行范围后最终确认；授权后仍须核验业务结果。"
    return (
        "在办理页补充信息后继续核对与最终确认，等待原任务核验业务结果。"
        if interaction["presentation"].get("flowVersion") == FLOW_VERSION
        else "请在安全页面补充信息，再按原任务提示核对并最终确认。"
    )


def _tone_badge(state: str) -> str:
    if state in {"failed", "expired"}:
        return "⛔"
    if state in {"pending", "processing"}:
        return "⚠️"
    return "ℹ️"


def render_interaction_message(
    raw: object,
    allowed_origins: frozenset[str],
    *,
    now: datetime | None = None,
) -> dict | None:
    """Build Telegram sendMessage kwargs (text, parse_mode, optional reply_markup) for a card."""
    card = normalize_interaction(raw, allowed_origins)
    if card is None:
        return None
    if card["state"] == "pending" and _is_expired(card.get("expiresAt"), now=now):
        card = {**card, "state": "expired"}
    lines = [f"{_tone_badge(card['state'])} <b>{escape(card['title'])}</b>"]
    if card["message"]:
        lines.append(escape(card["message"]))
    lines.append(f"<i>{escape(_context_text(card))}</i>")
    lines.append(escape(_guidance_text(card)))
    params: dict = {
        "text": "\n\n".join(lines),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if interaction_can_open(card, now=now):
        url = card["presentation"]["url"]
        if url:
            params["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": interaction_entry_label(card), "web_app": {"url": url}},
                    {"text": EXTERNAL_BROWSER_LABEL, "url": url},
                ]]
            }
    return params


def normalize_result_view(
    raw: object,
    *,
    source_task_id: str,
    endpoint_id: str,
    allowed_origins: frozenset[str],
    now: datetime | None = None,
) -> dict | None:
    if not isinstance(raw, dict) or raw.get("schemaVersion") != RESULT_VIEW_SCHEMA_VERSION:
        return None
    pres = raw.get("presentation")
    if (
        raw.get("sourceTaskId") != source_task_id
        or not isinstance(pres, dict)
        or pres.get("owner") != "agentbridge"
        or pres.get("modelMustNotCollectValues") is not True
        or pres.get("endpointId") != endpoint_id
    ):
        return None
    url = _trusted_https_url(pres.get("url"), allowed_origins)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.query or not re.fullmatch(r"/results/[A-Za-z0-9_-]{43}", parsed.path):
        return None
    expires_raw = _safe_str(raw.get("expiresAt"), 80)
    if not expires_raw:
        return None
    try:
        expires_dt = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if expires_dt <= current or (expires_dt - current).total_seconds() > 61 * 60:
        return None
    source_expires = _safe_str(raw.get("sourceExpiresAt"), 80) or None
    return {
        "schemaVersion": RESULT_VIEW_SCHEMA_VERSION,
        "title": _safe_str(raw.get("title"), 120) or "选择并办理 OA 待办",
        "sourceTaskId": source_task_id,
        "url": url,
        "selectionAvailable": raw.get("selectionAvailable") is True,
        "expiresAt": expires_raw,
        "sourceExpiresAt": source_expires,
    }


def render_result_view_message(
    view: dict,
    *,
    summary: str = "OA 待办已查询，可从下方进入选择与办理。",
    now: datetime | None = None,
) -> dict:
    can_select = view.get("selectionAvailable") is True and not _is_expired(
        view.get("sourceExpiresAt"), now=now
    )
    guidance = (
        summary
        if can_select
        else "原查询的可选时效已结束，可查看已选事项的办理进度；新选事项请先重新查询。"
    )
    lines = [
        "ℹ️ <b>OA 待办 · 选择与办理</b>",
        escape(guidance),
        "<i>选择与打开页面不会提交业务；批量办理也须逐项确认，结果未知时停止并核对。</i>",
    ]
    params: dict = {
        "text": "\n\n".join(lines),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    url = view.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        label = "选择并办理 OA 待办" if can_select else "查看办理进度"
        params["reply_markup"] = {
            "inline_keyboard": [[
                {"text": label, "web_app": {"url": url}},
                {"text": EXTERNAL_BROWSER_LABEL, "url": url},
            ]]
        }
    return params


def render_status_message(
    text: str,
    *,
    tone: str = "info",
    unknown: bool = False,
) -> dict:
    title = "业务结果待核实" if unknown else "办理状态更新"
    badge = "⚠️" if unknown or tone == "warning" else ("⛔" if tone == "danger" else "ℹ️")
    body = text.strip() or "原任务状态已更新，请在原任务查看结果。"
    footer = (
        "请先核对业务系统，不要重复提交；此消息不会再次提交。"
        if unknown
        else "以原任务的业务核验结果为准；此消息不会再次提交。"
    )
    lines = [
        f"{badge} <b>{title}</b>",
        escape(body),
        f"<i>{footer}</i>",
    ]
    return {
        "text": "\n\n".join(lines),
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }


_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+-]+)?\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_MARKDOWN_HINT_RE = re.compile(r"(```|`[^`\n]+`|\*\*[^*\n]+\*\*|^#{1,6}\s+\S)", re.MULTILINE)


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def format_ordinary_telegram_message(text: str) -> dict:
    """Convert ordinary Markdown replies into safe Telegram HTML when Markdown is present.

    Plain text without Markdown syntax is sent as plain text so exact-text receipts and
    UTF-16 chunk splitting stay unchanged. If escaped HTML exceeds Telegram's 4096 UTF-16
    limit, falls back cleanly to plain text.
    """
    if not _MARKDOWN_HINT_RE.search(text):
        return {"text": text}
    placeholders: list[str] = []

    def stash(html_fragment: str) -> str:
        token = f"\x00PH{len(placeholders)}\x00"
        placeholders.append(html_fragment)
        return token

    working = _FENCE_RE.sub(
        lambda m: stash(f"<pre><code>{escape(m.group(1).rstrip(chr(10)))}</code></pre>"),
        text,
    )
    working = _INLINE_CODE_RE.sub(lambda m: stash(f"<code>{escape(m.group(1))}</code>"), working)
    escaped = escape(working)
    escaped = _HEADING_RE.sub(lambda m: f"<b>{m.group(1).strip()}</b>", escaped)
    escaped = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    for idx, fragment in enumerate(placeholders):
        escaped = escaped.replace(f"\x00PH{idx}\x00", fragment)
    if _utf16_units(escaped) > 4096:
        return {"text": text}
    return {"text": escaped, "parse_mode": "HTML"}
