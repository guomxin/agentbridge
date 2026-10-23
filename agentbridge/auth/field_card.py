from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from math import ceil
import re
import secrets
from typing import Any
from urllib.parse import parse_qs

from agentbridge.auth.card import AuthCardResponse, MAX_AUTH_BODY_BYTES
from agentbridge.auth.embedded import EMBEDDED_SAFE_AREA_CSS, render_embedded_web_app_bridge
from agentbridge.auth.reading_content import project_review, render_reading
from agentbridge.core.field_values import _field_definitions, _normalize_submission
from agentbridge.core.field_submissions import (
    FieldSubmissionAccessDenied,
    FieldSubmissionIntegrityError,
    FieldSubmissionNotFound,
    FieldSubmissionStateError,
    FieldSubmissionStore,
)


class TrustedFieldApplication:
    def __init__(self, *, submission_store: FieldSubmissionStore) -> None:
        self.submission_store = submission_store

    def get_card(
        self,
        submission_id: str,
        *,
        secure_cookie: bool,
        presentation_id: str | None = None,
    ) -> AuthCardResponse:
        try:
            submission = self.submission_store.get(submission_id)
            if presentation_id is not None:
                self.submission_store.get_presentation(
                    submission_id,
                    presentation_id,
                )
        except FieldSubmissionNotFound:
            return self._message_response(
                status=404,
                title="字段填写卡不存在",
                message="请从智能体重新发起填写。",
                tone="error",
            )
        except FieldSubmissionIntegrityError:
            return self._message_response(
                status=409,
                title="字段填写卡校验失败",
                message="请从智能体重新发起填写。",
                tone="error",
            )
        state = submission["state"]
        if state == "pending":
            csrf_token = self.submission_store.issue_csrf(
                submission_id,
                presentation_id=presentation_id,
            )
            nonce = secrets.token_urlsafe(18)
            body = _render_form(
                submission,
                csrf_token=csrf_token,
                nonce=nonce,
                values={},
                error=None,
                presentation_id=presentation_id,
            )
            cookie_path = (
                f"/input/{submission_id}/present/{presentation_id}"
                if presentation_id is not None
                else f"/input/{submission_id}"
            )
            cookie = (
                f"agentbridge_csrf={csrf_token}; Path={cookie_path}; "
                f"HttpOnly; SameSite=Strict; Max-Age={_ttl_seconds(submission)}"
            )
            if secure_cookie:
                cookie += "; Secure"
            headers = _security_headers(nonce)
            headers["Set-Cookie"] = cookie
            return AuthCardResponse(200, headers, body.encode("utf-8"))
        if state == "submitted":
            return self._message_response(
                status=200,
                title="字段已提交",
                message="字段已经安全提交。请返回智能体继续；系统仍会另行请求写操作授权。",
                tone="success",
            )
        if state == "consumed":
            return self._message_response(
                status=200,
                title="字段已用于生成计划",
                message="请在独立的操作授权卡中确认最终计划。",
                tone="success",
            )
        return self._message_response(
            status=410,
            title="字段填写卡已失效",
            message="请返回智能体重新发起填写。",
            tone="error",
        )

    def submit_card(
        self,
        submission_id: str,
        *,
        body: bytes,
        content_type: str,
        csrf_cookie: str,
        presentation_id: str | None = None,
    ) -> AuthCardResponse:
        if len(body) > MAX_AUTH_BODY_BYTES:
            return self._message_response(
                status=413,
                title="请求过大",
                message="字段提交已被拒绝。",
                tone="error",
            )
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return self._message_response(
                status=415,
                title="请求格式不支持",
                message="字段提交已被拒绝。",
                tone="error",
            )
        try:
            submission = self.submission_store.get(submission_id)
        except FieldSubmissionNotFound:
            return self._message_response(
                status=404,
                title="字段填写卡不存在",
                message="请从智能体重新发起填写。",
                tone="error",
            )
        except FieldSubmissionIntegrityError:
            return self._message_response(
                status=409,
                title="字段填写卡校验失败",
                message="请从智能体重新发起填写。",
                tone="error",
            )
        if submission["state"] != "pending":
            return self._message_response(
                status=409,
                title="字段填写卡已被使用",
                message="请检查操作状态或重新发起填写。",
                tone="error",
            )

        schema = submission.get("form_schema")
        try:
            definitions = _field_definitions(schema)
        except ValueError:
            return self._message_response(
                status=409,
                title="字段定义无效",
                message="请从智能体重新发起填写。",
                tone="error",
            )

        payload = bytearray(body)
        parsed: dict[str, list[str]] = {}
        raw_values: dict[str, str] = {}
        try:
            try:
                parsed = parse_qs(
                    payload.decode("utf-8"),
                    keep_blank_values=True,
                    max_num_fields=max(32, len(definitions) + 1),
                    strict_parsing=True,
                )
            except (UnicodeDecodeError, ValueError):
                return self._message_response(
                    status=400,
                    title="请求格式错误",
                    message="字段提交已被拒绝。",
                    tone="error",
                )
            expected_names = {item["name"] for item in definitions} | {"csrf_token"}
            if set(parsed) != expected_names or any(len(values) != 1 for values in parsed.values()):
                return self._message_response(
                    status=400,
                    title="填写字段不匹配",
                    message="请从智能体重新打开字段填写卡。",
                    tone="error",
                )
            raw_values = {item["name"]: parsed[item["name"]][0] for item in definitions}
            csrf_token = parsed["csrf_token"][0]
            try:
                normalized = _normalize_submission(schema, raw_values)
            except ValueError as exc:
                nonce = secrets.token_urlsafe(18)
                page = _render_form(
                    submission,
                    csrf_token=csrf_token,
                    nonce=nonce,
                    values=raw_values,
                    error=str(exc),
                    presentation_id=presentation_id,
                )
                return AuthCardResponse(400, _security_headers(nonce), page.encode("utf-8"))
            try:
                self.submission_store.submit(
                    submission_id,
                    csrf_token=csrf_token,
                    csrf_cookie=csrf_cookie,
                    values=normalized,
                    presentation_id=presentation_id,
                )
            except FieldSubmissionAccessDenied:
                return self._message_response(
                    status=403,
                    title="字段提交校验失败",
                    message="请从智能体重新打开字段填写卡。",
                    tone="error",
                )
            except FieldSubmissionStateError:
                return self._message_response(
                    status=409,
                    title="字段填写卡已被使用",
                    message="请检查操作状态或重新发起填写。",
                    tone="error",
                )
            except FieldSubmissionIntegrityError:
                return self._message_response(
                    status=409,
                    title="字段填写卡校验失败",
                    message="请从智能体重新发起填写。",
                    tone="error",
                )
            return self._message_response(
                status=200,
                title="字段已提交",
                message="字段已经安全提交。请返回智能体继续；系统仍会另行请求写操作授权。",
                tone="success",
            )
        finally:
            for index in range(len(payload)):
                payload[index] = 0
            parsed.clear()
            raw_values.clear()

    @staticmethod
    def _message_response(
        *,
        status: int,
        title: str,
        message: str,
        tone: str,
    ) -> AuthCardResponse:
        nonce = secrets.token_urlsafe(18)
        body = _document(
            title=title,
            nonce=nonce,
            close_when_complete=tone == "success",
            body=f"""
            <main class="shell">
              <section class="card status-card {escape(tone)}" aria-labelledby="status-title">
                <div class="status-mark" aria-hidden="true"></div>
                <p class="eyebrow">AGENTBRIDGE TRUSTED INPUT</p>
                <h1 id="status-title">{escape(title)}</h1>
                <p class="status-copy">{escape(message)}</p>
              </section>
            </main>
            """,
        )
        return AuthCardResponse(status, _security_headers(nonce), body.encode("utf-8"))




def _render_form(
    submission: dict,
    *,
    csrf_token: str,
    nonce: str,
    values: dict[str, str],
    error: str | None,
    presentation_id: str | None,
) -> str:
    schema = submission["form_schema"]
    controls = "".join(
        _render_control(item, _render_value(item, values))
        for item in _field_definitions(schema)
    )
    error_html = (
        f'<p class="form-error" role="alert">{escape(error)}</p>' if error else ""
    )
    review_html = _render_review(schema.get("review"))
    return _document(
        title=str(schema.get("title") or "填写操作字段"),
        nonce=nonce,
        body=f"""
        <main class="shell">
          <section class="card" aria-labelledby="card-title">
            <header class="card-header">
              <div class="brand-mark" aria-hidden="true"><span></span></div>
              <div>
                <p class="eyebrow">AGENTBRIDGE TRUSTED INPUT</p>
                <h1 id="card-title">{escape(str(schema.get('title') or '填写操作字段'))}</h1>
                <p class="system-name">{escape(str(schema.get('system') or '目标系统'))}</p>
              </div>
            </header>
            {error_html}
            {review_html}
            <form method="post" action="{_form_action(submission['submission_id'], presentation_id)}">
              <input type="hidden" name="csrf_token" value="{escape(csrf_token)}">
              <div class="form-grid">{controls}</div>
              <p class="notice">{escape(str(schema.get('notice') or '字段提交后仍需单独确认操作计划。'))}</p>
              <button type="submit" class="primary">{escape(str(schema.get('submit_label') or '提交字段'))}</button>
            </form>
            <footer>填写卡将在 {_format_expiry(submission['expires_at'])} 失效，且只能提交一次</footer>
          </section>
        </main>
        """,
    )


def _review_sections(fields: list) -> tuple[list[dict], dict[str, list[dict]]]:
    """Group known detail-row labels without interpreting or changing business data."""
    clean = []
    for item in fields:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        field_value = str(item["value"] if item.get("value") is not None else "").strip()
        if label and field_value:
            clean.append({"label": label, "value": field_value,
                          **({"reading": item["reading"]} if "reading" in item else {})})
    key_labels = {"会议主题", "地点", "回复人", "当前回执", "参会意向", "回复说明", "事项", "申请人", "发起人", "姓名", "请假类型", "开始时间", "结束时间",
                  "请假天数", "请假小时", "本次操作", "应付金额合计", "员工与月份", "移交人", "交接日期",
                  "事由", "出差事由", "加班事由", "OA 当前确认结论", "日期", "发起日期"}
    overview, groups = [], {}
    for item in clean:
        label, content = item["label"], item["value"]
        match = re.match(r"^(航段\d+|工作明细\d+|财务明细\d+)\s*·", label)
        if match:
            groups.setdefault(match[1], []).append(item)
        elif label in key_labels:
            if len(content) > 180 or content.count("\n") >= 3:
                overview.append({"label": label, "value": content[:160].replace("\n", " ") + "…（全文见下方）"})
                groups.setdefault(label + " · 全文", []).append(item)
            else:
                overview.append(item)
        elif len(content) > 180 or content.count("\n") >= 3:
            groups.setdefault(label, []).append(item)
        else:
            groups.setdefault("其他申请信息", []).append(item)
    if not overview and "其他申请信息" in groups:
        overview = groups["其他申请信息"][:6]
        groups["其他申请信息"] = groups["其他申请信息"][6:]
    return overview, {name: items for name, items in groups.items() if items}


def _render_review(value: Any, *, element_id: str = "review-title") -> str:
    if not isinstance(value, dict):
        return ""
    value = project_review(value)
    fields = value.get("fields")
    overview, groups = _review_sections(fields if isinstance(fields, list) else [])
    for group in value.get("groups", []) if isinstance(value.get("groups"), list) else []:
        if not isinstance(group, dict):
            continue
        first, rest = _review_sections(group.get("fields") if isinstance(group.get("fields"), list) else [])
        grouped_fields = first + [item for items in rest.values() for item in items]
        if grouped_fields:
            groups.setdefault(str(group.get("title") or "申请明细"), []).extend(grouped_fields)
    def rows(items):
        def content(item):
            reading = item.get("reading")
            # Existing compact facts already preserve whitespace through dd's
            # style. Avoid adding layout wrappers for unchanged plain values.
            if (isinstance(reading, dict) and not reading.get("notices") and
                    reading.get("nodes") == [{"tag": "pre", "children": [item["value"]]}]):
                return escape(item["value"])
            return render_reading(reading) or escape(item["value"])
        return "".join(f'<div class="review-row"><dt>{escape(item["label"])}</dt>'
                       f'<dd>{content(item)}</dd></div>' for item in items)
    grouped = "".join(f'<details class="review-group"><summary>{escape(name)}'
                      f'<span> · {len(items)} 项</span></summary><dl>{rows(items)}</dl></details>'
                      for name, items in groups.items())
    materials = []
    for section in value.get("sections", []) if isinstance(value.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        body = ""
        if section.get("kind") == "text":
            body = render_reading(section.get("reading")) or escape(str(section.get("text") or ""))
        elif section.get("kind") == "facts":
            body = '<dl>' + rows([item for item in section.get("fields", [])
                                  if isinstance(item, dict) and "label" in item and "value" in item]) + '</dl>'
        elif section.get("kind") == "table":
            columns = [column for column in section.get("columns", [])
                       if isinstance(column, dict) and isinstance(column.get("key"), str)]
            head = '<tr>' + ''.join(f'<th scope="col">{escape(str(column.get("label") or column["key"]))}</th>'
                                    for column in columns) + '</tr>'
            table_rows = []
            for row in section.get("rows", []) if isinstance(section.get("rows"), list) else []:
                if not isinstance(row, dict) or not isinstance(row.get("cells"), dict):
                    continue
                readings = row.get("readings") if isinstance(row.get("readings"), dict) else {}
                table_rows.append('<tr>' + ''.join('<td>' + (render_reading(readings.get(column["key"])) or
                    escape(str(row["cells"].get(column["key"]) or ""))) + '</td>' for column in columns) + '</tr>')
            body = '<div class="reading-table" tabindex="0"><table><thead>' + head + '</thead><tbody>' + ''.join(table_rows) + '</tbody></table></div>'
        if body:
            materials.append('<details class="review-group"><summary>' + escape(str(section.get("title") or "原始材料")) +
                             '</summary>' + body + '</details>')
    attachments = value.get("attachments")
    if isinstance(attachments, list) and attachments:
        materials.append('<section class="material-attachments"><h3>附件名称</h3><ul>' + ''.join(
            '<li>' + escape(str(item.get("name") or "未命名附件")) + '</li>' for item in attachments if isinstance(item, dict)) +
            '</ul><p>此处仅列出源页面中的附件名称，附件内容未在本页读取。</p></section>')
    if not overview and not groups and not materials:
        return ""
    title = escape(str(value.get("title") or "审批详情"))
    effect = str(value.get("effect") or "").strip()
    effect_html = (
        f'<p class="review-effect">{escape(effect)}</p>' if effect else ""
    )
    return (
        f'<section class="review" aria-labelledby="{escape(element_id)}">'
        '<p class="review-kicker">审批依据</p>'
        f'<h2 id="{escape(element_id)}">{title}</h2>{effect_html}'
        f'<dl class="review-key-facts">{rows(overview)}</dl>{grouped}{"".join(materials)}</section>'
    )


def _form_action(submission_id: str, presentation_id: str | None) -> str:
    base = f"/input/{escape(submission_id)}"
    if presentation_id is None:
        return base
    return f"{base}/present/{escape(presentation_id)}"


def _render_value(item: dict[str, Any], values: dict[str, str]) -> str:
    name = item["name"]
    if name in values:
        return values[name]
    value = item.get("value")
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _render_control(item: dict[str, Any], value: str) -> str:
    name = escape(item["name"])
    label = escape(str(item.get("label") or item["name"]))
    control = item["control"]
    required = " required" if item.get("required") else ""
    wide = " wide" if control in {"textarea", "segmented"} else ""
    if control == "textarea":
        rows = max(2, min(8, int(item.get("rows") or 4)))
        maximum = _maximum_attribute(item)
        return (
            f'<div class="field{wide}"><label for="{name}">{label}</label>'
            f'<textarea id="{name}" name="{name}" rows="{rows}"{maximum}{required}>'
            f"{escape(value)}</textarea></div>"
        )
    if control == "select":
        options = ['<option value="">请选择</option>']
        for option in item.get("options") or []:
            option_value = str(option["value"])
            selected = " selected" if value == option_value else ""
            options.append(
                f'<option value="{escape(option_value)}"{selected}>'
                f"{escape(str(option.get('label') or option_value))}</option>"
            )
        return (
            f'<div class="field{wide}"><label for="{name}">{label}</label>'
            f'<select id="{name}" name="{name}"{required}>{"".join(options)}</select></div>'
        )
    if control == "segmented":
        options = []
        for index, option in enumerate(item.get("options") or []):
            option_value = str(option["value"])
            checked = " checked" if value == option_value else ""
            option_id = f"{name}_{index}"
            options.append(
                f'<label for="{option_id}"><input type="radio" id="{option_id}" '
                f'name="{name}" value="{escape(option_value)}"{checked}{required}>'
                f'<span>{escape(str(option.get("label") or option_value))}</span></label>'
            )
        return (
            f'<fieldset id="{name}" class="field segmented{wide}"><legend>{label}</legend>'
            f'<div class="segments">{"".join(options)}</div></fieldset>'
        )
    if control == "date":
        return (
            f'<div class="field{wide}"><label for="{name}">{label}</label>'
            f'<input type="date" id="{name}" name="{name}" '
            f'value="{escape(value)}"{required}></div>'
        )
    if control == "datetime-local":
        html_value = value.replace(" ", "T", 1)
        return (
            f'<div class="field{wide}"><label for="{name}">{label}</label>'
            f'<input type="datetime-local" id="{name}" name="{name}" '
            f'value="{escape(html_value)}" step="60"{required}></div>'
        )
    if control == "number":
        minimum = _numeric_attribute("min", item.get("minimum"))
        maximum = _numeric_attribute("max", item.get("maximum"))
        step = _numeric_attribute("step", item.get("step") or "any")
        return (
            f'<div class="field{wide}"><label for="{name}">{label}</label>'
            f'<input type="number" id="{name}" name="{name}" value="{escape(value)}"'
            f"{minimum}{maximum}{step}{required}></div>"
        )
    autocomplete = escape(str(item.get("autocomplete") or "off"))
    maximum = _maximum_attribute(item)
    return (
        f'<div class="field{wide}"><label for="{name}">{label}</label>'
        f'<input type="text" id="{name}" name="{name}" value="{escape(value)}" '
        f'autocomplete="{autocomplete}"{maximum}{required}></div>'
    )


def _maximum_attribute(item: dict[str, Any]) -> str:
    maximum = item.get("max_length")
    return f' maxlength="{int(maximum)}"' if maximum is not None else ""


def _numeric_attribute(name: str, value: Any) -> str:
    return f' {name}="{escape(str(value))}"' if value is not None else ""


def _document(
    *,
    title: str,
    nonce: str,
    body: str,
    close_when_complete: bool = False,
) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>{escape(title)} · AgentBridge</title>
  <style nonce="{nonce}">
    :root {{ color-scheme:light; --paper:#f3f5f2; --surface:#fff; --ink:#17201d;
      --muted:#62706a; --line:#d5dbd7; --teal:#087d72; --teal-dark:#075f58;
      --amber:#9b6517; --red:#b23a3a; --button-ink:#fff; --button-hover:#075f58;
      --field-bg:#fff; --field-ink:#17201d; --field-label:#35423d; --field-border:#899b91;
      --subtle-bg:#f1f5f1; --subtle-ink:#17201d; --subtle-muted:#62706a; --table-bg:#edf3ef;
      --selected-bg:#e5f4f1; --selected-ink:#075f58; --selected-border:#087d72;
      --warning-bg:#fff8ea; --warning-ink:#684a19; --warning-border:#b08038;
      --error-bg:#fff0ef; --error-ink:#7f2828; --disabled-bg:#e8ede9; --disabled-ink:#56655d;
      --secondary-bg:#edf1ee; --secondary-ink:#17201d; --focus-ring:#087d72; }}
    * {{ box-sizing:border-box; }} html,body {{ min-height:100%; }}
    body {{ margin:0; background:var(--paper); color:var(--ink);
      font-family:"Microsoft YaHei UI","Noto Sans CJK SC",sans-serif; letter-spacing:0; }}
    body::before {{ content:""; position:fixed; inset:0 0 auto; height:6px;
      background:linear-gradient(90deg,var(--teal) 0 68%,var(--amber) 68% 100%); }}
    .shell {{ min-height:100vh; display:flex; align-items:center; justify-content:center;
      padding:32px 18px; }}
    .card {{ width:660px; max-width:100%; background:var(--surface); border:1px solid var(--line);
      border-radius:8px; box-shadow:0 18px 45px rgba(23,32,29,.10); padding:30px; }}
    .card-header {{ display:flex; align-items:center; gap:16px; margin-bottom:24px; }}
    .brand-mark {{ width:46px; height:46px; border:2px solid var(--teal); display:grid;
      place-items:center; flex:0 0 auto; }}
    .brand-mark span {{ width:17px; height:17px; border:4px solid var(--teal); transform:rotate(45deg); }}
    .eyebrow {{ margin:0 0 5px; color:var(--teal-dark); font-size:11px; font-weight:700; }}
    h1 {{ margin:0; font-size:24px; line-height:1.3; }}
    .system-name {{ margin:5px 0 0; color:var(--muted); font-size:12px; }}
    .review {{ margin:0 0 24px; padding:18px; border:1px solid var(--line);
      border-radius:6px; background:var(--subtle-bg);color:var(--subtle-ink);--ink:var(--subtle-ink);--muted:var(--subtle-muted); }}
    .review-kicker {{ margin:0 0 5px; color:var(--teal-dark); font-size:11px; font-weight:700; }}
    .review h2 {{ margin:0; font-size:18px; line-height:1.45; }}
    .review-effect {{ margin:7px 0 0; color:var(--muted); font-size:13px; line-height:1.55; }}
    .review dl {{ display:grid; grid-template-columns:1fr 1fr; gap:0 18px; margin:16px 0 0; }}
    .review-row {{ min-width:0; padding:10px 0; border-top:1px solid var(--line); }}
    .review dt {{ margin:0 0 4px; color:var(--muted); font-size:11px; font-weight:700; }}
    .review dd {{ margin:0; color:var(--ink); font-size:14px; line-height:1.55;
      overflow-wrap:anywhere; white-space:pre-wrap; }}
    .review-group {{ margin-top:14px; border-top:1px solid var(--line); padding-top:12px; }}
    .review-group summary {{ cursor:pointer; font-size:13px; font-weight:700; line-height:1.5; }}
    .review-group summary span {{ color:var(--muted); font-weight:400; }}
    .review-group dl {{ grid-template-columns:1fr; margin-top:6px; }}
    .review-group .review-row {{ border-top:0; }}
    .material-reading {{white-space:normal;line-height:1.7;overflow-wrap:anywhere;min-width:0;margin-top:8px}}
    .material-reading p {{margin:8px 0}} .material-reading ul,.material-reading ol {{padding-left:24px;margin:8px 0}}
    .material-reading blockquote {{margin:10px 0;padding-left:12px;border-left:3px solid var(--line);color:var(--muted)}}
    .material-reading pre {{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;margin:0;line-height:1.7}}
    .reading-table {{overflow-x:auto;max-width:100%;margin:10px 0}}
    .reading-table table {{border-collapse:collapse;width:100%;font-size:13px}}
    .reading-table th,.reading-table td {{border:1px solid var(--line);padding:8px;vertical-align:top;text-align:left;min-width:80px;white-space:pre-wrap}}
    .reading-table th {{background:var(--table-bg);color:var(--subtle-ink)}} .reading-notice {{color:var(--muted);font-size:12px;line-height:1.6;margin:8px 0}}
    .form-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:17px 16px; }}
    .field {{ min-width:0; }} .field.wide {{ grid-column:1 / -1; }}
    label,legend {{ display:block; margin:0 0 7px; color:var(--field-label); font-size:13px; font-weight:700; }}
    input,select,textarea {{ width:100%; min-height:44px; border:1px solid var(--field-border);
      border-radius:5px; background:var(--field-bg); color:var(--field-ink); padding:9px 11px; font:inherit; font-size:14px; }}
    textarea {{ min-height:96px; resize:vertical; line-height:1.55; }}
    input:focus,select:focus,textarea:focus {{ outline:3px solid var(--focus-ring); border-color:var(--teal); }}
    fieldset {{ margin:0; padding:0; border:0; }}
    .segments {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0; }}
    .segments label {{ margin:0; cursor:pointer; }}
    .segments input {{ position:absolute; width:1px; height:1px; min-height:0; margin:0;
      opacity:0; pointer-events:none; }}
    .segments span {{ display:grid; place-items:center; min-height:44px; border:1px solid var(--field-border);
      background:var(--field-bg);color:var(--field-ink); font-size:14px; }}
    .segments label:first-child span {{ border-radius:5px 0 0 5px; }}
    .segments label:last-child span {{ border-radius:0 5px 5px 0; border-left:0; }}
    .segments input:checked + span {{ background:var(--selected-bg); border-color:var(--selected-border); color:var(--selected-ink); }}
    .segments input:disabled + span {{background:var(--disabled-bg);color:var(--disabled-ink);cursor:not-allowed}}
    .segments input:focus-visible + span {{ outline:3px solid var(--focus-ring); outline-offset:2px; }}
    .notice {{ margin:20px 0 16px; padding:12px; border-left:4px solid var(--amber); background:var(--warning-bg);
      color:var(--warning-ink); font-size:13px; line-height:1.6; }}
    .form-error {{ margin:0 0 18px; padding:11px 12px; border-left:4px solid var(--red);
      background:var(--error-bg); color:var(--error-ink); font-size:13px; }}
    button {{ width:100%; min-height:48px; border:0; border-radius:5px; padding:10px 16px;
      background:var(--teal); color:var(--button-ink); font:inherit; font-weight:700; cursor:pointer; }}
    button:hover:not(:disabled) {{ background:var(--button-hover); }}
    button:disabled,input:disabled,select:disabled,textarea:disabled {{background:var(--disabled-bg);color:var(--disabled-ink);cursor:not-allowed;opacity:1}}
    button.secondary {{background:var(--secondary-bg);color:var(--secondary-ink);border:1px solid var(--line)}}
    button.secondary:disabled {{background:var(--disabled-bg);color:var(--disabled-ink)}}
    button.secondary:hover:not(:disabled) {{background:var(--selected-bg);color:var(--selected-ink)}}
    a {{color:var(--teal-dark)}}
    footer {{ margin-top:18px; color:var(--muted); font-size:11px; text-align:center; }}
    .status-card {{ text-align:center; padding-block:44px; }}
    .status-mark {{ width:24px; height:24px; margin:0 auto 20px; border:5px solid var(--teal); transform:rotate(45deg); }}
    .status-card.error .status-mark {{ border-color:var(--red); }}
    .status-copy {{ margin:16px auto 0; max-width:380px; color:var(--muted); line-height:1.7; font-size:14px; }}
    @media (max-width:620px) {{ .shell {{ align-items:flex-start; padding:20px 10px; }}
      .card {{ padding:24px 18px; box-shadow:none; }} h1 {{ font-size:21px; }}
      .form-grid,.review dl {{ grid-template-columns:1fr; }} .field.wide {{ grid-column:auto; }}
      input:not([type="radio"]):not([type="checkbox"]),select,textarea {{ font-size:16px; }}
      .review-group summary {{ min-height:44px; padding-block:10px; }} }}
    {EMBEDDED_SAFE_AREA_CSS}
  </style>
</head>
<body>{body}{render_embedded_web_app_bridge(nonce=nonce, close_when_complete=close_when_complete)}</body>
</html>"""


def _security_headers(nonce: str) -> dict[str, str]:
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }


def _ttl_seconds(submission: dict) -> int:
    try:
        created = datetime.fromisoformat(submission["created_at"])
        expires = datetime.fromisoformat(submission["expires_at"])
        return max(1, min(1800, ceil((expires - created).total_seconds())))
    except (KeyError, TypeError, ValueError):
        return 900


def _format_expiry(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return escape(str(value))
    return escape(parsed.strftime("%Y-%m-%d %H:%M"))
