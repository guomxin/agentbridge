"""Fixed, read-only data collection and bounded text for the OA daily digest."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
import sqlite3
import time
import unicodedata
from zoneinfo import ZoneInfo

from agentbridge.adapters.seeyon_central import SeeyonLoginRequired
from agentbridge.browser.read_budget import check_read_deadline, read_deadline_scope
from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.business_watches import BusinessWatchError
from agentbridge.core.capability_runtime import CapabilityRejected
from agentbridge.core.session_secrets import SessionSecretError

_PENDING = "oa.workflow.pending.list"
_TIMEZONE = ZoneInfo("Asia/Shanghai")
_WATCH_RULE_LABELS = {"nodes": "节点变化", "handlers": "处理人变化", "ended": "流程结束"}
_UNAVAILABLE = {
    "permission": "没有当前 OA 读取权限",
    "session": "OA 会话不可用，请到工作台检查登录状态",
    "principal": "本次无法确认 OA 账号身份",
    "timeout": "本次查询超时",
    "busy": "OA 会话正在处理其他操作",
    "source": "本次未能读取 OA 待办列表",
}
_PAYLOAD_BUDGET = 3500


@dataclass(frozen=True)
class DigestReport:
    text: str
    watch_coverage_complete: bool


class _Unavailable(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason


def _same_oa_session(service, subject, original, principal):
    current = service.sessions.find(user_subject=subject, system_id="oa")
    if not current or current["state"] != "active":
        raise _Unavailable("session")
    if principal_binding(current) != principal:
        raise _Unavailable("principal")
    if (current["session_id"] != original["session_id"]
            or current.get("authority_generation") != original.get("authority_generation")):
        raise _Unavailable("session")
    return current


def _read_pending(service, subject, session, principal):
    arguments = {"limit": 100}
    service.user_grants.require_capability(subject, _PENDING, arguments)
    with read_deadline_scope(time.monotonic() + 15), service._session_lock(
        session["session_id"], wait_seconds=0.05, purpose="oa_daily_digest",
    ):
        current = _same_oa_session(service, subject, session, principal)
        state = service.session_states.load(current["session_id"])
        if state is None:
            raise _Unavailable("session")
        adapter = service.adapter
        with ExitStack() as stack:
            worker = stack.enter_context(service.worker_factory(current, adapter))
            worker.restore_session_state(state)
            scope = getattr(adapter, "capability_scope", None)
            if callable(scope):
                stack.enter_context(scope(worker))
            probe = adapter.probe_business_draft_session(worker)
            if not probe.get("authenticated"):
                raise _Unavailable("session")
            if probe.get("observed_principal_ref") != current["expected_principal_ref"]:
                raise _Unavailable("principal")
            result = adapter.list_workflows(worker, collection="pending", arguments=arguments)
            check_read_deadline()
            _same_oa_session(service, subject, session, principal)
            service.user_grants.require_capability(subject, _PENDING, arguments)
    if not isinstance(result, dict) or result.get("collection") != "pending":
        raise _Unavailable("source")
    return result


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def _date(value):
    return value.astimezone(_TIMEZONE).strftime("%m月%d日 %H:%M")


def _text(value, maximum):
    if not isinstance(value, str):
        return ""
    clean = "".join(" " if unicodedata.category(char) in {"Cc", "Cf", "Cs"} else char for char in value)
    clean = re.sub(r"https?://[^\s<>]+", "［链接］", clean, flags=re.IGNORECASE)
    clean = " ".join(clean.split())
    return clean if len(clean) <= maximum else clean[:maximum - 1] + "…"


def _units(value):
    return len(value.encode("utf-16-le")) // 2


def _section(header, lines, budget):
    """Bound each section independently, retaining its count when rows are omitted."""
    output = [header]
    reserve = _units(f"\n另有 {len(lines)} 项未在此列出。")
    size = _units(header)
    for index, line in enumerate(lines):
        new_size = size + _units("\n" + line)
        if new_size + reserve > budget:
            output.append(f"另有 {len(lines) - index} 项未在此列出。")
            break
        output.append(line)
        size = new_size
    return "\n".join(output)


def _pending_section(result, reason):
    if reason:
        return f"本人 OA 待办｜{_UNAVAILABLE[reason]}。"
    items = result.get("items") or []
    coverage = result.get("coverage") or {}
    observed = _when(coverage.get("observedAt"))
    suffix = f"（{_date(observed)}查询）" if observed else ""
    if coverage.get("status") == "complete" and coverage.get("hasMore") is not True:
        header = f"本人 OA 待办｜当前 {len(items)} 项{suffix}"
    else:
        total = result.get("total")
        listed_total = f"，OA 显示共 {total} 项" if type(total) is int and total >= len(items) else ""
        header = f"本人 OA 待办｜已读到 {len(items)} 项{listed_total}{suffix}，覆盖不完整"
    lines = ["• " + (_text(item.get("title"), 110) or "未显示标题") for item in items if isinstance(item, dict)]
    return _section(header, lines, 1350)


def _watch_line(watch, events, now):
    title = _text(watch.get("title"), 105) or "未显示标题"
    snapshot = watch.get("snapshot") or {}
    verified = _when(snapshot.get("observed_at")) or _when(watch.get("last_source_observed_at"))
    state = snapshot.get("workflow_state")
    status = "已结束" if state == "ended" else "流转中" if state == "in_progress" else "状态待核实"
    if verified:
        status += f"（{_date(verified)}核验）"
    next_check = _when(watch.get("next_check_at"))
    overdue = next_check is not None and next_check + timedelta(minutes=1) < now
    stale = (watch.get("background_paused") or watch.get("last_check_status") != "verified"
             or not verified or verified > now or overdue)
    if stale and watch.get("state") == "active":
        status += "，当前核验未完成"
    rules = set(watch.get("event_types") or [])
    changed = {rule for event in events for rule in event["change_types"] if rule in rules}
    details = []
    if changed:
        labels = "、".join(label for rule, label in _WATCH_RULE_LABELS.items() if rule in changed)
        details.append(f"{len(events)} 次变化：{labels}")
    if state == "in_progress" and "nodes" in rules:
        names = [_text(node.get("name"), 32) for node in (snapshot.get("current_nodes") or [])[:3]]
        if any(names):
            details.append("节点：" + "、".join(name for name in names if name))
    if state == "in_progress" and "handlers" in rules:
        names = [_text(name, 24) for name in (snapshot.get("current_handlers") or [])[:3]]
        if any(names):
            details.append("处理人：" + "、".join(name for name in names if name))
    return "• " + "｜".join([title, status, *details]), bool(stale and watch.get("state") == "active")


def _watch_history(watches, subject, watch, since, now):
    # The existing history getter validates ownership and the OA principal.
    if not watch.get("change_seq"):
        return watch, [], True
    detail = watches.get(subject, watch["watch_id"])
    current = detail["watch"]
    history = detail["events"]
    rules = set(current.get("event_types") or [])
    events = []
    observed_times = []
    previous_seq = None
    for event in history:
        seq = event.get("sequence")
        if type(seq) is not int or (previous_seq is not None and seq != previous_seq - 1):
            return current, events, False
        previous_seq = seq
        recorded_at = _when(event.get("created_at"))
        if recorded_at is None:
            return current, events, False
        observed_times.append(recorded_at)
        if since < recorded_at <= now and rules.intersection(event.get("change_types") or []):
            events.append(event)
    complete = (len(history) == current.get("change_seq")
                or bool(observed_times and observed_times[-1] <= since))
    if history and history[0].get("sequence") != current.get("change_seq"):
        complete = False
    return current, events, complete


def _watch_section(service, subject, since, now):
    watches = service.business_watch
    if since >= now:
        return "关注事项｜暂无可比较的进展时间范围。", False
    coverage = True
    changed_lines, unchanged_lines = [], []
    change_count, active_count, stale_count = 0, 0, 0
    cursor = None
    seen = set()
    for _ in range(5):
        page = watches.list(subject, cursor=cursor, limit=100)
        for watch in page["items"]:
            watch_id = watch["watch_id"]
            if watch_id in seen:
                coverage = False
                continue
            seen.add(watch_id)
            if watch["state"] not in {"active", "completed"}:
                continue
            try:
                watch, events, complete = _watch_history(watches, subject, watch, since, now)
            except KeyError:
                coverage = False
                continue
            if watch["state"] not in {"active", "completed"}:
                continue
            coverage = coverage and complete
            if watch["state"] == "active":
                active_count += 1
            if not events and watch["state"] == "completed":
                continue
            line, stale = _watch_line(watch, events, now)
            stale_count += int(stale)
            if events:
                changed_lines.append(line)
                change_count += len(events)
            else:
                unchanged_lines.append(line)
        if not page["hasMore"]:
            break
        next_cursor = page["nextCursor"]
        if not next_cursor or next_cursor == cursor:
            coverage = False
            break
        cursor = next_cursor
    else:
        coverage = False
    header = f"关注事项｜{active_count} 项关注中，本期记录到 {change_count} 次变化"
    if stale_count:
        header += f"，{stale_count} 项当前核验未完成"
    if not coverage:
        header += "，关注记录覆盖不完整"
    if not active_count and not change_count and coverage:
        header += "。本期没有可汇总的关注记录。"
    return _section(header, changed_lines + unchanged_lines, 1910), coverage


def collect_oa_daily_digest(service, *, user_subject, now: datetime, since: datetime, trial: bool) -> DigestReport:
    """Collect under the existing OA identity and read grants without refreshing activity."""
    if now.utcoffset() is None or since.utcoffset() is None or since > now:
        raise ValueError("digest requires an ordered timezone-aware reporting window")
    title = "OA 每日简报（测试）" if trial else "OA 每日简报"
    header = f"{title}\n{_date(now)}（北京时间）\n关注变化范围：{_date(since)} 至 {_date(now)}"
    result = None
    reason = None
    watch_reason = None
    watch_coverage = False
    session = service.sessions.find(user_subject=user_subject, system_id="oa")
    principal = principal_binding(session) if session else None
    if not session or session["state"] != "active":
        reason = "session"
    elif not principal:
        reason = "principal"
    else:
        try:
            result = _read_pending(service, user_subject, session, principal)
        except _Unavailable as exc:
            reason = exc.reason
        except (SeeyonLoginRequired, SessionSecretError):
            reason = "session"
        except PermissionError:
            reason = "permission"
        except TimeoutError:
            reason = "timeout"
        except CapabilityRejected as exc:
            reason = "busy" if exc.code == "SESSION_BUSY" else "source"
        except RuntimeError:
            reason = "source"
    if reason in {"session", "principal", "permission"}:
        watch_reason = _UNAVAILABLE[reason]
    else:
        try:
            watch_text, watch_coverage = _watch_section(service, user_subject, since, now)
            service.business_watch.require_permission(user_subject)
        except PermissionError:
            watch_reason = "没有当前关注读取权限"
        except (BusinessWatchError, sqlite3.OperationalError) as exc:
            watch_reason = {
                "WATCH_SESSION_UNAVAILABLE": _UNAVAILABLE["session"],
                "WATCH_PRINCIPAL_CHANGED": _UNAVAILABLE["principal"],
            }.get(getattr(exc, "code", None), "本次无法读取关注记录")
    if session and principal:
        try:
            _same_oa_session(service, user_subject, session, principal)
        except _Unavailable as exc:
            reason, watch_reason = exc.reason, _UNAVAILABLE[exc.reason]
    if reason is None:
        try:
            service.user_grants.require_capability(user_subject, _PENDING, {"limit": 100})
        except PermissionError:
            reason = "permission"
    if watch_reason:
        watch_text = f"关注事项｜{watch_reason}。"
        watch_coverage = False
    text = "\n\n".join((header, _pending_section(result, reason), watch_text))
    if _units(text) > _PAYLOAD_BUDGET:
        raise ValueError("digest exceeded its delivery budget")
    return DigestReport(text, watch_coverage)


def build_oa_daily_digest(service, *, user_subject, now: datetime, since: datetime, trial: bool) -> str:
    return collect_oa_daily_digest(service, user_subject=user_subject, now=now, since=since, trial=trial).text
