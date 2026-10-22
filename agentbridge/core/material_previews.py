"""Read-only material preview access bound to an existing trusted draft."""
from __future__ import annotations

import json
import re
from datetime import datetime

from agentbridge.adapters.seeyon_material_preview import (
    MaterialPreviewError, fetch_current_material, previews_for_review,
)
from agentbridge.core.business_drafts import BusinessDraftError


class BusinessMaterialPreviewService:
    def __init__(self, draft_service):
        self.drafts = draft_service

    def fetch(self, source_submission_id, *, presentation_id=None, material_id,
              csrf_token, csrf_cookie):
        drafts = self.drafts
        if not isinstance(material_id, str) or not re.fullmatch(r"[a-f0-9]{64}", material_id):
            raise BusinessDraftError("MATERIAL_NOT_FOUND", "材料入口无效。", status=404)

        def checked():
            with drafts.store._connect() as connection:
                source, interaction, task_id, _, draft = drafts._checked(connection,
                    source_submission_id, presentation_id, csrf_token, csrf_cookie)
                if source["system_id"] != "oa" or source["state"] != "pending" or draft["state"] in {"finalized", "expired"}:
                    raise BusinessDraftError("MATERIAL_DRAFT_UNAVAILABLE", "请从当前有效办理页面查看材料。", status=403)
                if presentation_id:
                    presentation = drafts.service.field_submissions._select_presentation(connection, source_submission_id, presentation_id)
                    if presentation["state"] != "active" or drafts.store.clock() >= datetime.fromisoformat(presentation["expires_at"]):
                        raise BusinessDraftError("MATERIAL_DRAFT_UNAVAILABLE", "此材料入口已失效，请从当前任务重新打开。", status=403)
                authority = drafts._authority(connection, source, interaction, task_id)
                # Source and current preview can have different material lists.
                # The rendered page uses the preview after preparation, so use
                # that same frozen review here without updating either record.
                review = source["form_schema"].get("review")
                if draft["preview_id"]:
                    row = connection.execute("SELECT summary_json,expires_at FROM business_draft_previews WHERE preview_id=? AND draft_id=?",
                                             (draft["preview_id"], draft["draft_id"])).fetchone()
                    if row and drafts.store.clock() < datetime.fromisoformat(row["expires_at"]):
                        review = json.loads(row["summary_json"])
                matches = [item for item in previews_for_review(review) if item["id"] == material_id]
                if len(matches) != 1 or matches[0]["state"] != "available":
                    raise BusinessDraftError("MATERIAL_NOT_FOUND", "此材料暂无可核验的预览入口，请在 OA 中查看。", status=404)
                marker = {"authority": authority, "version": draft["version"], "previewId": draft["preview_id"],
                          "material": matches[0], "schemaHash": source["schema_hash"]}
                return source, marker

        source, marker = checked()
        affair_id = source["form_schema"].get("_agentbridge_resume_arguments", {}).get("affair_id")

        def validate():
            _, current = checked()
            if current != marker:
                raise BusinessDraftError("MATERIAL_DRAFT_CHANGED", "办理内容或权限已变化，请刷新后重新查看材料。", status=409)

        def read(adapter, worker, _session):
            try:
                return fetch_current_material(adapter, worker, affair_id=affair_id, expected=marker["material"])
            except MaterialPreviewError as exc:
                raise BusinessDraftError(exc.code, exc.message, status=exc.status) from exc

        # Existing controlled reading owns queueing, encrypted session restore,
        # real downstream principal verification and capability scope.
        result = drafts._read_downstream(source, read, validate=validate)
        validate()  # Never deliver bytes after revocation, expiry or submission.
        return result
