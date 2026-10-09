"""Bounded, non-model feedback for a resumed read."""

import re


def _text(value: object, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def read_continuation_message(system_name: str, capability: str, response: dict) -> str:
    prefix = f"{system_name}登录已恢复，"
    if response.get("status") != "succeeded":
        code = _text((response.get("error") or {}).get("code") or response.get("status"), 100)
        return f"{prefix}但原查询未完成（错误码：{code}）。本次查询已停止，请重新发起；没有执行业务写入。"
    result = response.get("result")
    result = result if isinstance(result, dict) else {}
    labels = {"pending": "OA 待办", "done": "OA 已办", "sent": "OA 已发", "tracked": "OA 跟踪事项"}
    label = labels.get(result.get("collection"), "原查询")
    items = result.get("items")
    if isinstance(items, list):
        lines = [f"{prefix}已自动完成{label}，本次返回 {len(items)} 条："]
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name") or item.get("content") or "记录"
            details = [_text(item.get(key), 160) for key in ("date", "logDate", "fullname", "sender", "status")]
            if "hours" in item:
                details.append(f"{_text(item['hours'], 30)} 小时")
            metadata = " | ".join(value for value in details if value)
            block = f"{index}. {_text(title, 700)}" + (f"\n{metadata}" if metadata else "")
            if len("\n\n".join(lines + [block])) > 3300:
                lines.append(f"其余 {len(items) - index + 1} 条未在本条消息展开，可在任务进度查看完整结果。")
                break
            lines.append(block)
        coverage = result.get("coverage") or {}
        if coverage.get("hasMore") or coverage.get("status") in {"partial", "unknown"}:
            lines.append("本次结果未覆盖全部记录，不代表已查全。")
        return "\n\n".join(lines)
    if capability == "yuque.document.get":
        return f"{prefix}已继续读取《{_text((result.get('document') or {}).get('title'))}》。\n\n{_text(result.get('content'), 2800)}"
    return f"{prefix}原查询已完成，完整结构化结果可在任务进度查看。"
