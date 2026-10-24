"""Private text retention, separate from short-lived business execution authority.

Only field values are encrypted here. A retained copy never carries a preview,
authorization, cookie or execution permit; an active source must authorize each
restore independently and re-read its business materials before values return.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import secrets

from agentbridge.core.business_draft_profiles import profile_for_source


RETENTION_HOURS = 24
MAX_CONTENTS_PER_SUBJECT = 50
_PURPOSE = b"agentbridge.private-business-content.v1\x00"
_FIELD_CONTRACT_KEYS = frozenset({"name", "control", "required", "max_length", "minimum", "maximum", "step", "value_type", "options"})


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(value):
    return "sha256:" + hashlib.sha256(_json(value).encode()).hexdigest()


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS private_business_draft_contents (
            content_id TEXT PRIMARY KEY,
            user_subject TEXT NOT NULL, system_id TEXT NOT NULL, principal_hash TEXT NOT NULL,
            profile_id TEXT NOT NULL, profile_version TEXT NOT NULL, schema_hash TEXT NOT NULL,
            source_task_id TEXT NOT NULL, source_submission_id TEXT UNIQUE NOT NULL,
            source_revision INTEGER NOT NULL, scope_kind TEXT NOT NULL, scope_hash TEXT NOT NULL,
            binding_json TEXT NOT NULL, ciphertext BLOB NOT NULL, version INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, saved_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS private_business_draft_contents_scope
            ON private_business_draft_contents(user_subject,system_id,principal_hash,profile_id,schema_hash,scope_hash);
        CREATE INDEX IF NOT EXISTS private_business_draft_contents_retention
            ON private_business_draft_contents(user_subject,updated_at,content_id);
        CREATE INDEX IF NOT EXISTS private_business_draft_contents_expiry
            ON private_business_draft_contents(expires_at);
    """)


def cleanup(connection, now):
    connection.execute("DELETE FROM private_business_draft_contents WHERE expires_at<=? OR source_submission_id IN "
        "(SELECT source_submission_id FROM business_draft_decisions) OR source_submission_id IN "
        "(SELECT submission_id FROM field_submissions WHERE state IN ('submitted','consumed'))", (now.isoformat(),))


def schema_hash(source):
    profile = profile_for_source(source)
    schema = source["form_schema"]
    return _hash({"profile": profile.marker, "schemaVersion": profile.schema_version,
        "prepareVersion": source["capability_version"],
        "fields": [{key: value for key, value in field.items() if key in _FIELD_CONTRACT_KEYS
                    and not (key == "options" and profile.view_kind == "meeting_room" and field["name"] == "room")}
            for field in schema["fields"] if field["name"] in profile.fields],
        "constraints": schema.get("constraints") or []})


def _aad(row):
    # The purpose differs from every session/cookie store. All ownership,
    # version and lifetime metadata is authenticated along with the bytes.
    return _PURPOSE + _json({key: row[key] for key in (
        "content_id", "user_subject", "system_id", "principal_hash", "profile_id", "profile_version", "schema_hash",
        "source_task_id", "source_submission_id", "source_revision", "scope_kind", "scope_hash", "binding_json",
        "version", "created_at", "updated_at", "saved_at", "expires_at")}).encode()


def _metadata(row):
    return {"contentId": row["content_id"], "version": row["version"],
        "savedAt": row["saved_at"], "expiresAt": row["expires_at"]}


class BusinessContentDrafts:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _error(code, message):
        from agentbridge.core.business_drafts import BusinessDraftError
        return BusinessDraftError(code, message, status=409)

    def context(self, connection, source, draft):
        """Derive an owned retention scope; this is never execution authority."""
        from agentbridge.core.business_content_identity import principal_binding, validated_identity
        profile = profile_for_source(source)
        session = connection.execute("SELECT * FROM sessions WHERE session_id=? AND user_subject=? AND system_id=?",
            (source["session_id"], source["user_subject"], source["system_id"])).fetchone()
        if not session or session["state"] != "active":
            raise self._error("DRAFT_CONTENT_IDENTITY_UNAVAILABLE", "请先恢复原业务账号，再查看保留内容。")
        principal = principal_binding(dict(session))
        frozen = source["form_schema"].get("_agentbridge_content_principal")
        binding = json.loads(draft["binding_json"])
        if not frozen:
            raise self._error("DRAFT_CONTENT_LEGACY_SOURCE", "旧版页面未绑定保留内容，请使用原任务的新办理页面。")
        if not principal or principal != frozen:
            raise self._error("DRAFT_CONTENT_IDENTITY_UNAVAILABLE", "业务账号尚未核对，不能保存或恢复跨页面内容。")
        task = self.service.tasks._select_owned_task(connection, draft["task_id"], source["user_subject"])
        if task["status"] in {"outcome_unknown", "succeeded", "canceled"}:
            raise self._error("DRAFT_CONTENT_TASK_STOPPED", "原任务已结束，保留内容不会作为新的办理输入。")
        identity = validated_identity(profile, source["form_schema"].get("_agentbridge_content_identity"))
        recovery = json.loads(task["summary_json"]).get("oaPendingRecovery") or binding.get("recoveryPermitHash")
        # Recovery always starts blank and retains its own later edits only in
        # this child. Missing/legacy identities and creation forms are local to
        # an exact task step, never inferred from a title or a template.
        scope = ({"kind": "instance", "identity": identity} if identity and not recovery else
            {"kind": "task", "taskId": draft["task_id"],
             "arguments": source["form_schema"].get("_agentbridge_resume_arguments") or {},
             "plan": binding.get("plan"), "recovery": bool(recovery)})
        return {"user_subject": source["user_subject"], "system_id": source["system_id"],
            "principal_hash": principal, "profile_id": profile.profile_id, "profile_version": profile.version,
            "schema_hash": _hash({"schema": schema_hash(source), "commitVersion": binding["commitVersion"]}),
            "scope_kind": scope["kind"], "scope_hash": _hash(scope),
            "binding_json": _json(scope)}

    def _rows(self, connection, source, draft, *, now):
        context = self.context(connection, source, draft)
        rows = connection.execute("SELECT c.* FROM private_business_draft_contents c "
            "JOIN agent_tasks t ON t.task_id=c.source_task_id AND t.user_subject=c.user_subject "
            "WHERE c.user_subject=? AND c.system_id=? AND c.principal_hash=? AND c.profile_id=? "
            "AND c.profile_version=? AND c.schema_hash=? AND c.scope_hash=? AND c.expires_at>? "
            "AND t.status NOT IN ('outcome_unknown','succeeded','canceled') "
            "AND NOT EXISTS (SELECT 1 FROM business_draft_decisions d WHERE d.source_submission_id=c.source_submission_id) "
            "AND NOT EXISTS (SELECT 1 FROM field_submissions f WHERE f.submission_id=c.source_submission_id AND f.state IN ('submitted','consumed')) "
            "ORDER BY c.updated_at DESC,c.content_id DESC LIMIT ?",
            (*[context[key] for key in ("user_subject", "system_id", "principal_hash", "profile_id", "profile_version", "schema_hash", "scope_hash")],
             now.isoformat(), MAX_CONTENTS_PER_SUBJECT)).fetchall()
        return context, rows

    def view(self, connection, source, draft, *, now):
        result = {"retentionHours": RETENTION_HOURS, "saved": None, "restore": None, "requiresFreshCheck": True}
        if source["state"] != "pending" or draft["state"] in {"expired", "finalized"}:
            return result
        try:
            context, rows = self._rows(connection, source, draft, now=now)
        except Exception as error:
            from agentbridge.core.business_drafts import BusinessDraftError
            if not isinstance(error, BusinessDraftError):
                raise
            return {**result, "reason": error.code}
        current = next((row for row in rows if row["source_submission_id"] == source["submission_id"]), None)
        candidate = next((row for row in rows if row["source_submission_id"] != source["submission_id"]), None)
        if current:
            result["saved"] = _metadata(current)
        if candidate:
            result["restore"] = {**_metadata(candidate), "scope": "same_item" if context["scope_kind"] == "instance" else "same_task"}
        return result

    def save(self, connection, source, draft, values, *, now, retained=None):
        # Old sources lack a source-time principal attestation. Preserve their
        # existing short-lived editing contract without retroactively assigning
        # a current account to a new retained copy.
        if not source["form_schema"].get("_agentbridge_content_principal"):
            return
        context = self.context(connection, source, draft)
        previous = connection.execute("SELECT * FROM private_business_draft_contents WHERE source_submission_id=?",
            (source["submission_id"],)).fetchone()
        row = {**context, "content_id": previous["content_id"] if previous else secrets.token_urlsafe(24),
            "source_task_id": draft["task_id"], "source_submission_id": source["submission_id"], "source_revision": draft["revision"],
            "version": previous["version"] + 1 if previous else 1,
            "created_at": previous["created_at"] if previous else now.isoformat(), "updated_at": now.isoformat(),
            "saved_at": retained["saved_at"] if retained else now.isoformat(),
            "expires_at": retained["expires_at"] if retained else (now + timedelta(hours=RETENTION_HOURS)).isoformat()}
        try:
            ciphertext = self.service.session_states.protector.protect(_json(values).encode(), context=_aad(row))
        except Exception as error:
            raise self._error("DRAFT_CONTENT_SAVE_UNAVAILABLE", "填写内容暂未保存，请保留当前页面后重试。") from error
        columns = list(row) + ["ciphertext"]
        connection.execute("INSERT OR REPLACE INTO private_business_draft_contents (" + ",".join(columns)
            + ") VALUES (" + ",".join("?" for _ in columns) + ")", (*row.values(), ciphertext))
        cleanup(connection, now)
        connection.execute("DELETE FROM private_business_draft_contents WHERE content_id IN "
            "(SELECT content_id FROM private_business_draft_contents WHERE user_subject=? "
            "ORDER BY updated_at DESC,content_id DESC LIMIT -1 OFFSET ?)", (source["user_subject"], MAX_CONTENTS_PER_SUBJECT))

    def candidate(self, connection, source, draft, *, content_id, content_version, now, restoring):
        _, rows = self._rows(connection, source, draft, now=now)
        row = next((row for row in rows if row["content_id"] == content_id), None)
        if (not row or row["version"] != content_version
                or restoring and row["source_submission_id"] == source["submission_id"]):
            raise self._error("DRAFT_CONTENT_CHANGED", "保留内容已变化或不属于当前事项，请重新读取页面。")
        return row

    def values(self, source, row):
        try:
            values = json.loads(self.service.session_states.protector.unprotect(bytes(row["ciphertext"]), context=_aad(row)))
            if (not isinstance(values, dict) or set(values) != set(profile_for_source(source).fields)
                    or any(not isinstance(value, str) or len(value) > 5000 for value in values.values())):
                raise ValueError("invalid retained fields")
        except Exception as error:
            raise self._error("DRAFT_CONTENT_UNAVAILABLE", "保留内容校验失败，未恢复任何字段。") from error
        return values

    def delete_scope(self, connection, source, draft):
        if source["form_schema"].get("_agentbridge_content_principal"):
            context = self.context(connection, source, draft)
            connection.execute("DELETE FROM private_business_draft_contents WHERE user_subject=? AND system_id=? "
                "AND principal_hash=? AND profile_id=? AND profile_version=? AND schema_hash=? AND scope_hash=?",
                tuple(context[key] for key in ("user_subject", "system_id", "principal_hash", "profile_id", "profile_version", "schema_hash", "scope_hash")))
        connection.execute("DELETE FROM private_business_draft_contents WHERE source_submission_id=? AND user_subject=?",
            (source["submission_id"], source["user_subject"]))

    def delete_finalized(self, connection, source, draft):
        # Only exact source and previously attested matching scopes are erased.
        # A decision is never allowed to delete another account's retained text.
        self.delete_scope(connection, source, draft)
