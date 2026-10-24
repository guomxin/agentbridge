"""Trusted editing views for explicitly registered task-bound business drafts."""
from __future__ import annotations

from html import escape
import json
from pathlib import Path
import re
import secrets

from agentbridge.auth.card import AuthCardResponse, MAX_AUTH_BODY_BYTES
from agentbridge.auth.field_card import _document, _render_control, _render_review, _security_headers
from agentbridge.auth.reading_content import project_draft_state
from agentbridge.adapters.taihua import TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA
from agentbridge.adapters.seeyon_meeting_room_application import MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA
from agentbridge.adapters.seeyon_pending_actions import LEAVE_APPROVAL_FIELD_CARD_SCHEMA
from agentbridge.core.business_draft_profiles import PROFILES, profile_for_source


UI_PROFILES = {
    "taihua.work_log.create": {"prepare": "taihua.work_log.create.prepare", "viewKind": "work_log",
        "schema": TAIHUA_WORK_LOG_FIELD_CARD_SCHEMA, "title": "提交工作日志", "submitLabel": "确认并提交日志",
        "lead": "填写后会自动解析项目并检查重复日志。核对下方结果，再确认正式提交。"},
    "oa.meeting_room.application.create": {"prepare": "oa.meeting_room.application.prepare", "viewKind": "meeting_room",
        "schema": MEETING_ROOM_APPLICATION_FIELD_CARD_SCHEMA, "title": "申请会议室", "submitLabel": "确认并申请会议室",
        "lead": "调整时间后会重新查询空闲会议室。请明确选择房间，再核对本次申请。"},
    "oa.leave.approval": {"prepare": "oa.leave.approval.prepare", "viewKind": "approval",
        "schema": LEAVE_APPROVAL_FIELD_CARD_SCHEMA, "title": "审批请假申请", "submitLabel": "确认并审批通过",
        "lead": "先核对申请材料，再填写本次处理意见。确认后仅审批通过这条请假申请。"},
}
_SCRIPT = Path(__file__).with_name("static").joinpath("business_draft.mjs")
_BATCH_SCRIPT = Path(__file__).with_name("static").joinpath("batch_view.mjs")
_PDF_SCRIPT = Path(__file__).with_name("static").joinpath("pdf_preview.mjs")
_MATERIAL_SCRIPT = Path(__file__).with_name("static").joinpath("material_preview.mjs")

# Group only fields present in the frozen schema. These headings add no inputs
# and make draft-saving, submitting and reviewing distinct business operations.
FORM_GROUPS = {
    "work_log": (("日志内容", ("log_date", "hours", "project", "content")),),
    "meeting_reply": (("是否参加会议", ("attitude",)), ("本次回复", ("feedback",))),
    "meeting_room_application": (("申请用途", ("purpose",)), ("使用时段与会议室", ("start_time", "end_time", "room"))),
    "meeting": (("会议主题", ("subject",)), ("会议时段与会议室", ("start_time", "end_time", "room"))),
    "business_trip": (("出差行程", ("origin", "destination", "start_time", "end_time", "travel_mode")),
                      ("申请说明与流程设置", ("reason", "has_direct_supervisor"))),
    "leave": (("请假类型与时段", ("leave_type", "start_time", "end_time")),
              ("申请说明与流程设置", ("reason", "has_direct_supervisor"))),
    "missed_punch": (("补签时段", ("start_time", "end_time")), ("地点与补签说明", ("location", "reason_type", "explanation"))),
    "meeting_room_cancel": (("本次撤销原因", ("cancellation_reason",)),),
    "workflow_revoke": (("本次撤销说明", ("repeal_comment",)),),
    "alarm_remark": (("本次更新的备注", ("remark",)),),
}


def _group_controls(controls: dict[str, str], *, kind: str, review_kind: str) -> str:
    if kind == "approval":
        groups = (("本次处理意见", ("opinion",)),)
    else:
        groups = FORM_GROUPS.get(review_kind, (("填写办理内容", tuple(controls)),))
    remaining = dict(controls)
    rendered = []
    for title, names in groups:
        fields = [remaining.pop(name) for name in names if name in remaining]
        if fields:
            rendered.append(f'<section class="draft-fields"><h2>{escape(title)}</h2>'
                            f'<div class="form-grid">{"".join(fields)}</div></section>')
    if remaining:
        rendered.append('<div class="form-grid">' + "".join(remaining.values()) + '</div>')
    return "".join(rendered)


def business_draft_target(path: str) -> tuple[str, str | None, str] | None:
    if "?" in path and "/draft/content-" in path:
        return None
    match = re.fullmatch(
        r"/flow/input/([A-Za-z0-9_-]{32,128})(?:/present/([A-Za-z0-9_-]{32,128}))?"
        r"/draft/(state|values|options|preview|approve|content-restore|content-discard)", path.split("?", 1)[0],
    )
    return (match[1], match[2], match[3]) if match else None


class BusinessDraftApplication:
    def __init__(self, *, service=None, draft_service=None) -> None:
        self.service = service
        self._backend = draft_service

    @property
    def backend(self):
        if self._backend is None:
            from agentbridge.core.business_drafts import BusinessDraftService
            self._backend = BusinessDraftService(self.service)
        return self._backend

    @staticmethod
    def supports(context) -> bool:
        if context.kind != "input" or not context.task_id:
            return False
        from agentbridge.core.business_drafts import BusinessDraftError
        try:
            profile_for_source(context.resource)
            return True
        except (BusinessDraftError, KeyError, TypeError, ValueError):
            return False

    def has_decision(self, source_submission_id: str) -> bool:
        return self.backend.has_decision(source_submission_id=source_submission_id)

    def get_card(self, source_submission_id: str, *, presentation_id: str | None,
                 csrf_cookie: str, secure_cookie: bool, navigation: dict | None = None) -> AuthCardResponse:
        try:
            opened = self.backend.open(source_submission_id=source_submission_id,
                presentation_id=presentation_id, csrf_cookie=csrf_cookie)
            state, token = project_draft_state(opened["state"]), opened["csrfToken"]
            if navigation is not None:
                state["navigation"] = navigation
            path = _path(source_submission_id, presentation_id)
            nonce = secrets.token_urlsafe(18)
            headers = _security_headers(nonce)
            headers["Content-Security-Policy"] = headers["Content-Security-Policy"].replace(
                f"script-src 'nonce-{nonce}'", f"script-src 'nonce-{nonce}' 'strict-dynamic'")
            headers["Content-Security-Policy"] += "; connect-src 'self'; img-src blob:; font-src blob:; worker-src 'self'"
            headers["Set-Cookie"] = (
                f"agentbridge_csrf={token}; Path={path}; HttpOnly; SameSite=Strict; Max-Age={opened['maxAge']}"
                + ("; Secure" if secure_cookie else "")
            )
            return AuthCardResponse(200, headers, _render_page(state, token, path, nonce).encode())
        except Exception as error:
            response = self._error(error)
            nonce = secrets.token_urlsafe(18)
            message = json.loads(response.body)["error"]["message"]
            return AuthCardResponse(response.status, _security_headers(nonce), _document(
                title="办理页面不可用", nonce=nonce,
                body='<main class="shell"><section class="card status-card"><h1>办理页面不可用</h1>'
                     f'<p class="status-copy">{escape(message)}</p></section></main>',
            ).encode())

    def state(self, source_submission_id: str, *, presentation_id: str | None,
              token: str, csrf_cookie: str) -> AuthCardResponse:
        try:
            value = self.backend.state(source_submission_id=source_submission_id,
                presentation_id=presentation_id, csrf_token=token, csrf_cookie=csrf_cookie)
            return self.json(200, value)
        except Exception as error:
            return self._error(error)

    def mutate(self, source_submission_id: str, *, presentation_id: str | None,
               action: str, token: str, csrf_cookie: str, content_type: str,
               body: bytes) -> AuthCardResponse:
        if len(body) > MAX_AUTH_BODY_BYTES:
            return self.json(413, {"error": {"code": "BODY_TOO_LARGE", "message": "填写内容过大。"}})
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self.json(415, {"error": {"code": "CONTENT_TYPE", "message": "请求格式不支持。"}})
        try:
            payload = json.loads(body)
            expected_keys = {
                "values": {"expectedRevision", "values"},
                "preview": {"expectedRevision"},
                "options": {"expectedRevision"},
                "approve": {"expectedRevision", "previewId", "requestKey"},
                "content-restore": {"expectedRevision", "contentId", "contentVersion", "requestKey"},
                "content-discard": {"expectedRevision", "contentId", "contentVersion", "requestKey"},
            }.get(action)
            if (not isinstance(payload, dict) or expected_keys is None or set(payload) != expected_keys
                    or type(payload["expectedRevision"]) is not int or payload["expectedRevision"] < 0):
                raise ValueError("invalid request")
            arguments = dict(source_submission_id=source_submission_id, presentation_id=presentation_id,
                csrf_token=token, csrf_cookie=csrf_cookie, expected_revision=payload["expectedRevision"])
            if action == "values":
                supported_fields = [set(profile.fields) for profile in PROFILES]
                if (not isinstance(payload["values"], dict) or set(payload["values"]) not in supported_fields
                        or any(not isinstance(value, str) for value in payload["values"].values())):
                    raise ValueError("invalid values")
                value = self.backend.save(**arguments, values=payload["values"])
            elif action == "preview":
                value = self.backend.preview(**arguments)
            elif action == "options":
                value = self.backend.options(**arguments)
            elif action in {"content-restore", "content-discard"}:
                if (not isinstance(payload["contentId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", payload["contentId"])
                        or type(payload["contentVersion"]) is not int or payload["contentVersion"] < 1
                        or not isinstance(payload["requestKey"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", payload["requestKey"])):
                    raise ValueError("invalid retained content request")
                method = self.backend.restore_content if action == "content-restore" else self.backend.discard_content
                value = method(**arguments, content_id=payload["contentId"], content_version=payload["contentVersion"],
                    request_key=payload["requestKey"])
            else:
                if not all(isinstance(payload[key], str) and 1 <= len(payload[key]) <= 128
                           for key in ("previewId", "requestKey")):
                    raise ValueError("invalid decision")
                value = self.backend.approve(**arguments,
                    preview_id=payload["previewId"], request_key=payload["requestKey"])
            return self.json(200, value)
        except Exception as error:
            return self._error(error)

    @staticmethod
    def json(status: int, value: dict) -> AuthCardResponse:
        headers = _security_headers(secrets.token_urlsafe(18))
        headers["Content-Type"] = "application/json; charset=utf-8"
        return AuthCardResponse(status, headers, json.dumps(project_draft_state(value), ensure_ascii=False).encode())

    def _error(self, error: Exception) -> AuthCardResponse:
        # Only the backend's intentional user-facing error contract may expose text.
        from agentbridge.core.business_drafts import BusinessDraftError
        if isinstance(error, BusinessDraftError):
            value = {"error": {"code": error.code, "message": error.message,
                               "fieldErrors": error.field_errors}}
            if error.current is not None:
                value["current"] = error.current
            return self.json(error.status, value)
        if isinstance(error, (ValueError, UnicodeDecodeError)):
            return self.json(400, {"error": {"code": "INVALID_REQUEST", "message": "请求字段无效，请检查后重试。"}})
        if isinstance(error, (KeyError, PermissionError)):
            return self.json(403, {"error": {"code": "DRAFT_UNAVAILABLE", "message": "请从原任务重新打开有效办理入口。"}})
        return self.json(503, {"error": {"code": "TEMPORARILY_UNAVAILABLE", "message": "暂时无法读取办理状态，请保留本页后重试。"}})


def _path(source_submission_id: str, presentation_id: str | None) -> str:
    return f"/flow/input/{source_submission_id}" + (f"/present/{presentation_id}" if presentation_id else "")


def _room_control(value: str) -> str:
    return ('<div class="field wide room-field"><span class="field-label" id="room-label">会议室</span>'
            f'<input type="hidden" id="room" name="room" value="{escape(value, quote=True)}">'
            '<p id="room-status" class="room-status" role="status">填写起止时间后查询可用会议室</p>'
            '<div id="room-options" class="room-options" role="radiogroup" aria-labelledby="room-label"></div>'
            '<p id="room-selected" class="room-selected" hidden></p>'
            '<p class="field-error" id="error-room" aria-live="polite"></p></div>')



def _material_script():
    return re.sub(r"import \{renderPdfMaterial\} from ['\"]\./pdf_preview\.mjs['\"];?", "",
                  _MATERIAL_SCRIPT.read_text(encoding="utf-8"))


def _render_page(state: dict, token: str, path: str, nonce: str) -> str:
    ui = state.get("ui") or {"profileId": "taihua.work_log.create", "profileVersion": "1"}
    registered = next((profile for profile in PROFILES if profile.profile_id == ui.get("profileId")), None)
    if registered is None or str(ui.get("profileVersion")) != registered.version:
        raise ValueError("unsupported trusted view")
    kind = registered.view_kind
    if ui.get("viewKind", kind) != kind:
        raise ValueError("inconsistent trusted view")
    legacy = UI_PROFILES.get(registered.profile_id, {})
    definitions = ui.get("fields") or legacy.get("schema", {}).get("fields", [])
    # Presentation only: preserve the frozen names, allowed values and requiredness.
    definitions = [{**field, "control": "segmented"} if registered.review_kind == "meeting_reply"
                   and field.get("control") == "select" and 1 < len(field.get("options", [])) <= 3
                   else field for field in definitions]
    if {field["name"] for field in definitions} != set(registered.fields):
        raise ValueError("inconsistent trusted fields")
    if kind == "meeting_room":
        order = ("purpose", "subject", "start_time", "end_time", "room")
        definitions = sorted(definitions, key=lambda field: order.index(field["name"]) if field["name"] in order else len(order))
    title = str(ui.get("title") or registered.title)
    effect = str(ui.get("effect") or legacy.get("schema", {}).get("effect") or "执行已核对的本次操作")
    submit_label = registered.submit_label
    system_name = str(ui.get("systemName") or legacy.get("schema", {}).get("system") or "业务系统")
    lead = legacy.get("lead") or ("核对下方原始材料并填写本次意见。检查完成后，再明确确认本次操作。"
        if kind == "approval" else "填写后自动保存和检查。请核对实际办理内容及执行后果，再确认本次操作。")
    controls = {}
    differences = []
    for field in definitions:
        name = field["name"]
        raw = state["values"].get(name)
        value = "" if raw is None else str(raw)
        control = (_room_control(value) if kind == "meeting_room" and name == "room" else _render_control(field, value))
        control = control.replace(f'name="{name}"', f'name="{name}" aria-describedby="error-{name}"', 1)
        error_node = f'<p class="field-error" id="error-{name}" aria-live="polite"></p>'
        if field.get("control") in ("text", "textarea") and field.get("max_length"):
            error_node += f'<p class="field-count" id="count-{name}"></p>'
        controls[name] = (control if kind == "meeting_room" and name == "room" else
                         control.replace("</fieldset>", error_node + "</fieldset>") if field["control"] == "segmented" else
                         control.replace("</div>", error_node + "</div>", 1))
        differences.append(f'<div class="draft-difference" id="conflict-row-{name}"><h3>{escape(field["label"])}</h3>'
            f'<div><p>本页输入</p><pre id="conflict-local-{name}"></pre></div>'
            f'<div><p>另一页面的版本</p><pre id="conflict-server-{name}"></pre></div></div>')
    config = json.dumps({"state": state, "csrfToken": token, "apiUrl": path + "/draft",
                        "fieldNames": [field["name"] for field in definitions], "fields": definitions, "viewKind": kind},
                        ensure_ascii=False).replace("<", "\\u003c")
    has_evidence = kind == "approval" or bool(ui.get("review"))
    form_controls = _group_controls(controls, kind=kind, review_kind=ui.get("reviewKind", registered.review_kind))
    batch = state.get("batch") or {}
    batch_html = (f'<p class="draft-batch">批次第 {int(batch["ordinal"])} / {int(batch["totalCount"])} 项'
                  ' · 本次确认仅处理当前项，其他项仍需分别核对。</p>'
                  if type(batch.get("ordinal")) is int and type(batch.get("totalCount")) is int
                  and 0 < batch["ordinal"] <= batch["totalCount"] else "") + '<section id="draft-batch-list" hidden></section>'
    evidence = _render_review(ui.get("review"), element_id="draft-evidence-title") if has_evidence else ""
    review_body = ('<dl><dt>实际项目</dt><dd id="review-project"></dd><dt>项目编码</dt><dd id="review-project-code"></dd>'
                   '<dt>重复检查</dt><dd id="review-duplicate"></dd><dt>执行后果</dt><dd id="review-effect"></dd></dl>'
                   '<details><summary>查看将提交的日志正文</summary><pre id="review-content"></pre></details>'
                   if kind == "work_log" else
                   '<dl id="review-fields"></dl><p class="review-effect" id="review-effect"></p>')
    body = f"""
<style nonce="{nonce}">
  [hidden] {{display:none!important}} .draft-card {{width:760px}} .draft-lead {{color:var(--muted);font-size:14px;line-height:1.6}}
  .draft-card button:disabled,.draft-card input:disabled,.draft-card select:disabled,.draft-card textarea:disabled {{background:var(--disabled-bg);color:var(--disabled-ink);opacity:1;cursor:not-allowed}}
  .draft-card .field:has(#project) {{grid-column:1/-1}} .draft-status {{margin:18px 0 10px;color:var(--teal-dark);font-size:13px;line-height:1.6}}
  .draft-fields {{margin:24px 0}} .draft-fields h2 {{font-size:16px;margin:0 0 14px}} .draft-fields .form-grid {{margin-top:0}}
  .draft-outcome,.draft-batch {{padding:12px 14px;background:var(--subtle-bg);color:var(--subtle-ink);border-left:3px solid var(--teal);font-size:13px;line-height:1.6}}
  .draft-outcome strong {{display:block;margin-bottom:4px}} .material-excerpt {{font-size:13px;color:var(--muted);line-height:1.7;white-space:pre-wrap}}
  .draft-review {{margin:20px 0;background:var(--subtle-bg);color:var(--subtle-ink);--ink:var(--subtle-ink);--muted:var(--subtle-muted);border:1px solid var(--line);border-radius:6px;padding:18px}}
  .draft-review h2,.draft-conflict h2 {{font-size:17px;margin:0 0 12px}} .draft-review dl {{margin:0;display:grid;grid-template-columns:110px 1fr;gap:10px}}
  .draft-review dt {{color:var(--muted);font-size:13px}} .draft-review dd {{margin:0;overflow-wrap:anywhere;font-size:14px;line-height:1.6;white-space:pre-wrap}}
  .draft-review details {{margin-top:12px;font-size:13px}} .draft-review pre,.draft-difference pre {{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.6;margin:6px 0 0}}
  .draft-conflict {{padding:18px;margin:18px 0;border:1px solid var(--warning-border);background:var(--warning-bg);color:var(--warning-ink);border-radius:6px}}
  .draft-conflict>p {{font-size:13px;line-height:1.6}} .draft-difference {{display:grid;grid-template-columns:1fr 1fr;gap:12px;border-top:1px solid var(--warning-border);padding:12px 0}}
  .draft-difference h3 {{grid-column:1/-1;margin:0;font-size:13px}} .draft-difference p {{color:var(--muted);margin:0;font-size:12px}}
  .draft-difference pre {{max-height:160px;overflow:auto;font-size:13px}} .draft-actions {{display:flex;gap:10px;flex-wrap:wrap}}
  .draft-actions button {{width:auto;flex:1;font-size:13px}} .draft-card button.secondary {{background:var(--secondary-bg);color:var(--secondary-ink);border:1px solid var(--line)}}
  .draft-card button.secondary:disabled {{background:var(--disabled-bg);color:var(--disabled-ink)}}
  .field-error {{margin:5px 0 0;color:var(--red);font-size:12px}} #draft-retry {{margin-top:10px}}
  .room-field {{grid-column:1/-1}} .room-status,.room-selected {{font-size:13px;line-height:1.6;margin:8px 0;color:var(--muted)}}
  .room-selected {{color:var(--warning-ink)}} .room-options {{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}}
  .room-option {{display:flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:6px;padding:14px;cursor:pointer;background:var(--field-bg);color:var(--field-ink)}}
  .room-option:has(input:checked) {{border-color:var(--selected-border);background:var(--selected-bg);color:var(--selected-ink)}} .room-option:has(input:focus-visible) {{outline:2px solid var(--teal);outline-offset:2px}}
  .room-option:has(input:disabled) {{background:var(--disabled-bg);color:var(--disabled-ink);opacity:1;cursor:not-allowed}} .room-option input {{width:auto;margin:0;accent-color:var(--teal)}}
  .room-option strong {{display:block;font-size:14px}} .room-option small {{display:block;color:var(--muted);font-size:12px;margin-top:5px}}
  .draft-material-change {{margin:0 0 18px;border:1px solid var(--warning-border);border-radius:6px;padding:16px;background:var(--warning-bg);color:var(--warning-ink);font-size:14px;line-height:1.6}}
  .draft-material-change strong {{display:block;margin-bottom:4px}} .draft-evidence {{margin-top:20px}}
  .material-body {{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.7;margin:14px 0 0;max-height:480px;overflow:auto}}
  .material-primary-grid {{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}}
  .material-table {{overflow-x:auto;margin-top:14px}} .material-table table {{border-collapse:collapse;width:100%;font-size:13px}}
  .material-table th,.material-table td {{padding:10px;text-align:left;vertical-align:top;border:1px solid var(--line);white-space:pre-wrap;min-width:100px}}
  .material-table th {{background:var(--subtle-bg);color:var(--subtle-ink);font-weight:600}} .material-attachments {{margin:18px 0}}
  .material-attachments h3 {{font-size:14px}} .material-attachments li {{overflow-wrap:anywhere;line-height:1.7}}
  .review-focus {{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:10px;margin:16px 0}}
  .review-focus>div {{min-width:0;padding:12px;background:var(--subtle-bg);color:var(--subtle-ink);border-radius:6px}}
  .review-focus h3,.material-directory h3 {{font-size:14px;margin:0 0 8px}}
  .review-focus p {{font-size:13px;line-height:1.7;margin:6px 0;overflow-wrap:anywhere;white-space:pre-wrap}}
  .review-missing {{color:var(--muted)}} .review-focus a,.material-directory a,.review-row a {{color:var(--teal-dark);text-underline-offset:3px}}
  .material-directory {{border:1px solid var(--line);padding:12px;margin:16px 0;border-radius:6px}}
  .material-directory>div {{display:flex;gap:8px 16px;flex-wrap:wrap}} .material-directory a {{font-size:13px;line-height:1.8;overflow-wrap:anywhere}}
  .material-row-index {{margin-top:12px;font-size:13px}} .material-row-index>div {{display:flex;flex-direction:column;gap:8px;max-height:240px;overflow:auto;padding:10px 0}}
  .material-row-index a {{color:var(--teal-dark);overflow-wrap:anywhere}} .material-table tr:focus {{outline:2px solid var(--teal);outline-offset:-2px}}
  [id^="review-section-"],[id^="review-field-"],[id^="review-group-"],[id^="material-preview-"] {{scroll-margin-top:var(--draft-nav-clearance,100px)}}
  .draft-material-layout>* {{min-width:0}}
  @media(min-width:1100px) {{.draft-card:has(.material-side-by-side #draft-material-previews:not([hidden])) {{width:1120px;max-width:100%}}
    .material-side-by-side:has(#draft-material-previews:not([hidden])) {{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;align-items:start}}
    .material-side-by-side #draft-material-previews {{position:sticky;top:var(--draft-nav-clearance,100px);max-height:calc(100vh - var(--draft-nav-clearance,100px) - 20px);overflow:auto;padding-right:4px;scroll-padding-top:10px}}}}
  .material-attachments p,.material-completeness {{font-size:12px;line-height:1.6;color:var(--muted)}}
  .review-expense .review-key-facts,.review-attendance .review-key-facts {{background:var(--subtle-bg);color:var(--subtle-ink);padding:14px;border-radius:6px}}
  .draft-nav {{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0;position:sticky;top:0;z-index:2;background:var(--paper,#fff);padding:10px 0;border-bottom:1px solid var(--line)}}
  .draft-nav a {{padding:9px 13px;border-radius:5px;background:var(--selected-bg);color:var(--selected-ink);font-size:13px;text-decoration:none}}
  #draft-form,#draft-evidence,#card-title {{scroll-margin-top:var(--draft-nav-clearance,100px)}}
  .draft-expiry,.field-count,#draft-submit-hint {{font-size:12px;color:var(--muted);line-height:1.6}}
  .draft-expiry.urgent {{color:var(--warning-ink);background:var(--warning-bg);padding:10px}}
  .draft-content {{margin:16px 0;padding:14px;background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:12px}}
  .draft-content p {{font-size:13px;line-height:1.7;overflow-wrap:anywhere}}
  .draft-content .draft-actions {{display:flex;flex-wrap:wrap;gap:10px}}
  .business-batch-list {{margin:14px 0;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--ink)}}
  .business-batch-items {{padding-left:22px;line-height:1.8}} .business-batch-items [aria-current="step"] {{font-weight:700}}
  .material-pdf-search {{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0}}
  .material-pdf-search input {{min-width:0;flex:1 1 14em}} .material-pdf-search p,.material-pdf-search>div {{width:100%;font-size:13px;line-height:1.6}}
  .material-pdf-search-result {{width:100%!important;text-align:left;white-space:normal;overflow-wrap:anywhere}}
  .material-pdf-text {{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:8px}}
  .material-pdf-text-body {{max-height:60vh;overflow:auto;line-height:1.75;user-select:text;font-size:14px}}
  .material-pdf-display {{background:#fff;color:#17201d;border:1px solid var(--line)}}
  .draft-content-confirmation {{margin-top:14px;padding:14px;border:2px solid var(--teal);border-radius:10px;background:var(--paper);color:var(--ink)}}
  .draft-submit-bar {{position:sticky;bottom:0;background:var(--paper,#fff);border-top:1px solid var(--line);padding:12px 0 max(12px,env(safe-area-inset-bottom));z-index:3}}
  #draft-submit-hint {{display:block;margin-bottom:8px}} .draft-submit-bar button {{margin:0}}
  .draft-field-summary {{padding:12px;border:1px solid var(--warning-border);background:var(--warning-bg);color:var(--warning-ink);border-radius:6px;font-size:13px;line-height:1.8}}
  .draft-field-summary a {{color:var(--teal-dark)}} .material-reading-toggle {{width:auto!important;font-size:12px!important;margin:10px 0!important;padding:8px 12px!important}}
  .material-body.expanded {{max-height:none}} #draft-material-diff {{font-size:13px}} #draft-material-diff pre {{white-space:pre-wrap;overflow-wrap:anywhere;max-height:160px;overflow:auto}}
  .material-preview-item {{border:1px solid var(--line);border-radius:8px;padding:14px;margin:14px 0}} .material-preview-item h3 {{font-size:15px;margin:0 0 8px;overflow-wrap:anywhere}}
  .material-preview-actions {{display:flex;flex-wrap:wrap;gap:8px}} .material-preview-actions button,.material-preview-archive-item button {{width:auto!important;margin:4px 0!important;padding:9px 12px!important;font-size:13px!important}}
  .material-preview-status,.material-preview-note {{font-size:12px;line-height:1.7;color:var(--muted)}} .material-preview-content {{overflow-wrap:anywhere}}
  .material-preview-image-frame {{max-height:70vh;overflow:auto}} .material-preview-image {{display:block;max-width:100%;height:auto;transform-origin:top left}}
  .material-preview-pdf {{width:100%;min-height:520px;border:1px solid var(--line)}} .material-preview-archive-item {{padding:12px 0;border-top:1px solid var(--line)}}
  .oa-query-history {{padding:12px;border:1px solid var(--line)}}
  @media(max-width:620px) {{.draft-review dl {{grid-template-columns:85px 1fr}} .draft-difference,.material-primary-grid {{grid-template-columns:1fr}}}}
</style>
<main class="shell"><section class="card draft-card" aria-labelledby="card-title">
  <header class="card-header"><div class="brand-mark" aria-hidden="true"><span></span></div><div>
    <p class="eyebrow">AGENTBRIDGE · 可信办理</p><h1 id="card-title">{escape(title)}</h1><p class="system-name">{escape(system_name)}</p>
  </div></header>
  <div id="draft-material-change" class="draft-material-change" role="alert" hidden><strong>材料已更新，请重新核对</strong>下方已显示最新申请材料。本次尚未提交，请核对变化后再次确认。<div id="draft-material-diff"></div></div>
  <p class="draft-lead">{escape(lead)}</p>
  <p id="draft-expiry" class="draft-expiry" role="status"></p>
  <nav class="draft-nav" aria-label="办理页导航"><a href="#card-title">事项概览</a><a id="draft-evidence-link" href="#draft-evidence"{' hidden' if not has_evidence else ''}>核对材料</a><a href="#draft-form">填写与确认</a></nav>
  {batch_html}<p class="draft-outcome"><strong>本次确认后的操作</strong>{escape(effect)}</p>
  <div class="draft-material-layout{' material-side-by-side' if ui.get('reviewKind') in {'efficiency_data', 'quarterly_performance'} else ''}">
    <div id="draft-evidence" class="draft-evidence"{' hidden' if not has_evidence else ''}>{evidence}</div>
    <section id="draft-material-previews" aria-label="材料文件预览" hidden></section>
  </div>
  <form id="draft-form" method="post" action="{escape(path)}">
    <section id="draft-content" class="draft-content" aria-label="保留的填写内容" hidden>
      <strong>填写内容与办理有效期</strong><p id="draft-content-saved"></p><p id="draft-content-candidate"></p>
      <div class="draft-actions"><button id="draft-content-restore" class="secondary" type="button" hidden>恢复填写内容并重新检查</button><button id="draft-content-discard" class="secondary" type="button" hidden>删除此事项的保留内容</button></div>
      <section id="draft-content-confirmation" class="draft-content-confirmation" aria-labelledby="draft-content-confirm-title" aria-describedby="draft-content-confirm-message" hidden>
        <strong id="draft-content-confirm-title"></strong><p id="draft-content-confirm-message" role="status"></p>
        <div class="draft-actions"><button id="draft-content-cancel" class="secondary" type="button">取消</button><button id="draft-content-confirm" class="secondary" type="button">确认</button></div>
      </section>
    </section>
    <div id="draft-field-summary" class="draft-field-summary" hidden></div>{form_controls}
    <p id="draft-status" class="draft-status" role="status" aria-live="polite">正在读取草稿</p>
    <p id="draft-error" class="form-error" role="alert" hidden></p>
    <section id="draft-conflict" class="draft-conflict" aria-labelledby="conflict-title" hidden>
      <h2 id="conflict-title">另一页面更新了这份草稿</h2><p>本页输入已保留。请核对以下差异，再选择要继续使用的内容。</p>
      {''.join(differences)}<div class="draft-actions">
        <button id="draft-use-server" type="button" class="secondary">采用另一页面的版本</button>
        <button id="draft-keep-local" type="button" class="secondary">按本页内容更新草稿</button>
      </div>
    </section>
    <section id="draft-review" class="draft-review" aria-labelledby="review-title" hidden>
      <h2 id="review-title">核对本次操作</h2>{review_body}
    </section>
    <div class="draft-submit-bar"><span id="draft-submit-hint"></span><button id="draft-submit" data-agentbridge-action-label="{escape(submit_label)}" type="submit" disabled>{escape(submit_label)}</button></div>
    <button id="draft-retry" type="button" class="secondary" hidden>重新检查</button>
  </form>
  <footer>确认后的操作：{escape(effect.rstrip('。'))}。草稿仅保存在可信办理服务中；页面到期后，请从原任务重新打开。</footer>
  <noscript><p class="notice">此办理页需要 JavaScript。请启用后刷新；页面不会自动提交。</p></noscript>
</section></main>
<script type="module" nonce="{nonce}">
{_PDF_SCRIPT.read_text(encoding='utf-8')}
{_material_script()}
{_BATCH_SCRIPT.read_text(encoding='utf-8')}
{_SCRIPT.read_text(encoding='utf-8').replace("import {renderBatchChecklist} from './batch_view.mjs';", "")}
mountBusinessDraft({config}, document, window);
</script>"""
    return _document(title=title, nonce=nonce, body=body, close_when_complete=False)
