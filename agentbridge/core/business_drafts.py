"""Versioned trusted work-log drafts and atomic, recoverable user decisions.

Previews only read the downstream system. The final decision writes local ledgers
in one transaction; only the existing interaction coordinator can execute it.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
from threading import BoundedSemaphore

from agentbridge.adapters.base import AdapterBusinessRuleRejected, AdapterLoginRequired
from agentbridge.adapters.taihua import TaihuaDuplicateWorkLog
from agentbridge.core.business_draft_profiles import BY_PREPARE, profile_for_source, meeting_times, review_payload, room_choice
from agentbridge.core.capability_runtime import CapabilityContext
from agentbridge.core.field_submissions import _submission_from_row
from agentbridge.core.interactions import _interaction_from_row, build_interaction_envelope
from agentbridge.core.sessions import _session_from_row
from agentbridge.core.store_transactions import store_transaction
from agentbridge.core.write_authorizations import _authorization_from_row
from agentbridge.core.write_catalog import _TRUSTED_WRITE_DEFINITIONS


PREPARE = "taihua.work_log.create.prepare"
COMMIT = "taihua.work_log.create"
FIELDS = ("log_date", "hours", "project", "content")
_PREVIEWS = BoundedSemaphore(2)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


class BusinessDraftError(RuntimeError):
    def __init__(self, code, message, *, status=400, field_errors=None, current=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.field_errors, self.current = field_errors or {}, current


class BusinessDraftStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = _now
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS business_drafts (
                    draft_id TEXT PRIMARY KEY, source_submission_id TEXT UNIQUE NOT NULL,
                    source_interaction_id TEXT NOT NULL, user_subject TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, version INTEGER NOT NULL,
                    state TEXT NOT NULL, values_json TEXT NOT NULL, preview_id TEXT,
                    binding_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS business_draft_previews (
                    preview_id TEXT PRIMARY KEY, draft_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    values_hash TEXT NOT NULL, values_json TEXT NOT NULL, binding_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL, plan_hash TEXT NOT NULL, summary_json TEXT NOT NULL,
                    semantic_review_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_business_draft_preview
                BEFORE UPDATE ON business_draft_previews BEGIN
                    SELECT RAISE(ABORT, 'business draft preview is immutable');
                END;
                CREATE TABLE IF NOT EXISTS business_draft_decisions (
                    receipt_id TEXT PRIMARY KEY, draft_id TEXT UNIQUE NOT NULL,
                    source_submission_id TEXT UNIQUE NOT NULL, user_subject TEXT NOT NULL,
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, preview_id TEXT NOT NULL,
                    request_key TEXT NOT NULL, authorization_id TEXT UNIQUE NOT NULL,
                    interaction_id TEXT UNIQUE NOT NULL, prepare_operation_id TEXT NOT NULL,
                    plan_hash TEXT NOT NULL, semantic_review_hash TEXT NOT NULL,
                    binding_json TEXT NOT NULL, endpoint_id TEXT, created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_business_draft_decision
                BEFORE UPDATE ON business_draft_decisions BEGIN
                    SELECT RAISE(ABORT, 'business draft decision is immutable');
                END;
                CREATE INDEX IF NOT EXISTS business_drafts_expiry ON business_drafts(state, expires_at);
                CREATE TABLE IF NOT EXISTS business_draft_options (
                    draft_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, binding_json TEXT NOT NULL,
                    body_json TEXT NOT NULL, expires_at TEXT NOT NULL
                );
            """)
        self.cleanup()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup(self):
        now = self.clock().isoformat()
        with store_transaction(self) as connection:
            connection.execute("UPDATE business_drafts SET state='expired', values_json='{}', preview_id=NULL, "
                               "version=version+1, updated_at=? WHERE expires_at<=? AND state NOT IN ('finalized','expired')", (now, now))
            connection.execute("DELETE FROM business_draft_previews WHERE expires_at<=? OR draft_id IN "
                               "(SELECT draft_id FROM business_drafts WHERE state IN ('finalized','expired'))", (now,))
            connection.execute("DELETE FROM business_draft_options WHERE expires_at<=? OR draft_id IN "
                               "(SELECT draft_id FROM business_drafts WHERE state IN ('finalized','expired'))", (now,))


class BusinessDraftService:
    def __init__(self, service):
        self.service, self.store = service, service.business_drafts

    def has_decision(self, source_submission_id):
        """Entry selection only; callers must authenticate the source separately."""
        with self.store._connect() as connection:
            return connection.execute("SELECT 1 FROM business_draft_decisions WHERE source_submission_id=?",
                                      (source_submission_id,)).fetchone() is not None

    def source_context(self, user_subject, task_id, prepare_capability=PREPARE):
        """Capture constraints before source creation; later absence is not freedom."""
        with self.store._connect() as connection:
            self.service.tasks._select_owned_task(connection, task_id, user_subject)
            self.service.validate_skill_task(user_subject, task_id, capability=BY_PREPARE[prepare_capability].commit, connection=connection)
            skill = self.service.skills.for_task(user_subject, task_id, connection)
            if connection.execute("SELECT 1 FROM task_batches WHERE parent_task_id=?", (task_id,)).fetchone():
                raise BusinessDraftError("DRAFT_BATCH_UNSUPPORTED", "日志单次提交暂不支持批次事项。", status=409)
            row = connection.execute("SELECT * FROM task_plans WHERE parent_task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
            plan_binding = None
            if row:
                plan = self.service.task_plans._snapshot(connection, row)
                self.service.validate_task_plan_execution(plan)
                if not plan["current_step_key"]:
                    raise BusinessDraftError("DRAFT_PLAN_CONTEXT_MISSING", "缺少当前计划步骤。", status=409)
                plan_binding = {"planId": plan["plan_id"], "revision": plan["revision"], "stepKey": plan["current_step_key"]}
            return {"taskId": task_id, "skillBindingHash": _hash(skill) if skill else None, "plan": plan_binding, "batch": None}

    def open(self, source_submission_id, *, presentation_id=None, csrf_cookie=""):
        self.store.cleanup()
        with store_transaction(self.store) as connection:
            source, interaction, task_id, endpoint = self._source(connection, source_submission_id, presentation_id)
            draft = connection.execute("SELECT * FROM business_drafts WHERE source_submission_id=?", (source_submission_id,)).fetchone()
            if draft is None:
                if source["state"] != "pending":
                    raise BusinessDraftError("DRAFT_UNAVAILABLE", "该字段版本已提交，请查看原任务。", status=409)
                binding = self._authority(connection, source, interaction, task_id)
                values = {name: "" for name in profile_for_source(source).fields}
                for field in source["form_schema"]["fields"]:
                    if field["name"] in values:
                        values[field["name"]] = str(field.get("value", field.get("default", "")))
                now = self.store.clock().isoformat()
                connection.execute("INSERT INTO business_drafts VALUES (?,?,?,?,?,1,1,'editing',?,NULL,?,?,?,?)",
                    (secrets.token_urlsafe(24), source_submission_id, interaction["interaction_id"], source["user_subject"],
                     task_id, _json(values), _json(binding), now, now, source["expires_at"]))
                draft = self._draft(connection, source_submission_id)
            try:
                self._authenticate(connection, source, presentation_id, endpoint, csrf_cookie, csrf_cookie)
                token = csrf_cookie
            except BusinessDraftError:
                token = secrets.token_urlsafe(32)
                now = self.store.clock().isoformat()
                connection.execute("INSERT INTO field_submission_card_sessions "
                    "(card_session_id,submission_id,presentation_id,endpoint_id,csrf_hash,state,created_at,expires_at,consumed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (secrets.token_urlsafe(24), source_submission_id, presentation_id,
                     endpoint, hashlib.sha256(token.encode()).hexdigest(), "pending" if source["state"] == "pending" else "consumed",
                     now, source["expires_at"], None if source["state"] == "pending" else now))
            return {"state": self._view(connection, draft, presentation_id, endpoint), "csrfToken": token,
                    "maxAge": max(1, int((datetime.fromisoformat(source["expires_at"]) - self.store.clock()).total_seconds()))}

    def state(self, source_submission_id, *, presentation_id=None, csrf_token, csrf_cookie):
        self.store.cleanup()
        with store_transaction(self.store) as connection:
            source, interaction, task_id, endpoint, draft = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
            return self._view(connection, draft, presentation_id, endpoint)

    def save(self, source_submission_id, *, presentation_id=None, csrf_token, csrf_cookie, expected_revision, values):
        with store_transaction(self.store) as connection:
            source, interaction, task_id, endpoint, draft = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
            if (not isinstance(values, dict) or set(values) != set(profile_for_source(source).fields)
                    or any(not isinstance(value, str) or len(value) > 5000 for value in values.values())):
                raise BusinessDraftError("DRAFT_VALUES_INVALID", "请提供完整且有效的文本字段。")
            self._editable(connection, draft, source, expected_revision, presentation_id, endpoint)
            self._authority(connection, source, interaction, task_id)
            old_values = json.loads(draft["values_json"])
            if all(values.get(key) == old_values.get(key) for key in ("start_time", "end_time")):
                connection.execute("UPDATE business_draft_options SET revision=? WHERE draft_id=? AND revision=?",
                                   (draft["revision"] + 1, draft["draft_id"], draft["revision"]))
            connection.execute("UPDATE business_drafts SET revision=revision+1,version=version+1,state='editing', "
                "values_json=?,preview_id=NULL,updated_at=? WHERE draft_id=?", (_json(values), self.store.clock().isoformat(), draft["draft_id"]))
            connection.execute("DELETE FROM business_draft_previews WHERE draft_id=?", (draft["draft_id"],))
            return self._view(connection, self._draft(connection, source_submission_id), presentation_id, endpoint)

    def preview(self, source_submission_id, *, presentation_id=None, csrf_token, csrf_cookie, expected_revision):
        if not _PREVIEWS.acquire(blocking=False):
            raise BusinessDraftError("DRAFT_PREVIEW_BUSY", "预检正在处理其他请求，请稍后重试。", status=429)
        started_version = None
        try:
            with store_transaction(self.store) as connection:
                source, interaction, task_id, endpoint, draft = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
                self._editable(connection, draft, source, expected_revision, presentation_id, endpoint)
                binding = self._authority(connection, source, interaction, task_id)
                values, selection = self._values(connection, source, draft)
                started_version = int(draft["version"]) + 1
                connection.execute("UPDATE business_drafts SET state='previewing',version=?,preview_id=NULL,updated_at=? WHERE draft_id=?",
                                   (started_version, self.store.clock().isoformat(), draft["draft_id"]))
            prepared = self._prepare(source, task_id, values)
            self._check_selection(prepared, selection)
            with store_transaction(self.store) as connection:
                source, interaction, task_id, endpoint, current = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
                self._editable(connection, current, source, expected_revision, presentation_id, endpoint)
                if current["version"] != started_version or binding != self._authority(connection, source, interaction, task_id):
                    self._conflict(connection, current, presentation_id, endpoint)
                self._save_preview(connection, source, current, values, binding, prepared)
                return self._view(connection, self._draft(connection, source_submission_id), presentation_id, endpoint)
        except Exception as exc:
            if started_version is not None:
                with store_transaction(self.store) as connection:
                    connection.execute("UPDATE business_drafts SET state='editing',version=version+1,updated_at=? "
                        "WHERE source_submission_id=? AND version=? AND state='previewing'",
                        (self.store.clock().isoformat(), source_submission_id, started_version))
            if isinstance(exc, BusinessDraftError):
                raise
            if isinstance(exc, AdapterBusinessRuleRejected):
                raise self._read_error(exc) from exc
            if isinstance(exc, AdapterLoginRequired):
                raise BusinessDraftError("DRAFT_SESSION_REQUIRED", "登录已失效，请完成原任务的登录后继续。", status=409) from exc
            if hasattr(exc, "code"):
                raise BusinessDraftError(exc.code, str(exc), status=403) from exc
            raise BusinessDraftError("DRAFT_PREVIEW_UNAVAILABLE", "预检暂不可用，草稿已保留，请稍后重试。", status=503) from exc
        finally:
            _PREVIEWS.release()

    def options(self, source_submission_id, *, presentation_id=None, csrf_token, csrf_cookie, expected_revision):
        from agentbridge.adapters.seeyon_meeting_room_application import meeting_room_application_options
        if not _PREVIEWS.acquire(blocking=False):
            raise BusinessDraftError("DRAFT_PREVIEW_BUSY", "查询正在处理其他请求，请稍后重试。", status=429)
        try:
            with store_transaction(self.store) as connection:
                source, interaction, task_id, endpoint, draft = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
                self._editable(connection, draft, source, expected_revision, presentation_id, endpoint)
                if profile_for_source(source).view_kind != "meeting_room":
                    raise BusinessDraftError("DRAFT_OPTIONS_UNSUPPORTED", "此办理页面没有候选查询。", status=404)
                binding = self._authority(connection, source, interaction, task_id)
                values = json.loads(draft["values_json"])
                times = meeting_times(values)
                previous = self._option_body(connection, draft, include_stale=True)
                started_version = draft["version"] + 1
                connection.execute("UPDATE business_drafts SET version=?,state='editing',preview_id=NULL WHERE draft_id=?",
                                   (started_version, draft["draft_id"]))
            rooms = self._read_downstream(source, lambda adapter, worker, session:
                meeting_room_application_options(adapter, worker, **times))
            with store_transaction(self.store) as connection:
                source, interaction, task_id, endpoint, current = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
                self._editable(connection, current, source, expected_revision, presentation_id, endpoint)
                if current["version"] != started_version or binding != self._authority(connection, source, interaction, task_id):
                    self._conflict(connection, current, presentation_id, endpoint)
                now = self.store.clock()
                expires = min(datetime.fromisoformat(source["expires_at"]), now + timedelta(minutes=5)).isoformat()
                items = [room_choice(room["id"], room["name"]) for room in rooms]
                selected = next((item for item in items if item["value"] == values.get("room")), None)
                if not selected and values.get("room"):
                    old_items = (previous or {}).get("items", []) + ([(previous or {})["selected"]] if (previous or {}).get("selected") else [])
                    old = next((item for item in old_items if item["value"] == values["room"]), {})
                    selected = {"value": values["room"], "label": old.get("label", "原选择"), "id": old.get("id"),
                                "available": False, "reason": "原会议室已不可用或名称已变化，请明确重新选择。"}
                body = {"field": "room", "dependsOn": {key: values[key] for key in ("start_time", "end_time")},
                        "checkedAt": now.isoformat(), "expiresAt": expires, "items": items, "selected": selected}
                connection.execute("INSERT OR REPLACE INTO business_draft_options VALUES (?,?,?,?,?)",
                                   (current["draft_id"], current["revision"], _json(binding), _json(body), expires))
                connection.execute("UPDATE business_drafts SET version=version+1,binding_json=? WHERE draft_id=?", (_json(binding), current["draft_id"]))
                return self._view(connection, self._draft(connection, source_submission_id), presentation_id, endpoint)
        except BusinessDraftError:
            raise
        except Exception as exc:
            raise self._read_error(exc) from exc
        finally:
            _PREVIEWS.release()

    def _option_body(self, connection, draft, *, include_stale=False):
        row = connection.execute("SELECT * FROM business_draft_options WHERE draft_id=?", (draft["draft_id"],)).fetchone()
        if not row:
            return None
        body = json.loads(row["body_json"])
        values = json.loads(draft["values_json"])
        if not include_stale and (row["revision"] != draft["revision"] or self.store.clock() >= datetime.fromisoformat(row["expires_at"])
                                 or body["dependsOn"] != {key: values.get(key) for key in ("start_time", "end_time")}):
            return None
        body.update(revision=row["revision"], version=draft["version"])
        if not include_stale:
            selected = next((item for item in body["items"] if item["value"] == values.get("room")), None)
            if selected:
                body["selected"] = selected
            elif not values.get("room"):
                body["selected"] = None
            elif not body.get("selected") or body["selected"]["value"] != values["room"]:
                body["selected"] = {"value": values["room"], "label": "原选择", "available": False,
                                    "reason": "请从当前空闲会议室中重新选择。"}
        return body

    def _values(self, connection, source, draft):
        values = json.loads(draft["values_json"])
        profile = profile_for_source(source)
        if profile.view_kind == "work_log":
            return self._normalize(values), None
        if profile.view_kind == "approval":
            opinion = values["opinion"].strip()
            if not opinion or len(opinion) > 1000:
                raise BusinessDraftError("DRAFT_FIELDS_INVALID", "请检查标记的字段。", status=422,
                                         field_errors={"opinion": "处理意见不能为空且不能超过 1000 个字符。"})
            return {"opinion": opinion}, None
        times = meeting_times(values)
        options = self._option_body(connection, draft)
        row = connection.execute("SELECT binding_json FROM business_draft_options WHERE draft_id=?", (draft["draft_id"],)).fetchone()
        if not options or not row or row["binding_json"] != draft["binding_json"]:
            raise BusinessDraftError("DRAFT_OPTIONS_STALE", "请按当前时间重新查询空闲会议室。", status=409)
        selection = next((item for item in options["items"] if item["value"] == values["room"] and item["available"]), None)
        if not selection:
            raise BusinessDraftError("DRAFT_FIELDS_INVALID", "请重新选择会议室。", status=422,
                                     field_errors={"room": "请从当前空闲会议室中明确选择。"})
        purpose = values["purpose"].strip()
        if not purpose or len(purpose) > 80:
            raise BusinessDraftError("DRAFT_FIELDS_INVALID", "请检查标记的字段。", status=422,
                                     field_errors={"purpose": "申请用途不能为空且不能超过 80 个字符。"})
        return {"purpose": purpose, "room": selection["label"], **times}, selection

    @staticmethod
    def _check_selection(prepared, selection):
        if selection and (str(prepared["plan"]["target"].get("room_id")) != selection["id"]
                          or prepared["plan"]["target"].get("room_name") != selection["label"]):
            raise BusinessDraftError("DRAFT_TARGET_CHANGED", "会议室身份或名称已变化，请重新查询并明确选择。", status=409)

    def _save_preview(self, connection, source, draft, values, binding, prepared, *, changed=False):
        plan = prepared["plan"]
        summary, semantic = review_payload(profile_for_source(source), prepared)
        if changed:
            summary["reviewChanged"] = True
        now = self.store.clock()
        expires = min(datetime.fromisoformat(source["expires_at"]), now + timedelta(minutes=5)).isoformat()
        preview_id = secrets.token_urlsafe(24)
        connection.execute("INSERT INTO business_draft_previews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (preview_id, draft["draft_id"], draft["revision"], _hash(values), _json(values), _json(binding),
             _json(plan), _hash(plan), _json(summary), _hash(semantic), now.isoformat(), expires))
        connection.execute("UPDATE business_drafts SET state='ready',version=version+1,preview_id=?,binding_json=?,updated_at=? WHERE draft_id=?",
                           (preview_id, _json(binding), now.isoformat(), draft["draft_id"]))
        connection.execute("DELETE FROM business_draft_previews WHERE draft_id=? AND preview_id<>?", (draft["draft_id"], preview_id))

    @staticmethod
    def _read_error(exc):
        if isinstance(exc, BusinessDraftError):
            return exc
        if isinstance(exc, AdapterLoginRequired):
            return BusinessDraftError("DRAFT_SESSION_REQUIRED", "登录已失效，请完成原任务的登录后继续。", status=409)
        if isinstance(exc, AdapterBusinessRuleRejected):
            return BusinessDraftError("DUPLICATE_WORK_LOG" if isinstance(exc, TaihuaDuplicateWorkLog) else "DRAFT_BUSINESS_RULE_REJECTED", str(exc), status=422)
        if hasattr(exc, "code"):
            return BusinessDraftError(exc.code, str(exc), status=403)
        return BusinessDraftError("DRAFT_PREVIEW_UNAVAILABLE", "业务状态暂不可确认，请重新预检。", status=409)

    def _recheck(self, source_id, *, presentation_id, csrf_token, csrf_cookie, expected_revision, preview_id):
        """Network reads happen before the final transaction; CAS closes the gap."""
        with store_transaction(self.store) as connection:
            source, interaction, task_id, endpoint, draft = self._checked(connection, source_id, presentation_id, csrf_token, csrf_cookie)
            if connection.execute("SELECT 1 FROM business_draft_decisions WHERE draft_id=?", (draft["draft_id"],)).fetchone():
                return None
            profile = profile_for_source(source)
            if not profile.recheck:
                return None
            self._editable(connection, draft, source, expected_revision, presentation_id, endpoint)
            preview = connection.execute("SELECT * FROM business_draft_previews WHERE preview_id=? AND draft_id=?", (preview_id, draft["draft_id"])).fetchone()
            if not preview or draft["preview_id"] != preview_id or draft["state"] != "ready" or self.store.clock() >= datetime.fromisoformat(preview["expires_at"]):
                raise BusinessDraftError("DRAFT_PREVIEW_STALE", "预检版本已失效，请重新预检后确认。", status=409)
            binding = self._authority(connection, source, interaction, task_id)
            values, selection = self._values(connection, source, draft)
            if _json(binding) != preview["binding_json"] or _hash(values) != preview["values_hash"]:
                raise BusinessDraftError("DRAFT_CONTEXT_CHANGED", "账号或业务上下文已变化，请重新预检。", status=409)
            version, old_semantic, old_plan = draft["version"], preview["semantic_review_hash"], json.loads(preview["plan_json"])
        if not _PREVIEWS.acquire(blocking=False):
            raise BusinessDraftError("DRAFT_PREVIEW_BUSY", "业务状态正在核对，请稍后重试。", status=429)
        failure = None
        try:
            prepared = self._prepare(source, task_id, values)
            self._check_selection(prepared, selection)
            if profile.view_kind == "approval":
                identity = ("affair_id", "summary_id", "process_id", "template_id", "form_app_id", "form_record_id", "profile")
                if any(old_plan["target"].get(key) != prepared["plan"]["target"].get(key) for key in identity):
                    raise BusinessDraftError("DRAFT_TARGET_CHANGED", "待办身份已变化，请从原任务重新准备。", status=409)
            changed = _hash(review_payload(profile, prepared)[1]) != old_semantic
        except Exception as exc:
            failure = self._read_error(exc)
        finally:
            _PREVIEWS.release()
        with store_transaction(self.store) as connection:
            source, interaction, task_id, endpoint, current = self._checked(connection, source_id, presentation_id, csrf_token, csrf_cookie)
            if connection.execute("SELECT 1 FROM business_draft_decisions WHERE draft_id=?", (current["draft_id"],)).fetchone():
                return None
            self._editable(connection, current, source, expected_revision, presentation_id, endpoint)
            if current["version"] != version or current["preview_id"] != preview_id or binding != self._authority(connection, source, interaction, task_id):
                self._conflict(connection, current, presentation_id, endpoint)
            if failure:
                connection.execute("UPDATE business_drafts SET version=version+1,state='editing',preview_id=NULL WHERE draft_id=?", (current["draft_id"],))
                connection.execute("DELETE FROM business_draft_previews WHERE draft_id=?", (current["draft_id"],))
            elif changed:
                self._save_preview(connection, source, current, values, binding, prepared, changed=True)
            view = self._view(connection, self._draft(connection, source_id), presentation_id, endpoint)
        if failure:
            failure.current = view
            raise failure
        if changed:
            raise BusinessDraftError("DRAFT_REVIEW_CHANGED", "办理材料已变化，请核对新的预检内容后再次明确确认。", status=409, current=view)
        return version

    def approve(self, source_submission_id, *, presentation_id=None, csrf_token, csrf_cookie,
                expected_revision, preview_id, request_key):
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_key):
            raise BusinessDraftError("DRAFT_REQUEST_KEY_INVALID", "提交请求标识无效。")
        checked_version = self._recheck(source_submission_id, presentation_id=presentation_id,
            csrf_token=csrf_token, csrf_cookie=csrf_cookie, expected_revision=expected_revision, preview_id=preview_id)
        with store_transaction(self.store) as connection:
            source, interaction, task_id, endpoint, draft = self._checked(connection, source_submission_id, presentation_id, csrf_token, csrf_cookie)
            receipt = connection.execute("SELECT * FROM business_draft_decisions WHERE draft_id=?", (draft["draft_id"],)).fetchone()
            if receipt:
                if receipt["request_key"] == request_key and receipt["preview_id"] == preview_id and receipt["revision"] == expected_revision:
                    return self._view(connection, draft, presentation_id, endpoint)
                self._conflict(connection, draft, presentation_id, endpoint)
            self._editable(connection, draft, source, expected_revision, presentation_id, endpoint)
            if checked_version is not None and checked_version != draft["version"]:
                self._conflict(connection, draft, presentation_id, endpoint)
            preview = connection.execute("SELECT * FROM business_draft_previews WHERE preview_id=? AND draft_id=?", (preview_id, draft["draft_id"])).fetchone()
            now = self.store.clock()
            if (not preview or draft["state"] != "ready" or draft["preview_id"] != preview_id
                    or preview["revision"] != expected_revision or now >= datetime.fromisoformat(preview["expires_at"])):
                raise BusinessDraftError("DRAFT_PREVIEW_STALE", "预检版本已失效，请重新预检后确认。", status=409,
                                         current=self._view(connection, draft, presentation_id, endpoint))
            binding = self._authority(connection, source, interaction, task_id)
            if _json(binding) != preview["binding_json"] or _hash(self._values(connection, source, draft)[0]) != preview["values_hash"]:
                raise BusinessDraftError("DRAFT_CONTEXT_CHANGED", "账号、任务或业务上下文已变化，请重新预检。", status=409)
            if _hash(json.loads(preview["plan_json"])) != preview["plan_hash"]:
                raise BusinessDraftError("DRAFT_PREVIEW_INVALID", "预检记录校验失败。", status=409)
            ttl = int((datetime.fromisoformat(source["expires_at"]) - now).total_seconds())
            if ttl < 30:
                raise BusinessDraftError("DRAFT_EXPIRED", "办理页面即将过期，请从原任务重新准备。", status=409)
            self.service.field_submissions.submit(source_submission_id, csrf_token=csrf_token, csrf_cookie=csrf_cookie,
                values=json.loads(preview["values_json"]), presentation_id=presentation_id, connection=connection)
            profile = profile_for_source(source)
            operation, _ = self.service.operations.create(user_subject=source["user_subject"], capability_name=profile.prepare,
                capability_version=source["capability_version"], input_summary={"input_submission_id": source_submission_id},
                input_identity=interaction["resume_spec"]["arguments"], idempotency_key="draft-decision:" + draft["draft_id"], connection=connection)
            authorization = self.service.write_authorizations.create(user_subject=source["user_subject"], system_id=source["system_id"],
                session_id=source["session_id"], capability_name=profile.commit, capability_version=binding["commitVersion"],
                prepare_operation_id=operation["operation_id"], supersession_key=source["supersession_key"],
                plan=json.loads(preview["plan_json"]), summary=json.loads(preview["summary_json"]),
                card_base_url=self.service.trusted_card_base_url, ttl_seconds=ttl, connection=connection)
            connection.execute("UPDATE write_authorizations SET state='approved',decided_at=?,updated_at=?,decided_endpoint_id=? WHERE authorization_id=?",
                               (now.isoformat(), now.isoformat(), endpoint, authorization["authorization_id"]))
            authorization = _authorization_from_row(self.service.write_authorizations._select(connection, authorization["authorization_id"]), include_plan=False)
            envelope = self.service._execution_authorization_interaction(authorization, connection=connection)
            record = _interaction_from_row(self.service.interactions._select(connection, envelope["interactionId"]))
            operation = self.service.operations.mark_requires_user_action(operation["operation_id"], code="WRITE_AUTHORIZATION_RECORDED",
                message="用户已在可信草稿页确认并授权提交。", next_action={"type": "resume_interaction", "interaction": envelope}, connection=connection)
            self.service.tasks.link_operation(task_id=task_id, user_subject=source["user_subject"], operation=operation, connection=connection)
            self.service.tasks.link_interaction(task_id=task_id, user_subject=source["user_subject"],
                interaction_record=record, interaction=envelope, connection=connection)
            if binding["plan"]:
                self.service.task_plans.mark_step_waiting(binding["plan"]["planId"], user_subject=source["user_subject"],
                    step_key=binding["plan"]["stepKey"], operation_id=operation["operation_id"],
                    interaction_id=record["interaction_id"], input_hash=operation["input_hash"], connection=connection)
            receipt_id = secrets.token_urlsafe(24)
            connection.execute("INSERT INTO business_draft_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (receipt_id, draft["draft_id"], source_submission_id, source["user_subject"], task_id, expected_revision,
                 preview_id, request_key, authorization["authorization_id"], record["interaction_id"], operation["operation_id"],
                 preview["plan_hash"], preview["semantic_review_hash"], _json(binding), endpoint, now.isoformat()))
            connection.execute("UPDATE business_drafts SET state='finalized',version=version+1,values_json='{}',updated_at=? WHERE draft_id=?",
                               (now.isoformat(), draft["draft_id"]))
            connection.execute("DELETE FROM business_draft_previews WHERE draft_id=?", (draft["draft_id"],))
            connection.execute("DELETE FROM business_draft_options WHERE draft_id=?", (draft["draft_id"],))
            return self._view(connection, self._draft(connection, source_submission_id), presentation_id, endpoint)

    def _prepare(self, source, task_id, values):
        profile = profile_for_source(source)
        def prepare(adapter, worker, session):
            context = CapabilityContext(user_subject=source["user_subject"], request_id="draft-preview",
                operation_id=source["create_operation_id"], spec=self.service.registry.get(profile.prepare), task_id=task_id)
            arguments = {**source["form_schema"].get("_agentbridge_resume_arguments", {}), **values}
            prepared = self.service._controlled_writes().prepare_preview(context=context, session=session,
                adapter=adapter, worker=worker, arguments=arguments, field_submission=source,
                definition=_TRUSTED_WRITE_DEFINITIONS[profile.prepare])
            prepared["plan"]["_business_draft"] = self._plan_marker(source, task_id)
            return prepared
        return self._read_downstream(source, prepare)

    @staticmethod
    def _plan_marker(source, task_id):
        marker = {"schema_version": "v1", "source_submission_id": source["submission_id"], "task_id": task_id}
        if source["form_schema"].get("_agentbridge_business_draft_profile"):
            marker["profile"] = profile_for_source(source).marker
        return marker

    def _read_downstream(self, source, read):
        runtime = self.service._runtime_for_system(source["system_id"])
        if runtime is None:
            raise BusinessDraftError("DRAFT_RUNTIME_UNAVAILABLE", "业务系统暂不可用。", status=503)
        adapter, worker_factory = runtime
        with self.service._session_lock(source["session_id"]):
            session = self.service.sessions.get(source["session_id"])
            if session["state"] != "active":
                raise BusinessDraftError("DRAFT_SESSION_REQUIRED", "请先恢复原任务的登录。", status=409)
            state = self.service.session_states.load(source["session_id"])
            if state is None:
                raise BusinessDraftError("DRAFT_SESSION_REQUIRED", "请先恢复原任务的登录。", status=409)
            with worker_factory(session, adapter) as worker:
                worker.restore_session_state(state)
                probe = getattr(adapter, "probe_business_draft_session", adapter.probe_session)(worker)
                observed = str(probe.get("observed_principal_ref") or "").strip()
                principals = {str(session.get(key) or "").strip() for key in ("expected_principal_ref", "downstream_principal_ref")} - {""}
                if not observed or not principals or any(observed != principal for principal in principals):
                    raise BusinessDraftError("DRAFT_SESSION_CHANGED", "实际登录账号与原任务不一致，请重新登录并预检。", status=409)
                result = read(adapter, worker, session)
                self.service.session_states.save(source["session_id"], worker.capture_session_state())
                return result

    def resume_successor(self, source_submission_id, *, user_subject):
        """Resolve a durable decision; dispatch remains with the original caller."""
        with self.store._connect() as connection:
            receipt = connection.execute("SELECT * FROM business_draft_decisions WHERE source_submission_id=? AND user_subject=?",
                                         (source_submission_id, user_subject)).fetchone()
            if not receipt:
                return None  # Legacy field submission keeps its original prepare contract.
            self._receipt_links(connection, receipt)
            receipt = dict(receipt)
        record, authorization, envelope = self.service._load_interaction(user_subject=user_subject, interaction_id=receipt["interaction_id"])
        if authorization.get("commit_operation_id"):
            return self.service.resume_interaction(user_subject=user_subject, interaction_id=record["interaction_id"])
        return {"protocolVersion": "0.1", "status": "requires_user_action", "operationId": receipt["prepare_operation_id"],
                "interaction": envelope, "taskId": receipt["task_id"],
                "resumedFromInteractionId": json.loads(receipt["binding_json"])["sourceInteractionId"],
                "nextAction": {"type": "resume_interaction", "interaction": envelope}}

    def prepare_successor(self, source_submission_id, user_subject, task_id, *, capability_name=None, arguments=None):
        """Old hosts may invoke the frozen prepare contract instead of resume."""
        with self.store._connect() as connection:
            row = connection.execute("SELECT task_id FROM business_draft_decisions WHERE source_submission_id=? AND user_subject=?",
                                     (source_submission_id, user_subject)).fetchone()
            if not row:
                return None
            if row["task_id"] != task_id:
                raise BusinessDraftError("DRAFT_CONTEXT_CHANGED", "此提交决定属于原任务，不能转移。", status=403)
            if capability_name is not None:
                source = _submission_from_row(self.service.field_submissions._select(connection, source_submission_id), include_values=False)
                context = source["form_schema"].get("_agentbridge_resume_arguments", {})
                if source["capability_name"] != capability_name or any((arguments or {}).get(key) != value for key, value in context.items()):
                    raise BusinessDraftError("DRAFT_CONTEXT_CHANGED", "原业务能力或办理目标不匹配，不能继续此提交决定。", status=403)
        return self.resume_successor(source_submission_id, user_subject=user_subject)

    def guard_consumption(self, connection, authorization_id, user_subject):
        """Consume source fields under the authorization's existing writer lock."""
        receipt = connection.execute("SELECT * FROM business_draft_decisions WHERE authorization_id=? AND user_subject=?",
                                     (authorization_id, user_subject)).fetchone()
        authorization = self.service.write_authorizations._select(connection, authorization_id)
        marker = json.loads(authorization["plan_json"]).get("_business_draft")
        if receipt is None:
            if marker:
                raise BusinessDraftError("DRAFT_DECISION_MISSING", "缺少完整的提交决定，不能执行。", status=403)
            return
        self._receipt_links(connection, receipt)
        binding = json.loads(receipt["binding_json"])
        source = _submission_from_row(self.service.field_submissions._select(connection, receipt["source_submission_id"]), include_values=False)
        profile = profile_for_source(source)
        if marker != self._plan_marker(source, receipt["task_id"]) or authorization["capability_name"] != profile.commit:
            raise BusinessDraftError("DRAFT_DECISION_INVALID", "授权与提交决定不匹配。", status=403)
        task = self.service.tasks._select_owned_task(connection, receipt["task_id"], user_subject)
        if task["status"] not in {"active", "waiting_user", "running"}:
            raise BusinessDraftError("DRAFT_TASK_STOPPED", "原任务已停止，不能执行。", status=409)
        self.service.validate_skill_task(user_subject, receipt["task_id"], capability=profile.commit, connection=connection)
        skill = self.service.skills.for_task(user_subject, receipt["task_id"], connection)
        if binding["skillBindingHash"] != (_hash(skill) if skill else None):
            raise BusinessDraftError("DRAFT_CONTEXT_CHANGED", "业务助手上下文已变化，不能执行。", status=403)
        plan_row = connection.execute("SELECT * FROM task_plans WHERE parent_task_id=? ORDER BY revision DESC LIMIT 1", (receipt["task_id"],)).fetchone()
        if bool(plan_row) != bool(binding["plan"]):
            raise BusinessDraftError("DRAFT_PLAN_CONTEXT_MISSING", "任务计划关联缺失或已变化。", status=403)
        if plan_row:
            expected = binding["plan"]
            step = connection.execute("SELECT * FROM task_plan_steps WHERE plan_id=? AND step_key=?", (expected["planId"], expected["stepKey"])).fetchone()
            if (plan_row["plan_id"] != expected["planId"] or plan_row["revision"] != expected["revision"]
                    or plan_row["current_step_key"] != expected["stepKey"] or not step or step["interaction_id"] != receipt["interaction_id"]):
                raise BusinessDraftError("DRAFT_PLAN_CONTEXT_MISSING", "授权缺少正确计划步骤关联。", status=403)
        if connection.execute("SELECT 1 FROM task_batches WHERE parent_task_id=?", (receipt["task_id"],)).fetchone():
            raise BusinessDraftError("DRAFT_BATCH_UNSUPPORTED", "此决定不能转为批次授权。", status=403)
        source = self.service.field_submissions._select(connection, receipt["source_submission_id"])
        self.service.field_submissions.consume(source["submission_id"], user_subject=user_subject, system_id=source["system_id"],
            session_id=source["session_id"], capability_name=source["capability_name"], capability_version=source["capability_version"],
            consume_operation_id=receipt["prepare_operation_id"], connection=connection)

    def _receipt_links(self, connection, receipt):
        binding = json.loads(receipt["binding_json"])
        for interaction_id, resource_id, kind in ((binding["sourceInteractionId"], receipt["source_submission_id"], "business_input"),
                                                  (receipt["interaction_id"], receipt["authorization_id"], "execution_authorization")):
            row = connection.execute("SELECT i.*,ti.task_id FROM interactions i JOIN task_interactions ti ON ti.interaction_id=i.interaction_id "
                                     "WHERE i.interaction_id=? AND ti.user_subject=?", (interaction_id, receipt["user_subject"])).fetchone()
            if not row or row["resource_id"] != resource_id or row["interaction_type"] != kind or row["task_id"] != receipt["task_id"] or row["user_subject"] != receipt["user_subject"]:
                raise BusinessDraftError("DRAFT_CONTEXT_MISSING", "提交决定缺少原任务交互关联。", status=403)
        row = connection.execute("SELECT a.*,t.task_id FROM write_authorizations a JOIN task_operations t ON t.operation_id=a.prepare_operation_id WHERE a.authorization_id=?",
                                 (receipt["authorization_id"],)).fetchone()
        if (not row or row["task_id"] != receipt["task_id"] or row["prepare_operation_id"] != receipt["prepare_operation_id"]
                or row["plan_hash"] != receipt["plan_hash"] or row["user_subject"] != receipt["user_subject"]):
            raise BusinessDraftError("DRAFT_CONTEXT_MISSING", "提交决定缺少原任务准备关联。", status=403)

    def _source(self, connection, source_id, presentation_id):
        row = self.service.field_submissions._select(connection, source_id)
        self.service.field_submissions._verify_integrity(row, include_values=False)
        source = _submission_from_row(row, include_values=False)
        profile = profile_for_source(source)
        row = connection.execute("SELECT i.*,ti.task_id FROM interactions i JOIN task_interactions ti ON ti.interaction_id=i.interaction_id "
                                 "WHERE i.resource_id=? AND i.interaction_type='business_input' AND ti.user_subject=?",
                                 (source_id, source["user_subject"])).fetchone()
        if not row:
            raise BusinessDraftError("DRAFT_CONTEXT_MISSING", "缺少原任务关联，不能创建提交决定。", status=403)
        interaction, task_id = _interaction_from_row(row), row["task_id"]
        if (any(interaction[key] != source[key] for key in ("user_subject", "system_id", "session_id"))
                or interaction["operation_id"] != source["create_operation_id"]
                or interaction["resume_spec"].get("businessDraft") != "v1"):
            raise BusinessDraftError("DRAFT_CONTEXT_INVALID", "原任务合同不匹配。", status=403)
        try:
            self.service.user_grants.require_capability(source["user_subject"], profile.prepare)
            self.service.user_grants.require_capability(source["user_subject"], profile.commit)
            self.service.validate_skill_task(source["user_subject"], task_id, connection=connection)
        except Exception as exc:
            if isinstance(exc, PermissionError) or hasattr(exc, "code"):
                raise BusinessDraftError(getattr(exc, "code", "DRAFT_ACCESS_DENIED"), str(exc), status=403) from exc
            raise
        endpoint = None
        if presentation_id:
            presentation = self.service.field_submissions._select_presentation(connection, source_id, presentation_id)
            if presentation["user_subject"] != source["user_subject"]:
                raise BusinessDraftError("DRAFT_ACCESS_DENIED", "可信页面不属于此用户。", status=403)
            endpoint = presentation["endpoint_id"]
        if self.store.clock() >= datetime.fromisoformat(source["expires_at"]) or source["state"] in {"expired", "superseded"}:
            raise BusinessDraftError("DRAFT_EXPIRED", "办理页面已过期，请从原任务重新打开。", status=403)
        return source, interaction, task_id, endpoint

    def _authenticate(self, connection, source, presentation_id, endpoint, token, cookie):
        if not isinstance(token, str) or not isinstance(cookie, str):
            raise BusinessDraftError("DRAFT_ACCESS_DENIED", "可信页面会话无效，请重新打开。", status=403)
        row = connection.execute("SELECT * FROM field_submission_card_sessions WHERE submission_id=? AND presentation_id IS ? AND csrf_hash=?",
            (source["submission_id"], presentation_id, hashlib.sha256(token.encode()).hexdigest())).fetchone()
        if (not token or not cookie or not hmac.compare_digest(token, cookie) or not row
                or row["state"] not in {"pending", "consumed"} or row["endpoint_id"] != endpoint
                or self.store.clock() >= datetime.fromisoformat(row["expires_at"])):
            raise BusinessDraftError("DRAFT_ACCESS_DENIED", "可信页面会话无效，请重新打开。", status=403)

    def _checked(self, connection, source_id, presentation_id, token, cookie):
        source, interaction, task_id, endpoint = self._source(connection, source_id, presentation_id)
        self._authenticate(connection, source, presentation_id, endpoint, token, cookie)
        return source, interaction, task_id, endpoint, self._draft(connection, source_id)

    def _authority(self, connection, source, interaction, task_id):
        try:
            return self._authority_unchecked(connection, source, interaction, task_id)
        except BusinessDraftError:
            raise
        except Exception as exc:
            if isinstance(exc, PermissionError) or hasattr(exc, "code"):
                raise BusinessDraftError(getattr(exc, "code", "DRAFT_ACCESS_DENIED"), str(exc), status=403) from exc
            raise

    def _authority_unchecked(self, connection, source, interaction, task_id):
        subject = source["user_subject"]
        task = self.service.tasks._select_owned_task(connection, task_id, subject)
        if task["status"] not in {"active", "waiting_user", "running"}:
            raise BusinessDraftError("DRAFT_TASK_STOPPED", "原任务已停止，不能批准。", status=409)
        operation = connection.execute("SELECT o.*,t.task_id FROM operations o JOIN task_operations t ON t.operation_id=o.operation_id WHERE o.operation_id=?",
                                       (source["create_operation_id"],)).fetchone()
        if not operation or operation["task_id"] != task_id or any(operation[key] != source[key] for key in ("user_subject", "capability_name", "capability_version")):
            raise BusinessDraftError("DRAFT_CONTEXT_MISSING", "准备操作缺少正确任务关联。", status=403)
        if connection.execute("SELECT 1 FROM task_batches WHERE parent_task_id=?", (task_id,)).fetchone():
            raise BusinessDraftError("DRAFT_BATCH_UNSUPPORTED", "此单次提交入口暂不支持批次事项。", status=409)
        profile = profile_for_source(source)
        spec = self.service.registry.get(profile.prepare)
        commit = self.service.registry.get(profile.commit)
        if spec.version != source["capability_version"]:
            raise BusinessDraftError("DRAFT_VERSION_CHANGED", "业务能力版本已变化，请重新准备。", status=409)
        self.service.user_grants.require_capability(subject, profile.prepare)
        self.service.user_grants.require_capability(subject, profile.commit)
        self.service.validate_skill_task(subject, task_id, capability=profile.commit, connection=connection)
        skill = self.service.skills.for_task(subject, task_id, connection)
        for capability in (spec, commit):
            self.service.governance_policies.assert_write_allowed(system_id=source["system_id"], user_subject=subject,
                capability_name=capability.name, capability_version=capability.version)
        session_row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (source["session_id"],)).fetchone()
        if not session_row:
            raise BusinessDraftError("DRAFT_SESSION_REQUIRED", "业务登录不存在，请重新登录。", status=409)
        session = _session_from_row(session_row)
        if session["state"] != "active" or session["user_subject"] != subject or session["system_id"] != source["system_id"]:
            raise BusinessDraftError("DRAFT_SESSION_REQUIRED", "请先恢复原任务的登录。", status=409)
        plan_row = connection.execute("SELECT * FROM task_plans WHERE parent_task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
        plan_binding = None
        if plan_row:
            plan = self.service.task_plans._snapshot(connection, plan_row)
            self.service.validate_task_plan_execution(plan)
            steps = [step for step in plan["steps"] if step.get("interaction_id") == interaction["interaction_id"]]
            if len(steps) != 1 or steps[0]["step_key"] != plan["current_step_key"] or steps[0]["state"] != "waiting_user" or plan.get("commit_operation_id"):
                raise BusinessDraftError("DRAFT_PLAN_CONTEXT_MISSING", "原计划步骤关联不完整或已变化。", status=409)
            plan_binding = {"planId": plan["plan_id"], "revision": plan["revision"], "stepKey": steps[0]["step_key"]}
        source_context = {"taskId": task_id, "skillBindingHash": _hash(skill) if skill else None, "plan": plan_binding, "batch": None}
        if source["form_schema"].get("_agentbridge_business_draft_context") != source_context:
            raise BusinessDraftError("DRAFT_CONTEXT_CHANGED", "原始任务、业务助手或计划关联已变化，不能继续此草稿。", status=403)
        return {"taskId": task_id, "sourceInteractionId": interaction["interaction_id"],
                "prepareVersion": spec.version, "commitVersion": commit.version,
                "session": {key: session.get(key) for key in ("session_id", "expected_principal_ref", "downstream_principal_ref", "last_verified_at")},
                "skillBindingHash": _hash(skill) if skill else None, "plan": plan_binding, "batch": None}

    @staticmethod
    def _normalize(values):
        errors = {}
        try:
            normalized_date = date.fromisoformat(values["log_date"].strip()).isoformat()
        except (ValueError, KeyError):
            normalized_date = ""
            errors["log_date"] = "请选择有效日志日期。"
        try:
            hours = float(values["hours"])
            if not math.isfinite(hours) or not 0.5 <= hours <= 16 or hours * 2 != round(hours * 2):
                raise ValueError()
        except (ValueError, KeyError):
            hours = 0
            errors["hours"] = "工时必须是 0.5 至 16 之间的半小时倍数。"
        content, project = values.get("content", "").strip(), values.get("project", "").strip()
        if not content or len(content) > 4000:
            errors["content"] = "日志内容不能为空且不能超过 4000 个字符。"
        if len(project) > 255:
            errors["project"] = "项目名称或编码不能超过 255 个字符。"
        if errors:
            raise BusinessDraftError("DRAFT_FIELDS_INVALID", "请检查标记的字段。", status=422, field_errors=errors)
        return {"log_date": normalized_date, "hours": int(hours) if hours.is_integer() else hours, "project": project, "content": content}

    @staticmethod
    def _draft(connection, source_id):
        row = connection.execute("SELECT * FROM business_drafts WHERE source_submission_id=?", (source_id,)).fetchone()
        if not row:
            raise BusinessDraftError("DRAFT_NOT_FOUND", "请先打开可信草稿页面。", status=404)
        return row

    def _editable(self, connection, draft, source, revision, presentation_id, endpoint):
        if (not isinstance(revision, int) or isinstance(revision, bool) or draft["revision"] != revision
                or draft["state"] in {"finalized", "expired"} or source["state"] != "pending"):
            self._conflict(connection, draft, presentation_id, endpoint)

    def _conflict(self, connection, draft, presentation_id, endpoint):
        raise BusinessDraftError("DRAFT_REVISION_CONFLICT", "草稿已在另一页面更新或提交，请核对版本。", status=409,
                                 current=self._view(connection, draft, presentation_id, endpoint))

    def _view(self, connection, draft, presentation_id, endpoint):
        source = _submission_from_row(self.service.field_submissions._select(connection, draft["source_submission_id"]), include_values=False)
        profile = profile_for_source(source)
        preview = connection.execute("SELECT * FROM business_draft_previews WHERE preview_id=?", (draft["preview_id"],)).fetchone()
        receipt = connection.execute("SELECT * FROM business_draft_decisions WHERE draft_id=?", (draft["draft_id"],)).fetchone()
        valid = bool(preview and self.store.clock() < datetime.fromisoformat(preview["expires_at"]))
        result = {"draftId": draft["draft_id"], "revision": draft["revision"], "version": draft["version"],
            "state": draft["state"] if draft["state"] != "ready" or valid else "editing", "expiresAt": draft["expires_at"],
            "values": json.loads(draft["values_json"]), "allowedActions": [] if receipt or draft["state"] == "expired" else ["save", "preview"] + (["approve"] if valid else []),
            "preview": None, "receipt": None, "ui": profile.ui(source["form_schema"]),
            "options": self._option_body(connection, draft) if profile.view_kind == "meeting_room" else None}
        if profile.view_kind == "meeting_room" and result["allowedActions"]:
            result["allowedActions"].append("options")
        if result["ui"].get("review"):
            result["ui"]["review"]["snapshot"] = "source"
        if valid:
            summary = json.loads(preview["summary_json"])
            result["preview"] = {"previewId": preview["preview_id"], "revision": preview["revision"], "planHash": preview["plan_hash"],
                "semanticReviewHash": preview["semantic_review_hash"], "expiresAt": preview["expires_at"], "summary": summary,
                "reviewChanged": bool(summary.get("reviewChanged"))}
            if profile.view_kind == "approval":
                result["ui"]["review"] = {"title": summary.get("title", ""), "effect": summary.get("effect", ""),
                    "fields": [field for field in summary.get("fields", []) if field.get("label") not in {"处理意见", "审批意见", "意见"}],
                    "snapshot": "preview", "reviewChanged": bool(summary.get("reviewChanged"))}
        if receipt:
            path = "/flow/authorize/" + receipt["authorization_id"]
            if endpoint:
                presentation = self.service.write_authorizations.create_presentation(receipt["authorization_id"], user_subject=receipt["user_subject"],
                    endpoint_id=endpoint, connection=connection)
                path += "/present/" + presentation["presentation_id"]
            result["receipt"] = {"receiptId": receipt["receipt_id"], "authorizationId": receipt["authorization_id"],
                "interactionId": receipt["interaction_id"], "flowUrl": path, "requestKey": receipt["request_key"]}
        return result
